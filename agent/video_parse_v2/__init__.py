"""Video Parse V2 Harness 的公共入口。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import ParseAgent

__all__ = ["ParseAgent"]


def __getattr__(name: str) -> type[ParseAgent]:
    if name != "ParseAgent":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .runtime import ParseAgent

    globals()[name] = ParseAgent
    return ParseAgent
