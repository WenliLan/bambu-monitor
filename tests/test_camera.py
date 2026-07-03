"""测试摄像头管理器 - printer/camera.py

测试用例:
  TC-CAM-01: X1 系列不支持摄像头
  TC-CAM-02: P1S/A1 系列支持摄像头（有 ip+access_code）
  TC-CAM-03: 无 ip/access_code 时不支持摄像头
  TC-CAM-04: get_frame 无缓存时返回 None
"""

import pytest
from unittest.mock import MagicMock

from bumbo_stats.config import PrinterConfig
from bumbo_stats.printer.camera import CameraManager


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
    def test_tc_cam_01_x1_not_supported(self, cam_configs):
        """TC-CAM-01: X1 系列 (BL-P001) 不支持摄像头"""
        cam = CameraManager(cam_configs)
        assert cam.is_supported("BL-P001-001") is False

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