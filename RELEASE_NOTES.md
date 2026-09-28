bumbo_stats v{VERSION} 正式发布包
================================

(本文件由 build_release.ps1 自动注入版本号，模板在源码仓库 RELEASE_NOTES.md)

【包内容】
  bumbo_stats/               主程序(FastAPI + MQTT + SQLite + Web 面板)
  bambu_ai_deploy_dinoknn/   AI 缺陷检测器(含 DINOv2 权重+预训练特征库 gallery/)
  requirements-ai.txt        AI 检测运行时依赖清单
  printers.yaml.example      配置模板(全字段带中文注释)
  README.md                  使用说明(安装/配置/通知/AI/FAQ)
  LICENSE                    MIT 许可证

【开箱即检】
  模型权重与特征库已随包内置(作者机器长期调优成果), pip install ".[ai]" 后
  配置 ai_guard.enabled: true 即可直接报警,无需任何训练。
  默认决策阈值 0.5(k=5 近邻相似度加权投票, 连续 3 帧才报警), 可在 Web 设置页随时调节灵敏度。

【持续优化(可选,强烈建议)】
  包内已预留照片收集目录 bambu_ai_deploy_dinoknn/data/{normal,anomaly,uncertain}/
  把你自己打印的正常/失败照片放进去, watcher 会自动热追加到特征库(见该目录 README.txt),
  检测准确率会随你的实际使用不断提升(包内未含作者实拍照片,隐私已剔除)。

【包内不含(需自行获取)】
  拓竹 Token: 安装后运行 `bumbo-stats login` 交互登录获取

【快速上手(三步)】
  pip install ".[notify,ai]"       # 通知+AI 全功能; torch 约数百 MB, 仅 CPU 推理即可
  copy printers.yaml.example printers.yaml   # 然后编辑填打印机序列号等
  bumbo-stats login && bumbo-stats serve     # 面板默认 http://本机IP:8000

【环境要求】
  Python 3.10+（3.11/3.12 已验证）; Windows/Linux/macOS 均可;无需 Anaconda;
  需可访问局域网内打印机(摄像头 JPEG 6000 / RTSPS 322 端口)与拓竹云端。

【机型摄像头支持(v0.5.17 起全机型)】
  状态监控/产能/HMS 报警: 所有联网拓竹机型开箱即用(云端 MQTT 统一协议)。
  摄像头/AI 检测: 按端口自动分流两种协议——
    A1/A1 mini/P1P/P1S  → TCP 6000 JPEG, 开箱即用;
    X1 全系/P2S/P2P/X2D/H2D/H2S → RTSPS 322, 需 ffmpeg(三选一):
      系统安装 ffmpeg 加入 PATH / 设环境变量 BUMBO_FFMPEG_EXE / pip install ".[rtsp]"。
  未装 ffmpeg 时 RTSPS 机型仅摄像头不可用, 其他功能不受影响。

【安全提醒】
  printers.yaml 填好后含明文凭据,请勿提交到任何仓库;
  多用户共用机器建议用环境变量替代(BUMBO_WECOM_SECRET 等,见 README)。
