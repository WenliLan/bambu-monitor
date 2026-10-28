"""测试飞书通知模块 - feishu.py

测试用例:
  TC-FEISHU-01: FeishuNotifier 初始化
  TC-FEISHU-02: 未配置时不启动
  TC-FEISHU-03: layer3 事件入队
  TC-FEISHU-04: completed 事件入队
  TC-FEISHU-05: error 事件入队
  TC-FEISHU-06: 同一事件去重（layer3）
  TC-FEISHU-07: 同一事件去重（completed）
  TC-FEISHU-08: 新任务重置 layer3 标记
  TC-FEISHU-09: 新任务重置 completed 标记
  TC-FEISHU-10: HMS 错误合并通知
  TC-FEISHU-11: 同错误不重复通知
  TC-FEISHU-12: 禁用事件不入队
  TC-FEISHU-13: 未知打印机被忽略
  TC-FEISHU-14: 队列满时优雅丢弃
  TC-FEISHU-15: _PrinterNotifState.reset_if_new_job
  TC-FEISHU-16: 消息构建测试
  TC-FEISHU-17: _on_message_received 解析群聊 @机器人消息并入队
  TC-FEISHU-18: _on_message_received 拒绝非群聊/未@机器人消息
  TC-FEISHU-19: _on_message_received 冷却机制（同一指令 3s 内重复被忽略）
  TC-FEISHU-20: _cmd_threshold 修改阈值并触发 on_threshold_changed 持久化
  TC-FEISHU-21: _process_command 派发器路由五个指令
"""

import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime

from bambu_monitor.models import PrinterState, PrinterStatus, HMSError
from bambu_monitor.printer.feishu import (
    FeishuNotifier, FeishuConfig, _PrinterNotifState,
    EVENT_LAYER3, EVENT_PROGRESS50, EVENT_COMPLETED, EVENT_ERROR, EVENT_AI_ALARM,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_status(serial, state=PrinterState.RUNNING, gcode_file="test.gcode",
                current_layer=0, total_layers=100, progress=0,
                hms_errors=None):
    return PrinterStatus(
        serial=serial, name="Test", state=state,
        gcode_file=gcode_file, last_updated=datetime.now(),
        current_layer=current_layer, total_layers=total_layers,
        progress=progress,
        hms_errors=hms_errors or [],
    )


def _make_feishu_config():
    return FeishuConfig(
        app_id="fake_app_id",
        app_secret="fake_secret",
        chatid="fake_chatid",
        layer3=True,
        progress50=True,
        completed=True,
        error=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_camera():
    return AsyncMock()


# ---------------------------------------------------------------------------
# TC-FEISHU-01~16: FeishuNotifier 测试
# ---------------------------------------------------------------------------

class TestFeishuNotifier:
    def test_tc_feishu_01_init(self, mock_camera):
        """TC-FEISHU-01: FeishuNotifier 初始化"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)

        assert notifier._cfg.app_id == "fake_app_id"
        assert notifier._cfg.chatid == "fake_chatid"
        assert notifier._tenant_token is None

    def test_tc_feishu_02_no_start_without_config(self):
        """TC-FEISHU-02: 未配置 app_id/app_secret 时不启动"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = FeishuConfig(app_id="", app_secret="")
        notifier = FeishuNotifier(config, [printer_cfg])

        # start() 应该直接返回，不创建后台任务
        async def check():
            await notifier.start()
            assert notifier._task is None
        asyncio.run(check())

    def test_tc_feishu_03_layer3_event_queued(self, mock_camera):
        """TC-FEISHU-03: layer3 事件入队"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status)

        assert notifier._queue.qsize() == 1

    def test_tc_feishu_04_completed_event_queued(self, mock_camera):
        """TC-FEISHU-04: completed 事件入队"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=100, total_layers=100, progress=100)
        notifier.notify("S001", EVENT_COMPLETED, status)

        assert notifier._queue.qsize() == 1

    def test_tc_feishu_05_error_event_queued(self, mock_camera):
        """TC-FEISHU-05: error 事件入队"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        status = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status, errors)

        assert notifier._queue.qsize() == 1

    def test_tc_feishu_06_layer3_deduplicated(self, mock_camera):
        """TC-FEISHU-06: 同一 layer3 事件去重"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)

        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1  # 未增加

    def test_tc_feishu_07_completed_deduplicated(self, mock_camera):
        """TC-FEISHU-07: 同一 completed 事件去重"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=100, total_layers=100, progress=100)

        notifier.notify("S001", EVENT_COMPLETED, status)
        assert notifier._queue.qsize() == 1

        notifier.notify("S001", EVENT_COMPLETED, status)
        assert notifier._queue.qsize() == 1  # 未增加

    def test_tc_feishu_08_new_job_resets_layer3(self, mock_camera):
        """TC-FEISHU-08: 新任务重置 layer3 标记"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        # job1 触发
        status1 = _make_status("S001", gcode_file="job1.gcode", current_layer=3, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status1)
        assert notifier._queue.qsize() == 1

        # job2 触发（新任务）
        status2 = _make_status("S001", gcode_file="job2.gcode", current_layer=3, progress=4)
        notifier.notify("S001", EVENT_LAYER3, status2)
        assert notifier._queue.qsize() == 2

    def test_tc_feishu_09_new_job_resets_completed(self, mock_camera):
        """TC-FEISHU-09: 新任务重置 completed 标记"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        # job1 完成
        status1 = _make_status("S001", gcode_file="job1.gcode", current_layer=100, progress=100)
        notifier.notify("S001", EVENT_COMPLETED, status1)
        assert notifier._queue.qsize() == 1

        # job2 开始后再完成
        status2 = _make_status("S001", gcode_file="job2.gcode", current_layer=100, progress=100)
        notifier.notify("S001", EVENT_COMPLETED, status2)
        assert notifier._queue.qsize() == 2

    def test_tc_feishu_10_hms_errors_merged(self, mock_camera):
        """TC-FEISHU-10: HMS 多个错误合并为一条通知"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        errors = [
            HMSError(code=0x0B01, severity="error", message="堵嘴检测"),
            HMSError(code=0x0501, severity="error", message="缺料"),
        ]
        status = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status, errors)

        assert notifier._queue.qsize() == 1
        item = notifier._queue.get_nowait()
        assert len(item["errors"]) == 2

    def test_tc_feishu_11_same_hms_error_not_duplicated(self, mock_camera):
        """TC-FEISHU-11: 同一 HMS 错误不重复通知"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]

        status1 = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status1, errors)
        assert notifier._queue.qsize() == 1

        status2 = _make_status("S001", hms_errors=errors)
        notifier.notify("S001", EVENT_ERROR, status2, errors)
        assert notifier._queue.qsize() == 1  # 未增加

    def test_tc_feishu_12_disabled_event_no_queue(self, mock_camera):
        """TC-FEISHU-12: 禁用的通知事件不入队"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        config.layer3 = False  # 禁用 layer3
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=3, total_layers=100, progress=3)
        notifier.notify("S001", EVENT_LAYER3, status)

        assert notifier._queue.qsize() == 0

    def test_tc_feishu_13_unknown_printer_ignored(self, mock_camera):
        """TC-FEISHU-13: 未知打印机被忽略"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("UNKNOWN", current_layer=3)
        notifier.notify("UNKNOWN", EVENT_LAYER3, status)

        assert notifier._queue.qsize() == 0

    def test_tc_feishu_14_queue_full_drops(self, mock_camera):
        """TC-FEISHU-14: 队列满时优雅丢弃"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=1)  # 队列大小为1

        status = _make_status("S001", current_layer=3)
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1

        # 队列已满，再次通知应丢弃
        notifier.notify("S001", EVENT_LAYER3, status)
        assert notifier._queue.qsize() == 1  # 仍是1

    def test_tc_feishu_15_printer_notif_state_reset(self):
        """TC-FEISHU-15: _PrinterNotifState.reset_if_new_job"""
        state = _PrinterNotifState()
        state.last_gcode = "job1.gcode"
        state.layer3_sent = True
        state.progress50_sent = True
        state.completed_sent = True

        # 同任务不变
        state.reset_if_new_job("job1.gcode")
        assert state.layer3_sent is True
        assert state.progress50_sent is True
        assert state.completed_sent is True

        # 新任务重置
        state.reset_if_new_job("job2.gcode")
        assert state.layer3_sent is False
        assert state.progress50_sent is False
        assert state.completed_sent is False
        assert state.last_gcode == "job2.gcode"

    def test_tc_feishu_15b_same_job_filename_normalized(self):
        """TC-FEISHU-15b: 同一任务的 gcode 在不同阶段形式不同（带/不带扩展名），
        归一化比较后应识别为同一任务，不重置已发送标记。

        背景：MQTT 上报带 ``.3mf``（本地文件名），云端同步后可能为任务名
        （无扩展名）。若 reset_if_new_job 不归一化，形式变化就会被误判
        为新任务，导致 layer3/progress50/completed 在同一任务内多次重发。
        """
        from bambu_monitor.printer.wecom import normalize_gcode

        # 场景1: 先收到带扩展名的 gcode，并已发送 layer3
        state = _PrinterNotifState()
        state.last_gcode = "CUBE_2.4 i-type narrow.3mf"
        state.layer3_sent = True
        state.progress50_sent = True
        state.completed_sent = False

        # MQTT 上报形式变化（无扩展名），归一化后相等 → 不应重置
        state.reset_if_new_job("CUBE_2.4 i-type narrow")
        assert state.layer3_sent is True, \
            "同任务（归一化后相等）不应重置 layer3_sent，否则会重发通知"
        assert state.progress50_sent is True
        # last_gcode 同步为最新形式
        assert state.last_gcode == "CUBE_2.4 i-type narrow"

        # 场景2: 反向也一样（先无扩展名，后带扩展名）
        state2 = _PrinterNotifState()
        state2.last_gcode = "job-name"
        state2.layer3_sent = True
        state2.reset_if_new_job("job-name.3mf")
        assert state2.layer3_sent is True
        assert state2.last_gcode == "job-name.3mf"

        # 场景3: 真正的新任务（归一化后不等）应重置
        state3 = _PrinterNotifState()
        state3.last_gcode = "task_a.3mf"
        state3.layer3_sent = True
        state3.reset_if_new_job("task_b.gcode")
        assert state3.layer3_sent is False
        assert state3.last_gcode == "task_b.gcode"

        # 场景4: 空 gcode 不应触发重置（保护初始状态）
        state4 = _PrinterNotifState()
        state4.last_gcode = ""
        state4.layer3_sent = True
        state4.reset_if_new_job("")
        assert state4.layer3_sent is True

        # 场景5: 中英混合扩展名都该归一化（确认 normalize_gcode 行为）
        assert normalize_gcode("xxx.3mf") == normalize_gcode("xxx")
        assert normalize_gcode("xxx.GCODE") == normalize_gcode("xxx.gcode")

    def test_tc_feishu_16_message_build(self, mock_camera):
        """TC-FEISHU-16: 消息构建测试

        飞书 post 富文本不渲染 Markdown，模板须满足:
          1. 第一行含机器名称 + 信息关键内容
          2. 不能出现 ``**xxx**`` 加粗标记（飞书会原样显示星号）
          3. HMS 错误保留中文名 + 原始消息
        """
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)

        # ---- layer3 消息 ----
        status = _make_status("S001", gcode_file="test.gcode", current_layer=3, total_layers=100, progress=3)
        msg = notifier._build_message("TestPrinter", EVENT_LAYER3, status)
        assert "第 3 层" in msg
        assert "TestPrinter" in msg
        assert "test.gcode" in msg
        # 模板断言: 首行 = 机器名 + 事件关键内容
        first_line = msg.split("\n", 1)[0]
        assert "TestPrinter" in first_line
        assert "第 3 层" in first_line
        # 模板断言: 正文不出现 Markdown 加粗标记
        assert "**" not in msg

        # ---- completed 消息 ----
        status = _make_status("S001", gcode_file="test.gcode", current_layer=100, total_layers=100, progress=100)
        msg = notifier._build_message("TestPrinter", EVENT_COMPLETED, status)
        assert "完成" in msg
        assert "**" not in msg
        assert "TestPrinter" in msg.split("\n", 1)[0]

        # ---- error 消息 ----
        errors = [HMSError(code=0x0B01, severity="error", message="堵嘴检测")]
        msg = notifier._build_message("TestPrinter", EVENT_ERROR, None, errors)
        assert "异常" in msg
        assert "堵嘴检测" in msg
        assert "**" not in msg
        assert "TestPrinter" in msg.split("\n", 1)[0]

        # ---- AI 报警消息（为防回归） ----
        ai_alert = MagicMock()
        ai_alert.probability = 0.85
        ai_alert.message = "连续 3 帧检测到异常"
        status = _make_status("S001", gcode_file="test.gcode", current_layer=50, total_layers=100, progress=50)
        msg = notifier._build_message("TestPrinter", EVENT_AI_ALARM, status, ai_alert=ai_alert)
        first_line = msg.split("\n", 1)[0]
        assert "TestPrinter" in first_line
        assert "AI" in first_line
        # 概率以 1 位百分数呈现
        assert "85.0%" in first_line or "85%" in first_line
        assert "**" not in msg

        # ---- progress50 消息 ----
        status = _make_status("S001", gcode_file="test.gcode", current_layer=50, total_layers=100, progress=50)
        msg = notifier._build_message("TestPrinter", EVENT_PROGRESS50, status)
        first_line = msg.split("\n", 1)[0]
        # 首行 = 机器名 + 事件关键内容
        assert "TestPrinter" in first_line
        assert "半数" in first_line or "过半" in first_line
        # 补充信息: 进度 50% (层 50/100)
        assert "50%" in msg
        assert "50/100" in msg
        # 不能出现 Markdown 加粗
        assert "**" not in msg

    def test_tc_feishu_17_progress50_event_queued(self, mock_camera):
        """TC-FEISHU-17: progress50 事件入队"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=50, total_layers=100, progress=50)
        notifier.notify("S001", EVENT_PROGRESS50, status)

        assert notifier._queue.qsize() == 1
        # 状态标记应被设为 True
        assert notifier._states["S001"].progress50_sent is True

    def test_tc_feishu_18_progress50_deduplicated(self, mock_camera):
        """TC-FEISHU-18: 同一 progress50 事件去重"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=50, total_layers=100, progress=50)

        notifier.notify("S001", EVENT_PROGRESS50, status)
        assert notifier._queue.qsize() == 1

        notifier.notify("S001", EVENT_PROGRESS50, status)
        assert notifier._queue.qsize() == 1  # 未增加

    def test_tc_feishu_19_progress50_disabled_no_queue(self, mock_camera):
        """TC-FEISHU-19: 禁用 progress50 时不入队"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        config.progress50 = False  # 禁用 progress50
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        status = _make_status("S001", current_layer=50, total_layers=100, progress=50)
        notifier.notify("S001", EVENT_PROGRESS50, status)

        assert notifier._queue.qsize() == 0
        # 状态标记不应被设置（未入队）
        assert notifier._states["S001"].progress50_sent is False


# ---------------------------------------------------------------------------
# 辅助函数：构造 mock 的 lark-oapi 群聊事件对象
# ---------------------------------------------------------------------------

def _make_mention(key: str):
    """构造 mentions 中的一个 mention 对象"""
    m = MagicMock()
    m.key = key
    return m


def _make_lark_event(text: str, *, chat_type: str = "group",
                     message_type: str = "text",
                     mentioned_keys: list = None,
                     sender_type: str = "user"):
    """构造一个 mock 的 P2ImMessageReceiveV1 事件对象

    模拟飞书 SDK 反序列化后的 event/ev 结构：
      ev.event.sender.sender_type = "user"|"app"
      ev.event.message.chat_type / message_type / content / mentions / chat_id / message_id
    """
    import json as _json
    ev = MagicMock()
    sender = MagicMock()
    sender.sender_type = sender_type
    ev.sender = sender

    msg = MagicMock()
    msg.chat_type = chat_type
    msg.message_type = message_type
    msg.content = _json.dumps({"text": text})
    msg.mentions = [_make_mention(k) for k in (mentioned_keys or [])]
    msg.chat_id = "oc_fake_chat"
    msg.message_id = "om_fake_msg"

    event = MagicMock()
    event.sender = sender
    event.message = msg
    ev.event = event
    return ev


# ---------------------------------------------------------------------------
# TC-FEISHU-17~21: 飞书群聊指令路径测试
# ---------------------------------------------------------------------------

class TestFeishuCommands:
    """飞书入站消息 + 指令处理路径"""

    def test_tc_feishu_17_parsed_and_queued(self, mock_camera):
        """TC-FEISHU-17: 群聊 @机器人 发“截图”时解析并入队 snapshot 指令"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        # 群里 @机器人 发“截图”
        ev = _make_lark_event("@_user_1 截图", mentioned_keys=["@_user_1"])
        notifier._on_message_received(ev)

        assert notifier._queue.qsize() == 1
        item = notifier._queue.get_nowait()
        assert item["event"] == "snapshot"
        assert item["raw_text"] == "截图"  # @机器人 部分已被剥离
        assert item["chat_id"] == "oc_fake_chat"
        assert item["message_id"] == "om_fake_msg"

    def test_tc_feishu_18_rejects_non_group_or_no_mention(self, mock_camera):
        """TC-FEISHU-18: 非群聊 / 未 @机器人 被忽略；无法识别时给出反馈（P2-8）"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        # 私聊消息（chat_type=p2p）被忽略
        ev_p2p = _make_lark_event("截图", chat_type="p2p",
                                  mentioned_keys=["@_user_1"])
        notifier._on_message_received(ev_p2p)
        assert notifier._queue.qsize() == 0

        # 群聊但没 @机器人 被忽略
        ev_no_at = _make_lark_event("有人说了截图", chat_type="group",
                                    mentioned_keys=[])
        notifier._on_message_received(ev_no_at)
        assert notifier._queue.qsize() == 0

        # 群聊 @机器人 但无法识别：不执行指令，但回一条提示（不再静默）
        ev_no_kw = _make_lark_event("@_user_1 你好", chat_type="group",
                                    mentioned_keys=["@_user_1"])
        notifier._on_message_received(ev_no_kw)
        assert notifier._queue.qsize() == 1
        reply = notifier._queue.get_nowait()
        assert reply["event"] == "__reply__"
        assert "未识别到指令" in reply["text"]
        assert reply["chat_id"] == "oc_fake_chat"

    def test_tc_feishu_19_cooldown_blocks_duplicate(self, mock_camera):
        """TC-FEISHU-19: 冷却窗口内重复指令不再执行，仅回冷却提示（P2-8）"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._queue = asyncio.Queue(maxsize=10)

        ev = _make_lark_event("@_user_1 帮助", mentioned_keys=["@_user_1"])

        notifier._on_message_received(ev)
        assert notifier._queue.qsize() == 1  # 第一次入队
        assert notifier._queue.get_nowait()["event"] == "help"

        # 冷却时间内再次发同一指令 → 不重复执行，只回一条提示
        notifier._on_message_received(ev)
        assert notifier._queue.qsize() == 1
        reply = notifier._queue.get_nowait()
        assert reply["event"] == "__reply__"
        assert "刚刚已响应过" in reply["text"]

    async def test_tc_feishu_20_cmd_threshold_persists(self, mock_camera):
        """TC-FEISHU-20: _cmd_threshold 修改阈值并触发 on_threshold_changed 回调（持久化）

        验证：
          1. 调用 ai_guard.set_threshold 被实际触发
          2. set_threshold 内部回调 on_threshold_changed 被调用，传入新阈值
          3. 返回文本包含“已更新”和新阈值
        """
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)

        # 模拟 AIGuardLoop：set_threshold 返回旧值 + 同步回调 on_threshold_changed
        persist_calls: list[float] = []

        class _FakeGuard:
            threshold = 0.5

            def set_threshold(self, v: float) -> float:
                # 模拟 AIGuardLoop.set_threshold 的实际行为:
                # 修改 threshold + 调用 on_threshold_changed
                old = self.threshold
                clamped = max(0.0, min(1.0, float(v)))
                self.threshold = clamped
                self.on_threshold_changed(clamped)
                return old

            def on_threshold_changed(self, v: float) -> None:
                persist_calls.append(v)

        guard = _FakeGuard()
        notifier.bind_ai_guard(guard)

        # 注入 token，跳过 _refresh_token
        notifier._tenant_token = "fake_token"
        notifier._token_expires_at = time.time() + 3600

        # Mock _send_message 只记录 title + content，不真发
        sent: list[dict] = []
        async def _fake_send(content, frames, title=""):
            sent.append({"content": content, "title": title})
            return "om_mock"
        notifier._send_message = _fake_send  # type: ignore[assignment]

        await notifier._cmd_threshold("oc_fake_chat", 0.7)

        assert guard.threshold == 0.7
        assert persist_calls == [0.7]
        assert len(sent) == 1
        assert "已更新" in sent[0]["content"]
        assert "0.70" in sent[0]["content"]

    async def test_tc_feishu_21_dispatcher_routes(self, mock_camera):
        """TC-FEISHU-21: _process_command 派发器路由五个指令到对应 _cmd_* 方法"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        notifier._tenant_token = "fake_token"
        notifier._token_expires_at = time.time() + 3600

        # Mock 五个 _cmd_* 方法以验证派发路径
        called = {"snapshot": 0, "status": 0, "overview": 0,
                  "threshold": 0, "help": 0}

        async def _stub(*a, **k):
            return None
        async def _snapshot(chat_id):
            called["snapshot"] += 1
        async def _status(chat_id):
            called["status"] += 1
        async def _overview(chat_id):
            called["overview"] += 1
        async def _threshold(chat_id, v):
            called["threshold"] += 1
        async def _help(chat_id):
            called["help"] += 1
        notifier._cmd_snapshot = _snapshot  # type: ignore[assignment]
        notifier._cmd_status = _status  # type: ignore[assignment]
        notifier._cmd_overview = _overview  # type: ignore[assignment]
        notifier._cmd_threshold = _threshold  # type: ignore[assignment]
        notifier._cmd_help = _help  # type: ignore[assignment]

        for cmd in ("snapshot", "status", "overview", "help"):
            await notifier._process_command({"event": cmd, "chat_id": "oc_x"})
        await notifier._process_command({"event": "threshold", "chat_id": "oc_x",
                                          "numeric_value": 0.6})

        assert called == {"snapshot": 1, "status": 1, "overview": 1,
                          "threshold": 1, "help": 1}

    async def test_tc_feishu_22_unbound_ai_guard(self, mock_camera):
        """TC-FEISHU-22: 未绑定 ai_guard 时阈值指令返回友好提示，不崩"""
        from bambu_monitor.config import PrinterConfig

        printer_cfg = PrinterConfig(serial="S001", name="P1")
        config = _make_feishu_config()
        notifier = FeishuNotifier(config, [printer_cfg], mock_camera)
        # 不调用 bind_ai_guard
        notifier._tenant_token = "fake_token"
        notifier._token_expires_at = time.time() + 3600

        sent: list[dict] = []
        async def _fake_send(content, frames, title=""):
            sent.append({"content": content, "title": title})
            return "om_mock"
        notifier._send_message = _fake_send  # type: ignore[assignment]

        await notifier._cmd_threshold("oc_x", 0.8)
        assert any("未启动" in s["content"] for s in sent)
