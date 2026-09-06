# AGENTS.md — bumbo_stats Agent 任务入口

> 这是面向 AI Agent 的最小任务入口；视觉/功能合约见 `DESIGN.md`，安全审查见 `SECURITY_REVIEW.md`。

## 项目定位

bumbo_stats 是拓竹 3D 打印机多机云端监控面板，集成 FastAPI（HTTP/WebSocket）+ SQLite（产能与快照）+ MQTT（打印机状态）+ uvicorn（ASGI 运行时）。

## 入口命令

```bash
# 首次登录拓竹云账号获取 Token（凭据保存到用户主目录的 `.bumbo_token`）
python -m bumbo_stats.main login

# 启动监控面板 Web 服务
python -m bumbo_stats.main serve --host 0.0.0.0 --debug

# 配置 / Token 文件指定
python -m bumbo_stats.main --config /path/to/printers.yaml serve --port 8080
```

> 旧式无子命令的 `python -m bumbo_stats.main --port 8080` 仍兼容，但新增会话请使用 `serve` 子命令。

## 测试入口

```bash
# 全套（pyproject.toml 已设置 asyncio_mode=auto，可直接运行）
python -m pytest tests/

# 修改 database.py / monitor.py 后必跑的子集（覆盖目标 100%）
python -m pytest tests/test_database.py tests/test_monitor.py tests/test_numerical.py
```

## 关键约束

- `printers.yaml` 已在 `.gitignore`，不要将其写入仓库或回显凭据。
- 敏感凭据（企业微信 `secret`、摄像头 `access_code`、Token）不得硬编码；详见 `SECURITY_REVIEW.md`。
- 日志写入 `logs/app.log`（按天轮转，保留 7 天），不要修改其结构。
- 修改 `pyproject.toml` 中的 `asyncio_mode = "auto"`、第三方库日志静默白名单等既有设置。

## 关键目录速览

| 路径 | 职责 |
| --- | --- |
| `bumbo_stats/api/` | FastAPI 路由、`/ws` WebSocket 推送、`settings.html` 后端 |
| `bumbo_stats/printer/` | MQTT 状态机、相机帧、AI 异常守卫、企微/飞书通知（详见 `bumbo_stats/printer/AGENTS.md`） |
| `bumbo_stats/database.py` | SQLite 会话、产能、快照、心跳；100% 覆盖目标 |
| `bumbo_stats/printer/monitor.py` | 事件驱动 + 定期批量广播循环；100% 覆盖目标 |
| `bumbo_stats/core.py` / `models.py` | 配置加载与 HMS 错误码常量表 |
| `bumbo_stats/auth.py` | 拓竹云 Token 持久化与交互式登录 |
| `tests/` | 19 个测试文件，覆盖 database、monitor、数值精度、通知接口 |

## 改动后的最小验证

命中下列文件时，按该子集跑 pytest 后再继续：

| 修改命中 | 必跑命令 |
| --- | --- |
| `bumbo_stats/database.py` | `python -m pytest tests/test_database.py tests/test_numerical.py -x` |
| `bumbo_stats/printer/monitor.py` | `python -m pytest tests/test_monitor.py tests/test_numerical.py -x` |
| `bumbo_stats/printer/wecom.py` | `python -m pytest tests/test_wecom_notify.py -x` |
| `bumbo_stats/printer/feishu.py` | `python -m pytest tests/test_feishu_notify.py -x` |
| `bumbo_stats/api/*` / `bumbo_stats/auth.py` / 其它 | `python -m pytest tests/ -x` |

## 不属于本次入口的范畴

- 不修改 `pyproject.toml`、`DESIGN.md`、`SECURITY_REVIEW.md`。
- 不新增项目级 Skill / Command / Custom Agent / MCP / Workflow。
- 不做依赖大版本升级；遵循 `bumbo_stats/models.py` 中既有的数据契约。
