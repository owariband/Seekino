"""Streamable HTTP transport for local and published Cloud libraries."""

from __future__ import annotations

from ipaddress import ip_address
from contextlib import nullcontext
import json
from typing import Annotated, Any, Literal, TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..error.auth import AuthError
from ..model.dto.editing_template import TemplateDetails, TemplateEvaluation
from ..repository.library import SeriesNotFoundError
from ..service.access import model_query
from .mcp_skills import editing_skill

if TYPE_CHECKING:
    from ..service.auth import AuthService
    from ..service.mcp_retrieval import McpRetrievalService

_ERRORS = {
    "PERSONAL_LIBRARY_REQUIRED": "Copy an editing write token from the account menu and choose a series you can edit.",
    "TEMPLATE_NOT_FOUND": "No accessible series template or version exists.",
    "TEMPLATE_CONFLICT": "Read the current revision and retry. Reuse a request_id only for identical input.",
    "REFERENCE_INDEX_UNAVAILABLE": "Editing-reference search is unavailable on this instance.",
    "INVALID_ARGUMENT": "Check the tool arguments and their documented limits.",
    "EVENT_INDEX_NOT_READY": "The event index is not ready. Finish indexing before retrying.",
    "NO_SEARCHABLE_SERIES": "No searchable series are available in this library.",
    "SERIES_SELECTION_REQUIRED": "Use list_series and choose a smaller set of series_ids.",
    "INVALID_CURSOR": "The publication or filters changed, or the cursor is invalid. Restart from the first page.",
    "EVIDENCE_STALE": "The source changed. Repeat the search or open its current style card.",
    "INVALID_EVIDENCE_REF": "Copy an evidence_ref from a search result, a style-card exemplar or read_evidence.",
    "EVIDENCE_SCOPE_MISMATCH": "This evidence is unavailable in the current library.",
    "INVALID_CLIP_RANGE": "Supply start_ms and end_ms together, with 0 <= start_ms < end_ms <= duration_ms.",
    "CLIP_TOO_LONG": "Cloud clips may be at most 60,000 milliseconds. Choose a shorter range.",
}
_MAX_RESULT_BYTES = 131072
_SMALL_REQUEST = 65536
_TEMPLATE_ENVELOPE = 8 * 1024 * 1024
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RequestId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$",
                                 description="A client-generated unique key; reuse only when retrying the same write.")]


class _RequestSize:
    """Permit larger source files only on the authenticated save operation."""

    def __init__(self, app: ASGIApp, *, cloud: bool):
        self.app, self.cloud = app, cloud

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        identity = scope.get("state", {}).get("mcp_identity")
        personal = not self.cloud or (identity is not None and getattr(identity, "editing_tenant_id", None))
        limit = _TEMPLATE_ENVELOPE if personal else _SMALL_REQUEST
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if len(body) + len(message["body"]) > limit:
                return await JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)(scope, receive, send)
            body.extend(message["body"])
            if not message.get("more_body"):
                break
        if len(body) > _SMALL_REQUEST:
            try:
                rpc = json.loads(body)
                is_save = rpc.get("method") == "tools/call" and rpc["params"].get("name") == "save_edit_template_version"
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                is_save = False
            if not is_save:
                return await JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)(scope, receive, send)

        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


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


class _CloudPublic:
    def __init__(self, app: ASGIApp, *, auth: AuthService | None, origins: dict[str, str]) -> None:
        self.app, self.auth, self.origins = app, auth, origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/mcp":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            if self.auth is None or not self.origins:
                raise AuthError("Cloud MCP 需要身份服务及配置的 HTTPS 来源", status_code=503, code="MCP_UNAVAILABLE")
            hosts = request.headers.getlist("host")
            origin = self.origins.get(hosts[0].lower()) if len(hosts) == 1 else None
            browser_origins = request.headers.getlist("origin")
            if scope.get("scheme") != "https" or not origin or (browser_origins and browser_origins != [origin]):
                raise AuthError("请求来源不可用", status_code=403, code="ORIGIN_DENIED")
            authorization = request.headers.getlist("authorization")
            scheme, _, token = authorization[0].partition(" ") if len(authorization) == 1 else ("", "", "")
            if scheme.lower() != "bearer" or not token:
                raise AuthError("请先通过 /api/auth/mcp-token 获取 MCP 凭据")
            request.state.mcp_identity = await run_in_threadpool(self.auth.authenticate_mcp, token)
            request.state.mcp_token = token
            request.state.mcp_base_url = origin
        except AuthError as error:
            headers = {"WWW-Authenticate": 'Bearer realm="Seekino MCP"'} if error.status_code == 401 else {}
            await JSONResponse({"error": error.code, "message": str(error)}, status_code=error.status_code,
                               headers=headers)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_mcp_app(service: McpRetrievalService, *, auth_mode: str, auth: AuthService | None = None,
                   allowed_origins: tuple[str, ...] = (), maximum_concurrent: int | None = None) -> Starlette:
    """Build /mcp; compose this app's router.lifespan_context into the host lifespan."""
    server = MCPServer(
        "seekino",
        extensions=[editing_skill()],
        instructions=(
            "For editing, read skill://seekino-editing-materials/SKILL.md with resources/read. "
            "It explains shot selection, style and motion references, local cutouts and saving feedback. "
            "Read skill://video-shotcraft/SKILL.md for Remotion shot recipes and local installation, and "
            "skill://motion-showreel/SKILL.md for code-driven graphic, type or UI animation and supported layer compositing. "
            "Use the upstream guides when they fit the task; each keeps its own scope, workflow and confirmation steps. "
            "The server publishes these guides; author and render in the local editing project. "
            "Templates belong to series. All series viewers share versions and feedback. Editors with a write-capable credential can contribute. "
            "Find source performances with retrieve. For editing techniques and shot changes, use find_edit_templates, "
            "play its media.play_url and inspect the source frames. get_style_card returns a whole-film card "
            "with an evidence_ref for every exemplar. Read those references with read_evidence. Results are indexed evidence, "
            "not proof of unrecorded visual details. Use read_evidence for current source "
            "context and media URLs. All video times are integer milliseconds; intervals "
            "include start_ms and exclude end_ms. For editing, collect different candidate shots, "
            "try the character’s names and observable cues, and watch the source before choosing. "
            "When a gesture or expression change carries the scene, watch the lead-in and follow-through. "
            "Call read_evidence with your chosen start_ms and end_ms and download media.clip_url. "
            "Use the continuous footage as is, or keep its motion with your own local cutout tools. "
            "A still can emphasize a chosen beat. Ranked results are only a shortlist. "
            "Use browse_events for every published event in "
            "a series or every tagged appearance of a character. Follow next_cursor through the final page."
        ),
    )
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    writes = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    async def invoke(ctx: Context, method, **arguments: Any) -> dict[str, Any]:
        request = ctx.request_context.request

        def execute():
            if auth_mode != "cloud":
                return method(**arguments)
            viewer = request.state.mcp_identity
            budget = (model_query(auth, viewer, request.client.host if request.client else "unknown",
                                  maximum_concurrent=maximum_concurrent)
                      if method == service.retrieve else nullcontext())
            with budget:
                result = method(**arguments, viewer=viewer)
            auth.authenticate_mcp(request.state.mcp_token)
            return result

        try:
            result = await run_in_threadpool(execute)
        except SeriesNotFoundError:
            raise ToolError("EVIDENCE_SCOPE_MISMATCH: This series is unavailable in the public library.") from None
        except AuthError as exc:
            raise ToolError(f"{exc.code}: {exc}") from None
        except ValidationError:
            raise ToolError("INVALID_ARGUMENT: Check the template and note fields in the tool schema.") from None
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            if code in _ERRORS:
                raise ToolError(f"{code}: {_ERRORS[code]}") from None
            raise ToolError("RETRIEVAL_FAILED: Unable to complete this request.") from None
        except Exception:
            raise ToolError("RETRIEVAL_FAILED: Unable to complete this request.") from None
        result = dict(result)
        for item in ([result] if "media" in result else result.get("techniques", [])):
            media = dict(item["media"])
            base_url = request.state.mcp_base_url
            for key in ("original_url", "playback_url", "clip_url"):
                value = media.get(key)
                if value is not None:
                    parsed = urlsplit(value)
                    path = "/api/videos/clip" if key == "clip_url" else "/api/videos/content"
                    if (parsed.scheme or parsed.netloc or parsed.path != path or parsed.fragment
                            or (auth_mode == "cloud" and key != "clip_url")):
                        raise ToolError("RETRIEVAL_FAILED: Invalid media reference.")
                    if auth_mode == "cloud":
                        ticket = auth.issue_clip_ticket(request.state.mcp_token,
                                                        item.get("evidence_ref") or arguments["evidence_ref"],
                                                        media["start_ms"], media["end_ms"])
                        value += "&" + urlencode({"ticket": ticket})
                    media[key] = base_url + value
            if "segment_id" in item:
                media["play_url"] = media["clip_url"] + "&inline=true" if media.get("clip_url") else None
            item["media"] = media
        result_limit = _TEMPLATE_ENVELOPE if method == getattr(service, "get_edit_template", None) else _MAX_RESULT_BYTES
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > result_limit:
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
        """List accessible video series and their indexing status; use IDs to narrow retrieve."""
        return await invoke(ctx, service.list_series, offset=offset, limit=limit)

    @server.tool(annotations=annotations, structured_output=True)
    async def retrieve(
        query: Annotated[str, Field(min_length=1, max_length=4000,
                                   description="Describe the scene, action or dialogue to find in source videos.")],
        ctx: Context,
        series_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]] | None, Field(
            min_length=1, max_length=50, description="Optional IDs from list_series; omit to search accessible READY series.")] = None,
        top_k: Annotated[int, Field(ge=1, le=20, strict=True,
                                   description="Maximum distinct parent chunks; each may have multiple matching events.")] = 5,
        event_types: Annotated[list[Literal["SCENE", "ACTION", "DIALOGUE", "EMOTION", "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER"]] | None, Field(
            max_length=7, description="Optional Timeline event categories to include.")] = None,
        speaker: Annotated[str | None, Field(min_length=1, max_length=255,
                                            description="Filter reliable DIALOGUE speakers by name, not visible people.")] = None,
        cursor: Annotated[str | None, Field(min_length=1, max_length=512,
                                            description="Replay the same search with next_cursor to continue.")] = None,
    ) -> dict[str, Any]:
        """Retrieve event evidence by natural language, dialogue or action. top_k counts distinct chunks,
        not events. Omit series_ids to search the accessible library. Read returned evidence_ref
        with read_evidence for verified source context and downloadable media. Follow next_cursor for more
        ranked hits. For all candidate shots in a series, use browse_events. Search camera moves by
        observed direction, pace or reveal. A matching SCENE can carry a camera_ref. Times are milliseconds.
        """
        return await invoke(
            ctx, service.retrieve, query=query, series_ids=series_ids, top_k=top_k,
            event_types=event_types, speaker=speaker, cursor=cursor,
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def find_edit_templates(
        query: Annotated[str, Field(min_length=1, max_length=2000)], ctx: Context,
        series_ids: Annotated[list[str] | None, Field(min_length=1, max_length=32)] = None,
        tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]] | None, Field(max_length=16)] = None,
        include_parse_techniques: bool = True,
        top_k: Annotated[int, Field(ge=1, le=8, strict=True)] = 5,
    ) -> dict[str, Any]:
        """Find editing techniques by natural language. Each source-checked observation has original
        start/end milliseconds, shot steps and media.play_url for that interval. Choose whether to
        borrow it after watching the original. series_ids scope source footage. tags filter series-shared templates.
        Set include_parse_techniques=false to search only your template library."""
        return await invoke(ctx, service.find_edit_templates, query=query, series_ids=series_ids, tags=tags,
                            include_parse_techniques=include_parse_techniques, top_k=top_k)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_style_card(video_id: Annotated[str, Field(min_length=1, max_length=64)], ctx: Context) -> dict[str, Any]:
        """Read the current whole-video Refine card and every exemplar. Each exemplar has a source
        range and an evidence_ref accepted by read_evidence for playback and download."""
        return await invoke(ctx, service.get_style_card, video_id=video_id)

    @server.tool(annotations=annotations, structured_output=True)
    async def browse_events(
        series_id: Annotated[str, Field(min_length=1, max_length=64, description="An accessible ID from list_series.")],
        ctx: Context,
        visible_names: Annotated[list[Annotated[str, Field(min_length=1, max_length=255)]] | None, Field(
            min_length=1, max_length=32, description="Any of these on-screen names. Copy character names from "
            "event.visible_persons and include known aliases. Omit to browse every event.")] = None,
        limit: Annotated[int, Field(ge=1, le=50, strict=True, description="Maximum cards on this page.")] = 24,
        cursor: Annotated[str | None, Field(min_length=1, max_length=512,
                                            description="Pass the previous next_cursor with the same filters.")] = None,
    ) -> dict[str, Any]:
        """Enumerate all published source events in episode order, independent of ranked search.
        For a character shot, inspect several candidates through read_evidence and review source frames.
        Names filter recorded on-screen appearances; unambiguous aliases declared in the series background
        are included. Use the stored spelling from a ranked hit for ambiguous or undocumented aliases.
        Continue through empty pages until next_cursor is null. Omit the name filter to include untagged shots.
        """
        return await invoke(ctx, service.browse_events, series_id=series_id, visible_names=visible_names,
                            limit=limit, cursor=cursor)

    @server.tool(annotations=annotations, structured_output=True)
    async def read_evidence(
        evidence_ref: Annotated[str, Field(min_length=1, max_length=4096,
                                          description="Copy an evidence_ref or next_evidence_ref exactly; do not construct one.")],
        ctx: Context,
        start_ms: Annotated[int | None, Field(ge=0, strict=True, description="Optional inclusive start on the source video, "
                                             "in milliseconds. Supply both bounds to download a clip.")] = None,
        end_ms: Annotated[int | None, Field(gt=0, strict=True, description="Exclusive source end in milliseconds. "
                                           "May span adjacent events in the same video; Cloud limit: 60,000 ms per clip.")] = None,
    ) -> dict[str, Any]:
        """Read current evidence from retrieve, browse_events, find_edit_templates or get_style_card.
        For an editing reference, omitting bounds returns the measured source clip, note and native-frame evidence. Follow
        next_evidence_ref to continue bounded context. A stale reference requires a new retrieval.
        Omit both bounds to review source context. Supply both to get a downloadable MP4 at media.clip_url
        for your chosen source range, with its original motion and audio when present.
        For a matched camera_ref, camera_replay contains an editable ES module and params for supported
        continuous 2-D moves. Anchors are creative defaults; verify the observed framing in matched_event.
        """
        return await invoke(ctx, service.read_evidence, evidence_ref=evidence_ref,
                            start_ms=start_ms, end_ms=end_ms, cloud=auth_mode == "cloud")

    @server.tool(annotations=annotations, structured_output=True)
    async def get_edit_template(template_id: str, ctx: Context, version_id: str | None = None) -> dict[str, Any]:
        """Read saved code, source snapshots, evaluations and checks shared by a series you can view. Without a version ID,
        return the explicit default or list versions if no default is selected."""
        return await invoke(ctx, service.get_edit_template, template_id=template_id, version_id=version_id)

    @server.tool(annotations=annotations, structured_output=True)
    async def list_edit_template_versions(template_id: str, ctx: Context) -> dict[str, Any]:
        """List shared revisions, lineage and the default of a template in a series you can view."""
        return await invoke(ctx, service.list_edit_template_versions, template_id=template_id)

    @server.tool(annotations=writes, structured_output=True)
    async def save_edit_template_version(
        ctx: Context, request_id: RequestId, spec: dict, visual_requirements: dict | list, slots: list | dict,
        params_schema: dict, files: dict[str, str], entry: str, runtime: dict, evaluation: TemplateEvaluation,
        template_id: str | None = None, new_template: TemplateDetails | None = None, parent_version_id: str | None = None,
        series_id: Annotated[str | None, Field(min_length=1, max_length=64, description="Required for a new template. All viewers of this series share the saved version.")] = None,
        source_refs: Annotated[list[Annotated[str, Field(min_length=1, max_length=4096)]], Field(max_length=16)] = [],
        check: dict | None = None,
    ) -> dict[str, Any]:
        """Save a revision in a series you can edit. For a new template supply series_id and new_template; for a revision supply template_id. new_template is
        {name, description, tags}. files is a map of relative paths to text (1 MiB total); entry names one.
        Include source evidence_refs from this connection. For a parent use evaluation.kind=revision_rationale
        and describe changes. A save never selects a default. Reuse request_id on retry."""
        return await invoke(ctx, service.save_edit_template_version, request_id=request_id, template_id=template_id,
                            new_template=new_template.model_dump() if new_template else None, parent_version_id=parent_version_id, series_id=series_id, spec=spec,
                            visual_requirements=visual_requirements, slots=slots, params_schema=params_schema,
                            files=files, entry=entry, runtime=runtime, source_refs=source_refs,
                            evaluation=evaluation.model_dump(exclude_none=True), check=check)

    @server.tool(annotations=writes, structured_output=True)
    async def append_edit_template_note(ctx: Context, version_id: str, kind: Literal["evaluation", "check"],
                                        entry: dict, request_id: RequestId) -> dict[str, Any]:
        """Append attributed usage feedback or a client-reported render check to your chosen version.
        Separate user_feedback, agent_judgment, changes, verified and unverified. Reuse request_id on retry."""
        return await invoke(ctx, service.append_edit_template_note, version_id=version_id, kind=kind,
                            entry=entry, request_id=request_id)

    @server.tool(annotations=writes, structured_output=True)
    async def set_default_edit_template_version(ctx: Context, template_id: str, version_id: str,
                                                revision: Annotated[int, Field(ge=0)], reason: str,
                                                request_id: RequestId) -> dict[str, Any]:
        """Select the default explicitly. Supply the template's current revision and your selection reason."""
        return await invoke(ctx, service.set_default_edit_template_version, template_id=template_id,
                            version_id=version_id, revision=revision, reason=reason, request_id=request_id)

    public_origins = {}
    for value in allowed_origins:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname and "*" not in parsed.netloc and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in {"", "/"}):
            public_origins[parsed.netloc.lower()] = "https://" + parsed.netloc.lower()
    local_hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
    cloud = auth_mode == "cloud"
    app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        max_request_body_size=_TEMPLATE_ENVELOPE,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(public_origins) if cloud else local_hosts,
            allowed_origins=list(public_origins.values()) if cloud else [
                f"{scheme}://{host}" for scheme in ("http", "https") for host in local_hosts],
        ),
    )
    app.add_middleware(_RequestSize, cloud=cloud)
    if cloud:
        app.add_middleware(_CloudPublic, auth=auth, origins=public_origins)
    else:
        app.add_middleware(_LocalOnly, auth_mode=auth_mode)
    return app
