"""Lazily load artwork for visible cards, including stills from verified originals."""
from functools import lru_cache
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtQuick import QQuickImageProvider

from .qt_controller import _request


@lru_cache(maxsize=64)
def _frame_bytes(path, at_ms, poster_id):
    # Cache compressed frames only. Qt manages the per-window decoded image cache.
    from engine.common.pyav_media import PyAVMedia

    with TemporaryDirectory(prefix="seekino-poster-") as directory:
        frame = Path(directory) / "frame.jpg"
        PyAVMedia().frame_at(Path(path), frame, at_ms=at_ms, max_height=450, check_active=lambda: None)
        return frame.read_bytes()


@lru_cache(maxsize=48)
def _hit_image(path, at_ms, identity):
    return _playback_frame(path, at_ms, QSize(384, 216))


def _playback_frame(path, at_ms, bounds):
    # Use the same PTS and rotation rules as playback, including between-frame seeks.
    from server.desktop.playback import PlaybackDecoder

    decoder = PlaybackDecoder(path, rgb=True, video_only=True)
    try:
        decoder.seek(at_ms)
        while (sample := decoder.next_sample()) is not None:
            if sample[0] == 0:
                _, _, width, height, rgb, stride = sample
                source = QImage(rgb, width, height, stride, QImage.Format.Format_RGB888)
                target = source.size().boundedTo(source.size().scaled(bounds, Qt.KeepAspectRatio))
                return source.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation).copy()
        raise ValueError("该命中位置没有可预览画面")
    finally:
        decoder.close()


class PosterProvider(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Image,
                         QQuickImageProvider.Flag.ForceAsynchronousImageLoading)

    def requestImage(self, id, size, requestedSize):
        try:
            # Revalidate the video and artifact in the active library before using cached pixels.
            if id.startswith("hit/"):
                # Neither an image URL nor a cache key can supply a filesystem path or timestamp.
                match = re.fullmatch(r"hit/([0-9a-f]{64})/([A-Za-z0-9_-]{1,4096}={0,2})", id)
                if match is None:
                    raise ValueError("无效的命中预览")
                source = _request("hitPreview", scope=match[1], sourceRef=match[2])
                image = _hit_image(source["original"], source["atMs"], id)
            elif id.startswith(("hero/", "shelf/")):
                kind, remaining = id.split("/", 1)
                poster_id, position = remaining.rsplit("/", 1)
                if not re.fullmatch(r"-1|[0-9]{1,15}", position):
                    raise ValueError("无效的观看位置")
                bounds = QSize(2560, 1440) if kind == "hero" else QSize(960, 600)
                source = _request("posterSource", posterID=poster_id, surface=kind, atMs=int(position))
                image = (self._read_image(source["image"], bounds) if "image" in source else
                         _playback_frame(source["original"], source["atMs"], bounds))
            else:
                image = self._cover(id)
        except (EOFError, RuntimeError, OSError, ValueError):
            image = QImage()
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image

    @staticmethod
    def _cover(id):
        source = _request("posterSource", posterID=id)
        if "image" in source:
            return PosterProvider._read_image(source["image"], QSize(960, 540))
        return QImage.fromData(_frame_bytes(source["original"], source["atMs"], id), "JPEG")

    @staticmethod
    def _read_image(path, bounds):
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        dimensions = reader.size()
        if dimensions.isValid():
            reader.setScaledSize(dimensions.boundedTo(dimensions.scaled(bounds, Qt.KeepAspectRatio)))
        return reader.read()
