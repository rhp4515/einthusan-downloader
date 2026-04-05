#!/usr/bin/env bash
# One-time setup: create virtualenv and install Python dependencies using uv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Check uv is available
if ! command -v uv &>/dev/null; then
    echo "uv not found. Install it with:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "==> Creating virtualenv at $VENV_DIR …"
uv venv "$VENV_DIR"

echo "==> Installing Python dependencies …"
uv pip install -r "$SCRIPT_DIR/requirements.txt" --python "$VENV_DIR/bin/python3"

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. cp .env.example .env"
echo "  2. Edit .env with your Einthusan credentials and Radarr API key"
echo "  3. Verify Radarr:  $VENV_DIR/bin/python3 einthusan_dl.py --list-profiles"
echo "  4. Download:       $VENV_DIR/bin/python3 einthusan_dl.py <einthusan_movie_url>"
echo "  5. Run tests:      $VENV_DIR/bin/pytest tests/ -v"
