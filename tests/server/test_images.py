from __future__ import annotations

import base64
import json
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from server.config.media import ImageUploadConfig
from server.constants.media import SERIES_IMAGE_ID_PREFIX
from server.utils.images import prepare_series_image
from server.utils.media import decode_series_image_id, encode_series_image_id


def image_bytes(*, size=(32, 24), mode="RGB", image_format="PNG", color="red"):
    output = BytesIO()
    Image.new(mode, size, color).save(output, format=image_format)
    return output.getvalue()


def test_model_image_keeps_original_identity_and_normalizes_transparency():
    data = image_bytes(size=(240, 80), mode="RGBA", color=(0, 0, 0, 0))
    extension, model_data = prepare_series_image(data, config=ImageUploadConfig(model_max_edge=120))

    assert extension == "png"
    with Image.open(BytesIO(model_data)) as model:
        assert model.format == "JPEG"
        assert model.mode == "RGB"
        assert model.size == (120, 40)
        assert model.getpixel((0, 0)) == (255, 255, 255)
    with Image.open(BytesIO(data)) as original:
        assert original.size == (240, 80)
        assert original.mode == "RGBA"


def test_model_image_respects_exif_orientation():
    image = Image.new("RGB", (80, 40), "red")
    exif = Image.Exif()
    exif[274] = 6
    output = BytesIO()
    image.save(output, format="JPEG", exif=exif)

    extension, model_data = prepare_series_image(output.getvalue(), config=ImageUploadConfig())

    assert extension == "jpg"
    with Image.open(BytesIO(model_data)) as model:
        assert model.size == (40, 80)
        assert model.getexif().get(274) is None


@pytest.mark.parametrize("image_format,extension", [("JPEG", "jpg"), ("PNG", "png"), ("WEBP", "webp")])
def test_image_upload_detects_the_actual_format(image_format, extension):
    actual, model_data = prepare_series_image(
        image_bytes(image_format=image_format), config=ImageUploadConfig(),
    )
    assert actual == extension
    with Image.open(BytesIO(model_data)) as model:
        model.load()
        assert model.format == "JPEG"


def test_image_upload_rejects_corrupt_oversized_and_animated_images():
    data = image_bytes()
    for invalid in (b"", b"not an image", data[:-20]):
        with pytest.raises(ValueError):
            prepare_series_image(invalid, config=ImageUploadConfig())
    with pytest.raises(ValueError, match="maximum upload size"):
        prepare_series_image(data, config=ImageUploadConfig(maximum_upload_size_bytes=len(data) - 1))
    with pytest.raises(ValueError, match="maximum pixel count"):
        prepare_series_image(data, config=ImageUploadConfig(maximum_pixels=32 * 24 - 1))
    with pytest.raises(ValueError, match="JPEG, PNG, or WebP"):
        prepare_series_image(image_bytes(image_format="GIF"), config=ImageUploadConfig())
    animated = BytesIO()
    Image.new("RGB", (20, 20), "red").save(
        animated, format="PNG", save_all=True,
        append_images=[Image.new("RGB", (20, 20), "blue")], duration=100,
    )
    with pytest.raises(ValueError, match="animated"):
        prepare_series_image(animated.getvalue(), config=ImageUploadConfig())


def test_series_image_ids_require_canonical_scope_hash_and_extension():
    series_id = str(uuid4())
    checksum = "a" * 64
    identifier = encode_series_image_id(series_id, checksum, "png")
    assert decode_series_image_id(identifier) == (series_id, checksum, "png")

    for payload in (
        [series_id, "../outside", "png"], [series_id, checksum, "../../jpg"],
        [series_id, checksum, []], [series_id, checksum], {"id": series_id},
        [series_id, "A" * 64, "png"], [series_id.upper(), checksum, "png"],
    ):
        forged = SERIES_IMAGE_ID_PREFIX + base64.urlsafe_b64encode(
            json.dumps(payload).encode(),
        ).decode().rstrip("=")
        with pytest.raises(ValueError, match="invalid Series image ID"):
            decode_series_image_id(forged)
    for invalid in (identifier + "=", identifier + "/", "simg_", "artifact://test/private", None):
        with pytest.raises(ValueError, match="invalid Series image ID"):
            decode_series_image_id(invalid)


@pytest.mark.parametrize("field", ["maximum_upload_size_bytes", "maximum_pixels", "model_max_edge"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_image_upload_config_rejects_invalid_limits(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        ImageUploadConfig(**{field: value})
