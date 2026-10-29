"""Local MCP transport over the existing retrieval services."""

from __future__ import annotations

from ipaddress import ip_address
import json
from typing import Annotated, Any, Literal, TYPE_CHECKING
from urllib.parse import urlsplit

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from ..service.mcp_retrieval import McpRetrievalService

_ERRORS = {
    "INVALID_ARGUMENT": "Check the tool arguments and their documented limits.",
    "EVENT_INDEX_NOT_READY": "The event index is not ready. Finish indexing before retrying.",
    "NO_SEARCHABLE_SERIES": "No searchable series are available in this local library.",
    "SERIES_SELECTION_REQUIRED": "Use list_series and choose a smaller set of series_ids.",
    "EVIDENCE_STALE": "The source changed. Run retrieve again for current evidence.",
    "INVALID_EVIDENCE_REF": "Use an evidence_ref returned by retrieve or read_evidence.",
    "EVIDENCE_SCOPE_MISMATCH": "This evidence is unavailable in the current library.",
}
_MAX_RESULT_BYTES = 131072
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _LocalOnly:
    def __init__(self, app: ASGIApp, *, auth_mode: str) -> None:
        self.app = app
        self.auth_mode = auth_mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.auth_mode != "local":
            await JSONResponse({"error": "MCP_NOT_AVAILABLE"}, status_code=404)(scope, receive, send)
            return
        try:
            client = scope.get("client")
            if not client or not ip_address(client[0]).is_loopback:
                raise ValueError
            headers = scope["headers"]
            if any(name.lower() == b"forwarded" or name.lower().startswith(b"x-forwarded-") for name, _ in headers):
                raise ValueError
            hosts = [value.decode("ascii") for name, value in headers if name.lower() == b"host"]
            origins = [value.decode("ascii") for name, value in headers if name.lower() == b"origin"]
            if len(hosts) != 1 or len(origins) > 1:
                raise ValueError
            authority = hosts[0]
            parsed = urlsplit("//" + authority)
            if (parsed.hostname not in _LOCAL_HOSTS or parsed.username is not None
                    or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                    or any(character.isspace() for character in authority)):
                raise ValueError
            # Accessing port validates its syntax and range.
            if parsed.port == 0 or authority.endswith(":"):
                raise ValueError
            scheme = scope.get("scheme", "http")
            if scheme not in {"http", "https"}:
                raise ValueError
            base_url = f"{scheme}://{authority}"
            if origins and origins[0] != base_url:
                raise ValueError
        except (ValueError, UnicodeError):
            await JSONResponse({"error": "MCP_LOCAL_CONNECTION_REQUIRED"}, status_code=403)(scope, receive, send)
            return
        scope.setdefault("state", {})["mcp_base_url"] = base_url
        await self.app(scope, receive, send)


def create_mcp_app(service: McpRetrievalService, *, auth_mode: str) -> Starlette:
    """Build /mcp; compose this app's router.lifespan_context into the host lifespan."""
    server = MCPServer(
        "seekino",
        instructions=(
            "Search the local video library using retrieve. Results are indexed evidence, "
            "not proof of unrecorded visual details. Use read_evidence for current source "
            "context and media URLs. All video times are integer milliseconds; intervals "
            "include start_ms and exclude end_ms."
        ),
    )
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

    async def invoke(ctx: Context, method, **arguments: Any) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(method, **arguments)
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            if code in _ERRORS:
                raise ToolError(f"{code}: {_ERRORS[code]}") from None
            raise ToolError("RETRIEVAL_FAILED: Unable to complete this request.") from None
        except Exception:
            raise ToolError("RETRIEVAL_FAILED: Unable to complete this request.") from None
        result = dict(result)
        if "media" in result:
            media = dict(result["media"])
            request = ctx.request_context.request
            base_url = request.state.mcp_base_url
            for key in ("original_url", "playback_url"):
                value = media.get(key)
                if value is not None:
                    parsed = urlsplit(value)
                    if parsed.scheme or parsed.netloc or parsed.path != "/api/videos/content" or parsed.fragment:
                        raise ToolError("RETRIEVAL_FAILED: Invalid media reference.")
                    media[key] = base_url + value
            result["media"] = media
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ToolError("RESULT_TOO_LARGE: Narrow the query or request fewer results.")
        return result

    @server.tool(annotations=annotations, structured_output=True)
    async def list_series(
        ctx: Context,
        offset: Annotated[int, Field(ge=0, le=1_000_000, strict=True,
                                    description="Zero-based series offset; use next_offset to continue.")] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True,
                                   description="Maximum number of series to return in this page.")] = 20,
    ) -> dict[str, Any]:
        """List accessible local video series and their indexing status; use IDs to narrow retrieve."""
        return await invoke(ctx, service.list_series, offset=offset, limit=limit)

    @server.tool(annotations=annotations, structured_output=True)
    async def retrieve(
        query: Annotated[str, Field(min_length=1, max_length=4000,
                                   description="Describe the scene, action or dialogue to find in source videos.")],
        ctx: Context,
        series_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]] | None, Field(
            min_length=1, max_length=50, description="Optional IDs from list_series; omit to search local READY series.")] = None,
        top_k: Annotated[int, Field(ge=1, le=20, strict=True,
                                   description="Maximum distinct parent chunks; each may have multiple matching events.")] = 5,
        event_types: Annotated[list[Literal["SCENE", "ACTION", "DIALOGUE", "EMOTION", "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER"]] | None, Field(
            max_length=7, description="Optional Timeline event categories to include.")] = None,
        speaker: Annotated[str | None, Field(min_length=1, max_length=255,
                                            description="Filter reliable DIALOGUE speakers by name, not visible people.")] = None,
    ) -> dict[str, Any]:
        """Retrieve event evidence by natural language, dialogue or action. top_k counts distinct chunks,
        not events. Omit series_ids to search the accessible local library. Read returned evidence_ref
        with read_evidence for verified source context and downloadable media. Times are milliseconds.
        """
        return await invoke(
            ctx, service.retrieve, query=query, series_ids=series_ids, top_k=top_k,
            event_types=event_types, speaker=speaker,
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def read_evidence(
        evidence_ref: Annotated[str, Field(min_length=1, max_length=4096,
                                          description="Copy an evidence_ref or next_evidence_ref exactly; do not construct one.")],
        ctx: Context,
    ) -> dict[str, Any]:
        """Read current evidence and media for an opaque reference returned by retrieve. Follow
        next_evidence_ref to continue bounded context. A stale reference requires a new retrieval.
        Media URLs serve source videos; use the returned millisecond boundaries when clipping.
        """
        return await invoke(ctx, service.read_evidence, evidence_ref=evidence_ref)

    app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        max_request_body_size=65536,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"],
            allowed_origins=[f"{scheme}://{host}" for scheme in ("http", "https")
                             for host in ("localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*")],
        ),
    )
    app.add_middleware(_LocalOnly, auth_mode=auth_mode)
    return app
