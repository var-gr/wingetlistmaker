"""
sync_winget.py
Reads the winget source SQLite database (index.db) and writes
data/packages.json for the browser app.

Environment variables:
  DB_PATH   — path to the extracted index.db (default: /tmp/index.db)
  JSON_OUT  — output path for the JSON file  (default: data/packages.json)
"""

import json
import os
import sqlite3
import sys

DB_PATH  = os.environ.get("DB_PATH",  "/tmp/index.db")
JSON_OUT = os.environ.get("JSON_OUT", "data/packages.json")


def fetch_packages(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    print(f"Tables found: {sorted(tables)}")

    if "manifest" in tables and "versions" in tables:
        query = """
            SELECT
                i.id                          AS id,
                n.name                        AS name,
                COALESCE(p.publisher, '')     AS publisher,
                COALESCE(v.version, '')       AS version
            FROM ids AS i
            LEFT JOIN names      AS n ON n.rowid = i.rowid
            LEFT JOIN manifest   AS m ON m.id    = i.rowid
            LEFT JOIN versions   AS v ON v.rowid = m.version
            LEFT JOIN publishers AS p ON p.rowid = m.publisher
        """
    else:
        query = """
            SELECT i.id AS id, n.name AS name, '' AS publisher, '' AS version
            FROM ids AS i
            LEFT JOIN names AS n ON n.rowid = i.rowid
        """

    cur.execute(query)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id":        row["id"],
            "name":      row["name"] or row["id"],
            "publisher": row["publisher"] or "",
            "version":   row["version"] or "",
        }
        for row in rows
        if row["id"]
    ]


def main() -> None:
    if not os.path.exists(DB_PATH):
        sys.exit(f"ERROR: Database not found at {DB_PATH!r}")

    print(f"Reading {DB_PATH!r}…")
    packages = fetch_packages(DB_PATH)
    print(f"Found {len(packages):,} packages.")

    if not packages:
        sys.exit("ERROR: No packages found — aborting.")

    os.makedirs(os.path.dirname(JSON_OUT) or ".", exist_ok=True)
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(packages, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(JSON_OUT) / 1024
    print(f"Wrote {JSON_OUT!r} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
