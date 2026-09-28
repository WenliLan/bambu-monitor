"""bumbo_stats CLI 入口 - 登录认证与启动监控面板 Web 服务"""

import argparse
import getpass
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import uvicorn

from . import __version__


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

    # 获取用户名密码
    username = args.username or config.cloud.username
    if not username:
        username = input("请输入拓竹云账号邮箱: ").strip()

    password = args.password
    if not password:
        password = getpass.getpass("请输入密码: ")

    if not username or not password:
        print("错误: 用户名和密码不能为空", file=sys.stderr)
        sys.exit(1)

    # 登录
    region = "china" if config.cloud.mqtt_broker == "cn" else "global"
    token_mgr = TokenManager(config.cloud.token_file)
    try:
        print(f"正在登录 {username}...")
        print("（邮箱验证码将发送到你的注册邮箱）")
        token = token_mgr.interactive_login(username, password, region=region)
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

    print(f"Bumbo Stats v{__version__} (云端模式)")
    print(f"   监控 {len(config.printers)} 台打印机")
    print(f"   面板地址: http://{host}:{port}")
    print()

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
        help="拓竹云账号邮箱（也可从配置文件读取）",
    )
    login_parser.add_argument(
        "--password", "-p",
        default=None,
        help="密码（建议交互式输入）",
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

    # 配置日志：控制台 + 按天轮转的日志文件
    log_dir = Path(__file__).parent.parent
    log_file = log_dir / "logs" / "app.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # 清除根日志处理器，添加轮转文件处理器和控制台处理器
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG if getattr(args, "debug", False) else logging.INFO)
    
    # 静默第三方库日志（命令行和文件都静默）
    for logger_name in ["AiBotSDK", "bambulab", "uvicorn.access", "uvicorn.error"]:
        logging.getLogger(logger_name).setLevel(logging.ERROR)
        logging.getLogger(logger_name).disabled = True
    
    # 文件日志：每天轮转，保留 7 天
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    
    # 控制台日志
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # 禁用 bambulab 和 AiBotSDK 的根日志处理器（防止它们自己配置日志）
    for lib_logger in ["bambulab", "AiBotSDK"]:
        lib_logger_obj = logging.getLogger(lib_logger)
        lib_logger_obj.handlers.clear()
        lib_logger_obj.addHandler(logging.NullHandler())
        lib_logger_obj.disabled = True

    if args.command == "login":
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
