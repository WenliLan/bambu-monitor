"""测试摄像头管理器 - printer/camera.py (v0.5.17 协议自动分流版)

测试用例:
  TC-CAM-01: 配了 ip+access_code 的任意机型(含 X1 前缀)均支持摄像头
  TC-CAM-02: P1S/A1 支持摄像头
  TC-CAM-03: 无 ip/access_code 时不支持摄像头
  TC-CAM-04: get_frame 无缓存时返回 None
  TC-CAM-05: 6000 可达 -> JPEG 协议, 走 JPEGFrameStream
  TC-CAM-06: 6000 不可达/322 可达/ffmpeg 可用 -> RTSPS 协议, 走 capture_rtsp_frame
  TC-CAM-07: 322 可达但 ffmpeg 缺失 -> 返回 None 且协议不缓存(装好后可自动启用)
  TC-CAM-08: 协议探测结果按序列号缓存, 不重复探测
  TC-CAM-09: reload_configs 更新支持状态并重置协议缓存
"""

import pytest
from unittest.mock import patch

from bumbo_stats.config import PrinterConfig
from bumbo_stats.printer.camera import CameraManager, PROTO_JPEG, PROTO_RTSPS


@pytest.fixture
def cam_configs():
    return [
        PrinterConfig(name="X1C", serial="BL-P001-001", ip="1.1.1.1", access_code="0000"),
        PrinterConfig(name="P1S", serial="AC12309BH109", ip="1.1.1.2", access_code="0001"),
        PrinterConfig(name="A1", serial="AC12309BH110", ip="1.1.1.3", access_code="0002"),
    ]


@pytest.fixture
def no_camera_configs():
    """只有 serial，没有 ip/access_code"""
    return [
        PrinterConfig(name="CloudOnly", serial="CLOUD001"),
    ]


class TestCameraSupport:
    def test_tc_cam_01_x1_now_supported(self, cam_configs):
        """TC-CAM-01: X1 前缀不再被排除(协议由端口探测决定)"""
        cam = CameraManager(cam_configs)
        assert cam.is_supported("BL-P001-001") is True

    def test_tc_cam_02_p1s_a1_supported(self, cam_configs):
        """TC-CAM-02: P1S 和 A1 支持摄像头"""
        cam = CameraManager(cam_configs)
        assert cam.is_supported("AC12309BH109") is True
        assert cam.is_supported("AC12309BH110") is True

    def test_tc_cam_03_no_ip_no_camera(self, no_camera_configs):
        """TC-CAM-03: 无 ip/access_code 时不支持摄像头"""
        cam = CameraManager(no_camera_configs)
        assert cam.is_supported("CLOUD001") is False


@pytest.mark.asyncio
class TestCameraFrame:
    async def test_tc_cam_04_no_cache_returns_none(self, cam_configs):
        """TC-CAM-04: 无缓存帧时返回 None"""
        cam = CameraManager(cam_configs)
        frame = await cam.get_frame("AC12309BH109")
        assert frame is None


@pytest.mark.asyncio
class TestProtocolDispatch:
    async def test_tc_cam_05_jpeg_dispatch(self, cam_configs):
        """TC-CAM-05: 6000 可达 -> JPEG 协议(MagicMock 帧被类型检查拒绝, 不写缓存)"""
        cam = CameraManager(cam_configs)
        with patch("bumbo_stats.printer.camera._tcp_reachable", return_value=True):
            frame = await cam.capture_fresh_frame("AC12309BH109")
        # bambulab 已被 conftest mock 成 MagicMock, 返回帧非 bytes
        assert cam._protocol.get("AC12309BH109") == PROTO_JPEG
        assert frame is None

    async def test_tc_cam_05b_jpeg_bytes_cached(self, cam_configs):
        """TC-CAM-05b: JPEG 分支返回 bytes 时刷新缓存"""
        cam = CameraManager(cam_configs)
        cam._protocol["AC12309BH109"] = PROTO_RTSPS  # 借用 RTSPS 分支验证缓存写入
        with patch("bumbo_stats.printer.camera.capture_rtsp_frame", return_value=b"jpg-data"):
            frame = await cam.capture_fresh_frame("AC12309BH109")
        assert frame == b"jpg-data"
        assert await cam.get_frame("AC12309BH109") == b"jpg-data"

    async def test_tc_cam_06_rtsp_dispatch(self, cam_configs):
        """TC-CAM-06: 6000 不可达/322 可达/ffmpeg 在 -> RTSPS 协议"""
        cam = CameraManager(cam_configs)

        def fake_reachable(ip, port, timeout=1.5):
            return port == 322

        with patch("bumbo_stats.printer.camera._tcp_reachable", side_effect=fake_reachable), \
             patch("bumbo_stats.printer.camera.find_ffmpeg", return_value="ffmpeg"), \
             patch("bumbo_stats.printer.camera.capture_rtsp_frame", return_value=b"rtsp-jpg") as m:
            frame = await cam.capture_fresh_frame("BL-P001-001")
        assert cam._protocol.get("BL-P001-001") == PROTO_RTSPS
        assert frame == b"rtsp-jpg"
        m.assert_called_once_with("1.1.1.1", "0000")

    async def test_tc_cam_07_rtsp_no_ffmpeg_degrades(self, cam_configs):
        """TC-CAM-07: RTSPS 端口可达但 ffmpeg 缺失 -> None, 协议不缓存"""
        cam = CameraManager(cam_configs)

        def fake_reachable(ip, port, timeout=1.5):
            return port == 322

        with patch("bumbo_stats.printer.camera._tcp_reachable", side_effect=fake_reachable), \
             patch("bumbo_stats.printer.camera.find_ffmpeg", return_value=None):
            frame = await cam.capture_fresh_frame("BL-P001-001")
        assert frame is None
        # 未缓存协议: 对方装好 ffmpeg 后无需重启即可启用
        assert "BL-P001-001" not in cam._protocol

    async def test_tc_cam_08_protocol_cached(self, cam_configs):
        """TC-CAM-08: 协议只探测一次, 后续采集复用缓存"""
        cam = CameraManager(cam_configs)
        cam._protocol["AC12309BH109"] = PROTO_JPEG
        with patch("bumbo_stats.printer.camera._tcp_reachable") as probe:
            await cam._capture_one("AC12309BH109")
        probe.assert_not_called()

    async def test_tc_cam_09_reload_resets(self, cam_configs):
        """TC-CAM-09: reload_configs 更新支持状态并重置协议缓存"""
        cam = CameraManager(cam_configs)
        cam._protocol["AC12309BH109"] = PROTO_JPEG
        new_configs = [
            PrinterConfig(name="P1S", serial="AC12309BH109", ip="1.1.1.9", access_code="0001"),
            PrinterConfig(name="New", serial="NEW123456789", ip="1.1.1.8", access_code="0003"),
        ]
        cam.reload_configs(new_configs)
        assert cam.is_supported("AC12309BH109") is True
        assert cam.is_supported("NEW123456789") is True
        assert "AC12309BH109" not in cam._protocol  # ip 变更需重探
        assert "NEW123456789" not in cam._protocol
