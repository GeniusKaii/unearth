"""Copy a curated selection out of the recovery directory."""

from __future__ import annotations

import json
import os
import shutil

from . import util
from .triage import dup_primaries


def load_picks(path: str) -> list:
    """Read the ``picked.json`` the album downloads."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, list):
        return [int(x) for x in payload]
    ids = payload.get("ids") or []
    return [int(x) for x in ids]


def export_selection(out_dir: str, cat, dest_dir: str, ids=None,
                     only_complete: bool = False, flat: bool = False,
                     progress=None) -> dict:
    primaries = dup_primaries(cat.conn)
    rows = list(cat.iter_files())
    if ids is not None:
        wanted = set(int(i) for i in ids)
        rows = [r for r in rows if r["id"] in wanted]

    dest_dir = util.ensure_dir(dest_dir)
    seen_sha = set()
    taken = set()
    stats = {"copied": 0, "bytes": 0, "skipped_missing": 0, "skipped_dup": 0,
             "skipped_partial": 0, "dest": os.path.abspath(dest_dir)}

    for row in rows:
        gid = row["dup_group"] or 0
        if gid and primaries.get(gid) != row["id"]:
            stats["skipped_dup"] += 1
            continue
        sha = row["sha256"] or ""
        if sha and sha in seen_sha:
            stats["skipped_dup"] += 1
            continue
        if only_complete and not row["complete"]:
            stats["skipped_partial"] += 1
            continue
        src = util.locate_file(out_dir, row)
        if not src or not os.path.exists(src):
            stats["skipped_missing"] += 1
            continue
        if not flat:
            sub = os.path.join(dest_dir, row["day"] or "undated")
        else:
            sub = dest_dir
        util.ensure_dir(sub)
        name = util.safe_filename(row["name"] or os.path.basename(src), 110)
        target = util.unique_path(sub, name, taken)
        try:
            shutil.copy2(src, target)
        except OSError:
            stats["skipped_missing"] += 1
            continue
        if sha:
            seen_sha.add(sha)
        stats["copied"] += 1
        stats["bytes"] += os.path.getsize(target)
        if progress is not None:
            progress.update(1, extra="%d copied" % stats["copied"])
    return stats
