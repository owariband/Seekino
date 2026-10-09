from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from threading import Lock

import httpx

from gateway.config import TracingConfig, _tracing_base_url

from ..model.dto.parseUsageDTO import UsageObservationBatch


class LangfuseQueryError(RuntimeError):
    """The configured Langfuse project could not provide scoped usage rows."""


class LangfuseObservationStore:
    """Read model observations from Langfuse with bounded work and shared caching."""

    def __init__(
        self,
        settings: TracingConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._client: httpx.Client | None = None
        self._lock = Lock()
        self._cache: OrderedDict[tuple[str, datetime, datetime], tuple[float, UsageObservationBatch]] = OrderedDict()
        self._closed = False
        self.status = "disabled" if not settings.enabled else "not_configured"
        values = os.environ if environ is None else environ
        base_url = settings.base_url or values.get("LANGFUSE_BASE_URL")
        public_key = values.get(settings.public_key_env)
        secret_key = values.get(settings.secret_key_env)
        if settings.enabled and base_url and public_key and secret_key:
            try:
                base_url = _tracing_base_url(base_url)
            except ValueError:
                return
            self._client = httpx.Client(
                base_url=base_url + "/",
                auth=httpx.BasicAuth(public_key, secret_key),
                timeout=settings.export_timeout_seconds,
                transport=transport,
                follow_redirects=False,
            )
            self.status = "available"

    def query(
        self,
        *,
        tenant_id: str,
        from_start_time: datetime,
        to_start_time: datetime,
    ) -> UsageObservationBatch:
        if not tenant_id.strip() or len(tenant_id) > 64:
            raise ValueError("tenant_id must contain 1 to 64 characters")
        if (
            from_start_time.utcoffset() is None
            or to_start_time.utcoffset() is None
            or not 0 < (to_start_time - from_start_time).total_seconds() <= 31 * 86400
        ):
            raise ValueError("Langfuse usage requires an aware, bounded time window")
        if self._client is None or self._closed:
            raise LangfuseQueryError("Langfuse 用量查询尚未配置或已关闭")
        key = (tenant_id, from_start_time, to_start_time)
        if not self._lock.acquire(timeout=self._settings.export_timeout_seconds):
            raise LangfuseQueryError("Langfuse 用量查询繁忙，请稍后重试")
        try:
            cached = self._cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < 30:
                self._cache.move_to_end(key)
                return cached[1]
            result = self._read(tenant_id, from_start_time, to_start_time)
            self._cache[key] = (time.monotonic(), result)
            self._cache.move_to_end(key)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
            return result
        finally:
            self._lock.release()

    def _read(
        self, tenant_id: str, from_start_time: datetime, to_start_time: datetime,
    ) -> UsageObservationBatch:
        assert self._client is not None
        # Advanced filters replace the fixed parameters in the v2 API. Include
        # every scope condition here so pagination cannot widen the request.
        filters = [
            {"type": "stringObject", "column": "metadata", "key": "tenant_id", "operator": "=", "value": tenant_id},
            {"type": "datetime", "column": "startTime", "operator": ">=", "value": from_start_time.isoformat()},
            {"type": "datetime", "column": "startTime", "operator": "<", "value": to_start_time.isoformat()},
            {"type": "stringOptions", "column": "environment", "operator": "any of", "value": [self._settings.environment]},
            {"type": "stringOptions", "column": "type", "operator": "any of", "value": ["GENERATION", "EMBEDDING"]},
        ]
        params = {
            "filter": json.dumps(filters),
            "fields": "core,basic,metadata,model,usage",
            "expandMetadata": "usage_subsets",
            "limit": "1000",
        }
        rows: list[dict] = []
        cursors: set[str] = set()
        deadline = time.monotonic() + 15
        for _ in range(20):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return UsageObservationBatch(tuple(rows), truncated=True)
            try:
                response = self._client.get(
                    "api/public/v2/observations", params=params,
                    timeout=min(remaining, self._settings.export_timeout_seconds),
                )
                response.raise_for_status()
                payload = response.json(parse_float=Decimal)
            except (httpx.HTTPError, ValueError) as error:
                # Do not expose a response body, URL or credential in API errors.
                raise LangfuseQueryError("Langfuse 用量查询失败，请检查项目连接和读取权限") from error
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise LangfuseQueryError("Langfuse 返回了无法识别的用量结果")
            for row in payload["data"]:
                if not isinstance(row, dict):
                    raise LangfuseQueryError("Langfuse 返回了无法识别的用量记录")
                metadata = row.get("metadata")
                if not isinstance(metadata, dict) or metadata.get("tenant_id") != tenant_id:
                    raise LangfuseQueryError("Langfuse 返回的用量记录与请求范围不一致")
                rows.append(row)
            meta = payload.get("meta", {})
            if not isinstance(meta, dict):
                raise LangfuseQueryError("Langfuse 返回了无法识别的分页结果")
            cursor = meta.get("cursor")
            if cursor is None:
                return UsageObservationBatch(tuple(rows), truncated=False)
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                return UsageObservationBatch(tuple(rows), truncated=True)
            cursors.add(cursor)
            params["cursor"] = cursor
        return UsageObservationBatch(tuple(rows), truncated=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cache.clear()
            if self._client is not None:
                self._client.close()
