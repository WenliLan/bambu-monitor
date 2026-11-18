bambu_monitor v{VERSION} 正式发布包
================================

(本文件由 build_release.ps1 在打包时注入版本号，模板在源码仓库 RELEASE_NOTES.md)

【本版主题：AI 异常检测图库升级到 1256 条（准确率 78.7%→92.7%）；合并 PR#1 的空闲占用与产能统计修复】

〇、v0.6.1 变更

  · AI 检测图库（bambu_ai_deploy_dinoknn/gallery/）换代：出厂库由 265 条（仅 local_data 全量）
    换成 1256 条（去重后的 benchmark/train + local_data，720 anomaly + 536 normal），
    k=5 与判定阈值 0.7 不变。同一 benchmark 测试口径实测：
      准确率 78.66% → 92.69%   正常画面误报率 30.5% → 4.2%   AUC 0.815 → 0.971
    clone 后无需重新建库，直接可用。*_paths.json 改为相对 baselines 的来源标识（图片本身仍不分发）。

  · 阈值建议重测并纠正一处误导：0.8 相对 0.7 **不额外降噪**（误报同为 3/71 张），
    只是多丢 2.6 个百分点召回；确实需要接近零误报请升到 0.9（1/71）。
    完整阈值-召回-误报表见 bambu_ai_deploy_dinoknn/README.md「自定义阈值」。

  · AI 图库增量入库（合并 PR#1）：设置页可上传/扫描新图直接入库，入库加锁防并发、
    可中止、批量只落盘一次；图库写盘改为临时文件 + os.replace 原子替换，
    中途崩溃不再留下半截文件导致整库读不回来。检测历史改用定长队列，长期运行不再无界增长。

  · 空闲资源占用（合并 PR#1）：摄像头按需抓帧 + 状态广播去重 + 高频日志降级；
    摄像头暂停/恢复日志只在首次以 INFO 说明，后续降为 DEBUG；
    torch 推理线程限到 2 核，避免抢占切片与远程桌面的 CPU。

  · 产能统计修复（合并 PR#1）：打印时长按区间并集计算并封顶，修复饱和度 >100% / 0% 的假信号；
    启动补录孤儿任务改用云端 status=4 的真实开始时间，不再按本地心跳推算。

  · 安全（合并 PR#1）：飞书 access_key 不再出现在日志中（改由 SDK 构造参数控制日志级别）。

【以下为 v0.6.0 内容】

零、更名与发布（v0.6.0）
  · 项目由 bumbo_stats 更名为 bambu-monitor：Python 包目录、setuptools 包名、
    控制台命令、代码注释与全部文档同步统一。
  · 破坏性变更（升级必读，详见文末【升级注意】）：import 路径、CLI 命令、
    环境变量前缀、Token 与数据库文件名、Web 登录 Cookie 名均已改变。
  · DINOv2 ViT-S/14 骨干网（约 84MB，Meta CC-BY-NC 4.0 非商用许可）不再入仓库：
    本地缓存齐备时零网络加载，缺失时自动回退 torch.hub 在线下载；
    网络受限可从 Releases 下载 dinov2_cache 离线包解压到 bambu_ai_deploy_dinoknn/。
    本项目自训的 kNN 模型资产 gallery/ 始终随仓库分发，clone 即可用。
  · 修复：骨干网缓存缺失时 load_model() 直接抛 FileNotFoundError，而提示文案
    声称"会从 torch.hub 拉取"却无对应实现 —— 补上真正的在线回退分支并加回归测试。
  · 仓库历史清理：剔除模型缓存与旧研究方案权重，体积由 329MB 降至约 1MB。

【以下为 v0.5.18 基线内容】

一、致命问题（P0）
  · 云端 MQTT 不再复用打印机本体身份：监控端改用独立唯一 client_id
    (bambu-monitor-<serial>-<随机后缀>)，并等待 CONNACK 后再请求状态，
    彻底消除"服务看似启动成功、状态永不更新"的顶号掉线问题。

二、流程与假信号（P1）
  · 无 Token 也能启动：服务正常监听，网页「设置 → 云端」内完成拓竹云登录
    (账号密码 → 短信/邮件验证码)，登录后热连接；CLI login 仍可用但不再是前置条件。
  · 启动首条 MQTT 误报 ERROR 已消除：连接就绪前不再发布，失败分级为 WARNING。
  · 自检判据改为代码中真实存在的日志（AI 启动行打印 DINOv2+kNN(k) 与图库数量），
    xFormers 缺失告警明确为 CPU 预期降级；AI 检测器改为后台加载，不再阻塞启动。
  · 日志目录统一由 settings.log_dir（默认 ./logs，相对启动目录）决定，
    支持 BAMBU_LOG_DIR；启动横幅打印配置文件与日志的绝对路径。

三、依赖与配置（P2）
  · 移除第三方 bambu-lab-cloud-api：自研云端 REST / MQTT / JPEG 客户端，
    核心依赖仅 requests + paho-mqtt，不再被 Flask/OpenCV/flask-limiter 拖累。
  · 保存设置改为按段读-改-写，保留其他段落与未知键，并生成 printers.yaml.bak；
    配置模板明确"手改前先停 serve，增删打印机优先用网页"。
  · 删除无效配置项 ai_guard.model / ai_guard.track（从未传给检测器）；
    旧 YAML 中的残留键会被安全忽略。
  · AI 出厂默认决策阈值由 0.5 调整为 0.7，降低正常打印的单帧误报；
    阈值越高漏报风险相应增加，可在 Web 设置页或群指令随时调整。
  · 登录提示按区域区分：cn 手机号 + 短信，us 邮箱 + 邮件；验证码发送失败会原样报错。
  · 飞书可在面板内即时启用/停用；「🔍 获取群聊」直接列出并回填 chatid；
    加急 urgent_* 三项进入表单，旧表单保存不再清零。

四、安全（按 P1 处理）
  · 默认监听 127.0.0.1；改为 0.0.0.0 时横幅明确告警。
  · 除 /api/auth/* 外，所有 /api/* 数据接口要求登录；/healthz 与 /login 为公开端点。
  · 登录 Cookie 改为进程内随机 session token（HttpOnly，7 天过期），
    不再下发口令哈希。

五、前端与可观测性（P2/P3）
  · 提示音判定移到数据层：仅 RUNNING → {PAUSE, FINISH, FAILED, OFFLINE} 响一次；
    页面新增未知打印机时显示"刷新页面"横幅。
  · 摄像头网格列数单一来源（CSS 只消费 --camera-cols，窄屏由 JS 降级）。
  · 静态资源引用带版本串，升级后不再出现"新 HTML + 旧 JS"的假故障。
  · 移除未使用且 SRI 校验必然失败的 htmx 引用。
  · 重启后首个残缺状态包不再显示 0/0 · 0%，沿用上一份有效进度。
  · 启动批处理改为纯 ASCII；.gitattributes 强制 *.bat/*.ps1 使用 CRLF。

【包内容】
  bambu_monitor/               主程序(FastAPI + 自研 MQTT + SQLite + Web 面板)
  bambu_ai_deploy_dinoknn/   AI 缺陷检测器(含 DINOv2 权重 + 预训练特征库 gallery/)
                             scripts/ 内提供 detect_image / init_gallery / monitor
                             watcher.py 可热追加用户照片到特征库
  requirements-ai.txt        AI 检测运行时依赖清单
  printers.yaml.example      配置模板(全字段带中文注释)
  README.md                  使用说明(安装/网页登录/通知/群指令/AI/FAQ)
  LICENSE                    MIT 许可证

【快速上手（四步）】
  cd <发布包根目录>
  $env:PYTHONUTF8 = "1"                      # 仅中文 Windows 需要
  pip install ".[notify]"                    # 要 AI 检测改 ".[notify,ai]"
  copy printers.yaml.example printers.yaml   # 填写 serial(15-18 位标签原值) 等
  bambu-monitor serve                          # 打开 http://127.0.0.1:8000

  浏览器内：设置管理员密码 → 设置 → 云端登录(回填验证码) → 设置 → 打印机增删。
  自检：GET /healthz 返回 200；日志每 30 秒一条「状态同步 ... 层 x/y 进度 z%」。

【环境要求】
  Python 3.10+（3.11/3.12 已验证）; Windows/Linux/macOS 均可; 无需 Anaconda/GPU。
  安装 [ai] 需 VC++ 2015-2022 x64 ≥ 14.36（Windows）;
  中文 Windows 建议先设 PYTHONUTF8=1; 安装/重装前先退出所有 serve/login 进程。
  需可访问局域网内打印机(摄像头 JPEG 6000 / RTSPS 322 端口)与拓竹云端。

【机型摄像头支持(v0.5.17 起全机型)】
  状态监控/产能/HMS 报警: 所有联网拓竹机型开箱即用(云端 MQTT 统一协议)。
  摄像头/AI 检测: 按端口自动分流两种协议——
    A1/A1 mini/P1P/P1S  → TCP 6000 JPEG, 开箱即用;
    X1 全系/P2S/P2P/X2D/H2D/H2S → RTSPS 322, 需 ffmpeg(三选一):
      系统安装 ffmpeg 加入 PATH / 设环境变量 BAMBU_FFMPEG_EXE / pip install ".[rtsp]"。

【升级注意】
  · 更名迁移（v0.6.0，均为破坏性）：
      - 启动命令 python -m bumbo_stats.main → python -m bambu_monitor.main，
        控制台脚本 bumbo-stats → bambu-monitor
      - 环境变量前缀 BUMBO_* → BAMBU_*（含 TOKEN_FILE / WECOM_* / FEISHU_* /
        DASHBOARD_URL / LOG_DIR / AI_DEPLOY_DIR / FFMPEG_EXE）
      - Token 文件 ~/.bumbo_token → ~/.bambu_monitor_token
      - 数据库 ./data/bumbo_stats.db → ./data/bambu_monitor.db
        （两者均为改名不改动内容；若沿用旧文件名请在 printers.yaml 显式指定路径）
      - 登录 Cookie 改名，升级后需重新登录一次
      - 前端卡片布局的 localStorage 键改名，旧自定义排序会重置一次
    外部集成若曾解析上述名称（计划任务、监控脚本、反向代理规则），需同步调整。
  · 默认监听地址由 0.0.0.0 改为 127.0.0.1；需局域网访问请在 printers.yaml
    把 settings.host 改为 0.0.0.0（并确保已设管理员密码）。
  · 旧 printers.yaml 可直接沿用；ai_guard.model / track 会被忽略，可删除。
  · 旧版网页登录 Cookie 失效，升级后需重新登录一次。
  · 旧发布包中手工修改 site-packages 的 MQTT 补丁不再需要，也不再受影响。

【安全提醒】
  printers.yaml 填好后含明文凭据，请勿提交到任何仓库;
  多用户共用机器建议用环境变量替代(BAMBU_WECOM_SECRET 等，见 README)。
