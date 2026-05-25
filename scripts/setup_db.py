#!/usr/bin/env python3
"""Run Alembic database migrations.

Usage
-----
    # From the project root (bmcc-bot/):
    python scripts/setup_db.py

    # Or with a specific target revision:
    python scripts/setup_db.py --revision base    # roll all the way back
    python scripts/setup_db.py --revision head    # upgrade to latest (default)
    python scripts/setup_db.py --revision -1      # downgrade one step

Environment
-----------
    DATABASE_URL must be set (directly or via .env).

The script exits with code 0 on success and non-zero on failure so it can
be used safely in Railway's release phase or CI pipelines.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate the project root (parent of this script's directory)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.resolve()


def _load_dotenv() -> None:
    """Best-effort load of .env so the script works without a shell export."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't overwrite values that are already set in the environment
        os.environ.setdefault(key, value)


def _check_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print(
            "ERROR: DATABASE_URL is not set.\n"
            "       Export it or add it to .env before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)
    return url


def _run_alembic(revision: str) -> None:
    """Invoke alembic upgrade / downgrade as a subprocess."""
    if revision.startswith("-") or revision == "base":
        cmd = ["python", "-m", "alembic", "downgrade", revision]
        action = f"downgrade to {revision}"
    else:
        cmd = ["python", "-m", "alembic", "upgrade", revision]
        action = f"upgrade to {revision}"

    print(f"Running: {' '.join(cmd)}")
    print(f"Action:  {action}")
    print(f"Target:  {os.environ.get('DATABASE_URL', '').split('@')[-1]}")  # hide credentials
    print()

    result = subprocess.run(cmd, cwd=PROJECT_ROOT)

    if result.returncode != 0:
        print(f"\nERROR: alembic exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"\nDone — migration {action} completed successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Alembic database migrations")
    parser.add_argument(
        "--revision",
        default="head",
        help="Alembic revision target (default: head). Use '-1' to downgrade one step.",
    )
    args = parser.parse_args()

    _load_dotenv()
    _check_database_url()
    _run_alembic(args.revision)


if __name__ == "__main__":
    main()
