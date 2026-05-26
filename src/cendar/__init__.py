from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Callable, Literal, final, override

import cairo
import gi
from PIL import Image as PILImage
from pyvips import Image

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

if TYPE_CHECKING:
    import sane

CropPreset = Literal["full", "preset_1200_1700"]

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
    folder: Path = field(default_factory=lambda: Path.cwd())
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
    """Main window for the scanner/page manager."""

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app)
        self.set_title("Scanner Page Manager")
        self.set_default_size(1400, 900)
        self.set_size_request(400, 600)

        # Global settings model
        self.settings = AppSettings()

        # Temp dir for JXLs
        self.tmpdir_ctx = TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_ctx.name)
        self.tmp_idx = 0

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
        self._display_pixbuf: GdkPixbuf.Pixbuf | None = None
        self._display_scale: float = 1.0
        self._drag_rect: tuple[float, float, float, float] | None = None

        # Offset of the image inside the DrawingArea (for centering)
        self._display_offset_x: float = 0.0
        self._display_offset_y: float = 0.0

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

        self.folder_entry: Adw.EntryRow | None = None

        self.btn_new_group: Gtk.Button
        self.btn_clear_groups: Gtk.Button
        self.btn_export: Gtk.Button
        self.btn_refresh_scanners: Gtk.Button
        self.btn_browse_folder: Gtk.Button

        self.drawing_area: Gtk.DrawingArea

        # Guards
        self._suppress_group_expand_signal: bool = False
        self._suppress_scanner_row_signal: bool = False
        self._suppress_region_coord_update: bool = False

        self._install_css()
        self._build_ui()

        self.refresh_groups_list()
        self.refresh_regions_list()

        self._start_initial_sane_init()

    # --- Small internal helpers/factories ---

    @staticmethod
    def _string_list_set(store: Gtk.StringList | None, values: list[str]) -> None:
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
        if plural is None:
            plural = singular + "s"
        return f"{n} {singular if n == 1 else plural}"

    @staticmethod
    def _rotation_ccw_from_display(label: str) -> int:
        return _ROTATION_LABEL_TO_CCW.get(label.strip(), 0)

    @staticmethod
    def _rotation_display_from_ccw(deg_ccw: int) -> str:
        return _CCW_TO_ROTATION_LABEL.get(deg_ccw % 360, "0°")

    @staticmethod
    def _crop_preset_from_display(label: str) -> CropPreset:
        return _CROP_LABEL_TO_PRESET.get(label.strip(), "full")

    def _calc_preset_region(
        self, img_w: int, img_h: int, preset: CropPreset
    ) -> tuple[int, int, int, int]:
        if preset == "preset_1200_1700":
            res = self._normalize_and_clamp_region(
                1200, 1700, img_w, img_h, img_w, img_h
            )
            if res is not None:
                return res
        return 0, 0, img_w, img_h

    def _new_region(self, *, name: str, x1: int, y1: int, x2: int, y2: int) -> Region:
        return Region(id=str(uuid.uuid4()), name=name, x1=x1, y1=y1, x2=x2, y2=y2)

    # --- CSS ---

    def _install_css(self) -> None:
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
        if self._sane_initialized:
            return
        import sane

        sane.init()
        self._sane_initialized = True

    def _exit_sane(self) -> None:
        if not self._sane_initialized:
            return
        import sane

        sane.exit()
        self._sane_initialized = False

    def _list_sane_devices(self) -> list[tuple[str, str, str, str]]:
        import sane

        self._init_sane()
        return sane.get_devices()

    def _start_initial_sane_init(self) -> None:
        self._set_scanning_buttons_state(False)
        self._set_progress(0, 0, "Initializing scanner...")
        threading.Thread(
            target=self._initial_sane_worker,
            daemon=True,
        ).start()

    def _update_device_store_and_selection(self) -> None:
        display = [
            f"{name} ({vendor} {model})"
            for name, vendor, model, _ in self.available_devices
        ]
        self._string_list_set(self.device_store, display)

        prefix = "pixma:"
        preferred_index: int | None = None
        for idx, (name, _, _, _) in enumerate(self.available_devices):
            if name.startswith(prefix):
                preferred_index = idx
                break
        if preferred_index is None and self.available_devices:
            preferred_index = 0

        self._suppress_scanner_row_signal = True
        try:
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
        available_devices: list[tuple[str, str, str, str]] = []
        err: str | None = None

        try:
            self._set_progress(1, 3, "Detecting scanners...")
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

            self._set_progress(2, 3, "Opening scanner...")
            if self.selected_device_name is not None:
                self._apply_scanner()

            self._set_scanning_buttons_state(True)
            self._reset_progress()

        GLib.idle_add(finish)

    # --- UI construction ---

    def _build_ui(self) -> None:
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

        self.btn_export = Gtk.Button()
        export_content = Adw.ButtonContent()
        export_content.set_icon_name("document-save-symbolic")
        export_content.set_label("Export")
        self.btn_export.set_child(export_content)
        self.btn_export.add_css_class("suggested-action")
        self.btn_export.connect("clicked", lambda _b: self.export_changes())
        content_header.pack_end(self.btn_export)

        content_tv.add_top_bar(content_header)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.set_margin_top(6)
        main_box.set_margin_bottom(6)
        main_box.set_margin_start(3)
        main_box.set_margin_end(6)
        main_box.set_hexpand(True)
        main_box.set_vexpand(True)
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
        self,
    ) -> tuple[Gtk.ScrolledWindow, Gtk.ListBox]:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_propagate_natural_height(True)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.set_hexpand(True)
        wrapper.set_vexpand(True)
        scrolled.set_child(wrapper)

        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.set_vexpand(False)
        lb.set_valign(Gtk.Align.START)
        lb.set_margin_top(6)
        lb.set_margin_bottom(6)
        lb.set_margin_start(6)
        lb.set_margin_end(6)
        wrapper.append(lb)

        return scrolled, lb

    def _icon_only_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
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
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.add_css_class("dim-label")
        row.set_activatable(False)
        row.set_selectable(False)
        listbox.append(row)

    @staticmethod
    def _set_many_sensitive(enabled: bool, *widgets: Gtk.Widget | None) -> None:
        for w in widgets:
            if w is not None:
                w.set_sensitive(enabled)

    # --- Sidebar: settings + groups ---

    def _build_scanner_settings(self, parent: Gtk.Box) -> None:
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
        reg_box = self._build_card(
            parent, "Regions", hexpand=True, vexpand=True, pad_title=True
        )

        scrolled, self.regions_list = self._create_scrolled_list()
        reg_box.append(scrolled)

    # --- Top pane: page viewer ---

    def _build_page_panel(self, parent: Gtk.Box) -> None:
        viewer_box = self._build_card(
            parent, "Page Viewer", hexpand=True, vexpand=True, pad_title=True
        )

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.add_css_class("card")
        self.drawing_area.set_hexpand(True)
        self.drawing_area.set_vexpand(True)
        self.drawing_area.set_margin_top(6)
        self.drawing_area.set_margin_bottom(6)
        self.drawing_area.set_margin_start(6)
        self.drawing_area.set_margin_end(6)
        self.drawing_area.set_draw_func(self.on_draw)
        viewer_box.append(self.drawing_area)

        gesture_click = Gtk.GestureClick.new()
        gesture_click.set_button(Gdk.BUTTON_PRIMARY)
        gesture_click.connect("pressed", self.on_da_press)
        gesture_click.connect("released", self.on_da_release)
        self.drawing_area.add_controller(gesture_click)

        gesture_drag = Gtk.GestureDrag.new()
        gesture_drag.connect("drag-update", self.on_da_drag)
        self.drawing_area.add_controller(gesture_drag)

    def _show_cancel_button(self, show: bool) -> None:
        self.sidebar_btn_cancel_scan.set_visible(show)
        self.sidebar_btn_cancel_scan.set_sensitive(show)

    # --- Inline settings/combos ---

    def on_scanner_row_changed(self, _row: Adw.ComboRow, _pspec: Gio.ParamSpec) -> None:
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

    def on_dpi_changed(self, _row: Adw.ComboRow, _pspec: Gio.ParamSpec) -> None:
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

    def on_mode_changed(self, _row: Adw.ComboRow, _pspec: Gio.ParamSpec) -> None:
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
        _pspec: Gio.ParamSpec,
    ) -> None:
        viewer_box = self._viewer_paned_viewer_box
        regions_box = self._viewer_paned_regions_box

        if paned.get_orientation() == Gtk.Orientation.HORIZONTAL:
            # Horizontal: regions left (start), preview right (end)
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
            # Vertical: preview above (start), regions below (end)
            if (
                paned.get_start_child() is not viewer_box
                or paned.get_end_child() is not regions_box
            ):
                paned.set_start_child(None)
                paned.set_end_child(None)
                paned.set_start_child(viewer_box)
                paned.set_end_child(regions_box)
                regions_box.set_margin_top(6)

        # Reapply the split position for the new orientation
        GLib.idle_add(self._apply_viewer_paned_ratio, paned)

    def _apply_viewer_paned_ratio(self, paned: Gtk.Paned) -> None:
        """Set paned.position so preview and regions each get ~half the space."""
        alloc = paned.get_allocation()
        if paned.get_orientation() == Gtk.Orientation.VERTICAL:
            total = alloc.height
        else:
            total = alloc.width

        if total <= 0:
            return

        paned.set_position(total // 2)

    def _sync_default_rot_combo_from_settings(self) -> None:
        label = self._rotation_display_from_ccw(
            self.settings.defaults.default_rotation_ccw
        )
        try:
            idx = _ROTATION_LABELS.index(label)
        except ValueError:
            idx = 0
        self.default_rot_combo.set_selected(idx)

    def on_default_rotation_changed(
        self, _row: Adw.ComboRow, _pspec: Gio.ParamSpec
    ) -> None:
        idx = self.default_rot_combo.get_selected()
        label = _ROTATION_LABELS[idx]
        self.settings.defaults.default_rotation_ccw = self._rotation_ccw_from_display(
            label
        )

    def on_default_crop_preset_changed(
        self, _row: Adw.ComboRow, _pspec: Gio.ParamSpec
    ) -> None:
        idx = self.default_crop_combo.get_selected()
        label = _CROP_PRESET_LABELS[idx]
        self.settings.defaults.default_crop_preset = self._crop_preset_from_display(
            label
        )

    def on_folder_changed(self, _row: Adw.EntryRow, _pspec: Gio.ParamSpec) -> None:
        if self.folder_entry is None:
            return
        self.settings.folder = Path(self.folder_entry.get_text().strip()).expanduser()

    def on_browse_folder(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select output folder")
        dialog.select_folder(
            self,
            None,
            self._on_folder_dialog_response,
        )

    def _on_folder_dialog_response(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
    ) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        if path and self.folder_entry is not None:
            self.folder_entry.set_text(path)

    # --- Header titles ---

    def _update_header_titles(self) -> None:
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
        self._set_scanning_buttons_state(False)
        self._set_progress(0, 0, "Detecting scanners...")
        threading.Thread(
            target=self._refresh_scanner_list_worker,
            daemon=True,
        ).start()

    def _refresh_scanner_list_worker(self) -> None:
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
        dialog = Adw.AlertDialog.new(title, text)
        dialog.add_response("ok", "_OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.set_response_appearance("ok", appearance)
        dialog.present(self)

    def _error_dialog(self, title: str, text: str) -> None:
        self._show_message_dialog(title, text, Adw.ResponseAppearance.DESTRUCTIVE)

    def _info_dialog(self, title: str, text: str) -> None:
        self._show_message_dialog(title, text, Adw.ResponseAppearance.SUGGESTED)

    def _warning_dialog(self, title: str, text: str) -> None:
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
        if lb is None:
            return
        child = lb.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            lb.remove(child)
            child = nxt

    def refresh_groups_list(self) -> None:
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
                grp_row.set_subtitle(f"{len(grp.pages)} page(s)")
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
                    drag_source.connect(
                        "drag-begin",
                        lambda source, drag: self._on_page_drag_begin(source, drag),
                    )
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

        # Always keep the header in sync with current selection
        self._update_header_titles()

        if not self.selected_page:
            self._add_placeholder_row(
                self.regions_list,
                "No page selected",
                "Select a page to manage its regions.",
            )
            self.drawing_area.queue_draw()
            return

        page = self.selected_page

        if not page.regions:
            self._add_placeholder_row(
                self.regions_list,
                "No regions yet",
                "Drag on the Page Viewer to create a new region.",
            )
            self.drawing_area.queue_draw()
            return

        for i, reg in enumerate(page.regions):
            w, h = reg.x2 - reg.x1, reg.y2 - reg.y1

            row = Adw.ExpanderRow()
            row.set_title(reg.name)
            row.set_subtitle(f"{w}×{h} px")
            row.set_activatable(False)
            row.set_selectable(False)
            setattr(row, "_index", i)
            setattr(row, "_region_id", reg.id)

            row.set_expanded(reg.id in self.expanded_region_ids)

            def on_expanded_changed(
                exp_row: Adw.ExpanderRow, _pspec: Gio.ParamSpec, rid: str = reg.id
            ) -> None:
                if exp_row.get_expanded():
                    self.expanded_region_ids.add(rid)
                else:
                    self.expanded_region_ids.discard(rid)
                self.drawing_area.queue_draw()

            row.connect("notify::expanded", on_expanded_changed)

            delete_btn = self._icon_only_button(
                "user-trash-symbolic",
                "Delete region",
            )
            delete_btn.connect("clicked", lambda _b, idx=i: self.delete_region(idx))
            row.add_suffix(delete_btn)

            rename_btn = self._icon_only_button(
                "document-edit-symbolic",
                "Rename region",
            )
            rename_btn.connect("clicked", lambda _b, idx=i: self.rename_region(idx))
            row.add_suffix(rename_btn)

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

                exp_row.set_subtitle(f"{w2}×{h2} px")
                self.drawing_area.queue_draw()

            # Preset combo: "(Choose)" + real presets; "(Choose)" selected by default
            preset_model = Gtk.StringList.new(list(_REGION_CROP_PRESET_LABELS))
            preset_row = Adw.ComboRow(
                title="Preset",
                model=preset_model,
            )
            preset_row.set_selected(0)
            row.add_row(preset_row)

            def on_preset_selected(
                _combo: Adw.ComboRow,
                _pspec: Gio.ParamSpec,
                idx: int = i,
                left: Adw.EntryRow = left_row,
                top: Adw.EntryRow = top_row,
                right: Adw.EntryRow = right_row,
                bottom: Adw.EntryRow = bottom_row,
            ) -> None:
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

                # Apply immediately after preset selection
                apply_coords()

            preset_row.connect("notify::selected", on_preset_selected)

            # Row for misc actions (rounding etc.)
            actions_row = Adw.ActionRow(title="Actions")
            actions_row.set_activatable(False)

            round_btn = self._icon_only_button(
                "format-indent-more-symbolic",
                "Round coordinates to multiple of 10",
            )
            actions_row.add_suffix(round_btn)

            row.add_row(actions_row)

            # Separate Apply button row, visually emphasized
            apply_row = Adw.ButtonRow(title="Apply coordinates")
            apply_row.add_css_class("suggested-action")
            apply_row.set_activatable(True)
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

                # Commit rounded coordinates immediately
                apply_coords()

            round_btn.connect("clicked", round_coords)

            self.regions_list.append(row)

        self.drawing_area.queue_draw()

    # --- Copy/paste regions ---

    def copy_regions_from_page(self) -> None:
        if not self.selected_page:
            self._info_dialog("Copy regions", "No page selected.")
            return
        self.copied_regions = [
            self._new_region(name=reg.name, x1=reg.x1, y1=reg.y1, x2=reg.x2, y2=reg.y2)
            for reg in self.selected_page.regions
        ]
        self._info_dialog(
            "Copy regions",
            f"Copied {self._plural(len(self.copied_regions), 'region')} "
            + "from current page.",
        )

    def paste_regions_to_page(self) -> None:
        if not self.selected_page:
            self._info_dialog("Paste regions", "No page selected.")
            return
        if not self.copied_regions:
            self._info_dialog("Paste regions", "No copied regions to paste.")
            return
        for reg in self.copied_regions:
            new_reg = self._new_region(
                name=reg.name, x1=reg.x1, y1=reg.y1, x2=reg.x2, y2=reg.y2
            )
            self.selected_page.regions.append(new_reg)
        self.refresh_regions_list()
        self.refresh_groups_list()

    # --- Region management ---

    def rename_region(self, idx: int) -> None:
        if not self.selected_page:
            return
        if not (0 <= idx < len(self.selected_page.regions)):
            return

        reg = self.selected_page.regions[idx]

        def done(new_name: str | None) -> None:
            if new_name:
                reg.name = new_name
                self.refresh_regions_list()

        simple_prompt_async(
            self,
            "Rename region",
            "Region name:",
            reg.name,
            done,
        )

    def delete_region(self, idx: int) -> None:
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
            self.refresh_regions_list()
            self.refresh_groups_list()

        self._confirm_dialog(
            "Delete region",
            "Delete this region?",
            callback=on_confirm,
        )

    # --- Group/page selection ---

    def _on_group_row_expanded(self, row: Adw.ExpanderRow, group_idx: int) -> None:
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
        if not (0 <= idx < len(self.page_groups)):
            return
        self.selected_group = self.page_groups[idx]
        self._start_scan("into_group")

    def _delete_group_index(self, idx: int) -> None:
        if not (0 <= idx < len(self.page_groups)):
            return
        self.selected_group = self.page_groups[idx]
        self.delete_group()

    def _rotate_page_index(
        self, group_idx: int, page_idx: int, degrees_cw: int
    ) -> None:
        if not (0 <= group_idx < len(self.page_groups)):
            return
        grp = self.page_groups[group_idx]
        if not (0 <= page_idx < len(grp.pages)):
            return
        self.selected_group = grp
        self.selected_page = grp.pages[page_idx]
        self.rotate_page(degrees_cw)

    def _delete_page_index(self, group_idx: int, page_idx: int) -> None:
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
            self.refresh_groups_list()
            self.refresh_regions_list()

        self._confirm_dialog(
            "Delete group",
            "Delete this group and all its pages?",
            callback=on_confirm,
        )

    def clear_groups(self) -> None:
        if not self.page_groups:
            return

        def on_confirm(ok: bool) -> None:
            if not ok:
                return
            self.page_groups = []
            self.selected_group = None
            self.selected_page = None
            self.expanded_region_ids.clear()
            self.refresh_groups_list()
            self.refresh_regions_list()

        self._confirm_dialog(
            "Clear all groups",
            "This will remove all groups, pages, and regions from this session. Continue?",
            callback=on_confirm,
        )

    # --- Page management ---

    def delete_page(self) -> None:
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
        data = f"{group_idx}:{page_idx}"
        variant = GLib.Variant("s", data)
        return Gdk.ContentProvider.new_for_value(variant)

    def _on_page_drag_begin(self, _source: Gtk.DragSource, drag: Gdk.Drag) -> None:
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
        self._set_progress(0, 1, _SIDEBAR_DUMMY_SUBTITLE)

    def _set_scanning_buttons_state(self, enabled: bool) -> None:
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
            self._set_progress(0, 1, "Scanning...")
            self._show_cancel_button(True)
            self._scan_thread = threading.Thread(
                target=self._scan_worker, args=(mode,), daemon=True
            )
            self._scan_thread.start()

    def on_cancel_scan_clicked(self, _button: Gtk.Button) -> None:
        self._scan_cancel_event.set()
        self.sidebar_btn_cancel_scan.set_sensitive(False)
        self._set_progress(None, None, "Cancelling scan...")

    def _scan_worker(self, mode: Literal["new_group", "into_group"]) -> None:
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
        def progress(current: int, total: int) -> None:
            if self._scan_cancel_event.is_set():
                try:
                    dev.cancel()
                except Exception:
                    pass
                return
            self._set_progress(current, total, "Scanning...")

        try:
            np_arr = dev.arr_scan(progress=progress)
            img = Image.new_from_array(np_arr)
        except Exception as e:
            if self._scan_cancel_event.is_set():
                print(f"Scan cancelled: {e!r}")
                return None
            print(f"Scan failed: {e!r}")
            return None

        self._set_progress(1, 1, "Processing...")

        tmp_path = self.tmpdir / f"tmp-{self.tmp_idx}.jxl"
        self.tmp_idx += 1
        img.jxlsave(tmp_path, lossless=True)

        pil_img = PILImage.fromarray(img.numpy)

        ccw = self.settings.defaults.default_rotation_ccw % 360
        if ccw != 0:
            pil_img = pil_img.rotate(ccw, expand=True)

        self._set_progress(1, 1, "Done")
        return pil_img

    # --- Page/region creation and rotation ---

    def _page_from_pil(self, pil_img: PILImage.Image) -> Page:
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

    def _ensure_pixbuf(self) -> None:
        if not self.selected_page:
            self._display_pixbuf = None
            self._display_scale = 1.0
            self._display_offset_x = 0.0
            self._display_offset_y = 0.0
            return

        pil_img = self.selected_page.pil_image
        w, h = pil_img.size
        alloc = self.drawing_area.get_allocation()
        cw, ch = max(1, alloc.width), max(1, alloc.height)

        scale = min(cw / w, ch / h)
        if scale <= 0:
            scale = 1.0

        disp_w, disp_h = int(w * scale), int(h * scale)

        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        data = pil_img.tobytes()
        pixbuf = GdkPixbuf.Pixbuf.new_from_data(
            data,
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            w,
            h,
            w * 3,
        )
        self._display_pixbuf = pixbuf.scale_simple(
            disp_w, disp_h, GdkPixbuf.InterpType.BILINEAR
        )
        self._display_scale = scale

        self._display_offset_x = (cw - disp_w) / 2.0
        self._display_offset_y = (ch - disp_h) / 2.0

    def on_draw(
        self,
        _area: Gtk.DrawingArea,
        ctx: cairo.Context[cairo.Surface],
        _w: int,
        _h: int,
    ) -> None:
        if not self.selected_page:
            return

        self._ensure_pixbuf()
        if not self._display_pixbuf:
            return

        offset_x = self._display_offset_x
        offset_y = self._display_offset_y

        Gdk.cairo_set_source_pixbuf(ctx, self._display_pixbuf, offset_x, offset_y)
        ctx.paint()

        def img_to_canvas(px: int, py: int) -> tuple[float, float]:
            return (
                offset_x + px * self._display_scale,
                offset_y + py * self._display_scale,
            )

        for r in self.selected_page.regions:
            x1, y1 = img_to_canvas(r.x1, r.y1)
            x2, y2 = img_to_canvas(r.x2, r.y2)
            is_highlighted = r.id in self.expanded_region_ids
            if is_highlighted:
                ctx.set_source_rgb(1.0, 0.27, 0.0)
                line_w = 3.0
            else:
                ctx.set_source_rgb(0.12, 0.56, 1.0)
                line_w = 2.0
            ctx.set_line_width(line_w)
            ctx.rectangle(x1, y1, x2 - x1, y2 - y1)
            ctx.stroke()

        if self._drag_rect is not None:
            x1, y1, x2, y2 = self._drag_rect
            ctx.set_source_rgb(1.0, 0.27, 0.0)
            ctx.set_line_width(2.0)
            ctx.rectangle(x1, y1, x2 - x1, y2 - y1)
            ctx.stroke()

    def _canvas_to_image_coords(self, x: float, y: float) -> tuple[int, int]:
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

    # Gesture/drag handlers

    def on_da_press(
        self,
        _gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
    ) -> None:
        if not self.selected_page:
            return
        if n_press == 1:
            self._drag_rect = (x, y, x, y)
            self.drawing_area.queue_draw()

    def on_da_drag(
        self,
        _gesture: Gtk.GestureDrag,
        dx: float,
        dy: float,
    ) -> None:
        if self._drag_rect is None:
            return
        start_x, start_y, _, _ = self._drag_rect
        cur_x = start_x + dx
        cur_y = start_y + dy
        self._drag_rect = (start_x, start_y, cur_x, cur_y)
        self.drawing_area.queue_draw()

    def on_da_release(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        if self._drag_rect is None or not self.selected_page:
            self._drag_rect = None
            self.drawing_area.queue_draw()
            return
        x1, y1, x2, y2 = self._drag_rect
        self._drag_rect = None
        self.drawing_area.queue_draw()

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

    # --- Export ---

    def export_changes(self) -> None:
        if not self.page_groups:
            self._info_dialog("Export", "Nothing to export.")
            return

        self._set_scanning_buttons_state(False)
        self._set_progress(0, 1, "Preparing export...")
        threading.Thread(
            target=self._export_changes_worker,
            daemon=True,
        ).start()

    def _export_changes_worker(self) -> None:
        out_dir = self.settings.folder
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            GLib.idle_add(
                self._error_dialog,
                "Export",
                f"Failed to create output folder '{out_dir}': {e}",
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
        import numpy as np

        for idx, (gidx, pi, ri, page, reg) in enumerate(items, start=1):
            try:
                crop = page.pil_image.crop((reg.x1, reg.y1, reg.x2, reg.y2))
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
            self._set_progress(idx, total, f"Exporting {idx}/{total}...")

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
        if self.scanner_dev is not None:
            try:
                self.scanner_dev.close()
            except Exception:
                pass
        self._exit_sane()
        self.tmpdir_ctx.cleanup()
        self.destroy()


# --- Small utilities ---


def simple_prompt_async(
    parent: Gtk.Window,
    title: str,
    label: str,
    initial: str,
    callback: Callable[[str | None], None],
) -> None:
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
    GLib.idle_add(entry.grab_focus)


# --- libadwaita Application wrapper ---


class ScannerApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="com.example.ScannerPageManager")
        self.window: ScannerWindow | None = None

    @override
    def do_activate(self) -> None:
        if not self.window:
            self.window = ScannerWindow(self)
            self.window.connect("close-request", self.on_close_request)
        self.window.present()

    def on_close_request(self, win: ScannerWindow) -> bool:
        win.close_app()
        return True


def run() -> None:
    Adw.init()
    app = ScannerApplication()
    app.run(None)


if __name__ == "__main__":
    run()
