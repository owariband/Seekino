"""Exercise a configured model on small synthetic input of its declared kind."""
from pathlib import Path
from tempfile import TemporaryDirectory
import wave

from pydantic import BaseModel

from gateway.config import GatewayConfig, ModelBinding
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput


class _ProbeResponse(BaseModel):
    ok: bool


def probe_registered_model(config: GatewayConfig, binding: ModelBinding, *, allow_local: bool, environ):
    client = GatewayClient(config, environ=environ, allow_local_endpoints=allow_local)
    binding = binding.model_copy(update={"request_timeout_seconds": min(binding.request_timeout_seconds or 15, 15)})
    try:
        if binding.kind == "embedding":
            client.create_embedding_model(binding).embed_query("connection test")
        elif binding.kind == "rerank":
            client.create_rerank_model(binding).rerank("sample", ["sample"])
        elif binding.kind == "transcription":
            with TemporaryDirectory() as folder:
                sample = Path(folder) / "silence.wav"
                with wave.open(str(sample), "wb") as audio:
                    audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    audio.writeframes(bytes(16000))
                client.create_transcription_model(binding).transcribe(sample)
        else:
            from PIL import Image
            with TemporaryDirectory() as folder:
                sample = Path(folder) / "sample.png"
                Image.new("RGB", (16, 16), "white").save(sample)
                inputs = [TextInput('Return {"ok":true}.')]
                if binding.supports_vision_input:
                    inputs.append(ImageInput(sample))
                client.create_generative_model(binding).generate(inputs, schema_name="connection_test", result_type=_ProbeResponse)
    finally:
        client.close()
