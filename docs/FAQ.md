# 常见问题

| 现象 | 处理 |
|---|---|
| 启动报"配置文件不存在" | 未复制 `printers.yaml.example` 为 `printers.yaml` |
| 面板显示"未连接拓竹云" | 进 设置 → 云端 登录；或 CLI `bambu-monitor login`。账号区域要与 `mqtt_broker` 一致（cn 手机号 / us 邮箱） |
| `curl /api/printers` 返回 401 | 正常：接口已鉴权。先登录拿 Cookie，或看 `/healthz` |
| 状态一直 OFFLINE / 不更新 | 看 `/healthz` 的 `cloud.authenticated` 与 `printers[].mqtt_connected`；确认 Token 未失效、区域正确、`cn.mqtt.bambulab.com:8883` 可达 |
| 日志里找不到 `logs/app.log` | 看启动横幅打印的"日志文件"绝对路径；路径由 `settings.log_dir` / `BAMBU_LOG_DIR` 决定 |
| 摄像头黑屏 / 离线 | 确认打印机与主机同网段、`ip`/`access_code` 正确、固件已开启局域网；RTSPS 机型另确认 ffmpeg 可用（日志有"未找到 ffmpeg"告警） |
| 收不到通知 | 设置页确认通道 `enabled` 与各事件开关；企微检查机器人在群内且出口 IP 在白名单；飞书检查权限、事件订阅方式（长连接）与版本已发布 |
| 飞书点了启用 / 发送测试无效 | 热加载即时生效；若仍失败，看日志是否有凭据/权限错误，再重启一次确认 |
| 群指令没反应 | 确认在群里 **@机器人**；未识别、平台不支持、冷却中的指令都会回话，收到回复即说明链路正常 |
| AI 检测不生效 | 确认已 `pip install ".[notify,ai]"` 且 `ai_guard.enabled: true`；pip 部署在非根目录运行时需配 `ai_guard.deploy_dir` |
| AI 启动报 DINOv2 下载失败 | 网络无法访问 github.com / dl.fbaipublicfiles.com；从 [Releases](https://github.com/WenliLan/bambu-monitor/releases) 下载 `dinov2_cache` 离线包解压到 `bambu_ai_deploy_dinoknn/` |
| AI 启动有 `xFormers` 告警 | 预期降级（回退 MLP FFN），不是错误 |
| 改了 JS/CSS 但页面行为没变 | 静态资源已带版本串；若仍是旧行为，硬刷新（Ctrl+F5）或清除缓存 |
| `pip install` 报 exe 被占用 | 先退出所有 `serve`/`login`；清理 `site-packages/~*` 残留后 `pip install --force-reinstall .` |
| 开机自启后端口不监听、`LastTaskResult=255` | 启动批处理须保持 **CRLF 行尾**（LF 会被 cmd 误解析）；用 `Get-ScheduledTaskInfo` 查看返回码 |
