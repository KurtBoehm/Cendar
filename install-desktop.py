#!/usr/bin/env python3

# This file is part of https://github.com/KurtBoehm/Cendar.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Install desktop entry and icon for Cendar into the current user’s XDG application
and icon directories (~/.local/share/...).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

APP_ID = "org.kurbo96.Cendar"


def main() -> None:
    project_dir = Path(__file__).resolve().parent
    data_dir = project_dir / "data"
    icon_dir = data_dir / "icons"

    home = Path.home()
    hicolor_dir = home / ".local" / "share" / "icons" / "hicolor"
    app_dir = home / ".local" / "share" / "applications"

    desktop_src = data_dir / f"{APP_ID}.desktop"
    icons = [
        (
            icon_dir / f"{APP_ID}.svg",
            hicolor_dir / "scalable" / "apps" / f"{APP_ID}.svg",
        ),
        (
            icon_dir / f"{APP_ID}-symbolic.svg",
            hicolor_dir / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg",
        ),
    ]

    if not desktop_src.is_file():
        raise SystemExit(f"Desktop file not found: {desktop_src}")
    for icon_src, _ in icons:
        if not icon_src.is_file():
            raise SystemExit(f"Icon file not found: {icon_src}")

    app_dir.mkdir(parents=True, exist_ok=True)
    desktop_dest = app_dir / f"{APP_ID}.desktop"
    print(f"Copying {desktop_src} -> {desktop_dest}")
    shutil.copy2(desktop_src, desktop_dest)

    for icon_src, icon_dst in icons:
        icon_dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"Copying {icon_src} -> {icon_dst}")
        shutil.copy2(icon_src, icon_dst)

    # Try to update the desktop database
    try:
        print("Updating desktop database...")
        subprocess.run(
            ["update-desktop-database", str(app_dir)],
        )
    except FileNotFoundError:
        print("update-desktop-database not found; skipping.")

    # Try to update the icon cache
    try:
        print("Updating icon cache...")
        subprocess.run(
            ["gtk4-update-icon-cache", "-t", "-f", str(hicolor_dir)],
        )
    except FileNotFoundError:
        print("gtk4-update-icon-cache not found; skipping.")

    print("Done. You should now see 'Cendar' in your app launcher.")


if __name__ == "__main__":
    main()
