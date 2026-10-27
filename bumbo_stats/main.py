"""bumbo_stats CLI 入口 - 登录认证与启动监控面板 Web 服务"""

import argparse
import getpass
import logging
import socket
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import uvicorn

from . import __version__


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _setup_console_logging(level: int = logging.INFO) -> None:
    """只配置控制台日志（login 等短命令使用）"""
    root = logging.getLogger()
    root.setLevel(level)
    if any(getattr(h, "_bumbo_console", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler._bumbo_console = True  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    root.addHandler(handler)


def _setup_logging(log_dir: str, debug: bool = False) -> Path:
    """配置控制台 + 按天轮转的文件日志，返回日志文件的绝对路径

    v0.5.17 的日志目录是 ``Path(__file__).parent.parent``，pip 安装后落在
    site-packages 里（P1-4）。现在统一由 ``settings.log_dir``（默认 ./logs，
    相对启动目录）决定，并支持 ``BUMBO_LOG_DIR`` 覆盖。
    """
    log_path = (Path(log_dir).expanduser() / "app.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # 重复调用（如测试）时先移除旧的轮转 handler
    for handler in list(root.handlers):
        if getattr(handler, "_bumbo_file", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler._bumbo_file = True  # type: ignore[attr-defined]
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    root.addHandler(file_handler)

    _setup_console_logging()

    # 第三方库日志：保留 WARNING/ERROR，不再 disabled（P2-6）
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("AiBotSDK").setLevel(logging.WARNING)

    return log_path


def _lan_ip() -> str | None:
    """获取本机在局域网中的 IPv4（仅用于启动横幅，不会真的发包）"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            return ip if ip and not ip.startswith("127.") else None
    except OSError:
        return None


def _print_banner(version: str, config, host: str, port: int, log_path: Path, config_path: str):
    """启动横幅：打印可点击地址、日志与配置文件绝对路径（P3-5 / P1-4）"""
    print(f"Bumbo Stats v{version} (云端模式)")
    print(f"   监控 {len(config.printers)} 台打印机")
    if host in ("0.0.0.0", "::", ""):
        print(f"   面板地址: http://127.0.0.1:{port}")
        lan_ip = _lan_ip()
        if lan_ip:
            print(f"   局域网地址: http://{lan_ip}:{port}  (同网段设备可访问)")
        print("   ⚠️ 监听 0.0.0.0 会暴露到局域网，请确认已设置管理员密码")
    else:
        print(f"   面板地址: http://{host}:{port}")
    print(f"   配置文件: {Path(config_path).expanduser().resolve()}")
    print(f"   日志文件: {log_path}")
    print()


def _do_login(args):
    """执行登录子命令"""
    from .config import load_config
    from .auth import TokenManager

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        print("请创建 printers.yaml 配置文件，或使用 --config 指定路径", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"配置文件错误: {e}", file=sys.stderr)
        sys.exit(1)

    # 登录
    region = "china" if config.cloud.mqtt_broker == "cn" else "global"
    account_label = "手机号" if region == "china" else "邮箱"

    # 获取用户名密码
    username = args.username or config.cloud.username
    if not username:
        username = input(f"请输入拓竹云账号{account_label}: ").strip()

    password = args.password
    if not password:
        password = getpass.getpass("请输入密码: ")
    else:
        print(
            "⚠️  通过 --password 传入的密码会留在 shell 历史中，建议省略该参数改为交互输入",
            file=sys.stderr,
        )

    if not username or not password:
        print("错误: 用户名和密码不能为空", file=sys.stderr)
        sys.exit(1)

    token_mgr = TokenManager(config.cloud.token_file)
    try:
        print(f"正在登录 {username}...")
        if region == "china":
            print("（登录过程中会向该手机号发送短信验证码）")
        else:
            print("（登录过程中会向注册邮箱发送邮件验证码）")
        token_mgr.interactive_login(username, password, region=region)
        print(f"登录成功！Token 已保存到: {token_mgr.token_path}")
    except RuntimeError as e:
        print(f"登录失败: {e}", file=sys.stderr)
        sys.exit(1)


# 强制 stdout/stderr 为 UTF-8，避免 Windows GBK 终端写入 emoji 时抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass


def _do_serve(args):
    """执行服务启动"""
    from .config import load_config

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        print("请创建 printers.yaml 配置文件，或使用 --config 指定路径", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"配置文件错误: {e}", file=sys.stderr)
        sys.exit(1)

    host = args.host or config.settings.host
    port = args.port or config.settings.port

    log_path = _setup_logging(config.settings.log_dir, debug=args.debug)
    _print_banner(__version__, config, host, port, log_path, args.config)

    # 创建并启动 FastAPI 应用
    from .api.app import create_app
    app = create_app(config=config)

    # 配置 uvicorn 日志（完全禁用 access 日志，只保留项目日志）
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["formatters"]["access"]["fmt"] = ""
    log_config["handlers"]["access"]["level"] = "ERROR"  # 静默 access handler

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=log_config,
        log_level="info",
        reload=args.debug,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Bumbo Stats - 拓竹3D打印机多机云端监控面板",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bumbo_stats {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # login 子命令
    login_parser = subparsers.add_parser(
        "login",
        help="登录拓竹云账号获取 Token（首次使用需运行）",
    )
    login_parser.add_argument(
        "--config", "-c",
        default="printers.yaml",
        help="配置文件路径 (默认: printers.yaml)",
    )
    login_parser.add_argument(
        "--username", "-u",
        default=None,
        help="拓竹云账号：中国区填手机号，全球区填邮箱（也可从配置文件读取）",
    )
    login_parser.add_argument(
        "--password", "-p",
        default=None,
        help="密码（⚠️ 会留在 shell 历史中，建议省略该参数改为交互输入）",
    )

    # serve 子命令
    serve_parser = subparsers.add_parser(
        "serve",
        help="启动监控面板 Web 服务",
    )
    serve_parser.add_argument(
        "--config", "-c",
        default="printers.yaml",
        help="配置文件路径 (默认: printers.yaml)",
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        help="监听地址",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口",
    )
    serve_parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试模式（热重载）",
    )

    # 兼容旧版：直接运行不带子命令时默认 serve
    args = parser.parse_args()

    if args.command == "login":
        _setup_console_logging()
        _do_login(args)
    elif args.command == "serve" or args.command is None:
        # 如果没有子命令，检查是否有旧式参数
        if args.command is None:
            # 设置默认值
            args.config = "printers.yaml"
            args.host = None
            args.port = None
            args.debug = False
            # 重新解析以支持旧式参数
            legacy_parser = argparse.ArgumentParser(
                description="Bumbo Stats - 拓竹3D打印机多机云端监控面板",
            )
            legacy_parser.add_argument("--config", "-c", default="printers.yaml")
            legacy_parser.add_argument("--host", default=None)
            legacy_parser.add_argument("--port", "-p", type=int, default=None)
            legacy_parser.add_argument("--debug", action="store_true")
            legacy_parser.add_argument("--version", action="version", version="bumbo_stats 0.3.0")
            args = legacy_parser.parse_args()
        _do_serve(args)


if __name__ == "__main__":
    main()
