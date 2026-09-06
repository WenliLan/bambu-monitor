"""watcher.py —— 监控 data/ 文件夹, 自动提特征并加入 gallery (零训练热更新).

用法:
    from watcher import start_watch
    from detector import PrintDefectDetector
    det = PrintDefectDetector()
    obs = start_watch(det, data_dir="data")   # 阻塞

    # 或自定义 data_dir / gallery_dir / 子目录名:
    obs = start_watch(det, data_dir="adapting/data",
                            label_dirs={"good": "normal", "bad": "anomaly"})

依赖: pip install watchdog
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAVE_WATCHDOG = True
except ImportError:
    HAVE_WATCHDOG = False

from PIL import Image

from extractor import extract_one_from_pil
from knn import FeatureGallery

IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def _label_from_path(path: str, data_root: str,
                     label_dirs: dict) -> Optional[str]:
    """根据文件所在子目录名映射 label. 找不到返回 None."""
    rel = os.path.relpath(path, data_root).replace("\\", "/")
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    sub = parts[0].lower()
    return label_dirs.get(sub)


class GalleryWatcher(FileSystemEventHandler):
    """watchdog 事件处理器: 新文件 → 提特征 → 加入 gallery."""

    def __init__(self, gallery: FeatureGallery, data_root: str,
                 label_dirs: dict):
        super().__init__()
        self.gallery = gallery
        self.data_root = data_root
        self.label_dirs = label_dirs

    def on_created(self, event):
        if event.is_directory:
            return
        path = event.src_path
        if not path.lower().endswith(IMG_EXT):
            return
        # 文件可能还没写完, 等 0.5s
        time.sleep(0.5)
        if not os.path.exists(path):
            return
        label = _label_from_path(path, self.data_root, self.label_dirs)
        if label is None:
            print(f"[watch] 跳过 (未知子目录): {path}")
            return
        try:
            img = Image.open(path).convert("RGB")
            feat = extract_one_from_pil(img)
            self.gallery.add(feat, label, path)
            n_a = len(self.gallery.anomaly_paths)
            n_n = len(self.gallery.normal_paths)
            print(f"[watch] +1 {label:8s} -> {os.path.basename(path)} "
                  f"(gallery a={n_a} n={n_n})")
        except Exception as e:
            print(f"[watch] 处理失败 {path}: {e}")


def start_watch(detector, data_dir: str = "data",
                label_dirs: Optional[dict] = None,
                blocking: bool = True) -> Observer:
    """启动文件夹监控.

    参数:
        detector:     PrintDefectDetector 实例 (含 gallery)
        data_dir:     要监控的根目录
        label_dirs:   子目录名 -> label 的映射, 如 {"good": "normal", "bad": "anomaly"}
                      默认 {"anomaly": "anomaly", "normal": "normal", "uncertain": None}
        blocking:     True = 阻塞 (主循环), False = 后台

    返回:
        Observer 实例 (调用者可 .stop() 停止)
    """
    if not HAVE_WATCHDOG:
        raise ImportError(
            "watcher 需要 watchdog 库: pip install watchdog\n"
            "(此包不在 requirements 白名单, 用户主动装才生效)"
        )
    data_dir = os.path.abspath(data_dir)
    label_dirs = label_dirs or {
        "anomaly": "anomaly",
        "normal": "normal",
        "uncertain": None,  # 不自动加, 等用户标
    }

    handler = GalleryWatcher(detector.gallery, data_dir, label_dirs)
    obs = Observer()
    # 监控每个 label 子目录 (recursive=True 自动覆盖子子目录)
    for sub in os.listdir(data_dir) if os.path.exists(data_dir) else []:
        sub_path = os.path.join(data_dir, sub)
        if os.path.isdir(sub_path) and sub.lower() in label_dirs:
            obs.schedule(handler, sub_path, recursive=True)
            print(f"[watch] 监控: {sub_path}/  ({label_dirs[sub]} 类)")

    obs.start()
    if blocking:
        print(f"[watch] 按 Ctrl+C 停止")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            obs.stop()
            obs.join()
    return obs


# ============ CLI ============

if __name__ == "__main__":
    import argparse
    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, HERE)
    from detector import PrintDefectDetector

    ap = argparse.ArgumentParser(description="监控 data/ 自动加入 gallery")
    ap.add_argument("--data", default=os.path.join(HERE, "data"),
                    help="要监控的数据根目录")
    args = ap.parse_args()

    det = PrintDefectDetector()
    print(f"[init] {det.stats()}")
    start_watch(det, data_dir=args.data)