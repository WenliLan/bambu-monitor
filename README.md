# Bumbo Stats

拓竹(Bambu Lab)3D 打印机**多机云端监控面板**。一台电脑监控整个打印农场:实时摄像头画面、打印状态、温度/进度/层数、产能统计,支持企业微信/飞书消息推送与 AI 打印缺陷检测。

```
📷 实时监控     多路摄像头画面 + 状态角标(可拖拽排序,布局持久化)
📊 打印机状态   进度/喷嘴/热床/机箱温度/剩余时间/层数/文件名
🏭 产能饱和度   今日各机打印时长占比
📈 趋势图       近 30 天产能曲线
🔔 消息通知     企业微信/飞书:开始打印、50% 进度、完成、HMS 错误、AI 异常
🤖 AI 检测      定时抽帧推理,识别炒面/断丝/翘边等缺陷并报警(可选)
```

## 环境要求

- Python ≥ 3.10(Windows / Linux / macOS 均可,摄像头与局域网访问按平台通用)
- 拓竹云账号(用于通过云端 API/MQTT 获取打印机状态)
- 局域网内可访问的拓竹摄像头(可选,不配也能看状态数据)

## 快速开始

```bash
# 1. 安装(建议使用虚拟环境)
python -m venv venv
# Windows: venv\Scripts\activate    Linux/macOS: source venv/bin/activate

pip install .                    # 核心:监控面板+云端状态(轻量)
pip install ".[notify]"          # 推荐:含企微/飞书通知与监控截图;之后补装用同一条命令即可

# 2. 登录拓竹云,获取并保存 Token(凭据存于 ~/.bumbo_token)
bumbo-stats login

# 3. 生成配置文件,按注释填写打印机序列号、IP、摄像头接入码等
copy printers.yaml.example printers.yaml      # Windows
cp printers.yaml.example printers.yaml        # Linux/macOS

# 4. 启动服务(默认监听 0.0.0.0:8000)
bumbo-stats serve
```

浏览器打开 `http://<主机IP>:8000`,首次进入设置管理员密码即可。

> 通知依赖(企微/飞书 SDK、Playwright)已拆为可选分组:只装核心时通知自动禁用、
> 面板正常运行;截图功能需要额外执行 `playwright install msedge`(或 chromium)。

## 配置说明(printers.yaml)

| 段落 | 作用 | 关键字段 |
|---|---|---|
| `cloud` | 拓竹云账号 | `token_file`(默认 `~/.bumbo_token`)、`mqtt_broker`(`cn`/`us`) |
| `printers` | 打印机列表 | `name`、`serial`(18 位序列号)、`ip`(局域网 IP,摄像头用)、`access_code`(打印机管理页获取) |
| `settings` | 服务 | `host`、`port`、`db_path`、`camera_interval` |
| `wecom` | 企业微信机器人 | `enabled`、`bot_id`、`secret`、`chatid` 及各事件开关 |
| `feishu` | 飞书机器人 | `app_id`、`app_secret`、`chatid`、加急(`urgent_*`)配置 |
| `ai_guard` | AI 缺陷检测 | `enabled`、`interval`、`threshold`、`deploy_dir`(见下) |
| `ui` | 面板登录 | `admin_password_hash`(通过网页首次设置自动生成) |

面板的 **⚙️ 设置页** 也可以完成大部分配置的在线编辑与保存,改完自动热加载。

## 可选功能

### 消息通知(企业微信 / 飞书)

- 企业微信:创建智能机器人,填 `bot_id` + `secret`;群聊 `chatid` 留空可自动发现
- 飞书:创建自建应用并开通 `im.message` 权限,填 `app_id` + `app_secret`;
  支持"应用内加急"(弹窗+响铃),适合异常强提醒
- 安装方式:`pip install ".[notify]"`;通知内容含监控截图,还需 `playwright install msedge`

### AI 缺陷检测

检测器位于 `bambu_ai_deploy_dinoknn/`(kNN 特征库方案,依赖 torch)。
**发布包与源码仓库(v0.5.14 起)均已内置模型权重与预训练特征库**,开箱即可检测。

- 安装:`pip install ".[ai]"`(含 torch/onnxruntime,体积较大)
- 开箱即检:特征库在 `bambu_ai_deploy_dinoknn/gallery/`,直接可用(默认阈值 0.5 加权投票,可在设置页调节)
- **持续优化**:把你自己拍的照片放入 `bambu_ai_deploy_dinoknn/data/{normal,anomaly}/`,
  运行 `scripts/monitor.py`(watchdog)会自动热追加到特征库,越用越准
- 历史照片请保在本地——`*_paths.json` 中的路径仅为记账,不影响推理

- **源码仓库方式运行**:自动探测,无需配置
- **pip 安装方式运行**:在发布包根目录启动 `bumbo-stats serve` 即可自动探测到检测器;
  若在其他目录运行,需指定 `ai_guard.deploy_dir` 或环境变量 `BUMBO_AI_DEPLOY_DIR`。
  目录不存在时 AI 功能自动禁用,不影响其他模块

## 安全注意事项

- `printers.yaml` 含打印机 `access_code`、机器人 `secret` 等**明文凭据**,请勿提交到公开仓库(默认已被 `.gitignore` 排除),并妥善保管
- **多用户/容器环境推荐用环境变量替代明文落盘**(优先级高于 YAML):
  `BUMBO_TOKEN_FILE`、`BUMBO_WECOM_BOT_ID`、`BUMBO_WECOM_SECRET`、
  `BUMBO_FEISHU_APP_ID`、`BUMBO_FEISHU_APP_SECRET`、`BUMBO_DASHBOARD_URL`
- 服务默认监听 `0.0.0.0`,同局域网设备均可访问面板;公网服务器请改用 `--host 127.0.0.1` 并配合反向代理
- 面板有登录保护(SHA-256 口令哈希 + HttpOnly Cookie),但 Token/密钥未做本地加密存储,多用户环境请限制文件读取权限
- **分发时请用 git 导出**(`git clone` 或 `git archive`),不要直接打包工作目录:`data/`(数据库、报警实拍照片)、`printers.yaml`(你的全部凭据)、日志均只存在于本地工作区

## 开发与测试

```bash
pip install -e ".[dev]"
python -m pytest tests/            # 全量测试
python -m pytest tests/test_database.py tests/test_monitor.py -x   # 核心子集
```

代码入口:`bumbo_stats/main.py`(CLI)、`bumbo_stats/api/`(FastAPI 路由/WebSocket)、`bumbo_stats/printer/`(MQTT 状态机、通知、AI 守卫)。

## 常见问题

| 现象 | 处理 |
|---|---|
| 启动报"配置文件不存在" | 未复制 `printers.yaml.example` 为 `printers.yaml` |
| 摄像头黑屏/离线 | 检查打印机与主机同网段、`ip`/`access_code` 是否正确、打印机固件是否开启局域网 |
| 状态一直 OFFLINE | 重新 `bumbo-stats login` 刷新 Token;确认 `mqtt_broker` 区域与账号一致 |
| 收不到通知 | 设置页确认对应通道 `enabled`;企微检查机器人在群内;飞书检查应用权限已发布 |
| AI 检测不生效 | pip 部署需配置 `ai_guard.deploy_dir`;确认 torch 已安装、图库已初始化 |

## License

[MIT](LICENSE)
