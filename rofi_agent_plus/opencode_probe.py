"""opencode session probe executed locally or over SSH."""

SESSION_PROBE = r"""
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

limit = max(1, int(sys.argv[1]))
requested_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
installed = shutil.which("opencode") is not None


def clean_text(value):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value[:117] + "..." if len(value) > 120 else value


base = os.environ.get("XDG_DATA_HOME")
root = Path(base) if base else Path.home() / ".local" / "share"
db_path = root / "opencode" / "opencode.db"

sessions = []
if db_path.is_file():
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        query = (
            "SELECT id, title, directory, time_updated FROM session "
            "WHERE time_archived IS NULL AND parent_id IS NULL"
        )
        params = []
        if requested_id is not None:
            query += " AND id = ?"
            params.append(requested_id)
        query += " ORDER BY time_updated DESC"
        if requested_id is None:
            query += " LIMIT ?"
            params.append(limit)
        for row in conn.execute(query, params).fetchall():
            session_id, title, directory, updated = row
            updated_seconds = int(updated / 1000) if isinstance(updated, (int, float)) else 0
            sessions.append(
                {
                    "id": session_id,
                    "name": clean_text(title) if isinstance(title, str) else "",
                    "cwd": directory or "",
                    "recencyAt": updated_seconds,
                    "updatedAt": updated_seconds,
                }
            )
    except (OSError, sqlite3.Error) as exc:
        print(f"opencode database query failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if conn is not None:
            conn.close()

print(json.dumps({"installed": installed, "sessions": sessions}, separators=(",", ":")))
"""
