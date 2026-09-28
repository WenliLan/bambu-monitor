"""k-NN 特征记忆库 —— 零训练增量适配的核心。

设计原则:
  1. 特征向量持久化 (.npy), 重启不丢
  2. 单条追加 (add), 即时生效
  3. cosine 距离 (特征已 L2 归一化 → 内积)
  4. faiss IndexFlatIP (内积, 对 L2-normalized 向量等价于 cosine)
  5. 决策: k 个近邻的相似度加权 anomaly 占比 → 0~1 概率

存储:
  gallery/
  ├── anomaly_features.npy   # [N_a, D] float32, L2-normalized
  ├── normal_features.npy    # [N_n, D] float32, L2-normalized
  ├── anomaly_paths.json     # 对应图片路径 (审计)
  ├── normal_paths.json
  └── meta.json              # 创建时间/参数

为什么不直接复用 faiss 索引:
  - faiss 索引是只读的, 增量添加需要重建或 IndexIDMap (复杂)
  - numpy 数组 + 增量 faiss 重建更直观 (千级规模毫秒级)
  - 容量上界: 几万张内 numpy 够用 (几十万要换 HNSW/IVF)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Sequence

import numpy as np

try:
    import faiss
    HAVE_FAISS = True
except ImportError:
    HAVE_FAISS = False

from extractor import EMBED_DIM


@dataclass
class KnnResult:
    """单次 k-NN 检索结果"""
    anomaly: bool                          # 是否判为异常
    probability: float                     # P(anomaly) ∈ [0, 1]
    threshold: float                       # 决策阈值
    sub_class: str = ""                    # 子类名 ("anomaly" 或 "normal")
    nearest: list[dict] = field(default_factory=list)   # top-k 近邻 (可解释)
    ms: float = 0.0                        # 检索耗时


class FeatureGallery:
    """DINOv2 特征记忆库: 增量追加 + k-NN 检索. 零训练."""

    def __init__(
        self,
        gallery_dir: str,
        k: int = 5,
        decision_threshold: float = 0.5,
    ):
        """
        参数:
            gallery_dir:         gallery/ 目录路径
            k:                   k-NN 的 k (默认 5)
            decision_threshold:  加权 anomaly 占比 ≥ 阈值 → 判 anomaly (默认 0.5)
        """
        self.dir = gallery_dir
        self.k = k
        self.decision_threshold = decision_threshold
        os.makedirs(self.dir, exist_ok=True)

        # 两个 numpy 数组: anomaly / normal
        self.anomaly_feats: np.ndarray = self._load_npy("anomaly_features.npy")
        self.normal_feats: np.ndarray = self._load_npy("normal_features.npy")
        self.anomaly_paths: list[str] = self._load_json("anomaly_paths.json")
        self.normal_paths: list[str] = self._load_json("normal_paths.json")

        # meta.json (创建时间, k, threshold)
        meta_path = os.path.join(self.dir, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            # 以现有值优先 (用户可能手动调过 threshold)
            self.decision_threshold = meta.get("decision_threshold",
                                              self.decision_threshold)
            self.k = meta.get("k", self.k)
            self._created = meta.get("created", time.strftime("%Y-%m-%dT%H:%M:%S"))
        else:
            self._created = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._save_meta()

    # ============ 持久化 ============

    def _load_npy(self, fn: str) -> np.ndarray:
        p = os.path.join(self.dir, fn)
        if not os.path.exists(p):
            return np.zeros((0, EMBED_DIM), dtype="float32")
        return np.load(p)

    def _load_json(self, fn: str) -> list:
        p = os.path.join(self.dir, fn)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def _save_npy(self, fn: str, arr: np.ndarray) -> None:
        np.save(os.path.join(self.dir, fn), arr)

    def _save_json(self, fn: str, obj) -> None:
        with open(os.path.join(self.dir, fn), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0)

    def _save_meta(self) -> None:
        meta = {
            "created": self._created,
            "k": self.k,
            "decision_threshold": self.decision_threshold,
            "n_anomaly": len(self.anomaly_paths),
            "n_normal": len(self.normal_paths),
            "embed_dim": EMBED_DIM,
            "model": "dinov2_vits14 (DINOv2 ViT-S/14, 384-d CLS token)",
        }
        with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def save(self) -> None:
        """全量保存到磁盘."""
        self._save_npy("anomaly_features.npy", self.anomaly_feats)
        self._save_npy("normal_features.npy", self.normal_feats)
        self._save_json("anomaly_paths.json", self.anomaly_paths)
        self._save_json("normal_paths.json", self.normal_paths)
        self._save_meta()

    # ============ 增删改 ============

    def add(self, feat: np.ndarray, label: str, path: str) -> None:
        """追加单条样本. label ∈ {"anomaly", "normal"}."""
        assert label in ("anomaly", "normal"), f"label 必须是 anomaly/normal, 收到 {label}"
        assert feat.shape == (EMBED_DIM,), f"feat 形状应为 ({EMBED_DIM},)"
        feat = feat.astype("float32").reshape(1, -1)
        if label == "anomaly":
            self.anomaly_feats = np.vstack([self.anomaly_feats, feat]) \
                if len(self.anomaly_feats) else feat
            self.anomaly_paths.append(path)
        else:
            self.normal_feats = np.vstack([self.normal_feats, feat]) \
                if len(self.normal_feats) else feat
            self.normal_paths.append(path)
        self.save()

    def remove(self, path: str) -> bool:
        """按 path 删除一条. 返回是否找到."""
        for store, paths, feats in [
            ("anomaly", self.anomaly_paths, self.anomaly_feats),
            ("normal", self.normal_paths, self.normal_feats),
        ]:
            if path in paths:
                idx = paths.index(path)
                if len(feats) > 1:
                    new_feats = np.delete(feats, idx, axis=0)
                else:
                    new_feats = np.zeros((0, EMBED_DIM), dtype="float32")
                if store == "anomaly":
                    self.anomaly_feats = new_feats
                    self.anomaly_paths = paths[:idx] + paths[idx + 1:]
                else:
                    self.normal_feats = new_feats
                    self.normal_paths = paths[:idx] + paths[idx + 1:]
                self.save()
                return True
        return False

    def clear(self, label: Optional[str] = None) -> None:
        """清空. label=None 清空全部, 否则只清空指定类别."""
        if label in (None, "anomaly"):
            self.anomaly_feats = np.zeros((0, EMBED_DIM), dtype="float32")
            self.anomaly_paths = []
        if label in (None, "normal"):
            self.normal_feats = np.zeros((0, EMBED_DIM), dtype="float32")
            self.normal_paths = []
        self.save()

    # ============ 检索 ============

    def __len__(self) -> int:
        return len(self.anomaly_paths) + len(self.normal_paths)

    def predict(
        self,
        feat: np.ndarray,
        k: Optional[int] = None,
        return_nearest: bool = False,
    ) -> KnnResult:
        """单图特征 -> k-NN 决策.

        决策规则 (相似度加权投票):
          - 取 k 个最近邻, P(anomaly) = Σsim(anomaly) / Σsim(全部近邻)
          - P(anomaly) >= threshold → anomaly=True
          - 加权而非等权: 避免 gallery 自身成员被 normal 数量优势票淹没
            (实测旧等权版对照片少的异常类如 clog 系统性漏报)

        边界情况:
          - gallery 为空 → 返回 0.5 (未初始化, 不决策)
          - 单一类别 → P = 0 或 1
        """
        t0 = time.time()
        k = k or self.k
        n_anomaly = len(self.anomaly_feats)
        n_normal = len(self.normal_feats)

        # 边界: 空 gallery
        if n_anomaly == 0 and n_normal == 0:
            return KnnResult(
                anomaly=False, probability=0.5,
                threshold=self.decision_threshold,
                sub_class="unknown",
                ms=(time.time() - t0) * 1000,
            )

        # 合并
        all_feats = (np.vstack([self.anomaly_feats, self.normal_feats])
                     if n_anomaly and n_normal
                     else (self.anomaly_feats if n_anomaly else self.normal_feats))
        # labels: anomaly=1, normal=0
        all_labels = np.array(
            [1] * n_anomaly + [0] * n_normal,
            dtype="int32",
        )
        # 路径
        all_paths = self.anomaly_paths + self.normal_paths

        # 内积 (cosine, 因已 L2 归一化)
        q = feat.astype("float32").reshape(1, -1)
        # 统一产出: idxs = top-k 全局索引(按距离序), top_sims = 对应的 top-k 相似度(同序)
        # 用 faiss (如果有) 或 numpy
        if HAVE_FAISS and len(all_feats) >= 100:
            index = faiss.IndexFlatIP(all_feats.shape[1])
            index.add(all_feats)
            fa_sims, fa_idxs = index.search(q, min(k, len(all_feats)))
            idxs = fa_idxs[0]        # (k,) 全局索引
            top_sims = fa_sims[0]    # (k,) 已按序的相似度
        else:
            sims_all = (all_feats @ q.T).flatten()   # (N,) 全量
            idxs = np.argsort(-sims_all)[:k]          # (k,) 全局索引
            top_sims = sims_all[idxs]                 # (k,) 对应的相似度

        # 统计: 相似度加权投票 (sim 已 L2 归一化非负; 全 0 时退回等权)
        neighbor_labels = all_labels[idxs]
        neighbor_sims = np.clip(np.asarray(top_sims).flatten(), 0.0, None)
        w_sum = float(neighbor_sims.sum())
        if w_sum > 1e-8:
            prob = float(neighbor_sims[neighbor_labels == 1].sum() / w_sum)
        else:
            prob = int(neighbor_labels.sum()) / k
        anomaly = prob >= self.decision_threshold
        sub = "anomaly" if anomaly else "normal"

        # top-k 详情 (可解释): neighbor_sims[i] 与 idxs[i] 一一对应(均为排名序)
        nearest = []
        if return_nearest:
            for i, idx in enumerate(idxs):
                nearest.append({
                    "rank": i + 1,
                    "path": all_paths[idx],
                    "label": "anomaly" if all_labels[idx] == 1 else "normal",
                    "similarity": float(neighbor_sims[i]),
                })

        return KnnResult(
            anomaly=anomaly,
            probability=prob,
            threshold=self.decision_threshold,
            sub_class=sub,
            nearest=nearest,
            ms=(time.time() - t0) * 1000,
        )

    # ============ 状态查询 ============

    def update_threshold(self, new_threshold: float) -> float:
        """动态更新决策阈值，并持久化到 meta.json

        关键修复：原来仅修改 detector.threshold 不够，detect() 实际读取的是
        self.decision_threshold（被 FeatureGallery.__init__ 从 meta.json 覆盖）。
        暴露此方法让调用方能原子地"更新+写 meta.json"，避免重启后回到旧值。

        Args:
            new_threshold: 新的决策阈值（0~1），由调用方负责截断

        Returns:
            旧阈值
        """
        old = self.decision_threshold
        self.decision_threshold = float(new_threshold)
        try:
            self._save_meta()
        except Exception as e:
            print(f"[warn] gallery meta.json 持久化失败（运行时已更新）: {e}")
        return old

    def stats(self) -> dict:
        return {
            "n_anomaly": len(self.anomaly_paths),
            "n_normal": len(self.normal_paths),
            "total": len(self),
            "k": self.k,
            "decision_threshold": self.decision_threshold,
            "embed_dim": EMBED_DIM,
            "created": self._created,
            "has_faiss": HAVE_FAISS,
        }


if __name__ == "__main__":
    # 快速自检: 空 gallery
    g = FeatureGallery(gallery_dir="/tmp/_test_gallery", k=5)
    print("stats:", g.stats())
    import shutil
    shutil.rmtree("/tmp/_test_gallery", ignore_errors=True)