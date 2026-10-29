"""Run localhost applications and native databases from the selected worktree."""
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
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError

# Direct script execution puts scripts/, rather than the repository, on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import required_api_key_env_names, load_config
from server.caller.elasticsearch import create_elasticsearch_client, close_elasticsearch_client
from server.caller.mysql import (
    create_admin_database_engine, create_database_engine, close_database_engine,
)
from server.config import ServerSettings
from server.config.auth import AuthConfig
from server.database import create_schema
from server.storage.elasticsearch import ElasticsearchVideoChunkStore

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
    values = {key: value for key, value in os.environ.items() if key.startswith(("SEEKINO_", "VITE_"))}
    model_config = load_config()
    api_key_names = required_api_key_env_names(model_config)
    for key in sorted(api_key_names):
        value = os.environ.get(key, "")
        values[key] = hashlib.sha256(value.encode()).hexdigest()
    configured_model_path = Path(
        os.environ.get("SEEKINO_MODEL_CONFIG", str(ROOT / "config.yaml"))
    ).expanduser().resolve()
    configured_pricing_path = Path(
        os.environ.get(
            "SEEKINO_MODEL_PRICING_CONFIG",
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
        "SEEKINO_ENVIRONMENT": "development",
        "SEEKINO_DATABASE_URL": "mysql+pymysql://root@127.0.0.1:3306/seekino_development?charset=utf8mb4",
        "SEEKINO_HTTP_HOST": "127.0.0.1",
        "SEEKINO_HTTP_PORT": "9621",
        "SEEKINO_WEB_PORT": "5173",
        "SEEKINO_ARTIFACT_ROOT": str(ROOT / "assets/library"),
    }
    for key, value in defaults.items():
        if not os.environ.get(key):
            os.environ[key] = value
    web_port = port("SEEKINO_WEB_PORT")
    os.environ.setdefault(
        "SEEKINO_ALLOWED_ORIGINS",
        f"http://127.0.0.1:{web_port},http://localhost:{web_port}",
    )
    settings = ServerSettings.from_environment()
    if settings.elasticsearch is None:
        raise RuntimeError("本地启动需要 SEEKINO_ELASTICSEARCH_URL 和 SEEKINO_ELASTICSEARCH_INDEX。")
    database = make_url(settings.database_url)
    if settings.environment.value != "development":
        raise RuntimeError("本地启动脚本只支持 SEEKINO_ENVIRONMENT=development。")
    if database.drivername != "mysql+pymysql":
        raise RuntimeError("本地启动脚本需要 mysql+pymysql 数据库 URL。")
    hosts = [database.host, os.environ["SEEKINO_HTTP_HOST"]]
    if settings.elasticsearch is not None:
        hosts.append(urlsplit(settings.elasticsearch.url).hostname)
    for host in hosts:
        if host not in ("127.0.0.1", "localhost"):
            raise RuntimeError("本地启动脚本只支持 127.0.0.1 / localhost。")
    port("SEEKINO_HTTP_PORT")
    # Validate the development database name before starting or creating anything.
    engine = create_database_engine(settings.database_url, environment=settings.environment)
    close_database_engine(engine)
    if settings.elasticsearch is not None:
        ElasticsearchVideoChunkStore(
            None, index_name=settings.elasticsearch.index_name,
            environment=settings.environment,
            semantic_dimensions=settings.text_vector_dimensions,
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
    found = os.environ.get(f"SEEKINO_{name.upper()}_BIN") or shutil.which(name)
    if found and shutil.which(found):
        return shutil.which(found)
    for candidate in fallbacks:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(f"找不到 {name}，请安装，或设置 SEEKINO_{name.upper()}_BIN。")


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


def require_worktree_branch(expected: str) -> None:
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    if branch.returncode or branch.stdout.strip() != f"refs/heads/{expected}":
        raise RuntimeError(
            f"懒启动只允许 {expected} 工作区；请通过 ./start.sh --branch {expected} 定位。"
        )


def source_digest(name: str) -> str:
    paths = ["web"] if name == "web" else [
        "agent", "engine", "engines", "gateway", "server", "config",
        "pyproject.toml", "uv.lock", "scripts/local_services.py",
    ]
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *paths],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.split(b"\0")
    digest = hashlib.sha256()
    for relative in sorted(set(files) - {b""}):
        path = ROOT / os.fsdecode(relative)
        if path.is_file():
            digest.update(relative + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def owned_listener(name: str, number: int) -> bool:
    pid = owned_pid(name)
    if pid is None:
        return False
    listeners = subprocess.run(
        ["lsof", "-nP", "-t", f"-iTCP:{number}", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    )
    return listeners.returncode == 0 and set(listeners.stdout.split()) == {str(pid)}


def refresh_applications() -> None:
    for name, key in (("api", "SEEKINO_HTTP_PORT"), ("web", "SEEKINO_WEB_PORT")):
        number = port(key)
        if listening(number) and not owned_listener(name, number):
            raise RuntimeError(f"{name} 端口 {number} 被非本启动器管理的进程占用；请先停止该进程。")
    digest = config_digest()
    sources = {name: source_digest(name) for name in ("api", "web")}
    for name in ("api", "web"):
        if owned_pid(name):
            record = json.loads((STATE / f"{name}.pid.json").read_text())
            if (record.get("config") != digest
                    or record.get("source_root") != str(ROOT)
                    or record.get("source_digest") != sources[name]):
                print(f"[更新] {name} 使用当前工作区源码与配置", flush=True)
                stop((name,))


def launch(name: str, args: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> int:
    digest = config_digest()
    source = source_digest(name) if name in ("api", "web") else None
    log_path = STATE / f"{name}.log"
    with log_path.open("ab") as log:
        offset = log.tell()
        child = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    CHILDREN[name] = child
    record = {"pid": child.pid, "signature": signature(child.pid), "config": digest}
    if source is not None:
        record.update(source_root=str(ROOT), source_digest=source)
    (STATE / f"{name}.pid.json").write_text(json.dumps(record))
    print(f"[启动] {name} PID={child.pid}，日志 {log_path.relative_to(ROOT)}", flush=True)
    return offset


def wait_ready(name: str, check, *, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        child = CHILDREN.get(name)
        if child and child.poll() is not None:
            raise RuntimeError(f"{name} 提前退出，请查看 outputs/local-services/{name}.log。")
        # Native launchers may exec a JVM after Popen. Preserve ownership even
        # when readiness times out; only a live child handle authorizes this.
        if child is not None:
            path = STATE / f"{name}.pid.json"
            record = json.loads(path.read_text())
            current_signature = signature(child.pid)
            if record["pid"] == child.pid and current_signature and record["signature"] != current_signature:
                record["signature"] = current_signature
                path.write_text(json.dumps(record))
        if check():
            return
        time.sleep(0.5)
    raise RuntimeError(f"{name} 在 {timeout} 秒内未就绪，请查看 outputs/local-services/{name}.log。")


def mysql_data_directory() -> Path:
    configured = os.environ.get("SEEKINO_MYSQL_DATA_DIR")
    legacy = STATE / "mysql"
    if not configured and legacy.exists() and any(legacy.iterdir()):
        raise RuntimeError(
            f"检测到旧 MySQL 数据目录 {legacy}；不会自动改用空库或迁移数据。"
            "请显式设置 SEEKINO_MYSQL_DATA_DIR 指向要继续使用的目录，或完成备份迁移后再切换。"
        )
    return Path(configured).expanduser().resolve() if configured else ROOT / "data/mysql"


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
        datadir = mysql_data_directory()
        mysql_command = [mysqld, "--no-defaults"]
        # Official tarballs and resolved Homebrew keg binaries carry their
        # resources beside bin/. Other system layouts retain MySQL's defaults.
        basedir = Path(mysqld).resolve().parent.parent
        if (basedir / "share").is_dir() and (basedir / "lib").is_dir():
            mysql_command.append(f"--basedir={basedir}")
        mysql_command.append(f"--datadir={datadir}")
        # A new managed instance uses its own passwordless loopback root account.
        if url.username != "root" or url.password:
            raise RuntimeError(
                "MySQL 未运行；脚本自建实例使用 root / 空密码。"
                "请先启动配置的已有实例，或将 SEEKINO_DATABASE_URL 改为本地 root / 空密码 URL。"
            )
        datadir.mkdir(parents=True, exist_ok=True)
        if not (datadir / "mysql").is_dir():
            if any(datadir.iterdir()):
                raise RuntimeError("MySQL 数据目录非空但未初始化；请检查，脚本不会覆盖。")
            print("[初始化] 项目独立 MySQL 数据目录（首次启动）", flush=True)
            with (STATE / "mysql.log").open("ab") as log:
                subprocess.run(
                    [*mysql_command, "--initialize-insecure"],
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120,
                )
        # Keep the Unix socket short enough even when the repository path is long.
        socket_path = Path("/tmp") / f"seekino-mysql-{os.getuid()}-{mysql_port}.sock"
        launch("mysql", [
            *mysql_command, "--bind-address=127.0.0.1",
            f"--port={mysql_port}", f"--socket={socket_path}",
            f"--pid-file={STATE / 'mysql-server.pid'}", "--mysqlx=OFF",
            f"--innodb-buffer-pool-size={os.environ.get('SEEKINO_MYSQL_BUFFER_POOL_SIZE', '128M')}",
            "--binlog-expire-logs-seconds=604800",  # Keep seven days of binary logs.
            # Log only changed columns (FULL if CDC is added); compress transactions.
            "--binlog-row-image=MINIMAL", "--binlog-transaction-compression=ON",
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
            admin = create_admin_database_engine(
                settings.database_url, environment=settings.environment,
            )
            try:
                with admin.connect() as connection:
                    # Escape the validated development name as an identifier.
                    name = url.database.replace("`", "``")
                    connection.execute(text(
                        f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                    ))
            finally:
                close_database_engine(admin)
        create_schema(engine)
    finally:
        close_database_engine(engine)
    print(f"[就绪] MySQL / {url.database}，Schema 已校验", flush=True)


def start_elasticsearch(settings: ServerSettings) -> None:
    config = settings.elasticsearch
    if config is None:
        raise RuntimeError("本地启动需要显式配置 Elasticsearch。")
    address = urlsplit(config.url)
    http_port = address.port or (443 if address.scheme == "https" else 80)
    started = False
    if listening(http_port):
        print(f"[复用] Elasticsearch :{http_port}", flush=True)
    else:
        if owned_pid("elasticsearch"):
            raise RuntimeError("已有脚本启动的 Elasticsearch 尚未就绪，请查看 elasticsearch.log。")
        if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost"} or config.api_key:
            raise RuntimeError(
                "脚本自建 Elasticsearch 仅支持无认证的 loopback HTTP；请先启动配置的已有实例。"
            )
        executable = binary("elasticsearch")
        heap = os.getenv("SEEKINO_ELASTICSEARCH_HEAP_SIZE", "512m")
        if not re.fullmatch(r"[1-9][0-9]*[mMgG]", heap):
            raise RuntimeError("SEEKINO_ELASTICSEARCH_HEAP_SIZE 必须是 512m、1g 等正整数内存大小。")
        transport_port = port("SEEKINO_ELASTICSEARCH_TRANSPORT_PORT", http_port + 100)
        data = Path(os.getenv("SEEKINO_ELASTICSEARCH_DATA_DIR", str(ROOT / "data/elasticsearch"))).expanduser().resolve()
        logs = STATE / "elasticsearch-logs"
        data.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        launch("elasticsearch", [
            executable,
            "-Ecluster.name=seekino-local", "-Enode.name=seekino-local",
            "-Enetwork.host=127.0.0.1", "-Ehttp.host=127.0.0.1", "-Etransport.host=127.0.0.1",
            f"-Ehttp.port={http_port}",
            f"-Etransport.port={transport_port}", "-Ediscovery.type=single-node",
            "-Expack.security.enabled=false", "-Expack.security.autoconfiguration.enabled=false",
            f"-Epath.data={data}", f"-Epath.logs={logs}",
        ], env={**os.environ, "ES_JAVA_OPTS": f"-Xms{heap} -Xmx{heap}"})
        started = True
    client = create_elasticsearch_client(config, request_timeout_seconds=5)
    try:
        def ready() -> bool:
            try:
                health = client.cluster.health(wait_for_status="yellow", timeout="2s")
                return not health.get("timed_out") and health.get("status") in {"yellow", "green"}
            except Exception:
                return False

        if started:
            wait_ready("elasticsearch", ready, timeout=120)
        elif not ready():
            raise RuntimeError("Elasticsearch 集群未达到 yellow 状态")
        ElasticsearchVideoChunkStore(
            client, index_name=config.index_name, environment=settings.environment,
            semantic_dimensions=settings.text_vector_dimensions,
            vision_dimensions=settings.vision_vector_dimensions,
        ).ensure_index()
    except Exception as exc:
        raise RuntimeError(
            "Elasticsearch 未就绪或索引合同不匹配；请检查配置的实例或 outputs/local-services/elasticsearch.log。"
        ) from exc
    finally:
        close_elasticsearch_client(client)
    print(f"[就绪] Elasticsearch / {config.index_name}，索引 Schema 已校验", flush=True)


def initialize_storage() -> None:
    # Reuse the existing initialization contracts, including Deep Agents
    # checkpoints and the independent Event index; never rebuild existing data.
    for command in ("init-db", "init-event-index"):
        subprocess.run([sys.executable, "-m", "server.cli", command], cwd=ROOT, check=True)


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
    query = urlencode({"tenantId": AuthConfig.from_environment().local_tenant_id})
    payload = get(base + "/api/series/list?" + query)
    try:
        page = json.loads(payload) if payload is not None else None
        return isinstance(page, dict) and isinstance(page.get("items"), list) and isinstance(page.get("total"), int)
    except ValueError:
        return False


def start_applications() -> None:
    api_port = port("SEEKINO_HTTP_PORT")
    api_base = f"http://127.0.0.1:{api_port}"
    model_config = load_config()
    missing_api_keys = [
        key for key in sorted(required_api_key_env_names(model_config)) if not os.environ.get(key)
    ]
    if missing_api_keys:
        raise RuntimeError(
            "请在 .env.local 填写 " + "、".join(missing_api_keys)
            + "；当前配置的模型来源需要这些密钥。"
        )
    refresh_applications()
    if listening(api_port):
        if not api_ready(api_base):
            raise RuntimeError(f"API 端口 {api_port} 已占用，未识别到 seekino API。")
        print(f"[复用] API :{api_port}", flush=True)
    else:
        if owned_pid("api"):
            raise RuntimeError("已有脚本启动的 API 尚未就绪，请查看 api.log。")
        launch("api", [sys.executable, "-u", "-m", "server.api"])
        wait_ready("api", lambda: api_ready(api_base))
    if not library_ready(api_base):
        raise RuntimeError("API 已监听但资料库查询失败，请查看 api.log。")

    web_port = port("SEEKINO_WEB_PORT")
    web_base = f"http://127.0.0.1:{web_port}"
    if listening(web_port):
        page = get(web_base)
        if page is None or b"/@vite/client" not in page or b"/src/main.tsx" not in page:
            raise RuntimeError(f"Web 端口 {web_port} 已占用，未识别到 seekino Vite。")
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
        "API / Parse Consumer": api_ready(f"http://127.0.0.1:{port('SEEKINO_HTTP_PORT')}"),
        "Web": get(f"http://127.0.0.1:{port('SEEKINO_WEB_PORT')}/@vite/client") is not None,
    }
    if settings.elasticsearch is not None:
        config = settings.elasticsearch
        client = create_elasticsearch_client(config, request_timeout_seconds=3)
        try:
            health = client.cluster.health(wait_for_status="yellow", timeout="2s")
            checks["Elasticsearch health"] = (
                not health.get("timed_out") and health.get("status") in {"yellow", "green"}
            )
        except Exception:
            checks["Elasticsearch health"] = False
        finally:
            close_elasticsearch_client(client)
    for name, ready in checks.items():
        print(f"[{'运行' if ready else '未就绪'}] {name}")
    return all(checks.values())


def stop(names: tuple[str, ...] = ("web", "api", "elasticsearch", "mysql")) -> None:
    for name in names:
        pid = owned_pid(name)
        if not pid:
            print(f"[跳过] {name}：没有本脚本管理的存活进程")
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            # The verified owner may exit between inspection and signalling.
            pass
        deadline = time.monotonic() + 15
        while signature(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if owned_pid(name):
            raise RuntimeError(f"{name} 尚未退出，可能正在收尾；稍后重新运行 stop。")
        (STATE / f"{name}.pid.json").unlink(missing_ok=True)
        print(f"[停止] {name} PID={pid}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="懒启动 seekino 全部 localhost 服务。")
    parser.add_argument(
        "command",
        choices=("start", "status", "stop", "_env-names"),
        nargs="?",
        default="start",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--branch", default="main", help="启动来源分支，默认 main")
    args = parser.parse_args()
    if args.command == "_env-names":
        if args.env_file is None:
            raise ValueError("_env-names requires --env-file")
        for name in dotenv_variable_names(args.env_file):
            print(name)
        return 0
    require_worktree_branch(args.branch)
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
        for tool in ("ffmpeg", "ffprobe", "node", "npm", "lsof"):
            binary(tool)
        start_mysql(settings)
        start_elasticsearch(settings)
        initialize_storage()
        start_applications()
        for name, child in CHILDREN.items():
            if child.poll() is not None:
                raise RuntimeError(f"{name} 已退出，请查看 outputs/local-services/{name}.log。")
        print(
            f"\n服务已就绪：\n  Web  http://127.0.0.1:{port('SEEKINO_WEB_PORT')}"
            f"\n  API  http://127.0.0.1:{port('SEEKINO_HTTP_PORT')}/docs"
            f"\n  状态 ./start.sh --branch {args.branch} status"
            f"\n  停止 ./start.sh --branch {args.branch} stop"
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
