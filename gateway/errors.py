class SSEProxyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        response_id: str | None = None,
        partial_output_chars: int = 0,
        content_filter_type: str | None = None,
        content_filter_details: str | None = None,
    ) -> None:
        self.code = code
        self.response_id = response_id
        self.partial_output_chars = partial_output_chars
        self.content_filter_type = content_filter_type
        self.content_filter_details = content_filter_details

        details = [message]
        if code:
            details.append(f"code={code}")
        if response_id:
            details.append(f"response_id={response_id}")
        details.append(f"partial_output_chars={partial_output_chars}")
        super().__init__("; ".join(details))


class ContentFilterError(SSEProxyError):
    """The provider explicitly rejected generated output via content policy."""
