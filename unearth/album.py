"""Build the static album: one HTML file, embedded index, optional thumbnails.

No server, no dependencies, no network.  Double click ``album/index.html`` and
the whole dig site is browsable: a timeline of days, filter chips, a lightbox
with the provenance of every file (disk offset, original folder, camera), and
star/favourite + "export the current selection" for the curation step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import util
from .formats.exif import extract_jpeg_thumbnail
from .triage import dup_primaries

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "album_template.html")
THUMB_MAX = 420
EMBED_LIMIT = 200000


def _template() -> str:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# thumbnails
# --------------------------------------------------------------------------- #
def _pillow():
    try:
        from PIL import Image
        return Image
    except Exception:
        return None


def _thumb_jpeg_embedded(src_path: str, dest: str) -> bool:
    try:
        with open(src_path, "rb") as fh:
            prefix = fh.read(512 * 1024)
    except OSError:
        return False
    blob = extract_jpeg_thumbnail(prefix)
    if not blob:
        return False
    try:
        with open(dest, "wb") as fh:
            fh.write(blob)
        return True
    except OSError:
        return False


def _thumb_pillow(src_path: str, dest: str) -> bool:
    Image = _pillow()
    if Image is None:
        return False
    try:
        with Image.open(src_path) as im:
            im.draft("RGB", (THUMB_MAX * 2, THUMB_MAX * 2))
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            im.save(dest, "JPEG", quality=82, optimize=True)
        return True
    except Exception:
        return False


def _thumb_ffmpeg(src_path: str, dest: str) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    # 1s is a good poster frame for a long clip, but it lands past the end of a
    # short one, so retry from the very first frame.
    for ss in ("1", "0"):
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-ss", ss,
               "-i", src_path, "-frames:v", "1",
               "-vf", "scale=%d:-2" % THUMB_MAX, dest]
        try:
            subprocess.run(cmd, timeout=30, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return True
    return False


def make_thumbnail(row, out_dir: str, thumbs_dir: str) -> str:
    dest = os.path.join(thumbs_dir, "%d.jpg" % row["id"])
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return os.path.relpath(dest, os.path.dirname(thumbs_dir)).replace("\\", "/")
    src = os.path.join(out_dir, row["path"] or "")
    if not row["path"] or not os.path.exists(src):
        return ""
    ok = False
    ext = (row["ext"] or "").lower()
    if ext in ("jpg", "jpeg", "jpe"):
        ok = _thumb_jpeg_embedded(src, dest)
    if not ok and row["kind"] == "photo":
        ok = _thumb_pillow(src, dest)
    if not ok and row["kind"] in ("video", "audio"):
        ok = _thumb_ffmpeg(src, dest)
        if not ok and ext in ("mp4", "mov", "m4v", "3gp"):
            ok = _thumb_pillow(src, dest)  # some MP4s carry a cover art JPEG
    if not ok:
        return ""
    rel = os.path.relpath(dest, os.path.dirname(thumbs_dir)).replace("\\", "/")
    return rel


# --------------------------------------------------------------------------- #
# album
# --------------------------------------------------------------------------- #
def _item(row, album_dir: str, out_dir: str, thumb: str, dup_flag: int) -> dict:
    src = os.path.join(out_dir, row["path"] or "")
    rel = os.path.relpath(src, album_dir).replace("\\", "/") if row["path"] else ""
    item = {
        "id": row["id"],
        "k": row["kind"] or "other",
        "f": row["ext"] or "",
        "n": row["name"] or os.path.basename(row["path"] or ""),
        "p": rel,
        "sz": row["size"] or 0,
        "off": row["off"],
        "sc": row["score"] or 0,
        "c": int(row["complete"] or 0),
        "dup": dup_flag,
    }
    if row["day"]:
        item["d"] = row["day"]
    if row["captured"]:
        item["t"] = row["captured"]
    if row["width"]:
        item["w"] = row["width"]
    if row["height"]:
        item["h"] = row["height"]
    if row["duration"]:
        item["dur"] = round(row["duration"], 1)
    if row["make"]:
        item["mk"] = row["make"]
    if row["model"]:
        item["md"] = row["model"]
    if row["gps_lat"] is not None and row["gps_lon"] is not None:
        item["lat"] = round(row["gps_lat"], 6)
        item["lon"] = round(row["gps_lon"], 6)
    if thumb:
        item["th"] = thumb
    if row["note"]:
        item["note"] = row["note"]
    folder = _folder_hint(row, out_dir)
    if folder:
        item["o"] = folder
    return item


def _folder_hint(row, out_dir: str) -> str:
    if not row["path"]:
        return ""
    parts = row["path"].replace("\\", "/").split("/")
    if len(parts) >= 4 and parts[0] == "ntfs":
        return "/".join(parts[1:-1])
    return ""


def build_album(out_dir: str, cat, title: str = "拾遗 unearth",
                make_thumbs: bool = True, progress=None) -> str:
    album_dir = util.ensure_dir(os.path.join(out_dir, "album"))
    thumbs_dir = util.ensure_dir(os.path.join(album_dir, "thumbs"))
    primaries = dup_primaries(cat.conn)

    rows = list(cat.iter_files())
    items = []
    for row in rows:
        gid = row["dup_group"] or 0
        dup_flag = 1 if gid and primaries.get(gid) != row["id"] else 0
        thumb = ""
        if make_thumbs and row["path"]:
            thumb = make_thumbnail(row, out_dir, thumbs_dir)
        items.append(_item(row, album_dir, out_dir, thumb, dup_flag))
        if progress is not None:
            progress.update(1, extra="album %d/%d" % (len(items), len(rows)))
    cat.commit()

    # the album is only as good as its ordering hint: newest day first is handled
    # in the browser, but sort once here so ids in the json are already grouped.
    items.sort(key=lambda it: (it.get("d") or "9999", it.get("id", 0)), reverse=True)

    stats = cat.stats()
    payload = {
        "generated": util.now_iso(),
        "source": _source_label(cat),
        "stats": stats,
        "items": items[:EMBED_LIMIT],
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c").replace("&", "\\u0026")

    html = _template()
    header = "%s · %d 个文件" % (title, len(payload["items"]))
    html = html.replace("__TITLE__", header)
    html = html.replace("/*__DATA__*/", blob)
    index_path = os.path.join(album_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    with open(os.path.join(album_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return index_path


def _source_label(cat) -> str:
    row = cat.conn.execute(
        "SELECT target FROM scan ORDER BY id DESC LIMIT 1").fetchone()
    if row and row["target"]:
        return row["target"]
    row = cat.conn.execute(
        "SELECT spec FROM source ORDER BY id DESC LIMIT 1").fetchone()
    return row["spec"] if row else ""
