from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gateway.multimodal import ImageInput, TextInput


@dataclass(frozen=True, slots=True)
class BackgroundImage:
    image_id: str
    path: Path


def build_background_image_inputs(
    images: Sequence[BackgroundImage],
) -> list[TextInput | ImageInput]:
    inputs: list[TextInput | ImageInput] = []
    for image in images:
        inputs.extend(
            [
                TextInput(
                    text=(
                        f"背景参考图 asset://{image.image_id}：只用于人物身份、"
                        "术语与背景参考，不是当前视频证据；没有视频时间戳，"
                        "不能作为当前事件、动作或台词来源。"
                    )
                ),
                ImageInput(path=image.path, detail="high"),
            ]
        )
    return inputs
