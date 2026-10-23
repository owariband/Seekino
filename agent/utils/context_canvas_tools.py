"""The shared, bounded read/write Tool channel for business canvases."""

from __future__ import annotations

from collections.abc import Callable, Collection
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from .context_canvas import ContextCanvas, canvas_json
from .errors import AgentToolFeedbackError, AgentToolInvocationError, AgentToolOutputError
from .tool import AgentTool, build_agent_tool
from .tool_output import batch_items, bounded_json, page_items, range_text
from .tool_result import JsonToolResult, ToolResult


class ContextCanvasReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: Annotated[str, Field(min_length=1, max_length=100)]
    reference: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    references: list[Annotated[str, Field(min_length=1, max_length=256)]] | None = Field(
        default=None, min_length=1,
    )
    section: Annotated[str, Field(min_length=1, max_length=100)] = "content"
    catalog_cursor: Annotated[str, Field(min_length=1, max_length=2000)] | None = Field(
        default=None, description="Only for an oversized catalog: copy its next_cursor to continue.",
    )
    char_start: int = Field(default=0, ge=0, strict=True,
                           description="Character offset for a single oversized reference only.")
    char_end: int | None = Field(default=None, ge=0, strict=True,
                                description="Exclusive character end for a single oversized reference only.")


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
    # Legacy construction arguments remain compatible; the Tool byte budget is authoritative.

    def check() -> None:
        if check_control is not None:
            check_control()

    def get_context_canvas(
        collection: str, reference: str | None = None, section: str = "content",
        catalog_cursor: str | None = None, char_start: int = 0, char_end: int | None = None,
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
                    reference is not None or section == "overview" or catalog_cursor is not None
                    or char_start != 0 or char_end is not None or not references
                    or len(set(references)) != len(references)
                ):
                    raise AgentToolFeedbackError(
                        "INVALID_READ", "Batch reads require unique references and a content section.",
                        "Use references without reference, catalog_cursor or character offsets; "
                        "omit section to read complete content.",
                    )
                items = []
                for ref in references:
                    sections = canvas.sections(collection, ref, role=role)
                    if section not in sections:
                        raise AgentToolFeedbackError(
                            "UNKNOWN_SECTION", f"Unknown section {section!r} for reference {ref!r}.",
                            f"Use one of these sections: {', '.join(sections)}.",
                        )
                    content = sections[section]
                    items.append({
                        "collection": collection, "revision": revision, "reference": ref,
                        "id": ref, "section": section, "start": 0, "end": len(content),
                        "total_chars": len(content), "content": content,
                    })
                payload = json.loads(batch_items(
                    items, maximum_bytes=maximum_result_bytes,
                    metadata={"collection": collection, "revision": revision, "section": section},
                ))
            elif reference is None:
                if section not in {"content", "overview"} or char_start != 0 or char_end is not None:
                    raise AgentToolFeedbackError(
                        "INVALID_READ", "Catalog reads accept collection and catalog_cursor only.",
                        "Omit references for the catalog; specify references to read complete items.",
                    )
                namespace = f"{canvas.id}:{role}:{collection}:{revision}"
                try:
                    catalog = canvas.catalog(collection, role=role)
                    payload = {"collection": collection, "revision": revision, "items": catalog,
                               "total": len(catalog), "next_cursor": None}
                    if catalog_cursor is not None or len(canvas_json(payload).encode("utf-8")) > maximum_result_bytes:
                        payload = json.loads(page_items(
                            catalog, namespace=namespace, resource_id=collection, cursor=catalog_cursor,
                            maximum_items=max(1, len(catalog)), maximum_bytes=maximum_result_bytes,
                            metadata={"collection": collection, "revision": revision},
                        ))
                except AgentToolInvocationError as exc:
                    if isinstance(exc, AgentToolOutputError):
                        raise
                    raise AgentToolFeedbackError(
                        "INVALID_CATALOG_CURSOR", str(exc),
                        "Read the current catalog without catalog_cursor and review its entries again.",
                    ) from exc
            else:
                if catalog_cursor is not None:
                    raise AgentToolFeedbackError(
                        "INVALID_READ", "Detail reads do not accept a catalog cursor.",
                        "Use reference and section for a complete detail read.",
                    )
                sections = canvas.sections(collection, reference, role=role)
                metadata = {"collection": collection, "revision": revision, "reference": reference}
                if section == "overview":
                    if char_start != 0 or char_end is not None:
                        raise AgentToolFeedbackError(
                            "INVALID_RANGE", "overview does not accept a character range.",
                            "Omit character offsets when reading overview.",
                        )
                    payload = {
                        **metadata, "id": reference, "section": "overview",
                        "sections": [{"name": name, "total_chars": len(content)} for name, content in sections.items()],
                    }
                else:
                    if section not in sections:
                        raise AgentToolFeedbackError(
                            "UNKNOWN_SECTION", f"Unknown section {section!r}.",
                            f"Use one of these sections: {', '.join(sections)}.",
                        )
                    # Reserve exactly the extra object members before computing a readable range.
                    overhead = len(canvas_json(metadata).encode("utf-8")) - 1
                    if maximum_result_bytes <= overhead:
                        raise AgentToolOutputError("Tool result budget is too small for a readable Canvas range")
                    payload = {**json.loads(range_text(
                        sections[section], resource_id=reference, section=section,
                        start=char_start, end=char_end, maximum_bytes=maximum_result_bytes - overhead,
                        maximum_chars=max(1, len(sections[section])),
                    )), **metadata}
            bounded_json(payload, maximum_result_bytes)
            check()
            if on_read is not None:
                for item in payload["items"] if references is not None else [payload]:
                    if "content" in item:
                        on_read(item)
            return JsonToolResult(success=True, payload=payload)

    tools = [build_agent_tool(
        func=get_context_canvas, name="get_context_canvas",
        description=(
            f"Read the {role} Canvas catalog and exact entries. Allowed collections: "
            f"{', '.join(sorted(allowed))}. Omit reference/references to read the catalog; "
            "only if next_cursor is returned, pass it as catalog_cursor for the rest. "
            "Use references=[...] to read complete items in one call; section defaults to content. "
            "The Tool tries the entire batch within its result byte budget. If truncated, returned items "
            "are complete; read remaining_references in smaller batches. Skip overview for known sections. "
            "Only for an oversized single item, use reference with char_start/char_end character offsets "
            "(zero-based, end exclusive) or a smaller named section. section=overview lists section sizes. "
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
