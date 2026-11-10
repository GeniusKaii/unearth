"""Triage: rank, de-duplicate and prepare the catalogue for browsing.

Carving answers "what bytes are on this disk".  Triage answers the question the
user actually asks: *which of these are the photos of my life?*  The score is a
plain, inspectable sum of rules - capture time, camera identity, resolution,
GPS, completeness - not a model, so its mistakes are obvious and correctable.
"""

from __future__ import annotations

import os

from .. import util
from ..formats.registry import PROFILES
from .classify import (CANDIDATE_CLASSES, CLASS_LABELS_ZH, CLASS_ORDER,
                       classify_photo, class_rank, is_candidate)

KIND_BY_EXT = {}
for _ext in ("jpg jpeg jpe png gif bmp tif tiff heic heif webp avif dng cr2 nef "
             "arw orf rw2 pef srw raw").split():
    KIND_BY_EXT[_ext] = "photo"
for _ext in ("mp4 mov m4v 3gp 3g2 avi mkv webm wmv mpg mpeg m2ts ts flv".split()):
    KIND_BY_EXT[_ext] = "video"
for _ext in ("m4a mp3 wav aac flac ogg wma amr opus".split()):
    KIND_BY_EXT[_ext] = "audio"
for _ext in ("doc docx xls xlsx ppt pptx pdf txt rtf odt ods key pages numbers "
             "msg eml zip rar 7z".split()):
    KIND_BY_EXT[_ext] = "doc"

JUNK_WORDS = ("thumb", "thumbnail", "screenshot", "screen_shot", "鎴睆",
              "灞忓箷鎴浘", "cache", "temp", "tmp", ".nomedia", "icon", "logo")


def kind_from_ext(ext: str, default: str = "other") -> str:
    return KIND_BY_EXT.get((ext or "").lower(), default)


def score_row(row) -> int:
    """0..100, higher = more likely to be a photo/video of real life."""
    kind = row["kind"] or ""
    width = row["width"] or 0
    height = row["height"] or 0
    score = 0
    if kind == "video":
        score += 18
    elif kind == "photo":
        score += 12
    elif kind == "audio":
        score += 4
    if row["captured"]:
        score += 22
    if row["make"] or row["model"]:
        score += 14
    mp = (width * height) / 1e6 if width and height else 0.0
    if mp >= 6:
        score += 14
    elif mp >= 2:
        score += 11
    elif mp >= 0.5:
        score += 6
    elif mp > 0:
        score += 1
    if row["gps_lat"] is not None:
        score += 8
    if row["complete"]:
        score += 6
    name = (row["name"] or "").lower()
    if any(word in name for word in JUNK_WORDS):
        score -= 12
    if width and height and min(width, height) < 240:
        score -= 8
    if kind == "doc":
        score = min(score, 40)     # present, but do not crowd out the memories
    return max(0, min(100, score))


def assign_duplicates(conn) -> int:
    """Group byte identical files; returns the number of extra copies."""
    groups = {}
    for file_id, sha in conn.execute(
            "SELECT id, sha256 FROM file WHERE sha256 IS NOT NULL AND sha256<>'' ORDER BY id"):
        groups.setdefault(sha, []).append(file_id)
    gid = 0
    dupes = 0
    updates = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        gid += 1
        dupes += len(ids) - 1
        for file_id in ids:
            updates.append((gid, file_id))
    conn.executemany("UPDATE file SET dup_group=? WHERE id=?", updates)
    return dupes


def dup_primaries(conn) -> dict:
    """{group_id: first file id} - the copy the album/export keeps."""
    out = {}
    for gid, file_id in conn.execute(
            "SELECT dup_group, MIN(id) FROM file WHERE dup_group>0 GROUP BY dup_group"):
        out[int(gid)] = int(file_id)
    return out


def run_triage(out_dir: str, cat, progress=None) -> dict:
    """Classify, score, mark duplicates, and refresh the album."""
    rows = list(cat.iter_files())
    updates = []
    class_updates = []
    counts: dict = {}
    for row in rows:
        updates.append((score_row(row), row["id"]))
        cls = classify_photo(
            kind=row["kind"] or "", width=row["width"], height=row["height"],
            make=row["make"], model=row["model"], captured=row["captured"],
            size=row["size"],
            has_exposure_params=bool(
                row["exif_params"] if "exif_params" in row.keys() else 0),
        )
        counts[cls] = counts.get(cls, 0) + 1
        class_updates.append((cls, row["id"]))
    cat.conn.executemany("UPDATE file SET score=? WHERE id=?", updates)
    cat.conn.executemany("UPDATE file SET cls=? WHERE id=?",
                         [(c, i) for c, i in class_updates])
    cat.commit()
    dupes = assign_duplicates(cat.conn)
    cat.commit()

    from ..album import build_album

    album_path = build_album(out_dir, cat, progress=progress)
    return {"scored": len(rows), "dupes": dupes, "album": album_path,
            "by_class": counts}


def summarize(out_dir: str, cat) -> dict:
    stats = cat.stats()
    stats["by_class"] = {}
    for row in cat.conn.execute("SELECT cls, COUNT(*) n FROM file WHERE cls<>'' "
                                "GROUP BY cls ORDER BY n DESC"):
        stats["by_class"][row["cls"]] = row["n"]
    primaries = dup_primaries(cat.conn)
    conn = cat.conn
    unique = 0
    for row in conn.execute("SELECT id, dup_group FROM file"):
        gid = row["dup_group"] or 0
        if gid and primaries.get(gid) != row["id"]:
            continue
        unique += 1
    stats["unique"] = unique
    best = conn.execute(
        "SELECT day, COUNT(*) n FROM file WHERE day<>'' GROUP BY day ORDER BY n DESC LIMIT 5"
    ).fetchall()
    stats["top_days"] = [(r["day"], r["n"]) for r in best]
    stats["out_dir"] = out_dir
    return stats
