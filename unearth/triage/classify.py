"""Photo-vs-internet-image heuristics, ported from photo-digger.

Carving answers "what is on the disk".  This answers the question that comes
next, the one photo-digger was built for: among ten thousand recovered JPEGs,
which ones did a human being actually *shoot*?

The idea: real photos carry an EXIF fingerprint (camera model + capture
time/exposure), while images saved from the internet usually have the EXIF
stripped and come in standard screen resolutions.  We never look at pixels,
only at metadata - so it is fast, inspectable, and wrong in predictable ways.

Ported from photo-digger's classify.py (battle tested on a 500 GB recovery
disk: 7,902 JPEGs -> 1,560 candidates -> 450 confirmed camera photos).
"""

from __future__ import annotations

# Common screen resolutions (wallpaper / screenshot fingerprints)
SCREEN_RES = {
    (1920, 1080), (2560, 1440), (1366, 768), (1360, 768), (1280, 720),
    (1280, 800), (1440, 900), (1680, 1050), (1600, 900), (800, 600),
    (1024, 768), (1152, 864), (1280, 1024), (1920, 1200), (3840, 2160),
    (1024, 600), (1600, 1200), (2560, 1080), (3440, 1440), (1280, 960),
    (3200, 2000), (5120, 2880), (3200, 1800), (2560, 1600), (1920, 1160),
    (2880, 1800),
}

# Confidence ranking, highest first.  The first four are "candidates" worth
# a human's time; the internet_* ones are usually bulk-discarded.
CLASS_ORDER = [
    "camera_photo",         # camera model + capture time/params -> almost certainly real
    "likely_camera_photo",  # partial fingerprint -> very likely
    "maybe_photo",          # time or params survived, model stripped (WeChat et al.)
    "no_exif_unknown",      # no fingerprint at all, but dimensions not stock-junk
    "internet_screendim",   # exact screen resolution
    "internet_wallpaper",   # ultra-wide ratio, big
    "internet_small",       # tiny image
    "internet_smallsize",   # file size below any real photo
    "not_photo",            # not a still image at all
]

CANDIDATE_CLASSES = {"camera_photo", "likely_camera_photo",
                     "maybe_photo", "no_exif_unknown"}

CLASS_LABELS_ZH = {
    "camera_photo": "相机拍摄",
    "likely_camera_photo": "疑似相机",
    "maybe_photo": "部分指纹",
    "no_exif_unknown": "无EXIF待定",
    "internet_screendim": "屏幕分辨率图",
    "internet_wallpaper": "壁纸比例图",
    "internet_small": "小图",
    "internet_smallsize": "小文件图",
    "not_photo": "非照片",
}

# Below this many bytes a JPEG is almost never a real photo (thumbnails,
# emojis, UI assets).
REAL_PHOTO_MIN_BYTES = 60000


# Classes that only make sense for still images.
_IMAGE_ONLY = CLASS_ORDER[:4]

# Map non-photo kinds onto the same four-tier vocabulary so the album filter
# can stay one widget: a video is never "screen resolution wallpaper".
KIND_FALLBACK = {"video": "camera_photo", "audio": "maybe_photo"}


def classify_photo(kind, width, height, make, model, captured,
                   size, has_exposure_params=False) -> str:
    """One row -> one class.  Adapted from photo-digger's classify()."""
    if kind == "video":
        # A recovered clip is itself evidence of real life; EXIF rarely exists,
        # and a video file size/dimension never matches the junk filters below.
        return "camera_photo" if (make or model or captured) else "maybe_photo"
    if kind == "audio":
        return "maybe_photo"
    if kind != "photo":
        return "not_photo"
    w, h = width or 0, height or 0
    has_cam = bool(make and model) or bool(make) or bool(model)
    has_dt = bool(captured)
    if has_cam and (has_dt or has_exposure_params):
        return "camera_photo"
    if has_cam:
        return "likely_camera_photo"
    if has_exposure_params and has_dt:
        return "likely_camera_photo"
    if has_exposure_params or has_dt:
        return "maybe_photo"
    # no EXIF at all: judge by dimensions
    if w and h:
        if (w, h) in SCREEN_RES or (h, w) in SCREEN_RES:
            return "internet_screendim"
        ratio = max(w, h) / max(min(w, h), 1)
        if ratio > 2.4 and max(w, h) >= 1200:
            return "internet_wallpaper"
        if max(w, h) < 400:
            return "internet_small"
    if (size or 0) < REAL_PHOTO_MIN_BYTES:
        return "internet_smallsize"
    return "no_exif_unknown"


def class_rank(cls: str) -> int:
    try:
        return CLASS_ORDER.index(cls)
    except ValueError:
        return len(CLASS_ORDER)


def is_candidate(cls: str) -> bool:
    return cls in CANDIDATE_CLASSES
