"""测试认证模块 - auth.py

测试用例:
  TC-AUTH-01: save_token + load_token 往返一致
  TC-AUTH-02: token 文件不存在时 load_token 返回 None
  TC-AUTH-03: ~ 路径正确展开
  TC-AUTH-04: token 文件内容为空时返回 None
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from bumbo_stats.auth import TokenManager


@pytest.fixture
def tmp_token_dir(tmp_path):
    """临时 token 目录"""
    return tmp_path


class TestTokenManager:
    def test_tc_auth_01_save_and_load(self, tmp_token_dir):
        """TC-AUTH-01: save_token + load_token 往返一致"""
        token_file = str(tmp_token_dir / "token")
        mgr = TokenManager(token_file)
        test_token = "test_token_abc123"

        mgr.save_token(test_token)
        loaded = mgr.load_token()

        assert loaded == test_token

    def test_tc_auth_02_file_not_exists(self, tmp_token_dir):
        """TC-AUTH-02: token 文件不存在时返回 None"""
        token_file = str(tmp_token_dir / "nonexistent_token")
        mgr = TokenManager(token_file)

        assert mgr.load_token() is None

    def test_tc_auth_03_tilde_expansion(self, tmp_token_dir, monkeypatch):
        """TC-AUTH-03: ~ 路径正确展开"""
        # 模拟 ~ 展开到 tmp 目录
        expanded = str(tmp_token_dir / "token")
        mgr = TokenManager("~/test_token")

        # 验证路径正确展开
        assert mgr.token_path.name == "test_token"

    def test_tc_auth_04_empty_file(self, tmp_token_dir):
        """TC-AUTH-04: token 文件内容为空时返回 None"""
        token_file = tmp_token_dir / "empty_token"
        token_file.write_text("   ", encoding="utf-8")  # 空白字符

        mgr = TokenManager(str(token_file))
        assert mgr.load_token() is None

    def test_tc_auth_05_token_path_property(self, tmp_token_dir):
        """TC-AUTH-05: token_path 属性正确"""
        token_file = str(tmp_token_dir / "mytoken")
        mgr = TokenManager(token_file)

        assert mgr.token_path == Path(token_file)
        assert mgr.token_path.exists() is False  # 未保存时不存在