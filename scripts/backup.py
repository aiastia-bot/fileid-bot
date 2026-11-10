"""Consistent SQLite snapshot and restore check (never overwrites live data).

Usage: python scripts/backup.py data/backups/2026-11-10.db
MySQL deployments should use mysqldump --single-transaction and restore into an
isolated database, as described in OPERATIONS.md.
"""

import argparse
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DB_TYPE, DB_PATH


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.exists():
        raise ValueError("Source database does not exist")
    if destination.exists() or source == destination:
        raise ValueError("Destination must be a new file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".backup-")
    os.close(fd)
    try:
        with (
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as src,
            sqlite3.connect(temporary) as out,
        ):
            src.backup(out)
        with sqlite3.connect(temporary) as restored:
            if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
            tables = restored.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        # A hard link atomically creates the destination without clobbering.
        os.link(temporary, destination)
        return tables
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination")
    args = parser.parse_args()
    if DB_TYPE != "sqlite":
        parser.error("This command handles SQLite only; use your MySQL backup tooling")
    print(
        f"Backup verified: {args.destination} ({backup(DB_PATH, args.destination)} tables)"
    )
