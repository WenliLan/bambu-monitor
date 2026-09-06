"""scripts/monitor.py —— 持续监控摄像头 / 视频流, 异常时报警.

用法:
    python scripts/monitor.py                                   # 默认摄像头 (0)
    python scripts/monitor.py --source 1                        # 外接 USB 摄像头
    python scripts/monitor.py --source rtmp://.../stream        # RTSP/RTMP 流
    python scripts/monitor.py --source video.mp4                # 视频文件
    python scripts/monitor.py --interval 1.0 --alert-frames 5   # 1s/帧, 连续5帧才报警

依赖 (可选): pip install opencv-python
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY_DIR = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, DEPLOY_DIR)

from detector import PrintDefectDetector


def main():
    ap = argparse.ArgumentParser(description="持续监控摄像头 (DINOv2 + kNN)")
    ap.add_argument("--source", default="0",
                    help="视频源: 摄像头编号 / RTSP URL / 视频文件路径")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="每 N 秒抓一帧 (默认 1s)")
    ap.add_argument("--alert-frames", type=int, default=3,
                    help="连续 N 帧异常才报警 (默认 3)")
    ap.add_argument("--save-alerts", action="store_true", default=True)
    ap.add_argument("--save-all", action="store_true", default=False,
                    help="每帧都保存到 alerts_review/all/")
    args = ap.parse_args()

    try:
        import cv2
    except ImportError:
        print("[!] 需要 opencv-python: pip install opencv-python")
        sys.exit(1)

    # 解析 source
    if args.source.isdigit():
        cap = cv2.VideoCapture(int(args.source))
    else:
        cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        print(f"[!] 无法打开视频源: {args.source}")
        sys.exit(1)

    det = PrintDefectDetector(
        alert_frames=args.alert_frames,
        save_alerts=args.save_alerts,
        save_heatmap_always=args.save_all,
    )
    print(f"[init] {det.stats()}")
    print(f"[monitor] source={args.source}, interval={args.interval}s, "
          f"alert_frames={args.alert_frames}")
    print("[monitor] 按 Ctrl+C 停止\n")

    try:
        while True:
            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                if args.source.isdigit():
                    print("[!] 摄像头断开, 1s 后重试...")
                    time.sleep(1)
                    cap.open(int(args.source) if args.source.isdigit() else args.source)
                    continue
                else:
                    print("[monitor] 视频读完, 退出")
                    break

            # BGR -> RGB
            frame_rgb = frame[:, :, ::-1]
            from PIL import Image
            img = Image.fromarray(frame_rgb)

            r = det.push(img)
            if r is not None:
                # 报警
                print(f"\n{'!'*40}\n{r.message}\n{'!'*40}\n")
            else:
                # 实时打印当前帧状态 (每5秒一次, 避免刷屏)
                ts = time.strftime("%H:%M:%S")
                last = det.history[-1] if det.history else None
                if last:
                    flag = "[!]" if last.anomaly else " . "
                    print(f"  {ts} {flag} P={last.probability:.3f} "
                          f"sub={last.sub_class}")

            # 限速
            dt = time.time() - t0
            sleep_t = max(0, args.interval - dt)
            time.sleep(sleep_t)
    except KeyboardInterrupt:
        print("\n[monitor] 停止")
    finally:
        cap.release()


if __name__ == "__main__":
    main()