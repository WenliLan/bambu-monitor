"""AI 异常报警模块测试

测试策略:
  - mock PrintDefectDetector（不依赖 torch 实际加载模型）
  - 测试状态过滤、无帧跳过、报警触发、冷却机制
  - 测试 WebSocket 广播和企微通知入队
"""

import asyncio
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from bambu_monitor.config import AIGuardSettings
from bambu_monitor.models import PrinterState, PrinterStatus
from bambu_monitor.printer.ai_guard import AIGuardLoop


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
        threshold=0.0,
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
        from bambu_monitor.config import AIGuardSettings
        cfg = AIGuardSettings()
        assert cfg.enabled is True
        assert cfg.interval == 600
        assert cfg.frames_per_check == 10
        # P2-11：model/track 从未传给检测器，已删除，避免“改了也没用”
        assert not hasattr(cfg, "model")
        assert not hasattr(cfg, "track")
        assert cfg.threshold == 0.0
        assert cfg.alert_frames == 3
        assert cfg.cooldown == 1800
        assert cfg.alert_dir == "./data/alerts_review"

    def test_config_from_yaml(self, tmp_path):
        """TC-AI-13: 从 YAML 加载 ai_guard 配置"""
        from bambu_monitor.config import load_config
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
  model: dino_ft          # 历史遗留键：已删除的无效配置，加载时应被忽略
  alert_frames: 5
""", encoding="utf-8")

        config = load_config(str(config_file))
        assert config.ai_guard.enabled is True
        assert config.ai_guard.interval == 300
        # P2-11：旧 YAML 残留 model 键不报错，也不会产生“假配置项”
        assert not hasattr(config.ai_guard, "model")
        assert config.ai_guard.alert_frames == 5


class TestAIGuardThresholdSync:
    """TC-AI-14~17: set_threshold 必须同步到 gallery.decision_threshold

    历史 bug: 原来仅修改 ``self._detector.threshold`` 不会影响实际推理路径。
    detect() 调用 ``self.gallery.predict()``，决策用的是
    ``gallery.decision_threshold``（且 FeatureGallery.__init__ 会从 meta.json
    反向覆盖构造参数）。所以飞书发 "阈值 0.7" 后:
      - yaml 写入了 0.7 ✓
      - detector.threshold 变成了 0.7 ✓
      - 但实际推理还在用 gallery.decision_threshold=0.5 ✗
    """

    def _make_guard_with_gallery(self, init_threshold: float = 0.5):
        """构造一个带有 mock detector+gallery 的 AIGuardLoop"""
        config = _make_ai_config(threshold=init_threshold)
        guard = AIGuardLoop(config, MagicMock(), MagicMock(), MagicMock())

        mock_gallery = MagicMock()
        mock_gallery.decision_threshold = init_threshold
        mock_gallery.update_threshold = MagicMock(
            side_effect=lambda v: setattr(mock_gallery, "decision_threshold", v),
        )
        mock_detector = MagicMock()
        mock_detector.threshold = init_threshold
        mock_detector.gallery = mock_gallery

        guard._detector = mock_detector
        guard._init_error = None
        return guard, mock_detector, mock_gallery

    def test_threshold_sync_updates_gallery(self):
        """TC-AI-14: set_threshold 同步更新 gallery.decision_threshold"""
        guard, mock_detector, mock_gallery = self._make_guard_with_gallery(0.5)

        old = guard.set_threshold(0.7)

        assert old == 0.5
        assert mock_detector.threshold == 0.7
        # 关键：gallery 也必须同步（实际推理用）
        assert mock_gallery.decision_threshold == 0.7
        # 必须调用 update_threshold（持久化到 meta.json）
        mock_gallery.update_threshold.assert_called_once_with(0.7)

    def test_threshold_sync_triggers_persist_callback(self):
        """TC-AI-15: set_threshold 触发 yaml 持久化回调"""
        guard, mock_detector, _ = self._make_guard_with_gallery(0.5)
        persist_called = []
        guard._on_threshold_changed = lambda v: persist_called.append(v)

        guard.set_threshold(0.85)

        assert persist_called == [0.85]
        assert guard._cfg.threshold == 0.85

    def test_threshold_clamp_to_0_1(self):
        """TC-AI-16: 阈值自动截断到 [0, 1]"""
        guard, mock_detector, mock_gallery = self._make_guard_with_gallery(0.5)

        # 超出上界
        assert guard.set_threshold(2.5) == 0.5
        assert mock_detector.threshold == 1.0
        assert mock_gallery.decision_threshold == 1.0
        # 低于下界
        assert guard.set_threshold(-0.3) == 1.0
        assert mock_detector.threshold == 0.0
        assert mock_gallery.decision_threshold == 0.0

    def test_threshold_unavailable_returns_minus_one(self):
        """TC-AI-17: detector 不可用时 set_threshold 返回 -1，不调 gallery"""
        config = _make_ai_config()
        guard = AIGuardLoop(config, MagicMock(), MagicMock(), MagicMock())
        # 不设置 _detector，模拟 detector 未初始化
        mock_gallery = MagicMock()
        guard._detector = None  # is_available=False

        result = guard.set_threshold(0.7)
        assert result == -1.0
        mock_gallery.update_threshold.assert_not_called()

    def test_threshold_gallery_missing_fallback(self):
        """TC-AI-18: detector 没有 gallery 属性时优雅降级（不抛异常）"""
        guard, mock_detector, _ = self._make_guard_with_gallery(0.5)
        # 模拟 detector.gallery 不存在
        del mock_detector.gallery

        # 不应抛异常
        result = guard.set_threshold(0.7)
        assert result == 0.5
        assert mock_detector.threshold == 0.7


class TestAIGuardInitThresholdSync:
    """TC-AI-19: 启动时按 ai_config.threshold 强制覆盖 gallery

    历史 bug: FeatureGallery.__init__ 会从 meta.json 反向覆盖构造参数
    的 decision_threshold。重启服务后，yaml 里是 0.7，但 meta.json 是 0.5，
    最终 gallery.decision_threshold 会被覆盖回 0.5。
    """

    @pytest.mark.asyncio
    async def test_init_overrides_gallery_threshold(self):
        """TC-AI-19: _init_detector 完成后用 ai_config.threshold 覆盖 gallery"""
        config = _make_ai_config(threshold=0.7)  # 用户配置 0.7
        guard = AIGuardLoop(config, MagicMock(), MagicMock(), MagicMock())

        # 模拟 _init_detector 已经把 detector 装好（gallery 内部被 meta.json 覆盖回 0.5）
        mock_gallery = MagicMock()
        mock_gallery.decision_threshold = 0.5  # 来自 meta.json 的旧值
        mock_gallery.update_threshold = MagicMock(
            side_effect=lambda v: setattr(mock_gallery, "decision_threshold", v),
        )
        mock_detector = MagicMock()
        mock_detector.threshold = 0.5
        mock_detector.gallery = mock_gallery
        guard._detector = mock_detector

        # 显式调用同步逻辑（替代 _init_detector 内的内联代码）
        if guard._cfg.threshold > 0:
            gallery = getattr(guard._detector, "gallery", None)
            if gallery is not None and hasattr(gallery, "update_threshold"):
                gallery.update_threshold(guard._cfg.threshold)

        # ai_config 优先于 meta.json
        assert mock_gallery.decision_threshold == 0.7
        mock_gallery.update_threshold.assert_called_once_with(0.7)


# ========== 图库增量入库 ==========

class _FakeGallery:
    """替代 knn.FeatureGallery：路径落盘，特征不落盘（测试不关心向量）"""

    def __init__(self, gallery_dir, k=5, decision_threshold=0.7):
        self.dir = Path(gallery_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.k = k
        self.decision_threshold = decision_threshold
        self.save_calls = 0
        store = self.dir / "paths.json"
        data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
        self.normal_paths = data.get("normal", [])
        self.anomaly_paths = data.get("anomaly", [])

    def add(self, feat, label, path, autosave=True):
        getattr(self, f"{label}_paths").append(path)
        if autosave:
            self.save()

    def save(self):
        self.save_calls += 1
        (self.dir / "paths.json").write_text(
            json.dumps({"normal": self.normal_paths, "anomaly": self.anomaly_paths}),
            encoding="utf-8")

    def update_threshold(self, value):
        self.decision_threshold = value
        return value


def _fake_deploy(tmp_path, staged: dict[str, list[str]], known: dict | None = None):
    """搭一个假的部署目录：data/<label>/ 放素材，gallery/ 放已入库记录"""
    for label, names in staged.items():
        d = tmp_path / "data" / label
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"\xff\xd8\xff\xd9")
    gal = tmp_path / "gallery"
    gal.mkdir(parents=True, exist_ok=True)
    if known:
        (gal / "paths.json").write_text(json.dumps(known), encoding="utf-8")
    return tmp_path


def _fake_modules():
    extractor = MagicMock()
    extractor.extract_batch = lambda paths, batch_size=16: np.zeros(
        (len(paths), 384), dtype="float32")
    knn = MagicMock()
    knn.FeatureGallery = _FakeGallery
    return {"extractor": extractor, "knn": knn}


def _guard_with_gallery(tmp_path, **cfg):
    guard = AIGuardLoop(_make_ai_config(**cfg), MagicMock(), MagicMock(), MagicMock())
    guard._deploy_dir = tmp_path
    detector = MagicMock()
    detector.k = 5
    detector.threshold = 0.7
    detector.gallery = _FakeGallery(tmp_path / "gallery")
    guard._detector = detector
    return guard


class TestGalleryIngest:
    """TC-AI-20~24: 增量入库、去重、热替换"""

    @pytest.mark.asyncio
    async def test_tc_ai_20_ingest_dedup_and_hot_swap(self, tmp_path):
        """TC-AI-20: 跳过已入库、忽略 uncertain、完成后热替换图库"""
        deploy = _fake_deploy(tmp_path, {
            "normal": ["a.jpg", "b.jpg"],
            "anomaly": ["c.jpg"],
            "uncertain": ["d.jpg"],
        }, known={"normal": ["data/normal/a.jpg"], "anomaly": []})
        guard = _guard_with_gallery(deploy, threshold=0.7)
        old_gallery = guard._detector.gallery

        with patch.dict(sys.modules, _fake_modules()):
            job = await guard.ingest_gallery()
            await guard._ingest_task

        assert job["state"] == "done"
        assert job["skipped"] == 1
        assert job["added"] == 2
        assert job["added_normal"] == 1
        assert job["added_anomaly"] == 1
        assert job["failed"] == 0
        assert guard._detector.gallery is not old_gallery
        assert guard._detector.gallery.decision_threshold == 0.7
        assert "data/normal/b.jpg" in guard._detector.gallery.normal_paths
        assert "data/anomaly/c.jpg" in guard._detector.gallery.anomaly_paths
        assert not any("uncertain" in p for p in guard._detector.gallery.normal_paths)

    @pytest.mark.asyncio
    async def test_tc_ai_21_second_run_adds_nothing(self, tmp_path):
        """TC-AI-21: 重复点入库不会重复计入（add 本身不去重，靠路径比对）"""
        deploy = _fake_deploy(tmp_path, {"normal": ["a.jpg"], "anomaly": ["c.jpg"]})
        guard = _guard_with_gallery(deploy)

        with patch.dict(sys.modules, _fake_modules()):
            await guard.ingest_gallery()
            await guard._ingest_task
            first = guard._detector.gallery
            job2 = await guard.ingest_gallery()
            await guard._ingest_task

        assert job2["added"] == 0
        assert job2["skipped"] == 2
        assert guard._detector.gallery is first  # 无新增时不替换

    @pytest.mark.asyncio
    async def test_tc_ai_22_ingest_requires_detector(self, tmp_path):
        """TC-AI-22: 检测器不可用时拒绝入库"""
        guard = AIGuardLoop(_make_ai_config(), MagicMock(), MagicMock(), MagicMock())
        guard._deploy_dir = tmp_path
        with pytest.raises(RuntimeError):
            await guard.ingest_gallery()

    @pytest.mark.asyncio
    async def test_tc_ai_23_bad_image_counted_as_failed(self, tmp_path):
        """TC-AI-23: 单张提特征失败只计 failed，不影响其余图片"""
        deploy = _fake_deploy(tmp_path, {"normal": ["ok.jpg", "bad.jpg"]})
        guard = _guard_with_gallery(deploy)
        mods = _fake_modules()

        def flaky(paths, batch_size=16):
            if any(Path(p).name == "bad.jpg" for p in paths):
                raise ValueError("corrupt jpeg")
            return np.zeros((len(paths), 384), dtype="float32")

        mods["extractor"].extract_batch = flaky
        with patch.dict(sys.modules, mods):
            await guard.ingest_gallery()
            await guard._ingest_task

        job = guard._ingest_job
        assert job["added"] == 1
        assert job["failed"] == 1
        assert job["state"] == "done"

    def test_tc_ai_24_status_without_detector(self, tmp_path):
        """TC-AI-24: 检测器未就绪时仍能列出待入库素材"""
        deploy = _fake_deploy(tmp_path, {"normal": ["a.jpg"], "uncertain": ["u.jpg"]})
        guard = AIGuardLoop(_make_ai_config(enabled=False),
                            MagicMock(), MagicMock(), MagicMock())
        guard._deploy_dir = deploy
        info = guard.gallery_status()
        assert info["available"] is False
        assert info["staged"]["normal"]["total"] == 1
        assert info["staged"]["uncertain"]["total"] == 1
        assert info["data_dir"] == str(deploy / "data")

    @pytest.mark.asyncio
    async def test_tc_ai_29_job_total_known_before_worker_runs(self, tmp_path):
        """TC-AI-29: 启动响应即带真实 total/skipped，前端进度条不会从 0/0 起跳"""
        deploy = _fake_deploy(tmp_path, {
            "normal": ["a.jpg", "b.jpg"],
            "anomaly": ["c.jpg"],
        }, known={"normal": ["data/normal/a.jpg"], "anomaly": []})
        guard = _guard_with_gallery(deploy)

        with patch.dict(sys.modules, _fake_modules()):
            job = await guard.ingest_gallery()
            # 尚未让出控制权，worker 还没跑：此刻的值就是 POST 返回给前端的值
            assert job["total"] == 2
            assert job["skipped"] == 1
            assert job["done"] == 0
            await guard._ingest_task

        assert job["done"] == 2

    @pytest.mark.asyncio
    async def test_tc_ai_37_concurrent_ingest_runs_one_worker(self, tmp_path):
        """TC-AI-37: 并发点两次入库只起一个 worker，返回同一个 job"""
        deploy = _fake_deploy(tmp_path, {"normal": ["a.jpg", "b.jpg", "c.jpg"]})
        guard = _guard_with_gallery(deploy)
        started = []
        real_worker = guard._ingest_worker

        def slow_worker(job, pending):
            started.append(job)
            time.sleep(0.05)
            real_worker(job, pending)

        guard._ingest_worker = slow_worker
        with patch.dict(sys.modules, _fake_modules()):
            j1, j2 = await asyncio.gather(guard.ingest_gallery(), guard.ingest_gallery())
            await guard._ingest_task

        assert j1 is j2
        assert len(started) == 1
        assert j1["added"] == 3
        assert len(guard._detector.gallery.normal_paths) == 3

    @pytest.mark.asyncio
    async def test_tc_ai_38_stop_aborts_ingest_without_writing(self, tmp_path):
        """TC-AI-38: 面板停止时入库线程退出，不落盘、不热替换"""
        deploy = _fake_deploy(tmp_path, {"normal": [f"{i}.jpg" for i in range(40)]})
        guard = _guard_with_gallery(deploy)
        old_gallery = guard._detector.gallery
        mods = _fake_modules()
        gate = threading.Event()
        entered = threading.Event()
        exited = threading.Event()

        def blocking_extract(paths, batch_size=16):
            entered.set()
            gate.wait(5)
            return np.zeros((len(paths), 384), dtype="float32")

        mods["extractor"].extract_batch = blocking_extract
        real_worker = guard._ingest_worker

        def worker(job, pending):
            try:
                real_worker(job, pending)
            finally:
                exited.set()

        guard._ingest_worker = worker
        with patch.dict(sys.modules, mods):
            job = await guard.ingest_gallery()
            # 必须等 worker 真的卡在提特征里，否则 cancel 掉的是还没启动的任务
            assert await asyncio.to_thread(entered.wait, 2), "worker 未进入提特征"
            guard.stop()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await guard._ingest_task

        assert exited.wait(2), "入库线程没有随 stop() 退出"
        assert job["state"] == "error"
        assert guard._detector.gallery is old_gallery
        assert not (deploy / "gallery" / "paths.json").exists()

    @pytest.mark.asyncio
    async def test_tc_ai_39_bulk_ingest_saves_once(self, tmp_path):
        """TC-AI-39: 批量入库只写一次盘（每张都写是 O(n²) 全量重写）"""
        names = [f"{i}.jpg" for i in range(5)]
        deploy = _fake_deploy(tmp_path, {"normal": names})
        guard = _guard_with_gallery(deploy)

        with patch.dict(sys.modules, _fake_modules()):
            await guard.ingest_gallery()
            await guard._ingest_task

        gal = guard._detector.gallery
        assert gal.save_calls == 1
        stored = json.loads((deploy / "gallery" / "paths.json").read_text(encoding="utf-8"))
        assert stored["normal"] == [f"data/normal/{n}" for n in names]


class TestGalleryUpload:
    """TC-AI-25~28: 上传素材的边界与安全"""

    def test_tc_ai_25_upload_lands_in_label_dir(self, tmp_path):
        """TC-AI-25: 正常上传落到 data/<label>/ 并返回相对路径"""
        guard = _guard_with_gallery(tmp_path)
        rel = guard.stage_upload("anomaly", "photo.jpg", b"\xff\xd8\xff\xd9")
        assert rel == "data/anomaly/photo.jpg"
        assert (tmp_path / rel).read_bytes() == b"\xff\xd8\xff\xd9"

    def test_tc_ai_26_upload_cannot_escape_data_dir(self, tmp_path):
        """TC-AI-26: 路径穿越被 basename 化解，文件仍留在 data/ 内"""
        guard = _guard_with_gallery(tmp_path)
        rel = guard.stage_upload("normal", "..\\..\\evil.jpg", b"x")
        assert rel == "data/normal/evil.jpg"
        assert not (tmp_path.parent / "evil.jpg").exists()

    def test_tc_ai_27_upload_rejects_bad_input(self, tmp_path):
        """TC-AI-27: 非法分类 / 非图片 / 空文件 / 超大文件一律拒绝"""
        guard = _guard_with_gallery(tmp_path)
        for label, name, payload in [
            ("nope", "a.jpg", b"x"),
            ("normal", "a.txt", b"x"),
            ("normal", "a.jpg", b""),
            ("normal", "a.jpg", b"x" * (20 * 1024 * 1024 + 1)),
        ]:
            with pytest.raises(ValueError):
                guard.stage_upload(label, name, payload)

    def test_tc_ai_28_upload_does_not_overwrite(self, tmp_path):
        """TC-AI-28: 同名文件自动加序号，不覆盖已有素材"""
        guard = _guard_with_gallery(tmp_path)
        first = guard.stage_upload("normal", "a.jpg", b"1")
        second = guard.stage_upload("normal", "a.jpg", b"2")
        assert first == "data/normal/a.jpg"
        assert second == "data/normal/a_1.jpg"
        assert (tmp_path / first).read_bytes() == b"1"


class TestFlatImageSaving:
    """TC-AI-30~33: 报警/推理图片平铺保存，不再建事件子目录"""

    def test_tc_ai_30_alarm_frames_flat(self, tmp_path):
        """TC-AI-30: 报警多帧直接落在 alert_dir 根下，无子目录"""
        alert_dir = tmp_path / "alerts_review"
        guard = _guard_with_gallery(tmp_path, alert_dir=str(alert_dir))
        guard._save_alarm_frames([b"f1", b"f2", b"f3"], 0.81, "01P00C650602155")

        files = sorted(p.name for p in alert_dir.iterdir())
        assert len(files) == 3
        assert all(p.is_file() for p in alert_dir.iterdir())
        assert [f[-len("_原图_1.jpg"):] for f in files] == ["_原图_1.jpg", "_原图_2.jpg", "_原图_3.jpg"]
        assert all(f.startswith("602155_") and "_P0.81_" in f for f in files)
        assert (alert_dir / files[0]).read_bytes() == b"f1"
        assert (alert_dir / files[2]).read_bytes() == b"f3"

    def test_tc_ai_31_single_frame_flat_by_probability(self, tmp_path, monkeypatch):
        """TC-AI-31: 单帧按概率分流，两边都是平铺文件"""
        monkeypatch.chdir(tmp_path)
        alert_dir = tmp_path / "alerts_review"
        guard = _guard_with_gallery(tmp_path, alert_dir=str(alert_dir))
        guard._detector.threshold = 0.7

        guard._save_frame(b"bad", 0.9, "01P00C650602155")
        guard._save_frame(b"ok", 0.2, "01P00C650602155")

        alerts = list(alert_dir.iterdir())
        normals = list((tmp_path / "data" / "normal_review").iterdir())
        assert len(alerts) == 1 and alerts[0].is_file()
        assert len(normals) == 1 and normals[0].is_file()
        assert alerts[0].name.endswith("_P0.90_原图.jpg")
        assert normals[0].name.endswith("_P0.20_原图.jpg")
        assert alerts[0].read_bytes() == b"bad"
        assert normals[0].read_bytes() == b"ok"

    def test_tc_ai_32_two_events_do_not_mix(self, tmp_path):
        """TC-AI-32: 两次报警各自成组，前缀不同、互不覆盖"""
        alert_dir = tmp_path / "alerts_review"
        guard = _guard_with_gallery(tmp_path, alert_dir=str(alert_dir))
        guard._save_alarm_frames([b"a"], 0.8, "SERIAL000001")
        with patch("bambu_monitor.printer.ai_guard.datetime") as dt:
            dt.now.return_value.strftime.return_value = "20990101_000000"
            guard._save_alarm_frames([b"b"], 0.9, "SERIAL000002")

        files = sorted(p.name for p in alert_dir.iterdir())
        assert len(files) == 2
        assert files[0].startswith("000001_") and files[1].startswith("000002_")
        assert {p.read_bytes() for p in alert_dir.iterdir()} == {b"a", b"b"}

    def test_tc_ai_33_status_exposes_threshold(self, tmp_path):
        """TC-AI-33: gallery_status 顶层带 threshold，检测器不可用时为 None"""
        guard = _guard_with_gallery(tmp_path)
        assert guard.gallery_status()["threshold"] == 0.7

        bare = AIGuardLoop(_make_ai_config(enabled=False),
                           MagicMock(), MagicMock(), MagicMock())
        bare._deploy_dir = tmp_path
        assert bare.gallery_status()["threshold"] is None


class TestOpenAlertFolder:
    """TC-AI-34~36: 后端直接打开报警图片目录（浏览器无法预设文件框起始路径）"""

    def test_tc_ai_34_opens_configured_alert_dir(self, tmp_path):
        """TC-AI-34: 打开配置里的 alert_dir，目录不存在时先建好"""
        alert_dir = tmp_path / "alerts_review"
        guard = _guard_with_gallery(tmp_path, alert_dir=str(alert_dir))
        assert not alert_dir.exists()

        with patch("bambu_monitor.printer.ai_guard.os") as fake_os:
            returned = guard.open_alert_folder()

        assert returned == str(alert_dir.resolve())
        assert alert_dir.is_dir()
        fake_os.startfile.assert_called_once_with(str(alert_dir.resolve()))

    def test_tc_ai_35_relative_alert_dir_resolved(self, tmp_path, monkeypatch):
        """TC-AI-35: 出厂相对路径 ./data/alerts_review 按进程工作目录解析成绝对路径"""
        monkeypatch.chdir(tmp_path)
        guard = _guard_with_gallery(tmp_path, alert_dir="./data/alerts_review")

        with patch("bambu_monitor.printer.ai_guard.os") as fake_os:
            returned = guard.open_alert_folder()

        expected = tmp_path / "data" / "alerts_review"
        assert Path(returned).is_absolute()
        assert returned == str(expected.resolve())
        assert expected.is_dir()
        fake_os.startfile.assert_called_once_with(returned)

    def test_tc_ai_36_non_windows_raises(self, tmp_path):
        """TC-AI-36: 平台没有 os.startfile 时报错，不静默失败"""
        guard = _guard_with_gallery(tmp_path, alert_dir=str(tmp_path / "alerts_review"))
        with patch("bambu_monitor.printer.ai_guard.os", spec=[]):
            with pytest.raises(RuntimeError):
                guard.open_alert_folder()
        