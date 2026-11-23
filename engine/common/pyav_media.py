"""Local-file media operations for the embedded desktop runtime."""

from __future__ import annotations

import math
import wave
from collections.abc import Callable, Sequence
from fractions import Fraction
from pathlib import Path

import av

from .media_contract import MediaPreprocessing, PYAV_PREPROCESSING


def _video_stream(container):
    if not container.streams.video:
        raise ValueError("Source contains no video stream")
    return container.streams.video[0]


def _time(frame) -> Fraction:
    if frame.pts is None or frame.time_base is None:
        raise ValueError("Source frame is missing a presentation timestamp")
    return frame.pts * frame.time_base


def _origin(container, stream) -> Fraction:
    if container.start_time is not None:
        return Fraction(container.start_time, av.time_base)
    return (stream.start_time or 0) * stream.time_base


def _seek(container, stream, timestamp: Fraction) -> None:
    # Seeking in stream ticks lands on an earlier keyframe. Compare each decoded
    # frame's presentation timestamp, including reordered B frames.
    container.seek(math.floor(timestamp / stream.time_base), stream=stream, backward=True)


def _dimensions(frame, max_height: int, *, encode=False):
    width, height = frame.width, frame.height
    if abs(frame.rotation) == 90:
        width, height = height, width
    scaled_height = min(height, max_height)
    if encode:
        scaled_height = max(2, scaled_height // 2 * 2)
    return max(2, ((width * scaled_height + height) // (height * 2)) * 2), scaled_height


def _scaled_frame(frame, width: int, height: int, format: str):
    if frame.rotation:
        image = frame.to_image().rotate(frame.rotation, expand=True)
        frame = av.VideoFrame.from_image(image)
    return frame.reformat(width=width, height=height, format=format)


def _write_jpeg(frame, output: Path, max_height: int) -> None:
    width, height = _dimensions(frame, max_height)
    _scaled_frame(frame, width, height, "rgb24").to_image().save(output, format="JPEG", quality=92)


def _audio_samples(frame, start: Fraction, end: Fraction | None):
    """Return [first, stop) and the output sample PTS on the clip's clock."""
    at = _time(frame)
    rate = frame.sample_rate
    first = max(0, math.ceil((start - at) * rate))
    stop = frame.samples if end is None else min(frame.samples, math.ceil((end - at) * rate))
    return first, stop, round((at - start) * rate) + first


class PyAVMedia:
    def __init__(self, *, video_encoder: str = "h264_videotoolbox"):
        self.video_encoder = video_encoder
        self.preprocessing = MediaPreprocessing(f"pyav-{video_encoder}-yuv420p-scale-cap-v1", PYAV_PREPROCESSING.jpeg)

    def probe_duration(self, source: Path, *, check_active: Callable[[], None] | None = None) -> int:
        with av.open(str(source)) as container:
            stream = _video_stream(container)
            if stream.duration is not None:
                duration = stream.duration * stream.time_base
            else:
                first = last = None
                for index, frame in enumerate(container.decode(stream)):
                    if check_active and index % 32 == 0:
                        check_active()
                    at = _time(frame)
                    if frame.duration:
                        length = frame.duration * frame.time_base
                    elif stream.average_rate:
                        length = 1 / stream.average_rate
                    else:
                        raise ValueError("Video frame duration and average frame rate are missing")
                    first = at if first is None else min(first, at)
                    last = at + length if last is None else max(last, at + length)
                if first is None:
                    raise ValueError("Source contains no video frames")
                duration = last - first
        if duration <= 0:
            raise ValueError("Source video duration must be positive")
        return max(1, round(duration * 1000))

    def frames(self, source: Path, timestamps_ms: Sequence[int], *, output_dir: Path,
               max_height: int, check_active: Callable[[], None]) -> list[tuple[int, Path]]:
        times = sorted(set(timestamps_ms))
        if not times or any(type(t) is not int or t < 0 for t in times) or max_height <= 0:
            raise ValueError("Frame requests require nonnegative integer milliseconds and a positive height")
        output_dir.mkdir(parents=True, exist_ok=True)
        selected = {}
        with av.open(str(source)) as container:
            origin = last_frame = None
            for index, frame in enumerate(container.decode(_video_stream(container))):
                if index % 32 == 0:
                    check_active()
                at = _time(frame)
                if origin is None:
                    origin = at
                stamp = math.floor((at - origin) * 1000 + Fraction(1, 2))
                last_frame = frame
                if stamp < times[len(selected)]:
                    continue
                path = output_dir / f"frame-{stamp:012d}.jpg"
                _write_jpeg(frame, path, max_height)
                while len(selected) < len(times) and times[len(selected)] <= stamp:
                    selected[times[len(selected)]] = path
                if len(selected) == len(times):
                    break
            if last_frame is None:
                raise ValueError("Source contains no video frames")
            if len(selected) < len(times):
                path = output_dir / f"frame-{stamp:012d}.jpg"
                _write_jpeg(last_frame, path, max_height)
                selected.update(dict.fromkeys(times[len(selected):], path))
        return [(t, selected[t]) for t in timestamps_ms]

    def frame_at(self, video: Path, output: Path, *, at_ms: int, max_height: int,
                 check_active: Callable[[], None]) -> None:
        with av.open(str(video)) as container:
            stream = _video_stream(container)
            target = _origin(container, stream) + Fraction(at_ms, 1000)
            if at_ms:
                _seek(container, stream, target)
            last_frame = None
            for index, frame in enumerate(container.decode(stream)):
                if index % 32 == 0:
                    check_active()
                if _time(frame) >= target:
                    _write_jpeg(frame, output, max_height)
                    return
                last_frame = frame
            # A time within the last frame's display interval still has a picture.
            if last_frame is not None and target < _time(last_frame) + last_frame.duration * last_frame.time_base:
                _write_jpeg(last_frame, output, max_height)
                return
        raise ValueError(f"No video frame at {at_ms} milliseconds")

    def audio(self, video: Path, output: Path, *, start_ms: int = 0, end_ms: int | None = None,
              first_track: bool = False, check_active: Callable[[], None]) -> None:
        with av.open(str(video)) as container:
            if not container.streams.audio:
                raise ValueError("Source contains no audio stream")
            stream = container.streams.audio[0] if first_track else max(
                container.streams.audio,
                key=lambda s: (bool(s.disposition & av.stream.Disposition.default), s.codec_context.channels),
            )
            origin = _origin(container, stream)
            start = origin + Fraction(start_ms, 1000)
            end = None if end_ms is None else origin + Fraction(end_ms, 1000)
            # A demuxer can seek to the next packet on an audio-only seek. Decode
            # a preroll for codec delay and the resampler before trimming by sample.
            if start_ms > 1000:
                _seek(container, stream, start - 1)
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            with wave.open(str(output), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))

                def write(frame):
                    for pcm in resampler.resample(frame):
                        first, stop, _ = _audio_samples(pcm, start, end)
                        if first < stop:
                            wav.writeframesraw(memoryview(pcm.planes[0])[first*2:stop*2])

                for index, frame in enumerate(container.decode(stream)):
                    if index % 32 == 0:
                        check_active()
                    if end is not None and _time(frame) >= end:
                        break
                    write(frame)
                write(None)
                if not wav.getnframes():
                    raise ValueError("Source contains no audio samples in the requested interval")

    def clip(self, source: Path, output: Path, *, start_ms: int, end_ms: int,
             max_height: int, check_active: Callable[[], None]) -> None:
        # Rotation is frame side data. Determine the display size before muxing
        # any audio, even for sources whose first audio packet precedes video.
        with av.open(str(source)) as probe:
            first = next(probe.decode(_video_stream(probe)), None)
            if first is None:
                raise ValueError("Source contains no video frames")
            width, height = _dimensions(first, max_height, encode=True)

        with av.open(str(source)) as container, av.open(
            str(output), mode="w", format="mp4", options={"movflags": "+faststart"},
        ) as dest:
            video = _video_stream(container)
            origin = _origin(container, video)
            start = origin + Fraction(start_ms, 1000)
            end = origin + Fraction(end_ms, 1000)
            if start_ms > 1000:
                _seek(container, video, start - 1)
            encoded_video = dest.add_stream(self.video_encoder, rate=video.average_rate or video.guessed_rate or 25)
            encoded_video.width, encoded_video.height = width, height
            encoded_video.pix_fmt = "yuv420p"
            encoded_video.codec_context.time_base = video.time_base
            encoded_video.codec_context.max_b_frames = 0
            streams = [video]
            if container.streams.audio:
                audio = container.streams.audio[0]
                streams.append(audio)
                rate = min(av.Codec("aac", "w").audio_rates, key=lambda r: abs(r - audio.codec_context.sample_rate))
                encoded_audio = dest.add_stream("aac", rate=rate)
                encoded_audio.layout = audio.codec_context.layout
                resampler = av.AudioResampler(format="fltp", layout=encoded_audio.layout, rate=rate)

            def mux_audio(frame):
                for pcm in resampler.resample(frame):
                    first, stop, pts = _audio_samples(pcm, start, end)
                    if first >= stop:
                        continue
                    if first or stop < pcm.samples:
                        cropped = av.AudioFrame("fltp", encoded_audio.layout.name, stop - first)
                        for src, dst in zip(pcm.planes, cropped.planes, strict=True):
                            dst.update(memoryview(src)[first * pcm.format.bytes:stop * pcm.format.bytes])
                        pcm = cropped
                    pcm.sample_rate, pcm.time_base, pcm.pts = rate, Fraction(1, rate), pts
                    dest.mux(encoded_audio.encode(pcm))

            video_durations = {}

            def mux_video(frame):
                for packet in encoded_video.encode(frame):
                    duration = video_durations.pop(packet.pts, None)
                    if duration is not None:
                        packet.duration = duration
                    dest.mux(packet)

            finished = set()
            video_written = False
            for index, packet in enumerate(container.demux(*streams)):
                if index % 32 == 0:
                    check_active()
                if packet.stream.index in finished:
                    continue
                for frame in packet.decode():
                    at = _time(frame)
                    if at >= end:
                        finished.add(packet.stream.index)
                        break
                    if packet.stream.type == "audio":
                        mux_audio(frame)
                    elif at >= start:
                        scaled = _scaled_frame(frame, width, height, "yuv420p")
                        offset = round((at - start) / video.time_base)
                        if frame.duration:
                            length = min(end - at, frame.duration * frame.time_base)
                            video_durations[offset] = max(1, round(length / video.time_base))
                        scaled.time_base, scaled.pts = video.time_base, offset
                        mux_video(scaled)
                        video_written = True
                if len(finished) == len(streams):
                    break
            if not video_written:
                raise ValueError("Source contains no video frames in the requested interval")
            check_active()
            mux_video(None)
            if container.streams.audio:
                mux_audio(None)
                dest.mux(encoded_audio.encode(None))
