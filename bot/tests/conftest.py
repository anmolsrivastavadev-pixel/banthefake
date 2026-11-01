from __future__ import annotations

import sys
from pathlib import Path


# The `pytest` console script starts with bot/.venv/bin on sys.path. Add the worktree root so the
# documented `bot/.venv/bin/pytest -q bot/tests` command imports the local bot package reliably.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
