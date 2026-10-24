"""T-72 check: no combo in the live 9Router database lists the same model twice.

A combo is an ordered pool, so a repeated value is always corruption - the model
picker refuses to add a duplicate and the editor used to create one by committing
a stale edit buffer (a rename wrote the previous occupant's name into the row).

Reads a throwaway copy of the database, never the live file, so it is safe to run
while 9Router is up. Run: python tools/verify_combo_model_integrity.py
"""

import collections
import json
import os
import shutil
import sqlite3
import sys
import tempfile

APPDATA = os.environ.get("APPDATA", r"C:\Users\vac34\AppData\Roaming")
DB = os.path.join(APPDATA, "9router", "db", "data.sqlite")


def main():
    if not os.path.isfile(DB):
        raise SystemExit(f"FAIL: no 9Router database at {DB}")
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            src = DB + suffix
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(tmp, "data.sqlite" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "data.sqlite"))
        try:
            rows = con.execute("SELECT id, name, models FROM combos").fetchall()
        finally:
            con.close()

    failures = []
    for cid, name, models in rows:
        try:
            arr = json.loads(models or "[]")
        except ValueError as exc:
            failures.append(f"{name}: models is not JSON ({exc})")
            continue
        dup = [v for v, n in collections.Counter(arr).items() if n > 1]
        if dup:
            failures.append(f"{name} (id={cid}): duplicated {dup}")
        if any(not str(v).strip() for v in arr):
            failures.append(f"{name} (id={cid}): empty model entry")

    if failures:
        print("FAIL")
        for line in failures:
            print(" -", line)
        return 1
    print(f"PASS: {len(rows)} combos, no duplicated or empty model entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
