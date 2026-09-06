"""bootstrap.py —— 从已有数据集一次性初始化 gallery.

用法:
    python bootstrap.py --src ../local_data/
    python bootstrap.py --src ../local_data/anomaly/ --label anomaly
    python bootstrap.py --src ../local_data/normal/  --label normal

行为:
    1. 扫描 src 下所有图片 (支持子目录)
    2. 用 DINOv2 提特征
    3. 按 label ("anomaly" / "normal") 加入 gallery
    4. 保存到 gallery/anomaly_features.npy 等

支持子目录分层: 如 src=local_data/, 会扫描 anomaly/ 和 normal/ 子目录自动分类.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from typing import List, Tuple

from extractor import extract_batch
from knn import FeatureGallery

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GALLERY = os.path.join(HERE, "gallery")
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def scan_dir(src: str, label: str) -> List[str]:
    """递归收集 src 下所有图片路径."""
    paths = []
    for ext in IMG_EXT:
        # 注意: f"*.{ext}" 会产生 "*..jpg" (双点, 错误!)
        # 应该用 f"*{ext}" 或 '*' + ext
        pattern_top = os.path.join(src, "*" + ext)
        pattern_rec = os.path.join(src, "**", "*" + ext)
        paths.extend(glob.glob(pattern_top, recursive=False))
        paths.extend(glob.glob(pattern_rec, recursive=True))
    # 去重 + 排序
    paths = sorted(set(paths))
    return paths


def auto_label_from_dir(src: str) -> str:
    """根据 src 路径名猜 label. 命中 'normal' -> 'normal', 否则 'anomaly'."""
    base = os.path.basename(os.path.normpath(src)).lower()
    if base == "normal":
        return "normal"
    return "anomaly"


def main():
    ap = argparse.ArgumentParser(description="Bootstrap DINOv2 + kNN gallery")
    ap.add_argument("--src", required=True,
                    help="数据目录 (递归扫图)")
    ap.add_argument("--label", choices=["anomaly", "normal"], default=None,
                    help="强制指定 label (默认根据目录名猜)")
    ap.add_argument("--gallery", default=DEFAULT_GALLERY,
                    help=f"gallery 目录 (默认 {DEFAULT_GALLERY})")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印不实际写入")
    args = ap.parse_args()

    label = args.label or auto_label_from_dir(args.src)
    paths = scan_dir(args.src, label)

    if not paths:
        print(f"[!] {args.src} 下没找到任何图片 ({IMG_EXT})")
        sys.exit(1)

    print(f"[bootstrap] 扫描到 {len(paths)} 张 ({label}), src={args.src}")
    if args.dry_run:
        print("[dry-run] 跳过 DINOv2 前向与 gallery 写入")
        return

    # 加载/创建 gallery
    gallery = FeatureGallery(args.gallery)
    n0 = len(gallery)
    print(f"[bootstrap] 当前 gallery: a={len(gallery.anomaly_paths)} "
          f"n={len(gallery.normal_paths)}")

    # 提特征 (批量)
    t0 = time.time()
    feats = extract_batch(paths, batch_size=args.batch_size)
    dt = time.time() - t0
    print(f"[bootstrap] 提特征 {dt:.1f}s ({len(paths)/dt:.1f} 张/s, "
          f"shape={feats.shape})")

    # 逐条加入 gallery (让 .save() 也持久化)
    for i, (feat, p) in enumerate(zip(feats, paths)):
        gallery.add(feat, label, p)
        if (i + 1) % 50 == 0 or i == len(paths) - 1:
            print(f"   +{i+1}/{len(paths)}  ->  "
                  f"a={len(gallery.anomaly_paths)} "
                  f"n={len(gallery.normal_paths)}")

    n1 = len(gallery)
    print(f"\n[bootstrap] 完成! 新增 {n1-n0} 条 ({label}), "
          f"gallery 总计 {n1} 条.")
    print(f"[bootstrap] gallery 目录: {args.gallery}")


if __name__ == "__main__":
    main()