<div align="center">

![Cendar Logo](https://raw.githubusercontent.com/KurtBoehm/Cendar/main/data/icons/org.kurbo96.Cendar.svg)

# Cendar

</div>

Cendar (from Quenya _cenda-_ “to watch (intensively), observe (for some time); to read” and _-r_ “agental suffix”, per [Eldamo](https://eldamo.org/content/word-indexes/words-q.html)) is a GTK 4 + libadwaita app for Linux that lets you scan pages with SANE, mark rectangular regions, and batch-export them as lossless JPEG XL.

## ✨ Features

- SANE-based scanning with per-device DPI and mode selection, plus progress and cancel.
- Timestamped **groups** of pages; reorder pages, move them between groups, and rotate pages with automatic region remapping.
- Draw and edit rectangular regions; numeric coordinate editing, simple crop presets, coordinate rounding, and copy/paste between pages.
- Per-region rotation and a **region preview** mode to see exactly what will be exported.
- Batch-export all regions as lossless **JPEG XL** with deterministic filenames; export runs in the background.
- Native GTK 4 + libadwaita UI with an adaptive split view (viewer/regions side-by-side or stacked).

## 📦 Requirements

- Linux with SANE backends
- Python 3.12+
- GTK 4, libadwaita, PyGObject
- `python-sane`, Pillow, NumPy, cairo bindings
- libvips with JPEG XL support (`pyvips`)

## 🛠️ Installation

The easiest way to install Cendar is using `pipx`:

```bash
pipx install git+https://github.com/KurtBoehm/Cendar
```

Make sure the system libraries for SANE, GTK 4, libadwaita, and libvips are installed (use your distribution’s packages).

After installation, Cendar can be started using just its name:

```bash
Cendar
```

## 🖥️ Desktop Integration

To add Cendar to your desktop’s application launcher with its icon, use the helper script from a local clone:

```bash
git clone https://github.com/KurtBoehm/Cendar
cd Cendar
./install-desktop.py
```

This will:

- Copy `data/org.kurbo96.Cendar.desktop` to `~/.local/share/applications/`
- Copy icons from `data/icons/` to `~/.local/share/icons/hicolor/`
- Try to update the desktop database and icon cache

Afterwards, look for **“Cendar”** in your app launcher.

> `install-desktop.py` only installs the desktop entry and icons.  
> Install the app itself via `pipx` (or another Python package method).

## ▶️ Basic Usage

1. Start `Cendar`.
2. In **Scanner and Settings**, choose a device, DPI, mode, default rotation, and output folder.
3. In **Groups and Pages**, click “+” to scan a new group of pages.
4. Select a page, then drag in the **Page Viewer** to create regions.
5. In **Regions**, adjust coordinates, apply presets or rounding, rotate/rename regions, or copy/paste them to other pages.
6. Click **Export** to write all regions as lossless JPEG XL images to the selected folder.

## 📜 Licence

Cendar is licensed under the terms of the Mozilla Public Licence 2.0, provided in [`License`](https://github.com/KurtBoehm/Cendar/blob/main/License).
