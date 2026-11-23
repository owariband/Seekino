"""Lazily load artwork for visible cards, including stills from verified originals."""
from functools import lru_cache
from pathlib import Path
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


class PosterProvider(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Image,
                         QQuickImageProvider.Flag.ForceAsynchronousImageLoading)

    def requestImage(self, id, size, requestedSize):
        try:
            # Revalidate the video and artifact in the active library before using cached pixels.
            source = _request("posterSource", posterID=id)
            if "image" in source:
                reader = QImageReader(source["image"])
                reader.setAutoTransform(True)
                dimensions = reader.size()
                if dimensions.isValid():
                    reader.setScaledSize(dimensions.boundedTo(dimensions.scaled(
                        QSize(960, 540), Qt.AspectRatioMode.KeepAspectRatio)))
                image = reader.read()
            else:
                image = QImage.fromData(_frame_bytes(source["original"], source["atMs"], id), "JPEG")
        except (RuntimeError, OSError, ValueError):
            image = QImage()
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image
