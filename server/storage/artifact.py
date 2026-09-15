from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol
from urllib.parse import quote, unquote, urlparse

from ..database import DatabaseEnvironment
from ..identity import canonical_uuid
from ..utils.media import decode_series_image_id, encode_series_image_id

_COPY_BUFFER_BYTES = 1024 * 1024


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


class ArtifactIntegrityError(RuntimeError):
    """Persisted upload bytes disagree with their MySQL completion facts."""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    uri: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UploadSegmentArtifact:
    segment_number: int
    uri: str
    size_bytes: int
    sha256: str


class ArtifactStore(Protocol):
    """Durable media boundary used by API and Worker orchestration."""

    def put_upload_segment(
        self,
        upload_session_id: str,
        segment_number: int,
        data: bytes,
    ) -> StoredArtifact: ...

    def assemble_upload(
        self,
        upload_session_id: str,
        segments: Sequence[UploadSegmentArtifact],
        *,
        expected_total_size: int,
    ) -> StoredArtifact: ...

    def publish_original(
        self,
        staged: StoredArtifact,
        *,
        series_id: str,
        video_id: str,
        original_filename: str,
    ) -> StoredArtifact: ...

    def publish_video_artifact(
        self,
        source_path: str | Path,
        *,
        series_id: str,
        video_id: str,
        category: str,
        filename: str,
    ) -> StoredArtifact: ...

    def put_series_image(
        self, series_id: str, data: bytes, *, extension: str, model_data: bytes,
    ) -> str: ...

    def resolve_series_image(
        self, image_id: str, *, series_id: str, variant: str = "original",
    ) -> Path: ...

    def uri_for_path(self, path: str | Path) -> str: ...

    def fingerprint(self, uri: str) -> StoredArtifact: ...

    def resolve(self, uri: str) -> Path: ...

    def work_directory(self, job_id: str) -> Path: ...

    def delete_upload_staging(self, upload_session_id: str) -> None: ...


class LocalArtifactStore:
    """Environment-isolated durable media storage for the local M1 runtime."""

    def __init__(
        self,
        root: str | Path,
        *,
        environment: DatabaseEnvironment | str,
    ) -> None:
        configured_root = Path(root).expanduser().resolve()
        if configured_root == Path(configured_root.anchor):
            raise ValueError("artifact root must not be a filesystem root")
        self.environment = DatabaseEnvironment(environment)
        self.root = configured_root
        self.base_directory = configured_root / self.environment.value
        self.base_directory.mkdir(parents=True, exist_ok=True)

    def put_upload_segment(
        self,
        upload_session_id: str,
        segment_number: int,
        data: bytes,
    ) -> StoredArtifact:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        if isinstance(segment_number, bool) or not isinstance(segment_number, int):
            raise ValueError("segment_number must be an integer")
        if segment_number < 0:
            raise ValueError("segment_number must be nonnegative")
        if not isinstance(data, bytes) or not data:
            raise ValueError("upload segment data must not be empty")

        checksum = hashlib.sha256(data).hexdigest()
        relative_path = PurePosixPath(
            "staging",
            "uploads",
            session_id,
            "segments",
            f"{segment_number:08d}-{checksum}.part",
        )
        target = self._path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            self._write_atomic(target, data)
        elif target.stat().st_size != len(data):
            raise ArtifactIntegrityError(
                f"existing upload segment has a different size: {relative_path}"
            )
        return StoredArtifact(
            uri=self._uri(relative_path),
            size_bytes=len(data),
            sha256=checksum,
        )

    def assemble_upload(
        self,
        upload_session_id: str,
        segments: Sequence[UploadSegmentArtifact],
        *,
        expected_total_size: int,
    ) -> StoredArtifact:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        if expected_total_size <= 0:
            raise ValueError("expected_total_size must be greater than zero")
        if not segments:
            raise ValueError("at least one upload segment is required")

        ordered = sorted(segments, key=lambda item: item.segment_number)
        if [item.segment_number for item in ordered] != list(range(len(ordered))):
            raise ArtifactIntegrityError("upload segments must be contiguous from zero")

        assembled_directory = self._path(
            PurePosixPath("staging", "uploads", session_id, "assembled")
        )
        assembled_directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="assembling-",
            suffix=".tmp",
            dir=assembled_directory,
        )
        total_size = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as destination:
                for expected_number, segment in enumerate(ordered):
                    if segment.segment_number != expected_number:
                        raise ArtifactIntegrityError(
                            "upload segment order changed during assembly"
                        )
                    source = self.resolve(segment.uri)
                    actual_size, actual_sha256 = self._copy_verified(
                        source,
                        destination,
                        digest,
                    )
                    if actual_size != segment.size_bytes:
                        raise ArtifactIntegrityError(
                            f"upload segment {expected_number} size changed"
                        )
                    if actual_sha256 != segment.sha256:
                        raise ArtifactIntegrityError(
                            f"upload segment {expected_number} checksum changed"
                        )
                    total_size += actual_size
                destination.flush()
                os.fsync(destination.fileno())
            if total_size != expected_total_size:
                raise ArtifactIntegrityError(
                    "assembled upload size does not match the declared total"
                )

            content_hash = digest.hexdigest()
            relative_path = PurePosixPath(
                "staging",
                "uploads",
                session_id,
                "assembled",
                f"{content_hash}.upload",
            )
            target = self._path(relative_path)
            temporary_path = Path(temporary_name)
            if target.exists():
                if target.stat().st_size != total_size:
                    raise ArtifactIntegrityError(
                        "an assembled upload with the same hash has another size"
                    )
                temporary_path.unlink()
            else:
                temporary_path.replace(target)
            return StoredArtifact(
                uri=self._uri(relative_path),
                size_bytes=total_size,
                sha256=content_hash,
            )
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def publish_original(
        self,
        staged: StoredArtifact,
        *,
        series_id: str,
        video_id: str,
        original_filename: str,
    ) -> StoredArtifact:
        canonical_series_id = canonical_uuid(series_id, "series_id")
        canonical_video_id = canonical_uuid(video_id, "video_id")
        filename = self._safe_filename(original_filename)
        source = self.resolve(staged.uri)
        if source.stat().st_size != staged.size_bytes:
            raise ArtifactIntegrityError("assembled upload changed before publication")

        relative_path = PurePosixPath(
            "media",
            "series",
            canonical_series_id,
            "videos",
            canonical_video_id,
            "original",
            filename,
        )
        target = self._path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = self._fingerprint(target)
            if (
                existing.size_bytes != staged.size_bytes
                or existing.sha256 != staged.sha256
            ):
                raise ArtifactIntegrityError(
                    "the published original path contains different bytes"
                )
            source.unlink(missing_ok=True)
        else:
            source.replace(target)
        return StoredArtifact(
            uri=self._uri(relative_path),
            size_bytes=staged.size_bytes,
            sha256=staged.sha256,
        )

    def publish_video_artifact(
        self,
        source_path: str | Path,
        *,
        series_id: str,
        video_id: str,
        category: str,
        filename: str,
    ) -> StoredArtifact:
        """Copy a derived media artifact to its stable Video namespace."""

        if category not in {"chunks", "analysis-windows", "keyframes", "clips"}:
            raise ValueError("unsupported Video artifact category")
        canonical_series_id = canonical_uuid(series_id, "series_id")
        canonical_video_id = canonical_uuid(video_id, "video_id")
        safe_filename = self._safe_filename(filename)
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise FileNotFoundError(source)
        fingerprint = self._fingerprint(source)
        relative_path = PurePosixPath(
            "media",
            "series",
            canonical_series_id,
            "videos",
            canonical_video_id,
            category,
            safe_filename,
        )
        target = self._path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = self._fingerprint(target)
            if (
                existing.size_bytes != fingerprint.size_bytes
                or existing.sha256 != fingerprint.sha256
            ):
                raise ArtifactIntegrityError(
                    f"published {category} path contains different bytes"
                )
        else:
            self._copy_file_atomic(source, target)
        return StoredArtifact(
            uri=self._uri(relative_path),
            size_bytes=fingerprint.size_bytes,
            sha256=fingerprint.sha256,
        )

    def uri_for_path(self, path: str | Path) -> str:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        try:
            relative = resolved.relative_to(self.base_directory)
        except ValueError as exc:
            raise ValueError("artifact path is outside the configured store") from exc
        return self._uri(PurePosixPath(*relative.parts))

    def put_series_image(
        self, series_id: str, data: bytes, *, extension: str, model_data: bytes,
    ) -> str:
        if not isinstance(data, bytes) or not data:
            raise ValueError("image data must not be empty")
        if not isinstance(model_data, bytes) or not model_data:
            raise ValueError("model image data must not be empty")
        checksum = hashlib.sha256(data).hexdigest()
        image_id = encode_series_image_id(series_id, checksum, extension)
        canonical_series_id = canonical_uuid(series_id, "series_id")
        relative_directory = PurePosixPath(
            "media", "series", canonical_series_id, "background", "images", checksum,
        )
        directory = self._path(relative_directory)
        directory.mkdir(parents=True, exist_ok=True)
        original = self._path(relative_directory / f"original.{extension}")
        model = self._path(relative_directory / "model.jpg")
        for target, content in ((original, data), (model, model_data)):
            self._write_immutable(target, content)
        return image_id

    def resolve_series_image(
        self, image_id: str, *, series_id: str, variant: str = "original",
    ) -> Path:
        image_series_id, checksum, extension = decode_series_image_id(image_id)
        if image_series_id != canonical_uuid(series_id, "series_id"):
            raise ValueError("image belongs to another Series")
        if variant not in {"original", "model"}:
            raise ValueError("unsupported Series image variant")
        filename = f"original.{extension}" if variant == "original" else "model.jpg"
        path = self._path(PurePosixPath(
            "media", "series", image_series_id, "background", "images", checksum, filename,
        ))
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def fingerprint(self, uri: str) -> StoredArtifact:
        path = self.resolve(uri)
        result = self._fingerprint(path)
        return StoredArtifact(
            uri=uri,
            size_bytes=result.size_bytes,
            sha256=result.sha256,
        )

    def resolve(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "artifact" or parsed.netloc != self.environment.value:
            raise ValueError("artifact URI belongs to another store or environment")
        relative = PurePosixPath(unquote(parsed.path).lstrip("/"))
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("artifact URI contains an invalid path")
        path = self._path(relative)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def work_directory(self, job_id: str) -> Path:
        canonical_job_id = canonical_uuid(job_id, "job_id")
        directory = self._path(PurePosixPath("jobs", canonical_job_id))
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def delete_upload_staging(self, upload_session_id: str) -> None:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        directory = self._path(PurePosixPath("staging", "uploads", session_id))
        if directory.exists():
            shutil.rmtree(directory)

    def _path(self, relative_path: PurePosixPath) -> Path:
        candidate = self.base_directory.joinpath(*relative_path.parts).resolve()
        try:
            candidate.relative_to(self.base_directory)
        except ValueError as exc:
            raise ValueError("artifact path escapes the configured root") from exc
        return candidate

    def _uri(self, relative_path: PurePosixPath) -> str:
        encoded = "/".join(quote(part, safe="") for part in relative_path.parts)
        return f"artifact://{self.environment.value}/{encoded}"

    @staticmethod
    def _safe_filename(value: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("original_filename must be a non-empty filename")
        if Path(value).name != value or "\x00" in value:
            raise ValueError("original_filename must not contain a path")
        return value

    @staticmethod
    def _write_atomic(target: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            Path(temporary_name).replace(target)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_immutable(target: Path, data: bytes) -> None:
        """Publish complete bytes once without replacing a concurrent writer."""
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            try:
                os.link(temporary_name, target)
            except FileExistsError:
                # An earlier model derivative remains stable across retries/config edits.
                if target.name != "model.jpg" and target.read_bytes() != data:
                    raise ArtifactIntegrityError("stored Series image bytes changed")
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _copy_file_atomic(source: Path, target: Path) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        try:
            with source.open("rb") as input_stream, os.fdopen(
                descriptor,
                "wb",
            ) as output_stream:
                shutil.copyfileobj(
                    input_stream,
                    output_stream,
                    length=_COPY_BUFFER_BYTES,
                )
                output_stream.flush()
                os.fsync(output_stream.fileno())
            Path(temporary_name).replace(target)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _copy_verified(
        source: Path,
        destination: BinaryIO,
        assembled_digest: _Digest,
    ) -> tuple[int, str]:
        segment_digest = hashlib.sha256()
        size = 0
        with source.open("rb") as stream:
            while chunk := stream.read(_COPY_BUFFER_BYTES):
                destination.write(chunk)
                segment_digest.update(chunk)
                assembled_digest.update(chunk)
                size += len(chunk)
        return size, segment_digest.hexdigest()

    @staticmethod
    def _fingerprint(path: Path) -> StoredArtifact:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(_COPY_BUFFER_BYTES):
                digest.update(chunk)
                size += len(chunk)
        return StoredArtifact(uri="", size_bytes=size, sha256=digest.hexdigest())
