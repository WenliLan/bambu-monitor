# 配置参考（printers.yaml）

从 `printers.yaml.example` 复制一份为 `printers.yaml` 后按需修改。面板 **⚙️ 设置页** 可在线完成大部分配置。

| 段落 | 作用 | 关键字段 |
|---|---|---|
| `cloud` | 拓竹云账号 | `token_file`(默认 `~/.bambu_monitor_token`)、`mqtt_broker`(`cn`/`us`，须与账号区域一致) |
| `printers` | 打印机列表 | `name`、`serial`(15-18 位标签原值)、`ip`(摄像头用)、`access_code`(打印机管理页获取)、`location` |
| `settings` | 服务 | `host`(默认 `127.0.0.1`)、`port`、`db_path`、`camera_interval`、`log_dir`(默认 `./logs`)、`dashboard_url` |
| `wecom` | 企业微信机器人 | `enabled`、`bot_id`、`secret`、`chatid`(留空自动发现)及各事件开关 |
| `feishu` | 飞书机器人 | `app_id`、`app_secret`、`chatid`、事件开关、加急(`urgent_*`) |
| `ai_guard` | AI 缺陷检测 | `enabled`、`interval`、`threshold`、`alert_frames`、`cooldown`、`alert_dir`、`deploy_dir` |
| `ui` | 面板登录 | `admin_password_hash`(首次设密码时自动生成，无需手填) |

## 路径解析规则

**相对路径**（`db_path` / `log_dir` / `alert_dir` / `deploy_dir`）均按**启动时所在目录**解析，建议固定在仓库根目录启动 `serve`。

## 环境变量覆盖

以下环境变量优先级高于 YAML，适合多用户 / 容器环境避免明文落盘：

| 变量 | 覆盖项 |
|---|---|
| `BAMBU_TOKEN_FILE` | `cloud.token_file` |
| `BAMBU_WECOM_BOT_ID` / `BAMBU_WECOM_SECRET` | 企业微信凭据 |
| `BAMBU_FEISHU_APP_ID` / `BAMBU_FEISHU_APP_SECRET` | 飞书凭据 |
| `BAMBU_DASHBOARD_URL` | `settings.dashboard_url` |
| `BAMBU_LOG_DIR` | `settings.log_dir` |
| `BAMBU_AI_DEPLOY_DIR` | `ai_guard.deploy_dir` |
| `BAMBU_FFMPEG_EXE` | RTSPS 机型 ffmpeg 可执行文件路径 |

## 行为说明

- 面板保存设置时只回写本次修改的段并生成 `printers.yaml.bak`，其他段落与未知键保留
- 增删打印机推荐用 **设置 → 打印机**（热加载，无需重启）；手改 YAML 后需重启 `serve`
- `dashboard_url` 配置后，通知与群指令的回复末尾会附"打开监控面板"链接
- `ai_guard.threshold` 填 `0` 表示沿用特征库出厂阈值（当前为 `0.7`）；阈值越高误报越少、漏报风险增加
