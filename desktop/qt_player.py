"""Play verified local originals on the PyAV source timeline in a Qt Quick item."""

from collections import deque
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic

from PySide6.QtCore import Property, QCoreApplication, QRectF, QSizeF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices, QtAudio
from PySide6.QtQml import qmlRegisterType
from PySide6.QtQuick import QQuickPaintedItem

from server.desktop.playback import PlaybackDecoder

_RATE = 48_000
_BYTES_PER_FRAME = 4  # Interleaved, signed little-endian stereo PCM16.


def _decode(path, target, packets, cancelled):
    """One owner per container. Backpressure limits decoded image memory."""
    def send(packet):
        while not cancelled.is_set():
            try:
                packets.put(packet, timeout=0.04)
                return
            except Full:
                continue

    try:
        decoder = PlaybackDecoder(path, rgb=True)
        try:
            decoder.seek(target)
            while not cancelled.is_set():
                sample = decoder.next_sample()
                if sample is None:
                    send(("end",))
                    return
                if sample[0] == 0:
                    _, pts, width, height, pixels, stride = sample
                    frame = QImage(pixels, width, height, stride, QImage.Format.Format_RGB888)
                    # The queue owns these pixels after the PyAV plane is released.
                    send(("video", pts, frame.copy()))
                else:
                    _, pts, frames, pcm = sample
                    send(("audio", pts, frames, bytes(pcm)))
        finally:
            decoder.close()
    except Exception as exc:
        send(("error", str(exc) or type(exc).__name__))


class VideoSurface(QQuickPaintedItem):
    positionChanged = Signal()
    durationChanged = Signal()
    playingChanged = Signal()
    frameChanged = Signal()
    errorChanged = Signal()
    volumeChanged = Signal()
    mutedChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFillColor(Qt.GlobalColor.black)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._path = ""
        self._duration = self._position = 0
        self._playing = self._has_frame = False
        self._muted = False
        self._volume = 1.0
        self._error = ""
        self._image = QImage()
        self._videos = deque()
        self._audio = deque()
        self._sink = self._device = None
        self._cancel = None
        self._thread = None
        self._packets = None
        self._clock = None
        self._ended = self._audio_unavailable = False
        QCoreApplication.instance().aboutToQuit.connect(self.close)

    positionMs = Property(int, lambda self: self._position, notify=positionChanged)
    durationMs = Property(int, lambda self: self._duration, notify=durationChanged)
    playing = Property(bool, lambda self: self._playing, notify=playingChanged)
    hasFrame = Property(bool, lambda self: self._has_frame, notify=frameChanged)
    error = Property(str, lambda self: self._error, notify=errorChanged)

    @Property(bool, notify=mutedChanged)
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        if self._muted != value:
            self._muted = value
            self._apply_volume()
            self.mutedChanged.emit()

    @Property(float, notify=volumeChanged)
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        value = max(0.0, min(1.0, value))
        if self._volume != value:
            self._volume = value
            self._apply_volume()
            self.volumeChanged.emit()

    def _apply_volume(self):
        if self._sink:
            self._sink.setVolume(0.0 if self._muted else self._volume)

    def paint(self, painter):
        if not self._image.isNull():
            bounds = self.boundingRect()
            size = QSizeF(self._image.size()).scaled(bounds.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(0, 0, size.width(), size.height())
            target.moveCenter(bounds.center())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, self._image)

    @Slot(str, int, int)
    def open(self, path, start, duration):
        self.close()
        self._path = path
        self._duration = max(0, duration)
        self.durationChanged.emit()
        self._playing = True
        self.playingChanged.emit()
        self.seek(start)

    @Slot(int)
    def seek(self, target):
        if not self._path:
            return
        self._reset_stream()
        self._error = ""
        self.errorChanged.emit()
        self._set_position(max(0, min(target, max(0, self._duration - 1))))
        self._cancel = Event()
        self._packets = Queue(maxsize=3)
        self._thread = Thread(target=_decode, name="Seekino media", daemon=True,
                              args=(self._path, self._position, self._packets, self._cancel))
        self._thread.start()
        self._timer.start()

    @Slot(bool)
    def setPlaying(self, value):
        if not self._path or value == self._playing:
            return
        self._playing = value
        self.playingChanged.emit()
        if value and self._position >= self._duration:
            self.seek(0)
        elif value:
            self._anchor()
            if self._sink:
                self._sink.resume()
            self._timer.start()
        else:
            if self._sink:
                self._sink.suspend()
            self._clock = None
            if self._has_frame:
                self._timer.stop()

    @Slot()
    def close(self):
        self._timer.stop()
        self._reset_stream()
        self._path = ""
        self._duration = 0
        self.durationChanged.emit()
        self._set_position(0)
        self._playing = False
        self.playingChanged.emit()
        self._error = ""
        self.errorChanged.emit()
        self._image = QImage()
        self.update()

    def _reset_stream(self):
        if self._cancel:
            self._cancel.set()
        self._cancel = self._thread = self._packets = None
        if self._sink:
            self._sink.reset()
            self._sink.deleteLater()
        self._sink = self._device = None
        self._audio_unavailable = False
        self._videos.clear()
        self._audio.clear()
        self._ended = False
        self._has_frame = False
        self.frameChanged.emit()
        self._clock = None

    def _anchor(self):
        self._clock = monotonic() - self._position / 1000

    def _set_position(self, value):
        if self._position != value:
            self._position = value
            self.positionChanged.emit()

    def _tick(self):
        if self._packets is None:
            return
        # Drain demux output without letting queued high-resolution images grow.
        while len(self._videos) < 4 and (not self._has_frame or len(self._audio) < 64):
            try:
                packet = self._packets.get_nowait()
            except Empty:
                break
            if packet[0] == "error":
                self._fail(packet[1])
                return
            if packet[0] == "end":
                self._ended = True
                break
            if packet[0] == "video":
                self._videos.append(packet)
            elif not self._audio_unavailable:
                self._audio.append(packet)
        if self._ended and not self._has_frame and not self._videos:
            self._fail("所选时间附近没有可显示的视频帧")
            return

        if self._playing and self._clock is None and (self._videos or self._has_frame):
            self._anchor()
        if self._clock is not None:
            position = (monotonic() - self._clock) * 1000
            # The device is master while it consumes queued PCM. Reanchor the
            # monotonic clock so silence after the last audio sample also plays.
            if self._sink and self._sink.state() == QtAudio.State.ActiveState:
                position = min(self._audio_start_ms + self._sink.processedUSecs() / 1000,
                               self._audio_cursor * 1000 / _RATE)
                self._clock = monotonic() - position / 1000
            self._set_position(min(self._duration, max(self._position, int(position))))

        frame = None
        while self._videos and (not self._has_frame or self._videos[0][1] <= self._position * 1000):
            _, _, frame = self._videos.popleft()
            if not self._has_frame:
                self._has_frame = True
                self.frameChanged.emit()
        if frame is not None:
            self._image = frame
            self.update()
        if self._playing:
            self._feed_audio()
            if self._position >= self._duration:
                self.setPlaying(False)
        elif self._has_frame:
            self._timer.stop()

    def _fail(self, message):
        self._reset_stream()
        self._timer.stop()
        self._playing = False
        self.playingChanged.emit()
        self._error = message
        self.errorChanged.emit()

    def _feed_audio(self):
        if not self._has_frame or not self._audio:
            return
        if self._sink is None:
            fmt = QAudioFormat()
            fmt.setSampleRate(_RATE)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            output = QMediaDevices.defaultAudioOutput()
            if output.isNull() or not output.isFormatSupported(fmt):
                self._anchor()
                self._audio_unavailable = True
                self._audio.clear()
                return
            self._sink = QAudioSink(output, fmt, self)
            self._sink.setBufferSize(_RATE * _BYTES_PER_FRAME // 8)
            self._apply_volume()
            self._device = self._sink.start()
            if not self._device:
                self._fail("无法打开系统音频输出设备")
                return
            # Initial device discovery can take longer than a short clip. Start
            # the source clock after the device is ready, preserving the first frame.
            self._anchor()
            self._audio_start_ms = self._position
            self._audio_cursor = round(self._position * _RATE / 1000)

        while self._audio:
            _, pts, frames, data = self._audio[0]
            room = self._sink.bytesFree() // _BYTES_PER_FRAME
            if room <= 0:
                return
            if pts + frames <= self._audio_cursor:
                self._audio.popleft()
                continue
            if pts > self._audio_cursor:
                chunk = bytes(min(room, pts - self._audio_cursor) * _BYTES_PER_FRAME)
            else:
                offset = (self._audio_cursor - pts) * _BYTES_PER_FRAME
                chunk = data[offset:offset + room * _BYTES_PER_FRAME]
            written = self._device.write(chunk)
            if written <= 0:
                return
            self._audio_cursor += written // _BYTES_PER_FRAME


def register_player():
    qmlRegisterType(VideoSurface, "Seekino.Media", 1, 0, "VideoSurface")
