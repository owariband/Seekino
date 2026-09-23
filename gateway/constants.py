from __future__ import annotations

from typing import Final


# Adapter names select a request protocol and its implementation.
OPENAI_ADAPTER: Final = "openai"
ARK_ADAPTER: Final = "ark"
CODEX_APP_SERVER_ADAPTER: Final = "codex_app_server"
CLAUDE_SDK_ADAPTER: Final = "claude_sdk"

# Provider names remain stable provenance, usage, and pricing labels.
ARK_PROVIDER: Final = "ark"
CLAUDE_CODE_PROVIDER: Final = "claude_code"
CODEX_PROVIDER: Final = "codex"
DEEPSEEK_PROVIDER: Final = "deepseek"

# Profiles describe dialect differences within one protocol implementation.
OPENAI_STANDARD_PROFILE: Final = "standard"
OPENAI_DEEPSEEK_PROFILE: Final = "deepseek"

ARK_OPENAI_BASE_URL: Final = "https://ark.cn-beijing.volces.com/api/v3"
