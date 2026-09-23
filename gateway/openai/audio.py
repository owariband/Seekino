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
    with OpenAI(
        base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0
    ) as client:
        with (
            path.open("rb") as audio,
            track_model_call(
                provider=provider,
                model=model,
                operation="candidate_transcript",
            ) as call,
        ):
            response = client.audio.transcriptions.create(
                model=model,
                file=audio,
                response_format=response_format,
            )
            call.capture_response(response)
            return response.model_dump()
