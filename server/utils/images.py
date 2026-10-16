from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from ..config.media import ImageUploadConfig
from ..constants.media import IMAGE_FORMAT_EXTENSIONS, MODEL_IMAGE_JPEG_QUALITY


@dataclass(frozen=True, slots=True)
class StaticImageInfo:
    extension: str
    media_type: str
    width: int
    height: int


def inspect_static_image(data: bytes, *, config: ImageUploadConfig) -> StaticImageInfo:
    """Validate the original image without resizing or re-encoding its pixels."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("image data must not be empty")
    if len(data) > config.maximum_upload_size_bytes:
        raise ValueError("image exceeds the maximum upload size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                extension = IMAGE_FORMAT_EXTENSIONS.get(source.format)
                if extension is None:
                    raise ValueError("image must be JPEG, PNG, or WebP")
                if source.width * source.height > config.maximum_pixels:
                    raise ValueError("image exceeds the maximum pixel count")
                if getattr(source, "is_animated", False):
                    raise ValueError("animated images are not supported")
                info = StaticImageInfo(
                    extension, Image.MIME[source.format], source.width, source.height,
                )
                source.verify()
            with Image.open(BytesIO(data)) as source:
                source.load()
            return info
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError("image exceeds the maximum pixel count") from exc
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("image data is corrupt or unsupported") from exc


def prepare_series_image(data: bytes, *, config: ImageUploadConfig) -> tuple[str, bytes]:
    """Validate original pixels and build the static image consumed by Parse."""
    info = inspect_static_image(data, config=config)
    with Image.open(BytesIO(data)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        image.thumbnail(
            (config.model_max_edge, config.model_max_edge), Image.Resampling.LANCZOS,
        )
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
        else:
            rgb = image.convert("RGB")
        output = BytesIO()
        rgb.save(output, format="JPEG", quality=MODEL_IMAGE_JPEG_QUALITY)
        return info.extension, output.getvalue()
