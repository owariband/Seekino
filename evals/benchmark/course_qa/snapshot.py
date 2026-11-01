"""Read-only runtime fingerprint and fixture snapshot; never starts services."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse

import httpx
import pymysql
import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--runtime-worktree", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--database-port", type=int, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if urlparse(args.api).hostname not in {"localhost", "127.0.0.1"} or not args.database.endswith("_test"):
        parser.error("This pilot requires an isolated loopback service and a database ending in _test")
    args.out.mkdir(parents=True, exist_ok=False)

    def save(name: str, value) -> None:
        (args.out / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")

    def git(*options: str) -> str:
        return subprocess.check_output(["git", "-C", str(args.runtime_worktree), *options]).decode().strip()

    paths = git("ls-files", "--cached", "--others", "--exclude-standard", "agent", "server", "gateway", "engine", "web/src").splitlines()
    manifest = {
        "captured_at": datetime.now(UTC).isoformat(), "api": args.api,
        "database": args.database, "task_id": args.task_id,
        "runtime_worktree": str(args.runtime_worktree),
        "runtime_head": git("rev-parse", "HEAD"), "runtime_dirty": git("status", "--short"),
        "source_sha256": {path: hashlib.sha256((args.runtime_worktree / path).read_bytes()).hexdigest()
                          for path in paths if (args.runtime_worktree / path).is_file()},
        "loaded_source_identity": "Disk fingerprints do not prove what an older process loaded; verify deployment separately.",
    }
    config_path = args.runtime_dir / "model-config.yaml"
    config = yaml.safe_load(config_path.read_text())
    manifest["config_sha256"] = hashlib.sha256(config_path.read_bytes()).hexdigest()
    # Never serialize sources, URLs, auth or arbitrary binding keys.
    allowed = {"model", "source", "kind", "reasoning_effort", "stream", "temperature", "max_output_tokens", "request_timeout_seconds", "dimensions", "input_mode"}
    manifest["models"] = {role: {k: v for k, v in binding.items() if k in allowed}
                          for role, binding in config.get("models", {}).items()}
    manifest["harness"] = config.get("supervisor_harness")
    process = args.runtime_dir / "api.process.json"
    if process.exists():
        manifest["process_record"] = json.loads(process.read_text())
    with httpx.Client(base_url=args.api, trust_env=False, timeout=30) as client:
        response = client.get("/api/jobs/video-parse/get", params={"tenantId": "local-default", "videoParseTaskId": args.task_id})
        response.raise_for_status()
        save("task", response.json())
    with pymysql.connect(host="127.0.0.1", port=args.database_port, user="root", database=args.database,
                         cursorclass=pymysql.cursors.DictCursor) as connection:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT status,COUNT(*) AS n FROM chunk_parse_task WHERE video_task_id=%s GROUP BY status", (args.task_id,))
            manifest["chunk_statuses"] = cursor.fetchall()
            cursor.execute("SELECT v.sequence_no,c.version,c.content FROM chunk_parse_content c "
                           "JOIN chunk_parse_task t ON t.id=c.chunk_task_id JOIN video_chunk v ON v.id=t.chunk_id "
                           "WHERE t.video_task_id=%s AND v.sequence_no IN (0,1,2) ORDER BY v.sequence_no", (args.task_id,))
            chunks = cursor.fetchall()
            manifest["warm_base_precondition"] = len(chunks) == 3 and all(row["content"] is not None for row in chunks)
            save("chunks", chunks)
    save("manifest", manifest)
    print(json.dumps({key: manifest[key] for key in ("captured_at", "runtime_head", "chunk_statuses", "warm_base_precondition")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
