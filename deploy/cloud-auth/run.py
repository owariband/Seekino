"""Manage an isolated native cloud phone-test instance without changing local."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import signal
import smtplib
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import local_services as local
from redis import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url


def stop_owned(name: str) -> None:
    pid = local.owned_pid(name)
    if pid is None:
        return
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while local.signature(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if local.owned_pid(name):
        raise RuntimeError(f"{name} 尚未退出；保留进程，不强制终止。")
    (local.STATE / f"{name}.pid.json").unlink(missing_ok=True)
    print(f"[停止] {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init-data", "start", "status", "stop", "restart-api"), default="start", nargs="?")
    action = parser.parse_args().action
    os.chdir(ROOT)
    local.STATE.mkdir(parents=True, exist_ok=True)
    with (local.STATE / "startup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == "stop":
            for name in ("phone-api", "phone-mailpit", "phone-redis"):
                stop_owned(name)
            return
        settings = local.configure()
        if settings.auth.mode != "cloud":
            raise RuntimeError("请先加载手机测试的 cloud 环境配置。")
        api_port = local.port("ANIVAG_HTTP_PORT")
        if (api_port != 9622 or make_url(settings.database_url).database != "seekino_phone_dev"
                or settings.artifact_root.resolve() != (ROOT / "outputs/phone-test/artifacts").resolve()
                or settings.elasticsearch.index_name != "seekino_phone_chunks_dev"
                or settings.elasticsearch.event_index_for_environment(settings.environment) != "seekino_phone_events_dev"
                or settings.auth.cookie_name != "seekino_phone_auth"):
            raise RuntimeError("手机测试须使用独立的 9622 API、phone 数据库/索引/目录和 Cookie；拒绝接管 local。")
        if action == "init-data":
            local.start_mysql(settings)
            local.start_elasticsearch(settings)
            subprocess.run([sys.executable, "-m", "server.cli", "init-event-index"], check=True)
            return
        redis_port = local.port("ANIVAG_REDIS_PORT", 16379)
        smtp_port = local.port("ANIVAG_MAILPIT_SMTP_PORT", 1025)
        inbox_port = local.port("ANIVAG_MAILPIT_WEB_PORT", 8025)
        redis_url = urlsplit(settings.auth.redis_url)
        if redis_url.hostname != "127.0.0.1" or redis_url.port != redis_port:
            raise RuntimeError("认证 Redis URL 必须匹配本项目的 loopback Redis 端口。")
        if settings.auth.smtp.host != "127.0.0.1" or settings.auth.smtp.port != smtp_port:
            raise RuntimeError("测试 SMTP 必须指向本机 Mailpit。")
        if settings.auth.smtp.use_ssl or settings.auth.smtp.start_tls or settings.auth.smtp.username:
            raise RuntimeError("本机 Mailpit 使用无认证的明文 SMTP，请关闭 SMTP SSL/STARTTLS 并清空用户名。")
        state = local.STATE / "cloud-auth"
        state.mkdir(mode=0o700, exist_ok=True)
        (state / "redis").mkdir(mode=0o700, exist_ok=True)
        specs = {
            "phone-redis": ([local.binary("redis-server"), "--bind", "127.0.0.1", "--port", str(redis_port),
                "--protected-mode", "yes", "--daemonize", "no", "--set-proc-title", "no", "--dir", str(state / "redis"),
                "--save", "", "--appendonly", "yes", "--appendfsync", "everysec"], [redis_port]),
            "phone-mailpit": ([local.binary("mailpit"), "--smtp", f"127.0.0.1:{smtp_port}",
                "--listen", f"127.0.0.1:{inbox_port}", "--database", str(state / "mailpit.db"),
                "--disable-version-check", "--smtp-disable-rdns", "--label", "Seekino phone test"], [smtp_port, inbox_port]),
        }
        clean_env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
        for name, (command, ports) in specs.items():
            owned = local.owned_pid(name)
            ready = all(local.listening(port) for port in ports)
            if action == "status":
                print(f"{name}: {'ready' if owned and ready else 'not ready'}")
                continue
            if owned:
                if not ready:
                    raise RuntimeError(f"{name} 进程存活但未就绪，请查看项目日志。")
            elif any(local.listening(port) for port in ports):
                raise RuntimeError(f"{name} 端口已被其他进程占用，不接管。")
            else:
                local.launch(name, command, env=clean_env)
                local.wait_ready(name, lambda: all(local.listening(port) for port in ports), timeout=20)
        if action != "restart-api":
            if action == "status":
                print(f"phone-api: {'ready' if local.owned_pid('phone-api') and local.api_ready(f'http://127.0.0.1:{api_port}') else 'not ready'}")
            return

        from server.bootstrap import prepare_service_configuration

        prepare_service_configuration(settings)
        with Redis.from_url(settings.auth.redis_url, socket_timeout=3) as redis:
            redis.ping()
        with smtplib.SMTP(settings.auth.smtp.host, settings.auth.smtp.port, timeout=3) as smtp:
            if smtp.noop()[0] != 250:
                raise RuntimeError("模拟 SMTP 未就绪，保留现有 API。")
        model = local.load_config()
        missing = [key for key in local.required_api_key_env_names(model) if not os.environ.get(key)]
        if missing:
            raise RuntimeError("缺少模型配置所需环境变量：" + ", ".join(sorted(missing)))
        engine = local.create_database_engine(settings.database_url, environment=settings.environment)
        try:
            with engine.connect() as connection:
                active = connection.execute(text("SELECT COUNT(*) FROM video_parse_task WHERE is_deleted=0 "
                    "AND status IN ('PENDING','PREPARING','PARSING','INDEXING')")).scalar_one()
            if active and local.owned_pid("phone-api"):
                raise RuntimeError(f"仍有 {active} 个活跃解析任务，暂不重启 API。")
        finally:
            engine.dispose()
        if local.listening(api_port) and local.owned_pid("phone-api") is None:
            raise RuntimeError("API 端口由其他进程占用，不接管。")
        stop_owned("phone-api")
        if local.listening(api_port):
            raise RuntimeError("旧 API 端口尚未释放，不启动第二个 Consumer。")
        local.launch("phone-api", [sys.executable, "-u", "-m", "server.api"])
        try:
            local.wait_ready("phone-api", lambda: local.api_ready(f"http://127.0.0.1:{api_port}"))
        except Exception:
            stop_owned("phone-api")
            raise
        print("[就绪] 独立 cloud API :9622；原 local API :9621、Vite 和原资料保持不变。")


if __name__ == "__main__":
    main()
