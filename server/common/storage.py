from __future__ import annotations

from pathlib import Path

from ..database import DatabaseEnvironment
from ..storage.artifact import LocalArtifactStore


def build_artifact_storage(
    root: Path, *, environment: DatabaseEnvironment,
    create_base_directory: bool = True,
) -> LocalArtifactStore:
    return LocalArtifactStore(
        root, environment=environment, create_base_directory=create_base_directory,
    )
