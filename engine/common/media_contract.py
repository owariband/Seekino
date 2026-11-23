"""The transformations applied to visual model inputs by each media backend."""
from dataclasses import dataclass
from typing import Literal

JpegPreprocessRevision = Literal["ffmpeg-jpeg-q3-scale-cap-v1", "pyav-pillow-jpeg92-scale-cap-v1"]


@dataclass(frozen=True, slots=True)
class MediaPreprocessing:
    clip: str
    jpeg: JpegPreprocessRevision


FFMPEG_PREPROCESSING = MediaPreprocessing("ffmpeg-h264-crf23-scale-cap-v1", "ffmpeg-jpeg-q3-scale-cap-v1")
PYAV_PREPROCESSING = MediaPreprocessing("pyav-h264_videotoolbox-yuv420p-scale-cap-v1", "pyav-pillow-jpeg92-scale-cap-v1")
