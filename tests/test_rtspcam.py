"""测试 RTSPS 取帧模块 - printer/rtspcam.py

测试用例:
  TC-RTSP-01: find_ffmpeg 优先使用 BUMBO_FFMPEG_EXE 环境变量
  TC-RTSP-02: 环境变量无效时回退系统 PATH
  TC-RTSP-03: 全部不可用时 find_ffmpeg 返回 None
  TC-RTSP-04: ffmpeg 缺失时 capture_rtsp_frame 返回 None 且只告警一次
  TC-RTSP-05: capture_rtsp_frame 成功时返回 ffmpeg stdout 的 JPEG
  TC-RTSP-06: ffmpeg 非零退出码时返回 None
  TC-RTSP-07: RTSPS 地址格式正确(凭据内嵌/322 端口/live/1)
"""

import subprocess
from unittest.mock import patch, MagicMock

from bambu_monitor.printer import rtspcam


def setup_function():
    rtspcam._reset_warn_state()


class TestFindFfmpeg:
    def test_tc_rtsp_01_env_var_first(self, tmp_path):
        """TC-RTSP-01: BUMBO_FFMPEG_EXE 指向存在的文件时优先使用"""
        exe = tmp_path / "myffmpeg.exe"
        exe.write_bytes(b"")
        with patch.dict(rtspcam.os.environ, {"BUMBO_FFMPEG_EXE": str(exe)}):
            assert rtspcam.find_ffmpeg() == str(exe)

    def test_tc_rtsp_02_path_fallback(self):
        """TC-RTSP-02: 环境变量未设时回退 shutil.which"""
        with patch.dict(rtspcam.os.environ, {}, clear=True), \
             patch.object(rtspcam.shutil, "which", return_value="/usr/bin/ffmpeg"):
            assert rtspcam.find_ffmpeg() == "/usr/bin/ffmpeg"

    def test_tc_rtsp_03_none_when_missing(self):
        """TC-RTSP-03: 环境变量/PATH/imageio 全不可用 -> None"""
        with patch.dict(rtspcam.os.environ, {}, clear=True), \
             patch.object(rtspcam.shutil, "which", return_value=None), \
             patch.dict("sys.modules", {"imageio_ffmpeg": None}):
            assert rtspcam.find_ffmpeg() is None


class TestCaptureRtspFrame:
    def test_tc_rtsp_04_no_ffmpeg_warn_once(self, caplog):
        """TC-RTSP-04: ffmpeg 缺失返回 None, 多次调用只告警一次"""
        import logging
        with patch.object(rtspcam, "find_ffmpeg", return_value=None):
            with caplog.at_level(logging.WARNING):
                assert rtspcam.capture_rtsp_frame("1.2.3.4", "1234") is None
                assert rtspcam.capture_rtsp_frame("1.2.3.4", "1234") is None
        warns = [r for r in caplog.records if "未找到 ffmpeg" in r.getMessage()]
        assert len(warns) == 1

    def test_tc_rtsp_05_success_returns_bytes(self):
        """TC-RTSP-05: 退出码 0 且有输出时返回 JPEG bytes"""
        completed = subprocess.CompletedProcess("ffmpeg", 0, stdout=b"\xff\xd8fakejpg", stderr=b"")
        with patch.object(rtspcam, "find_ffmpeg", return_value="ffmpeg"), \
             patch.object(rtspcam.subprocess, "run", return_value=completed):
            assert rtspcam.capture_rtsp_frame("1.2.3.4", "1234") == b"\xff\xd8fakejpg"

    def test_tc_rtsp_06_nonzero_exit(self):
        """TC-RTSP-06: ffmpeg 非零退出(如鉴权失败)返回 None"""
        completed = subprocess.CompletedProcess("ffmpeg", 1, stdout=b"", stderr=b"401 Unauthorized")
        with patch.object(rtspcam, "find_ffmpeg", return_value="ffmpeg"), \
             patch.object(rtspcam.subprocess, "run", return_value=completed):
            assert rtspcam.capture_rtsp_frame("1.2.3.4", "1234") is None

    def test_tc_rtsp_07_url_format(self):
        """TC-RTSP-07: 抓帧命令内嵌正确的 RTSPS 地址"""
        completed = subprocess.CompletedProcess("ffmpeg", 0, stdout=b"jpg", stderr=b"")
        run = MagicMock(return_value=completed)
        with patch.object(rtspcam, "find_ffmpeg", return_value="ffmpeg"), \
             patch.object(rtspcam.subprocess, "run", run):
            rtspcam.capture_rtsp_frame("192.168.1.50", "87654321")
        cmd = run.call_args[0][0]
        url = rtspcam.rtsp_frame_url("192.168.1.50", "87654321")
        assert url == "rtsps://bblp:87654321@192.168.1.50:322/streaming/live/1"
        assert url in cmd
        # 强制 TCP 传输（UDP 在很多 NAT/防火墙环境不通）
        assert "-rtsp_transport" in cmd and "tcp" in cmd

    def test_tc_rtsp_08_timeout_returns_none(self):
        """TC-RTSP-08: 抓帧超时返回 None 不抛异常"""
        with patch.object(rtspcam, "find_ffmpeg", return_value="ffmpeg"), \
             patch.object(rtspcam.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            assert rtspcam.capture_rtsp_frame("1.2.3.4", "1234") is None
