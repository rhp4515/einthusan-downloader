#!/usr/bin/env python3
"""Write the API's OpenAPI document to docs/openapi.json.

The spec is generated from the live FastAPI app rather than hand-written, so it
cannot drift from the routes. Regenerate after changing anything under api/:

    .venv/bin/python scripts/export_openapi.py

The checked-in document is what client generators (e.g. an Android app) consume.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.main import create_app
from api.settings import Settings

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def main() -> None:
    # Placeholder settings: generating the schema must not need a real .env,
    # and nothing from here ends up in the document.
    app = create_app(Settings(api_key="placeholder", core_config={}))
    spec = app.openapi()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} ({len(spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
