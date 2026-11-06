# 安全说明

## 网络暴露

- 服务**默认只监听 `127.0.0.1`**。改为 `0.0.0.0` 前请先设置管理员密码，并确认同网段设备可信——这会把实时画面和数据暴露给局域网内所有设备。
- 公网部署请保持 `127.0.0.1` 监听并配合反向代理（HTTPS），不要把面板直接暴露到公网。

## 接口鉴权

- 所有 `/api/*` **数据接口**都要求登录：未携带会话 Cookie 时返回 **401**。
- 公开端点仅三个：`/healthz`、`/login` 与认证接口 `/api/auth/*`。
- 登录 Cookie 保存**进程内随机 session token**（`secrets.token_urlsafe`，HttpOnly，7 天过期），不再是口令哈希；服务重启后需重新登录。

## 凭据管理

- `printers.yaml` 含打印机 `access_code`、机器人 `secret` 等**明文凭据**，已被 `.gitignore` 排除，请勿提交到任何仓库。
- 「从云端导入」会自动把云端下发的 `dev_access_code` 写入 `printers.yaml`（与手工录入的落盘方式一致）。
  候选列表接口 `GET /api/settings/cloud/devices` **只返回"接入码是否已获取"的布尔标记，不回显码值**；
  导入时由服务端按序列号自行重新取值。
- 局域网发现（`lan_discovery`）默认关闭：开启后会在本机监听一个 UDP 端口并向子网广播探测报文，
  请仅在可信内网启用。
- 多用户 / 容器环境推荐用环境变量替代明文落盘（优先级高于 YAML），清单见 [CONFIG.md](CONFIG.md#环境变量覆盖)。
- Token / 密钥未做本地加密存储，多用户环境请限制文件读取权限。

## 分发与分享

**分发时请用 git 导出**（`git clone` / `git archive`），不要直接打包工作目录。以下路径只应留在本地，且均已被 `.gitignore` 覆盖：

| 路径 | 内容 |
|---|---|
| `printers.yaml` | 全部设备与机器人凭据 |
| `data/` | SQLite 数据库、报警实拍照片 |
| `bambu_ai_deploy_dinoknn/data/` | AI 训练实拍照片 |
| `bambu_ai_deploy_dinoknn/dinov2_cache/` | 第三方骨干网权重（许可限制） |
| `logs/` | 运行日志（含设备序列号与状态） |
| `cookies.txt`、`*.bak` | 会话凭据与配置备份 |

## 第三方组件许可

逐项清单见仓库根目录的 [NOTICE](../NOTICE)。要点：

- 本项目代码：**MIT**
- DINOv2 骨干网权重：**CC-BY-NC 4.0**（Meta，禁止商用），因此不随仓库分发；商用请自行评估授权或更换编码器
- 随仓库分发的 `gallery/*.npy` 特征库由该非商用模型提取（见 `gallery/meta.json`），同样建议按非商用范围使用
- 打印机接入使用拓竹云端 MQTT / REST 协议，需用户自有账号；本项目非拓竹官方产品，是否符合其服务条款由使用者自行评估
