# 部署指南

面向人工部署与 **AI Agent 自动部署**。标 ⛔ 的步骤必须人类参与（接收验证码），Agent 不能代做。

## 环境要求

- Python ≥ 3.10（Windows / Linux / macOS 均可）
- **标准 CPython + pip 即可，无需 Anaconda / conda**；AI 检测用 CPU 版 `torch`，**不需要显卡**
- 拓竹云账号：`mqtt_broker: cn`（国内）用**手机号 + 短信验证码**；`mqtt_broker: us`（海外）用**邮箱 + 邮件验证码**
- 局域网内可访问的拓竹打印机（摄像头为可选项，不配也能看状态数据）

Windows 额外注意：

- 中文区域建议先设 `$env:PYTHONUTF8="1"`，否则个别源码包在 cp936 下构建会报 `UnicodeDecodeError`
- 安装 `[ai]`（torch）需要 **VC++ 2015-2022 x64 运行库 ≥ 14.36**；版本过旧会在 `import torch` 时报 `OSError [WinError 1114] ... c10.dll`
- 大陆网络可选用清华镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`（第三方索引，信任与否请自行判断）
- **安装/重装前先退出所有 `bambu-monitor serve` / `login` 进程**，否则 Windows 会因 exe 被占用留下半成品包

## 0. 先问清这些输入（缺则无法部署）

- 拓竹云账号 **+ 密码** ⛔
  - `mqtt_broker: cn`（国内账号）→ **手机号 + 短信验证码**
  - `mqtt_broker: us`（海外账号）→ **邮箱 + 邮件验证码**
- 每台打印机的 **序列号 `serial`**：照抄机身/包装标签原值（长度随机型为 15-18 位，以云端设备列表返回的 `dev_id` 为准）。
  **完成云端登录后其实不必手工收集**：用 **设置 → 打印机 → ☁️ 从云端导入** 可一次性列出账号下全部设备，
  序列号、机器名与摄像头接入码均由云端自动写入，只有局域网 IP 需要补填（或开启 `lan_discovery` 自动探测）。
- 看摄像头画面还需打印机局域网 `ip` + **接入码 `access_code`**；接入码可由云端导入自动获取，
  手工添加时才需要去打印机 Web 管理页 → 安全设置 → 接入码 查看
- 可选：是否要消息通知（企业微信/飞书凭据）、是否要 AI 缺陷检测

## 1. 安装

全程在仓库根目录（即含 `pyproject.toml` 的目录）执行。

**Windows PowerShell：**

```powershell
cd <仓库根目录>
$env:PYTHONUTF8 = "1"
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install ".[notify]"                 # 要 AI 缺陷检测改 ".[notify,ai]"
copy printers.yaml.example printers.yaml
notepad printers.yaml                   # 填 serial / name / ip / access_code 后保存
```

**Linux / macOS bash：**

```bash
cd <仓库根目录>
python3 -m venv venv && source venv/bin/activate
pip install ".[notify]"                 # 要 AI 缺陷检测改 ".[notify,ai]"
cp printers.yaml.example printers.yaml
$EDITOR printers.yaml
```

依赖分组：

| 分组 | 内容 | 何时装 |
|---|---|---|
| 核心 | FastAPI / uvicorn / MQTT / SQLite | 必装 |
| `[notify]` | 企业微信、飞书 SDK | 需要消息推送 |
| `[ai]` | torch、numpy 等（体积大，CPU 版即可） | 需要缺陷检测 |
| `[rtsp]` | imageio-ffmpeg（自带静态 ffmpeg） | X1/P2S/P2P/X2D/H2D/H2S 等 RTSPS 机型摄像头 |
| `[dev]` | pytest、ruff | 仅开发 |

## 2. 启动与登录

```bash
bambu-monitor serve          # 默认 http://127.0.0.1:8000
```

启动后：

1. 浏览器打开面板，首次设置管理员密码；
2. 在 **设置 → 云端** 填账号密码，⛔ 由人类回填短信/邮件验证码，登录成功后自动连接云端并热加载；
3. 在 **设置 → 打印机** 添加设备：优先点 **☁️ 从云端导入** 勾选账号下的机器（序列号/机器名/接入码自动写入），
   或点 ➕ 添加打印机手工录入；两种方式都是即时生效，无需重启。

也可以用 CLI 登录（同样 ⛔ 需要人类回填验证码）：

```bash
bambu-monitor login                     # cn 提示手机号，us 提示邮箱
# 成功标志：~/.bambu_monitor_token 生成（JSON，含 region 与 token）
```

## 3. 部署自检（逐条验证，全过即成功）

1. `bambu-monitor --version` 有输出
2. 启动横幅出现 `Bambu Monitor v<版本>`、`监控 N 台打印机`、面板地址，以及**配置文件与日志文件的绝对路径**
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
   - 模型加载在后台线程进行，不阻塞启动；首次推理成功后写 INFO 日志，`/healthz` 的 `ai.last_check_at` 也会更新

## 4. 必须知道的坑

- ⛔ **网页云端登录 / CLI `login` 都需要人类回填验证码**，Agent 不能自动完成；Token 落在 `~/.bambu_monitor_token`
- **不需要"先 login 再 serve"**：无 Token 会降级启动，先在网页登录即可
- `pip install .` 是非 editable 安装：修改项目根源码对已安装 CLI 无效；开发调试请 `pip install -e ".[dev]"`
- **改 `printers.yaml` 前先停 `serve`**：面板保存设置时只回写本次修改的段并保留 `.bak`，但运行期手工编辑仍可能被覆盖；增删打印机优先用网页 **设置 → 打印机**
- 日志路径由 `settings.log_dir` 决定（默认 `./logs`，相对启动目录；可用 `BAMBU_LOG_DIR` 覆盖），启动横幅会打印绝对路径
- 通知截图依赖 Playwright，需额外 `playwright install msedge`（或 chromium）；未安装时通知自动降级为纯文本
- AI 检测器自动探测仅在仓库根目录启动 `serve` 时生效；在其他目录启动需配 `ai_guard.deploy_dir` 或 `BAMBU_AI_DEPLOY_DIR=<根>/bambu_ai_deploy_dinoknn`；目录无效时 AI 自动禁用，不影响其他功能
- 摄像头按端口自动分流：A1/P1 系（TCP 6000）开箱即用；RTSPS 机型需 ffmpeg（日志出现 `摄像头协议探测: ... -> RTSPS(322, ffmpeg)` 即生效）
- Windows 计划任务启动脚本若用批处理，必须保存为 **CRLF 行尾**；纯 LF 会被 cmd 逐行误解析（`LastTaskResult=255` 且端口不监听）
- 安装/重装前先退出所有 `serve`/`login` 进程；重启前确认旧进程已退出，否则端口被占会报错退出

## 开机自启（Windows）

```powershell
# 以管理员身份运行；脚本内部用 schtasks 注册登录时触发任务
powershell -ExecutionPolicy Bypass -File .\register_autostart.ps1
```

手动启动可直接双击 `start_service.bat`（该文件与 `printers.yaml` 同属本地文件，不入库）。
