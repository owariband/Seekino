"""The shared, bounded read/write Tool channel for business canvases."""

from __future__ import annotations

from collections.abc import Callable, Collection
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
import tiktoken

from .context_canvas import ContextCanvas, canvas_json
from .errors import AgentToolFeedbackError, AgentToolInvocationError, AgentToolOutputError
from .tool import AgentTool, build_agent_tool
from .tool_output import bounded_json, cursor_position, page_items, range_items, range_text
from .tool_result import JsonToolResult, ToolResult


class ContextCanvasReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: Annotated[str, Field(min_length=1, max_length=100)]
    reference: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    references: list[Annotated[str, Field(min_length=1, max_length=256)]] | None = Field(
        default=None, min_length=1,
    )
    section: Annotated[str, Field(min_length=1, max_length=100)] = "overview"
    cursor: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    start: int = Field(default=0, ge=0, strict=True,
                       description="First item index for references; first character offset for reference.")
    end: int | None = Field(default=None, ge=0, strict=True,
                           description="Exclusive end item index for references, or character offset for reference.")


def build_context_canvas_tools(
    canvas: ContextCanvas,
    *,
    role: str,
    read_collections: Collection[str],
    maximum_result_bytes: int,
    maximum_read_chars: int,
    maximum_catalog_items: int,
    write_args_schema: type[BaseModel] | None = None,
    check_control: Callable[[], None] | None = None,
    on_call: Callable[[str], None] | None = None,
    on_read: Callable[[dict[str, Any]], None] | None = None,
) -> list[AgentTool]:
    allowed = frozenset(read_collections)
    detail_encoding = tiktoken.get_encoding("o200k_base")

    def check() -> None:
        if check_control is not None:
            check_control()

    def get_context_canvas(
        collection: str, reference: str | None = None, section: str = "overview",
        cursor: str | None = None, start: int = 0, end: int | None = None,
        references: list[str] | None = None,
    ) -> ToolResult:
        check()
        if on_call is not None:
            on_call("get_context_canvas")
        if collection not in allowed:
            raise AgentToolFeedbackError(
                "FORBIDDEN_COLLECTION", f"Collection {collection!r} is unavailable to role {role!r}.",
                f"Read only these collections: {', '.join(sorted(allowed))}.",
            )
        with canvas._lock:
            revision = canvas.revision
            if references is not None:
                if (
                    reference is not None or section == "overview" or cursor is not None
                    or not references or len(references) > maximum_catalog_items
                    or len(set(references)) != len(references)
                ):
                    raise AgentToolFeedbackError(
                        "INVALID_READ", "Batch detail reads require unique references and an explicit section.",
                        f"Use at most {maximum_catalog_items} references, no reference/cursor, "
                        "and an explicit section with a start/end item range.",
                    )
                stop = len(references) if end is None else end
                if not 0 <= start < stop <= len(references):
                    raise AgentToolFeedbackError(
                        "INVALID_RANGE", "Batch start/end are outside the reference list.",
                        f"Use 0 <= start < end <= {len(references)}; end is exclusive.",
                    )
                items = []
                for ref in references[start:stop]:
                    sections = canvas.sections(collection, ref, role=role)
                    if section not in sections:
                        raise AgentToolFeedbackError(
                            "UNKNOWN_SECTION", f"Unknown section {section!r} for reference {ref!r}.",
                            "Read that reference's overview to obtain its section names.",
                        )
                    content = sections[section]
                    items.append({
                        "collection": collection, "revision": revision, "reference": ref,
                        "id": ref, "section": section, "start": 0, "end": len(content),
                        "total_chars": len(content), "content": content,
                    })
                payload = json.loads(range_items(
                    items, start=start, total=len(references), maximum_bytes=maximum_result_bytes,
                    maximum_chars=maximum_read_chars, encoding=detail_encoding,
                    metadata={"collection": collection, "revision": revision, "section": section},
                ))
            elif reference is None:
                if section != "overview" or start != 0 or end is not None:
                    raise AgentToolFeedbackError(
                        "INVALID_READ", "Catalog reads accept collection and cursor only.",
                        "Use a reference and section for detail reads, or a cursor for catalog pages.",
                    )
                namespace = f"{canvas.id}:{role}:{collection}:{revision}"
                try:
                    offset = cursor_position(cursor, namespace=namespace).offset if cursor else 0
                    payload = json.loads(page_items(
                        canvas.catalog(collection, role=role), namespace=namespace,
                        resource_id=collection, cursor=cursor,
                        maximum_items=maximum_catalog_items, maximum_bytes=maximum_result_bytes,
                        metadata={"collection": collection, "revision": revision, "start": offset},
                    ))
                except AgentToolInvocationError as exc:
                    if isinstance(exc, AgentToolOutputError):
                        raise
                    raise AgentToolFeedbackError(
                        "INVALID_CATALOG_CURSOR", str(exc),
                        "Read the current catalog without a cursor and review its pages again.",
                    ) from exc
            else:
                if cursor is not None:
                    raise AgentToolFeedbackError(
                        "INVALID_READ", "Detail reads do not accept a catalog cursor.",
                        "Use reference, section, start and end for a detail read.",
                    )
                sections = canvas.sections(collection, reference, role=role)
                metadata = {"collection": collection, "revision": revision, "reference": reference}
                if section == "overview":
                    if start != 0 or end is not None:
                        raise AgentToolFeedbackError(
                            "INVALID_RANGE", "overview does not accept a character range.",
                            "Read overview first, then read each named section in character ranges.",
                        )
                    payload = {
                        **metadata, "id": reference, "section": "overview",
                        "sections": [{"name": name, "total_chars": len(content)} for name, content in sections.items()],
                    }
                else:
                    if section not in sections:
                        raise AgentToolFeedbackError(
                            "UNKNOWN_SECTION", f"Unknown section {section!r}.",
                            "Read overview to obtain the available section names.",
                        )
                    # Reserve exactly the extra object members before computing a readable range.
                    overhead = len(canvas_json(metadata).encode("utf-8")) - 1
                    if maximum_result_bytes <= overhead:
                        raise AgentToolOutputError("Tool result budget is too small for a readable Canvas range")
                    payload = {**json.loads(range_text(
                        sections[section], resource_id=reference, section=section,
                        start=start, end=end, maximum_bytes=maximum_result_bytes - overhead,
                        maximum_chars=maximum_read_chars,
                    )), **metadata}
            bounded_json(payload, maximum_result_bytes)
            check()
            if on_read is not None:
                for item in payload["items"] if references is not None else [payload]:
                    if not item.get("truncated", False):
                        on_read(item)
            return JsonToolResult(success=True, payload=payload)

    tools = [build_agent_tool(
        func=get_context_canvas, name="get_context_canvas",
        description=(
            f"Read the {role} Canvas catalog and exact entries. Allowed collections: "
            f"{', '.join(sorted(allowed))}. Omit reference/references to page catalogs with cursor. "
            f"Batch at most {maximum_catalog_items} references per request. "
            "Use references=[...] and an explicit section to batch details with start/end item indices "
            "(zero-based, end exclusive), without cursor. The complete JSON is limited to 8,000 o200k_base tokens. "
            "If truncated, only items before complete_end are fully read; narrow the remaining item range. "
            "Known sections can be read directly without overview. "
            "Use single reference/start/end ranges for oversized entries; overview lists unknown sections. "
            "Respect role-specific review requirements. "
            "Messages and checkpoints are navigation only."
        ), args_schema=ContextCanvasReadArguments, maximum_result_bytes=maximum_result_bytes,
    )]
    if write_args_schema is not None:
        def write_context_canvas(**values: Any) -> ToolResult:
            check()
            if on_call is not None:
                on_call("write_context_canvas")
            result = canvas.write(  # type: ignore[attr-defined]
                role=role, maximum_result_bytes=maximum_result_bytes, **values,
            )
            check()
            return JsonToolResult(success=True, payload=result)

        tools.append(build_agent_tool(
            func=write_context_canvas, name="write_context_canvas",
            description=(
                f"Apply a validated batch to the {role} Canvas draft. Use the current expected_revision "
                "and a unique batch_id; retrying an identical executed batch is idempotent. "
                "Omitted entries and fields stay unchanged. Evidence and approved snapshots are read-only."
            ), args_schema=write_args_schema, maximum_result_bytes=maximum_result_bytes,
        ))
    return tools
