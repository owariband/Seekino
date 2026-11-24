"""Decode an original file on the same source clock as the PyAV media service."""
from fractions import Fraction

import av

from engine.common.pyav_media import _audio_samples, _origin, _seek, _time, _video_stream

_AUDIO_RATE = 48_000


class PlaybackDecoder:
    def __init__(self, path: str, *, rgb: bool = False, video_only: bool = False):
        self._rgb = rgb
        self._container = av.open(path)
        try:
            self._video = _video_stream(self._container)
            self._video.thread_type = "AUTO"
            self._video.codec_context.thread_count = 4
            audios = () if video_only else self._container.streams.audio
            self._audio = max(audios, key=lambda stream: (
                bool(stream.disposition & av.stream.Disposition.default), stream.codec_context.channels,
            ), default=None)
            self._origin = _origin(self._container, self._video)
            self._samples = None
        except BaseException:
            self._container.close()
            raise

    def seek(self, ms: int) -> None:
        start = self._origin + Fraction(ms, 1000)
        # Read an audio preroll even when the requested video frame is a keyframe.
        _seek(self._container, self._video, max(self._origin, start - 1) if self._audio is not None else start)
        self._samples = self._decode(start)

    def next_sample(self):
        return next(self._samples, None)

    def close(self) -> None:
        self._samples = None
        self._container.close()

    def _decode(self, start):
        streams = [self._video] if self._audio is None else [self._video, self._audio]
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=_AUDIO_RATE)
        tail = None
        for frame in self._container.decode(*streams):
            if isinstance(frame, av.VideoFrame):
                at = _time(frame)
                if at < start:
                    tail = frame
                    continue
                if tail is not None and at > start:
                    yield self._picture(tail, start)
                tail = None
                yield self._picture(frame, at)
            else:
                for pcm in resampler.resample(frame):
                    sample = self._sound(pcm, start)
                    if sample is not None:
                        yield sample
        if tail is not None and _time(tail) + (tail.duration or 0) * tail.time_base > start:
            yield self._picture(tail, start)
        if self._audio is not None:
            for pcm in resampler.resample(None):
                sample = self._sound(pcm, start)
                if sample is not None:
                    yield sample

    def _picture(self, frame, at):
        if frame.rotation:
            frame = av.VideoFrame.from_image(frame.to_image().rotate(frame.rotation, expand=True))
        if self._rgb:
            picture = frame.reformat(format="rgb24")
            plane, = picture.planes
            return (0, round((at - self._origin) * 1_000_000), picture.width, picture.height,
                    memoryview(plane), plane.line_size)
        # The original macOS display layer consumes video-range NV12.
        picture = frame.reformat(format="nv12", dst_color_range="MPEG")
        luma, chroma = picture.planes
        return (0, round((at - self._origin) * 1_000_000), picture.width, picture.height,
                memoryview(luma), luma.line_size, memoryview(chroma), chroma.line_size)

    def _sound(self, frame, start):
        first, stop, _ = _audio_samples(frame, start, None)
        if first >= stop:
            return None
        pts = round((_time(frame) - self._origin) * _AUDIO_RATE) + first
        return (1, pts, stop - first, memoryview(frame.planes[0])[first * 4:stop * 4])
