# Bambu Monitor

拓竹（Bambu Lab）3D 打印机**多机云端监控面板**。一台电脑看住整个打印农场：实时画面、打印状态、产能统计、消息推送，外加可选的 AI 缺陷检测。

<p align="left">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="python" src="https://img.shields.io/badge/python-≥3.10-blue">
  <img alt="release" src="https://img.shields.io/badge/release-v0.6.0-orange">
  <img alt="gpu" src="https://img.shields.io/badge/GPU-不需要-lightgrey">
</p>

> [!TIP]
> **不想自己折腾环境？把整个仓库交给你的 AI 编码助手。**
> 在 Claude Code / Cursor / Codex 等 Agent 里打开本仓库，把下面这段话发给它，
> 它会读 [`docs/DEPLOY.md`](docs/DEPLOY.md) 并替你完成安装、配置、启动与自检，只在需要验证码时停下来问你：
>
> ```text
> 请按 docs/DEPLOY.md 的顺序帮我部署这个项目：
> 建虚拟环境并安装依赖，复制 printers.yaml.example 为 printers.yaml，
> 用我提供的打印机序列号填好配置，启动服务后把访问地址告诉我，
> 最后按文档里的"部署自检"逐条确认是否成功。需要我输入验证码时停下来问我。
> ```

## 它能做什么

| | |
|---|---|
| 📷 **实时监控** | 多路摄像头画面 + 状态角标，卡片可拖拽排序、布局持久化 |
| 🖨️ **打印机状态** | 进度 / 喷嘴 / 热床 / 机箱温度 / 剩余时间 / 当前层数 / 文件名 |
| ☁️ **云端一键导入** | 列出拓竹云账号下的设备，序列号、机器名与摄像头接入码自动写入，无需手抄标签 |
| 🏭 **产能看板** | 今日各机打印时长占比 + 近 30 天产能趋势 |
| 🔔 **消息通知** | 企业微信 / 飞书推送：第 3 层、50%、完成、HMS 错误、AI 异常 |
| 💬 **群聊指令** | @机器人 `截图` / `状态` / `总览` / `阈值` / `帮助` |
| 🤖 **AI 缺陷检测** | 定时抽帧推理，识别炒面、断丝、翘边等缺陷并报警（可选） |

## 30 秒跑起来

```bash
git clone https://github.com/WenliLan/bambu-monitor.git
cd bambu-monitor

python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install ".[notify]"

cp printers.yaml.example printers.yaml            # Windows: copy
bambu-monitor serve                               # → http://127.0.0.1:8000
```

首次打开面板后：设置管理员密码 → **设置 → 云端** 用拓竹云账号登录 → **设置 → 打印机 → ☁️ 从云端导入**
勾选机器（序列号、机器名、摄像头接入码自动写入；局域网 IP 可由可选的本地发现补全，或手工填一次）。
增删设备即时生效，无需重启。

需要 AI 缺陷检测就装 `pip install ".[notify,ai]"` 并把 `ai_guard.enabled` 改为 `true`；CPU 即可推理，不需要显卡。

## 文档

| 我想… | 看这里 |
|---|---|
| 从零部署（含 Agent 可执行的自检清单） | [docs/DEPLOY.md](docs/DEPLOY.md) |
| 查配置项与环境变量 | [docs/CONFIG.md](docs/CONFIG.md) |
| 接企业微信 / 飞书机器人 | [docs/NOTIFY.md](docs/NOTIFY.md) |
| 开启或调优 AI 缺陷检测 | [docs/AI.md](docs/AI.md) |
| 了解鉴权、凭据与分发边界 | [docs/SECURITY.md](docs/SECURITY.md) |
| 排查具体问题 | [docs/FAQ.md](docs/FAQ.md) |

## 机型支持

状态监控、产能统计与 HMS 报警对所有联网拓竹机型通用（云端 MQTT 统一协议）。摄像头画面按端口自动分流，无需配置：

| 机型 | 协议 | 要求 |
|---|---|---|
| A1 / A1 mini / P1P / P1S | TCP 6000 JPEG | 开箱即用 |
| X1 全系 / P2S / P2P / X2D / H2D / H2S | RTSPS 322 | 需 ffmpeg：系统 PATH、`BAMBU_FFMPEG_EXE`、或 `pip install ".[rtsp]"` 三选一 |

## 项目结构

```
bambu_monitor/          核心应用
├── api/                FastAPI 路由、WebSocket 推送、设置页
├── printer/            云端 MQTT 状态机、摄像头、通知、AI 守卫
├── static/ templates/  前端面板
├── database.py         SQLite 会话 / 产能 / 快照
└── main.py             CLI（login / serve）
bambu_ai_deploy_dinoknn/  DINOv2 + kNN 缺陷检测器（含预置特征库）
docs/                   部署与使用文档
tests/                  360 个测试用例
```

## 开发

```bash
pip install -e ".[dev]"
python -m pytest tests/                              # 全量
python -m pytest tests/test_database.py tests/test_monitor.py -x   # 核心子集
ruff check bambu_monitor tests
```

## License

[MIT](LICENSE) · DINOv2 骨干网权重为 Meta 的 **CC-BY-NC 4.0**（非商用），故不随仓库分发，详见 [docs/AI.md](docs/AI.md)。
