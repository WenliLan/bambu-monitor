# AI 打印缺陷检测（可选）

检测器位于 `bambu_ai_deploy_dinoknn/`，采用 **DINOv2 ViT-S/14 + kNN 特征库**，零训练、开箱即检，可识别炒面、断丝、翘边等缺陷。更完整的说明见 [`bambu_ai_deploy_dinoknn/README.md`](../bambu_ai_deploy_dinoknn/README.md)。

## 启用

```bash
pip install ".[notify,ai]"
```

然后把 `printers.yaml` 的 `ai_guard.enabled` 改为 `true`。CPU 推理即可，不需要显卡。

启动日志会打印真实加载信息：

```
AI 异常报警已启动: 检测器=DINOv2+kNN(k=5) 图库 normal=131/anomaly=134 阈值=0.70 间隔=600s
```

- 允许出现 `xFormers is not available` 与 `using MLP layer as FFN` 告警，这是 CPU 环境的**预期降级**
- 模型在后台线程加载，不阻塞面板启动

## 模型由两部分组成

| 组成 | 位置 | 是否随仓库分发 |
|---|---|---|
| **kNN 特征库**（本项目训练产物） | `bambu_ai_deploy_dinoknn/gallery/*.npy` + `meta.json` | ✅ 分发，clone 即可用 |
| **DINOv2 ViT-S/14 骨干网**（Meta 官方权重，约 84MB） | 本地 `dinov2_cache/` 或 `~/.cache/torch/hub` | ❌ 不入库（CC-BY-NC 4.0 非商用许可） |

骨干网获取按以下顺序自动进行：

1. **本地离线包**（零网络）：若存在 `bambu_ai_deploy_dinoknn/dinov2_cache/hub/` 则直接加载。可从 [Releases](https://github.com/WenliLan/bambu-monitor/releases) 下载 `dinov2_cache-v<版本>.zip`，解压到 `bambu_ai_deploy_dinoknn/` 即得该目录。
2. **在线自动下载**：由 `torch.hub` 从 github.com 拉实现、从 `dl.fbaipublicfiles.com` 拉权重，缓存到 `~/.cache/torch/hub`，之后即可离线复用。

两步都失败（网络受限）时会抛出带上述操作路径的 `RuntimeError`，不影响监控、通知等其他功能。

## 阈值调整

`ai_guard.threshold` 出厂默认 `0.7`；填 `0` 表示沿用特征库内置阈值。阈值越高误报越少、漏报风险增加。可随时在设置页或群里发 `阈值 0.6` 调整（持久化到 YAML）。

## 用自己的数据继续训练

把照片放入 `bambu_ai_deploy_dinoknn/data/{normal,anomaly}/`，在该目录下运行：

```bash
pip install watchdog
python watcher.py                                  # 热追加：丢图即提特征入库
python scripts/init_gallery.py --src <数据目录> --append   # 批量重建/追加
```

`data/` 已被 `.gitignore` 排除，实拍照片不会进入仓库。

## 独立使用检测器

```bash
cd bambu_ai_deploy_dinoknn
python scripts/detect_image.py test.jpg --nearest    # 单图/批量检测
python scripts/monitor.py --source 0                 # 摄像头直连监控（需 opencv-python）
```

## 部署位置探测

- **源码仓库方式运行**：自动探测，无需配置
- **pip 安装方式运行**：在仓库根目录启动 `bambu-monitor serve` 即可自动探测；在其他目录运行需指定 `ai_guard.deploy_dir` 或环境变量 `BAMBU_AI_DEPLOY_DIR`
- 目录不存在或依赖缺失时 AI 功能自动禁用，不影响其他模块
