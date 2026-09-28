"""DINOv2 + kNN 部署检测器 —— 零训练、增量适配、文件夹即热更新。

用法:
    from detector import PrintDefectDetector
    det = PrintDefectDetector()   # 自动加载 gallery/gallery 目录
    r = det.detect("test.jpg")     # 跑 k-NN
    print(r.to_dict())

工作流:
    用户丢图 data/anomaly/  → watcher.py 自动提特征加入 gallery
    → 推理时立即用上新图 (无需重启 detector)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union

import numpy as np
from PIL import Image

from extractor import extract_one_from_pil, warmup
from knn import FeatureGallery, KnnResult

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GALLERY_DIR = os.path.join(HERE, "gallery")
DEFAULT_ALERT_DIR = os.path.join(HERE, "alerts_review")
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


@dataclass
class DetectionResult:
    """单帧检测结果 (与旧版 pytorch 部署包同口径, 仅保留字段兼容)"""
    anomaly: bool
    probability: float          # k-NN 投出的 anomaly 占比
    threshold: float            # 决策阈值
    severity: str = ""          # high/medium/empty
    message: str = ""
    sub_class: str = ""         # "anomaly" / "normal" / "unknown"
    ms: float = 0.0             # 总耗时 (含 DINOv2 前向)
    nearest: list[dict] = field(default_factory=list)
    heatmap_path: Optional[str] = None  # 此模式无 Grad-CAM (ViT 无 conv)

    def to_dict(self) -> dict:
        return {
            "anomaly": self.anomaly,
            "probability": round(self.probability, 4),
            "threshold": self.threshold,
            "severity": self.severity,
            "message": self.message,
            "sub_class": self.sub_class,
            "ms": round(self.ms, 1),
            "n_nearest": len(self.nearest),
        }


class PrintDefectDetector:
    """DINOv2 (ViT-S/14) + kNN gallery 异常检测器.

    参数:
        gallery_dir:   gallery/ 目录 (含 anomaly/normal_features.npy 等)
        k:             k-NN 的 k
        threshold:     anomaly 占比阈值 (默认 0.5 = 多数投票)
        alert_frames:  push() 模式连续 N 帧异常才触发报警
        save_alerts:   是否保存报警图片到 alerts_review/
        save_heatmap_always: 每帧保存原图到 alerts_review/all/
        alert_dir:     报警目录
    """

    def __init__(
        self,
        gallery_dir: str = DEFAULT_GALLERY_DIR,
        k: int = 5,
        threshold: Optional[float] = None,
        alert_frames: int = 3,
        save_alerts: bool = True,
        save_heatmap_always: bool = False,
        alert_dir: str = DEFAULT_ALERT_DIR,
    ):
        # 1) 加载 gallery
        # 先创建空 gallery 实例探一下 threshold (来自 meta.json)
        probe = FeatureGallery(gallery_dir, k=k,
                               decision_threshold=threshold or 0.5)
        self.gallery = probe
        self.k = probe.k
        self.threshold = threshold if threshold is not None else probe.decision_threshold

        # 2) DINOv2 模型预热 (避免首次 detect 卡顿)
        warmup()

        # 3) 报警参数
        self.alert_frames = alert_frames
        self.save_alerts = save_alerts
        self.save_heatmap_always = save_heatmap_always
        self.alert_dir = alert_dir
        self.heatmap_dir = os.path.join(alert_dir, "all")
        self._consecutive = 0
        self._history: list[DetectionResult] = []
        self._pending_frames: list[Image.Image] = []

        if self.save_alerts:
            os.makedirs(self.alert_dir, exist_ok=True)
            if self.save_heatmap_always:
                os.makedirs(self.heatmap_dir, exist_ok=True)

    # ============ 主接口 ============

    def detect(
        self,
        image: Union[str, Image.Image, np.ndarray, bytes],
        return_nearest: bool = False,
    ) -> DetectionResult:
        """单帧检测: 输入图片 -> DetectionResult."""
        img = self._to_pil(image)
        t0 = time.time()

        # 1) 提特征
        feat = extract_one_from_pil(img)

        # 2) k-NN 检索
        kr = self.gallery.predict(feat, k=self.k, return_nearest=return_nearest)
        ms = (time.time() - t0) * 1000

        result = DetectionResult(
            anomaly=kr.anomaly,
            probability=kr.probability,
            threshold=kr.threshold,
            severity=self._severity(kr.probability),
            message=(f"检测到打印异常 (P={kr.probability:.2f}, "
                     f"阈值={kr.threshold:.2f})") if kr.anomaly else "",
            sub_class=kr.sub_class,
            ms=ms,
            nearest=kr.nearest,
        )

        # 3) save_heatmap_always 模式 (ViT 无 Grad-CAM, 这里只存原图)
        if self.save_heatmap_always and self.save_alerts:
            try:
                self._save_single_frame(img, result)
            except Exception as e:
                print(f"[warn] 帧保存失败: {e}")

        self._history.append(result)
        self._pending_frames.append(img)
        if len(self._pending_frames) > self.alert_frames:
            self._pending_frames.pop(0)

        return result

    def push(
        self,
        image: Union[str, Image.Image, np.ndarray, bytes],
    ) -> Optional[DetectionResult]:
        """监控模式: 连续 alert_frames 帧异常才报警, 否则 None."""
        result = self.detect(image)
        if result.anomaly:
            self._consecutive += 1
        else:
            self._consecutive = 0
            self._pending_frames.clear()

        if self._consecutive >= self.alert_frames:
            self._consecutive = 0
            result.message = (
                f"[!] 连续 {self.alert_frames} 帧检测到异常! "
                f"最近概率={result.probability:.2f}, 建议停机检查。"
            )
            result.severity = "high"
            if self.save_alerts:
                saved = self._save_alert_images(result)
                result.message += f" 图片已保存: {saved}"
            return result
        return None

    def add_to_gallery(
        self,
        image: Union[str, Image.Image, np.ndarray, bytes],
        label: str,
        path: Optional[str] = None,
    ) -> None:
        """手动加图到 gallery (label=anomaly/normal)."""
        img = self._to_pil(image)
        feat = extract_one_from_pil(img)
        path = path or (image if isinstance(image, str) else f"<memory:{label}>")
        self.gallery.add(feat, label, path)
        print(f"[gallery] +1 {label}: {path} "
              f"(a={len(self.gallery.anomaly_paths)} "
              f"n={len(self.gallery.normal_paths)})")

    def reset(self) -> None:
        """重置连续帧计数 + 历史."""
        self._consecutive = 0
        self._pending_frames.clear()
        self._history.clear()

    @property
    def history(self) -> list[DetectionResult]:
        return self._history

    def stats(self) -> dict:
        """打印当前状态."""
        s = self.gallery.stats()
        s.update({
            "alert_frames": self.alert_frames,
            "history_len": len(self._history),
        })
        return s

    # ============ 保存 ============

    def _save_single_frame(self, img: Image.Image, result: DetectionResult) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        status = "anomaly" if result.anomaly else "normal"
        folder = os.path.join(
            self.heatmap_dir,
            f"{ts}_P{result.probability:.3f}_{status}",
        )
        os.makedirs(folder, exist_ok=True)
        img.save(os.path.join(folder, "frame.jpg"), quality=95)
        meta = {
            "time": datetime.now().isoformat(),
            "probability": round(result.probability, 4),
            "sub_class": result.sub_class,
            "nearest": result.nearest[:3],   # 只存前 3 近邻
        }
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def _save_alert_images(self, result: DetectionResult) -> str:
        """保存报警事件 (连续 N 帧) 到 alerts_review/<时间戳>_P<prob>/."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        event_dir = os.path.join(
            self.alert_dir,
            f"{ts}_P{result.probability:.2f}",
        )
        os.makedirs(event_dir, exist_ok=True)

        for i, frame in enumerate(self._pending_frames):
            frame.save(os.path.join(event_dir, f"frame_{i+1}.jpg"), quality=95)

        meta = {
            "time": datetime.now().isoformat(),
            "decision_rule": "DINOv2 ViT-S/14 + kNN (k={}, threshold={})".format(
                self.k, self.threshold),
            "probability": round(result.probability, 4),
            "sub_class": result.sub_class,
            "alert_frames": self.alert_frames,
            "severity": result.severity,
            "nearest_top3": result.nearest[:3],
            "review_status": "pending",
        }
        with open(os.path.join(event_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        self._pending_frames.clear()
        return os.path.basename(event_dir)

    # ============ 内部 ============

    @staticmethod
    def _severity(prob: float) -> str:
        if prob >= 0.85:
            return "high"
        if prob >= 0.5:
            return "medium"
        return ""

    @staticmethod
    def _to_pil(image) -> Image.Image:
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        if isinstance(image, str):
            return Image.open(image).convert("RGB")
        if isinstance(image, bytes):
            from io import BytesIO
            return Image.open(BytesIO(image)).convert("RGB")
        if isinstance(image, np.ndarray):
            if image.ndim == 3 and image.shape[2] == 3:
                return Image.fromarray(image[:, :, ::-1].copy()).convert("RGB")
            return Image.fromarray(image).convert("RGB")
        raise TypeError(f"不支持的输入类型: {type(image)}")


# ============ CLI ============

def detect_image(image_path: str, **kwargs) -> dict:
    """一次性检测单张图片."""
    det = PrintDefectDetector(**kwargs)
    return det.detect(image_path, return_nearest=True).to_dict()


# ============ CLI ============

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="DINOv2 + kNN 异常检测 (零训练)")
    ap.add_argument("images", nargs="+", help="图片路径 (可多个)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--nearest", action="store_true", help="打印最近邻")
    args = ap.parse_args()

    det = PrintDefectDetector(k=args.k, threshold=args.threshold,
                              save_alerts=False)
    print(f"[stats] {det.stats()}")

    for p in args.images:
        r = det.detect(p, return_nearest=args.nearest)
        flag = "[!] 异常" if r.anomaly else "[OK] 正常"
        print(f"\n{flag}  P={r.probability:.3f}  sub={r.sub_class}  "
              f"({r.ms:.0f}ms)  {p}")
        if args.nearest and r.nearest:
            for nn in r.nearest[:3]:
                print(f"    #{nn['rank']} {nn['label']:8s} "
                      f"sim={nn['similarity']:.3f}  {os.path.basename(nn['path'])}")