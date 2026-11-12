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
            "用场景描述找到可核对的原片、出处和剪辑参考，按用户当前任务取用。"
            "需要制作成片时，在本机编写镜头并渲染。"
            "剪辑前通过 resources/read 读取 skill://seekino-editing-materials/SKILL.md，"
            "其中说明选片、风格参考、运动建议、本机分层和经验写回。"
            "Remotion 镜头配方与完整安装方法见 skill://video-shotcraft/SKILL.md；"
            "代码驱动的图形、文字、界面动画及支持的分层合成见 skill://motion-showreel/SKILL.md。"
            "按任务查阅制作参考，遵循用户已明确的要求与授权，沿用各指引的适用范围、工作流和确认条件。"
            "服务端提供指引与素材，代码编写和渲染在本机完成。"
            "用 retrieve 找人物表演，结合人物名字的不同写法和可观察的动作，比较多个候选。"
            "用 find_edit_templates 找剪辑技法与镜头变化，打开 media.play_url 并检查原帧。"
            "get_style_card 给出整片风格卡和每个例证的 evidence_ref，再用 read_evidence 核对例证。"
            "索引命中只提供线索；未记录的画面细节要看原片，当前上下文和媒体入口由 read_evidence 返回。"
            "动作或表情变化承担情节时，检查它的准备、发生与收尾，再确定取片区间。"
            "剧情剪辑按剧本所需的神情与动作变化比较候选，保留人物一段时间内的连续表演。"
            "需要换景或合成时，在本机制作时间对齐的动态人物层，检查是否始终保留同一人物、边缘是否完整、前后动作是否衔接。"
            "视频时间都是整数毫秒，区间包含 start_ms、不包含 end_ms。"
            "将自行确定的 start_ms 和 end_ms 交给 read_evidence，及时下载 media.clip_url。"
            "可直接使用连续原片，也可在本机分层后保留其运动；有意停住某个节拍时可以使用静帧。"
            "排序结果只是候选；需要查全一个系列或某个人物已标注的出镜事件时，用 browse_events，"
            "跟随 next_cursor 直到最后一页。"
            "模板归属 series，所有有权查看该 series 的人共享版本与反馈；"
            "编辑者使用可写凭据，在已有编辑权限内保存经验。"
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
                                    description="从 0 开始的系列偏移量；继续翻页时传入返回的 next_offset。")] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True,
                                   description="本页最多返回多少个系列。")] = 20,
    ) -> dict[str, Any]:
        """查看可访问的系列及索引状态，取得准确的系列 ID 后缩小 retrieve 的搜索范围。"""
        return await invoke(ctx, service.list_series, offset=offset, limit=limit)

    @server.tool(annotations=annotations, structured_output=True)
    async def retrieve(
        query: Annotated[str, Field(min_length=1, max_length=4000,
                                   description="描述要找的场景、动作或对白，尽量写出画面中可观察的变化。")],
        ctx: Context,
        series_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]] | None, Field(
            min_length=1, max_length=50, description="可选，使用 list_series 返回的 ID；省略时搜索可访问的 READY 系列。")] = None,
        top_k: Annotated[int, Field(ge=1, le=20, strict=True,
                                   description="最多召回多少个不同的父 Chunk；每个 Chunk 可以包含多个匹配事件。")] = 5,
        event_types: Annotated[list[Literal["SCENE", "ACTION", "DIALOGUE", "EMOTION", "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER"]] | None, Field(
            max_length=7, description="可选，限定返回的 Timeline 事件类型。")] = None,
        speaker: Annotated[str | None, Field(min_length=1, max_length=255,
                                            description="按可靠的 DIALOGUE 说话人姓名筛选；此项不筛选画面出镜人物。")] = None,
        cursor: Annotated[str | None, Field(min_length=1, max_length=512,
                                            description="继续同一次检索时，保持其他条件不变，传入返回的 next_cursor。")] = None,
    ) -> dict[str, Any]:
        """按自然语言、对白或动作寻找原片候选。top_k 计算不同 Chunk 的数量，一个 Chunk 可有多个事件。
        省略 series_ids 时搜索可访问的素材库。把返回的 evidence_ref 交给 read_evidence，核对上下文并取片；
        用 next_cursor 继续查看排序结果，查全系列候选则用 browse_events。
        剧情表演先按神情与动作变化寻找候选，再回看整段，按剧本判断节奏与衔接。
        找运镜时描述方向、速度变化或露出了什么；命中的 SCENE 可能带有 camera_ref。时间单位为毫秒。
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
        """按画面变化找可借鉴的技法，同时检索可见 series 的共享模板。
        技法观察已经回源核对，包含原片毫秒区间、镜头步骤和 media.play_url；回看后再判断是否适合当前片子。
        series_ids 限定来源系列，tags 筛选共享模板。设 include_parse_techniques=false 时只查可见的模板库。"""
        return await invoke(ctx, service.find_edit_templates, query=query, series_ids=series_ids, tags=tags,
                            include_parse_techniques=include_parse_techniques, top_k=top_k)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_style_card(video_id: Annotated[str, Field(min_length=1, max_length=64)], ctx: Context) -> dict[str, Any]:
        """查看当前整片 Refine 风格卡及全部例证，了解反复出现的风格特征。
        每个例证都有原片区间和 evidence_ref，可交给 read_evidence 回看或下载。"""
        return await invoke(ctx, service.get_style_card, video_id=video_id)

    @server.tool(annotations=annotations, structured_output=True)
    async def browse_events(
        series_id: Annotated[str, Field(min_length=1, max_length=64, description="使用 list_series 返回的可访问系列 ID。")],
        ctx: Context,
        visible_names: Annotated[list[Annotated[str, Field(min_length=1, max_length=255)]] | None, Field(
            min_length=1, max_length=32, description="匹配任一出镜人物；采用 event.visible_persons 中记录的人名，"
            "可带已知别名。省略时浏览全部事件。")] = None,
        limit: Annotated[int, Field(ge=1, le=50, strict=True, description="本页最多返回多少张事件卡。")] = 24,
        cursor: Annotated[str | None, Field(min_length=1, max_length=512,
                                            description="保持筛选条件不变，传入上一页的 next_cursor。")] = None,
    ) -> dict[str, Any]:
        """按集数顺序浏览全部已发布事件，不依赖检索排序。找人物镜头时比较多个候选，用 read_evidence 核对原帧。
        人名筛选依据已记录的出镜信息，包含系列背景中无歧义的别名；别名有歧义或未声明时，采用命中结果中的实际写法。
        遇到空页也继续翻页，直到 next_cursor 为 null；省略人名筛选可以看到未标注人物的镜头。
        """
        return await invoke(ctx, service.browse_events, series_id=series_id, visible_names=visible_names,
                            limit=limit, cursor=cursor)

    @server.tool(annotations=annotations, structured_output=True)
    async def read_evidence(
        evidence_ref: Annotated[str, Field(min_length=1, max_length=4096,
                                          description="原样使用返回的 evidence_ref 或 next_evidence_ref，不自行拼接引用。")],
        ctx: Context,
        start_ms: Annotated[int | None, Field(ge=0, strict=True, description="可选，源视频中的起点毫秒，包含此时刻；"
                                             "取片时同时提供起点和终点。")] = None,
        end_ms: Annotated[int | None, Field(gt=0, strict=True, description="源视频中的终点毫秒，不包含此时刻；"
                                           "可跨同一视频内的相邻事件，Cloud 每段最多 60,000 毫秒。")] = None,
    ) -> dict[str, Any]:
        """核对 retrieve、browse_events、find_edit_templates 或 get_style_card 返回的当前来源。
        不传起止时间时读取上下文；剪辑参考会带回实测原片区间、笔记和原帧证据。用 next_evidence_ref 继续查看有界上下文，
        引用过期则重新检索。同时提供起止时间后，从 media.clip_url 下载所选区间的 MP4，保留原有运动及存在的音轨。
        命中 camera_ref 时，camera_replay 为受支持的连续二维运镜提供可编辑 ES 模块和参数。
        锚点是创作默认值，实际构图仍要核对 matched_event。
        """
        return await invoke(ctx, service.read_evidence, evidence_ref=evidence_ref,
                            start_ms=start_ms, end_ms=end_ms, cloud=auth_mode == "cloud")

    @server.tool(annotations=annotations, structured_output=True)
    async def get_edit_template(template_id: str, ctx: Context, version_id: str | None = None) -> dict[str, Any]:
        """读取可见 series 共享的代码、来源快照、评价和检查记录。
        未指定版本时返回明确设定的默认版；没有默认版则列出版本，供你确定要用的一版。"""
        return await invoke(ctx, service.get_edit_template, template_id=template_id, version_id=version_id)

    @server.tool(annotations=annotations, structured_output=True)
    async def list_edit_template_versions(template_id: str, ctx: Context) -> dict[str, Any]:
        """查看可见 series 中模板的共享版本、派生关系和默认版本。"""
        return await invoke(ctx, service.list_edit_template_versions, template_id=template_id)

    @server.tool(annotations=writes, structured_output=True)
    async def save_edit_template_version(
        ctx: Context, request_id: RequestId, spec: dict, visual_requirements: dict | list, slots: list | dict,
        params_schema: dict, files: dict[str, str], entry: str, runtime: dict, evaluation: TemplateEvaluation,
        template_id: str | None = None, new_template: TemplateDetails | None = None, parent_version_id: str | None = None,
        series_id: Annotated[str | None, Field(min_length=1, max_length=64, description="新模板必填；所有有权查看该 series 的人共享保存的版本。")] = None,
        source_refs: Annotated[list[Annotated[str, Field(min_length=1, max_length=4096)]], Field(max_length=16)] = [],
        check: dict | None = None,
    ) -> dict[str, Any]:
        """把可复用实现保存到有编辑权限的 series。新模板提供 series_id 和 new_template，修订已有模板提供 template_id。
        new_template 包含 {name, description, tags}；files 是相对路径到文本的映射，总计最多 1 MiB，entry 指向其中一个文件。
        来源 evidence_refs 必须来自本连接。基于父版本修改时，用 evaluation.kind=revision_rationale 说明变化。
        保存不会设置默认版本；同一次写入重试时沿用 request_id。"""
        return await invoke(ctx, service.save_edit_template_version, request_id=request_id, template_id=template_id,
                            new_template=new_template.model_dump() if new_template else None, parent_version_id=parent_version_id, series_id=series_id, spec=spec,
                            visual_requirements=visual_requirements, slots=slots, params_schema=params_schema,
                            files=files, entry=entry, runtime=runtime, source_refs=source_refs,
                            evaluation=evaluation.model_dump(exclude_none=True), check=check)

    @server.tool(annotations=writes, structured_output=True)
    async def append_edit_template_note(ctx: Context, version_id: str, kind: Literal["evaluation", "check"],
                                        entry: dict, request_id: RequestId) -> dict[str, Any]:
        """为指定版本追加有署名的使用反馈或客户端渲染检查。
        分别记录 user_feedback、agent_judgment、changes、verified 和 unverified；同一次写入重试时沿用 request_id。"""
        return await invoke(ctx, service.append_edit_template_note, version_id=version_id, kind=kind,
                            entry=entry, request_id=request_id)

    @server.tool(annotations=writes, structured_output=True)
    async def set_default_edit_template_version(ctx: Context, template_id: str, version_id: str,
                                                revision: Annotated[int, Field(ge=0)], reason: str,
                                                request_id: RequestId) -> dict[str, Any]:
        """明确指定模板的默认版本，提供当前 revision，并说明设置理由。"""
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
