"""Write a JSON Schema file per message, for the TypeScript dashboard and for docs.

Run from the repo root:  python -m shared.contracts.export_schemas
Then generate TS types, e.g. with json-schema-to-typescript.
"""

import json
from pathlib import Path

from . import MESSAGES, SCHEMA_VERSION

OUT = Path(__file__).parent / "schemas"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for model in MESSAGES:
        schema = model.model_json_schema()
        schema["$id"] = f"{model.__name__}-v{SCHEMA_VERSION}"
        path = OUT / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(schema, indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
