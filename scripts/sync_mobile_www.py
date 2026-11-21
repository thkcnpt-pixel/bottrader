"""Synchronize `frontend/index.html` and `frontend/app.js` to `mobile-android/www/`."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
ANDROID_WWW_DIR = ROOT_DIR / "mobile-android" / "www"


def sync_frontend_to_android() -> None:
    ANDROID_WWW_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("index.html", "app.js"):
        src = FRONTEND_DIR / fname
        dst = ANDROID_WWW_DIR / fname
        if src.exists():
            shutil.copyfile(src, dst)


if __name__ == "__main__":
    sync_frontend_to_android()
