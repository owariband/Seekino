"""Start missing localhost services without replacing existing processes or data."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from qdrant_client import QdrantClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError

# Direct script execution puts scripts/, rather than the repository, on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import load_config
from server.config import ServerSettings
from server.database import create_database_engine, create_schema
from server.storage.qdrant import QdrantVideoChunkStore

STATE = ROOT / "outputs/local-services"
HTTP = build_opener(ProxyHandler({}))
CHILDREN: dict[str, subprocess.Popen] = {}
_DOTENV_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*="
)


def dotenv_variable_names(path: str | Path) -> tuple[str, ...]:
    """Return assignment names without evaluating or exposing dotenv values."""

    names: dict[str, None] = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        match = _DOTENV_ASSIGNMENT.match(line)
        if match is not None:
            names.setdefault(match.group(1), None)
    return tuple(names)


def config_digest() -> str:
    values = {key: value for key, value in os.environ.items() if key.startswith(("ANIVAG_", "VITE_"))}
    model_config = load_config()
    api_key_names = {"ARK_API_KEY", model_config.agent_model.api_key_env}
    if model_config.video_parser.fallback.enabled:
        api_key_names.add(model_config.video_parser.fallback.deepseek.api_key_env)
    for key in sorted(api_key_names):
        value = os.environ.get(key, "")
        values[key] = hashlib.sha256(value.encode()).hexdigest()
    configured_model_path = Path(
        os.environ.get("ANIVAG_MODEL_CONFIG", str(ROOT / "config.yaml"))
    ).expanduser().resolve()
    configured_pricing_path = Path(
        os.environ.get(
            "ANIVAG_MODEL_PRICING_CONFIG",
            str(ROOT / "config/model-pricing.yaml"),
        )
    ).expanduser().resolve()
    # Agent run settings are reloaded for new messages. Keep the restart-only
    # concurrency setting in the digest alongside model and parser settings.
    model_values = model_config.model_dump(mode="json")
    runtime_values = model_values.get("agent_runtime", {})
    model_values["agent_runtime"] = {
        "maximum_concurrent_kicks": runtime_values["maximum_concurrent_kicks"]
    } if "maximum_concurrent_kicks" in runtime_values else {}
    values[str(configured_model_path)] = hashlib.sha256(
        json.dumps(model_values, sort_keys=True).encode()
    ).hexdigest()
    values[str(configured_pricing_path)] = hashlib.sha256(
        configured_pricing_path.read_bytes()
    ).hexdigest()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def configure() -> ServerSettings:
    defaults = {
        "ANIVAG_ENVIRONMENT": "development",
        "ANIVAG_DATABASE_URL": "mysql+pymysql://root@127.0.0.1:3306/anivag_development?charset=utf8mb4",
        "ANIVAG_QDRANT_URL": "http://127.0.0.1:6333",
        "ANIVAG_QDRANT_COLLECTION": "anivag_video_chunks_development",
        "ANIVAG_HTTP_HOST": "127.0.0.1",
        "ANIVAG_HTTP_PORT": "9621",
        "ANIVAG_WEB_PORT": "5173",
        "ANIVAG_ARTIFACT_ROOT": str(ROOT / "outputs/server/artifacts"),
    }
    for key, value in defaults.items():
        if not os.environ.get(key):
            os.environ[key] = value
    web_port = port("ANIVAG_WEB_PORT")
    os.environ.setdefault(
        "ANIVAG_ALLOWED_ORIGINS",
        f"http://127.0.0.1:{web_port},http://localhost:{web_port}",
    )
    settings = ServerSettings.from_environment()
    database = make_url(settings.database_url)
    qdrant = urlsplit(settings.qdrant_url)
    if settings.environment.value != "development":
        raise RuntimeError("本地启动脚本只支持 ANIVAG_ENVIRONMENT=development。")
    if database.drivername != "mysql+pymysql":
        raise RuntimeError("本地启动脚本需要 mysql+pymysql 数据库 URL。")
    for host in (database.host, qdrant.hostname, os.environ["ANIVAG_HTTP_HOST"]):
        if host not in ("127.0.0.1", "localhost"):
            raise RuntimeError("本地启动脚本只支持 127.0.0.1 / localhost。")
    if qdrant.scheme not in ("http", "https") or qdrant.path.rstrip("/"):
        raise RuntimeError("ANIVAG_QDRANT_URL 需要本地 HTTP(S) 地址，不带路径。")
    port("ANIVAG_HTTP_PORT")
    # Validate the development database name before starting or creating anything.
    engine = create_database_engine(settings.database_url, environment=settings.environment)
    engine.dispose()
    QdrantVideoChunkStore(
        None, collection_name=settings.qdrant_collection, environment=settings.environment,
        text_dimensions=settings.text_vector_dimensions,
        vision_dimensions=settings.vision_vector_dimensions,
    )
    return settings


def port(key: str, default: int | None = None) -> int:
    value = int(os.environ.get(key, str(default)))
    if not 1 <= value <= 65535:
        raise RuntimeError(f"{key} 必须在 1–65535 之间。")
    return value


def listening(number: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", number), timeout=0.5):
            return True
    except OSError:
        return False


def get(url: str) -> bytes | None:
    try:
        with HTTP.open(url, timeout=2) as response:
            return response.read()
    except (OSError, URLError):
        return None


def binary(name: str, *fallbacks: Path) -> str:
    found = os.environ.get(f"ANIVAG_{name.upper()}_BIN") or shutil.which(name)
    if found and shutil.which(found):
        return shutil.which(found)
    for candidate in fallbacks:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(f"找不到 {name}，请安装，或设置 ANIVAG_{name.upper()}_BIN。")


def signature(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "args="],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def owned_pid(name: str) -> int | None:
    try:
        record = json.loads((STATE / f"{name}.pid.json").read_text())
        pid = record["pid"]
        if record["signature"] and signature(pid) == record["signature"]:
            return pid
    except (OSError, ValueError, KeyError):
        pass
    return None


def launch(name: str, args: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> int:
    digest = config_digest()
    log_path = STATE / f"{name}.log"
    with log_path.open("ab") as log:
        offset = log.tell()
        child = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    CHILDREN[name] = child
    record = {"pid": child.pid, "signature": signature(child.pid), "config": digest}
    (STATE / f"{name}.pid.json").write_text(json.dumps(record))
    print(f"[启动] {name} PID={child.pid}，日志 {log_path.relative_to(ROOT)}", flush=True)
    return offset


def wait_ready(name: str, check, *, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        child = CHILDREN.get(name)
        if child and child.poll() is not None:
            raise RuntimeError(f"{name} 提前退出，请查看 outputs/local-services/{name}.log。")
        if check():
            return
        time.sleep(0.5)
    raise RuntimeError(f"{name} 在 {timeout} 秒内未就绪，请查看 outputs/local-services/{name}.log。")


def start_mysql(settings: ServerSettings) -> None:
    url = make_url(settings.database_url)
    mysql_port = url.port or 3306
    if listening(mysql_port):
        print(f"[复用] MySQL :{mysql_port}", flush=True)
    else:
        if owned_pid("mysql"):
            raise RuntimeError("已有脚本启动的 MySQL 尚未就绪，请查看 mysql.log。")
        mysqld = binary(
            "mysqld", Path("/opt/homebrew/opt/mysql@8.4/bin/mysqld"),
            Path("/usr/local/opt/mysql@8.4/bin/mysqld"),
        )
        datadir = STATE / "mysql"
        # A new managed instance uses its own passwordless loopback root account.
        if url.username != "root" or url.password:
            raise RuntimeError(
                "MySQL 未运行；脚本自建实例使用 root / 空密码。"
                "请先启动配置的已有实例，或将 ANIVAG_DATABASE_URL 改为本地 root / 空密码 URL。"
            )
        datadir.mkdir(exist_ok=True)
        if not (datadir / "mysql").is_dir():
            if any(datadir.iterdir()):
                raise RuntimeError("MySQL 数据目录非空但未初始化；请检查，脚本不会覆盖。")
            print("[初始化] 项目独立 MySQL 数据目录（首次启动）", flush=True)
            with (STATE / "mysql.log").open("ab") as log:
                subprocess.run(
                    [mysqld, "--no-defaults", "--initialize-insecure", f"--datadir={datadir}"],
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120,
                )
        # Keep the Unix socket short enough even when the repository path is long.
        socket_path = Path("/tmp") / f"anivag-mysql-{os.getuid()}-{mysql_port}.sock"
        launch("mysql", [
            mysqld, "--no-defaults", f"--datadir={datadir}", "--bind-address=127.0.0.1",
            f"--port={mysql_port}", f"--socket={socket_path}",
            f"--pid-file={STATE / 'mysql-server.pid'}", "--mysqlx=OFF",
        ])
        wait_ready("mysql", lambda: listening(mysql_port))

    engine = create_database_engine(settings.database_url, environment=settings.environment)
    try:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except OperationalError as error:
            if error.orig.args[0] != 1049:  # MySQL: unknown database
                raise
            admin = create_engine(url.set(database=""), connect_args={"connect_timeout": 5})
            try:
                with admin.connect() as connection:
                    # Escape the validated development name as an identifier.
                    name = url.database.replace("`", "``")
                    connection.execute(text(
                        f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                    ))
            finally:
                admin.dispose()
        create_schema(engine)
    finally:
        engine.dispose()
    print(f"[就绪] MySQL / {url.database}，Schema 已校验", flush=True)


def start_qdrant(settings: ServerSettings) -> None:
    url = urlsplit(settings.qdrant_url)
    http_port = url.port or (443 if url.scheme == "https" else 80)
    if listening(http_port):
        print(f"[复用] Qdrant :{http_port}", flush=True)
        if get(settings.qdrant_url.rstrip("/") + "/readyz") is None:
            raise RuntimeError("Qdrant 端口已占用但 /readyz 未就绪。")
    else:
        if owned_pid("qdrant"):
            raise RuntimeError("已有脚本启动的 Qdrant 尚未就绪，请查看 qdrant.log。")
        if url.scheme != "http" or settings.qdrant_api_key:
            raise RuntimeError("自建 Qdrant 使用 HTTP / 无 API Key，请先启动配置的已有实例。")
        grpc_port = port("ANIVAG_QDRANT_GRPC_PORT", 6334)
        if listening(grpc_port):
            raise RuntimeError(f"Qdrant gRPC 端口 {grpc_port} 已占用。")
        qdrant = binary("qdrant", Path.home() / ".local/bin/qdrant")
        directory = STATE / "qdrant"
        directory.mkdir(exist_ok=True)
        env = os.environ.copy()
        env.update({
            "QDRANT__STORAGE__STORAGE_PATH": str(directory / "storage"),
            "QDRANT__STORAGE__SNAPSHOTS_PATH": str(directory / "snapshots"),
            "QDRANT__SERVICE__HOST": "127.0.0.1",
            "QDRANT__SERVICE__HTTP_PORT": str(http_port),
            "QDRANT__SERVICE__GRPC_PORT": str(grpc_port),
            "QDRANT__TELEMETRY_DISABLED": "true",
        })
        launch("qdrant", [qdrant, "--disable-telemetry"], cwd=directory, env=env)
        wait_ready("qdrant", lambda: get(settings.qdrant_url.rstrip("/") + "/readyz") is not None)
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=5)
    try:
        QdrantVideoChunkStore(
            client, collection_name=settings.qdrant_collection, environment=settings.environment,
            text_dimensions=settings.text_vector_dimensions,
            vision_dimensions=settings.vision_vector_dimensions,
        ).ensure_collection()
    finally:
        client.close()
    print(f"[就绪] Qdrant / {settings.qdrant_collection}，向量 Schema 已校验", flush=True)


def api_ready(base: str) -> bool:
    payload = get(base + "/healthz")
    try:
        health = json.loads(payload) if payload is not None else None
        return (
            isinstance(health, dict)
            and health.get("status") == "ok"
            and health.get("components", {})
            .get("parseConsumer", {})
            .get("status")
            == "running"
        )
    except (AttributeError, ValueError):
        return False


def library_ready(base: str) -> bool:
    payload = get(base + "/api/series/list?workspaceId=local-default")
    try:
        page = json.loads(payload) if payload is not None else None
        return isinstance(page, dict) and isinstance(page.get("items"), list) and isinstance(page.get("total"), int)
    except ValueError:
        return False


def start_applications() -> None:
    api_port = port("ANIVAG_HTTP_PORT")
    api_base = f"http://127.0.0.1:{api_port}"
    if listening(api_port):
        if not api_ready(api_base):
            raise RuntimeError(f"API 端口 {api_port} 已占用，未识别到 anivag API。")
        print(f"[复用] API :{api_port}", flush=True)
    else:
        if owned_pid("api"):
            raise RuntimeError("已有脚本启动的 API 尚未就绪，请查看 api.log。")
        if not os.environ.get("ARK_API_KEY"):
            raise RuntimeError(
                "请在 .env.local 填写 ARK_API_KEY，anivag Server 的 Parse Consumer 需要它完成启动。"
            )
        model_config = load_config()
        deepseek_key_names = {model_config.agent_model.api_key_env}
        if model_config.video_parser.fallback.enabled:
            deepseek_key_names.add(
                model_config.video_parser.fallback.deepseek.api_key_env
            )
        missing_deepseek_keys = [
            key for key in sorted(deepseek_key_names) if not os.environ.get(key)
        ]
        if missing_deepseek_keys:
            raise RuntimeError(
                "请在 .env.local 填写 "
                + "、".join(missing_deepseek_keys)
                + "；anivag Agent 或已启用的 DeepSeek 解析需要它完成启动。"
            )
        launch("api", [sys.executable, "-u", "-m", "server.api"])
        wait_ready("api", lambda: api_ready(api_base))
    if not library_ready(api_base):
        raise RuntimeError("API 已监听但资料库查询失败，请查看 api.log。")

    web_port = port("ANIVAG_WEB_PORT")
    web_base = f"http://127.0.0.1:{web_port}"
    if listening(web_port):
        page = get(web_base)
        if page is None or b"/@vite/client" not in page or b"anivag" not in page:
            raise RuntimeError(f"Web 端口 {web_port} 已占用，未识别到 anivag Vite。")
        print(f"[复用] Web :{web_port}", flush=True)
    else:
        if owned_pid("web"):
            raise RuntimeError("已有脚本启动的 Web 尚未就绪，请查看 web.log。")
        vite = ROOT / "web/node_modules/vite/bin/vite.js"
        if not vite.is_file():
            subprocess.run(["npm", "ci"], cwd=ROOT / "web", check=True)
        launch("web", [
            binary("node"), str(vite), "--host", "127.0.0.1",
            "--port", str(web_port), "--strictPort",
        ], cwd=ROOT / "web")
        wait_ready("web", lambda: get(web_base) is not None)
    if not library_ready(web_base):
        raise RuntimeError("Web 已监听但 API 代理查询失败，请检查端口配置或 web.log。")


def status(settings: ServerSettings) -> bool:
    database_port = make_url(settings.database_url).port or 3306
    checks = {
        "MySQL TCP": listening(database_port),
        "Qdrant readyz": get(settings.qdrant_url.rstrip("/") + "/readyz") is not None,
        "API / Parse Consumer": api_ready(f"http://127.0.0.1:{port('ANIVAG_HTTP_PORT')}"),
        "Web": get(f"http://127.0.0.1:{port('ANIVAG_WEB_PORT')}/@vite/client") is not None,
    }
    for name, ready in checks.items():
        print(f"[{'运行' if ready else '未就绪'}] {name}")
    return all(checks.values())


def stop() -> None:
    for name in ("web", "api", "qdrant", "mysql"):
        pid = owned_pid(name)
        if not pid:
            print(f"[跳过] {name}：没有本脚本管理的存活进程")
            continue
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while signature(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if owned_pid(name):
            raise RuntimeError(f"{name} 尚未退出，可能正在收尾；稍后重新运行 stop。")
        (STATE / f"{name}.pid.json").unlink(missing_ok=True)
        print(f"[停止] {name} PID={pid}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="懒启动 anivag 全部 localhost 服务。")
    parser.add_argument(
        "command",
        choices=("start", "status", "stop", "_env-names"),
        nargs="?",
        default="start",
    )
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.command == "_env-names":
        if args.env_file is None:
            raise ValueError("_env-names requires --env-file")
        for name in dotenv_variable_names(args.env_file):
            print(name)
        return 0
    os.chdir(ROOT)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "startup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个启动/停止操作正在进行，请等待它完成。") from None
        if args.command == "stop":
            stop()
            return 0
        settings = configure()
        if args.command == "status":
            return 0 if status(settings) else 1
        for tool in ("ffmpeg", "ffprobe", "node", "npm"):
            binary(tool)
        for name in ("api", "web"):
            if owned_pid(name):
                record = json.loads((STATE / f"{name}.pid.json").read_text())
                if record.get("config") != config_digest():
                    raise RuntimeError(f"{name} 的启动配置已变化，请先 ./start.sh stop 再启动。")
        start_mysql(settings)
        start_qdrant(settings)
        start_applications()
        for name, child in CHILDREN.items():
            if child.poll() is not None:
                raise RuntimeError(f"{name} 已退出，请查看 outputs/local-services/{name}.log。")
        print(
            f"\n服务已就绪：\n  Web  http://127.0.0.1:{port('ANIVAG_WEB_PORT')}"
            f"\n  API  http://127.0.0.1:{port('ANIVAG_HTTP_PORT')}/docs"
            "\n  状态 ./start.sh status\n  停止 ./start.sh stop"
            "\n  日志 outputs/local-services/*.log",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SQLAlchemyError as error:
        print(f"[失败] 数据库连接或 Schema 初始化失败：{error.orig if hasattr(error, 'orig') else type(error).__name__}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"[失败] {error}", file=sys.stderr)
        sys.exit(1)
