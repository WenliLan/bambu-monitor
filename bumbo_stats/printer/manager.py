"""打印机管理器 - 通过拓竹云端 MQTT 管理多台打印机的状态订阅"""

import asyncio
import logging
from datetime import datetime
from typing import Callable, Awaitable, Optional

from ..config import PrinterConfig
from ..models import PrinterState, PrinterStatus, HMSError

logger = logging.getLogger(__name__)

# MQTT gcode_state 到 PrinterState 的映射
_STATE_MAP = {
    "IDLE": PrinterState.IDLE,
    "RUNNING": PrinterState.RUNNING,
    "PAUSE": PrinterState.PAUSE,
    "FINISH": PrinterState.FINISH,
    "FAILED": PrinterState.FAILED,
    "OFFLINE": PrinterState.OFFLINE,
    "SUCCESS": PrinterState.FINISH,  # SUCCESS 等同于 FINISH
}

# 状态变更回调类型
StatusChangeFn = Callable[[str, PrinterStatus], Awaitable[None]]


class PrinterManager:
    """通过云端 MQTT 管理多台打印机的状态订阅"""

    def __init__(
        self,
        configs: list[PrinterConfig],
        on_status_change: Optional[StatusChangeFn] = None,
    ):
        self._configs = {c.serial: c for c in configs}
        self._mqtt_clients: dict = {}  # serial -> MQTTClient
        self._status_cache: dict[str, PrinterStatus] = {}
        self._print_data_cache: dict[str, dict] = {}  # serial -> 最后收到的 print 数据（用于合并）
        self._on_status_change = on_status_change
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._uid: str = ""
        self._token: str = ""
        self._region: str = "global"
        self._pushall_task: Optional[asyncio.Task] = None  # 定时 pushall 任务

    @property
    def serials(self) -> list[str]:
        return list(self._configs.keys())

    @property
    def printer_names(self) -> dict[str, str]:
        return {s: c.name for s, c in self._configs.items()}

    def get_config(self, serial: str) -> Optional[PrinterConfig]:
        return self._configs.get(serial)

    async def add_printer(self, config: PrinterConfig):
        """动态添加一台打印机（运行时重载用）

        Args:
            config: 打印机配置
        """
        if config.serial in self._configs:
            logger.info("打印机已存在，跳过添加: %s", config.serial)
            return

        self._configs[config.serial] = config
        logger.info("已添加打印机配置: %s (%s)", config.name, config.serial)

        # 如果已有 MQTT 连接，建立新连接
        if self._uid and self._token:
            from bambulab import MQTTClient
            if self._region == "china":
                MQTTClient.BROKER = "cn.mqtt.bambulab.com"
            else:
                MQTTClient.BROKER = "us.mqtt.bambulab.com"
            try:
                mqtt = MQTTClient(
                    username=self._uid,
                    access_token=self._token,
                    device_id=config.serial,
                    on_message=self._make_callback(config.serial),
                )
                await asyncio.to_thread(mqtt.connect, blocking=False)
                self._mqtt_clients[config.serial] = mqtt
                await asyncio.to_thread(mqtt.request_full_status)
                logger.info("MQTT 已连接: %s", config.serial)
            except Exception as e:
                logger.error("MQTT 连接失败 %s: %s", config.serial, e)

    async def remove_printer(self, serial: str):
        """动态移除一台打印机（运行时重载用）

        Args:
            serial: 打印机序列号
        """
        if serial not in self._configs:
            return

        # 断开 MQTT
        if serial in self._mqtt_clients:
            try:
                await asyncio.to_thread(self._mqtt_clients[serial].disconnect)
            except Exception as e:
                logger.warning("断开 MQTT 失败 %s: %s", serial, e)
            del self._mqtt_clients[serial]

        # 清理缓存
        self._configs.pop(serial, None)
        self._status_cache.pop(serial, None)
        self._print_data_cache.pop(serial, None)
        logger.info("已移除打印机: %s", serial)

    async def connect_all(self, uid: str, token: str, region: str = "global"):
        """连接所有打印机的云端 MQTT

        Args:
            uid: 用户 ID（从拓竹云端获取）
            token: 访问 token
            region: API 区域 "global" 或 "china"
        """
        from bambulab import MQTTClient

        self._uid = uid
        self._token = token
        self._region = region
        self._loop = asyncio.get_running_loop()

        # 设置正确的 MQTT broker（中国区用 cn，全球区用 us）
        if region == "china":
            MQTTClient.BROKER = "cn.mqtt.bambulab.com"
        else:
            MQTTClient.BROKER = "us.mqtt.bambulab.com"

        for serial, config in self._configs.items():
            try:
                mqtt = MQTTClient(
                    username=uid,
                    access_token=token,
                    device_id=serial,
                    on_message=self._make_callback(serial),
                )
                await asyncio.to_thread(mqtt.connect, blocking=False)
                self._mqtt_clients[serial] = mqtt
                # 发送 pushall 请求获取完整状态（包含 layer_num, mc_percent 等）
                await asyncio.to_thread(mqtt.request_full_status)
                logger.info("MQTT 已连接并请求完整状态: %s (%s)", config.name, serial)
            except Exception as e:
                logger.error("MQTT 连接失败 %s: %s", config.name, e)
        
        # 启动定时 pushall 任务（每 60 秒获取完整状态）
        self._pushall_task = asyncio.create_task(self._periodic_pushall())
        logger.info("定时 pushall 任务已启动")

    async def disconnect_all(self):
        """断开所有打印机的 MQTT 连接"""
        # 停止定时 pushall
        if self._pushall_task and not self._pushall_task.done():
            self._pushall_task.cancel()
            self._pushall_task = None
        
        for serial, mqtt in self._mqtt_clients.items():
            try:
                await asyncio.to_thread(mqtt.disconnect)
                logger.info("MQTT 已断开: %s", serial)
            except Exception as e:
                logger.warning("断开 MQTT %s 时出错: %s", serial, e)
        self._mqtt_clients.clear()

    async def _periodic_pushall(self):
        """定时发送 pushall 请求获取完整状态（每 30 秒）"""
        while True:
            await asyncio.sleep(30)  # 每 30 秒请求一次完整状态
            for serial, mqtt in self._mqtt_clients.items():
                try:
                    await asyncio.to_thread(mqtt.request_full_status)
                    # 显示实际状态信息
                    status = self._status_cache.get(serial)
                    if status:
                        logger.info(
                            "状态同步 [%s]: 层 %d/%d 进度 %d%% 状态 %s",
                            serial, status.current_layer, status.total_layers,
                            status.progress, status.state.value
                        )
                except Exception as e:
                    logger.warning("定时 pushall 失败 %s: %s", serial, e)

    async def reconnect_all(self):
        """检查并重连断开的 MQTT 客户端"""
        from bambulab import MQTTClient

        # 重新设置 broker（以防万一）
        if self._region == "china":
            MQTTClient.BROKER = "cn.mqtt.bambulab.com"
        else:
            MQTTClient.BROKER = "us.mqtt.bambulab.com"

        for serial, config in self._configs.items():
            if serial not in self._mqtt_clients:
                try:
                    mqtt = MQTTClient(
                        username=self._uid,
                        access_token=self._token,
                        device_id=serial,
                        on_message=self._make_callback(serial),
                    )
                    await asyncio.to_thread(mqtt.connect, blocking=False)
                    self._mqtt_clients[serial] = mqtt
                    # 重连后也要发送 pushall 请求获取完整状态
                    await asyncio.to_thread(mqtt.request_full_status)
                    logger.info("MQTT 重连成功并请求完整状态: %s", config.name)
                except Exception as e:
                    logger.warning("MQTT 重连失败 %s: %s", config.name, e)

    def get_status(self, serial: str) -> PrinterStatus:
        """获取单台打印机状态（从缓存）

        Args:
            serial: 打印机序列号

        Returns:
            PrinterStatus 实例
        """
        config = self._configs.get(serial)
        if not config:
            return PrinterStatus(serial=serial, name="未知", state=PrinterState.OFFLINE)

        return self._status_cache.get(
            serial,
            PrinterStatus(serial=serial, name=config.name, state=PrinterState.OFFLINE),
        )

    def get_all_statuses(self) -> dict[str, PrinterStatus]:
        """获取所有打印机的当前状态（从缓存）

        Returns:
            serial -> PrinterStatus 的字典
        """
        result = {}
        for serial, config in self._configs.items():
            result[serial] = self._status_cache.get(
                serial,
                PrinterStatus(serial=serial, name=config.name, state=PrinterState.OFFLINE),
            )
        return result

    def _make_callback(self, serial: str):
        """创建 MQTT 消息回调函数（线程安全）

        Args:
            serial: 打印机序列号

        Returns:
            回调函数
        """
        def callback(device_id: str, data: dict):
            config = self._configs.get(serial)
            if not config:
                return

            # 提取并缓存 print 数据（用于合并多批消息）
            print_data = data.get('print', {})
            
            if print_data:
                # 合并：已有数据 + 新数据
                # 注意：gcode_state 只保留旧值（因为新消息可能不包含）
                existing = self._print_data_cache.get(serial, {})
                merged = {**existing, **print_data}
                # 如果新消息没有 gcode_state，保持旧值
                if 'gcode_state' not in print_data and 'gcode_state' in existing:
                    merged['gcode_state'] = existing['gcode_state']
                self._print_data_cache[serial] = merged
                parse_data = merged
            else:
                # 没有 print 数据，使用缓存
                cached = self._print_data_cache.get(serial, {})
                if not cached:
                    return
                parse_data = cached

            # 解析 MQTT 推送数据（使用合并后的数据）
            ams_data = data.get("ams")
            status = self._parse_status(serial, config.name, parse_data, ams_data)
            self._status_cache[serial] = status

            # 触发异步回调（线程安全地调度到 asyncio 事件循环）
            if self._on_status_change and self._loop:
                try:
                    self._loop.call_soon_threadsafe(
                        asyncio.ensure_future,
                        self._on_status_change(serial, status),
                    )
                except RuntimeError:
                    # 事件循环已关闭，忽略
                    pass

        return callback

    def _parse_status(self, serial: str, name: str, print_data: dict,
                      ams_data=None) -> PrinterStatus:
        """从云端 MQTT 推送数据解析 PrinterStatus

        Args:
            serial: 打印机序列号
            name: 打印机名称
            print_data: MQTT 推送的 print 数据（已合并）
            ams_data: AMS 耗材数据（从 MQTT data 中提取）

        Returns:
            PrinterStatus 实例
        """
        # 解析打印机状态
        raw_state = str(print_data.get("gcode_state", "UNKNOWN")).upper()
        state = _STATE_MAP.get(raw_state, PrinterState.UNKNOWN)

        # 解析灯光状态
        lights = print_data.get("lights_report", [])
        light_on = False
        if lights and isinstance(lights, list):
            for light in lights:
                if isinstance(light, dict) and light.get("mode") == "on":
                    light_on = True
                    break

        # 解析 HMS 错误
        hms_raw = print_data.get("hms", [])
        hms_errors = []
        if hms_raw and isinstance(hms_raw, list):
            for hms_item in hms_raw:
                if isinstance(hms_item, dict):
                    hms_errors.append(HMSError(
                        code=hms_item.get("err_code", 0),
                        severity=hms_item.get("severity", ""),
                        message=hms_item.get("err_msg", ""),
                    ))

        # 解析 AMS 耗材状态
        ams_status = dict(ams_data) if ams_data else None

        # 文件名兼容：云端可能用 subtask_name
        gcode_file = str(print_data.get("gcode_file", "") or print_data.get("subtask_name", ""))

        return PrinterStatus(
            serial=serial,
            name=name,
            state=state,
            nozzle_temp=float(print_data.get("nozzle_temper", 0)),
            nozzle_target_temp=float(print_data.get("nozzle_target_temper", 0)),
            bed_temp=float(print_data.get("bed_temper", 0)),
            bed_target_temp=float(print_data.get("bed_target_temper", 0)),
            chamber_temp=float(print_data.get("chamber_temper", 0)),
            progress=int(print_data.get("mc_percent", 0)),
            remaining_time=int(print_data.get("mc_remaining_time", 0)),
            current_layer=int(print_data.get("layer_num", 0)),
            total_layers=int(print_data.get("total_layer_num", 0)),
            gcode_file=gcode_file,
            print_speed=int(print_data.get("spd_lvl", 0)),
            wifi_signal=str(print_data.get("wifi_signal", "")),
            light_on=light_on,
            hms_errors=hms_errors,
            ams_status=ams_status,
            last_updated=datetime.now(),
        )
