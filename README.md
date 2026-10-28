# Bumbo Stats

拓竹(Bambu Lab)3D 打印机**多机云端监控面板**。一台电脑监控整个打印农场：实时摄像头画面、打印状态、温度/进度/层数、产能统计，支持企业微信/飞书消息推送与 AI 打印缺陷检测。

```
📷 实时监控     多路摄像头画面 + 状态角标(可拖拽排序,布局持久化)
📊 打印机状态   进度/喷嘴/热床/机箱温度/剩余时间/层数/文件名
🏭 产能饱和度   今日各机打印时长占比
📈 趋势图       近 30 天产能曲线
🔔 消息通知     企业微信/飞书:第 3 层、50%、完成、HMS 错误、AI 异常
🤖 AI 检测      定时抽帧推理,识别炒面/断丝/翘边等缺陷并报警(可选)
💬 群聊指令     @机器人 截图 / 状态 / 总览 / 阈值 / 帮助
```

## 环境要求

- Python ≥ 3.10（Windows / Linux / macOS 均可）
- **标准 CPython + pip 即可，无需 Anaconda / conda**；AI 检测用 CPU 版 `torch`，**不需要显卡**
- 拓竹云账号：`mqtt_broker: cn`（国内）用**手机号 + 短信验证码**；`mqtt_broker: us`（海外）用**邮箱 + 邮件验证码**
- 局域网内可访问的拓竹打印机（摄像头为可选项，不配也能看状态数据）

Windows 额外注意：

- 中文区域建议先设 `$env:PYTHONUTF8="1"`，否则个别源码包在 cp936 下构建会报 `UnicodeDecodeError`（v0.5.18 已移除会触发该问题的第三方传递依赖，但保留此设置更稳妥）
- 安装 `[ai]`（torch）需要 **VC++ 2015-2022 x64 运行库 ≥ 14.36**；版本过旧会在 `import torch` 时报 `OSError [WinError 1114] ... c10.dll`
- 大陆网络可选用清华镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`（第三方索引，信任与否请自行判断）
- **安装/重装前先退出所有 `bumbo-stats serve` / `login` 进程**，否则 Windows 会因 exe 被占用留下半成品包

## 快速开始

```bash
# 1. 建虚拟环境并安装（推荐 [notify]：核心 + 企微/飞书通知，轻量）
python -m venv venv
# Windows: venv\Scripts\activate    Linux/macOS: source venv/bin/activate
pip install ".[notify]"

#   需要 AI 缺陷检测时再显式加 [ai]（含 torch，体积大，CPU 版即可）：
#   pip install ".[notify,ai]"      # 装完把 printers.yaml 的 ai_guard.enabled 改为 true
#   X1/P2S/P2P/X2D/H2D 等 RTSPS 摄像头机型再加 [rtsp]

# 2. 生成配置文件，按注释填写打印机序列号等
copy printers.yaml.example printers.yaml      # Windows
cp printers.yaml.example printers.yaml        # Linux/macOS

# 3. 启动服务（默认只监听 127.0.0.1:8000）
bumbo-stats serve
```

浏览器打开 `http://127.0.0.1:8000`：

1. 首次进入按提示**设置面板管理员密码**；
2. 进入 **⚙️ 设置 → 云端**，用拓竹云账号完成登录（国内手机号收短信、海外邮箱收邮件验证码）；
3. 进入 **设置 → 打印机** 添加/删除设备（即时生效，无需重启）。

> **v0.5.18 起无 Token 也能启动**：服务会正常监听，面板显示"未连接拓竹云"，在网页里登录即可。
> CLI 登录仍然可用（`bumbo-stats login`），但不再是启动的前置条件。
>
> 需要局域网其他设备访问面板时，把 `printers.yaml` 的 `settings.host` 改为 `0.0.0.0`
>（或启动时加 `--host 0.0.0.0`）。**这会把实时画面和数据暴露给同网段设备**，务必先设好管理员密码并确认网络可信。
>
> 通知截图依赖 Playwright：`pip install ".[notify]"` 之后执行 `playwright install msedge`（或 chromium）。
> 未安装时通知自动降级为纯文本，面板功能不受影响。

## 🤖 AI Agent 部署 Runbook

> 面向接收方的编码 Agent（Claude / Cursor 等）：按下列顺序照做即可跑通。
> 标 ⛔ 的步骤必须人类参与（接收验证码），Agent 不能代做。

### 0. 先向人类问清这些输入（缺则无法部署）

- 拓竹云账号 **+ 密码** ⛔
  - `mqtt_broker: cn`（国内账号）→ **手机号 + 短信验证码**
  - `mqtt_broker: us`（海外账号）→ **邮箱 + 邮件验证码**
- 每台打印机的 **序列号 `serial`**：照抄机身/包装标签原值（长度随机型为 15-18 位，以云端设备列表返回的 `dev_id` 为准）
- 看摄像头画面还需打印机局域网 `ip` + **接入码 `access_code`**（打印机 Web 管理页 → 安全设置 → 接入码）
- 可选：是否要消息通知（企业微信/飞书凭据）、是否要 AI 缺陷检测

### 1. 安装（全程在发布包根目录，即含 `pyproject.toml` 的目录）

**Windows PowerShell（中文环境建议先设编码）：**

```powershell
cd <发布包根目录>
$env:PYTHONUTF8 = "1"
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install ".[notify]"                 # 要 AI 缺陷检测改 ".[notify,ai]"
copy printers.yaml.example printers.yaml
notepad printers.yaml                   # 填 serial / name / ip / access_code 后保存
```

**Linux / macOS bash：**

```bash
cd <发布包根目录>
python3 -m venv venv && source venv/bin/activate
pip install ".[notify]"                 # 要 AI 缺陷检测改 ".[notify,ai]"
cp printers.yaml.example printers.yaml
$EDITOR printers.yaml
```

说明：

- 推荐基准是 `[notify]`；AI 是显式可选项，装完必须把 `ai_guard.enabled` 改为 `true` 才会启用
- `[ai]` 需要 VC++ 2015-2022 x64 ≥ 14.36（Windows）
- X1/P2S/P2P/X2D/H2D/H2S 等 RTSPS 摄像头机型需 ffmpeg：系统 PATH / `BUMBO_FFMPEG_EXE` 指向 ffmpeg.exe / `pip install ".[rtsp]"` 三选一

### 2. 启动与登录

```bash
bumbo-stats serve          # 默认 http://127.0.0.1:8000
```

启动后：

1. 浏览器打开面板，首次设置管理员密码；
2. 在 **设置 → 云端** 填账号密码，⛔ 由人类回填短信/邮件验证码，登录成功后自动连接云端并热加载；
3. 在 **设置 → 打印机** 增删设备（即时生效）。

也可以继续用 CLI 登录（同样 ⛔ 需要人类回填验证码）：

```bash
bumbo-stats login                     # cn 提示手机号，us 提示邮箱
# 成功标志：~/.bumbo_token 生成（JSON，含 region 与 token）
```

### 3. 部署自检（逐条验证，全过即成功）

1. `bumbo-stats --version` 有输出
2. 启动横幅出现 `Bumbo Stats v<版本>`、`监控 N 台打印机`、面板地址，以及**配置文件与日志文件的绝对路径**
3. `curl http://127.0.0.1:8000/healthz` 返回 200；未登录云端时 `status` 为 `degraded`，登录后为 `ok`（该端点公开，便于外部探针）
4. `GET /login` 返回 200；`GET /api/status` 未登录时应为 **401**（鉴权生效，属正常）
5. **实时链路（唯一硬证据）**：日志每 30 秒一条
   `状态同步 [<serial>]: 层 x/y 进度 z% 状态 RUNNING`，且 x、z 随时间递增
6. 正常情况下启动日志应出现 `MQTT 已连接 ... 订阅 device/<serial>/report`，且**不应**出现
   `MQTT 连接失败` 或 `定时 pushall 失败 ... Not connected to broker`；
   若出现，先看 `/healthz` 的 `cloud.authenticated` / `printers[].mqtt_connected`，再排查网络、账号区域与 Token
7. 画面链路（需先登录面板取得 Cookie）：

   ```bash
   curl -c cookies.txt -X POST http://127.0.0.1:8000/api/auth/login \
        -H "Content-Type: application/json" -d "{\"password\":\"<面板密码>\"}"
   curl -b cookies.txt -o frame.jpg http://127.0.0.1:8000/api/printers/<serial>/camera
   # 期望 200 + image/jpeg
   ```

8. 仅当安装了 `[ai]` 且 `ai_guard.enabled: true` 时检查：
   日志出现 `AI 异常报警已启动: 检测器=DINOv2+kNN(k=...) 图库 normal=.../anomaly=... 阈值=...`
   - 允许伴随 `UserWarning: xFormers is not available` 与 `using MLP layer as FFN`，这是 CPU 环境的**预期降级**，不是错误
   - 模型加载已改为后台进行，不再阻塞启动；首次推理成功后写 INFO 日志，`/healthz` 的 `ai.last_check_at` 也会更新

### 4. 必须知道的坑

- ⛔ **网页云端登录 / CLI `login` 都需要人类回填验证码**，Agent 不能自动完成；Token 落在 `~/.bumbo_token`
- **不再需要"先 login 再 serve"**：无 Token 会降级启动，先在网页登录即可
- `pip install .` 是非 editable 安装：修改项目根源码对已安装 CLI 无效；开发调试请 `pip install -e ".[dev]"`
- **改 `printers.yaml` 前先停 `serve`**：面板保存设置时只回写本次修改的段并保留 `.bak`，但运行期手工编辑仍可能被覆盖；增删打印机优先用网页 **设置 → 打印机**
- 日志路径由 `settings.log_dir` 决定（默认 `./logs`，相对启动目录；可用 `BUMBO_LOG_DIR` 覆盖），启动横幅会打印绝对路径，**不要再按旧文档去 site-packages 里找**
- 发布包不含 `tests/`，不要试图跑 pytest
- AI 检测器自动探测仅在发布包根目录启动 `serve` 时生效；在其他目录启动需配 `ai_guard.deploy_dir` 或环境变量 `BUMBO_AI_DEPLOY_DIR=<根>/bambu_ai_deploy_dinoknn`；目录无效时 AI 自动禁用，不影响其他功能
- 摄像头按端口自动分流：A1/P1 系（TCP 6000）开箱即用；RTSPS 机型需 ffmpeg（日志出现 `摄像头协议探测: ... -> RTSPS(322, ffmpeg)` 即生效）
- 通知截图依赖 Playwright，需额外 `playwright install msedge`（或 chromium）
- `printers.yaml` 含明文 `access_code`/`secret`，**勿提交到公开仓库**
- 默认监听 `127.0.0.1:8000`；要让局域网访问才改 `0.0.0.0`，并确认已设管理员密码
- 安装/重装前先退出所有 `serve`/`login` 进程；重启前确认旧进程已退出，否则端口被占会报错退出
- Windows 计划任务启动脚本若用批处理，必须保存为 **CRLF 行尾**；纯 LF 会被 cmd 逐行误解析（`LastTaskResult=255` 且端口不监听）

## 配置说明（printers.yaml）

| 段落 | 作用 | 关键字段 |
|---|---|---|
| `cloud` | 拓竹云账号 | `token_file`(默认 `~/.bumbo_token`)、`mqtt_broker`(`cn`/`us`，须与账号区域一致) |
| `printers` | 打印机列表 | `name`、`serial`(15-18 位标签原值)、`ip`(摄像头用)、`access_code`(打印机管理页获取)、`location` |
| `settings` | 服务 | `host`(默认 `127.0.0.1`)、`port`、`db_path`、`camera_interval`、`log_dir`(默认 `./logs`)、`dashboard_url` |
| `wecom` | 企业微信机器人 | `enabled`、`bot_id`、`secret`、`chatid`(留空自动发现)及各事件开关 |
| `feishu` | 飞书机器人 | `app_id`、`app_secret`、`chatid`、事件开关、加急(`urgent_*`) |
| `ai_guard` | AI 缺陷检测 | `enabled`、`interval`、`threshold`、`alert_frames`、`cooldown`、`alert_dir`、`deploy_dir` |
| `ui` | 面板登录 | `admin_password_hash`(首次设密码时自动生成，无需手填) |

- **相对路径**（`db_path` / `log_dir` / `alert_dir` / `deploy_dir`）均按**启动时所在目录**解析，建议固定在发布包根目录启动
- 面板 **⚙️ 设置页** 可在线完成大部分配置；保存只回写本次修改的段，并生成 `printers.yaml.bak`
- 增删打印机推荐用 **设置 → 打印机**（热加载，无需重启）；手改 YAML 后需重启 `serve`
- `dashboard_url` 配置后，通知与群指令的回复末尾会附"打开监控面板"链接

## 机型与摄像头支持

- **状态监控 / 产能 / HMS 报警**：所有联网拓竹机型开箱即用（云端 MQTT 统一协议，无机型差异）
- **摄像头画面 / AI 检测**：按端口自动分流两种协议，无需配置：

  | 机型 | 协议 | 要求 |
  |---|---|---|
  | A1 / A1 mini / P1P / P1S | TCP 6000 JPEG | 开箱即用 |
  | X1 全系 / P2S / P2P / X2D / H2D / H2S 等 | RTSPS 322 | 需 ffmpeg（三选一） |

- RTSPS 机型的 ffmpeg 任选一种：系统安装并加入 PATH；设 `BUMBO_FFMPEG_EXE` 指向 ffmpeg.exe；或 `pip install ".[rtsp]"`（自带静态 ffmpeg）。未装时仅该类机型无画面，其他功能不受影响

## 消息通知（企业微信 / 飞书）

自动推送事件（可在设置页逐项开关）：

- 打印到达第 3 层
- 进度达到 50%
- 打印完成（含截图）
- HMS 错误告警
- AI 检测到打印异常

### 企业微信

1. 在企业微信中创建智能机器人，取得 `bot_id` + `secret`；
2. 把机器人拉进目标群，`chatid` 留空可自动发现；
3. 面板 设置 → 企业微信 填写并启用，点"发送测试"验证。

企微后台需把服务器出口 IP 加入**可信 IP 白名单**，否则会认证失败。

### 飞书

1. 在飞书开放平台创建**自建应用**，开通权限：
   - `im:chat`（读取群列表）
   - `im:message`（发送消息）
   - `im:message.group_at_msg`（接收群内 @机器人 事件）
2. 事件订阅方式选择**使用长连接接收事件**，并发布应用版本；
3. 把应用/机器人加入目标群；
4. 面板 设置 → 飞书 填 `app_id` / `app_secret`，点 **🔍 获取群聊** 自动列出候选群并回填 `chatid`；
5. 保存并启用后即可"发送测试"（无需重启，v0.5.18 起运行时启用即时生效）。

`urgent_*` 为"应用内加急"（强制弹窗 + 响铃）：填被加急用户的 `open_id`、触发事件和每小时上限即可。留空 `urgent_user_id` 即不启用。

### 群聊指令

在群里 **@机器人** 发送含以下关键词的消息：

| 指令 | 关键词 | 作用 | 冷却 |
|---|---|---|---|
| 截图 | `截图` / `监控` | 发送所有打印机实时摄像头画面 | 10 秒 |
| 状态 | `状态` | 文字回复各机状态（层数/进度/剩余时间/温度） | 5 秒 |
| 总览 | `总览` | 生成监控面板完整长截图（状态 + 截图 + 产能图表） | 10 秒 |
| 阈值 | `阈值` / `threshold` + 数字 | 查询/修改 AI 检测阈值（0~1），修改持久化到 YAML | 3 秒 |
| 帮助 | `帮助` / `help` | 显示指令说明 | 3 秒 |

- 未识别的指令、当前平台不支持的指令、以及冷却期内的重复指令都会得到明确回复，不再静默
- `阈值 0.6` 修改阈值，只发 `阈值` 则查询当前值

## AI 缺陷检测（可选）

检测器位于 `bambu_ai_deploy_dinoknn/`，采用 **DINOv2 ViT-S/14 + kNN 特征库**，零训练、开箱即检。
**发布包与源码仓库已内置模型权重与预训练特征库**（`gallery/`，默认 `k=5`、阈值 0.7）。

- 安装：`pip install ".[notify,ai]"`，然后把 `printers.yaml` 的 `ai_guard.enabled` 改为 `true`
- 启动日志会打印真实信息：
  `AI 异常报警已启动: 检测器=DINOv2+kNN(k=5) 图库 normal=131/anomaly=134 阈值=0.70 间隔=600s ...`
- `printers.yaml.example` 出厂配置为 `threshold: 0.7`；填 `0` 表示沿用特征库出厂阈值（同样为 0.7）。阈值越高误报越少，但漏报风险增加，可在设置页或群指令 `阈值 0.6` 调整
- CPU 推理即可；首次启动会加载 ViT-S/14 权重（后台线程加载，不阻塞面板），并允许出现 xFormers 缺失告警

**持续优化（可选）**：把你自己拍的照片放入 `bambu_ai_deploy_dinoknn/data/{normal,anomaly}/`，
在该目录下运行 `python watcher.py`（需 `pip install watchdog`）即可热追加到特征库，越用越准。
也可用 `python scripts/init_gallery.py --src <数据目录> --append` 批量重建/追加。
上述脚本随发布包提供，路径以 `bambu_ai_deploy_dinoknn/` 为工作目录。

单图/批量检测或摄像头直连监控：

```bash
cd bambu_ai_deploy_dinoknn
python scripts/detect_image.py test.jpg --nearest
python scripts/monitor.py --source 0        # 需 pip install opencv-python
```

- **源码仓库方式运行**：自动探测，无需配置
- **pip 安装方式运行**：在发布包根目录启动 `bumbo-stats serve` 即可自动探测；
  若在其他目录运行，需指定 `ai_guard.deploy_dir` 或 `BUMBO_AI_DEPLOY_DIR`
- 目录不存在或依赖缺失时 AI 功能自动禁用，不影响其他模块

## 安全注意事项

- 服务**默认只监听 `127.0.0.1`**。改为 `0.0.0.0` 前请先设置管理员密码，并确认同网段设备可信
- 所有 `/api/*` **数据接口**都要求登录：未携带会话 Cookie 时返回 **401**；`/healthz`、`/login` 与认证接口（`/api/auth/*`）是仅有的公开端点
- 登录 Cookie 保存**进程内随机 session token**（`secrets.token_urlsafe`，HttpOnly，7 天过期），不再是口令哈希；服务重启后需重新登录
- `printers.yaml` 含打印机 `access_code`、机器人 `secret` 等**明文凭据**，请勿提交到公开仓库（默认已被 `.gitignore` 排除）
- **多用户/容器环境推荐用环境变量替代明文落盘**（优先级高于 YAML）：
  `BUMBO_TOKEN_FILE`、`BUMBO_WECOM_BOT_ID`、`BUMBO_WECOM_SECRET`、
  `BUMBO_FEISHU_APP_ID`、`BUMBO_FEISHU_APP_SECRET`、`BUMBO_DASHBOARD_URL`、`BUMBO_LOG_DIR`
- 公网部署请保持 `127.0.0.1` 监听并配合反向代理（HTTPS），不要把面板直接暴露到公网
- **分发时请用 git 导出**（`git clone` / `git archive`），不要直接打包工作目录：`data/`（数据库、报警实拍照片）、`printers.yaml`（全部凭据）、`logs/` 均只应留在本地
- Token/密钥未做本地加密存储，多用户环境请限制文件读取权限

## 常见问题

| 现象 | 处理 |
|---|---|
| 启动报"配置文件不存在" | 未复制 `printers.yaml.example` 为 `printers.yaml` |
| 面板显示"未连接拓竹云" | 进 设置 → 云端 登录；或 CLI `bumbo-stats login`。账号区域要与 `mqtt_broker` 一致（cn 手机号 / us 邮箱） |
| `curl /api/printers` 返回 401 | 正常：接口已鉴权。先登录拿 Cookie，或看 `/healthz` |
| 状态一直 OFFLINE / 不更新 | 看 `/healthz` 的 `cloud.authenticated` 与 `printers[].mqtt_connected`；确认 Token 未失效、区域正确、`cn.mqtt.bambulab.com:8883` 可达 |
| 日志里找不到 `logs/app.log` | 看启动横幅打印的"日志文件"绝对路径；路径由 `settings.log_dir` / `BUMBO_LOG_DIR` 决定 |
| 摄像头黑屏/离线 | 确认打印机与主机同网段、`ip`/`access_code` 正确、固件已开启局域网；RTSPS 机型另确认 ffmpeg 可用（日志有"未找到 ffmpeg"告警） |
| 收不到通知 | 设置页确认通道 `enabled` 与各事件开关；企微检查机器人在群内且 IP 在白名单；飞书检查权限、事件订阅方式（长连接）与版本已发布 |
| 飞书点了启用/发送测试无效 | v0.5.18 起热加载即时生效；若仍失败，看日志是否有凭据/权限错误，再重启一次确认 |
| 群指令没反应 | 确认在群里 **@机器人**；未识别、平台不支持、冷却中的指令都会回话，收到回复即说明链路正常 |
| AI 检测不生效 | 确认已 `pip install ".[notify,ai]"` 且 `ai_guard.enabled: true`；pip 部署在非根目录运行时需配 `ai_guard.deploy_dir` |
| AI 启动有 `xFormers` 告警 | 预期降级（回退 MLP FFN），不是错误 |
| 改了 JS/CSS 但页面行为没变 | 静态资源已带版本串；若仍是旧行为，硬刷新（Ctrl+F5）或清除缓存 |
| `pip install` 报 exe 被占用 | 先退出所有 `serve`/`login`；清理 `site-packages/~*` 残留后 `pip install --force-reinstall .` |
| 开机自启后端口不监听、`LastTaskResult=255` | 启动批处理须保持 **CRLF 行尾**（LF 会被 cmd 误解析）；用 `Get-ScheduledTaskInfo` 查看返回码 |

## 开发与测试（仅源码仓库）

```bash
pip install -e ".[dev]"
python -m pytest tests/            # 全量测试
python -m pytest tests/test_database.py tests/test_monitor.py -x   # 核心子集
ruff check bumbo_stats tests
```

代码入口：`bumbo_stats/main.py`（CLI）、`bumbo_stats/api/`（FastAPI 路由/WebSocket）、
`bumbo_stats/printer/`（云端 MQTT 状态机、通知、AI 守卫）、`bumbo_stats/cloud_api.py`（自研云端 REST）。

发布包**不含** `tests/` 与开发文档（`AGENTS.md` / `DESIGN.md` / `SECURITY_REVIEW.md`）。

## License

[MIT](LICENSE)
