#!/usr/bin/env bash
# Run without Docker (needs python3 and git on PATH). Example:
#   REPOS_ROOT=$HOME/code ./run-dev.sh
set -euo pipefail
cd "$(dirname "$0")/backend"
export REPOS_ROOT="${REPOS_ROOT:-$HOME}"
export REPOS_ROOT_HOST="${REPOS_ROOT_HOST:-$REPOS_ROOT}"
export DATA_DIR="${DATA_DIR:-$(pwd)/../data}"
python3 -m pip install -q -r requirements.txt
echo "gitscope on http://localhost:${PORT:-8080}  (repos root: $REPOS_ROOT)"
exec python3 -m uvicorn app:app --host 127.0.0.1 --port "${PORT:-8080}" --reload
