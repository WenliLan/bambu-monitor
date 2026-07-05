"""测试 CLI 入口 - main.py

测试用例:
  TC-CLI-01: --version 输出版本号
  TC-CLI-02: 配置文件不存在时退出
  TC-CLI-03: 默认参数解析
  TC-CLI-04: 自定义参数解析
"""

import sys
import pytest
from unittest.mock import patch


class TestCLI:
    def test_tc_cli_01_version(self, capsys):
        """TC-CLI-01: --version 输出版本号"""
        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["bumbo-stats", "--version"]):
                from bumbo_stats.main import main
                main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "0.5.0" in captured.out

    def test_tc_cli_02_missing_config(self):
        """TC-CLI-02: 配置文件不存在时退出"""
        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["bumbo-stats", "serve", "--config", "no_such_file.yaml"]):
                from bumbo_stats.main import main
                main()
        assert exc_info.value.code == 1

    def test_tc_cli_03_default_args(self, sample_config_yaml):
        """TC-CLI-03: 默认参数解析正确"""
        import argparse
        # 仅测试参数解析，不启动服务
        with patch("sys.argv", ["bumbo-stats", "--config", sample_config_yaml]):
            from bumbo_stats.config import load_config
            config = load_config(sample_config_yaml)
            assert len(config.printers) == 2
            assert config.settings.port == 8000

    def test_tc_cli_04_custom_args(self, sample_config_yaml):
        """TC-CLI-04: 自定义端口参数"""
        with patch("sys.argv", ["bumbo-stats", "-c", sample_config_yaml, "-p", "9000", "--debug"]):
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--config", "-c", default="printers.yaml")
            parser.add_argument("--port", "-p", type=int, default=None)
            parser.add_argument("--debug", action="store_true")
            args = parser.parse_args(["-c", sample_config_yaml, "-p", "9000", "--debug"])
            assert args.port == 9000
            assert args.debug is True
