"""Create a deterministic, checksummed zip bundle for one trained model version."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .train import DEFAULT_ARTIFACTS_DIR

REQUIRED_FILES = (
    "model.joblib",
    "training_metadata.json",
    "metrics.json",
    "model_card.md",
)
EXCLUDED_SUFFIXES = {".zip"}
MANIFEST_NAME = "bundle_manifest.json"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_bundle(artifact_dir: Path, output_path: Path) -> dict[str, object]:
    """Package all reports for one artifact and record a checksum for every file."""
    missing = [name for name in REQUIRED_FILES if not (artifact_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"artifact is incomplete; missing files: {missing}")

    files = sorted(
        path
        for path in artifact_dir.iterdir()
        if path.is_file()
        and path.name != MANIFEST_NAME
        and path.suffix.lower() not in EXCLUDED_SUFFIXES
    )
    manifest: dict[str, object] = {
        "format_version": 1,
        "artifact_version": artifact_dir.name,
        "files": {
            path.name: {"sha256": _sha256(path.read_bytes()), "bytes": path.stat().st_size}
            for path in files
        },
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (artifact_dir / MANIFEST_NAME).write_bytes(manifest_bytes)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in [*files, artifact_dir / MANIFEST_NAME]:
            info = ZipInfo(path.name, date_time=FIXED_ZIP_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    artifact_dir = args.artifacts_dir / args.version
    output = args.output or artifact_dir / f"infra-model-{args.version}.zip"
    manifest = build_bundle(artifact_dir, output)
    print(json.dumps({"bundle": str(output), **manifest}, indent=2))


if __name__ == "__main__":
    main()
