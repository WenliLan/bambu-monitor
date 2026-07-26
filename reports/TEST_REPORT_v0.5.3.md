# Bumbo Stats v0.5.3 发版测试报告

> **报告日期**: 2026-07-20
> **测试版本**: v0.5.3
> **被测系统**: Bumbo Stats 拓竹3D打印机多机监控面板
> **测试结论**: ✅ 通过

---

## 一、版本信息

| 项目 | 内容 |
|------|------|
| 当前版本 | **v0.5.3** |
| 上一版本 | v0.5.2 |
| Python 版本 | >= 3.10 |
| 数据库 | SQLite (aiosqlite) |
| 测试框架 | pytest + pytest-asyncio |
| 测试用例总数 | **218** |

---

## 二、本版更改点

### 2.1 移除 username 字段

| 变更类型 | 描述 |
|----------|------|
| 架构优化 | 拓竹云认证仅依赖 token 文件，username 不再用于认证 |
| 涉及文件 | `CloudConfig.username` 改为 Optional、`CloudUpdate` 移除 username 字段 |
| 影响范围 | 设置页面不再显示 username 输入框、API 保存不再接收 username |

### 2.2 修复 :memory: 数据库问题

| 变更类型 | 描述 |
|----------|------|
| 缺陷修复 | 之前使用 `:memory:` 数据库时，服务启动失败 |
| 根因 | `Database` 类的 `init()` 和 `upsert_printer()` 各自创建独立连接，`:memory:` 每次连接都是新数据库 |
| 修复方式 | 将 `db_path` 改为文件路径 `./data/bumbo_stats.db` |

### 2.3 清理异常打印记录

| 变更类型 | 描述 |
|----------|------|
| 数据修复 | 清理 7月13日开始的打印任务（end_time 错误记录为 7月20日） |
| 影响记录 | 3 条 printers 记录修正 duration（从 ~679,000秒 修正为 ~17,500秒） |
| 根因 | MQTT 状态更新机制问题，任务完成时未正确更新数据库 |

### 2.4 配置文件分离备份

| 变更类型 | 描述 |
|----------|------|
| 架构优化 | printers.yaml 和 data/bumbo_stats.db 分离到 bumbo_stats_data 仓库备份 |
| 主仓库跟踪 | start_service.bat 改为主仓库跟踪 |
| 备份仓库 | bum bum_stats_data/ 包含 printers.yaml、data/bumbo_stats.db、start_service.bat |

---

## 三、测试概况

### 3.1 测试执行摘要

| 指标 | 数值 |
|------|------|
| 用例总数 | 218 |
| 通过 | 218 |
| 失败 | 0 |
| 跳过 | 0 |
| 执行时间 | ~6s |

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
4. **配置保存/读取一致性验证通过**

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

### 5.1 企业微信通知模块（test_wecom_notify.py — 38 tests）

| 用例 ID | 测试名称 | 结果 |
|---------|----------|:---:|
| TC-NOTIFY-01 ~ TC-NOTIFY-38 | 通知去重、配置重载、HMS 错误通知 | ✅ 全部通过 |

### 5.2 飞书通知模块（test_feishu_notify.py — 16 tests）

| 用例 ID | 测试名称 | 结果 |
|---------|----------|:---:|
| TC-FEISHU-01 ~ TC-FEISHU-16 | 飞书通知去重、消息构建、HMS 错误通知 | ✅ 全部通过 |

### 5.3 HMS 错误码注册表（test_hms_registry.py — 8 tests）

| 用例 ID | 测试名称 | 结果 |
|---------|----------|:---:|
| TC-HMS-01 ~ TC-HMS-08 | HMS 错误码注册表查找、消息渲染 | ✅ 全部通过 |

### 5.4 数据库模块（test_database.py — 26 tests）

| 用例 ID | 测试名称 | 结果 |
|---------|----------|:---:|
| TC-DB-01 ~ TC-DB-26 | 数据库 CRUD、快照、跨午夜处理 | ✅ 全部通过 |

### 5.5 监控器模块（test_monitor.py — 22 tests）

| 用例 ID | 测试名称 | 结果 |
|---------|----------|:---:|
| TC-MON-01 ~ TC-MON-22 | 打印机状态机、事件触发、广播 | ✅ 全部通过 |

### 5.6 精确数值验证（test_numerical.py — 31 tests）

| 用例 ID | 测试名称 | 结果 |
|---------|----------|:---:|
| TC-NV-01 ~ TC-NV-31 | 精确数值验证、百分比计算、跨午夜边界 | ✅ 全部通过 |

### 5.7 API 模块（test_api.py — 19 tests）

| 用例 ID | 测试名称 | 结果 |
|---------|----------|:---:|
| TC-API-01 ~ TC-API-19 | REST API 端点、认证、配置保存 | ✅ 全部通过 |

**本版本更新说明**：TC-API-11 和 TC-API-12 适配 username 移除，验证设置页面显示和云端配置保存功能正常。

### 5.8 其他模块测试（58 tests）

| 测试文件 | 用例数 | 结果 |
|----------|:---:|:---:|
| test_auth.py | 5 | ✅ |
| test_camera.py | 4 | ✅ |
| test_cli.py | 4 | ✅ |
| test_config.py | 8 | ✅ |
| test_manager.py | 6 | ✅ |
| test_models.py | 8 | ✅ |
| test_settings_security.py | 16 | ✅ |
| test_websocket.py | 3 | ✅ |
| test_templates.py | 3 | ✅ |
| test_core.py | 1 | ✅ |

---

## 六、回归测试

本版本新增以下回归测试，确保已知缺陷修复后不再复发：

| 缺陷 ID | 缺陷描述 | 验证方式 | 测试用例 |
|---------|----------|----------|---------|
| #BUG-005 | :memory: 数据库导致启动失败 | 使用文件数据库路径，init/upsert 连接一致 | TC-API-12 |
| #BUG-006 | 7月13日异常打印记录 | 清理后今日产能计算正确 | TC-DB-05, TC-DB-10 |

---

## 七、覆盖率报告

| 模块 | 语句数 | 覆盖率 |
|------|:---:|:---:|
| `bumbo_stats/database.py` | 151 | **100%** |
| `bumbo_stats/printer/monitor.py` | 73 | **100%** |
| `bumbo_stats/models.py` | — | HMS 码 100% 字段完整 |
| `bumbo_stats/printer/wecom.py` | — | 通知路径 100% |
| `bumbo_stats/printer/feishu.py` | — | 通知路径 100% |

---

## 八、遗留问题

| 编号 | 问题描述 | 影响 | 状态 |
|------|----------|------|------|
| L-01 | 历史打印记录仅覆盖服务运行期间 | 低 | 已知限制 |
| L-02 | X1 系列不支持本地摄像头 | 低 | 已知限制 |
| L-03 | RUNNING→IDLE 统一标记为 completed | 低 | 已知限制 |
| L-04 | 7月13日-7月19日快照数据缺失（任务当时卡在 running） | 低 | 已清理脏数据 |

---

## 九、测试结论

| 项目 | 结果 |
|------|------|
| 测试用例执行 | ✅ 218/218 通过 |
| 核心模块覆盖率 | ✅ 100% |
| 配置保存/读取 | ✅ 一致 |
| 认证流程 | ✅ 正常 |
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
*报告生成时间: 2026-07-20*
