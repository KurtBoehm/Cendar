# 📄 Cendar

Cendar (from Quenya _cenda-_ “to watch (intensively), observe (for some time); to read”) allows you to scan pages with SANE, define rectangular regions visually, and batch‑export cropped images using a GTK 4 + libadwaita interface.

## ✨ Features

- Discover and configure SANE-compatible scanners
- Scan pages into logical groups (batches)
- Rotate pages and manage per-page regions visually
- Drag to create regions; edit coordinates numerically; rotate and preview regions
- Copy/paste regions between pages
- Batch‑export all regions to lossless JPEG XL files

## 📦 Requirements

- Linux (SANE backends supported)
- Python 3.12+
- GTK 4, libadwaita, and PyGObject
- SANE and `python-sane`
- Pillow, NumPy, cairo bindings

## 🛠️ Installation

The easiest way to install Cendar is using `pipx`:

```bash
pipx install git+https://github.com/KurtBoehm/Cendar
```

Make sure your system has SANE, GTK 4, libadwaita, and the relevant development
packages installed (see your distribution’s documentation).

## ▶️ Usage

After installation with `pipx`, Cendar can easily be started using just its name:

```bash
Cendar
```

Then:

1. Select a scanner and scan DPI/mode.
2. Click “+” to scan a new group of pages.
3. Drag on the page viewer to create regions; refine coordinates in the sidebar.
4. Click “Export” to write all regions as JPEG XL files to the chosen output folder.

## 📜 License

Cendar is licensed under the terms of the Mozilla Public Licence 2.0, provided in [`License`](https://github.com/KurtBoehm/Cendar/blob/main/License).
