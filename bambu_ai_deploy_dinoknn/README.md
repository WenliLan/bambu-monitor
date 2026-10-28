# bambu_ai_deploy_dinoknn

**DINOv2 (ViT-S/14) + kNN gallery 的零训练增量适配部署包**。

## 一句话总结

把新图丢进 `data/` 文件夹，**无需重启、无需重新训练**，模型立刻学会识别新的缺陷模式。

---

## 目录结构

```
bambu_ai_deploy_dinoknn/
├── README.md               ← 本文档
├── detector.py             ← 主入口：PrintDefectDetector 类
├── extractor.py            ← DINOv2 特征提取器（封装 scripts/dino_knn）
├── knn.py                  ← FeatureGallery：特征库管理 + k-NN 检索
├── bootstrap.py            ← 从数据集一次性初始化 gallery
├── watcher.py              ← 监控 data/ 文件夹，自动提特征加入 gallery
├── scripts/
│   ├── init_gallery.py     ← 一键初始化 CLI（推荐日常使用）
│   ├── detect_image.py     ← 单图/批量检测 CLI
│   └── monitor.py          ← 持续监控摄像头 / 视频流
├── data/                   ← 用户日常丢图的位置
│   ├── anomaly/            ← 异常图（自动加到 gallery 的 anomaly 类）
│   ├── normal/             ← 正常图（自动加到 gallery 的 normal 类）
│   └── uncertain/          ← 模型不确定的图（不会自动加，等用户标）
├── gallery/                ← 特征库（仓库默认包含出厂初始数据 = local_data 全量特征）
│   ├── anomaly_features.npy    [N_a, 384] float32, L2-normalized (git 跟踪)
│   ├── normal_features.npy     [N_n, 384] float32, L2-normalized (git 跟踪)
│   ├── anomaly_paths.json      对应图片路径（审计用，git 跟踪）
│   ├── normal_paths.json
│   └── meta.json               创建时间、k、阈值等（git 跟踪）
└── alerts_review/          ← 报警记录（运行时生成，已 git 忽略）
    ├── <时间戳>_P<prob>/        ← push() 报警事件包（含 frame_*.jpg + meta.json）
    └── all/                      ← save_heatmap_always=True 时每帧原图
```

---

## 快速上手 (3 步)

### ⚡ 跳过初始化直接用

本仓库已将 `baselines/local_data/` (本机适配数据, 134 anomaly + 131 normal = 265 张)
的 DINOv2 特征提取好提交在 `gallery/` 目录下，**clone 后可直接检测**：

```bash
python scripts/detect_image.py test.jpg
python scripts/monitor.py --source 0
```

只有需要扩充 gallery 或换数据集时才跳到下面的第 2 步。

### 第 1 步：安装依赖

`torch` + `torchvision` + `numpy` + `Pillow` + `faiss-cpu` 已经满足核心需求。
若要用文件夹监控自动加图，再装：

```bash
pip install watchdog
# 监控摄像头的话:
pip install opencv-python
```

### 第 2 步：初始化 gallery

从已有数据集一次性提特征，存入 `gallery/`：

```bash
# 默认扫 ../local_data/ 的 anomaly/ 和 normal/
python scripts/init_gallery.py

# 指定其他数据集
python scripts/init_gallery.py --src ../benchmark/train

# 调试用：限制每类 50 张
python scripts/init_gallery.py --limit 50
```

输出示例：
```
================================================================
>>> anomaly: h:\...\local_data\anomaly
================================================================
[bootstrap] 扫描到 134 张 (anomaly)
[bootstrap] 提特征 17.5s (7.7 张/s, shape=(134, 384))
...
[done] gallery 初始化完成!
  gallery 目录: h:\...\bambu_ai_deploy_dinoknn\gallery
```

**注意**：CPU 上 DINOv2 ViT-S/14 提特征约 7-10 张/秒。几百张图约 1 分钟。

### 第 3 步：检测 / 持续监控

#### 一次性检测（单图或多图）

```bash
python scripts/detect_image.py test.jpg                    # 单图
python scripts/detect_image.py test1.jpg test2.jpg ...     # 多图
python scripts/detect_image.py frames/*.jpg --nearest     # 通配 + 显示最近邻
python scripts/detect_image.py test.jpg --save-alerts      # 异常时保存到 alerts_review/
python scripts/detect_image.py frames/*.jpg --save-all     # 每帧都保存到 alerts_review/all/
```

输出示例：
```
[!] 异常  P=0.800  sub=anomaly  (63ms)  12d31b345f5c9329.jpg
    #1 anomaly  sim=0.943  12d31b345f5c9329.jpg    ← 这就是为啥判异常
    #2 anomaly  sim=0.881  137f807e76cbe59a.jpg
    #3 anomaly  sim=0.872  32215145087eb047.jpg
```

#### 摄像头持续监控

```bash
# 默认本机摄像头 (0)
python scripts/monitor.py

# 外接 USB 摄像头
python scripts/monitor.py --source 1

# RTSP / RTMP 流
python scripts/monitor.py --source rtsp://admin:pwd@192.168.1.10/stream

# 视频文件
python scripts/monitor.py --source video.mp4

# 调参：连续 5 帧异常才报警
python scripts/monitor.py --alert-frames 5 --interval 2.0
```

输出示例：
```
[init] {'n_anomaly': 134, 'n_normal': 80, 'total': 214, 'k': 5, ...}
[monitor] source=0, interval=1.0s, alert_frames=3
  14:32:01  . P=0.123 sub=normal
  14:32:02  . P=0.087 sub=normal
  14:32:03 [!] P=0.823 sub=anomaly
  14:32:04 [!] P=0.901 sub=anomaly
  14:32:05 [!] P=0.876 sub=anomaly

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
[!] 连续 3 帧检测到异常! 最近概率=0.88, 建议停机检查。
 图片已保存: 20260820_143205_P0.88
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

---

## 核心特性：零训练增量适配

### 场景：发现模型认不出的新缺陷

```bash
# 1. 把新图丢进 data/anomaly/ (随便用户怎么丢: 拖拽、scp、save_as)
cp ~/Downloads/spaghetti_case_05.jpg data/anomaly/

# 2. 后台 watcher 自动:
#    - 检测到新文件 → 用 DINOv2 提特征 → 加入 gallery 的 anomaly 类
#    - gallery 自动持久化到 .npy
#    - 下一次 detect() 立即用上新特征
```

无需：
- ❌ 重启任何进程
- ❌ 重新训练模型
- ❌ 改配置文件
- ❌ 重新初始化

### 启动后台 watcher

```bash
python watcher.py
```

或者在代码里：

```python
from detector import PrintDefectDetector
from watcher import start_watch

det = PrintDefectDetector(save_alerts=True)
observer = start_watch(det, data_dir="data", blocking=True)
# blocking=True 会一直跑直到 Ctrl+C
```

### 用户操作清单

| 用户想做什么 | 操作 |
|-------------|------|
| 加入新异常样本 | 把图丢进 `data/anomaly/` |
| 加入新正常样本 | 把图丢进 `data/normal/` |
| 修正分类错误 | 在 `data/anomaly/` ↔ `data/normal/` 之间移动文件 |
| 删除某样本 | 用 `det.gallery.remove(path)` API；或手动编辑 `gallery/*.json` |
| 看哪些样本最近被加入 | 读 `gallery/meta.json` 的 n_anomaly/n_normal |
| 完全重置 gallery | `rm gallery/*.npy gallery/*.json && python scripts/init_gallery.py` |

---

## Python API 完整示例

```python
import sys
sys.path.insert(0, r"h:\QoderWorkspace\bambu_AI\baselines\bambu_ai_deploy_dinoknn")

from detector import PrintDefectDetector

# === 1. 初始化（自动加载 gallery + 预热 DINOv2） ===
det = PrintDefectDetector(
    k=5,                         # k-NN 的 k (默认 5)
    threshold=0.7,               # anomaly 占比阈值 (默认 0.7)
    alert_frames=3,              # 连续 N 帧异常才报警 (默认 3)
    save_alerts=True,            # 是否保存报警图片
    save_heatmap_always=False,   # 是否每帧都存 (调试用, 默认 False)
)
print(det.stats())
# {'n_anomaly': 134, 'n_normal': 80, 'k': 5, ...}

# === 2. 单帧检测 ===
result = det.detect("test.jpg", return_nearest=True)
print(result.to_dict())
# {'anomaly': True, 'probability': 0.8, 'threshold': 0.7, 'sub_class': 'anomaly',
#  'ms': 63.2, 'n_nearest': 5}

for nn in result.nearest[:3]:
    print(f"  #{nn['rank']} {nn['label']:8s} sim={nn['similarity']:.3f} {nn['path']}")

# === 3. 监控模式（连续 N 帧才报警）===
alert = det.push(frame)
if alert:
    print(f"[!] 报警: {alert.message}")
    notify_user(alert)   # 接你的告警通道

# === 4. 手动加图到 gallery ===
det.add_to_gallery("new_anomaly.jpg", label="anomaly")
det.add_to_gallery("new_normal.jpg", label="normal")

# === 5. 看最近帧的历史 ===
for r in det.history[-10:]:
    print(f"  {r.ms:.0f}ms  P={r.probability:.2f}  sub={r.sub_class}")

# === 6. 重置连续帧计数 ===
det.reset()
```

---

## 决策规则（细节）

对每张待检测图片：

1. **提特征**：DINOv2 ViT-S/14 输出 384 维 CLS token，L2 归一化
2. **k-NN 检索**：在 gallery（已合并 anomaly + normal）中找 cosine 距离最近的 k=5 个样本
3. **统计**：这 5 个邻居里有多少是 anomaly，记为 `a`，则 `P(anomaly) = a / k`
4. **决策**：`P(anomaly) >= threshold` (默认 0.7) → 判为异常

**优点**：
- 完全可解释：返回 top-k 最近邻，能直接看到"为什么判异常"
- 不会遗忘：旧样本永远在 gallery 里
- 即时生效：丢图立刻能用

**权衡**：
- 冷启动：必须先初始化 gallery（不能空集推理）
- 容量：几万张内 numpy + faiss 够用；百万级要换 HNSW/IVF
- 用户标注错会污染 gallery（这是 kNN 的固有特性）

---

## 进阶用法

### 自定义阈值

预置 gallery 的默认决策阈值为 **0.7（k=5 近邻相似度加权投票）**：P(anomaly) =
Σsim(anomaly 近邻) / Σsim(全部近邻)，比等权投票对“照片数量少的异常类”更公平。
基于随包 gallery 实测：0.7 对已收录异常类召回约 69%，比 0.5 的约 83% 更保守、误报更少；
0.8 几乎零误报但召回约 66%。配合连续 3 帧才报警，可进一步压低实际误报。
注意：照片极少的异常类（如仅 1 张参考图的 clog）分箱在阈值边缘，多补几张同类型照片即可稳定触发。
担心漏报就调低到 0.5，攒照片过程中仍偏吵就调到 0.8：

```bash
# 严格模式: 80% 才判异常 (减少误报)
python scripts/detect_image.py test.jpg --threshold 0.8

# 宽松模式: 40% 就判异常 (减少漏报)
python scripts/detect_image.py test.jpg --threshold 0.4
```

或者直接编辑 `gallery/meta.json` 的 `decision_threshold`，下次启动自动加载。

### 自定义 k 值

```bash
python scripts/detect_image.py test.jpg --k 10
```

经验值：
- `k=5`：默认，平衡
- `k=1`：最近邻单点判，最快但噪声大
- `k=10~20`：更稳健但响应慢
- `k=全部`：等价于"哪个类别的样本多就判哪类"（异常用例）

### 自定义 data 子目录映射

```python
# data/good/ → normal, data/bad/ → anomaly, data/maybe/ → 不自动加
from watcher import start_watch
start_watch(det, data_dir="data",
            label_dirs={"good": "normal", "bad": "anomaly", "maybe": None})
```

### 把 uncertain/ 的样本标好后再入库

```python
import os
from pathlib import Path

# 把 data/uncertain/*.jpg → data/anomaly/ 或 data/normal/
# watcher 不会自动加 uncertain/, 但用户搬动后会触发加入
```

---

## 为什么是 kNN 方案

发布包只内置本检测器(当前生产部署方案)。对比旧式分类器方案：
- 零训练：不需要维护训练管线，丢图即生效
- 可持续优化：照片越攒越多，阈值可在 Web 设置页实时调节
- 可解释：每次报警都附最近邻参考图（报警 meta.json 里的 nearest_top3）

---

## 常见问题

### Q: gallery 为空时报什么错？
A: 不报错。`detect()` 返回 `sub_class="unknown"`, `probability=0.5`，用户可以选择忽略或报警。日志会显示 `gallery 为空`。

### Q: 加图后多久生效？
A: 即时。watcher 监听到 `on_created` 事件后会立刻 `gallery.add()` + `save()`。下一次 `detect()` 就是用上新 gallery。

### Q: 我有一张图想加入 gallery 但不想自动监控整个文件夹？
A: 用 `det.add_to_gallery(path, label="anomaly")` 手动加。

### Q: 怎么删除某条样本？
A: 用 `det.gallery.remove(path)`。会从 .npy 和 .json 同步删除。

### Q: 我的数据集很大，几万张以上，会不会慢？
A: 几万张内 numpy + faiss 够用，提特征 ms 级、检索 ms 级。如果上 10 万张，考虑：
- 降采样到 1-2 万代表样本
- 或换 HNSW/IVF 索引（需要新代码）

### Q: 用户标错了怎么办？
A: 这是 kNN 的固有弱点。建议：
- bootstrap 时人工筛选干净的数据集
- 监控时看 `result.nearest`，如果发现错的，移到正确类别
- 严重污染时直接 `rm gallery/*.npy && python scripts/init_gallery.py` 重来

### Q: DINOv2 模型权重在哪？
A: `baselines/dinov2_cache/hub/checkpoints/dinov2_vits14_pretrain.pth` (~80MB)。本包自动走本地缓存，无需联网。

### Q: 推理速度多少？
A: CPU 上 DINOv2 ViT-S/14 提特征约 50-80ms/张。k-NN 检索几 ms。整体 ~80ms/帧。

---

## 文件清单（实现细节）

| 文件 | 行数 | 职责 |
|------|-----|------|
| `extractor.py` | ~75 | 封装 `scripts/dino_knn.extract`，支持 PIL.Image 直接入参 |
| `knn.py` | ~290 | `FeatureGallery` 类：增删改 + k-NN 检索 + 持久化 |
| `detector.py` | ~280 | `PrintDefectDetector` 主类：与 ONNX 版同 API 口径 |
| `bootstrap.py` | ~105 | CLI: 从数据集初始化 gallery |
| `watcher.py` | ~145 | watchdog 文件监控 + 自动提特征 |
| `scripts/init_gallery.py` | ~55 | 一键初始化 |
| `scripts/detect_image.py` | ~75 | 单图/批量检测 |
| `scripts/monitor.py` | ~105 | 持续摄像头监控 |

总计 ~1130 行 Python。