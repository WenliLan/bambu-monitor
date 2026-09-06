"""scripts/init_gallery.py —— 一键初始化 gallery.

用法 (在 bambu_ai_deploy_dinoknn/ 目录下):
    python scripts/init_gallery.py            # 自动扫 ../local_data/ 的 anomaly/ 和 normal/
    python scripts/init_gallery.py --src ../benchmark/train  # 自定义源
    python scripts/init_gallery.py --append   # 不清空, 追加到现有 gallery
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY_DIR = os.path.normpath(os.path.join(HERE, ".."))


def main():
    ap = argparse.ArgumentParser(description="初始化 DINOv2 + kNN gallery")
    ap.add_argument("--src", default=os.path.normpath(
        os.path.join(DEPLOY_DIR, "..", "local_data")),
        help="数据根目录 (默认 ../local_data)")
    ap.add_argument("--anomaly-subdir", default="anomaly",
                    help="异常子目录名 (默认 anomaly)")
    ap.add_argument("--normal-subdir", default="normal",
                    help="正常子目录名 (默认 normal)")
    ap.add_argument("--limit", type=int, default=None,
                    help="每类最多取几张 (调试用)")
    ap.add_argument("--batch-size", type=int, default=16)
    args = ap.parse_args()

    # 复用 deploy 目录下的 bootstrap.py
    bootstrap = os.path.join(DEPLOY_DIR, "bootstrap.py")
    if not os.path.exists(bootstrap):
        print(f"[!] 找不到 {bootstrap}")
        sys.exit(1)

    for label, sub in [("anomaly", args.anomaly_subdir),
                       ("normal", args.normal_subdir)]:
        src = os.path.join(args.src, sub)
        if not os.path.isdir(src):
            print(f"[跳过] {src} 不存在")
            continue
        cmd = [sys.executable, bootstrap,
               "--src", src,
               "--label", label,
               "--gallery", os.path.join(DEPLOY_DIR, "gallery"),
               "--batch-size", str(args.batch_size)]
        print(f"\n{'='*60}\n>>> {label}: {src}\n{'='*60}")
        subprocess.run(cmd, check=True)

    print("\n[done] gallery 初始化完成!")
    print(f"  gallery 目录: {os.path.join(DEPLOY_DIR, 'gallery')}")


if __name__ == "__main__":
    main()