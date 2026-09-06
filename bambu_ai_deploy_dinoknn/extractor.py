"""DINOv2 (ViT-S/14) 特征提取器 —— 冻结、零训练、即录即用。

复用 scripts/dino_knn.extract, 后者已封装好:
  - 走本地 hub 仓库 + 本地权重 (零网络依赖)
  - 224×224 ImageNet 归一化
  - CLS token (L2 归一化后) -> [D] numpy

本模块只是为 deploy 包提供一个清晰的导入接口, 不重复实现。
"""
from __future__ import annotations

import os
import sys
from typing import Sequence

import numpy as np

# 把 baselines/scripts 加进 sys.path, 让 dino_knn 可被 import
# deploy 包位于 baselines/bambu_ai_deploy_dinoknn/, 上层 ../ 即 baselines/scripts
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# 复用项目里的 dino_knn (已含 load_model / extract / TFM 等)
import dino_knn  # noqa: E402

EMBED_DIM = dino_knn.EMBED_DIM   # 384 (ViT-S/14)
DINOV2_NAME = dino_knn.DINOV2_NAME
TFM = dino_knn.TFM


def extract_one(img_path: str) -> np.ndarray:
    """单图提特征 -> [D] float32 numpy (L2 归一化)."""
    return dino_knn.extract([img_path])[0]


def extract_one_from_pil(img: "Image.Image") -> np.ndarray:
    """从 PIL.Image 直接提特征 (无需写临时文件).

    走 dino_knn.load_model() + ImageNet 归一化 TFM, 与 extract_one 完全一致.
    适合来自摄像头/内存的实时流 (避免中间 JPEG 编解码).
    """
    import io
    import torch
    pil = img.convert("RGB")
    x = TFM(pil).unsqueeze(0)
    model = dino_knn.load_model()
    with torch.no_grad():
        feat = model(x.to(dino_knn.DEVICE))
    feat = torch.nn.functional.normalize(feat, dim=1)
    return feat.cpu().numpy()[0].astype("float32")


def extract_batch(img_paths: Sequence[str], batch_size: int = 32) -> np.ndarray:
    """批量提特征 -> [N, D] float32 numpy (L2 归一化)."""
    return dino_knn.extract(list(img_paths), bs=batch_size)


def warmup():
    """预加载 DINOv2 模型到内存 (避免第一次推理时卡顿)."""
    return dino_knn.load_model()


if __name__ == "__main__":
    # 快速自检
    import sys
    from PIL import Image
    if len(sys.argv) < 2:
        print("用法: python extractor.py <图片路径>")
        sys.exit(1)
    p = sys.argv[1]
    print(f"加载 DINOv2 {DINOV2_NAME}...")
    warmup()
    feat = extract_one(p)
    print(f"特征维度: {feat.shape}, dtype: {feat.dtype}, "
          f"L2 范数: {np.linalg.norm(feat):.4f} (≈1.0 表示归一化正确)")
    print(f"前 8 维: {feat[:8]}")