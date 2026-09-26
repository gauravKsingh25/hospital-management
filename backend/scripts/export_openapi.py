"""Write the API's OpenAPI schema to a file.

The frontend's TypeScript types are generated from this, so that the contract
between Pydantic and TypeScript is checked by a tool rather than remembered by
a person. Hand-written frontend types are the same class of bug the backend's
`tests/test_rbac_seed.py` exists to catch — two declarations of one truth,
drifting apart quietly until something breaks in production.

    python scripts/export_openapi.py --output ../frontend/openapi.json

No server is started and no database is touched: FastAPI can produce the
schema from the app object alone. That matters because it means the frontend's
codegen has no runtime dependency on a live backend, and CI can verify the
committed types are current without standing anything up.

The frontend wraps this as `npm run codegen`, and `npm run codegen:check`
fails when the committed types are stale.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# `app.core.config` refuses to import without a signing key. Generating a
# schema needs no real secret, and demanding a configured .env here would put
# a working backend environment between a frontend developer and their types.
os.environ.setdefault("SECRET_KEY", "openapi-export-placeholder-not-a-secret")

from app.main import app


def export(output: Path) -> int:
    schema = app.openapi()

    # Sorted keys and a trailing newline so the file is diff-stable: an
    # unchanged API must produce a byte-identical file, or `codegen:check`
    # would fail for no reason and quickly be ignored.
    rendered = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")

    print(
        f"Wrote {output} — {len(schema['paths'])} paths, "
        f"{len(schema.get('components', {}).get('schemas', {}))} schemas."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "frontend" / "openapi.json",
        help="Where to write the schema (default: frontend/openapi.json).",
    )
    args = parser.parse_args()
    return export(args.output)


if __name__ == "__main__":
    sys.exit(main())
