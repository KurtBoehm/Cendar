# This file is part of https://github.com/KurtBoehm/Cendar.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, final, override

import ctypes
import numpy as np
import gi
from PIL import Image as PILImage
from pyvips import Image
from OpenGL import GL

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GObject, Gdk, Gio, GLib, Gtk  # noqa: E402  # pyright: ignore[reportMissingModuleSource]

if TYPE_CHECKING:
    import sane

__version__ = "1.0.0"

PILImage.MAX_IMAGE_PIXELS = 1 << 30

CropPreset = Literal["full", "preset_1200_1700"]
RegionHandle = Literal[
    "left",
    "right",
    "top",
    "bottom",
    "top_left",
    "top_right",
    "bottom_left",
    "bottom_right",
]

_ROTATION_LABEL_TO_CCW: dict[str, int] = {
    "0°": 0,
    "90° CW": 270,
    "180°": 180,
    "90° CCW": 90,
}
_ROTATION_LABELS: tuple[str, ...] = tuple(_ROTATION_LABEL_TO_CCW.keys())
_CCW_TO_ROTATION_LABEL: dict[int, str] = {
    v: k for k, v in _ROTATION_LABEL_TO_CCW.items()
}

_CROP_LABEL_TO_PRESET: dict[str, CropPreset] = {
    "Full image": "full",
    "Preset 1200x1700": "preset_1200_1700",
}
_CROP_PRESET_LABELS: tuple[str, ...] = tuple(_CROP_LABEL_TO_PRESET.keys())
_REGION_CROP_PRESET_LABELS: tuple[str, ...] = ("(Choose)",) + _CROP_PRESET_LABELS

_REGION_BORDER_WIDTH = 4
_SIDEBAR_DUMMY_SUBTITLE = ""

# --- Data classes and configuration ---


@dataclass
class AppDefaults:
    """Defaults for newly scanned pages and regions."""

    default_rotation_ccw: int = 0
    default_crop_preset: CropPreset = "full"


@dataclass
class AppSettings:
    """Application-wide settings editable from the UI."""

    resolution: int = 600
    folder: Path = field(default_factory=Path.cwd)
    defaults: AppDefaults = field(default_factory=AppDefaults)


@dataclass
class Region:
    """Rectangular region on a page, in image pixel coordinates."""

    id: str
    name: str
    x1: int
    y1: int
    x2: int
    y2: int
    rotation: int = 0  # degrees clockwise (0, 90, 180, 270)


@dataclass
class Page:
    """A scanned page and its regions."""

    id: str
    pil_image: PILImage.Image
    rotation: int = 0
    regions: list[Region] = field(default_factory=list)


@dataclass
class PageGroup:
    """Logical group of pages (e.g. one scan batch)."""

    id: str
    name: str
    pages: list[Page] = field(default_factory=list)


# --- Main application window (libadwaita + GTK 4) ---


@final
class ScannerWindow(Adw.ApplicationWindow):
    """Main window for scanning, managing pages, and exporting regions."""

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app)
        self.set_title("Cendar")
        self.set_default_size(1400, 900)
        self.set_size_request(400, 600)

        # Global settings model
        self.settings = AppSettings()

        # SANE-related state
        self.scanner_dev: "sane.SaneDev | None" = None
        self.available_devices: list[tuple[str, str, str, str]] = []
        self.selected_device_name: str | None = None
        self._sane_initialized = False

        # Data model
        self.page_groups: list[PageGroup] = []
        self.selected_group: PageGroup | None = None
        self.selected_page: Page | None = None

        # IDs of regions whose ExpanderRow is expanded
        # (expanded regions are also highlighted in the viewer)
        self.expanded_region_ids: set[str] = set()

        self.copied_regions: list[Region] = []

        # Scan threading
        self._scan_thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self._scan_cancel_event = threading.Event()

        # Drawing state
        self._display_scale: float = 1.0
        self._drag_rect: tuple[float, float, float, float] | None = None

        # Current cursor shape for the viewer (None = default)
        self._viewer_cursor_name: str | None = None

        # Offset of the image inside the DrawingArea (for centering)
        self._display_offset_x: float = 0.0
        self._display_offset_y: float = 0.0

        # If not None, Page Viewer shows only this region’s crop instead of full page
        self._preview_region_id: str | None = None

        # Map region.id -> its “eye” preview button (for highlighting)
        self._region_preview_buttons: dict[str, Gtk.Button] = {}

        # Active region-resize drag state
        self._resize_region_id: str | None = None
        self._resize_handle: RegionHandle | None = None
        self._resize_start_x: float = 0.0
        self._resize_start_y: float = 0.0

        # Widgets
        self._viewer_paned_viewer_box: Gtk.Box
        self._viewer_paned_regions_box: Gtk.Box
        self.viewer_paned: Gtk.Paned

        self.groups_list: Gtk.ListBox
        self.regions_list: Gtk.ListBox

        self.device_store: Gtk.StringList
        self.dpi_store: Gtk.StringList
        self.mode_store: Gtk.StringList
        self.default_rot_store: Gtk.StringList
        self.default_crop_store: Gtk.StringList

        self.scanner_row: Adw.ComboRow
        self.dpi_combo: Adw.ComboRow
        self.mode_combo: Adw.ComboRow
        self.default_rot_combo: Adw.ComboRow
        self.default_crop_combo: Adw.ComboRow

        self.folder_entry: Adw.EntryRow

        self.btn_new_group: Gtk.Button
        self.btn_clear_groups: Gtk.Button
        self.btn_export: Gtk.Button
        self.btn_refresh_scanners: Gtk.Button
        self.btn_browse_folder: Gtk.Button

        self.drawing_area: Gtk.GLArea

        # GL preview state (texture + key to know when to rebuild)
        self._gl_use_texel_fetch: bool = False
        self._gl_is_gles: bool = False
        self._gl_glsl_version_num: int = 100  # e.g. 100, 300, 330
        self._gl_tex_id: int | None = None
        self._gl_tex_w: int = 0
        self._gl_tex_h: int = 0
        self._image_w: int = 0
        self._image_h: int = 0
        self._gl_tex_key: (
            tuple[str, int, int, str, int, int]
            | tuple[str, int, int, str, str, int, int, int, int, int, int, int]
        ) | None = None
        self._gl_preview_active: bool = False

        # GL shader pipeline
        self._gl_program: int = 0
        self._gl_attr_pos: int = -1
        self._gl_attr_texcoord: int = -1
        self._gl_uniform_use_tex: int = -1
        self._gl_uniform_color: int = -1
        self._gl_uniform_sampler: int = -1
        self._gl_vbo: int | None = None
        self._gl_vao: int | None = None

        self._gl_uniform_rect_min: int = -1
        self._gl_uniform_rect_max: int = -1
        self._gl_uniform_border: int = -1
        self._gl_uniform_viewport_size: int = -1

        # Guards
        self._suppress_group_expand_signal: bool = False
        self._suppress_scanner_row_signal: bool = False
        self._suppress_region_coord_update: bool = False

        self._install_css()
        self._build_ui()

        self.refresh_groups_list()
        self.refresh_regions_list()

        self._start_initial_sane_init()

    def _detect_gl_version(self) -> None:
        # Called after make_current()
        ver = GL.glGetString(GL.GL_VERSION)
        sl = GL.glGetString(GL.GL_SHADING_LANGUAGE_VERSION)
        if ver is None or sl is None:
            return

        ver_s = ver.decode("ascii", "ignore")
        sl_s = sl.decode("ascii", "ignore")

        # Very simple ES detection
        self._gl_is_gles = "OpenGL ES" in ver_s

        # Parse "x.y" from the GLSL version string
        m = re.search(r"(\d+)\.(\d+)", sl_s)
        if m:
            glsl_major = int(m.group(1))
            glsl_minor = int(m.group(2))
            # For ES this is 1.00, 3.00, 3.10 → 100, 300, 310, ...
            # For desktop this is 1.20, 3.30 → 120, 330, ...
            self._gl_glsl_version_num = glsl_major * 100 + glsl_minor

        # texelFetch availability:
        # - GLES: fragment texelFetch is core in GLSL ES 3.00+ (ES 3.0+)
        # - Desktop: texelFetch is core in GLSL 1.30+ (GL 3.0+)
        if self._gl_is_gles:
            self._gl_use_texel_fetch = self._gl_glsl_version_num >= 300
        else:
            self._gl_use_texel_fetch = self._gl_glsl_version_num >= 130

    # --- Small internal helpers/factories ---

    @staticmethod
    def _string_list_set(store: Gtk.StringList | None, values: list[str]) -> None:
        """Replace all items in a `Gtk.StringList` with the given values."""
        if store is None:
            return
        store.splice(0, store.get_n_items(), values)

    @staticmethod
    def _normalize_and_clamp_region(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        img_w: int,
        img_h: int,
    ) -> tuple[int, int, int, int] | None:
        """
        Normalize `(x1, y1, x2, y2)` to top-left/bottom-right order
        and clamp to image bounds.
        """
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1

        x1 = max(0, min(x1, img_w - 1))
        x2 = max(0, min(x2, img_w))
        y1 = max(0, min(y1, img_h - 1))
        y2 = max(0, min(y2, img_h))

        if x2 <= x1 or y2 <= y1:
            return None
        return x1, y1, x2, y2

    @staticmethod
    def _plural(n: int, singular: str, plural: str | None = None) -> str:
        """Return a simple “n thing(s)” string with correct pluralization."""
        if plural is None:
            plural = singular + "s"
        return f"{n} {singular if n == 1 else plural}"

    @staticmethod
    def _rotation_ccw_from_display(label: str) -> int:
        """Map a human-readable rotation label to a CCW angle."""
        return _ROTATION_LABEL_TO_CCW.get(label.strip(), 0)

    @staticmethod
    def _rotation_display_from_ccw(deg_ccw: int) -> str:
        """Map a CCW angle to a human-readable rotation label."""
        return _CCW_TO_ROTATION_LABEL.get(deg_ccw % 360, "0°")

    @staticmethod
    def _crop_preset_from_display(label: str) -> CropPreset:
        """Map a crop preset label to its internal identifier."""
        return _CROP_LABEL_TO_PRESET.get(label.strip(), "full")

    def _calc_preset_region(
        self, img_w: int, img_h: int, preset: CropPreset
    ) -> tuple[int, int, int, int]:
        """Return the default region rectangle for a given preset and image size."""
        if preset == "preset_1200_1700":
            res = self._normalize_and_clamp_region(
                1200, 1700, img_w, img_h, img_w, img_h
            )
            if res is not None:
                return res
        return 0, 0, img_w, img_h

    def _new_region(
        self,
        *,
        name: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        rotation: int = 0,
    ) -> Region:
        """Create a new Region with a fresh UUID."""
        return Region(
            id=str(uuid.uuid4()),
            name=name,
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            rotation=rotation,
        )

    # --- CSS ---

    def _install_css(self) -> None:
        """Install small application-specific CSS snippets."""
        css = b"""
        .flat-paned > separator {
            color: transparent;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # --- SANE integration ---

    def _init_sane(self) -> None:
        """Initialize the SANE backend if it has not been initialized yet."""
        if self._sane_initialized:
            return
        import sane

        sane.init()
        self._sane_initialized = True

    def _exit_sane(self) -> None:
        """Shut down the SANE backend if it is active."""
        if not self._sane_initialized:
            return
        import sane

        sane.exit()
        self._sane_initialized = False

    def _list_sane_devices(self) -> list[tuple[str, str, str, str]]:
        """Return the list of available SANE scanner devices."""
        import sane

        self._init_sane()
        return sane.get_devices()

    def _start_initial_sane_init(self) -> None:
        """Kick off initial SANE initialization and device detection."""
        self._set_scanning_buttons_state(False)
        self._set_progress(0, 0, "Initializing scanner…")
        threading.Thread(target=self._initial_sane_worker, daemon=True).start()

    def _update_device_store_and_selection(self) -> None:
        """Update the device combo model and select a preferred device, if any."""
        display = [
            f"{name} ({vendor} {model})"
            for name, vendor, model, _ in self.available_devices
        ]

        self._suppress_scanner_row_signal = True
        try:
            self._string_list_set(self.device_store, display)

            prefix = "pixma:"
            preferred_index: int | None = None
            for idx, (name, _, _, _) in enumerate(self.available_devices):
                if name.startswith(prefix):
                    preferred_index = idx
                    break
            if preferred_index is None and self.available_devices:
                preferred_index = 0

            if preferred_index is not None:
                self.selected_device_name = self.available_devices[preferred_index][0]
                self.scanner_row.set_selected(preferred_index)
            else:
                self.selected_device_name = None
                if self.device_store.get_n_items():
                    self.scanner_row.set_selected(0)
        finally:
            self._suppress_scanner_row_signal = False

    def _initial_sane_worker(self) -> None:
        """Background worker for initial SANE initialization and device opening."""
        available_devices: list[tuple[str, str, str, str]] = []
        err: str | None = None

        try:
            self._set_progress(1, 3, "Detecting scanners…")
            available_devices = self._list_sane_devices()
        except Exception as e:
            err = f"Failed to initialize scanner: {e}"

        def finish() -> None:
            if err is not None:
                self.available_devices = []
                self._string_list_set(self.device_store, [])
                self.selected_device_name = None
                self._set_scanning_buttons_state(True)
                self._reset_progress()
                self._error_dialog("Scanner error", err)
                return

            self.available_devices = available_devices
            self._update_device_store_and_selection()

            self._set_progress(2, 3, "Opening scanner…")
            if self.selected_device_name is not None:
                self._apply_scanner()

            self._set_scanning_buttons_state(True)
            self._reset_progress()

        GLib.idle_add(finish)

    # --- UI construction ---

    def _build_ui(self) -> None:
        """Construct the main window layout (sidebar + content)."""
        nav_split = Adw.NavigationSplitView()
        nav_split.set_min_sidebar_width(380)
        nav_split.set_max_sidebar_width(500)
        self.set_content(nav_split)
        self.nav_split = nav_split

        bpc = Adw.BreakpointCondition.new_length(
            Adw.BreakpointConditionLengthType.MAX_WIDTH, 800, Adw.LengthUnit.SP
        )
        bp = Adw.Breakpoint.new(bpc)
        bp.add_setter(nav_split, "collapsed", True)
        self.add_breakpoint(bp)

        # Sidebar
        sidebar_tv = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        self.sidebar_title = Adw.WindowTitle.new(
            "Scanner Page Manager", _SIDEBAR_DUMMY_SUBTITLE
        )
        sidebar_header.set_title_widget(self.sidebar_title)

        self.sidebar_progress_bar = Gtk.ProgressBar()
        self.sidebar_progress_bar.add_css_class("osd")
        self.sidebar_progress_bar.set_hexpand(True)
        self.sidebar_progress_bar.set_halign(Gtk.Align.FILL)
        self.sidebar_progress_bar.set_valign(Gtk.Align.END)
        self.sidebar_progress_bar.set_visible(False)

        self.sidebar_btn_cancel_scan = self._icon_only_button(
            "process-stop-symbolic", "Cancel scan"
        )
        self.sidebar_btn_cancel_scan.set_valign(Gtk.Align.END)
        self.sidebar_btn_cancel_scan.set_halign(Gtk.Align.END)
        self.sidebar_btn_cancel_scan.set_visible(False)
        self.sidebar_btn_cancel_scan.set_sensitive(False)
        self.sidebar_btn_cancel_scan.connect("clicked", self.on_cancel_scan_clicked)
        self.sidebar_btn_cancel_scan.set_margin_end(6)
        self.sidebar_btn_cancel_scan.set_margin_bottom(4)

        sidebar_header_overlay = Gtk.Overlay.new()
        sidebar_header_overlay.set_child(sidebar_header)
        sidebar_header_overlay.add_overlay(self.sidebar_progress_bar)
        sidebar_header_overlay.add_overlay(self.sidebar_btn_cancel_scan)
        sidebar_tv.add_top_bar(sidebar_header_overlay)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar_box.set_margin_top(6)
        sidebar_box.set_margin_bottom(6)
        sidebar_box.set_margin_start(6)
        sidebar_box.set_margin_end(3)
        sidebar_box.set_hexpand(False)
        sidebar_box.set_vexpand(True)
        sidebar_tv.set_content(sidebar_box)

        self._build_scanner_settings(sidebar_box)
        self._build_groups_panel(sidebar_box)

        sidebar_page = Adw.NavigationPage.new(sidebar_tv, "Navigation")
        nav_split.set_sidebar(sidebar_page)

        # Content
        content_tv = Adw.ToolbarView()
        content_header = Adw.HeaderBar()
        self.content_title = Adw.WindowTitle.new(
            "No page selected",
            "Scan, crop, and export",
        )
        content_header.set_title_widget(self.content_title)

        export_content = Adw.ButtonContent()
        export_content.set_icon_name("document-save-symbolic")
        export_content.set_label("Export")
        self.btn_export = Gtk.Button(
            child=export_content,
            css_classes=["suggested-action"],
        )
        self.btn_export.connect("clicked", lambda _b: self.export_changes())
        content_header.pack_end(self.btn_export)

        content_tv.add_top_bar(content_header)

        main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=3,
            margin_end=6,
            hexpand=True,
            vexpand=True,
        )
        content_tv.set_content(main_box)

        # Viewer above Regions, vertically resizable via Gtk.Paned
        paned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_position(500)
        paned.add_css_class("flat-paned")
        self.viewer_paned = paned
        main_box.append(paned)

        top_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top_box.set_hexpand(True)
        top_box.set_vexpand(True)

        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bottom_box.set_hexpand(True)
        bottom_box.set_vexpand(True)

        # Remember which box is viewer vs regions so we can swap/reposition
        self._viewer_paned_viewer_box = top_box
        self._viewer_paned_regions_box = bottom_box

        paned.set_start_child(top_box)
        paned.set_end_child(bottom_box)

        paned.connect("notify::orientation", self._on_viewer_paned_orientation_changed)

        paned_bp_cond = Adw.BreakpointCondition.new_length(
            Adw.BreakpointConditionLengthType.MIN_WIDTH, 1200, Adw.LengthUnit.SP
        )
        paned_bp = Adw.Breakpoint.new(paned_bp_cond)
        paned_bp.add_setter(paned, "orientation", Gtk.Orientation.HORIZONTAL)
        self.add_breakpoint(paned_bp)

        self._build_page_panel(top_box)
        self._build_regions_panel(bottom_box)

        content_page = Adw.NavigationPage.new(content_tv, "Content")
        nav_split.set_content(content_page)

    def _build_card(
        self,
        parent: Gtk.Box,
        title: str | None,
        hexpand: bool = True,
        vexpand: bool = True,
        pad_title: bool = True,
    ) -> Gtk.Box:
        """Create a simple card-style box with an optional title."""
        spacing = 6 if pad_title else 0
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        outer.set_hexpand(hexpand)
        outer.set_vexpand(vexpand)
        parent.append(outer)

        if title is not None:
            title_label = Gtk.Label(label=title, xalign=0)
            title_label.add_css_class("heading")
            title_label.add_css_class("h4")
            if pad_title:
                title_label.set_margin_top(8)
                title_label.set_margin_bottom(2)
                title_label.set_margin_start(6)
                title_label.set_margin_end(6)
            outer.append(title_label)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.append(inner)

        return inner

    def _create_scrolled_list(
        self, *, separate: bool = False
    ) -> tuple[Gtk.ScrolledWindow, Gtk.ListBox]:
        """Create a scrolled `ListBox` with standard margins and styling."""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_propagate_natural_height(True)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.set_hexpand(True)
        wrapper.set_vexpand(True)
        scrolled.set_child(wrapper)

        lb = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            css_classes=["boxed-list-separate" if separate else "boxed-list"],
            vexpand=False,
            valign=Gtk.Align.START,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        wrapper.append(lb)

        return scrolled, lb

    def _icon_only_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
        """Create a flat button that only shows an icon and optional tooltip."""
        btn = Gtk.Button()
        img = Gtk.Image.new_from_icon_name(icon_name)
        btn.set_child(img)
        btn.add_css_class("flat")
        btn.set_valign(Gtk.Align.CENTER)
        if tooltip:
            btn.set_tooltip_text(tooltip)
        return btn

    def _add_placeholder_row(
        self,
        listbox: Gtk.ListBox,
        title: str,
        subtitle: str,
    ) -> None:
        """Append a disabled placeholder row with title and subtitle."""
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.add_css_class("dim-label")
        row.set_activatable(False)
        row.set_selectable(False)
        listbox.append(row)

    @staticmethod
    def _set_many_sensitive(enabled: bool, *widgets: Gtk.Widget | None) -> None:
        """Set the sensitivity of multiple widgets at once."""
        for w in widgets:
            if w is not None:
                w.set_sensitive(enabled)

    # --- Sidebar: settings + groups ---

    def _build_scanner_settings(self, parent: Gtk.Box) -> None:
        """Build the scanner and global settings section in the sidebar."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        parent.append(outer)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(6)
        header.set_margin_end(6)
        outer.append(header)

        title_label = Gtk.Label(label="Scanner and Settings", xalign=0)
        title_label.add_css_class("heading")
        title_label.add_css_class("h4")
        title_label.set_hexpand(True)
        title_label.set_halign(Gtk.Align.START)
        header.append(title_label)

        self.btn_refresh_scanners = self._icon_only_button(
            "view-refresh-symbolic", "Refresh scanners"
        )
        self.btn_refresh_scanners.connect(
            "clicked", lambda _b: self._refresh_scanner_list()
        )
        header.append(self.btn_refresh_scanners)

        self.device_store = Gtk.StringList.new([])
        self.dpi_store = Gtk.StringList.new([])
        self.mode_store = Gtk.StringList.new([])
        self.default_rot_store = Gtk.StringList.new(list(_ROTATION_LABELS))
        self.default_crop_store = Gtk.StringList.new(list(_CROP_PRESET_LABELS))

        settings_list = Gtk.ListBox()
        settings_list.set_selection_mode(Gtk.SelectionMode.NONE)
        settings_list.add_css_class("boxed-list")
        settings_list.set_margin_bottom(6)
        settings_list.set_margin_start(6)
        settings_list.set_margin_end(6)
        outer.append(settings_list)

        self.scanner_row = Adw.ComboRow(title="Device", model=self.device_store)
        self.scanner_row.connect("notify::selected", self.on_scanner_row_changed)
        settings_list.append(self.scanner_row)

        self.dpi_combo = Adw.ComboRow(title="Resolution (DPI)", model=self.dpi_store)
        self.dpi_combo.connect("notify::selected", self.on_dpi_changed)
        settings_list.append(self.dpi_combo)

        self.mode_combo = Adw.ComboRow(title="Scan mode", model=self.mode_store)
        self.mode_combo.connect("notify::selected", self.on_mode_changed)
        settings_list.append(self.mode_combo)

        self.default_rot_combo = Adw.ComboRow(
            title="Default rotation", model=self.default_rot_store
        )
        self._sync_default_rot_combo_from_settings()
        self.default_rot_combo.connect(
            "notify::selected", self.on_default_rotation_changed
        )
        settings_list.append(self.default_rot_combo)

        self.default_crop_combo = Adw.ComboRow(
            title="Default crop", model=self.default_crop_store
        )
        self.default_crop_combo.set_selected(
            0 if self.settings.defaults.default_crop_preset == "full" else 1
        )
        self.default_crop_combo.connect(
            "notify::selected", self.on_default_crop_preset_changed
        )
        settings_list.append(self.default_crop_combo)

        self.folder_entry = Adw.EntryRow(title="Output folder")
        self.folder_entry.set_text(str(self.settings.folder))
        self.folder_entry.connect("notify::text", self.on_folder_changed)
        self.btn_browse_folder = self._icon_only_button(
            "folder-open-symbolic", "Browse…"
        )
        self.btn_browse_folder.connect("clicked", self.on_browse_folder)
        self.folder_entry.add_suffix(self.btn_browse_folder)
        settings_list.append(self.folder_entry)

    def _build_groups_panel(self, parent: Gtk.Box) -> None:
        """Build the Groups and Pages section in the sidebar."""
        grp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        grp_box.set_hexpand(True)
        grp_box.set_vexpand(True)
        parent.append(grp_box)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(6)
        header.set_margin_end(6)
        grp_box.append(header)

        title_label = Gtk.Label(label="Groups and Pages", xalign=0)
        title_label.add_css_class("heading")
        title_label.add_css_class("h4")
        title_label.set_hexpand(True)
        title_label.set_halign(Gtk.Align.START)
        header.append(title_label)

        self.btn_new_group = self._icon_only_button(
            "list-add-symbolic", "New group from scan"
        )
        self.btn_new_group.connect("clicked", lambda _b: self._start_scan("new_group"))
        header.append(self.btn_new_group)

        self.btn_clear_groups = self._icon_only_button(
            "user-trash-symbolic", "Clear all groups"
        )
        self.btn_clear_groups.connect("clicked", lambda _b: self.clear_groups())
        header.append(self.btn_clear_groups)

        scrolled, self.groups_list = self._create_scrolled_list()
        grp_box.append(scrolled)

    # --- Bottom pane: regions list ---

    def _build_regions_panel(self, parent: Gtk.Box) -> None:
        """Build the Regions list panel."""
        reg_box = self._build_card(
            parent, "Regions", hexpand=True, vexpand=True, pad_title=True
        )

        scrolled, self.regions_list = self._create_scrolled_list(separate=True)
        reg_box.append(scrolled)

    # --- Top pane: page viewer ---

    def _build_page_panel(self, parent: Gtk.Box) -> None:
        """Build the Page Viewer panel with GL rendering and gestures."""
        viewer_box = self._build_card(
            parent, "Page Viewer", hexpand=True, vexpand=True, pad_title=True
        )

        self.drawing_area = Gtk.GLArea()
        self.drawing_area.add_css_class("card")
        self.drawing_area.set_hexpand(True)
        self.drawing_area.set_vexpand(True)
        self.drawing_area.set_margin_top(6)
        self.drawing_area.set_margin_bottom(6)
        self.drawing_area.set_margin_start(6)
        self.drawing_area.set_margin_end(6)

        self.drawing_area.connect("render", self.on_gl_render)
        self.drawing_area.connect("unrealize", self.on_glarea_unrealize)
        self.drawing_area.connect(
            "notify::scale-factor",
            self.on_da_scale_factor_changed,
        )

        viewer_box.append(self.drawing_area)

        gesture_click = Gtk.GestureClick.new()
        gesture_click.set_button(Gdk.BUTTON_PRIMARY)
        gesture_click.connect("pressed", self.on_da_press)
        gesture_click.connect("released", self.on_da_release)
        self.drawing_area.add_controller(gesture_click)

        gesture_drag = Gtk.GestureDrag.new()
        gesture_drag.connect("drag-update", self.on_da_drag)
        self.drawing_area.add_controller(gesture_drag)

        # Pointer motion for hover feedback (resize cursors)
        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self.on_da_motion)
        motion.connect("leave", self.on_da_leave)
        self.drawing_area.add_controller(motion)

    def _show_cancel_button(self, show: bool) -> None:
        """Show or hide the scan-cancel button."""
        self.sidebar_btn_cancel_scan.set_visible(show)
        self.sidebar_btn_cancel_scan.set_sensitive(show)

    # --- Inline settings/combos ---

    def on_scanner_row_changed(
        self, _row: Adw.ComboRow, _pspec: GObject.ParamSpec
    ) -> None:
        """Handle selection changes in the scanner device combo row."""
        if self._suppress_scanner_row_signal:
            return

        idx = self.scanner_row.get_selected()
        if idx < 0 or idx >= self.device_store.get_n_items():
            new_name = None
        else:
            if 0 <= idx < len(self.available_devices):
                new_name = self.available_devices[idx][0]
            else:
                new_name = None

        if new_name is None or new_name == self.selected_device_name:
            return

        self.selected_device_name = new_name
        self._apply_scanner()

    def on_dpi_changed(self, _row: Adw.ComboRow, _pspec: GObject.ParamSpec) -> None:
        """Handle resolution (DPI) changes from the combo row."""
        idx = self.dpi_combo.get_selected()
        if idx < 0 or idx >= self.dpi_store.get_n_items():
            return

        try:
            dpi_str = self.dpi_store.get_string(idx)
            assert dpi_str
            dpi = int(dpi_str)
        except (ValueError, TypeError):
            return

        if dpi <= 0 or dpi == self.settings.resolution:
            return

        self.settings.resolution = dpi

        if self.scanner_dev is not None:
            try:
                self.scanner_dev.resolution = dpi
            except Exception as e:
                self._warning_dialog("Scanner", f"Failed to set resolution: {e}")

    def on_mode_changed(self, _row: Adw.ComboRow, _pspec: GObject.ParamSpec) -> None:
        """Handle scan mode changes from the combo row."""
        idx = self.mode_combo.get_selected()
        if idx < 0 or idx >= self.mode_store.get_n_items():
            return

        mode = self.mode_store.get_string(idx)
        if not mode or self.scanner_dev is None:
            return

        try:
            self.scanner_dev.mode = mode
        except Exception as e:
            self._warning_dialog("Scanner", f"Failed to set mode: {e}")

    def _on_viewer_paned_orientation_changed(
        self,
        paned: Gtk.Paned,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Swap viewer/regions panels when the split orientation changes."""
        viewer_box = self._viewer_paned_viewer_box
        regions_box = self._viewer_paned_regions_box

        if paned.get_orientation() == Gtk.Orientation.HORIZONTAL:
            if (
                paned.get_start_child() is not regions_box
                or paned.get_end_child() is not viewer_box
            ):
                paned.set_start_child(None)
                paned.set_end_child(None)
                paned.set_start_child(regions_box)
                paned.set_end_child(viewer_box)
                regions_box.set_margin_top(0)
        else:
            if (
                paned.get_start_child() is not viewer_box
                or paned.get_end_child() is not regions_box
            ):
                paned.set_start_child(None)
                paned.set_end_child(None)
                paned.set_start_child(viewer_box)
                paned.set_end_child(regions_box)
                regions_box.set_margin_top(6)

        GLib.idle_add(self._apply_viewer_paned_ratio, paned)

    def _apply_viewer_paned_ratio(self, paned: Gtk.Paned) -> None:
        """Set the paned divider to roughly half of available space."""
        alloc = paned.get_allocation()
        total = (
            alloc.height
            if paned.get_orientation() == Gtk.Orientation.VERTICAL
            else alloc.width
        )
        if total <= 0:
            return
        paned.set_position(total // 2)

    def _sync_default_rot_combo_from_settings(self) -> None:
        """Synchronize the default rotation combo with current settings."""
        label = self._rotation_display_from_ccw(
            self.settings.defaults.default_rotation_ccw
        )
        try:
            idx = _ROTATION_LABELS.index(label)
        except ValueError:
            idx = 0
        self.default_rot_combo.set_selected(idx)

    def on_default_rotation_changed(
        self, _row: Adw.ComboRow, _pspec: GObject.ParamSpec
    ) -> None:
        """Handle changes to the default page rotation setting."""
        idx = self.default_rot_combo.get_selected()
        label = _ROTATION_LABELS[idx]
        self.settings.defaults.default_rotation_ccw = self._rotation_ccw_from_display(
            label
        )

    def on_default_crop_preset_changed(
        self, _row: Adw.ComboRow, _pspec: GObject.ParamSpec
    ) -> None:
        """Handle changes to the default crop preset setting."""
        idx = self.default_crop_combo.get_selected()
        label = _CROP_PRESET_LABELS[idx]
        self.settings.defaults.default_crop_preset = self._crop_preset_from_display(
            label
        )

    def on_folder_changed(self, _row: Adw.EntryRow, _pspec: GObject.ParamSpec) -> None:
        """Handle manual edits to the output folder entry."""
        self.settings.folder = Path(self.folder_entry.get_text().strip()).expanduser()

    def on_browse_folder(self, _button: Gtk.Button) -> None:
        """Open a folder chooser dialog for the export output folder."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select output folder")
        dialog.select_folder(self, None, self._on_folder_dialog_response)

    def _on_folder_dialog_response(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
    ) -> None:
        """Handle completion of the output folder selection dialog."""
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        if path:
            self.folder_entry.set_text(path)

    # --- Header titles ---

    def _update_header_titles(self) -> None:
        """Update window titles based on current group/page selection."""
        self.sidebar_title.set_title("Scanner Page Manager")
        self.sidebar_title.set_subtitle(_SIDEBAR_DUMMY_SUBTITLE)

        if not self.selected_page:
            self.content_title.set_title("No page selected")
            self.content_title.set_subtitle("Select a page from the sidebar.")
            return

        page = self.selected_page

        grp: PageGroup | None = self.selected_group
        page_index: int | None = None
        total_pages: int | None = None

        if grp is None or page not in grp.pages:
            grp = None
            for g in self.page_groups:
                if page in g.pages:
                    grp = g
                    break

        if grp is not None:
            total_pages = len(grp.pages)
            try:
                page_index = grp.pages.index(page) + 1
            except ValueError:
                page_index = None

        group_name = grp.name if grp is not None else "Unknown group"

        if page_index is not None and total_pages is not None:
            main_title = f"Page {page_index} of {total_pages} – {group_name}"
        else:
            main_title = f"Page – {group_name}"

        w, h = page.pil_image.size
        n_regions = len(page.regions)
        subtitle = (
            f"{w}×{h} px, "
            f"{self._plural(n_regions, 'region')}, "
            f"rotation {page.rotation}° CW"
        )

        self.content_title.set_title(main_title)
        self.content_title.set_subtitle(subtitle)

    # --- Scanner/device helpers ---

    def _refresh_scanner_list(self) -> None:
        """Refresh the list of SANE devices in the scanner combo."""
        self._set_scanning_buttons_state(False)
        self._set_progress(0, 0, "Detecting scanners…")
        threading.Thread(target=self._refresh_scanner_list_worker, daemon=True).start()

    def _refresh_scanner_list_worker(self) -> None:
        """Background worker to fetch SANE device list."""
        available_devices: list[tuple[str, str, str, str]] = []
        err: str | None = None

        try:
            available_devices = self._list_sane_devices()
        except Exception as e:
            err = f"Failed to list devices: {e}"

        def finish() -> None:
            self._set_scanning_buttons_state(True)
            self._reset_progress()

            if err is not None:
                self.available_devices = []
                self._string_list_set(self.device_store, [])
                self.selected_device_name = None
                self._error_dialog("Scanner error", err)
                return

            self.available_devices = available_devices
            self._update_device_store_and_selection()

        GLib.idle_add(finish)

    def _populate_dpi_combo_from_scanner(self, dev: "sane.SaneDev") -> None:
        """Populate the resolution combo using the scanner’s resolution option."""
        cur_res = self.settings.resolution
        candidates: list[int] = []

        try:
            opt = dev.opt["resolution"]  # type: ignore[index]
            constr = getattr(opt, "constraint", None)
            if constr:
                if isinstance(constr, (list, tuple)):
                    if len(constr) == 3 and all(
                        isinstance(x, (int, float)) for x in constr
                    ):
                        lo, hi, step = constr
                        if step and step > 0:
                            v = int(lo)
                            vals: list[int] = []
                            while v <= hi and len(vals) < 30:
                                vals.append(int(v))
                                v += int(step)
                            candidates = vals
                    else:
                        vals = [
                            int(v)
                            for v in constr
                            if isinstance(v, (int, float)) and v > 0
                        ]
                        candidates = sorted(set(vals))
        except Exception:
            pass

        if not candidates:
            candidates = [150, 300, 600, 1200]

        candidates = sorted(set(candidates))
        self._string_list_set(self.dpi_store, [str(v) for v in candidates])

        if cur_res not in candidates:
            try:
                dev_res = getattr(dev, "resolution", candidates[0])
                cur_res = int(dev_res)
            except Exception:
                cur_res = candidates[0]

            if cur_res not in candidates:
                cur_res = candidates[0]

        self.settings.resolution = cur_res

        try:
            idx = candidates.index(cur_res)
        except ValueError:
            idx = 0
        self.dpi_combo.set_selected(idx)

    def _populate_mode_combo_from_scanner(self, dev: "sane.SaneDev") -> None:
        """Populate the scan mode combo using the scanner’s mode option."""
        modes: list[str] = []
        try:
            opt = dev.opt["mode"]  # type: ignore[index]
            constr = getattr(opt, "constraint", None)
            if isinstance(constr, (list, tuple)):
                modes = [str(m) for m in constr if isinstance(m, str)]
        except Exception:
            pass

        if not modes:
            modes = ["Color", "Gray", "Lineart"]

        seen: set[str] = set()
        uniq_modes: list[str] = []
        for m in modes:
            if m not in seen:
                seen.add(m)
                uniq_modes.append(m)

        self._string_list_set(self.mode_store, uniq_modes)

        try:
            cur_mode = str(getattr(dev, "mode", uniq_modes[0]))
        except Exception:
            cur_mode = uniq_modes[0]

        try:
            idx = uniq_modes.index(cur_mode)
        except ValueError:
            idx = 0

        self.mode_combo.set_selected(idx)

    def _apply_scanner(self) -> None:
        """Open the selected scanner device and sync its options to the UI."""
        import sane

        if self.scanner_dev is not None:
            try:
                self.scanner_dev.close()
            except Exception:
                pass
            self.scanner_dev = None

        if not self.selected_device_name:
            self._warning_dialog("Scanner", "No scanner selected.")
            return

        try:
            dev = sane.open(self.selected_device_name)
        except Exception as e:
            self._error_dialog("Scanner error", f"Failed to open scanner: {e}")
            return

        self._populate_dpi_combo_from_scanner(dev)
        self._populate_mode_combo_from_scanner(dev)

        try:
            dev.resolution = self.settings.resolution
        except Exception:
            pass

        self.scanner_dev = dev

    # --- Message dialogs ---

    def _show_message_dialog(
        self, title: str, text: str, appearance: Adw.ResponseAppearance
    ) -> None:
        """Show a simple one-button alert dialog."""
        dialog = Adw.AlertDialog.new(title, text)
        dialog.add_response("ok", "_OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.set_response_appearance("ok", appearance)
        dialog.present(self)

    def _error_dialog(self, title: str, text: str) -> None:
        """Show an error dialog with destructive styling."""
        self._show_message_dialog(title, text, Adw.ResponseAppearance.DESTRUCTIVE)

    def _info_dialog(self, title: str, text: str) -> None:
        """Show an informational dialog."""
        self._show_message_dialog(title, text, Adw.ResponseAppearance.SUGGESTED)

    def _warning_dialog(self, title: str, text: str) -> None:
        """Show a warning dialog."""
        self._show_message_dialog(title, text, Adw.ResponseAppearance.SUGGESTED)

    def _confirm_dialog(
        self,
        title: str,
        text: str,
        *,
        confirm_label: str = "_Yes",
        cancel_label: str = "_No",
        confirm_appearance: Adw.ResponseAppearance = Adw.ResponseAppearance.DESTRUCTIVE,
        callback: Callable[[bool], None] | None = None,
    ) -> None:
        """Show a yes/no style confirmation dialog and invoke a callback."""
        dialog = Adw.AlertDialog.new(title, text)

        dialog.add_response("cancel", cancel_label)
        dialog.add_response("confirm", confirm_label)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("confirm", confirm_appearance)

        def on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if callback is not None:
                callback(response == "confirm")

        dialog.connect("response", on_response)
        dialog.present(self)

    # --- List refresh (groups/pages/regions) ---

    def _clear_listbox(self, lb: Gtk.ListBox | None) -> None:
        """Remove all children from a `ListBox`."""
        if lb is None:
            return
        child = lb.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            lb.remove(child)
            child = nxt

    def refresh_groups_list(self) -> None:
        """Rebuild the Groups/Pages list UI from the current model."""
        expanded_ids: set[str] = set()
        child = self.groups_list.get_first_child()
        while child is not None:
            if isinstance(child, Adw.ExpanderRow):
                gid = getattr(child, "_group_id", None)
                if gid and child.get_expanded():
                    expanded_ids.add(gid)
            child = child.get_next_sibling()

        if self.selected_group is not None:
            expanded_ids.add(self.selected_group.id)

        self._suppress_group_expand_signal = True
        try:
            self._clear_listbox(self.groups_list)

            if not self.page_groups:
                self._add_placeholder_row(
                    self.groups_list,
                    "No groups yet",
                    "Click + to scan a new group of pages.",
                )
                return

            for gi, grp in enumerate(self.page_groups):
                grp_row = Adw.ExpanderRow()
                grp_row.set_title(grp.name)
                grp_row.set_subtitle(self._plural(len(grp.pages), "page"))
                setattr(grp_row, "_group_index", gi)
                setattr(grp_row, "_group_id", grp.id)

                if grp.id in expanded_ids:
                    grp_row.set_expanded(True)

                grp_row.connect(
                    "notify::expanded",
                    lambda row, _pspec, idx=gi: self._on_group_row_expanded(row, idx),
                )

                delete_btn = self._icon_only_button(
                    "user-trash-symbolic",
                    "Delete group",
                )
                delete_btn.connect(
                    "clicked", lambda _b, idx=gi: self._delete_group_index(idx)
                )
                grp_row.add_suffix(delete_btn)

                scan_btn = self._icon_only_button(
                    "list-add-symbolic",
                    "Scan a page into this group",
                )
                scan_btn.connect(
                    "clicked", lambda _b, idx=gi: self._scan_into_group_index(idx)
                )
                grp_row.add_suffix(scan_btn)

                group_drop_target = Gtk.DropTarget.new(
                    GLib.Variant, Gdk.DragAction.MOVE
                )
                group_drop_target.connect(
                    "drop",
                    lambda _target, value, x, y, dest_g=gi: self._on_page_drop(
                        value, dest_g, None
                    ),
                )
                grp_row.add_controller(group_drop_target)

                for pi, page in enumerate(grp.pages):
                    row = Adw.ActionRow()
                    row.set_title(f"Page {pi + 1}")
                    row.set_subtitle(
                        f"{self._plural(len(page.regions), 'region')}, "
                        + f"rotation {page.rotation}° CW"
                    )
                    row.set_activatable(True)
                    setattr(row, "_group_index", gi)
                    setattr(row, "_page_index", pi)
                    row.connect(
                        "activated",
                        lambda _r, g=gi, p=pi: self.on_page_row_activated(g, p),
                    )

                    drag_handle = Gtk.Image.new_from_icon_name(
                        "list-drag-handle-symbolic"
                    )
                    drag_handle.add_css_class("drag-handle")
                    row.add_prefix(drag_handle)

                    drag_source = Gtk.DragSource.new()
                    drag_source.set_actions(Gdk.DragAction.MOVE)
                    drag_source.connect(
                        "prepare",
                        lambda _source, x, y, g=gi, p=pi: self._on_page_drag_prepare(
                            g, p
                        ),
                    )
                    drag_source.connect("drag-begin", self._on_page_drag_begin)
                    drag_handle.add_controller(drag_source)

                    drop_target = Gtk.DropTarget.new(GLib.Variant, Gdk.DragAction.MOVE)
                    drop_target.connect(
                        "drop",
                        lambda _target, value, x, y, dest_g=gi, dest_p=pi: (
                            self._on_page_drop(value, dest_g, dest_p)
                        ),
                    )
                    row.add_controller(drop_target)

                    btn_rot_ccw = self._icon_only_button(
                        "object-rotate-left-symbolic",
                        "Rotate page 90° counter-clockwise",
                    )
                    btn_rot_ccw.connect(
                        "clicked",
                        lambda _b, g=gi, p=pi: self._rotate_page_index(g, p, -90),
                    )
                    row.add_suffix(btn_rot_ccw)

                    btn_rot_cw = self._icon_only_button(
                        "object-rotate-right-symbolic",
                        "Rotate page 90° clockwise",
                    )
                    btn_rot_cw.connect(
                        "clicked",
                        lambda _b, g=gi, p=pi: self._rotate_page_index(g, p, 90),
                    )
                    row.add_suffix(btn_rot_cw)

                    btn_delete = self._icon_only_button(
                        "user-trash-symbolic",
                        "Delete page",
                    )
                    btn_delete.connect(
                        "clicked",
                        lambda _b, g=gi, p=pi: self._delete_page_index(g, p),
                    )
                    row.add_suffix(btn_delete)

                    grp_row.add_row(row)

                self.groups_list.append(grp_row)
        finally:
            self._suppress_group_expand_signal = False

    def refresh_regions_list(self) -> None:
        """Rebuild the Regions list for the selected page and sync viewer highlights."""
        self._clear_listbox(self.regions_list)
        self._region_preview_buttons.clear()

        self._update_header_titles()

        if not self.selected_page:
            self._preview_region_id = None
            self._add_placeholder_row(
                self.regions_list,
                "No page selected",
                "Select a page to manage its regions.",
            )
            self.drawing_area.queue_render()
            return

        page = self.selected_page

        if not page.regions:
            self._preview_region_id = None
            self._add_placeholder_row(
                self.regions_list,
                "No regions yet",
                "Drag on the Page Viewer to create a new region.",
            )
            self.drawing_area.queue_render()
            return

        for i, reg in enumerate(page.regions):
            w, h = reg.x2 - reg.x1, reg.y2 - reg.y1
            rot = reg.rotation % 360

            subtitle = f"{w}×{h} px"
            if rot:
                subtitle += f", {rot}° CW"
            row = Adw.ExpanderRow(
                title=reg.name,
                subtitle=subtitle,
                activatable=False,
                selectable=False,
            )
            setattr(row, "_index", i)
            setattr(row, "_region_id", reg.id)

            row.set_expanded(reg.id in self.expanded_region_ids)

            def on_expanded_changed(
                exp_row: Adw.ExpanderRow, _pspec: GObject.ParamSpec, rid: str = reg.id
            ) -> None:
                if exp_row.get_expanded():
                    self.expanded_region_ids.add(rid)
                else:
                    self.expanded_region_ids.discard(rid)
                self.drawing_area.queue_render()

            row.connect("notify::expanded", on_expanded_changed)

            suffixes: list[Gtk.Widget] = []

            preview_btn = self._icon_only_button(
                "view-reveal-symbolic",
                "Preview region",
            )
            self._region_preview_buttons[reg.id] = preview_btn
            preview_btn.connect("clicked", lambda _b, idx=i: self.preview_region(idx))
            suffixes.append(preview_btn)

            btn_rot_ccw = self._icon_only_button(
                "object-rotate-left-symbolic",
                "Rotate region 90° counter-clockwise",
            )
            btn_rot_ccw.connect(
                "clicked",
                lambda _b, idx=i: self.rotate_region(idx, -90),
            )
            suffixes.append(btn_rot_ccw)

            btn_rot_cw = self._icon_only_button(
                "object-rotate-right-symbolic",
                "Rotate region 90° clockwise",
            )
            btn_rot_cw.connect(
                "clicked",
                lambda _b, idx=i: self.rotate_region(idx, 90),
            )
            suffixes.append(btn_rot_cw)

            rename_btn = self._icon_only_button(
                "document-edit-symbolic",
                "Rename region",
            )
            rename_btn.connect("clicked", lambda _b, idx=i: self.rename_region(idx))
            suffixes.append(rename_btn)

            delete_btn = self._icon_only_button(
                "user-trash-symbolic",
                "Delete region",
            )
            delete_btn.connect("clicked", lambda _b, idx=i: self.delete_region(idx))
            suffixes.append(delete_btn)

            for suffix in reversed(suffixes):
                row.add_suffix(suffix)

            left_row = Adw.EntryRow(title="Left (x1)")
            left_row.set_text(str(reg.x1))
            row.add_row(left_row)

            top_row = Adw.EntryRow(title="Top (y1)")
            top_row.set_text(str(reg.y1))
            row.add_row(top_row)

            right_row = Adw.EntryRow(title="Right (x2)")
            right_row.set_text(str(reg.x2))
            row.add_row(right_row)

            bottom_row = Adw.EntryRow(title="Bottom (y2)")
            bottom_row.set_text(str(reg.y2))
            row.add_row(bottom_row)

            def apply_coords(
                _btn: Gtk.Button | None = None,
                idx: int = i,
                exp_row: Adw.ExpanderRow = row,
                left: Adw.EntryRow = left_row,
                top: Adw.EntryRow = top_row,
                right: Adw.EntryRow = right_row,
                bottom: Adw.EntryRow = bottom_row,
            ) -> None:
                """Apply edited coordinates from the entries to the region."""
                if not self.selected_page or not (
                    0 <= idx < len(self.selected_page.regions)
                ):
                    return

                try:
                    x1 = int(left.get_text())
                    y1 = int(top.get_text())
                    x2 = int(right.get_text())
                    y2 = int(bottom.get_text())
                except ValueError:
                    self._warning_dialog("Region", "Coordinates must be integers.")
                    return

                img_w, img_h = self.selected_page.pil_image.size
                res = self._normalize_and_clamp_region(x1, y1, x2, y2, img_w, img_h)
                if res is None:
                    self._warning_dialog("Region", "Invalid region rectangle.")
                    return

                reg2 = self.selected_page.regions[idx]
                reg2.x1, reg2.y1, reg2.x2, reg2.y2 = res

                w2, h2 = reg2.x2 - reg2.x1, reg2.y2 - reg2.y1

                self._suppress_region_coord_update = True
                try:
                    left.set_text(str(reg2.x1))
                    top.set_text(str(reg2.y1))
                    right.set_text(str(reg2.x2))
                    bottom.set_text(str(reg2.y2))
                finally:
                    self._suppress_region_coord_update = False

                rot2 = reg2.rotation % 360
                subtitle2 = f"{w2}×{h2} px"
                if rot2:
                    subtitle2 += f", {rot2}° CW"
                exp_row.set_subtitle(subtitle2)
                self.drawing_area.queue_render()

            preset_model = Gtk.StringList.new(list(_REGION_CROP_PRESET_LABELS))
            preset_row = Adw.ComboRow(title="Preset", model=preset_model)
            preset_row.set_selected(0)
            row.add_row(preset_row)

            def on_preset_selected(
                _combo: Adw.ComboRow,
                _pspec: GObject.ParamSpec,
                idx: int = i,
                left: Adw.EntryRow = left_row,
                top: Adw.EntryRow = top_row,
                right: Adw.EntryRow = right_row,
                bottom: Adw.EntryRow = bottom_row,
            ) -> None:
                """Handle preset selection for a region and update coordinates."""
                if not self.selected_page or not (
                    0 <= idx < len(self.selected_page.regions)
                ):
                    return

                sel = _combo.get_selected()
                if sel <= 0:
                    return

                img_w, img_h = self.selected_page.pil_image.size
                label = _CROP_PRESET_LABELS[sel - 1]
                preset = self._crop_preset_from_display(label)
                x1, y1, x2, y2 = self._calc_preset_region(img_w, img_h, preset)

                self._suppress_region_coord_update = True
                try:
                    left.set_text(str(x1))
                    top.set_text(str(y1))
                    right.set_text(str(x2))
                    bottom.set_text(str(y2))
                finally:
                    self._suppress_region_coord_update = False

                apply_coords()

            preset_row.connect("notify::selected", on_preset_selected)

            actions_row = Adw.ActionRow(title="Actions")
            actions_row.set_activatable(False)

            round_btn = self._icon_only_button(
                "format-indent-more-symbolic",
                "Round coordinates to multiple of 10",
            )
            actions_row.add_suffix(round_btn)

            row.add_row(actions_row)

            apply_row = Adw.ButtonRow(title="Apply coordinates", activatable=True)
            apply_row.connect("activated", lambda _r, f=apply_coords: f())
            row.add_row(apply_row)

            def round_coords(
                _btn: Gtk.Button,
                idx: int = i,
                left: Adw.EntryRow = left_row,
                top: Adw.EntryRow = top_row,
                right: Adw.EntryRow = right_row,
                bottom: Adw.EntryRow = bottom_row,
            ) -> None:
                """Round the region coordinates to multiples of 10 and apply."""
                if not self.selected_page or not (
                    0 <= idx < len(self.selected_page.regions)
                ):
                    return
                reg2 = self.selected_page.regions[idx]
                img_w, img_h = self.selected_page.pil_image.size

                def rd(v: int) -> int:
                    return (v // 10) * 10

                def ru(v: int) -> int:
                    return ((v + 9) // 10) * 10

                x1 = rd(reg2.x1)
                y1 = rd(reg2.y1)
                x2 = ru(reg2.x2)
                y2 = ru(reg2.y2)

                res = self._normalize_and_clamp_region(x1, y1, x2, y2, img_w, img_h)
                if res is None:
                    self._error_dialog(
                        "Region", "Rounded coordinates produced an invalid region."
                    )
                    return
                x1, y1, x2, y2 = res

                self._suppress_region_coord_update = True
                try:
                    left.set_text(str(x1))
                    top.set_text(str(y1))
                    right.set_text(str(x2))
                    bottom.set_text(str(y2))
                finally:
                    self._suppress_region_coord_update = False

                apply_coords()

            round_btn.connect("clicked", round_coords)

            self.regions_list.append(row)

        self._refresh_preview_eye_highlight()
        self.drawing_area.queue_render()

    # --- Copy/paste regions ---

    def copy_regions_from_page(self) -> None:
        """Copy all regions from the currently selected page into a buffer."""
        if not self.selected_page:
            self._info_dialog("Copy regions", "No page selected.")
            return
        self.copied_regions = [
            self._new_region(
                name=reg.name,
                x1=reg.x1,
                y1=reg.y1,
                x2=reg.x2,
                y2=reg.y2,
                rotation=reg.rotation,
            )
            for reg in self.selected_page.regions
        ]
        self._info_dialog(
            "Copy regions",
            f"Copied {self._plural(len(self.copied_regions), 'region')} "
            + "from current page.",
        )

    def paste_regions_to_page(self) -> None:
        """Paste previously copied regions into the currently selected page."""
        if not self.selected_page:
            self._info_dialog("Paste regions", "No page selected.")
            return
        if not self.copied_regions:
            self._info_dialog("Paste regions", "No copied regions to paste.")
            return
        for reg in self.copied_regions:
            new_reg = self._new_region(
                name=reg.name,
                x1=reg.x1,
                y1=reg.y1,
                x2=reg.x2,
                y2=reg.y2,
                rotation=reg.rotation,
            )
            self.selected_page.regions.append(new_reg)
        self.refresh_regions_list()
        self.refresh_groups_list()

    # --- Region management ---

    def rename_region(self, idx: int) -> None:
        """Prompt the user to rename a region."""
        if not self.selected_page:
            return
        if not (0 <= idx < len(self.selected_page.regions)):
            return

        reg = self.selected_page.regions[idx]

        def done(new_name: str | None) -> None:
            if new_name:
                reg.name = new_name
                self.refresh_regions_list()

        simple_prompt_async(self, "Rename region", "Region name:", reg.name, done)

    def delete_region(self, idx: int) -> None:
        """Delete a region after confirmation."""
        if not self.selected_page:
            return
        if not (0 <= idx < len(self.selected_page.regions)):
            return

        reg = self.selected_page.regions[idx]

        def on_confirm(ok: bool) -> None:
            if not ok:
                return
            rid = reg.id
            assert self.selected_page
            del self.selected_page.regions[idx]
            self.expanded_region_ids.discard(rid)
            if self._preview_region_id == rid:
                self._preview_region_id = None
            self.refresh_regions_list()
            self.refresh_groups_list()

        self._confirm_dialog(
            "Delete region",
            "Delete this region?",
            callback=on_confirm,
        )

    def rotate_region(self, idx: int, degrees_cw: int) -> None:
        """Rotate a region independently of the page, in 90° steps."""
        if not self.selected_page:
            return
        if not (0 <= idx < len(self.selected_page.regions)):
            return

        reg = self.selected_page.regions[idx]
        reg.rotation = (reg.rotation + degrees_cw) % 360
        self.refresh_regions_list()
        self.drawing_area.queue_render()

    def _refresh_preview_eye_highlight(self) -> None:
        """Highlight the eye button for the currently previewed region."""
        for rid, btn in self._region_preview_buttons.items():
            if self._preview_region_id == rid:
                btn.remove_css_class("flat")
            else:
                btn.add_css_class("flat")

    def preview_region(self, idx: int) -> None:
        """Toggle preview of a region in the main Page Viewer."""
        if not self.selected_page:
            return
        if not (0 <= idx < len(self.selected_page.regions)):
            return

        page = self.selected_page
        reg = page.regions[idx]

        if self._preview_region_id == reg.id:
            self._preview_region_id = None
        else:
            self._preview_region_id = reg.id

        self._refresh_preview_eye_highlight()
        self.drawing_area.queue_render()

    # --- Group/page selection ---

    def _on_group_row_expanded(self, row: Adw.ExpanderRow, group_idx: int) -> None:
        """When a group row expands, select its group and ensure a page is selected."""
        if self._suppress_group_expand_signal:
            return
        if not row.get_expanded():
            return
        if not (0 <= group_idx < len(self.page_groups)):
            return
        grp = self.page_groups[group_idx]
        self.selected_group = grp
        if self.selected_page not in grp.pages:
            self.selected_page = grp.pages[0] if grp.pages else None
        self.refresh_regions_list()

    def on_page_row_activated(self, group_idx: int, page_idx: int) -> None:
        """Handle activation of a page row in the groups list."""
        if not (0 <= group_idx < len(self.page_groups)):
            return
        grp = self.page_groups[group_idx]
        if not (0 <= page_idx < len(grp.pages)):
            return
        self.selected_group = grp
        self.selected_page = grp.pages[page_idx]
        self.nav_split.set_show_content(True)
        self.refresh_regions_list()

    # --- Per-index helpers ---

    def _scan_into_group_index(self, idx: int) -> None:
        """Start a scan and append the new page to the given group index."""
        if not (0 <= idx < len(self.page_groups)):
            return
        self.selected_group = self.page_groups[idx]
        self._start_scan("into_group")

    def _delete_group_index(self, idx: int) -> None:
        """Delete a group identified by its index."""
        if not (0 <= idx < len(self.page_groups)):
            return
        self.selected_group = self.page_groups[idx]
        self.delete_group()

    def _rotate_page_index(
        self, group_idx: int, page_idx: int, degrees_cw: int
    ) -> None:
        """Rotate the page identified by group/page indices."""
        if not (0 <= group_idx < len(self.page_groups)):
            return
        grp = self.page_groups[group_idx]
        if not (0 <= page_idx < len(grp.pages)):
            return
        self.selected_group = grp
        self.selected_page = grp.pages[page_idx]
        self.rotate_page(degrees_cw)

    def _delete_page_index(self, group_idx: int, page_idx: int) -> None:
        """Delete the page identified by group/page indices."""
        if not (0 <= group_idx < len(self.page_groups)):
            return
        grp = self.page_groups[group_idx]
        if not (0 <= page_idx < len(grp.pages)):
            return
        self.selected_group = grp
        self.selected_page = grp.pages[page_idx]
        self.delete_page()

    # --- Group management ---

    def rename_group(self) -> None:
        """Prompt the user to rename the currently selected group."""
        if not self.selected_group:
            return

        def done(new_name: str | None) -> None:
            if new_name:
                assert self.selected_group
                self.selected_group.name = new_name
                self.refresh_groups_list()

        simple_prompt_async(
            self,
            "Rename group",
            "Group name:",
            self.selected_group.name,
            done,
        )

    def delete_group(self) -> None:
        """Delete the currently selected group and all its pages."""
        if not self.selected_group:
            return

        grp = self.selected_group

        def on_confirm(ok: bool) -> None:
            if not ok:
                return
            for page in grp.pages:
                for r in page.regions:
                    self.expanded_region_ids.discard(r.id)

            self.page_groups = [g for g in self.page_groups if g is not grp]
            self.selected_group = None
            self.selected_page = None
            self._preview_region_id = None
            self.refresh_groups_list()
            self.refresh_regions_list()

        self._confirm_dialog(
            "Delete group",
            "Delete this group and all its pages?",
            callback=on_confirm,
        )

    def clear_groups(self) -> None:
        """Remove all groups, pages, and regions from the session."""
        if not self.page_groups:
            return

        def on_confirm(ok: bool) -> None:
            if not ok:
                return
            self.page_groups = []
            self.selected_group = None
            self.selected_page = None
            self.expanded_region_ids.clear()
            self._preview_region_id = None
            self.refresh_groups_list()
            self.refresh_regions_list()

        self._confirm_dialog(
            "Clear all groups",
            "This will remove all groups, pages, and regions from this session. Continue?",
            callback=on_confirm,
        )

    # --- Page management ---

    def delete_page(self) -> None:
        """Delete the currently selected page from its group."""
        if not self.selected_group or not self.selected_page:
            return
        grp = self.selected_group
        page = self.selected_page
        if page not in grp.pages:
            return

        def on_confirm(ok: bool) -> None:
            if not ok:
                return
            for r in page.regions:
                self.expanded_region_ids.discard(r.id)

            idx = grp.pages.index(page)
            grp.pages.remove(page)
            if grp.pages:
                new_idx = min(idx, len(grp.pages) - 1)
                self.selected_page = grp.pages[new_idx]
            else:
                self.selected_page = None
                self._preview_region_id = None
            self.refresh_groups_list()
            self.refresh_regions_list()

        self._confirm_dialog(
            "Delete page",
            "Delete the selected page from this group?",
            callback=on_confirm,
        )

    def _on_page_drag_prepare(
        self, group_idx: int, page_idx: int
    ) -> Gdk.ContentProvider:
        """Prepare drag data describing the source group/page indices."""
        data = f"{group_idx}:{page_idx}"
        variant = GLib.Variant("s", data)
        return Gdk.ContentProvider.new_for_value(variant)

    def _on_page_drag_begin(self, _source: Gtk.DragSource, drag: Gdk.Drag) -> None:
        """Set a simple icon for page drag operations."""
        icon = Gtk.DragIcon.get_for_drag(drag)
        image = Gtk.Image.new_from_icon_name("x-office-document")
        image.set_pixel_size(24)
        icon.set_child(image)

    def _on_page_drop(
        self,
        value: GLib.Variant | None,
        dest_group_idx: int,
        dest_page_idx: int | None,
    ) -> bool:
        """Handle dropping a dragged page onto another page/group."""
        if value is None:
            return False
        try:
            s = value.get_string()
        except Exception:
            return False

        try:
            src_group_str, src_page_str = s.split(":", 1)
            src_group_idx = int(src_group_str)
            src_page_idx = int(src_page_str)
        except Exception:
            return False

        self._move_page(src_group_idx, src_page_idx, dest_group_idx, dest_page_idx)
        return True

    def _move_page(
        self,
        src_group_idx: int,
        src_page_idx: int,
        dest_group_idx: int,
        dest_page_idx: int | None,
    ) -> None:
        """Move a page from one group/index to another."""
        if not (0 <= src_group_idx < len(self.page_groups)):
            return
        if not (0 <= dest_group_idx < len(self.page_groups)):
            return

        src_grp = self.page_groups[src_group_idx]
        dest_grp = self.page_groups[dest_group_idx]

        if not (0 <= src_page_idx < len(src_grp.pages)):
            return

        page = src_grp.pages[src_page_idx]
        del src_grp.pages[src_page_idx]

        if dest_page_idx is None or dest_page_idx > len(dest_grp.pages):
            dest_idx = len(dest_grp.pages)
        else:
            dest_idx = max(0, dest_page_idx)

        if src_grp is dest_grp and src_page_idx < dest_idx:
            dest_idx -= 1

        dest_grp.pages.insert(dest_idx, page)

        self.selected_group = dest_grp
        self.selected_page = page

        self.refresh_groups_list()
        self.refresh_regions_list()

    # --- Progress ---

    def _set_progress(
        self, current: int | None, total: int | None, text: str | None
    ) -> None:
        """Update the sidebar progress bar and subtitle from a background task."""

        def update() -> None:
            if current is None or total in (None, 0):
                frac = 0.0
            else:
                frac = max(0.0, min(1.0, float(current) / float(total)))

            self.sidebar_progress_bar.set_fraction(frac)

            if text is not None:
                self.sidebar_title.set_subtitle(text)

            self.sidebar_progress_bar.set_visible(True)

        GLib.idle_add(update)

    def _reset_progress(self) -> None:
        """Reset the sidebar progress bar and subtitle."""
        self._set_progress(0, 1, _SIDEBAR_DUMMY_SUBTITLE)

    def _set_scanning_buttons_state(self, enabled: bool) -> None:
        """Enable or disable UI elements while scanning/exporting."""
        self._set_many_sensitive(
            enabled,
            self.btn_new_group,
            self.btn_clear_groups,
            self.btn_refresh_scanners,
            self.btn_browse_folder,
            self.btn_export,
            self.scanner_row,
            self.default_rot_combo,
            self.default_crop_combo,
            self.dpi_combo,
            self.mode_combo,
            self.folder_entry,
            self.groups_list,
            self.regions_list,
            self.drawing_area,
        )

    # --- Scanning (async) ---

    def _start_scan(self, mode: Literal["new_group", "into_group"]) -> None:
        """Start a background scan, either into a new group or the selected group."""
        if not self.scanner_dev:
            self._error_dialog("Scanner error", "Scanner not initialized.")
            return

        if mode == "into_group" and not self.selected_group:
            self._info_dialog("Scan", "No group selected. Please select a group first.")
            return

        with self._scan_lock:
            if self._scan_thread and self._scan_thread.is_alive():
                self._info_dialog("Scan", "A scan is already in progress.")
                return
            self._scan_cancel_event.clear()
            self._set_scanning_buttons_state(False)
            self._set_progress(0, 1, "Scanning…")
            self._show_cancel_button(True)
            self._scan_thread = threading.Thread(
                target=self._scan_worker, args=(mode,), daemon=True
            )
            self._scan_thread.start()

    def on_cancel_scan_clicked(self, _button: Gtk.Button) -> None:
        """Signal the current scan to cancel, if possible."""
        self._scan_cancel_event.set()
        self.sidebar_btn_cancel_scan.set_sensitive(False)
        self._set_progress(None, None, "Cancelling scan…")

    def _scan_worker(self, mode: Literal["new_group", "into_group"]) -> None:
        """Background scan worker that produces one Page and updates the model."""
        dev = self.scanner_dev
        if not dev:
            GLib.idle_add(self._set_scanning_buttons_state, True)
            GLib.idle_add(self._show_cancel_button, False)
            return

        pages: list[Page] = []
        try:
            img = self._scan_one_image(dev)
            if img is not None:
                pages.append(self._page_from_pil(img))
        except Exception as e:
            GLib.idle_add(self._error_dialog, "Scan error", str(e))

        def finish() -> None:
            self._scan_cancel_event.clear()
            self._reset_progress()
            self._set_scanning_buttons_state(True)
            self._show_cancel_button(False)
            if not pages:
                return

            if mode == "new_group":
                grp_name = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                grp = PageGroup(id=str(uuid.uuid4()), name=grp_name, pages=pages)
                self.page_groups.append(grp)
                self.selected_group = grp
                self.selected_page = grp.pages[0] if grp.pages else None
            elif mode == "into_group":
                if not self.selected_group:
                    return
                page = pages[0]
                self.selected_group.pages.append(page)
                self.selected_page = page

            self.refresh_groups_list()
            self.refresh_regions_list()

        GLib.idle_add(finish)

    def _scan_one_image(self, dev: "sane.SaneDev") -> PILImage.Image | None:
        """Scan a single image from the device and return it as a PIL image."""

        def progress(current: int, total: int) -> None:
            if self._scan_cancel_event.is_set():
                try:
                    dev.cancel()
                except Exception:
                    pass
                return
            self._set_progress(current, total, "Scanning…")

        try:
            np_arr = dev.arr_scan(progress=progress)
        except Exception as e:
            if self._scan_cancel_event.is_set():
                print(f"Scan cancelled: {e!r}")
                return None
            print(f"Scan failed: {e!r}")
            return None

        self._set_progress(1, 1, "Processing…")

        try:
            pil_img = PILImage.fromarray(np_arr)
        except Exception as e:
            print(f"Failed to convert scanned data to image: {e!r}")
            return None

        ccw = self.settings.defaults.default_rotation_ccw % 360
        if ccw != 0:
            pil_img = pil_img.rotate(ccw, expand=True)

        self._set_progress(1, 1, "Done")
        return pil_img

    # --- Page/region creation and rotation ---

    def _page_from_pil(self, pil_img: PILImage.Image) -> Page:
        """Create a Page object with a default region from a scanned PIL image."""
        w, h = pil_img.size
        preset = self.settings.defaults.default_crop_preset
        x1, y1, x2, y2 = self._calc_preset_region(w, h, preset)
        name = (
            _CROP_PRESET_LABELS[1]
            if preset == "preset_1200_1700"
            else _CROP_PRESET_LABELS[0]
        )

        region = self._new_region(name=name, x1=x1, y1=y1, x2=x2, y2=y2)
        rotation_cw = (-self.settings.defaults.default_rotation_ccw) % 360
        return Page(
            id=str(uuid.uuid4()),
            pil_image=pil_img,
            rotation=rotation_cw,
            regions=[region],
        )

    def rotate_page(self, degrees_cw: int) -> None:
        """Rotate the selected page and all its regions by the given angle."""
        if not self.selected_page:
            return
        page = self.selected_page
        orig_w, orig_h = page.pil_image.size
        delta_ccw = (-degrees_cw) % 360
        page.pil_image = page.pil_image.rotate(delta_ccw, expand=True)
        page.rotation = (page.rotation + degrees_cw) % 360
        self._rotate_regions_cw(page, degrees_cw, orig_w, orig_h)
        self.refresh_groups_list()
        self.refresh_regions_list()

    def _rotate_regions_cw(
        self,
        page: Page,
        degrees_cw: int,
        orig_w: int,
        orig_h: int,
    ) -> None:
        """Update all region coordinates after a page rotation."""
        w, h = orig_w, orig_h
        d = degrees_cw % 360
        if d == 0:
            return
        for r in page.regions:
            x1, y1, x2, y2 = r.x1, r.y1, r.x2, r.y2
            if d == 90:
                nx1, ny1 = h - y2, x1
                nx2, ny2 = h - y1, x2
            elif d == 180:
                nx1, ny1 = w - x2, h - y2
                nx2, ny2 = w - x1, h - y1
            elif d == 270:
                nx1, ny1 = y1, w - x2
                nx2, ny2 = y2, w - x1
            else:
                continue
            r.x1, r.y1, r.x2, r.y2 = nx1, ny1, nx2, ny2

    # --- DrawingArea (image + regions) ---

    def _get_display_pil_image(self) -> tuple[PILImage.Image, Region | None]:
        """
        Return the PIL image currently shown in the viewer, optionally cropped
        and rotated for a previewed region.
        """
        assert self.selected_page is not None

        page = self.selected_page
        pil_img = page.pil_image
        reg: Region | None = None

        if self._preview_region_id is not None:
            reg = next(
                (r for r in page.regions if r.id == self._preview_region_id),
                None,
            )
            if reg is None:
                self._preview_region_id = None
            else:
                try:
                    crop = pil_img.crop((reg.x1, reg.y1, reg.x2, reg.y2))
                except Exception:
                    self._preview_region_id = None
                    reg = None
                else:
                    rot = reg.rotation % 360
                    if rot:
                        crop = crop.rotate(-rot, expand=True)
                    pil_img = crop

        return pil_img, reg

    def _update_display_geometry(
        self,
        canvas_w: int,
        canvas_h: int,
        img_w: int,
        img_h: int,
        preview_active: bool,
    ) -> None:
        """Compute scale and centering offsets for the current image."""
        cw = max(1, canvas_w)
        ch = max(1, canvas_h)

        if preview_active:
            cwp, chp = cw, ch
        else:
            cwp = max(1, cw - _REGION_BORDER_WIDTH)
            chp = max(1, ch - _REGION_BORDER_WIDTH)

        scale = min(cwp / img_w, chp / img_h)
        if scale <= 0:
            scale = 1.0

        disp_w = img_w * scale
        disp_h = img_h * scale

        self._display_scale = scale
        self._display_offset_x = (cw - disp_w) / 2.0
        self._display_offset_y = (ch - disp_h) / 2.0

    def _get_glsl_sources(self) -> tuple[str, str]:
        """
        Return GLSL source strings for the textured quad + SDF border shader.

        Chooses between:
        - ES 2.0 / GL 2.x style (#version 100 / 120, texture2D, gl_FragColor)
        - ES 3.0+ / GL 3.0+ style (#version 300 es / 130, texelFetch, out vec4)
        based on self._gl_is_gles and self._gl_use_texel_fetch.
        """
        is_gles = self._gl_is_gles
        use_tf = self._gl_use_texel_fetch

        # --- Select dialect-specific pieces -------------------------------------
        if is_gles:
            precision = "precision mediump float;\n"
            if use_tf:
                # GLES 3.0+ (GLSL ES 3.00): texelFetch path
                v_ver = f_ver = "#version 300 es\n"
                v_in_pos = "layout(location = 0) in vec2 aPos;"
                v_in_uv = "layout(location = 1) in vec2 aTexCoord;"
                v_out_uv = "out vec2 vTexCoord;"
                f_in_uv = "in vec2 vTexCoord;"
                f_out_decl = "out vec4 fragColor;"
                f_out_var = "fragColor"
                ext_line = ""  # derivatives are core
            else:
                # GLES 2.0 (GLSL ES 1.00): no texelFetch
                v_ver = f_ver = "#version 100\n"
                v_in_pos = "attribute vec2 aPos;"
                v_in_uv = "attribute vec2 aTexCoord;"
                v_out_uv = "varying vec2 vTexCoord;"
                f_in_uv = "varying vec2 vTexCoord;"
                f_out_decl = ""
                f_out_var = "gl_FragColor"
                ext_line = "#extension GL_OES_standard_derivatives : enable\n"
        else:
            # Desktop GL
            precision = ""
            ext_line = ""
            if use_tf:
                # GL 3.0+ (GLSL 1.30+): texelFetch path
                v_ver = f_ver = "#version 130\n"
                v_in_pos = "in vec2 aPos;"
                v_in_uv = "in vec2 aTexCoord;"
                v_out_uv = "out vec2 vTexCoord;"
                f_in_uv = "in vec2 vTexCoord;"
                f_out_decl = "out vec4 fragColor;"
                f_out_var = "fragColor"
            else:
                # GL 2.1 (GLSL 1.20): no texelFetch
                v_ver = f_ver = "#version 120\n"
                v_in_pos = "attribute vec2 aPos;"
                v_in_uv = "attribute vec2 aTexCoord;"
                v_out_uv = "varying vec2 vTexCoord;"
                f_in_uv = "varying vec2 vTexCoord;"
                f_out_decl = ""
                f_out_var = "gl_FragColor"

        # Texture sampling expression
        if use_tf:
            tex_expr = (
                "texelFetch(uTexture, "
                "ivec2(vTexCoord * vec2(textureSize(uTexture, 0))), 0)"
            )
        else:
            tex_expr = "texture2D(uTexture, vTexCoord)"

        # Normalize strings that might be empty
        f_out_decl_nl = (f_out_decl + "\n") if f_out_decl else ""

        # --- Vertex shader ------------------------------------------------------
        vert_src = (
            f"{v_ver}"
            f"{precision}"
            f"{v_in_pos}\n"
            f"{v_in_uv}\n"
            f"{v_out_uv}\n"
            "void main() {\n"
            "    gl_Position = vec4(aPos, 0.0, 1.0);\n"
            "    vTexCoord = aTexCoord;\n"
            "}\n"
        )

        # --- Fragment shader (shared SDF logic) --------------------------------
        frag_src = (
            f"{f_ver}"
            f"{ext_line}"
            f"{precision}"
            f"{f_in_uv}\n"
            "uniform sampler2D uTexture;\n"
            "uniform bool uUseTexture;\n"
            "uniform vec4 uColor;\n"
            "\n"
            "// SDF uniforms\n"
            "uniform vec2 uRectMin;\n"
            "uniform vec2 uRectMax;\n"
            "uniform float uBorder;\n"
            "uniform vec2 uViewportSize;\n"
            f"{f_out_decl_nl}"
            "void main() {\n"
            "    if (uUseTexture) {\n"
            f"        {f_out_var} = {tex_expr};\n"
            "    } else {\n"
            "        // Fragment position in canvas coordinates, origin at top-left\n"
            "        vec2 fragPx = vec2(gl_FragCoord.x,\n"
            "                           uViewportSize.y - gl_FragCoord.y);\n"
            "\n"
            '        // Rect center and half-size in px (this is the "path" rect)\n'
            "        vec2 rectCenter = 0.5 * (uRectMin + uRectMax);\n"
            "        vec2 halfSize   = 0.5 * (uRectMax - uRectMin);\n"
            "\n"
            "        // Clamp radius to not exceed half the minimum dimension\n"
            "        float r = min(uBorder, min(halfSize.x, halfSize.y));\n"
            "\n"
            "        // Signed distance to rounded rectangle from\n"
            "        // https://iquilezles.org/articles/distfunctions2d/\n"
            "        vec2 p = fragPx - rectCenter;\n"
            "        vec2 q = abs(p) - halfSize + r;\n"
            "        float dist = min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;\n"
            "\n"
            "        // Stroke width uBorder, centered on the distance=0 contour:\n"
            "        float halfB = 0.5 * uBorder;\n"
            "\n"
            "        // Anti-aliasing width in pixels\n"
            "        float aa = max(fwidth(dist), 1.0);\n"
            "\n"
            "        // We want the band |dist| <= halfB to be opaque.\n"
            "        // dist_abs <= halfB - aa  => alpha ~ 1\n"
            "        // dist_abs >= halfB + aa  => alpha ~ 0\n"
            "        float dist_abs = abs(dist);\n"
            "        float a = halfB - aa;\n"
            "        float b = halfB + aa;\n"
            "        float alpha = 1.0 - smoothstep(a, b, dist_abs);\n"
            "\n"
            "        if (alpha <= 0.0) {\n"
            "            discard;\n"
            "        }\n"
            "\n"
            f"        {f_out_var} = uColor * alpha;\n"
            "    }\n"
            "}\n"
        )

        return vert_src, frag_src

    def _init_gl_resources(self, context: Gdk.GLContext) -> None:
        """Compile shaders and set up GL buffers the first time we get a context."""
        if self._gl_program != 0:
            return

        self._detect_gl_version()

        vert_src, frag_src = self._get_glsl_sources()

        def compile_shader(src: str, shader_type: int) -> int:
            sid = GL.glCreateShader(shader_type)
            GL.glShaderSource(sid, src)
            GL.glCompileShader(sid)
            status = GL.glGetShaderiv(sid, GL.GL_COMPILE_STATUS)
            if not status:
                log = GL.glGetShaderInfoLog(sid).decode("utf-8", "ignore")
                raise RuntimeError(f"Shader compile failed: {log}")
            return sid

        vs = compile_shader(vert_src, GL.GL_VERTEX_SHADER)
        fs = compile_shader(frag_src, GL.GL_FRAGMENT_SHADER)

        prog = GL.glCreateProgram()
        GL.glAttachShader(prog, vs)
        GL.glAttachShader(prog, fs)
        GL.glLinkProgram(prog)

        link_status = GL.glGetProgramiv(prog, GL.GL_LINK_STATUS)
        if not link_status:
            log = GL.glGetProgramInfoLog(prog).decode("utf-8", "ignore")
            raise RuntimeError(f"Program link failed: {log}")

        GL.glDetachShader(prog, vs)
        GL.glDetachShader(prog, fs)
        GL.glDeleteShader(vs)
        GL.glDeleteShader(fs)

        self._gl_program = prog
        self._gl_attr_pos = GL.glGetAttribLocation(prog, "aPos")
        self._gl_attr_texcoord = GL.glGetAttribLocation(prog, "aTexCoord")
        self._gl_uniform_use_tex = GL.glGetUniformLocation(prog, "uUseTexture")
        self._gl_uniform_color = GL.glGetUniformLocation(prog, "uColor")
        self._gl_uniform_sampler = GL.glGetUniformLocation(prog, "uTexture")

        self._gl_uniform_rect_min = GL.glGetUniformLocation(prog, "uRectMin")
        self._gl_uniform_rect_max = GL.glGetUniformLocation(prog, "uRectMax")
        self._gl_uniform_border = GL.glGetUniformLocation(prog, "uBorder")
        self._gl_uniform_viewport_size = GL.glGetUniformLocation(prog, "uViewportSize")

        self._gl_vbo = GL.glGenBuffers(1)

        self._gl_vao = None
        if hasattr(GL, "glGenVertexArrays"):
            self._gl_vao = GL.glGenVertexArrays(1)

    def _ensure_gl_texture(
        self,
        canvas_w: int,
        canvas_h: int,
        scale_factor: int,
    ) -> bool:
        """
        Ensure we have a GL texture for the current page or region preview.

        The texture is downsampled to exactly the size it will occupy in
        the framebuffer, so that one texel corresponds to one screen pixel.
        """
        if not self.selected_page:
            return False

        page = self.selected_page

        pil_img, reg = self._get_display_pil_image()
        img_w, img_h = pil_img.size
        preview_active = reg is not None

        # Save logical image size (used for layout, hit-testing, etc.)
        self._image_w = img_w
        self._image_h = img_h

        # --- Compute display geometry (same logic as _update_display_geometry) ---

        cw = max(1, canvas_w)
        ch = max(1, canvas_h)

        if preview_active:
            cwp, chp = cw, ch
        else:
            cwp = max(1, cw - _REGION_BORDER_WIDTH)
            chp = max(1, ch - _REGION_BORDER_WIDTH)

        scale = min(cwp / img_w, chp / img_h)
        if scale <= 0.0:
            scale = 1.0

        disp_w_logical = img_w * scale
        disp_h_logical = img_h * scale

        # Desired size on screen in framebuffer pixels (one texel == one pixel)
        fb_disp_w = max(1, int(round(disp_w_logical * scale_factor)))
        fb_disp_h = max(1, int(round(disp_h_logical * scale_factor)))

        # --- Build texture cache key including target size ---

        if reg is None:
            key: (
                tuple[str, int, int, str, int, int]
                | tuple[str, int, int, str, str, int, int, int, int, int, int, int]
            ) = (page.id, img_w, img_h, "full", fb_disp_w, fb_disp_h)
        else:
            key = (
                page.id,
                img_w,
                img_h,
                "reg",
                reg.id,
                reg.x1,
                reg.y1,
                reg.x2,
                reg.y2,
                reg.rotation % 360,
                fb_disp_w,
                fb_disp_h,
            )

        if key != self._gl_tex_key:
            # True area downsampling when shrinking
            pil_for_tex = pil_img.resize(
                (fb_disp_w, fb_disp_h),
                resample=PILImage.Resampling.BOX,
            )

            if pil_for_tex.mode != "RGBA":
                pil_for_tex = pil_for_tex.convert("RGBA")
            data = pil_for_tex.tobytes("raw", "RGBA")

            if self._gl_tex_id:
                GL.glDeleteTextures([self._gl_tex_id])
                self._gl_tex_id = None

            self._gl_tex_id = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._gl_tex_id)

            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)

            # 1:1 sampling, no additional filtering from GL
            GL.glTexParameteri(
                GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST
            )
            GL.glTexParameteri(
                GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST
            )
            GL.glTexParameteri(
                GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE
            )
            GL.glTexParameteri(
                GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE
            )

            GL.glTexImage2D(
                GL.GL_TEXTURE_2D,
                0,
                GL.GL_RGBA,
                fb_disp_w,
                fb_disp_h,
                0,
                GL.GL_RGBA,
                GL.GL_UNSIGNED_BYTE,
                data,
            )

            self._gl_tex_w = fb_disp_w
            self._gl_tex_h = fb_disp_h
            self._gl_tex_key = key

        self._gl_preview_active = preview_active

        # Compute offsets + logical scale for hit-testing and layout
        self._update_display_geometry(canvas_w, canvas_h, img_w, img_h, preview_active)

        return self._gl_tex_id is not None

    def on_gl_render(self, area: Gtk.GLArea, context: Gdk.GLContext) -> bool:
        """GLArea render callback: draw the page texture and region overlays."""
        area.make_current()
        err = area.get_error()
        if err is not None:
            print(f"GLArea error: {err.message}")
            return False

        if self._gl_program == 0:
            try:
                self._init_gl_resources(context)
            except Exception as e:
                print(f"Failed to init GL resources: {e}")
                return False

        scale_factor = max(1, area.get_scale_factor())

        width = area.get_allocated_width()
        height = area.get_allocated_height()
        if width <= 0 or height <= 0:
            return True

        fb_width = width * scale_factor
        fb_height = height * scale_factor

        GL.glViewport(0, 0, fb_width, fb_height)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        if not self.selected_page:
            return True

        # Use logical width/height for display geometry.
        if not self._ensure_gl_texture(width, height, scale_factor):
            return True

        GL.glUseProgram(self._gl_program)
        if self._gl_vao is not None:
            GL.glBindVertexArray(self._gl_vao)
        assert self._gl_vbo is not None
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._gl_vbo)

        def canvas_to_ndc(x: float, y: float) -> tuple[float, float]:
            # x, y in logical canvas units; NDC uses logical width/height
            nx = (2.0 * x / float(width)) - 1.0
            ny = 1.0 - (2.0 * y / float(height))
            return nx, ny

        # Use logical image size for geometry
        img_w = self._image_w
        img_h = self._image_h
        scale = self._display_scale

        disp_w = img_w * scale
        disp_h = img_h * scale
        x0 = self._display_offset_x
        y0 = self._display_offset_y
        x1 = x0 + disp_w
        y1 = y0 + disp_h

        x0n, y0n = canvas_to_ndc(x0, y0)
        x1n, y1n = canvas_to_ndc(x1, y1)

        # fmt: off
        vertices_quad = np.array(
            [
                # first triangle
                x0n, y0n, 0.0, 0.0,
                x1n, y0n, 1.0, 0.0,
                x1n, y1n, 1.0, 1.0,
                # second triangle
                x0n, y0n, 0.0, 0.0,
                x1n, y1n, 1.0, 1.0,
                x0n, y1n, 0.0, 1.0,
            ],
            dtype=np.float32,
        )
        # fmt: on

        GL.glBufferData(
            GL.GL_ARRAY_BUFFER,
            vertices_quad.nbytes,
            vertices_quad,
            GL.GL_DYNAMIC_DRAW,
        )

        stride = 4 * 4
        GL.glEnableVertexAttribArray(self._gl_attr_pos)
        GL.glVertexAttribPointer(
            self._gl_attr_pos,
            2,
            GL.GL_FLOAT,
            GL.GL_FALSE,
            stride,
            ctypes.c_void_p(0),
        )
        GL.glEnableVertexAttribArray(self._gl_attr_texcoord)
        GL.glVertexAttribPointer(
            self._gl_attr_texcoord,
            2,
            GL.GL_FLOAT,
            GL.GL_FALSE,
            stride,
            ctypes.c_void_p(2 * 4),
        )

        GL.glActiveTexture(GL.GL_TEXTURE0)
        assert self._gl_tex_id is not None
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._gl_tex_id)
        GL.glUniform1i(self._gl_uniform_sampler, 0)
        GL.glUniform1i(self._gl_uniform_use_tex, 1)
        GL.glUniform4f(self._gl_uniform_color, 1.0, 1.0, 1.0, 1.0)

        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 6)

        if self._gl_preview_active:
            GL.glUseProgram(0)
            if self._gl_vao is not None:
                GL.glBindVertexArray(0)
            return True

        # --- Region overlays (SDF borders) ---

        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)

        # Viewport size in framebuffer pixels (for gl_FragCoord)
        GL.glUniform2f(
            self._gl_uniform_viewport_size,
            float(fb_width),
            float(fb_height),
        )
        GL.glUniform1i(self._gl_uniform_use_tex, 0)

        def img_to_canvas(px: int, py: int) -> tuple[float, float]:
            return (
                self._display_offset_x + px * self._display_scale,
                self._display_offset_y + py * self._display_scale,
            )

        def draw_sdf_rounded_rect_canvas(
            x1c: float,
            y1c: float,
            x2c: float,
            y2c: float,
            color: tuple[float, float, float],
            border_px: float,
        ) -> None:
            """
            Draw a rounded-rectangle border in canvas coordinates using the SDF shader.
            """
            if x2c < x1c:
                x1c, x2c = x2c, x1c
            if y2c < y1c:
                y1c, y2c = y2c, y1c

            w = x2c - x1c
            h = y2c - y1c
            if w <= 0.0 or h <= 0.0:
                return

            # Convert logical canvas coordinates to framebuffer pixels for the SDF
            x1_px, y1_px = x1c * scale_factor, y1c * scale_factor
            x2_px, y2_px = x2c * scale_factor, y2c * scale_factor

            GL.glUniform2f(self._gl_uniform_rect_min, x1_px, y1_px)
            GL.glUniform2f(self._gl_uniform_rect_max, x2_px, y2_px)
            GL.glUniform1f(self._gl_uniform_border, border_px * scale_factor)
            GL.glUniform4f(self._gl_uniform_color, color[0], color[1], color[2], 1.0)

            # Expand quad in logical units
            bx1, by1 = x1c - border_px, y1c - border_px
            bx2, by2 = x2c + border_px, y2c + border_px

            bx1n, by1n = canvas_to_ndc(bx1, by1)
            bx2n, by2n = canvas_to_ndc(bx2, by2)
            # fmt: off
            vertices = np.array(
                [
                    bx1n, by1n, 0.0, 0.0,
                    bx2n, by1n, 0.0, 0.0,
                    bx2n, by2n, 0.0, 0.0,
                    bx1n, by1n, 0.0, 0.0,
                    bx2n, by2n, 0.0, 0.0,
                    bx1n, by2n, 0.0, 0.0,
                ],
                dtype=np.float32,
            )
            # fmt: on

            GL.glBufferData(
                GL.GL_ARRAY_BUFFER,
                vertices.nbytes,
                vertices,
                GL.GL_DYNAMIC_DRAW,
            )
            GL.glVertexAttribPointer(
                self._gl_attr_pos,
                2,
                GL.GL_FLOAT,
                GL.GL_FALSE,
                stride,
                ctypes.c_void_p(0),
            )
            GL.glVertexAttribPointer(
                self._gl_attr_texcoord,
                2,
                GL.GL_FLOAT,
                GL.GL_FALSE,
                stride,
                ctypes.c_void_p(2 * 4),
            )

            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 6)

        for r in self.selected_page.regions:
            x1c, y1c = img_to_canvas(r.x1, r.y1)
            x2c, y2c = img_to_canvas(r.x2, r.y2)
            col = (
                (0.2, 0.82, 0.478)
                if r.id == self._resize_region_id
                else (0.878, 0.106, 0.141)
                if r.id in self.expanded_region_ids
                else (0.208, 0.518, 0.894)
            )
            draw_sdf_rounded_rect_canvas(x1c, y1c, x2c, y2c, col, _REGION_BORDER_WIDTH)

        if self._drag_rect is not None:
            x1d, y1d, x2d, y2d = self._drag_rect
            draw_sdf_rounded_rect_canvas(
                x1d, y1d, x2d, y2d, (0.781, 0.094, 0.125), _REGION_BORDER_WIDTH
            )

        return False

    def on_da_scale_factor_changed(
        self, area: Gtk.GLArea, _pspec: GObject.ParamSpec
    ) -> None:
        """
        Called when the widget scale factor changes (e.g. moving between monitors with
        different DPI). Force a redraw so we recompute the viewport and SDF uniforms
        with the new scale.
        """
        area.queue_render()

    def on_glarea_unrealize(self, area: Gtk.GLArea) -> None:
        """GLArea unrealize callback: free GL resources."""
        area.make_current()
        if area.get_error() is not None:
            return

        if self._gl_tex_id:
            GL.glDeleteTextures([self._gl_tex_id])
            self._gl_tex_id = None
            self._gl_tex_key = None

        if self._gl_vbo:
            GL.glDeleteBuffers(1, [self._gl_vbo])
            self._gl_vbo = None

        if self._gl_vao:
            GL.glDeleteVertexArrays(1, [self._gl_vao])
            self._gl_vao = None

        if self._gl_program:
            GL.glDeleteProgram(self._gl_program)
            self._gl_program = 0

    def _img_to_canvas(self, px: int, py: int) -> tuple[float, float]:
        """Convert image pixel coordinates to canvas coordinates."""
        if self._display_scale <= 0:
            return 0.0, 0.0
        return (
            self._display_offset_x + px * self._display_scale,
            self._display_offset_y + py * self._display_scale,
        )

    def _canvas_to_image_coords(self, x: float, y: float) -> tuple[int, int]:
        """Convert canvas coordinates to image pixel coordinates."""
        if not self.selected_page:
            return 0, 0
        img_w, img_h = self.selected_page.pil_image.size
        if self._display_scale <= 0:
            return 0, 0

        x_rel = x - self._display_offset_x
        y_rel = y - self._display_offset_y

        ix = int(x_rel / self._display_scale)
        iy = int(y_rel / self._display_scale)

        ix = max(0, min(ix, img_w - 1))
        iy = max(0, min(iy, img_h - 1))
        return ix, iy

    def _hit_test_region_edge(
        self,
        x: float,
        y: float,
    ) -> tuple[Region, RegionHandle] | None:
        """
        Return (region, handle) if (x, y) is near a region border.

        Preference is given to the region whose eye was last clicked.
        """
        if not self.selected_page or not self.selected_page.regions:
            return None
        if self._display_scale <= 0:
            return None

        margin = 6.0  # px in canvas coordinates around borders/corners
        candidates: list[tuple[Region, RegionHandle]] = []

        # Hit-test in reverse order so later regions (visually on top) win by default
        for reg in reversed(self.selected_page.regions):
            x1c, y1c = self._img_to_canvas(reg.x1, reg.y1)
            x2c, y2c = self._img_to_canvas(reg.x2, reg.y2)

            # Quick reject if far outside the region + margin
            if (
                x < x1c - margin
                or x > x2c + margin
                or y < y1c - margin
                or y > y2c + margin
            ):
                continue

            on_left = abs(x - x1c) <= margin
            on_right = abs(x - x2c) <= margin
            on_top = abs(y - y1c) <= margin
            on_bottom = abs(y - y2c) <= margin

            handle: RegionHandle | None = None

            # Corners first: drag in both dimensions
            if on_left and on_top:
                handle = "top_left"
            elif on_right and on_top:
                handle = "top_right"
            elif on_left and on_bottom:
                handle = "bottom_left"
            elif on_right and on_bottom:
                handle = "bottom_right"
            # Edges next: drag in a single dimension
            elif on_left:
                handle = "left"
            elif on_right:
                handle = "right"
            elif on_top:
                handle = "top"
            elif on_bottom:
                handle = "bottom"

            if handle is not None:
                candidates.append((reg, handle))

        if not candidates:
            return None

        return candidates[0]

    def _set_viewer_cursor(self, cursor_name: str | None) -> None:
        """Set the mouse cursor for the page viewer, if it has changed."""
        if cursor_name == self._viewer_cursor_name:
            return
        self._viewer_cursor_name = cursor_name
        self.drawing_area.set_cursor(
            Gdk.Cursor.new_from_name(cursor_name) if cursor_name is not None else None
        )

    @staticmethod
    def _cursor_name_for_handle(handle: RegionHandle) -> str:
        """Map a resize handle name to a standard cursor name."""
        if handle in ("left", "right"):
            return "ew-resize"
        if handle in ("top", "bottom"):
            return "ns-resize"
        if handle in ("top_left", "bottom_right"):
            return "nwse-resize"
        if handle in ("top_right", "bottom_left"):
            return "nesw-resize"

    def _begin_region_resize(self, x: float, y: float) -> bool:
        """
        If (x, y) is near a region border, start a resize operation.

        Returns True if a resize was started, False otherwise.
        """
        if not self.selected_page:
            return False
        # Do not allow resizing while page is in cropped preview mode
        if self._preview_region_id is not None:
            return False

        hit = self._hit_test_region_edge(x, y)
        if hit is None:
            return False

        reg, edge = hit
        self._resize_region_id = reg.id
        self._resize_handle = edge
        self._resize_start_x = x
        self._resize_start_y = y

        # Update cursor to reflect the active resize handle
        cursor_name = self._cursor_name_for_handle(edge)
        self._set_viewer_cursor(cursor_name)

        self.drawing_area.queue_render()
        return True

    def _apply_region_resize(self, canvas_x: float, canvas_y: float) -> None:
        """Update the active region's rectangle based on the current drag position."""
        if not self.selected_page:
            return
        if not self._resize_region_id or not self._resize_handle:
            return

        page = self.selected_page
        reg = next((r for r in page.regions if r.id == self._resize_region_id), None)
        if reg is None:
            return

        ix, iy = self._canvas_to_image_coords(canvas_x, canvas_y)
        img_w, img_h = page.pil_image.size

        x1, y1, x2, y2 = reg.x1, reg.y1, reg.x2, reg.y2
        handle = self._resize_handle

        # Horizontal adjustment
        if "left" in handle:
            x1 = ix
        elif "right" in handle:
            x2 = ix

        # Vertical adjustment
        if "top" in handle:
            y1 = iy
        elif "bottom" in handle:
            y2 = iy

        res = self._normalize_and_clamp_region(x1, y1, x2, y2, img_w, img_h)
        if res is None:
            return
        reg.x1, reg.y1, reg.x2, reg.y2 = res

    # Gesture/drag handlers

    def on_da_press(
        self,
        _gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
    ) -> None:
        """Start a region resize (if near a border) or a new-region drag rectangle."""
        if not self.selected_page:
            return

        # First try to start resizing an existing region
        if self._begin_region_resize(x, y):
            return

        # Do not create new regions while a preview crop is active
        if self._preview_region_id is not None:
            return

        if n_press == 1:
            self._drag_rect = (x, y, x, y)
            self.drawing_area.queue_render()

    def on_da_drag(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        """Update either an active region resize or the new-region drag rectangle."""
        # Active region-resize takes priority
        if self._resize_region_id is not None:
            cur_x = self._resize_start_x + dx
            cur_y = self._resize_start_y + dy
            self._apply_region_resize(cur_x, cur_y)
            self.drawing_area.queue_render()
            return

        # Fallback: new-region creation drag
        if self._drag_rect is None:
            return
        if self._preview_region_id is not None:
            return
        start_x, start_y, _, _ = self._drag_rect
        cur_x, cur_y = start_x + dx, start_y + dy
        self._drag_rect = (start_x, start_y, cur_x, cur_y)
        self.drawing_area.queue_render()

    def on_da_release(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        """Finish a region resize or create a new region from the drag rectangle."""
        # Finish an active resize, if any
        if self._resize_region_id is not None:
            self._resize_region_id = None
            self._resize_handle = None
            # Sync the coordinate editor UI with the new region rectangle
            self.refresh_regions_list()
            self.refresh_groups_list()
            self.drawing_area.queue_render()
            return

        if self._preview_region_id is not None:
            # Ignore clicks/releases in preview mode
            self._drag_rect = None
            self.drawing_area.queue_render()
            return

        if self._drag_rect is None or not self.selected_page:
            self._drag_rect = None
            self.drawing_area.queue_render()
            return
        x1, y1, x2, y2 = self._drag_rect
        self._drag_rect = None
        self.drawing_area.queue_render()

        ix1, iy1 = self._canvas_to_image_coords(x1, y1)
        ix2, iy2 = self._canvas_to_image_coords(x2, y2)
        if abs(ix2 - ix1) < 10 or abs(iy2 - iy1) < 10:
            return

        img_w, img_h = self.selected_page.pil_image.size
        res = self._normalize_and_clamp_region(ix1, iy1, ix2, iy2, img_w, img_h)
        if res is None:
            return
        ix1, iy1, ix2, iy2 = res

        name = f"Region {len(self.selected_page.regions) + 1}"
        reg = self._new_region(name=name, x1=ix1, y1=iy1, x2=ix2, y2=iy2)
        self.selected_page.regions.append(reg)

        # New region starts expanded (and therefore highlighted)
        self.expanded_region_ids.add(reg.id)

        self.refresh_regions_list()
        self.refresh_groups_list()

    def on_da_motion(
        self,
        _controller: Gtk.EventControllerMotion,
        x: float,
        y: float,
    ) -> None:
        """
        Update the viewer cursor when hovering near region borders/corners.

        Shows resize cursors near draggable borders; default cursor elsewhere.
        """
        # No page or in preview mode: no region resizing, default cursor
        if not self.selected_page or self._preview_region_id is not None:
            self._set_viewer_cursor(None)
            return

        handle: RegionHandle | None = None

        # While actively resizing, keep the resize cursor consistent
        if self._resize_region_id is not None and self._resize_handle is not None:
            handle = self._resize_handle
        else:
            hit = self._hit_test_region_edge(x, y)
            if hit is not None:
                _reg, handle = hit

        cursor_name = (
            self._cursor_name_for_handle(handle) if handle is not None else None
        )
        self._set_viewer_cursor(cursor_name)

    def on_da_leave(self, _controller: Gtk.EventControllerMotion) -> None:
        """Restore the default cursor when the pointer leaves the viewer."""
        self._set_viewer_cursor(None)

    # --- Export ---

    def export_changes(self) -> None:
        """Export all regions of all pages to JPEG XL files in the output folder."""
        if not self.page_groups:
            self._info_dialog("Export", "Nothing to export.")
            return

        self._set_scanning_buttons_state(False)
        self._set_progress(0, 1, "Preparing export…")
        threading.Thread(target=self._export_changes_worker, daemon=True).start()

    def _export_changes_worker(self) -> None:
        """Background export worker: crop, rotate, and save all regions."""
        out_dir = self.settings.folder
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            GLib.idle_add(
                self._error_dialog,
                "Export",
                f"Failed to create output folder “{out_dir}”: {e}",
            )
            GLib.idle_add(self._set_scanning_buttons_state, True)
            GLib.idle_add(self._reset_progress)
            return

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        items: list[tuple[int, int, int, Page, Region]] = []
        for gidx, grp in enumerate(self.page_groups, 1):
            for pi, page in enumerate(grp.pages, 1):
                for ri, reg in enumerate(page.regions, 1):
                    items.append((gidx, pi, ri, page, reg))

        total = len(items)
        if total == 0:

            def finish_empty() -> None:
                self._reset_progress()
                self._set_scanning_buttons_state(True)
                self._info_dialog("Export", "Nothing to export.")

            GLib.idle_add(finish_empty)
            return

        exported_count = 0
        last_error: str | None = None

        for idx, (gidx, pi, ri, page, reg) in enumerate(items, start=1):
            try:
                crop = page.pil_image.crop((reg.x1, reg.y1, reg.x2, reg.y2))

                rot = reg.rotation % 360
                if rot:
                    crop = crop.rotate(-rot, expand=True)

                if crop.mode != "RGB":
                    crop = crop.convert("RGB")
                arr = np.array(crop)
                timg = Image.new_from_array(arr)
                fname = f"{timestamp}_G{gidx:02d}_P{pi:02d}_R{ri:02d}.jxl"
                out_path = out_dir / fname
                timg.jxlsave(out_path, lossless=True)
            except Exception as e:
                last_error = str(e)
            else:
                exported_count += 1
            self._set_progress(idx, total, f"Exporting {idx}/{total}…")

        def finish() -> None:
            self.refresh_regions_list()
            self._set_scanning_buttons_state(True)
            self._reset_progress()
            msg = f"Exported {self._plural(exported_count, 'region')}."
            if last_error is not None:
                msg += f" Last error: {last_error}"
            self._info_dialog("Export", msg)

        GLib.idle_add(finish)

    # --- Cleanup ---

    def close_app(self) -> None:
        """Close the scanner device, shut down SANE, and destroy the window."""
        if self.scanner_dev is not None:
            try:
                self.scanner_dev.close()
            except Exception:
                pass
        self._exit_sane()
        self.destroy()


# --- Small utilities ---


def simple_prompt_async(
    parent: Gtk.Window,
    title: str,
    label: str,
    initial: str,
    callback: Callable[[str | None], None],
) -> None:
    """Show a simple text entry dialog and deliver the result asynchronously."""
    dialog = Adw.AlertDialog.new(title, None)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

    lbl = Gtk.Label(label=label, xalign=0)
    box.append(lbl)

    entry = Gtk.Entry()
    entry.set_text(initial)
    box.append(entry)

    dialog.set_extra_child(box)

    dialog.add_response("cancel", "_Cancel")
    dialog.add_response("ok", "_OK")
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

    def on_response(_dlg: Adw.AlertDialog, response: str) -> None:
        if response == "ok":
            callback(entry.get_text())
        else:
            callback(None)

    dialog.connect("response", on_response)
    dialog.present(parent)
    GLib.idle_add(lambda: not entry.grab_focus())


# --- libadwaita Application wrapper ---


class ScannerApplication(Adw.Application):
    """libadwaita Application wrapper that owns the main window."""

    def __init__(self) -> None:
        super().__init__(application_id="org.kurbo96.Cendar")
        self.window: ScannerWindow | None = None

    @override
    def do_activate(self) -> None:
        """Create and present the main window on first activation."""
        if not self.window:
            self.window = ScannerWindow(self)
            self.window.connect("close-request", self.on_close_request)
        self.window.present()

    def on_close_request(self, win: ScannerWindow) -> bool:
        """Handle the window close request by performing cleanup."""
        win.close_app()
        return True


def run() -> None:
    """Run the `ScannerApplication` event loop."""
    Adw.init()
    app = ScannerApplication()
    app.run(None)


if __name__ == "__main__":
    run()
