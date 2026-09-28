"""kNN predict 两条检索分支 (faiss / numpy) 的一致性回归测试.

背景 (v0.5.15 复盘发现的生产级 bug):
    相似度加权投票改版后, predict() 统一用 `sims[idxs]` 取近邻相似度,
    但 faiss 分支返回的 sims 已是 top-k (长度 k), numpy 分支是全量 (长度 N),
    导致 faiss 分支 IndexError 崩溃。作者本机 Anaconda 装有 faiss,
    生产 ai_guard 每次检测都会命中该崩溃路径。此测试锁死两条分支行为一致。

依赖 torch (经 extractor->dino_knn 导入链), 无 torch 时自动跳过;
faiss 分支仅在装了 faiss 时执行。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("torch", reason="knn 经 extractor 依赖 torch")

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "bambu_ai_deploy_dinoknn"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import knn  # noqa: E402
from knn import FeatureGallery  # noqa: E402

D = knn.EMBED_DIM  # 384


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _make_gallery(tmp_path, n_anomaly=60, n_normal=60, seed=0):
    """在临时目录构造一个可复现的 gallery (>=100 条以触发 faiss 分支)."""
    rng = np.random.default_rng(seed)
    anom = np.array([_unit(rng.normal(size=D)) for _ in range(n_anomaly)],
                    dtype="float32")
    norm = np.array([_unit(rng.normal(size=D)) for _ in range(n_normal)],
                    dtype="float32")
    np.save(tmp_path / "anomaly_features.npy", anom)
    np.save(tmp_path / "normal_features.npy", norm)
    (tmp_path / "anomaly_paths.json").write_text(
        json.dumps([f"anomaly/{i}.jpg" for i in range(n_anomaly)]),
        encoding="utf-8")
    (tmp_path / "normal_paths.json").write_text(
        json.dumps([f"normal/{i}.jpg" for i in range(n_normal)]),
        encoding="utf-8")
    # 不写 meta.json -> 构造函数用传入参数, 且会自行创建
    return anom, norm


def _brute_expected(g, query, k=5):
    """纯 numpy 暴力复算加权概率与 top-k 相似度, 作为两分支的公共基准."""
    all_feats = np.vstack([g.anomaly_feats, g.normal_feats])
    labels = np.array([1] * len(g.anomaly_feats) + [0] * len(g.normal_feats))
    sims = (all_feats @ query.astype("float32").reshape(-1, 1)).flatten()
    idx = np.argsort(-sims)[:k]
    ts = np.clip(sims[idx], 0, None)
    prob = float(ts[labels[idx] == 1].sum() / ts.sum())
    return prob, np.sort(ts)[::-1]   # top-k 相似度降序, 与分支无关


@pytest.fixture
def gallery(tmp_path):
    _make_gallery(tmp_path)
    return FeatureGallery(str(tmp_path), k=5, decision_threshold=0.5)


def test_predict_numpy_branch(gallery):
    rng = np.random.default_rng(123)
    query = _unit(rng.normal(size=D)).astype("float32")
    exp_prob, exp_top = _brute_expected(gallery, query)

    gallery.k = 5
    knn.HAVE_FAISS = False  # 走 numpy 分支
    r = gallery.predict(query, return_nearest=True)

    assert r.probability == pytest.approx(exp_prob, abs=1e-5)
    assert len(r.nearest) == 5
    got = sorted((nn["similarity"] for nn in r.nearest), reverse=True)
    assert got == pytest.approx(list(exp_top), abs=1e-5)


@pytest.mark.parametrize("_", [0])
def test_predict_faiss_branch(gallery, _):
    pytest.importorskip("faiss", reason="faiss 分支需安装 faiss")
    assert len(gallery) >= 100  # 确保命中 faiss 分支条件
    rng = np.random.default_rng(123)
    query = _unit(rng.normal(size=D)).astype("float32")
    exp_prob, exp_top = _brute_expected(gallery, query)

    knn.HAVE_FAISS = True  # 走 faiss 分支
    r = gallery.predict(query, return_nearest=True)

    assert r.probability == pytest.approx(exp_prob, abs=1e-5)
    assert len(r.nearest) == 5
    got = sorted((nn["similarity"] for nn in r.nearest), reverse=True)
    assert got == pytest.approx(list(exp_top), abs=1e-5)


def test_both_branches_agree(gallery):
    """核心回归: faiss 与 numpy 分支对同一 query 必须给出完全一致的概率."""
    pytest.importorskip("faiss")
    rng = np.random.default_rng(7)
    query = _unit(rng.normal(size=D)).astype("float32")

    knn.HAVE_FAISS = False
    p_np = gallery.predict(query).probability
    knn.HAVE_FAISS = True
    p_fa = gallery.predict(query).probability
    assert p_np == pytest.approx(p_fa, abs=1e-6)
