"""DINOv2 骨干网加载两条分支 (本地缓存 / torch.hub 在线回退) 的回归测试.

背景 (开源发布前复盘发现):
    dino_knn.load_model() 原本只有本地缓存分支, 缺缓存时直接抛 FileNotFoundError,
    而提示文案却写着 "会从 torch.hub 拉取" —— 实际并不存在该路径。
    他人 clone 仓库后(缓存不入库) AI 检测必然崩溃。
    现补上在线回退分支, 此测试锁死 "本地优先 / 缺失回退 / 回退失败给可操作提示" 三条行为。

依赖 torch, 无 torch 时自动跳过; 全程不触网(下载调用被替换), 不写任何真实配置或缓存。
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch", reason="dino_knn 依赖 torch")

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "bambu_ai_deploy_dinoknn"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import dino_knn  # noqa: E402


class _FakeModel:
    """记录 load_model() 收尾动作的假模型."""

    def __init__(self):
        self.eval_called = False
        self.to_called = False
        self.p = _FakeParam()

    def eval(self):
        self.eval_called = True
        return self

    def to(self, device):
        self.to_called = True
        return self

    def parameters(self):
        return [self.p]


class _FakeParam:
    requires_grad = True


@pytest.fixture
def branch_spy(monkeypatch, tmp_path):
    """把两条加载分支替换为哨兵, 记录实际命中的分支."""
    hub_local = tmp_path / "hub" / "facebookresearch_dinov2_main"
    ckpt_local = tmp_path / "checkpoints" / "dinov2_vits14_pretrain.pth"
    monkeypatch.setattr(dino_knn, "DINOV2_HUB_LOCAL", str(hub_local))
    monkeypatch.setattr(dino_knn, "DINOV2_CKPT_LOCAL", str(ckpt_local))
    monkeypatch.setattr(dino_knn, "_MODEL", None)

    hit = []

    def _fake_local():
        hit.append("local")
        return _FakeModel()

    def _fake_hub():
        hit.append("hub")
        return _FakeModel()

    monkeypatch.setattr(dino_knn, "_load_from_local", _fake_local)
    monkeypatch.setattr(dino_knn, "_load_from_hub", _fake_hub)
    return hit, hub_local, ckpt_local


def test_load_model_prefers_local_cache(branch_spy):
    """本地 hub 仓库与权重齐备时走离线分支, 不触网."""
    hit, hub_local, ckpt_local = branch_spy
    hub_local.mkdir(parents=True)
    ckpt_local.parent.mkdir(parents=True)
    ckpt_local.write_bytes(b"fake")

    model = dino_knn.load_model()

    assert hit == ["local"]
    assert model.eval_called and model.to_called
    assert model.p.requires_grad is False  # 骨干冻结


def test_load_model_falls_back_to_hub_when_cache_missing(branch_spy):
    """缓存缺失(他人 clone 的真实场景)时必须走在线回退, 而不是抛 FileNotFoundError."""
    hit, _, _ = branch_spy

    model = dino_knn.load_model()  # 目录与权重都不存在

    assert hit == ["hub"]
    assert model.eval_called and model.to_called


def test_load_model_is_cached_across_calls(branch_spy, monkeypatch):
    """第二次调用复用 _MODEL, 不重复加载骨干网."""
    hit, hub_local, ckpt_local = branch_spy
    hub_local.mkdir(parents=True)
    ckpt_local.parent.mkdir(parents=True)
    ckpt_local.write_bytes(b"fake")

    first = dino_knn.load_model()
    second = dino_knn.load_model()

    assert first is second
    assert hit == ["local"]


def test_hub_fallback_failure_gives_actionable_guidance(monkeypatch, tmp_path):
    """在线下载失败时, 错误信息必须同时给出网络前提与 Release 离线包路径."""
    def _boom(*args, **kwargs):
        raise OSError("target machine refused connection")

    monkeypatch.setattr(dino_knn.torch.hub, "load", _boom)
    monkeypatch.setattr(dino_knn, "BASE", str(tmp_path))

    with pytest.raises(RuntimeError) as excinfo:
        dino_knn._load_from_hub()

    msg = str(excinfo.value)
    assert "github.com" in msg
    assert "dinov2_cache" in msg
    assert "Release" in msg
