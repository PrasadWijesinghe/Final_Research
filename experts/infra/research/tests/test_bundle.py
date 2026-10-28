from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest
from model.bundle import MANIFEST_NAME, REQUIRED_FILES, build_bundle


def test_bundle_contains_required_files_and_manifest(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "v-test"
    artifact_dir.mkdir()
    for name in REQUIRED_FILES:
        (artifact_dir / name).write_text(f"content for {name}", encoding="utf-8")

    output = tmp_path / "bundle.zip"
    manifest = build_bundle(artifact_dir, output)

    with ZipFile(output) as archive:
        names = set(archive.namelist())
    assert set(REQUIRED_FILES) | {MANIFEST_NAME} <= names
    assert set(manifest["files"]) == set(REQUIRED_FILES)


def test_bundle_rejects_an_incomplete_artifact(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_bundle(tmp_path, tmp_path / "bundle.zip")
