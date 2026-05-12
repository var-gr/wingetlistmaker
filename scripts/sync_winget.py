"""
sync_winget.py
1. Reads winget source SQLite (index.db) → flat package list, one row per ID
2. Fetches locale YAML from winget-pkgs GitHub → description + homepage URL
3. Writes data/packages.json
"""

import json
import os
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yaml
except ImportError:
    sys.exit("ERROR: pyyaml not installed. Run: pip install pyyaml")

DB_PATH  = os.environ.get("DB_PATH",  "/tmp/index.db")
JSON_OUT = os.environ.get("JSON_OUT", "data/packages.json")

GITHUB_RAW = "https://raw.githubusercontent.com/microsoft/winget-pkgs/master/manifests"
MANIFEST_WORKERS = 30
MANIFEST_TIMEOUT = 12


# ── Step 1: parse index.db ────────────────────────────────────────────────────

def fetch_packages(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    print(f"Tables: {sorted(tables)}")

    pub_col     = None
    pub_map_col = None
    if "norm_publishers" in tables:
        cur.execute("PRAGMA table_info(norm_publishers)")
        cols = [r[1] for r in cur.fetchall() if r[1] != "rowid"]
        pub_col = cols[0] if cols else None
        print(f"norm_publishers column: {pub_col}")
    if "norm_publishers_map" in tables:
        cur.execute("PRAGMA table_info(norm_publishers_map)")
        cols = [r[1] for r in cur.fetchall() if r[1] != "rowid"]
        pub_map_col = cols[1] if len(cols) > 1 else (cols[0] if cols else None)
        print(f"norm_publishers_map columns: {cols} → using {pub_map_col}")

    has_versions   = "versions"             in tables
    has_publishers = "norm_publishers"      in tables
    has_pub_map    = "norm_publishers_map"  in tables

    # Always drive from manifest so names, ids, versions are correctly linked.
    # manifest.id  → ids.rowid
    # manifest.name → names.rowid
    # manifest.version → versions.rowid
    if "manifest" in tables and has_versions:
        if has_publishers and has_pub_map and pub_col and pub_map_col:
            query = f"""
                SELECT
                    i.id                           AS id,
                    n.name                         AS name,
                    COALESCE(np.{pub_col}, '')     AS publisher,
                    MAX(v.version)                 AS version
                FROM manifest AS m
                LEFT JOIN ids                 AS i   ON i.rowid      = m.id
                LEFT JOIN names               AS n   ON n.rowid      = m.name
                LEFT JOIN versions            AS v   ON v.rowid      = m.version
                LEFT JOIN norm_publishers_map AS npm ON npm.manifest = m.rowid
                LEFT JOIN norm_publishers     AS np  ON np.rowid     = npm.{pub_map_col}
                WHERE i.id IS NOT NULL
                GROUP BY i.id
            """
        else:
            query = """
                SELECT i.id AS id, n.name AS name, '' AS publisher, MAX(v.version) AS version
                FROM manifest AS m
                LEFT JOIN ids      AS i ON i.rowid = m.id
                LEFT JOIN names    AS n ON n.rowid = m.name
                LEFT JOIN versions AS v ON v.rowid = m.version
                WHERE i.id IS NOT NULL
                GROUP BY i.id
            """
    else:
        query = """
            SELECT i.id AS id, i.id AS name, '' AS publisher, '' AS version
            FROM ids AS i
            GROUP BY i.id
        """

    cur.execute(query)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id":        row["id"],
            "name":      row["name"] or row["id"],
            "publisher": row["publisher"] or "",
            "version":   row["version"]   or "",
            "description": "",
            "url":         "",
            "icon":        "",
        }
        for row in rows if row["id"]
    ]


# ── Step 2: fetch manifest YAML from GitHub ───────────────────────────────────

def manifest_url(pkg_id: str, version: str) -> str:
    first     = pkg_id[0].lower()
    dot       = pkg_id.index(".")
    publisher = pkg_id[:dot]
    pkg_name  = pkg_id[dot + 1:]
    return f"{GITHUB_RAW}/{first}/{publisher}/{pkg_name}/{version}/{pkg_id}.locale.en-US.yaml"


def fetch_manifest(pkg: dict) -> tuple[str, dict]:
    pkg_id  = pkg["id"]
    version = pkg["version"]
    if not version:
        return pkg_id, {}
    try:
        url = manifest_url(pkg_id, version)
        req = urllib.request.Request(
            url, headers={"User-Agent": "winget-list-maker/1.0"}
        )
        with urllib.request.urlopen(req, timeout=MANIFEST_TIMEOUT) as resp:
            data = yaml.safe_load(resp.read().decode("utf-8")) or {}

        desc = (data.get("ShortDescription") or data.get("Description") or "").strip()
        home = (data.get("PackageUrl") or "").strip()

        # Extract first icon URL from the Icons array if present
        icon = ""
        icons = data.get("Icons") or []
        for entry in icons:
            if isinstance(entry, dict):
                candidate = entry.get("IconUrl", "")
                if candidate:
                    icon = candidate
                    break

        return pkg_id, {"description": desc[:300], "url": home, "icon": icon}
    except Exception:
        return pkg_id, {}


def enrich_packages(packages: list[dict]) -> None:
    print(f"Fetching manifest data for {len(packages):,} packages "
          f"({MANIFEST_WORKERS} workers)…")
    done  = 0
    found = 0
    with ThreadPoolExecutor(max_workers=MANIFEST_WORKERS) as pool:
        futures = {pool.submit(fetch_manifest, p): p["id"] for p in packages}
        pkg_map = {p["id"]: p for p in packages}
        for future in as_completed(futures):
            pkg_id, info = future.result()
            if info:
                pkg_map[pkg_id].update(info)
                if info.get("url"):
                    found += 1
            done += 1
            if done % 1000 == 0 or done == len(packages):
                print(f"  {done:,} / {len(packages):,}  ({found:,} with URLs)")


# ── Step 3: write JSON ────────────────────────────────────────────────────────

def main() -> None:
    if not os.path.exists(DB_PATH):
        sys.exit(f"ERROR: {DB_PATH!r} not found")

    print(f"Reading {DB_PATH!r}…")
    packages = fetch_packages(DB_PATH)
    print(f"Found {len(packages):,} unique packages.")
    if not packages:
        sys.exit("ERROR: No packages — aborting.")

    enrich_packages(packages)

    os.makedirs(os.path.dirname(JSON_OUT) or ".", exist_ok=True)
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(packages, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(JSON_OUT) / 1024
    print(f"Wrote {JSON_OUT!r}  ({size_kb:.0f} KB, {len(packages):,} packages)")


if __name__ == "__main__":
    main()
