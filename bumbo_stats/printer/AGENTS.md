# AGENTS.md — bumbo_stats/printer Agent 任务入口

> 子目录级任务入口；项目级入口见仓库根 `AGENTS.md`。本目录文件聚焦 MQTT 事件、相机帧、企微/飞书通知与 AI 异常守卫。

## 模块速览

| 文件 | 职责 |
| --- | --- |
| `monitor.py` | `PrintMonitor`：事件驱动 + 定期批量广播，封装 layer3 / 50% / completed 三类通知的去重状态 |
| `manager.py` | `PrinterManager`：维护打印机连接、状态聚合、MQTT 订阅生命周期 |
| `camera.py` | 摄像头 JPEG 帧拉取与缩略图压缩 |
| `ai_guard.py` | AI 异常检测守卫，串联 `bambu_ai_deploy_*` 推理模型 |
| `wecom.py` | 企业微信机器人通知（含命令指令路由、layer3/cooldown 去重、HMS/AI 报警合并） |
| `feishu.py` | 飞书机器人通知（与 `wecom.py` 镜像但走飞书模板卡片） |
| `__init__.py` | 包标识，无公开符号 |

## MQTT 状态机

- 入口：`PrintMonitor.handle_status_update(...)`，由 `PrinterManager` 在 MQTT 推送到来时调用。
- 状态枚举见 `bumbo_stats/models.py::PrinterState`，常见迁移：UNKNOWN → IDLE → RUNNING / PAUSE / FINISH / FAILED / OFFLINE。
- 进度字段 `mc_percent` 从 0 → 100；`NOTIFY_PROGRESS=50`、`NOTIFY_COMPLETE_PROGRESS=99` 见 `monitor.py` 顶部常量。
- 重连/断线通过 `_last_definite_state` 区分「同一任务的连接抖动」和「新任务」，避免重发通知。
- 心跳：`HEARTBEAT_INTERVAL=60.0` 秒写一次 `Database.update_heartbeat()`，用于结算停机期的孤儿 session。

## 企微去重规则（wecom.py）

`NotifyWeCom.notify(...)` 是主入口，状态由以下三类键维护：

| 维度 | 字段 | 含义 |
| --- | --- | --- |
| task 边界 | `_last_gcode[serial]` | 当前通知周期对应的 `gcode`，切换即视为新任务 |
| layer3 | `_layer3_notified[serial]` | `NOTIFY_LAYER=3` 时是否已发，去重直到新任务 |
| 50% 进度 | `_progress50_notified[serial]` | `NOTIFY_PROGRESS=50` 时是否已发，去重直到新任务 |
| completed | `_completed_notified[serial]` | `NOTIFY_COMPLETE_PROGRESS=99` 时是否已发，避免每条消息刷日志 |

新任务判定：`_notify_job_gcode[serial]` 与上一 `gcode` 不同 → 清空上述三个标记位，让 layer3 / 50% / completed 重新触发一次。

指令侧 cooldown：`COMMAND_COOLDOWN` 字典定义每条命令的最小间隔；`_last_cmd_ts[cmd]` 保存上次触发时间。

## HMS 错误处理

- 数据源：拓竹 MQTT push 中的 `hms_errors` 列表（每项含 `code`/`severity`/`message`），过滤 `severity == "error"` 后入队通知。
- 错误码常量表：`bumbo_stats/models.py::HMS_ERROR_CODES`（int → `{cn, en, category, suggestion}`），按 `0x03xxxx / 0x05xxxx / 0x07xxxx / 0x0Cxxxx / 0x12xxxx / 0x18xxxx / 0x1Axxxx` 分类。
- 合并：`monitor.handle_status_update()` 用 `set` 去重 `code`，同一码只触发一次通知；不同打印机独立计数。
- AI 报警与 HMS 互不抑制：AI 报警由 `ai_guard.py` 单独走图片帧推送，与 HMS 文本通知正交。

## AI 异常守卫（ai_guard.py）

- 使用 `bambu_ai_deploy_dinoknn`（DINOv2 + kNN，当前生产部署方案）；旧研究方案 `bambu_ai_deploy_onnx`/`bambu_ai_deploy_pytorch` 仅留在本地，不入 git 不入发布包。
- 评分阈值、冷却时间、报警帧采样间隔均由 `printers.yaml` 提供，不要硬编码覆盖。
- 命中后将帧缓存到 `data/alerts_review/<serial>_<ts>_P<score>/`，由 `wecom.py`/`feishu.py` 异步推送。

## 修改本目录文件时的验证

| 修改命中 | 必跑命令 |
| --- | --- |
| `monitor.py` | `python -m pytest tests/test_monitor.py tests/test_numerical.py -x` |
| `wecom.py` | `python -m pytest tests/test_wecom_notify.py -x` |
| `feishu.py` | `python -m pytest tests/test_feishu_notify.py -x` |
| `ai_guard.py` | `python -m pytest tests/test_ai_guard.py -x` |
| `manager.py` / `camera.py` | `python -m pytest tests/test_manager.py tests/test_camera.py -x` |

同时参照仓库根 `AGENTS.md` 的项目级入口与约束（本目录不重复列全局命令）。
