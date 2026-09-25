"""AI 异常报警模块 - 定时对 RUNNING 状态打印机截图推理，触发报警通知

架构:
  AIGuardLoop 在后台 asyncio 任务中运行。
  每隔 interval 秒（默认 10 分钟），对每台 RUNNING 状态的打印机：
    - 连续抓取 frames_per_check 帧（间隔 = camera_interval 秒）
    - 每帧推入 PrintDefectDetector.push()
    - 连续 alert_frames 帧异常 → 触发报警
  报警触发后：
    1. 报警图片自动保存到 alert_dir（detector 内置机制）
    2. WebSocket 广播 → 网页弹出通知
    3. WeComNotifier 入队 → 企业微信机器人推送
  每台打印机有独立冷却期（默认 30 分钟），防止重复轰炸。

优雅降级:
  torch 未安装 / 权重缺失 / 模型加载失败时，功能自动禁用，不影响其他模块。
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..config import AIGuardSettings
from ..models import PrinterState

logger = logging.getLogger(__name__)


class AIGuardLoop:
    """AI 异常检测后台任务

    使用方法:
        guard = AIGuardLoop(config, camera_manager, printer_manager, ws_manager, wecom_notifier)
        await guard.start()
        # ... 运行中 ...
        guard.stop()
    """

    def __init__(
        self,
        ai_config: AIGuardSettings,
        camera_manager,
        printer_manager,
        ws_manager,
        wecom_notifier=None,
        feishu_notifier=None,
        camera_interval: float = 5.0,
        on_threshold_changed: Optional[Callable[[float], None]] = None,
    ):
        self._cfg = ai_config
        self._camera = camera_manager
        self._manager = printer_manager
        self._ws_manager = ws_manager
        self._wecom = wecom_notifier
        self._feishu = feishu_notifier
        self._camera_interval = camera_interval
        # 阈值修改回调（持久化到 yaml），传入 None 则仅修改内存值
        self._on_threshold_changed = on_threshold_changed

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._detector = None  # 延迟初始化（可能在子线程加载模型）
        self._init_error: Optional[str] = None

        # 每台打印机上次报警时间戳（冷却用）
        self._last_alarm_ts: dict[str, float] = {}

    async def start(self):
        """启动后台检测任务"""
        if not self._cfg.enabled:
            logger.info("AI 异常报警已禁用")
            return

        # 尝试初始化检测器（在子线程中加载模型，避免阻塞事件循环）
        try:
            await self._init_detector()
        except Exception as e:
            self._init_error = str(e)
            logger.warning("AI 异常报警初始化失败，功能已禁用: %s", e)
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "AI 异常报警已启动: 模型=%s track=%s 阈值=%.2f 间隔=%ds 帧数=%d 报警帧=%d 冷却=%ds",
            self._cfg.model, self._cfg.track,
            self._detector.threshold if self._detector else 0,
            self._cfg.interval, self._cfg.frames_per_check,
            self._cfg.alert_frames, self._cfg.cooldown,
        )

    def stop(self):
        """停止后台任务"""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("AI 异常报警已停止")

    @property
    def is_available(self) -> bool:
        """检测器是否可用"""
        return self._detector is not None and self._init_error is None

    @property
    def threshold(self) -> Optional[float]:
        """当前阈值（检测器未初始化时返回 None）"""
        return self._detector.threshold if self._detector else None

    def set_threshold(self, value: float) -> float:
        """动态修改检测阈值（并持久化到 yaml + meta.json）

        关键修复：原来只更新 ``self._detector.threshold``，但 detect()
        实际读取的是 ``self._detector.gallery.decision_threshold``
        （且 FeatureGallery.__init__ 会从 meta.json 反向覆盖构造参数）。
        这里必须同时：
          1) 改 detector.threshold（供 to_dict() / API 查询返回最新值）
          2) 调 gallery.update_threshold()（实际推理用 + 持久化 meta.json）
          3) 同步 ai_config.threshold（避免热重载时拿到旧值）
          4) 触发 on_threshold_changed 回调（写 yaml）

        Args:
            value: 新阈值（0.0 ~ 1.0），超出范围会被截断

        Returns:
            旧阈值（-1 表示检测器不可用）
        """
        if not self.is_available:
            return -1.0
        old = self._detector.threshold
        clamped = max(0.0, min(1.0, float(value)))
        self._detector.threshold = clamped
        # 关键：同步到 gallery 的 decision_threshold，否则 detect() 用的还是旧值
        gallery = getattr(self._detector, "gallery", None)
        if gallery is not None and hasattr(gallery, "update_threshold"):
            try:
                gallery.update_threshold(clamped)
            except Exception as e:
                logger.error("同步 gallery decision_threshold 失败（detector.threshold 已更新）: %s", e)
        # 同步更新内存中的 ai_config，避免热重载 / API 读取时拿到旧值
        self._cfg.threshold = clamped
        # 触发持久化回调（app.py 注入，将 config 写回 printers.yaml）
        if self._on_threshold_changed is not None:
            try:
                self._on_threshold_changed(clamped)
                logger.info("AI 检测阈值已更新并持久化到 yaml: %.2f -> %.2f", old, clamped)
            except Exception as e:
                logger.error("AI 检测阈值持久化失败（内存值已更新）: %s", e)
        else:
            logger.info(
                "AI 检测阈值已更新（仅内存，重启失效）: %.2f -> %.2f",
                old, clamped,
            )
        return old

    async def detect_single(self, frame: bytes, serial: str) -> dict:
        """手动单帧推理（供 API 调用，不触发报警）

        Args:
            frame: JPEG 帧数据
            serial: 打印机序列号

        Returns:
            {anomaly, probability, threshold, severity, message, ms}
        """
        if not self.is_available:
            raise RuntimeError("AI detector not available")
        result = await asyncio.to_thread(self._detector.detect, frame)
        d = result.to_dict()
        # 保存图片
        self._save_frame(frame, result.probability, serial)
        return d

    async def _init_detector(self):
        """在子线程中初始化检测器（模型加载约 2-3 秒）"""
        def _load():
            import sys
            from pathlib import Path
            # 将 bambu_ai_deploy_dinoknn 目录加入 sys.path
            deploy_dir = str(Path(__file__).parent.parent.parent / "bambu_ai_deploy_dinoknn")
            if deploy_dir not in sys.path:
                sys.path.insert(0, deploy_dir)
            # dino_knn.py 在 baselines/scripts 下, 也是 detector 内部依赖
            dino_knn_dir = str(Path(deploy_dir).parent.parent / "bambu_AI" / "baselines" / "scripts")
            if dino_knn_dir not in sys.path:
                sys.path.insert(0, dino_knn_dir)
            from detector import PrintDefectDetector

            threshold = self._cfg.threshold if self._cfg.threshold > 0 else None
            return PrintDefectDetector(
                threshold=threshold,
                alert_frames=self._cfg.alert_frames,
                save_alerts=False,         # 由 ai_guard 自己控制保存路径
                save_heatmap_always=False,  # DINOv2+kNN 无热力图
                alert_dir=self._cfg.alert_dir,
            )

        self._detector = await asyncio.to_thread(_load)

        # 关键修复：FeatureGallery.__init__ 会从 meta.json 反向覆盖构造参数
        # 的 decision_threshold，导致 detector 实际推理用的还是 meta.json 里的旧值。
        # 这里强制以 ai_config.threshold 为准（用户已显式配置时优先），并写回
        # meta.json 让重启后保持一致。
        if self._cfg.threshold > 0:
            gallery = getattr(self._detector, "gallery", None)
            if gallery is not None and hasattr(gallery, "update_threshold"):
                try:
                    gallery.update_threshold(self._cfg.threshold)
                    logger.info(
                        "启动时同步 gallery.decision_threshold=%.2f（ai_config 优先于 meta.json）",
                        self._cfg.threshold,
                    )
                except Exception as e:
                    logger.warning("启动同步 gallery threshold 失败: %s", e)

    async def _loop(self):
        """主循环：每隔 interval 秒执行一次检测"""
        while self._running:
            try:
                await asyncio.sleep(self._cfg.interval)
                if not self._running:
                    break
                await self._run_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("AI 异常检测循环异常: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _run_check(self):
        """执行一轮检测：遍历所有 RUNNING 状态的打印机"""
        if not self._detector:
            return

        statuses = self._manager.get_all_statuses()
        running_printers = [
            (serial, st) for serial, st in statuses.items()
            if st.state == PrinterState.RUNNING
        ]

        if not running_printers:
            return

        logger.debug("AI 检测开始: %d 台 RUNNING 打印机", len(running_printers))

        for serial, status in running_printers:
            if not self._running:
                break
            # 检查冷却
            last_ts = self._last_alarm_ts.get(serial, 0)
            if time.time() - last_ts < self._cfg.cooldown:
                continue
            # 检查摄像头支持
            if not self._camera.is_supported(serial):
                continue

            await self._check_printer(serial, status)

        logger.debug("AI 检测完成")

    async def _check_printer(self, serial: str, status):
        """对单台打印机执行连续帧检测

        流程:
          - 每帧间隔 camera_interval 秒从摄像头缓存取帧
          - 循环中只推理+缓存，不保存（避免重复）
          - 检测结束后统一保存:
              * 报警 → 所有帧保存到 alerts_review 单一子目录
              * 正常 → 每帧保存到 normal_review（独立子目录）
        """
        printer_name = status.name or serial[-6:]
        # 本轮所有帧（用于循环结束后统一保存）
        all_frames: list[bytes] = []
        all_probs: list[float] = []
        # 滑动窗口（最近 N 帧，用于报警时发企微）
        recent_frames: list[bytes] = []
        recent_probs: list[float] = []
        triggered_alert = None

        for i in range(self._cfg.frames_per_check):
            if not self._running:
                break

            # 从摄像头缓存获取最新帧（间隔 camera_interval 秒）
            frame = await self._camera.get_frame(serial)
            if not frame:
                logger.debug("打印机 %s 无可用帧，跳过", printer_name)
                break

            # 在子线程中推理，避免阻塞事件循环
            try:
                alert = await self._run_inference(frame)
            except Exception as e:
                logger.warning("打印机 %s 推理失败: %s", printer_name, e)
                break

            prob = self._detector._history[-1].probability if self._detector._history else 0.0
            all_frames.append(frame)
            all_probs.append(prob)

            # 维护滑动窗口（用于报警时发送给企微）
            recent_frames.append(frame)
            recent_probs.append(prob)
            if len(recent_frames) > self._cfg.alert_frames:
                recent_frames.pop(0)
                recent_probs.pop(0)

            if alert:
                triggered_alert = alert
                break

            # 间隔等待下一帧（同步摄像头刷新时间）
            if i < self._cfg.frames_per_check - 1:
                await asyncio.sleep(self._camera_interval)

        # 循环结束后统一保存（避免报警时帧被保存两次）
        if not all_frames:
            return

        if triggered_alert:
            # 报警：所有帧一起保存到 alerts_review 单一子目录
            await asyncio.to_thread(
                self._save_alarm_frames, all_frames, triggered_alert.probability, serial,
            )
            await self._trigger_alarm(serial, status, triggered_alert, recent_frames)
            self._last_alarm_ts[serial] = time.time()
            self._detector.reset()

    async def _run_inference(self, frame):
        """在子线程中执行推理（便于测试 mock）"""
        return await asyncio.to_thread(self._detector.push, frame)

    def _save_frame(self, frame: bytes, probability: float, serial: str):
        """保存单帧推理图片（手动/定时非报警场景）

        每次保存创建子文件夹:
            data/normal_review/{serial}_{时间}_P<概率}/{serial}_{时间}_P<概率>_原图.jpg
            data/normal_review/{serial}_{时间}_P<概率>/{serial}_{时间}_P<概率>_热力图.jpg

        异常 -> data/alerts_review/
        正常 -> data/normal_review/
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = serial[-6:]
        if probability >= self._detector.threshold:
            base_dir = Path(self._cfg.alert_dir)
        else:
            base_dir = Path("./data/normal_review")

        folder_name = f"{suffix}_{ts}_P{probability:.2f}"
        event_dir = base_dir / folder_name
        event_dir.mkdir(parents=True, exist_ok=True)

        # 保存原图
        img_name = f"{folder_name}_原图.jpg"
        with open(event_dir / img_name, "wb") as f:
            f.write(frame)

        logger.debug("推理图片已保存: %s", event_dir)

    def _save_alarm_frames(self, frames: list[bytes], probability: float, serial: str):
        """保存报警事件的多帧原图

        结构:
            data/alerts_review/{serial}_{时间}_P<概率>/
                {serial}_{时间}_P<概率>_原图_1.jpg
                {serial}_{时间}_P<概率>_原图_2.jpg
                ...
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = serial[-6:]
        folder_name = f"{suffix}_{ts}_P{probability:.2f}"
        event_dir = Path(self._cfg.alert_dir) / folder_name
        event_dir.mkdir(parents=True, exist_ok=True)

        for i, frame_bytes in enumerate(frames, start=1):
            base_name = f"{folder_name}_原图_{i}.jpg"
            with open(event_dir / base_name, "wb") as f:
                f.write(frame_bytes)

        logger.warning("报警事件已保存: %s (共 %d 帧)", event_dir, len(frames))

    async def _trigger_alarm(self, serial: str, status, alert, alarm_frames: list[bytes] = None):
        """触发报警：WebSocket 广播 + 企业微信通知

        本地保存由 _check_printer 在循环结束后统一处理，本方法不再重复保存。
        alarm_frames 为滑动窗口内最近 N 帧（用于企微发送）。

        Args:
            alarm_frames: 触发报警的滑动窗口内连续帧
        """
        printer_name = status.name or serial[-6:]
        logger.warning(
            "🚨 AI 异常报警: %s | P=%.3f | %s",
            printer_name, alert.probability, alert.message,
        )

        # 1. WebSocket 广播 → 网页通知
        if self._ws_manager:
            await self._ws_manager.broadcast({
                "type": "ai_alarm",
                "serial": serial,
                "printer_name": printer_name,
                "probability": round(alert.probability, 4),
                "message": alert.message,
                "severity": alert.severity,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

        # 2. 企业微信通知（附带触发报警的截图）
        if self._wecom:
            from .wecom import EVENT_AI_ALARM
            self._wecom.notify(
                serial=serial,
                event=EVENT_AI_ALARM,
                status=status,
                ai_alert=alert,
                alarm_frames=alarm_frames,
            )

        # 3. 飞书通知（同样附带截图）
        if self._feishu:
            from .feishu import EVENT_AI_ALARM as FEISHU_AI_ALARM
            self._feishu.notify(
                serial=serial,
                event=FEISHU_AI_ALARM,
                status=status,
                ai_alert=alert,
                alarm_frames=alarm_frames,
            )
