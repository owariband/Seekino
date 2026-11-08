"""OpenAI-compatible Audio Transcriptions transport."""

from pathlib import Path

from openai import OpenAI

from gateway.usage import track_model_call


def transcribe_audio(
    path: Path,
    *,
    model: str,
    provider: str,
    base_url: str,
    api_key: str | None,
    timeout: float,
    response_format: str,
    allow_local: bool | None = None,
) -> dict:
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if allow_local is not None:
        from gateway.personal_endpoint import endpoint_http_client
        with endpoint_http_client(base_url, allow_local=allow_local, timeout=timeout) as client, path.open("rb") as audio:
            with track_model_call(provider=provider, model=model, operation="candidate_transcript") as call:
                unregister = call.add_cancel_callback(client.close)
                try:
                    call.raise_if_cancelled()
                    response = client.post(base_url.rstrip("/") + "/audio/transcriptions",
                        headers={"Authorization": "Bearer " + api_key} if api_key else {},
                        data={"model": model, "response_format": response_format}, files={"file": (path.name, audio)})
                    response.raise_for_status()
                    result = response.json()
                    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                        raise ValueError("转写服务返回格式有误")
                    call.raise_if_cancelled()
                    call.capture_response(result)
                    return result
                finally:
                    unregister()
    with OpenAI(
        base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0
    ) as client, path.open("rb") as audio:
        with track_model_call(
            provider=provider,
            model=model,
            operation="candidate_transcript",
        ) as call:
            unregister_cancel = call.add_cancel_callback(client.close)
            try:
                call.raise_if_cancelled()
                response = client.audio.transcriptions.create(
                    model=model,
                    file=audio,
                    response_format=response_format,
                )
            finally:
                unregister_cancel()
            call.capture_response(response)
            return response.model_dump()
