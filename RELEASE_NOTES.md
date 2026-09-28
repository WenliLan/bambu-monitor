bumbo_stats v{VERSION} 正式发布包
================================

(本文件由 build_release.ps1 自动注入版本号，模板在源码仓库 RELEASE_NOTES.md)

【包内容】
  bumbo_stats/               主程序(FastAPI + MQTT + SQLite + Web 面板)
  bambu_ai_deploy_dinoknn/   AI 缺陷检测器代码(kNN 方案,不含特征库)
  bambu_ai_deploy_onnx/      AI 检测器代码(ONNX 方案,不含模型权重)
  bambu_ai_deploy_pytorch/   AI 检测器代码(PyTorch 方案,不含模型权重)
  printers.yaml.example      配置模板(全字段带中文注释)
  README.md                  使用说明(安装/配置/通知/AI/FAQ)
  LICENSE                    MIT 许可证

【包内不含(需自行获取)】
  1. AI 模型权重(.pt/.onnx)与 kNN 特征库(.npy):
     - kNN 特征库: 用你自己拍摄的正常/异常照片运行
       bambu_ai_deploy_dinoknn/scripts/init_gallery.py 现场构建(推荐,效果更好)
     - 深度模型权重: 联系作者获取,或按 bambu_ai_deploy_pytorch/README.md 自行导出
     - 不使用 AI 检测不影响其他功能, 配置 ai_guard.enabled: false 即可
  2. 拓竹 Token: 安装后运行 `bumbo-stats login` 交互登录获取

【快速上手(三步)】
  pip install ".[notify]"          # 带企业微信/飞书通知; 仅 pip install . 为无通知的轻量版
  copy printers.yaml.example printers.yaml   # 然后编辑填打印机序列号等
  bumbo-stats login && bumbo-stats serve     # 面板默认 http://本机IP:8000

【环境要求】
  Python 3.10+（3.11/3.12 已验证）; Windows/Linux/macOS 均可;
  需可访问局域网内打印机(MQTT 8883端口)与拓竹云端。

【安全提醒】
  printers.yaml 填好后含明文凭据,请勿提交到任何仓库;
  多用户共用机器建议用环境变量替代(BUMBO_WECOM_SECRET 等,见 README)。
