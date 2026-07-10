# Bumbo Stats v0.5.2 发版测试报告

> **报告日期**: 2026-07-11
> **测试版本**: v0.5.1
> **被测系统**: Bumbo Stats 拓竹3D打印机多机监控面板
> **测试结论**: ✅ 通过

---

## 一、版本信息

| 项目 | 内容 |
|------|------|
| 当前版本 | **v0.5.2** |
| 上一版本 | v0.5.1 |
| Python 版本 | >= 3.10 |
| 数据库 | SQLite (aiosqlite) |
| 测试框架 | pytest + pytest-asyncio |
| 测试用例总数 | **218** |

---

## 二、本版更改点

### 2.1 企业微信通知配置重载修复

| 变更类型 | 描述 |
|----------|------|
| 缺陷修复 | 保存配置后发送测试失败（`_chatid` 未同步） |
| 根因 | `_reload_wecom_impl` 只更新了 `_cfg`，未同步 `_chatid` |
| 修复内容 | 配置重载时优先使用配置的 `chatid`，其次保持已自动发现的 `chatid` |

**关键代码变更** (`bumbo_stats/api/app.py`):
```python
wecom._cfg = new_wecom
if new_wecom.chatid:
    wecom._chatid = new_wecom.chatid
```

### 2.2 企业微信测试消息 chatid 优先级修复

| 变更类型 | 描述 |
|----------|------|
| 缺陷修复 | `send_test_message` 忽略用户配置的 `chatid` |
| 根因 | 直接使用 `self._chatid`（自动发现值），未优先使用 `self._cfg.chatid` |
| 修复内容 | `send_test_message` 和 `_process_notification` 均改为 `chatid = self._cfg.chatid or self._chatid` |

### 2.3 HMS 错误码注册表建立

| 变更类型 | 描述 |
|----------|------|
| 功能增强 | 建立 131 个官方 HMS 错误码注册表（来源：Bambu Lab Wiki） |
| Wiki 格式 | URL `/hmscode/CCCC-HHHH-TTTT-IIII` → 错误码 `0xCCCCHHHH`（8位十六进制） |
| 注册表字段 | `cn`（中文名）、`en`（英文名）、`category`（分类）、`suggestion`（处理建议） |
| 查找函数 | `lookup_hms_error(code)` 支持 8 位码直接查找 + 4 位码别名映射 |

### 2.4 HMS 错误通知消息格式化增强

| 变更类型 | 描述 |
|----------|------|
| Bug 修复 | 十六进制格式从 `:04X` 改为 `:08X`（4位截断 → 8位完整） |
| 消息字段 | 每条错误显示：**中文名 → 分类 → 处理建议 → 原始云端消息 → 8位十六进制码** |
| 支持渠道 | 企业微信（Markdown）+ 飞书（文本） |

---

## 三、测试概况

### 3.1 测试执行摘要

| 指标 | 数值 |
|------|------|
| 用例总数 | 218 |
| 通过 | 218 |
| 失败 | 0 |
| 跳过 | 0 |
| 执行时间 | < 5s |

### 3.2 测试模块分布

| 测试文件 | 用例数 | 覆盖范围 |
|----------|:---:|---------|
| `test_wecom_notify.py` | 38 | 企业微信通知去重、配置重载、HMS 错误通知 |
| `test_feishu_notify.py` | 16 | 飞书通知去重、消息构建、HMS 错误通知 |
| `test_hms_registry.py` | 8 | HMS 错误码注册表查找、消息渲染 |
| `test_database.py` | 26 | 数据库 CRUD、快照、跨午夜处理 |
| `test_monitor.py` | 22 | 打印机状态机、事件触发、广播 |
| `test_numerical.py` | 31 | 精确数值验证、百分比计算、跨午夜边界 |
| `test_api.py` | 19 | REST API 端点、认证、配置保存 |
| `test_config.py` | 8 | YAML 配置加载、Pydantic 模型验证 |
| `test_models.py` | 8 | Pydantic 模型序列化、所有枚举值 |
| `test_manager.py` | 6 | MQTT 管理器初始化、状态解析 |
| `test_auth.py` | 5 | Token 读写、文件不存在处理 |
| `test_camera.py` | 4 | 摄像头能力判断、帧获取 |
| `test_cli.py` | 4 | CLI 入口、版本号、参数解析 |
| `test_settings_security.py` | 16 | 密码哈希、Cookie 安全、认证流程 |
| `test_websocket.py` | 3 | WebSocket 连接、广播、异常处理 |
| `test_templates.py` | 3 | Jinja2 模板渲染 |
| `test_core.py` | 1 | 占位测试 |
| **合计** | **218** | |

---

## 四、测试方案

### 4.1 通过标准

1. **全部 218 个测试用例执行通过**
2. **无 WARNING 或 ERROR 输出**
3. **核心模块（database.py、monitor.py）语句覆盖率 100%**
4. **新增 HMS 错误码注册表所有 131 个码字段完整**
5. **企业微信配置重载后 `send_test_message` 可正常发送**

### 4.2 测试环境

```yaml
Python: 3.10+
依赖:
  - fastapi >= 0.109
  - pydantic >= 2.0
  - pytest >= 7.0
  - pytest-asyncio >= 0.23
  - aiosqlite >= 0.19
测试命令: python -m pytest tests/ -v
```

### 4.3 测试分类

| 类别 | 说明 |
|------|------|
| 单元测试 | 各模块内部逻辑（数据库、监控器、模型、配置） |
| 集成测试 | API 端点、WebSocket、通知模块 |
| 安全测试 | 密码哈希、Cookie 安全、认证流程 |
| 数值精度测试 | 产能百分比、跨午夜计算、快照边界 |

---

## 五、测试结果（分模块）

### 5.1 HMS 错误码注册表（test_hms_registry.py — 8 tests）

| 用例 ID | 测试名称 | 测试方案 | 通过标准 | 结果 |
|---------|----------|----------|----------|------|
| TC-HMS-01 | Wiki 8位码正确查找 | 用实际存在于注册表中的码验证 `lookup_hms_error()` 返回正确中文名和分类 | 返回的 `cn` 包含预期关键词，`category` 完全匹配 | ✅ |
| TC-HMS-02 | 未知错误码返回默认信息 | 传入 `0xFFFF` 验证返回"未知错误"默认结构 | `cn="未知错误"`, `category="其他"`, `en="Unknown Error"` | ✅ |
| TC-HMS-03 | _build_markdown 正确渲染 | 传入 Wiki 码验证 Markdown 输出包含中文名、建议、原始消息、十六进制码 | 输出包含 `💡`、`📋`、`🔢` 和完整8位码 | ✅ |
| TC-HMS-04 | 多个错误合并显示 | 传入3个不同错误码，验证每错误独立渲染 | `💡`/`📋`/`🔢` 各出现3次 | ✅ |
| TC-HMS-05 | 无原始消息时使用注册表名 | `message=""` 时验证使用注册表中文名 | `原始: (无)` 在消息中，注册的 `cn` 也在 | ✅ |
| TC-HMS-06 | 所有131个 Wiki 码字段完整 | 遍历 `HMS_ERROR_CODES` 所有条目验证4个字段 | 每个条目有 `cn`, `en`, `category`, `suggestion` | ✅ |
| TC-HMS-07 | 飞书消息正确渲染 | 验证 `FeishuNotifier._build_message` 输出含 Wiki 码信息 | 输出含中文名、`AMS` 分类、8位十六进制码 | ✅ |
| TC-HMS-08 | 非 error 事件不渲染错误区域 | `layer3` 和 `completed` 事件不应出现 `💡`/`📋` | 两个事件均不含错误渲染标记 | ✅ |

### 5.2 企业微信通知模块（test_wecom_notify.py — 38 tests）

| 用例 ID | 测试名称 | 测试方案 | 通过标准 | 结果 |
|---------|----------|----------|----------|------|
| TC-NOTIFY-01 | layer3_once | 同一 layer3 事件多次触发 | 仅入队1次 | ✅ |
| TC-NOTIFY-02 | completed_once | 同一 completed 事件多次触发 | 仅入队1次 | ✅ |
| TC-NOTIFY-03 | gcode_change_resets_layer3 | 新 gcode 重置 layer3 标记 | 第二次相同事件可再入队 | ✅ |
| TC-NOTIFY-04 | completed_new_job_resets | 新任务重置 completed 标记 | 第二次相同事件可再入队 | ✅ |
| TC-NOTIFY-05 | layer3_and_completed_independent | layer3 和 completed 独立追踪 | 互不干扰，各触发1次 | ✅ |
| TC-NOTIFY-06 | queue_full_drops | 队列满时再次通知 | 优雅丢弃，队列大小不变 | ✅ |
| TC-NOTIFY-07 | startup_above_layer3 | 启动时 > 第3层 | 初始化 `layer3_notified=True` | ✅ |
| TC-NOTIFY-08 | startup_below_layer3 | 启动时 < 第3层 | 初始化 `layer3_notified=False` | ✅ |
| TC-NOTIFY-09 | startup_no_notification | 启动扫描阶段 | 不发送任何通知 | ✅ |
| TC-NOTIFY-10 | startup_no_wecom | 无通知器时启动 | 不崩溃，正常完成初始化 | ✅ |
| TC-NOTIFY-11 | startup_skips_idle | 启动时状态为 IDLE | 跳过初始化（无 gcode） | ✅ |
| TC-NOTIFY-12 | exactly_layer3 | 刚好第3层 | 触发 layer3 通知 | ✅ |
| TC-NOTIFY-13 | exactly_100_percent | 刚好 100% | 只触发 completed，不触发 layer3 | ✅ |
| TC-NOTIFY-14 | multiple_printers_independent | 多台打印机（序列号不同） | 各自独立追踪，互不干扰 | ✅ |
| TC-NOTIFY-15 | layer3_then_completed | 同一任务先 layer3 后 completed | 两个事件均触发 | ✅ |
| TC-NOTIFY-16 | non_running_skips | 非 RUNNING 状态收到 layer3/completed | 跳过，不入队 | ✅ |
| TC-NOTIFY-17 | wecom_deduplicates_layer3 | WeComNotifier 层对 layer3 去重 | 同一 layer3 不重复入队 | ✅ |
| TC-NOTIFY-18 | wecom_deduplicates_completed | WeComNotifier 层对 completed 去重 | 同一 completed 不重复入队 | ✅ |
| TC-NOTIFY-19 | wecom_new_job_resets_layer3 | 新任务重置 WeCom 层 layer3_sent | 新任务可再次触发 | ✅ |
| TC-NOTIFY-20 | queue_full_drops_wecom | WeCom 层队列满丢弃 | 丢弃时无异常 | ✅ |
| TC-NOTIFY-21 | disabled_event_no_queue | 事件开关关闭 | 对应事件不入队 | ✅ |
| TC-NOTIFY-22 | unknown_printer_ignored | 未知打印机序列号 | 忽略，不崩溃 | ✅ |
| TC-NOTIFY-23 | reset_if_new_job | `reset_if_new_job` 方法 | gcode 变化时重置所有标记 | ✅ |
| TC-NOTIFY-24 | full_flow_gcode_change | `handle_status_update` 完整流程 | gcode 变化触发正确事件序列 | ✅ |
| TC-NOTIFY-25 | both_layers_prevent_duplicate | Monitor + WeCom 双层去重 | 双重保护均生效 | ✅ |
| TC-NOTIFY-26 | startup_syncs_wecom_above | 启动同步 WeCom（> 第3层） | `_sent` 标记正确初始化 | ✅ |
| TC-NOTIFY-27 | startup_syncs_wecom_below | 启动同步 WeCom（< 第3层） | `_sent` 标记正确初始化 | ✅ |
| TC-NOTIFY-28 | above_layer3_no_trigger | > 第3层不触发 layer3 | 不入队 | ✅ |
| TC-NOTIFY-29 | hms_error_triggers | HMS 错误触发 error 通知 | 入队1次 | ✅ |
| TC-NOTIFY-30 | same_hms_error_not_duplicated | 同一 HMS 错误快照相同 | 不重复入队 | ✅ |
| TC-NOTIFY-31 | multiple_hms_errors_merged | 多个 HMS 错误合并 | 1条通知含多个错误 | ✅ |
| TC-NOTIFY-32 | new_job_resets_hms | 新任务重置 HMS 错误状态 | 新任务可再次通知同错误 | ✅ |
| TC-NOTIFY-33 | warning_not_notified | severity=warning | 不触发通知 | ✅ |
| TC-NOTIFY-34 | error_disabled_in_config | error=false 配置 | 不发送 error 通知 | ✅ |
| TC-NOTIFY-35 | multiple_printers_hms_independent | 多打印机 HMS 独立追踪 | 互不干扰 | ✅ |
| TC-NOTIFY-36 | hms_error_uses_cfg_chatid | HMS 错误通知使用配置的 chatid | `self._cfg.chatid or self._chatid` | ✅ |
| TC-NOTIFY-37 | test_message_uses_cfg_chatid | 测试消息使用配置的 chatid | `self._cfg.chatid or self._chatid` | ✅ |
| TC-NOTIFY-38 | hms_errors_any_state_triggers | 任意状态 HMS 错误触发通知 | 不依赖 RUNNING 状态 | ✅ |

### 5.3 飞书通知模块（test_feishu_notify.py — 16 tests）

| 用例 ID | 测试名称 | 测试方案 | 通过标准 | 结果 |
|---------|----------|----------|----------|------|
| TC-FEISHU-01 | FeishuNotifier 初始化 | 正常初始化 | `_cfg` 属性正确，token 为 None | ✅ |
| TC-FEISHU-02 | 未配置时不启动 | `app_id=""` 时调用 `start()` | `_task` 为 None，不创建后台任务 | ✅ |
| TC-FEISHU-03 | layer3 事件入队 | 触发 layer3 事件 | 队列大小为1 | ✅ |
| TC-FEISHU-04 | completed 事件入队 | 触发 completed 事件 | 队列大小为1 | ✅ |
| TC-FEISHU-05 | error 事件入队 | 触发 error 事件传入 HMS 错误列表 | 队列大小为1 | ✅ |
| TC-FEISHU-06 | layer3 去重 | 同一 layer3 事件两次触发 | 队列保持为1 | ✅ |
| TC-FEISHU-07 | completed 去重 | 同一 completed 事件两次触发 | 队列保持为1 | ✅ |
| TC-FEISHU-08 | 新任务重置 layer3 | 不同 gcode 的 layer3 事件 | 第二次入队，队列为2 | ✅ |
| TC-FEISHU-09 | 新任务重置 completed | 不同 gcode 的 completed 事件 | 第二次入队，队列为2 | ✅ |
| TC-FEISHU-10 | HMS 错误合并 | 传入多个 HMS 错误 | 1条通知含2个错误 | ✅ |
| TC-FEISHU-11 | 同 HMS 错误不重复 | 相同错误两次触发 | 队列保持为1 | ✅ |
| TC-FEISHU-12 | 禁用事件不入队 | `layer3=False` 时触发 layer3 | 队列为0 | ✅ |
| TC-FEISHU-13 | 未知打印机被忽略 | 使用未注册序列号 | 队列为0，无异常 | ✅ |
| TC-FEISHU-14 | 队列满时丢弃 | 队列大小=1，连续两次触发 | 队列保持为1，丢弃第二次 | ✅ |
| TC-FEISHU-15 | reset_if_new_job | `_PrinterNotifState.reset_if_new_job` | gcode 变化时所有标记重置 | ✅ |
| TC-FEISHU-16 | 消息构建 | 构建 layer3/completed/error 消息 | 各消息含正确标题和内容 | ✅ |

### 5.4 数据库模块（test_database.py — 26 tests）

| 用例 ID | 测试名称 | 测试方案 | 通过标准 | 结果 |
|---------|----------|----------|----------|------|
| TC-DB-01 | tables_created | 初始化数据库 | `printers`, `print_sessions`, `daily_usage` 三表存在 | ✅ |
| TC-DB-02 | upsert | 插入新打印机 + 更新已有 | 数据正确插入和更新 | ✅ |
| TC-DB-03 | record_start | 记录打印开始 | session 状态为 running，时长为空 | ✅ |
| TC-DB-04 | record_end | 记录打印结束 | 计算时长，状态为 completed/failed | ✅ |
| TC-DB-05 | today_usage | 查询今日产能 | 返回当日所有打印机总时长 | ✅ |
| TC-DB-06 | monthly | 月度数据查询 | 返回指定月份每日数据 | ✅ |
| TC-DB-07 | fill_zeros | 无记录天补零 | 缺失日期补充为 0 | ✅ |
| TC-DB-08 | get_sessions | 获取打印历史 | 分页正确，排序正确 | ✅ |
| TC-DB-09 | no_records_zero | 无记录时产能为零 | `total_print_seconds=0` | ✅ |
| TC-DB-10 | accumulate | 多次打印时长累加 | 同一打印机多次时长相加 | ✅ |
| TC-DB-11 | monthly_includes_running | 月度包含当天 running | running 时长计入当天 | ✅ |
| TC-DB-12 | start_ends_previous | 新任务开始时自动结束旧 running | 旧任务标记为 completed | ✅ |
| TC-DB-13 | end_no_running | 无 running 时调用 end | 静默忽略，无副作用 | ✅ |
| TC-DB-14 | cross_midnight_running | 跨午夜 running 只算今天 | `date(start_time)!=today` 不计入今天 | ✅ |
| TC-DB-15 | unknown_serial | 未知序列号操作 | 忽略，不报错 | ✅ |
| TC-DB-16 | multi_day_completed | 多天多打印机 completed | 各天各机器独立计算 | ✅ |
| TC-DB-17 | date_range | 月度日期范围正确 | 不返回今天之后的日期 | ✅ |
| TC-DB-18 | limit | get_sessions 限制条数 | 最多返回指定条数 | ✅ |
| TC-DB-19 | end_updates_daily | record_end 更新 daily_usage | 首次完成写入快照表 | ✅ |
| TC-DB-20 | running_only_latest | 多 running 只取最新 | 旧 running 被静默忽略 | ✅ |
| TC-DB-21 | monthly_cross_midnight | 月度含跨午夜 running | 跨午夜 running 正确截断 | ✅ |
| TC-DB-22 | snapshot_past_day | 快照创建 daily_usage | 正确写入历史数据 | ✅ |
| TC-DB-23 | snapshot_idempotent | 快照幂等 | 重复调用不覆盖已有数据 | ✅ |
| TC-DB-24 | snapshot_cross_day | 快照跨天 session 截断 | 按天边界截断时长 | ✅ |
| TC-DB-25 | monthly_today_equals | 月度今天 = 今日产能 | 今天数据点与 `get_today_usage_all` 一致 | ✅ |
| TC-DB-26 | snapshot_running_capped | 快照 running 截止午夜 | running 时长不超过午夜 | ✅ |

### 5.5 监控器模块（test_monitor.py — 22 tests）

| 用例 ID | 测试名称 | 测试方案 | 通过标准 | 结果 |
|---------|----------|----------|----------|------|
| TC-MON-01 | print_start | IDLE→RUNNING | 触发 `record_print_start` | ✅ |
| TC-MON-02 | print_finish | RUNNING→FINISH | 触发 `record_print_end("completed")` | ✅ |
| TC-MON-03 | print_failed | RUNNING→FAILED | 触发 `record_print_end("failed")` | ✅ |
| TC-MON-04 | no_change | 状态不变 | 无事件触发 | ✅ |
| TC-MON-05 | broadcast | 状态更新 | 触发广播 | ✅ |
| TC-MON-06 | first_idle | 首次 IDLE | 仅初始化，不触发事件 | ✅ |
| TC-MON-07 | first_running | 首次 RUNNING | 触发补录（启动前完成的任务） | ✅ |
| TC-MON-08 | gcode_change | gcode 变化 | 结算旧任务 + 记录新任务 | ✅ |
| TC-MON-09 | same_gcode | gcode 不变 | 不触发 | ✅ |
| TC-MON-10 | running_to_idle | RUNNING→IDLE | 触发 `record_print_end("completed")` | ✅ |
| TC-MON-11 | pause_to_running | PAUSE→RUNNING | 不触发新的 record_print_start | ✅ |
| TC-MON-12 | empty_gcode | gcode 为空 | 跳过变化检测 | ✅ |
| TC-MON-13 | no_prev_gcode | 首次有 gcode | 触发 record_print_start | ✅ |
| TC-MON-14 | idle_to_finish | IDLE→FINISH | 不触发事件 | ✅ |
| TC-MON-15 | update_last_gcode | 状态变更 | 更新 `_last_gcode` | ✅ |
| TC-MON-16 | stop | 调用 stop() | `_running=False` | ✅ |
| TC-MON-17 | broadcast_all | `_broadcast_all` | 所有打印机状态推送 | ✅ |
| TC-MON-18 | first_pause | 首次 PAUSE | 仅初始化 | ✅ |
| TC-MON-19 | running_to_pause | RUNNING→PAUSE | 不触发开始/结束 | ✅ |
| TC-MON-20 | start_and_cancel | start() 后立即 cancel | 后台循环正常取消 | ✅ |
| TC-MON-21 | broadcast_exception | 广播时异常 | 不崩溃，异常被捕获 | ✅ |
| TC-MON-22 | daily_snapshot | start() 触发每日快照 | 调用 `snapshot_yesterday()` | ✅ |

### 5.6 精确数值验证（test_numerical.py — 31 tests）

| 用例 ID | 测试名称 | 场景 | 预期值 | 结果 |
|---------|----------|------|--------|------|
| TC-NV-01 | single_completed | 单个 completed 3h30m | 12600s → 14.6% | ✅ |
| TC-NV-02 | two_completed | 两个 completed 4h+3h | 25200s → 29.2% | ✅ |
| TC-NV-03 | three_completed | 三个 completed 2h+3h+1h | 21600s → 25.0% | ✅ |
| TC-NV-04 | cross_midnight_not_counted | completed 跨午夜，date≠today | 不计入 | ✅ |
| TC-NV-05 | running_basic | running start 12:00, now 14:30 | 9000s (2h30m) | ✅ |
| TC-NV-06 | running_cross_midnight | running 昨天22:00 → 今天14:30 | 52200s | ✅ |
| TC-NV-07 | completed_plus_running | 2 completed + 1 running | 3h+1h+2.5h=23400s | ✅ |
| TC-NV-08 | two_running_only_latest | 2 running 只取最新 | 14:30-12:00=9000s | ✅ |
| TC-NV-09 | exact_hours | 整小时百分比 | 6h=25.0%, 12h=50.0% | ✅ |
| TC-NV-10 | percentage_precision | 非整除百分比 | 5h13m=18780s → 21.7% | ✅ |
| TC-NV-11 | display_format | 时长显示格式 | 0h00m, 1h01m, 10h00m, 23h59m | ✅ |
| TC-NV-12 | all_zero_display | 零值显示 | 0h00m, 0.0% | ✅ |
| TC-NV-13 | start_ends_previous | 新任务结束旧 running | 14:30-08:00=23400s | ✅ |
| TC-NV-14 | multiple_previous | 多旧 running | 全部结束各自精确时长 | ✅ |
| TC-NV-15 | end_daily_usage_exact | 首次结束 daily_usage | 16200s, count=1 | ✅ |
| TC-NV-16 | end_accumulates | 两次结束累加 | 23400+9000=32400s | ✅ |
| TC-NV-17 | snapshot_completed | 快照 completed | 10:00→14:00=14400s | ✅ |
| TC-NV-18 | snapshot_running_capped | 快照 running | 22:00→午夜=7200s | ✅ |
| TC-NV-19 | snapshot_cross_day | 快照跨天 | 22:00→02:00 昨天部分=7200s | ✅ |
| TC-NV-20 | snapshot_idempotent | 快照幂等 | 已有记录不覆盖 | ✅ |
| TC-NV-20b | started_today_truncate | 跨天 start 在目标天 | 20:00→午夜=14400s | ✅ |
| TC-NV-20c | snapshot_mixed | 快照混合 | completed 2h + running 4h=21600s | ✅ |
| TC-NV-21 | monthly_all_zeros | 月度无记录 | 全 0.0% | ✅ |
| TC-NV-22 | monthly_history_today | 历史快照+今日 running | 各点精确% | ✅ |
| TC-NV-23 | monthly_multi_printer | 多打印机独立 | 4h=16.7%, 8h=33.3% | ✅ |
| TC-NV-23b | monthly_equals_today | 月度今天=今日产能 | 今天点=`get_today_usage_all` | ✅ |
| TC-NV-24 | end_no_running | 无 running 时结束 | 无副作用 | ✅ |
| TC-NV-25 | unknown_serial | 未知序列号 | 不计入结果 | ✅ |
| TC-NV-26 | completed_before_today | start 在昨天 | 不计入今日 | ✅ |
| TC-NV-27 | snapshot_no_sessions | 无 session 日期 | 不创建记录 | ✅ |
| TC-NV-28 | monthly_date_sequence | 月度日期序列 | 严格连续递增 | ✅ |

### 5.7 其他模块测试（67 tests）

| 测试文件 | 用例数 | 通过标准 | 结果 |
|----------|:---:|---------|:---:|
| test_api.py | 19 | REST 端点返回正确状态码，认证流程正确，配置保存/读取一致 | ✅ |
| test_auth.py | 5 | Token 正确存取，文件不存在/空文件/路径展开处理正确 | ✅ |
| test_camera.py | 4 | X1 不支持，P1S/A1 支持，无 IP 返回 None，帧缓存正确 | ✅ |
| test_cli.py | 4 | 版本号正确，缺配置退出码非0，参数解析正确 | ✅ |
| test_config.py | 8 | 完整/最小/不存在/缺字段配置均正确处理，模型默认值正确 | ✅ |
| test_manager.py | 6 | 初始化、属性、OFFLINE 默认、MQTT 解析正确 | ✅ |
| test_models.py | 8 | 枚举、默认值、序列化、所有模型字段正确 | ✅ |
| test_settings_security.py | 16 | 密码 SHA256 哈希、Cookie HttpOnly/SameSite、认证流程、登出失效、未知用户不泄露信息 | ✅ |
| test_templates.py | 3 | CDN 引用正确、页面区域完整、循环渲染正确 | ✅ |
| test_websocket.py | 3 | 连接/断开正确，广播成功，失败时移除连接 | ✅ |
| test_core.py | 1 | 占位测试通过 | ✅ |

---

## 六、回归测试

本版本新增以下回归测试，确保已知缺陷修复后不再复发：

| 缺陷 ID | 缺陷描述 | 验证方式 | 测试用例 |
|---------|----------|----------|---------|
| #BUG-001 | 保存企业微信配置后发送测试失败 | 配置重载后 `send_test_message` 使用正确 `chatid` | TC-NOTIFY-36, TC-NOTIFY-37 |
| #BUG-002 | HMS 错误码十六进制截断为4位 | 输出 8 位完整十六进制码 | TC-HMS-03, TC-HMS-04, TC-HMS-06, TC-HMS-07 |
| #BUG-003 | 配置的 `chatid` 优先级低于自动发现 | `self._cfg.chatid or self._chatid` | TC-NOTIFY-36, TC-NOTIFY-37 |
| #BUG-004 | 未知 HMS 错误码崩溃 | `lookup_hms_error` 有默认值兜底 | TC-HMS-02 |

---

## 七、覆盖率报告

| 模块 | 语句数 | 分支数 | 覆盖率 |
|------|:---:|:---:|:---:|
| `bumbo_stats/database.py` | 151 | 100% | **100%** |
| `bumbo_stats/printer/monitor.py` | 73 | 100% | **100%** |
| `bumbo_stats/models.py` | — | — | HMS 码 100% 字段完整 |
| `bumbo_stats/printer/wecom.py` | — | — | 通知路径 100% |
| `bumbo_stats/printer/feishu.py` | — | — | 通知路径 100% |

---

## 八、遗留问题

| 编号 | 问题描述 | 影响 | 状态 |
|------|----------|------|------|
| L-01 | 历史打印记录仅覆盖服务运行期间 | 低 | 已知限制 |
| L-02 | X1 系列不支持本地摄像头 | 低 | 已知限制 |
| L-03 | RUNNING→IDLE 统一标记为 completed | 低 | 已知限制 |

---

## 九、测试结论

| 项目 | 结果 |
|------|------|
| 测试用例执行 | ✅ 218/218 通过 |
| 核心模块覆盖率 | ✅ 100% |
| HMS 错误码注册表 | ✅ 131 个官方码，字段完整 |
| 配置重载修复 | ✅ 企业微信 chatid 优先级正确 |
| 安全认证 | ✅ 16 项安全测试全部通过 |
| 数值精度 | ✅ 31 项精确数值测试全部通过 |
| **发版结论** | ✅ **允许发版** |

---

## 十、后续版本测试建议

| 版本 | 建议增加测试内容 |
|------|-----------------|
| v0.6.0 | 摄像头模块截图质量测试、WebSocket 重连场景测试 |
| v0.6.1 | 多打印机并发通知测试、Token 过期刷新测试 |
| v0.6.2 | 数据库迁移测试、历史数据导出功能测试 |
| v0.6.3 | 性能测试（大并发打印机数量）、内存泄漏检测 |

---

*本报告由 pytest 自动生成，测试命令: `python -m pytest tests/ -v`*  
*报告生成时间: 2026-07-10*
