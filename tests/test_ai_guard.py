"""AI 异常报警模块测试

测试策略:
  - mock PrintDefectDetector（不依赖 torch 实际加载模型）
  - 测试状态过滤、无帧跳过、报警触发、冷却机制
  - 测试 WebSocket 广播和企微通知入队
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bumbo_stats.config import AIGuardSettings
from bumbo_stats.models import PrinterState, PrinterStatus
from bumbo_stats.printer.ai_guard import AIGuardLoop


# ========== 辅助数据 ==========

@dataclass
class FakeDetectionResult:
    """模拟 DetectionResult"""
    anomaly: bool = False
    probability: float = 0.1
    threshold: float = 0.79
    severity: str = ""
    message: str = ""
    ms: float = 10.0


def _make_status(state: PrinterState = PrinterState.RUNNING, name: str = "TestPrinter",
                 gcode: str = "test.gcode") -> PrinterStatus:
    return PrinterStatus(
        serial="TEST001", name=name, state=state, gcode_file=gcode,
        progress=50, current_layer=10, total_layers=20,
        nozzle_temp=220, nozzle_target_temp=220,
        bed_temp=60, bed_target_temp=60,
        chamber_temp=35, remaining_time=600,
    )


def _make_ai_config(**kwargs) -> AIGuardSettings:
    defaults = dict(
        enabled=True, interval=600, frames_per_check=3,
        model="fff_gray", track="adapted", threshold=0.0,
        alert_frames=3, cooldown=1800, alert_dir="/tmp/test_alerts",
    )
    defaults.update(kwargs)
    return AIGuardSettings(**defaults)


# ========== Fixtures ==========

@pytest.fixture
def mock_camera():
    camera = MagicMock()
    camera.is_supported.return_value = True
    camera.get_frame = AsyncMock(return_value=b"fake_jpeg_data")
    camera.capture_fresh_frame = AsyncMock(return_value=b"fake_jpeg_data")
    return camera


@pytest.fixture
def mock_manager():
    manager = MagicMock()
    manager.get_all_statuses.return_value = {
        "TEST001": _make_status(PrinterState.RUNNING),
    }
    return manager


@pytest.fixture
def mock_ws():
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    return ws


@pytest.fixture
def mock_wecom():
    wecom = MagicMock()
    wecom.notify = MagicMock()
    return wecom


# ========== 测试用例 ==========

class TestAIGuardInit:
    """TC-AI-01~03: 初始化与优雅降级"""

    def test_disabled_does_nothing(self):
        """TC-AI-01: enabled=False 时不启动"""
        config = _make_ai_config(enabled=False)
        guard = AIGuardLoop(config, MagicMock(), MagicMock(), MagicMock())
        assert not guard.is_available

    @pytest.mark.asyncio
    async def test_init_failure_graceful(self):
        """TC-AI-02: 模型加载失败时优雅降级"""
        config = _make_ai_config()
        guard = AIGuardLoop(config, MagicMock(), MagicMock(), MagicMock())

        # 模拟 detector 初始化失败
        with patch.object(guard, '_init_detector', side_effect=ImportError("torch not found")):
            await guard.start()

        assert not guard.is_available
        assert guard._init_error is not None
        assert "torch not found" in guard._init_error

    @pytest.mark.asyncio
    async def test_init_success(self):
        """TC-AI-03: 正常初始化"""
        config = _make_ai_config()
        guard = AIGuardLoop(config, MagicMock(), MagicMock(), MagicMock())

        mock_detector = MagicMock()
        mock_detector.threshold = 0.79
        guard._detector = mock_detector
        guard._init_error = None

        assert guard.is_available


class TestAIGuardStatusFilter:
    """TC-AI-04~06: 状态过滤"""

    @pytest.mark.asyncio
    async def test_skip_non_running(self, mock_camera, mock_manager, mock_ws):
        """TC-AI-04: 非 RUNNING 状态不检测"""
        config = _make_ai_config()
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws)
        guard._detector = MagicMock()

        # 所有打印机都是 IDLE
        mock_manager.get_all_statuses.return_value = {
            "TEST001": _make_status(PrinterState.IDLE),
            "TEST002": _make_status(PrinterState.PAUSE),
            "TEST003": _make_status(PrinterState.FINISH),
        }

        await guard._run_check()

        # 不应获取任何帧
        mock_camera.get_frame.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_check_running(self, mock_camera, mock_manager, mock_ws):
        """TC-AI-05: 只检测 RUNNING 状态的打印机"""
        config = _make_ai_config(frames_per_check=1)
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws)
        guard._running = True  # 必须设置为 True 才会实际执行检测

        mock_detector = MagicMock()
        mock_detector.push.return_value = None  # 无报警
        mock_detector.threshold = 0.79
        guard._detector = mock_detector

        mock_manager.get_all_statuses.return_value = {
            "TEST001": _make_status(PrinterState.IDLE),
            "TEST002": _make_status(PrinterState.RUNNING),
            "TEST003": _make_status(PrinterState.FINISH),
        }

        # Patch _run_inference to call detector directly (avoid thread issues with mocks)
        async def fake_inference(self_guard, frame):
            return self_guard._detector.push(frame)

        with patch.object(AIGuardLoop, '_run_inference', fake_inference), \
             patch.object(AIGuardLoop, '_save_frame'):
            await guard._run_check()

        # 只有 TEST002 应该被检测
        assert mock_camera.get_frame.call_count == 1

    @pytest.mark.asyncio
    async def test_skip_no_frame(self, mock_camera, mock_manager, mock_ws):
        """TC-AI-06: 无可用帧时跳过"""
        config = _make_ai_config(frames_per_check=3)
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws)

        mock_detector = MagicMock()
        mock_detector.push.return_value = None
        guard._detector = mock_detector

        # 摄像头返回 None
        mock_camera.get_frame = AsyncMock(return_value=None)

        await guard._run_check()

        # 不应调用推理
        mock_detector.push.assert_not_called()


class TestAIGuardAlarm:
    """TC-AI-07~10: 报警触发"""

    @pytest.mark.asyncio
    async def test_alarm_triggers_broadcast(self, mock_camera, mock_manager, mock_ws, mock_wecom):
        """TC-AI-07: 报警触发 WebSocket 广播"""
        config = _make_ai_config(frames_per_check=3)
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws, mock_wecom)
        guard._running = True

        alert = FakeDetectionResult(
            anomaly=True, probability=0.85, severity="high",
            message="⚠️ 连续 3 帧检测到异常!",
        )

        mock_detector = MagicMock()
        mock_detector.push.return_value = alert
        mock_detector.threshold = 0.79
        guard._detector = mock_detector

        async def fake_inference(self_guard, frame):
            return self_guard._detector.push(frame)

        with patch.object(AIGuardLoop, '_run_inference', fake_inference), \
             patch.object(AIGuardLoop, '_save_frame'):
            await guard._run_check()

        # WebSocket 广播应该被调用
        mock_ws.broadcast.assert_called_once()
        call_args = mock_ws.broadcast.call_args[0][0]
        assert call_args["type"] == "ai_alarm"
        assert call_args["serial"] == "TEST001"
        assert call_args["probability"] == 0.85

    @pytest.mark.asyncio
    async def test_alarm_triggers_wecom(self, mock_camera, mock_manager, mock_ws, mock_wecom):
        """TC-AI-08: 报警触发企业微信通知"""
        config = _make_ai_config(frames_per_check=3)
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws, mock_wecom)
        guard._running = True

        alert = FakeDetectionResult(anomaly=True, probability=0.90, severity="high")
        mock_detector = MagicMock()
        mock_detector.push.return_value = alert
        mock_detector.threshold = 0.79
        guard._detector = mock_detector

        async def fake_inference(self_guard, frame):
            return self_guard._detector.push(frame)

        with patch.object(AIGuardLoop, '_run_inference', fake_inference), \
             patch.object(AIGuardLoop, '_save_frame'):
            await guard._run_check()

        # 企微通知应该被调用
        mock_wecom.notify.assert_called_once()
        call_kwargs = mock_wecom.notify.call_args[1]
        assert call_kwargs["event"] == "ai_alarm"
        assert call_kwargs["ai_alert"] == alert

    @pytest.mark.asyncio
    async def test_no_alarm_no_broadcast(self, mock_camera, mock_manager, mock_ws, mock_wecom):
        """TC-AI-09: 无报警时不广播"""
        config = _make_ai_config(frames_per_check=3)
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws, mock_wecom)

        mock_detector = MagicMock()
        mock_detector.push.return_value = None  # 无报警
        mock_detector.threshold = 0.79
        guard._detector = mock_detector

        with patch.object(AIGuardLoop, '_save_frame'):
            await guard._run_check()

        mock_ws.broadcast.assert_not_called()
        mock_wecom.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_repeat(self, mock_camera, mock_manager, mock_ws, mock_wecom):
        """TC-AI-10: 冷却期内不重复报警"""
        config = _make_ai_config(frames_per_check=1, cooldown=1800)
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws, mock_wecom)
        guard._running = True

        alert = FakeDetectionResult(anomaly=True, probability=0.85)
        mock_detector = MagicMock()
        mock_detector.push.return_value = alert
        mock_detector.threshold = 0.79
        guard._detector = mock_detector

        async def fake_inference(self_guard, frame):
            return self_guard._detector.push(frame)

        # 第一次检测 → 触发报警
        with patch.object(AIGuardLoop, '_run_inference', fake_inference), \
             patch.object(AIGuardLoop, '_save_frame'):
            await guard._run_check()
        assert mock_ws.broadcast.call_count == 1

        # 第二次检测 → 冷却中，不触发
        with patch.object(AIGuardLoop, '_run_inference', fake_inference), \
             patch.object(AIGuardLoop, '_save_frame'):
            await guard._run_check()
        assert mock_ws.broadcast.call_count == 1  # 仍然是 1

        # 手动设置冷却过期
        guard._last_alarm_ts["TEST001"] = time.time() - 2000
        with patch.object(AIGuardLoop, '_run_inference', fake_inference), \
             patch.object(AIGuardLoop, '_save_frame'):
            await guard._run_check()
        assert mock_ws.broadcast.call_count == 2  # 冷却过期后再次触发


class TestAIGuardCameraUnsupported:
    """TC-AI-11: 摄像头不支持时跳过"""

    @pytest.mark.asyncio
    async def test_skip_unsupported_camera(self, mock_camera, mock_manager, mock_ws):
        """摄像头不支持的打印机不检测"""
        config = _make_ai_config()
        guard = AIGuardLoop(config, mock_camera, mock_manager, mock_ws)

        mock_detector = MagicMock()
        mock_detector.threshold = 0.79
        guard._detector = mock_detector

        mock_camera.is_supported.return_value = False

        await guard._run_check()

        mock_camera.get_frame.assert_not_called()


class TestAIGuardConfig:
    """TC-AI-12~13: 配置加载"""

    def test_default_config(self):
        """TC-AI-12: 默认配置值"""
        from bumbo_stats.config import AIGuardSettings
        cfg = AIGuardSettings()
        assert cfg.enabled is True
        assert cfg.interval == 600
        assert cfg.frames_per_check == 10
        assert cfg.model == "fff_gray"
        assert cfg.track == "adapted"
        assert cfg.threshold == 0.0
        assert cfg.alert_frames == 3
        assert cfg.cooldown == 1800
        assert cfg.alert_dir == "./data/alerts_review"

    def test_config_from_yaml(self, tmp_path):
        """TC-AI-13: 从 YAML 加载 ai_guard 配置"""
        from bumbo_stats.config import load_config
        config_file = tmp_path / "test.yaml"
        config_file.write_text("""
cloud:
  mqtt_broker: cn
printers:
  - serial: "TEST001"
    name: "Test"
ai_guard:
  enabled: true
  interval: 300
  model: dino_ft
  alert_frames: 5
""", encoding="utf-8")

        config = load_config(str(config_file))
        assert config.ai_guard.enabled is True
        assert config.ai_guard.interval == 300
        assert config.ai_guard.model == "dino_ft"
        assert config.ai_guard.alert_frames == 5
        # 未指定的字段用默认值
        assert config.ai_guard.track == "adapted"
        assert config.ai_guard.cooldown == 1800
