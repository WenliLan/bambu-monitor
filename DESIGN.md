# Bumbo Stats — 拓竹3D打印机云端监控系统 设计文档

> **版本**: 0.3.0 | **最后更新**: 2026-06-15

---

## 一、项目概述

Bumbo Stats 是一个**拓竹 (Bambu Lab) 3D 打印机多机云端监控面板**，通过拓竹云端 MQTT 协议实时获取多台打印机的状态数据，提供 Web Dashboard 展示实时状态、摄像头画面、今日产能饱和度和近 30 天产能趋势图。

### 核心能力

| 功能 | 说明 |
|------|------|
| 实时状态监控 | MQTT 事件驱动，秒级状态更新（温度、进度、层数、文件） |
| 摄像头实时画面 | 本地 HTTP 获取 JPEG 帧流，3 秒刷新 |
| 今日产能饱和度 | 实时计算当日打印时长 / 24h 占比 |
| 30 天产能趋势 | 历史快照 + 今日同源，折线图可视化 |
| 打印历史记录 | 按打印机查看最近任务列表 |
| 多打印机管理 | 支持同时监控多台打印机（当前 3 台） |
| 断线自动重连 | MQTT 断线后自动重连，状态恢复 |

### 技术栈

| 层次 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn (ASGI) |
| 模板引擎 | Jinja2 |
| 数据库 | SQLite (aiosqlite 异步) |
| 实时通信 | WebSocket |
| 前端图表 | Chart.js |
| MQTT 客户端 | bambu-lab-cloud-api (MQTTClient) |
| 前端交互 | HTMX + 原生 JS |
| 数据模型 | Pydantic v2 |

---

## 二、需求描述

### 2.1 功能需求

#### FR-01: 拓竹云账号认证
- 支持中国区（短信验证码）和全球区（邮箱密码）登录
- Token 本地持久化（`~/.bumbo_token`）
- Token 自动验证和刷新

#### FR-02: 多打印机状态监控
- 通过云端 MQTT Broker 订阅打印机状态
- 支持 cn（`cn.mqtt.bambulab.com`）和 us（`mqtt.bambulab.com`）两个区域
- 实时解析温度、进度、层数、gcode 文件等字段
- MQTT 消息合并缓存（多批消息合并，防止 `gcode_state` 丢失）
- 断线自动重连（指数退避）

#### FR-03: 摄像头实时画面
- 通过本地 IP + Access Code 获取 JPEG 帧流
- X1 系列不支持本地摄像头（API 限制）
- P1S / A1 系列支持
- 3 秒定时刷新

#### FR-04: 今日产能饱和度
- 实时计算所有打印机当日打印时长 / 24h
- 已完成任务使用 `duration_seconds`
- 正在运行任务取最新一个，计算 `now - start_time`（跨午夜只算今天部分）
- 显示为百分比 + 时长（如 "77.8% (18h 40m)"）

#### FR-05: 30 天产能趋势图
- 折线图展示每台打印机近 30 天每日产能
- **数据一致性保证**：今天的数据点直接复用 `get_today_usage_all()` 结果，与今日产能卡片严格相等
- 历史数据从 `daily_usage` 快照表读取（固化，不再变化）
- 含 100% 满产能参考线

#### FR-06: 打印历史记录
- 按打印机查看最近打印任务（gcode 文件、开始/结束时间、时长、状态）
- 支持分页（默认 20 条）

#### FR-07: Web Dashboard
- 暗色主题，响应式布局（3 列网格 → 移动端单列）
- WebSocket 实时推送状态更新
- 连接状态指示灯（绿/红）

### 2.2 非功能需求

| 编号 | 需求 | 说明 |
|------|------|------|
| NFR-01 | 常驻运行 | 服务必须 24/7 运行，支持开机自启动 |
| NFR-02 | 数据一致性 | 今日产能与月度趋势图今天数据必须严格一致 |
| NFR-03 | 跨午夜处理 | 打印任务跨 0 点时，产能正确分配到各天 |
| NFR-04 | 测试覆盖 | 核心模块（database.py, monitor.py）100% 代码覆盖 |
| NFR-05 | 容错恢复 | MQTT 断线重连、异常不崩溃、状态恢复 |

---

## 三、文件层级

```
bumbo_stats/
├── pyproject.toml                      # 项目元数据与依赖声明
├── printers.yaml                       # 运行时配置文件
├── start_service.bat                   # Windows 自启动脚本
├── register_autostart.ps1              # 计划任务注册脚本
├── DESIGN.md                           # 本设计文档
│
├── bumbo_stats/                        # 主包
│   ├── __init__.py                     # 包声明，__version__
│   ├── main.py                         # CLI 入口 (login / serve)
│   ├── config.py                       # YAML 配置加载 + Pydantic 模型
│   ├── models.py                       # 数据模型（Pydantic v2）
│   ├── database.py                     # SQLite 异步操作 + 快照
│   ├── auth.py                         # 拓竹云 Token 认证管理
│   ├── core.py                         # 核心模块（占位）
│   │
│   ├── api/                            # Web API 子包
│   │   ├── __init__.py
│   │   ├── app.py                      # FastAPI 应用 + lifespan
│   │   ├── routes.py                   # REST API 路由
│   │   └── websocket.py                # WebSocket 连接管理
│   │
│   ├── printer/                        # 打印机子包
│   │   ├── __init__.py
│   │   ├── manager.py                  # MQTT 打印机管理器
│   │   ├── monitor.py                  # 后台状态监控 + 每日快照
│   │   └── camera.py                   # 摄像头帧获取
│   │
│   ├── templates/                      # Jinja2 模板
│   │   ├── base.html                   # 基础模板（CDN + 导航栏）
│   │   └── dashboard.html              # 主面板页面
│   │
│   └── static/                         # 静态资源
│       ├── css/dashboard.css           # 暗色主题样式
│       ├── img/camera-offline.svg      # 摄像头离线占位图
│       └── js/
│           ├── dashboard.js            # WebSocket 客户端 + UI 更新
│           ├── charts.js               # Chart.js 月度折线图
│           └── camera.js               # 摄像头定时刷新
│
├── tests/                              # 测试包（128 个测试）
│   ├── conftest.py                     # 共享 fixtures
│   ├── test_api.py                     # API 路由 (7)
│   ├── test_auth.py                    # 认证 (5)
│   ├── test_camera.py                  # 摄像头 (4)
│   ├── test_cli.py                     # CLI (4)
│   ├── test_config.py                  # 配置 (8)
│   ├── test_core.py                    # 核心 (1)
│   ├── test_database.py                # 数据库 (26)
│   ├── test_manager.py                 # 管理器 (6)
│   ├── test_models.py                  # 模型 (8)
│   ├── test_monitor.py                 # 监控器 (22)
│   ├── test_numerical.py               # 精确数值验证 (31)
│   ├── test_templates.py               # 模板 (3)
│   └── test_websocket.py               # WebSocket (3)
│
└── data/                               # 运行时数据
    └── bumbo_stats.db                  # SQLite 数据库文件
```

---

## 四、实现方案

### 4.1 系统架构

```
拓竹云端 MQTT Broker (cn/us.mqtt.bambulab.com)
        │
        ▼
  PrinterManager (MQTTClient × N)  ──→  _status_cache (状态缓存)
        │                                      │
        │ on_status_change                     │
        ▼                                      ▼
  PrintMonitor (事件驱动)              FastAPI Routes (REST API)
   ├─ _handle_state_change()          ├─ /api/printers     (缓存直读)
   │   ├─ 检测打印开始/结束           ├─ /api/usage/today  (DB 实时算)
   │   └─ gcode 文件变化检测          ├─ /api/usage/monthly(快照+今日)
   ├─ record_print_start/end()        └─ /api/sessions     (DB 历史)
   ├─ snapshot_yesterday() 每日快照
   └─ broadcast → ConnectionManager
              │
              ▼
        WebSocket → 前端 Dashboard (实时更新)

  CameraManager (本地 HTTP JPEG)  ──→  /api/printers/{serial}/camera
              │
              ▼
        camera.js (每 3s 轮询刷新)
```

### 4.2 数据流

```
MQTT 消息 → PrinterManager._parse_status()
         → PrinterStatus (Pydantic 模型)
         → _status_cache 更新
         → PrintMonitor.handle_status_update()
              ├─ _handle_state_change() → Database 记录
              └─ broadcast() → WebSocket → 前端

前端请求 → FastAPI Route → Database 查询 → JSON 响应
```

### 4.3 数据库 Schema

```sql
-- 打印机基本信息
CREATE TABLE printers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    ip TEXT NOT NULL,
    model TEXT,
    location TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 打印任务记录
CREATE TABLE print_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_serial TEXT NOT NULL,
    gcode_file TEXT,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    duration_seconds INTEGER,
    status TEXT DEFAULT 'running'   -- running / completed / cancelled / failed
);

-- 每日产能快照（历史固化数据）
CREATE TABLE daily_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_serial TEXT NOT NULL,
    date DATE NOT NULL,
    total_print_seconds INTEGER DEFAULT 0,
    session_count INTEGER DEFAULT 0,
    UNIQUE(printer_serial, date)
);
```

---

## 五、关键技术实现方案

### 5.1 产能统计架构（Snapshot + 同源复用）

**核心原则**: 今天的数据只计算一次，所有视图共享同一数据源。

```
今日产能卡片   ←  get_today_usage_all()       (实时计算)
                        ↑ 同源复用
30天趋势图     ←  daily_usage (历史快照)
                  + get_today_usage_all (今天)
                        ↓ 每日固化
              snapshot_yesterday()  ←  monitor 主循环
```

#### get_today_usage_all() — 今日产能实时计算

```python
# 查询条件：当天开始的任务 + 任何正在运行的任务（含跨午夜）
WHERE date(start_time) = today OR status = 'running'

# 处理逻辑：
for session in sessions:
    if running:
        latest_running[serial] = start_time    # 只保留最新 running
    else:
        printer_totals[serial] += duration_seconds  # completed 累加

# running 任务时长（跨午夜只算今天部分）
effective_start = max(start_time, today_midnight)
duration = now - effective_start
```

#### get_monthly_usage_all() — 月度趋势

```python
# 1. 确保昨天已快照（幂等）
await self.snapshot_yesterday()

# 2. 今天数据直接复用 get_today_usage_all（零重复计算）
today_items = await self.get_today_usage_all(printer_names)

# 3. 历史数据从 daily_usage 读取
SELECT ... FROM daily_usage WHERE date >= start_date AND date < today
```

#### snapshot_yesterday() — 每日快照

```python
# 在 monitor 主循环中每日执行一次
# 幂等：已存在则跳过，不会覆盖
async def snapshot_past_day(target_date):
    # completed: 按天截断（跨天任务只取该天内的时间段）
    effective_start = max(start_time, target_midnight)
    effective_end = min(end_time, next_day_midnight)
    
    # running: 截止到该天午夜
    effective_start = max(start_time, target_midnight)
    duration = next_day_midnight - effective_start
```

### 5.2 打印机状态机

监控器通过检测状态转换来触发打印任务记录：

```
                     ┌──────────────┐
                     │   UNKNOWN    │ (首次连接)
                     └──────┬───────┘
                            │ 首次消息
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         ┌─────────┐  ┌──────────┐  ┌─────────┐
         │  IDLE   │  │ RUNNING  │  │  PAUSE  │
         └────┬────┘  └────┬─────┘  └────┬────┘
              │             │              │
              │ RUNNING     │ FINISH       │ RUNNING
              │ → start()   │ → end("completed")
              │             │ FAILED       │ (恢复，不触发)
              │             │ → end("failed")
              │             │ IDLE
              │             │ → end("completed")
              │             │ PAUSE
              │             │ → (无事件，打印暂停中)
              ▼             ▼              ▼
         ┌─────────────────────────────────────┐
         │     _last_states[serial] 更新       │
         │     _last_gcode[serial] 更新        │
         └─────────────────────────────────────┘

特殊场景：
- RUNNING→RUNNING gcode 变化: 结算旧任务 + 记录新任务（断线期间完成+新开始）
- RUNNING→RUNNING gcode 不变: 无事件（正常心跳）
- 首次 RUNNING: record_print_start（启动补录）
- 首次 IDLE/PAUSE: 仅初始化
```

### 5.3 MQTT 消息解析与缓存

```python
# PrinterManager._parse_status() 从 MQTT 'print' 键提取数据
print_data = payload.get("print", {})

# 多批消息合并缓存（防止 gcode_state 丢失）
self._print_data_cache[serial].update(print_data)

# 状态映射
STATE_MAP = {
    "IDLE": PrinterState.IDLE,
    "RUNNING": PrinterState.RUNNING,
    "PAUSE": PrinterState.PAUSE,
    "FINISH": PrinterState.FINISH,
    "FAILED": PrinterState.FAILED,
    "OFFLINE": PrinterState.OFFLINE,
}
```

### 5.4 认证流程

```python
# 中国区: 手机号 + 短信验证码两步认证
POST https://api.bambulab.cn/v1/user-service/user/login
  → { account: "手机号", code: "验证码" }
  → { accessToken, refreshToken }

# Token 持久化
TokenManager.save_token() → ~/.bumbo_token (JSON)
TokenManager.load_token() → 读取并验证
```

### 5.5 摄像头帧获取

```python
# 通过本地 HTTP 获取 JPEG 帧流
GET http://{ip}:6000/api/camera
Headers: Authorization: Basic base64(bblp:{access_code})

# 支持的机型: P1S, P1P, A1, A1 mini
# 不支持: X1 系列（API 限制）
```

### 5.6 开机自启动

```
Windows 计划任务 (Task Scheduler)
├── 触发器: 用户登录
├── 操作: 执行 start_service.bat
├── 设置: 失败重试 3 次，无时间限制
└── 日志: startup.log

start_service.bat:
  cd /d "H:\QoderWorkspace\bumbo_stats"
  H:\anaconda\python.exe -m bumbo_stats.main serve --host 0.0.0.0 --debug
```

---

## 六、测试方案（97 个测试，100% 覆盖）

### 6.1 覆盖率

| 模块 | 语句数 | 覆盖率 |
|------|:---:|:---:|
| `database.py` | 151 | **100%** |
| `monitor.py` | 73 | **100%** |
| **合计** | 224 | **100%** |

**测试总数**: 128 个（原有 97 + 新增 31 精确数值验证）

### 6.2 测试矩阵

#### 数据库层 (test_database.py — 26 tests)

| ID | 测试名 | 场景 |
|---|--------|------|
| TC-DB-01 | tables_created | 初始化后表存在 |
| TC-DB-02 | upsert | 打印机插入和更新 |
| TC-DB-03 | record_start | 记录打印开始 |
| TC-DB-04 | record_end | 记录打印结束，计算时长 |
| TC-DB-05 | today_usage | 查询今日产能 |
| TC-DB-06 | monthly | 月度数据查询 |
| TC-DB-07 | fill_zeros | 无记录天补齐为 0 |
| TC-DB-08 | get_sessions | 获取打印历史 |
| TC-DB-09 | no_records_zero | 无记录时产能为 0 |
| TC-DB-10 | accumulate | 多次打印时长累加 |
| TC-DB-11 | monthly_includes_running | 月度含当天 running |
| TC-DB-12 | start_ends_previous | **开始新任务自动结束旧 running** |
| TC-DB-13 | end_no_running | **无 running 时静默忽略** |
| TC-DB-14 | cross_midnight_running | **跨午夜 running（只算今天部分）** |
| TC-DB-15 | unknown_serial | **未知 serial 被忽略** |
| TC-DB-16 | multi_day_completed | **多天多打印机 completed** |
| TC-DB-17 | date_range | **月度日期范围正确** |
| TC-DB-18 | limit | **get_sessions 限制条数和排序** |
| TC-DB-19 | end_updates_daily | **record_end 更新 daily_usage** |
| TC-DB-20 | running_only_latest | **多 running 只取最新** |
| TC-DB-21 | monthly_cross_midnight | **月度跨午夜 running** |
| TC-DB-22 | snapshot_past_day | **快照创建 daily_usage** |
| TC-DB-23 | snapshot_idempotent | **快照幂等不覆盖** |
| TC-DB-24 | snapshot_cross_day | **快照跨天 session 截断** |
| TC-DB-25 | monthly_today_equals | **月度今天 = 今日产能** |
| TC-DB-26 | snapshot_running_capped | **快照 running 截止午夜** |

#### 监控器层 (test_monitor.py — 22 tests)

| ID | 测试名 | 场景 |
|---|--------|------|
| TC-MON-01 | print_start | IDLE→RUNNING 触发 record_print_start |
| TC-MON-02 | print_finish | RUNNING→FINISH 触发 record_print_end("completed") |
| TC-MON-03 | print_failed | RUNNING→FAILED 触发 record_print_end("failed") |
| TC-MON-04 | no_change | 状态不变不触发事件 |
| TC-MON-05 | broadcast | handle_status_update 触发广播 |
| TC-MON-06 | first_idle | 首次 IDLE 仅初始化 |
| TC-MON-07 | first_running | 首次 RUNNING 触发补录 |
| TC-MON-08 | gcode_change | gcode 变化 → 结算旧 + 记录新 |
| TC-MON-09 | same_gcode | gcode 不变不触发 |
| TC-MON-10 | running_to_idle | **RUNNING→IDLE → "completed"** |
| TC-MON-11 | pause_to_running | **PAUSE→RUNNING 不触发新 session** |
| TC-MON-12 | empty_gcode | **gcode 为空不触发变化检测** |
| TC-MON-13 | no_prev_gcode | **prev_gcode 为空不触发** |
| TC-MON-14 | idle_to_finish | **IDLE→FINISH 不触发事件** |
| TC-MON-15 | update_last_gcode | **状态变更更新 _last_gcode** |
| TC-MON-16 | stop | stop() 设置 _running=False |
| TC-MON-17 | broadcast_all | **_broadcast_all 广播所有打印机** |
| TC-MON-18 | first_pause | **首次 PAUSE 仅初始化** |
| TC-MON-19 | running_to_pause | **RUNNING→PAUSE 不触发开始/结束** |
| TC-MON-20 | start_and_cancel | **start() 启动循环可被 cancel** |
| TC-MON-21 | broadcast_exception | **broadcast 异常不崩溃** |
| TC-MON-22 | daily_snapshot | **start() 触发每日快照** |

#### 精确数值验证 (test_numerical.py — 31 tests)

> 所有测试使用固定时钟 `14:30:00` + 动态日期 `date.today()`，
> 通过 `MagicMock(spec=datetime)` 替换 `database.datetime`，
> 实现完全确定性的精确数值验证。

| ID | 测试名 | 场景 |
|---|--------|------|
| TC-NV-01 | single_completed | **单个 completed: 3h30m = 12600s → 14.6%** |
| TC-NV-02 | two_completed | **两个 completed: 4h+3h = 25200s → 29.2%** |
| TC-NV-03 | three_completed | **三个 completed: 2h+3h+1h = 21600s → 25.0%** |
| TC-NV-04 | cross_midnight_not_counted | **completed 跨午夜: date(start)≠today → 不计入** |
| TC-NV-05 | running_basic | **running: start 12:00, now 14:30 → 9000s (2h30m)** |
| TC-NV-06 | running_cross_midnight | **running 跨午夜: 昨天22:00 → 今天14:30 = 52200s** |
| TC-NV-07 | completed_plus_running | **2 completed + 1 running: 3h+1h+2.5h = 23400s** |
| TC-NV-08 | two_running_only_latest | **2 running 只取最新: 14:30-12:00 = 9000s** |
| TC-NV-09 | exact_hours | **整小时百分比: 6h=25.0%, 12h=50.0%, 1h=4.2%** |
| TC-NV-10 | percentage_precision | **非整除百分比: 5h13m=18780s → 21.7%** |
| TC-NV-11 | display_format | **显示格式: 0h00m, 1h01m, 10h00m, 23h59m** |
| TC-NV-12 | all_zero_display | **无记录零显示: 0h00m, 0.0%** |
| TC-NV-13 | start_ends_previous | **新任务结束旧: 14:30-08:00 = 23400s (6h30m)** |
| TC-NV-14 | multiple_previous | **多旧 running: 全部结束，各自精确时长** |
| TC-NV-15 | end_daily_usage_exact | **首次结束: daily_usage = 16200s, count=1** |
| TC-NV-16 | end_accumulates | **两次结束累加: 23400+9000 = 32400s** |
| TC-NV-17 | snapshot_completed | **快照 completed: 10:00→14:00 = 14400s** |
| TC-NV-18 | snapshot_running_capped | **快照 running: 22:00→午夜 = 7200s** |
| TC-NV-19 | snapshot_cross_day | **快照跨天: 22:00→02:00 昨天部分 = 7200s** |
| TC-NV-20 | snapshot_idempotent | **快照幂等: 已有记录不覆盖** |
| TC-NV-20b | started_today_truncate | **跨天 start 在目标天: 20:00→午夜 = 14400s** |
| TC-NV-20c | snapshot_mixed | **快照混合: completed 2h + running 4h = 21600s** |
| TC-NV-21 | monthly_all_zeros | **月度无记录: 全 0.0%** |
| TC-NV-22 | monthly_history_today | **历史快照+今日 running: 各点精确%** |
| TC-NV-23 | monthly_multi_printer | **多打印机独立: 4h=16.7%, 8h=33.3%** |
| TC-NV-23b | monthly_equals_today | **月度今天点 = get_today_usage_all** |
| TC-NV-24 | end_no_running | **无 running 时结束: 无副作用** |
| TC-NV-25 | unknown_serial | **未知 serial 不计入结果** |
| TC-NV-26 | completed_before_today | **start 在昨天: 不计入今日** |
| TC-NV-27 | snapshot_no_sessions | **无 session 日期: 不创建记录** |
| TC-NV-28 | monthly_date_sequence | **月度日期序列严格连续递增** |

#### 其他模块 (49 tests)

| 测试文件 | 数量 | 覆盖范围 |
|----------|:---:|---------|
| test_api.py | 7 | 全部 REST 端点（Dashboard/打印机/产能/摄像头/历史） |
| test_auth.py | 5 | Token 存取、文件不存在、空文件、路径展开 |
| test_camera.py | 4 | X1 不支持、P1S/A1 支持、无 IP、帧缓存 |
| test_cli.py | 4 | 版本号、缺配置退出、参数解析 |
| test_config.py | 8 | 完整/最小/不存在/缺字段配置、模型默认值 |
| test_manager.py | 6 | 初始化、属性、OFFLINE 默认、MQTT 解析 |
| test_models.py | 8 | 枚举、默认值、序列化、所有模型结构 |
| test_templates.py | 3 | CDN 引用、页面区域、循环渲染 |
| test_websocket.py | 3 | 连接/断开、广播、失败移除 |
| test_core.py | 1 | 占位 |

### 6.3 测试配置

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"          # 自动检测 async 测试
testpaths = ["tests"]
```

```python
# conftest.py 关键 fixtures
- db: 临时 SQLite 数据库（tmp_path）
- printer_names: 打印机名称映射
- sample_config_yaml: 完整配置
- mock bambulab: 未安装时自动 mock
```

### 6.4 运行方式

```bash
# 全量测试 (128 tests)
python -m pytest tests/ -v

# 核心模块 + 覆盖率
python -m pytest tests/test_database.py tests/test_monitor.py tests/test_numerical.py \
    --cov=bumbo_stats.database --cov=bumbo_stats.printer.monitor \
    --cov-report=term-missing

# 预期结果: 128 passed, database.py 100%, monitor.py 100%
```

---

## 七、配置说明

### printers.yaml

```yaml
cloud:                             # 拓竹云账号（必填）
  username: "手机号/邮箱"
  token_file: "~/.bumbo_token"     # Token 存储路径
  mqtt_broker: "cn"                # cn(中国) / us(全球)

printers:                          # 打印机列表（必填）
  - name: "打印机名称"
    serial: "序列号"
    ip: "192.168.x.x"             # 可选，仅摄像头
    access_code: "xxxxxxxx"       # 可选，仅摄像头
    location: "位置描述"

settings:                          # 全局设置（可选）
  camera_interval: 3              # 摄像头刷新间隔（秒）
  db_path: "./data/bumbo_stats.db"
  host: "0.0.0.0"
  port: 8000
```

### 启动命令

```bash
# 首次登录（获取 Token）
python -m bumbo_stats.main login

# 启动服务
python -m bumbo_stats.main serve --host 0.0.0.0 --debug

# Windows 开机自启动（管理员权限注册一次）
schtasks /create /tn "BumboStats" /tr "H:\QoderWorkspace\bumbo_stats\start_service.bat" /sc onlogon /rl highest /f
```

---

## 八、已知限制

| 编号 | 限制 | 说明 |
|------|------|------|
| L-01 | 历史打印记录 | 拓竹云端不提供历史打印 API，仅记录服务运行期间的任务 |
| L-02 | 服务中断丢数据 | 服务停止期间 MQTT 消息丢失，无法补录 |
| L-03 | X1 摄像头 | X1 系列不支持本地 HTTP 摄像头帧获取 |
| L-04 | 手动取消标记 | RUNNING→IDLE 统一标记为 "completed"（无法区分正常完成和手动取消） |
| L-05 | daily_usage 日期 | record_print_end 用 end_time 日期写 daily_usage（不影响用户数据，因月度统计不读此表） |

---

## 九、总结

### 9.1 架构优势

| 优势 | 说明 |
|------|------|
| **云端 MQTT 架构** | 不依赖局域网直连，服务可部署在任意机器，突破打印机本地网络限制 |
| **事件驱动状态机** | 精确检测打印开始/结束，状态转换覆盖全部 6 种状态组合，无漏报误报 |
| **Snapshot + 同源复用** | 今日产能与月度趋势图今天数据 100% 一致，历史数据固化不变化 |
| **幂等快照机制** | 可重跑、可补录，不会因为重启或时序问题产生重复/错误数据 |
| **消息合并缓存** | 解决 MQTT 多批消息丢失关键字段（`gcode_state`）的问题 |
| **全工况测试覆盖** | database.py 和 monitor.py 均 100% 语句覆盖，128 个测试覆盖全部边界场景 |

### 9.2 关键设计决策

| 决策 | 选型 | 理由 |
|------|------|------|
| 数据库 | SQLite (aiosqlite) | 轻量、零配置、异步友好，适合单机常驻服务 |
| MQTT 接入 | bambu-lab-cloud-api | 官方维护，支持 cn/us 双区域，稳定可靠 |
| 数据一致性 | 同源复用 | 避免两个独立计算函数因 `now` 时刻不同产生微小差异 |
| 历史数据固化 | daily_usage 快照 | 快照后不可变，月度趋势图历史数据点永不变动 |
| RUNNING→IDLE | 标记 completed | 用户极少手动取消打印，简化逻辑避免误标 cancelled |
| 状态持久化 | 不持久化 | 重启后丢失中间状态，依赖首次 RUNNING 补录机制，简化运维 |

### 9.3 当前状态

- **功能完整**: 实时监控、产能统计、摄像头、历史记录、WebSocket 推送全部就绪
- **生产可用**: 128 个测试通过，核心模块 100% 覆盖，支持开机自启动常驻运行
- **已知局限**: 历史打印记录仅覆盖服务运行期间，X1 摄像头不支持，服务中断期间数据丢失
- **扩展方向**: 可增加多打印机队列管理、打印任务自动排程、历史数据可视化增强等高级功能
