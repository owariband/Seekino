"""Split one anime frame into a matte and two aligned RGBA layers on the editing computer."""

import argparse
import hashlib
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image


WEIGHTS_SHA256 = "f15622d853e8260172812b657053460e20806f04b9e05147d49af7bed31a6e99"


def predict_alpha(session, image):
    width, height = image.size
    side = 1024
    scale = side / max(width, height)
    resized = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    left = (side - resized.width) // 2
    top = (side - resized.height) // 2
    rows = slice(top, top + resized.height)
    cols = slice(left, left + resized.width)
    padded = np.zeros((side, side, 3), dtype=np.float32)
    padded[rows, cols] = np.asarray(resized, dtype=np.float32) / 255
    model_input = np.ascontiguousarray(padded.transpose(2, 0, 1)[None])
    output = session.run(None, {session.get_inputs()[0].name: model_input})[0]
    matte = Image.fromarray(output[0, 0, rows, cols]).resize(image.size, Image.Resampling.BILINEAR)
    # Snap the near-empty and near-solid ends; keep the transition at the silhouette soft.
    # Truncating sigmoid probabilities leaves 1/255 in the inverse layer even
    # in the middle of the face and fists.
    alpha = np.clip((np.asarray(matte) - 0.07) / (0.98 - 0.07), 0, 1)
    return Image.fromarray(np.rint(255 * alpha).astype(np.uint8))


def layer(image, alpha):
    rgba = image.convert("RGBA")
    rgba.putalpha(alpha)
    rgba.paste((0, 0, 0, 0), mask=alpha.point(lambda v: 255 if v == 0 else 0))
    return rgba


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.weights.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != WEIGHTS_SHA256:
            parser.error("Expected the skytnt/anime-seg isnetis.onnx weights.")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(str(args.weights), options, providers=["CPUExecutionProvider"])
    with Image.open(args.image) as source:
        image = source.convert("RGB")
    alpha = predict_alpha(session, image)
    foreground = layer(image, alpha)
    background = layer(image, alpha.point(lambda v: 255 - v))
    red = Image.new("RGBA", image.size, (192, 0, 0, 255))
    red.alpha_composite(foreground)
    args.output.mkdir(parents=True, exist_ok=True)
    alpha.save(args.output / "mask.png")
    foreground.save(args.output / "foreground.png")
    background.save(args.output / "background-hole.png")
    red.convert("RGB").save(args.output / "red-preview.png")


if __name__ == "__main__":
    main()
