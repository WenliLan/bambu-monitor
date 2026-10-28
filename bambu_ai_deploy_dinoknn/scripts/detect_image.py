"""scripts/detect_image.py —— 单图/批量检测 CLI.

用法:
    python scripts/detect_image.py test.jpg                    # 单图
    python scripts/detect_image.py test1.jpg test2.jpg ...     # 多图
    python scripts/detect_image.py frames/*.jpg                # 通配
    python scripts/detect_image.py test.jpg --nearest          # 显示最近邻
    python scripts/detect_image.py test.jpg --save-alerts      # 保存到 alerts_review/
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY_DIR = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, DEPLOY_DIR)

from detector import PrintDefectDetector


def main():
    ap = argparse.ArgumentParser(description="单图/批量异常检测 (DINOv2 + kNN)")
    ap.add_argument("images", nargs="+", help="图片路径 (支持通配)")
    ap.add_argument("--k", type=int, default=5, help="k-NN 的 k (默认 5)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="anomaly 占比阈值 (默认 0.7)")
    ap.add_argument("--nearest", action="store_true",
                    help="显示 top-k 最近邻图片")
    ap.add_argument("--save-alerts", action="store_true",
                    help="保存异常图片到 alerts_review/")
    ap.add_argument("--save-all", action="store_true",
                    help="保存每帧到 alerts_review/all/")
    args = ap.parse_args()

    # 展开通配符
    img_paths = []
    for pat in args.images:
        if "*" in pat or "?" in pat:
            img_paths.extend(sorted(glob.glob(pat)))
        else:
            img_paths.append(pat)
    if not img_paths:
        print("[!] 没有匹配的图片")
        sys.exit(1)

    det = PrintDefectDetector(
        k=args.k,
        threshold=args.threshold,
        save_alerts=args.save_alerts,
        save_heatmap_always=args.save_all,
    )
    print(f"[init] {det.stats()}\n")

    n_anomaly = 0
    for p in img_paths:
        if not os.path.exists(p):
            print(f"[!] 跳过 (不存在): {p}")
            continue
        r = det.detect(p, return_nearest=args.nearest)
        flag = "[!] 异常" if r.anomaly else "[OK] 正常"
        n_anomaly += int(r.anomaly)
        print(f"{flag}  P={r.probability:.3f}  sub={r.sub_class:7s}  "
              f"({r.ms:.0f}ms)  {os.path.basename(p)}")
        if args.nearest and r.nearest:
            for nn in r.nearest[:3]:
                print(f"        #{nn['rank']} {nn['label']:8s} "
                      f"sim={nn['similarity']:.3f}  "
                      f"{os.path.basename(nn['path'])}")
    print(f"\n汇总: {n_anomaly}/{len(img_paths)} 张判为异常")


if __name__ == "__main__":
    main()