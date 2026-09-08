"""Entry point: `python -m api`."""

import os

import uvicorn

from api.main import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", "8500"))
    uvicorn.run(app, host="0.0.0.0", port=port)
