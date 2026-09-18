from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping


ARK_PROVIDER: Final = "ark"
DEEPSEEK_PROVIDER: Final = "deepseek"
ARK_OPENAI_BASE_URL: Final = "https://ark.cn-beijing.volces.com/api/v3"

DEEPSEEK_NON_THINKING_EXTRA_BODY: Final[
    Mapping[str, Mapping[str, str]]
] = MappingProxyType({
    "thinking": MappingProxyType({"type": "disabled"}),
})
