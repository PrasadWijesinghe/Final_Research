"""The schema exporter writes one valid, versioned JSON Schema per message model."""

import json
from pathlib import Path

import pytest

from shared.contracts import MESSAGES, SCHEMA_VERSION, export_schemas


def test_export_writes_one_schema_per_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "schemas"
    monkeypatch.setattr(export_schemas, "OUT", out)
    export_schemas.main()
    capsys.readouterr()

    expected = {f"{m.__name__}.schema.json" for m in MESSAGES}
    assert {p.name for p in out.iterdir()} == expected
    for model in MESSAGES:
        schema = json.loads((out / f"{model.__name__}.schema.json").read_text())
        assert schema["$id"].endswith(f"-v{SCHEMA_VERSION}")
