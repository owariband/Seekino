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
    api_key: str,
    timeout: float,
    response_format: str,
) -> dict:
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
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
