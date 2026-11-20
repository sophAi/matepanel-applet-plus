#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Taskwarrior Interactive Desktop Calendar Widget (`taskwarrior_desktop_widget.py`)

Features:
- Embeds seamlessly onto the MATE desktop (Compiz/Marco + Caja safe):
  - Borderless RGBA transparent window (`set_decorated(False)`), `set_keep_below(True)`,
    `stick()` across all workspaces, `skip_taskbar` + `skip_pager`.
- Single full 7x6 monthly calendar displaying tasks directly inside each date cell as
  color-coded task pills (overdue/high=red, warn/medium=amber, normal=blue, completed=green).
- Real interactive operations:
  - Click any task pill -> direct Edit / Complete / Delete dialog.
  - Click empty area of any date cell -> Day Popover with full task list + quick add (`due:YYYY-MM-DD`).
  - Right-click anywhere -> Context menu (Add, Undo, Lock/Unlock drag, Appearance Settings, Quit).
- Live 0.0% idle CPU sync with Taskwarrior Applet, Clock-Weather tab & CLI via `Gio.FileMonitor` (~/.task).
"""

import calendar
from datetime import date, datetime, timedelta
import html
import json
import math
import os
import signal
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk
import cairo

_COMMON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)
from taskwarrior_shared import TaskwarriorBackend

CONFIG_DIR = os.path.expanduser("~/.config/matepanel-applet-plus")
CONFIG_PATH = os.path.join(CONFIG_DIR, "taskwarrior-desktop.json")
TASK_COLORS_PATH = os.path.join(CONFIG_DIR, "task-colors.json")
WEATHER_CACHE_PATH = os.path.join(CONFIG_DIR, "weather-cache.json")

TASK_COLOR_PRESETS = [
    ("#dc2626", "紅"),
    ("#ea580c", "橘"),
    ("#ca8a04", "黃"),
    ("#16a34a", "綠"),
    ("#0891b2", "青"),
    ("#2563eb", "藍"),
    ("#9333ea", "紫"),
    ("#db2777", "粉"),
]

DEFAULT_CONFIG = {
    "x": 80,
    "y": 60,
    "width": 940,
    "height": 620,
    "mini_x": None,
    "mini_y": None,
    "locked": False,
    "bg_color": "#0f172a",
    "bg_opacity": 58,           # 0..100% 整體背景透明度
    "cell_opacity": 42,         # 0..100% 日期格子底色透明度
    "today_bg_color": "#1a5c4d",        # 今日日期背景顏色
    "today_border_color": "#38e0a6",    # 今日日期邊框顏色
    "cur_month_bg_color": "#141f2e",    # 本月日期背景顏色
    "other_month_bg_color": "#0d141f",  # 非本月日期背景顏色
    "fg_color": "#f1f5f9",      # 文字顏色
    "text_shadow": True,        # 透明背景時文字陰影增強可讀性
    "font_size": 11,            # 9..18
    "show_completed": True,     # 在日期格子內顯示已完成任務
    "show_weather": True,       # 在日期列顯示 7 天天氣預報
    "due_soon_days": 3,
}


def hex_to_rgb(hex_str, default=(0.06, 0.09, 0.16)):
    if not hex_str or not isinstance(hex_str, str):
        return default
    s = hex_str.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        return (
            int(s[0:2], 16) / 255.0,
            int(s[2:4], 16) / 255.0,
            int(s[4:6], 16) / 255.0,
        )
    except ValueError:
        return default


def rgb_luminance(rgb):
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def aqi_to_rgb(aqi):
    """Map US AQI value to RGB tuple matching clock_weather_applet.py."""
    if aqi is None:
        return None
    try:
        v = int(round(float(aqi)))
    except (TypeError, ValueError):
        return None
    if v <= 50:
        return hex_to_rgb("#34d399")
    if v <= 100:
        return hex_to_rgb("#fbbf24")
    if v <= 150:
        return hex_to_rgb("#fb923c")
    if v <= 200:
        return hex_to_rgb("#f87171")
    return hex_to_rgb("#c084fc")


def wmo_to_icon_name(code):
    """Map WMO weather code (0..99) to standard freedesktop/MATE icon theme name."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "weather-few-clouds"
    if c == 0:
        return "weather-clear"
    if c in (1, 2):
        return "weather-few-clouds"
    if c == 3:
        return "weather-overcast"
    if c in (45, 48):
        return "weather-fog"
    if c in (51, 53, 55, 56, 57, 61, 80):
        return "weather-showers-scattered"
    if c in (63, 65, 66, 67, 81, 82):
        return "weather-showers"
    if c in (71, 73, 75, 77, 85, 86):
        return "weather-snow"
    if c in (95, 96, 99):
        return "weather-storm"
    return "weather-few-clouds"


def draw_raindrop(ctx, cx, cy, size, shadow=True):
    """Draw a crisp vector teardrop at center (cx, cy) with height `size` (no emoji font needed)."""
    h = float(size)
    w = h * 0.68
    top_y = cy - h * 0.50
    bot_y = cy + h * 0.50
    mid_y = cy + h * 0.14
    ctx.new_sub_path()
    ctx.move_to(cx, top_y)
    ctx.curve_to(cx + w * 0.55, mid_y - h * 0.12, cx + w * 0.55, bot_y, cx, bot_y)
    ctx.curve_to(cx - w * 0.55, bot_y, cx - w * 0.55, mid_y - h * 0.12, cx, top_y)
    ctx.close_path()
    if shadow:
        ctx.save()
        ctx.set_source_rgba(0.0, 0.0, 0.0, 0.65)
        ctx.set_line_width(1.4)
        ctx.stroke_preserve()
        ctx.restore()
    ctx.set_source_rgba(0.44, 0.85, 1.0, 0.96)
    ctx.fill()


def draw_rounded_rect(ctx, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


class MiniDesktopIconWindow(Gtk.Window):
    """Compact 58x58 RGBA desktop icon shown when the main calendar is minimized."""

    ICON_SIZE = 58

    def __init__(self, parent_cal):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.parent_cal = parent_cal
        self.set_title("Taskwarrior Desktop Icon")
        self.set_decorated(False)
        self.set_keep_below(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.stick()
        self.set_default_size(self.ICON_SIZE, self.ICON_SIZE)
        self.set_size_request(self.ICON_SIZE, self.ICON_SIZE)

        screen = self.get_screen()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None and screen.is_composited():
                self.set_visual(visual)
        self.set_app_paintable(True)

        self._press_root = None
        self._win_origin = None
        self._dragging = False

        self.canvas = Gtk.DrawingArea()
        self.canvas.set_size_request(self.ICON_SIZE, self.ICON_SIZE)
        self.canvas.set_tooltip_text("點擊還原 Taskwarrior 桌面月曆（按住拖曳可移動，右鍵開啟選單）")
        self.canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.canvas.connect("draw", self._on_draw)
        self.canvas.connect("button-press-event", self._on_press)
        self.canvas.connect("motion-notify-event", self._on_motion)
        self.canvas.connect("button-release-event", self._on_release)
        self.add(self.canvas)

    def _assert_desktop_layer(self):
        self.set_keep_below(True)
        self.stick()
        return False

    def _on_press(self, _w, event):
        if event.button == 1:
            self._press_root = (float(event.x_root), float(event.y_root))
            self._win_origin = self.get_position()
            self._dragging = False
            return True
        if event.button == 3:
            menu = Gtk.Menu()
            items = [
                ("📅 還原完整桌面月曆", self.parent_cal.restore_from_mini),
                ("＋ 新增任務...", lambda *_: self.parent_cal._open_add_dialog(date.today())),
                (None, None),
                ("✕ 關閉桌面月曆", lambda *_: Gtk.main_quit()),
            ]
            for lbl, cb in items:
                if lbl is None:
                    menu.append(Gtk.SeparatorMenuItem())
                else:
                    mi = Gtk.MenuItem(label=lbl)
                    mi.connect("activate", cb)
                    menu.append(mi)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def _on_motion(self, _w, event):
        if self._press_root is None or self._win_origin is None:
            return False
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        dx = float(event.x_root) - self._press_root[0]
        dy = float(event.y_root) - self._press_root[1]
        if not self._dragging and math.hypot(dx, dy) > 5.0:
            self._dragging = True
        if self._dragging:
            self.move(int(round(self._win_origin[0] + dx)), int(round(self._win_origin[1] + dy)))
        return True

    def _on_release(self, _w, event):
        if event.button != 1 or self._press_root is None:
            return False
        was_drag = self._dragging
        self._press_root = None
        self._win_origin = None
        self._dragging = False
        if was_drag:
            px, py = self.get_position()
            self.parent_cal.config["mini_x"] = int(px)
            self.parent_cal.config["mini_y"] = int(py)
            self.parent_cal._save_config()
        else:
            self.parent_cal.restore_from_mini()
        return True

    def _on_draw(self, _widget, ctx):
        w = float(self.ICON_SIZE)
        h = float(self.ICON_SIZE)
        ctx.save()
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        # Card geometry (leaves space for subtle shadow & top-right pending badge)
        cx, cy, cw, ch = 4.0, 6.0, w - 10.0, h - 10.0
        hdr_h = 15.0

        # Subtle drop shadow
        draw_rounded_rect(ctx, cx + 1.0, cy + 2.0, cw, ch, 8.0)
        ctx.set_source_rgba(0.0, 0.0, 0.0, 0.35)
        ctx.fill()

        # Calendar card body
        draw_rounded_rect(ctx, cx, cy, cw, ch, 8.0)
        ctx.set_source_rgba(0.11, 0.16, 0.24, 0.94)
        ctx.fill()

        # Calendar top red header strip (clipped to card rounded rect)
        ctx.save()
        draw_rounded_rect(ctx, cx, cy, cw, ch, 8.0)
        ctx.clip()
        ctx.rectangle(cx, cy, cw, hdr_h)
        ctx.set_source_rgba(0.86, 0.15, 0.15, 0.98)
        ctx.fill()
        ctx.restore()

        # Crisp card border using configured today_border_color
        bd_rgb = hex_to_rgb(self.parent_cal.config.get("today_border_color", "#38e0a6"), default=(0.22, 0.88, 0.65))
        draw_rounded_rect(ctx, cx, cy, cw, ch, 8.0)
        ctx.set_source_rgba(bd_rgb[0], bd_rgb[1], bd_rgb[2], 0.92)
        ctx.set_line_width(1.3)
        ctx.stroke()

        # Header text: month
        today = date.today()
        m_txt = f"{today.month}月"
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(9.5)
        m_ext = ctx.text_extents(m_txt)
        ctx.set_source_rgba(1.0, 1.0, 1.0, 0.98)
        ctx.move_to(cx + (cw - m_ext.width) / 2.0 - m_ext.x_bearing, cy + (hdr_h - m_ext.height) / 2.0 - m_ext.y_bearing)
        ctx.show_text(m_txt)

        # Body text: big today day number
        d_txt = str(today.day)
        ctx.set_font_size(20.0)
        d_ext = ctx.text_extents(d_txt)
        body_y = cy + hdr_h
        body_h = ch - hdr_h
        ctx.set_source_rgba(0.96, 0.98, 1.0, 0.98)
        ctx.move_to(cx + (cw - d_ext.width) / 2.0 - d_ext.x_bearing, body_y + (body_h - d_ext.height) / 2.0 - d_ext.y_bearing)
        ctx.show_text(d_txt)

        # Top-right pending count badge
        n_pending = len(self.parent_cal.pending_tasks)
        if n_pending > 0:
            b_txt = "99+" if n_pending > 99 else str(n_pending)
            ctx.set_font_size(8.5)
            b_ext = ctx.text_extents(b_txt)
            bw = max(16.0, b_ext.width + 7.0)
            bh = 15.0
            bx = w - bw - 1.0
            by = 1.0
            draw_rounded_rect(ctx, bx, by, bw, bh, bh / 2.0)
            ctx.set_source_rgba(0.06, 0.56, 0.88, 0.98)
            ctx.fill_preserve()
            ctx.set_source_rgba(1.0, 1.0, 1.0, 0.90)
            ctx.set_line_width(1.0)
            ctx.stroke()
            ctx.set_source_rgba(1.0, 1.0, 1.0, 1.0)
            ctx.move_to(bx + (bw - b_ext.width) / 2.0 - b_ext.x_bearing, by + (bh - b_ext.height) / 2.0 - b_ext.y_bearing)
            ctx.show_text(b_txt)

        ctx.restore()
        return False


class TaskwarriorDesktopCalendar(Gtk.Window):
    """Single-pane full monthly Taskwarrior calendar embedded on the MATE desktop."""

    WEEKDAYS_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Taskwarrior Desktop Calendar")
        self.set_decorated(False)
        self.set_keep_below(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(True)
        self.stick()

        self.config = self._load_config()
        self.task_colors = self._load_task_colors()
        self.weather_data = self._load_weather_cache()
        self.icon_theme = Gtk.IconTheme.get_default()
        self._icon_cache = {}
        self.prefs_win = None

        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.cell_dates = []
        self._cell_rects = []       # list of (x, y, w, h, date_obj)
        self._task_hit_rects = []   # list of (x, y, w, h, task_dict, is_completed)

        self.pending_tasks = []
        self.completed_tasks = []
        self._by_date_pending = {}
        self._by_date_completed = {}
        self._unscheduled = []

        self._file_monitor = None
        self._monitor_debounce_id = None
        self._config_monitor = None
        self._config_debounce_id = None
        self._geom_save_id = None
        self._is_minimized = False
        self.mini_win = None

        # Enable RGBA transparency
        screen = self.get_screen()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None and screen.is_composited():
                self.set_visual(visual)
        self.set_app_paintable(True)

        self.connect("draw", self._on_draw_bg)
        self.connect("configure-event", self._on_configure)
        self.connect("destroy", self._on_destroy)

        self._build_ui()

        w = max(520, int(self.config.get("width", 940)))
        h = max(380, int(self.config.get("height", 620)))
        x = int(self.config.get("x", 80))
        y = int(self.config.get("y", 60))
        self.set_default_size(w, h)
        self.resize(w, h)
        self.move(x, y)

        self._start_file_monitor()
        self._start_config_monitor()
        self.reload_tasks()
        self.show_all()
        # Re-assert desktop layer hints after map (Compiz friendly)
        GLib.idle_add(self._assert_desktop_layer)

    def _assert_desktop_layer(self):
        self.set_keep_below(True)
        self.stick()
        return False

    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception:
                pass
        return cfg

    def _save_config(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_task_colors(self):
        if os.path.exists(TASK_COLORS_PATH):
            try:
                with open(TASK_COLORS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.startswith("#")}
            except Exception:
                pass
        return {}

    def _save_task_colors(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(TASK_COLORS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.task_colors, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_weather_cache(self):
        if os.path.exists(WEATHER_CACHE_PATH):
            try:
                with open(WEATHER_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _get_weather_pixbuf(self, icon_name, size):
        key = (icon_name, int(size))
        if key in self._icon_cache:
            return self._icon_cache[key]
        pb = None
        if self.icon_theme is not None:
            for name in (icon_name, "weather-few-clouds", "weather-overcast"):
                try:
                    pb = self.icon_theme.load_icon(name, int(size), Gtk.IconLookupFlags.FORCE_SIZE)
                    if pb is not None:
                        break
                except Exception:
                    pb = None
        self._icon_cache[key] = pb
        return pb

    def _on_configure(self, _w, event):
        if self._is_minimized or not self.get_visible():
            return False
        if self._geom_save_id is not None:
            try:
                GLib.source_remove(self._geom_save_id)
            except Exception:
                pass

        def _commit():
            self._geom_save_id = None
            if self._is_minimized or not self.get_visible():
                return False
            px, py = self.get_position()
            pw, ph = self.get_size()
            self.config["x"] = int(px)
            self.config["y"] = int(py)
            self.config["width"] = int(pw)
            self.config["height"] = int(ph)
            self._save_config()
            return False

        self._geom_save_id = GLib.timeout_add(400, _commit)
        return False

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(12)
        root.set_margin_end(12)

        # Top header bar (draggable when unlocked)
        self.hdr_ev = Gtk.EventBox()
        self.hdr_ev.set_visible_window(False)
        self.hdr_ev.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.hdr_ev.connect("button-press-event", self._on_header_button_press)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        def _make_btn(txt, tip):
            b = Gtk.Button()
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            lb = Gtk.Label()
            b.add(lb)
            b._inner_lbl = lb
            b._raw_txt = txt
            return b

        self.btn_prev = _make_btn("◀", "上個月")
        self.btn_today = _make_btn("今天", "回到本月")
        self.btn_next = _make_btn("▶", "下個月")
        self.btn_prev.connect("clicked", lambda *_: self._shift_month(-1))
        self.btn_today.connect("clicked", lambda *_: self._jump_today())
        self.btn_next.connect("clicked", lambda *_: self._shift_month(1))

        self.month_lbl = Gtk.Label(xalign=0.0)
        self.month_lbl.set_margin_start(8)

        self.btn_unsched = _make_btn("📌 未排期 (0)", "檢視未設定到期日的待辦任務")
        self.btn_unsched.connect("clicked", self._open_unscheduled_popover)

        self.btn_add = _make_btn("＋ 新增", "新增任務")
        self.btn_add.connect("clicked", lambda *_: self._open_add_dialog(date.today()))

        self.btn_undo = _make_btn("↩", "復原上一步 (task undo)")
        self.btn_undo.connect("clicked", self._on_undo)

        self.btn_lock = _make_btn("🔒" if self.config.get("locked") else "🔓", "切換鎖定位置 / 解鎖拖曳移動")
        self.btn_lock.connect("clicked", self._on_toggle_lock)

        self.btn_prefs = _make_btn("⚙", "外觀與位置設定")
        self.btn_prefs.connect("clicked", self.show_preferences_dialog)

        self.btn_mini = _make_btn("—", "縮小為桌面小圖示（方便操作桌面檔案）")
        self.btn_mini.connect("clicked", self.minimize_to_icon)

        self._nav_buttons = [
            self.btn_prev, self.btn_today, self.btn_next,
            self.btn_unsched, self.btn_add, self.btn_undo, self.btn_lock, self.btn_prefs, self.btn_mini,
        ]

        nav.pack_start(self.btn_prev, False, False, 0)
        nav.pack_start(self.btn_today, False, False, 0)
        nav.pack_start(self.btn_next, False, False, 0)
        nav.pack_start(self.month_lbl, True, True, 0)
        nav.pack_end(self.btn_mini, False, False, 0)
        nav.pack_end(self.btn_prefs, False, False, 0)
        nav.pack_end(self.btn_lock, False, False, 0)
        nav.pack_end(self.btn_undo, False, False, 0)
        nav.pack_end(self.btn_add, False, False, 0)
        nav.pack_end(self.btn_unsched, False, False, 0)

        self.hdr_ev.add(nav)
        root.pack_start(self.hdr_ev, False, False, 0)

        # Main 7x6 Full Calendar Canvas
        self.cal_canvas = Gtk.DrawingArea()
        self.cal_canvas.set_hexpand(True)
        self.cal_canvas.set_vexpand(True)
        self.cal_canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.cal_canvas.connect("draw", self._on_draw_calendar)
        self.cal_canvas.connect("button-press-event", self._on_canvas_click)
        root.pack_start(self.cal_canvas, True, True, 0)

        self.add(root)
        self._style_nav_labels()

    def _style_nav_labels(self):
        fg = html.escape(self.config.get("fg_color", "#f1f5f9"))
        locked = bool(self.config.get("locked", False))
        self.btn_lock._raw_txt = "🔒" if locked else "🔓"
        self.btn_unsched._raw_txt = f"📌 未排期 ({len(self._unscheduled)})"
        for b in self._nav_buttons:
            b._inner_lbl.set_markup(f"<span foreground='{fg}'><b>{html.escape(b._raw_txt)}</b></span>")
        self.btn_unsched.set_visible(len(self._unscheduled) > 0)

    def _on_header_button_press(self, _w, event):
        if event.button == 1 and not self.config.get("locked", False):
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        if event.button == 3:
            self._show_context_menu(event)
            return True
        return False

    def _on_toggle_lock(self, *_args):
        self.config["locked"] = not bool(self.config.get("locked", False))
        self._save_config()
        self._style_nav_labels()
        self.cal_canvas.queue_draw()

    # =========================================================================
    # Live Sync with ~/.task via Gio.FileMonitor (0.0% idle CPU)
    # =========================================================================

    def _start_file_monitor(self):
        task_dir = os.path.expanduser("~/.task")
        if not os.path.isdir(task_dir):
            return
        try:
            gfile = Gio.File.new_for_path(task_dir)
            mon = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", self._on_task_dir_changed)
            self._file_monitor = mon
        except Exception:
            self._file_monitor = None

    def _on_task_dir_changed(self, _mon, _f, _other, _ev_type):
        if self._monitor_debounce_id is not None:
            try:
                GLib.source_remove(self._monitor_debounce_id)
            except Exception:
                pass
        self._monitor_debounce_id = GLib.timeout_add(250, self._on_debounced_reload)

    def _on_debounced_reload(self):
        self._monitor_debounce_id = None
        self.reload_tasks()
        return False

    def _start_config_monitor(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            gfile = Gio.File.new_for_path(CONFIG_DIR)
            mon = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", self._on_config_dir_changed)
            self._config_monitor = mon
        except Exception:
            self._config_monitor = None

    def _on_config_dir_changed(self, _mon, f, _other, _ev_type):
        bname = f.get_basename() if f is not None else ""
        if bname not in ("weather-cache.json", "task-colors.json"):
            return
        if self._config_debounce_id is not None:
            try:
                GLib.source_remove(self._config_debounce_id)
            except Exception:
                pass
        self._config_debounce_id = GLib.timeout_add(250, self._on_debounced_config_reload)

    def _on_debounced_config_reload(self):
        self._config_debounce_id = None
        self.weather_data = self._load_weather_cache()
        self.task_colors = self._load_task_colors()
        self.cal_canvas.queue_draw()
        return False

    def reload_tasks(self):
        if not TaskwarriorBackend.is_available():
            self.pending_tasks = []
            self.completed_tasks = []
            self._by_date_pending = {}
            self._by_date_completed = {}
            self._unscheduled = []
            self._refresh_view()
            return

        due_soon = int(self.config.get("due_soon_days", 3))
        data = TaskwarriorBackend.fetch_all(due_soon_days=due_soon, completed_limit=80)
        self.pending_tasks = data.get("pending", [])
        self.completed_tasks = data.get("all_completed", [])

        by_p = {}
        unsched = []
        for t in self.pending_tasks:
            d_str = t.get("_due_date") or ""
            if d_str:
                by_p.setdefault(d_str, []).append(t)
            else:
                unsched.append(t)

        by_c = {}
        for t in self.completed_tasks:
            d_str = t.get("_end_date") or t.get("_due_date") or ""
            if d_str:
                by_c.setdefault(d_str, []).append(t)

        self._by_date_pending = by_p
        self._by_date_completed = by_c
        self._unscheduled = unsched
        self._refresh_view()

    def _compute_month_cells(self):
        cal = calendar.Calendar(firstweekday=0)
        days = list(cal.itermonthdates(self.view_year, self.view_month))
        while len(days) < 42:
            days.append(days[-1] + timedelta(days=1))
        self.cell_dates = days[:42]

    def _shift_month(self, delta):
        m = self.view_month + delta
        y = self.view_year
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        self.view_year = y
        self.view_month = m
        self._refresh_view()

    def _jump_today(self):
        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self._refresh_view()

    def _refresh_view(self):
        self._compute_month_cells()
        fg = html.escape(self.config.get("fg_color", "#f1f5f9"))
        fs = max(9, min(18, int(self.config.get("font_size", 11))))
        t_pt = int((fs + 3.5) * 1024)
        s_pt = int(max(8.5, fs - 0.5) * 1024)
        self.month_lbl.set_markup(
            f"<span foreground='{fg}' size='{t_pt}'><b>{self.view_year} 年 {self.view_month:02d} 月</b></span>   "
            f"<span foreground='{fg}' alpha='75%' size='{s_pt}'>待辦 {len(self.pending_tasks)} 筆</span>"
        )
        self._style_nav_labels()
        self.queue_draw()
        self.cal_canvas.queue_draw()
        if self.mini_win is not None and self.mini_win.get_visible():
            self.mini_win.canvas.queue_draw()

    def minimize_to_icon(self, *_args):
        if self._is_minimized:
            return
        px, py = self.get_position()
        pw, ph = self.get_size()
        self.config["x"] = int(px)
        self.config["y"] = int(py)
        self.config["width"] = int(pw)
        self.config["height"] = int(ph)
        self._save_config()

        self._is_minimized = True
        if self.mini_win is None:
            self.mini_win = MiniDesktopIconWindow(self)

        mx = self.config.get("mini_x")
        my = self.config.get("mini_y")
        if mx is None or my is None:
            mx = max(0, int(px) + int(pw) - MiniDesktopIconWindow.ICON_SIZE)
            my = max(0, int(py))

        self.hide()
        self.mini_win.move(int(mx), int(my))
        self.mini_win.show_all()
        self.mini_win.canvas.queue_draw()
        GLib.idle_add(self.mini_win._assert_desktop_layer)

    def restore_from_mini(self, *_args):
        if not self._is_minimized:
            return
        if self.mini_win is not None:
            self.mini_win.hide()
        w = max(520, int(self.config.get("width", 940)))
        h = max(380, int(self.config.get("height", 620)))
        x = int(self.config.get("x", 80))
        y = int(self.config.get("y", 60))
        self._is_minimized = False
        self.resize(w, h)
        self.move(x, y)
        self.show_all()
        self._refresh_view()
        GLib.idle_add(self._assert_desktop_layer)

    # =========================================================================
    # Cairo RGBA Drawing (Background + Full 7x6 Calendar with Inline Task Pills)
    # =========================================================================

    def _on_draw_bg(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)
        ctx.save()
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        bg_rgb = hex_to_rgb(self.config.get("bg_color", "#0f172a"))
        bg_a = max(0.0, min(1.0, float(self.config.get("bg_opacity", 58)) / 100.0))
        if bg_a > 0.0:
            draw_rounded_rect(ctx, 0.0, 0.0, max(2.0, w), max(2.0, h), 12.0)
            ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], bg_a)
            ctx.fill()

        # Subtle bottom-right resize handle indicator when unlocked
        if not self.config.get("locked", False):
            fg_rgb = hex_to_rgb(self.config.get("fg_color", "#f1f5f9"), default=(0.95, 0.96, 0.98))
            ctx.set_source_rgba(fg_rgb[0], fg_rgb[1], fg_rgb[2], 0.45)
            ctx.set_line_width(1.3)
            for off in (5.0, 9.0, 13.0):
                ctx.move_to(w - off, h - 3.0)
                ctx.line_to(w - 3.0, h - off)
            ctx.stroke()

        ctx.restore()
        return False

    def _draw_text(self, ctx, x, y, text, rgb, alpha=1.0):
        if self.config.get("text_shadow", True):
            ctx.set_source_rgba(0.0, 0.0, 0.0, 0.72 * alpha)
            ctx.move_to(x + 0.8, y + 0.8)
            ctx.show_text(text)
        ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], alpha)
        ctx.move_to(x, y)
        ctx.show_text(text)

    def _truncate_text(self, ctx, text, max_w):
        if ctx.text_extents(text).width <= max_w:
            return text
        lo, hi = 0, len(text)
        best = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = text[:mid] + "…"
            if ctx.text_extents(cand).width <= max_w:
                best = cand
                lo = mid + 1
            else:
                hi = mid - 1
        return best or "…"

    def _on_draw_calendar(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)
        fg_rgb = hex_to_rgb(self.config.get("fg_color", "#f1f5f9"), default=(0.95, 0.96, 0.98))
        today_bg_rgb = hex_to_rgb(self.config.get("today_bg_color", "#1a5c4d"), default=(0.10, 0.36, 0.30))
        today_bd_rgb = hex_to_rgb(self.config.get("today_border_color", "#38e0a6"), default=(0.22, 0.88, 0.65))
        cur_m_bg_rgb = hex_to_rgb(self.config.get("cur_month_bg_color", "#141f2e"), default=(0.08, 0.12, 0.18))
        other_m_bg_rgb = hex_to_rgb(self.config.get("other_month_bg_color", "#0d141f"), default=(0.05, 0.08, 0.12))
        cell_a = max(0.0, min(1.0, float(self.config.get("cell_opacity", 42)) / 100.0))
        base_fs = float(max(9, min(18, int(self.config.get("font_size", 11)))))
        show_comp = bool(self.config.get("show_completed", True))
        show_weather = bool(self.config.get("show_weather", True))
        use_shadow = bool(self.config.get("text_shadow", True))
        daily_map = (self.weather_data or {}).get("daily_by_date") or {}
        today_aqi_rgb = aqi_to_rgb(((self.weather_data or {}).get("current") or {}).get("us_aqi"))

        header_h = max(24.0, base_fs * 2.1)
        grid_x = 2.0
        grid_y = header_h + 2.0
        col_w = (w - 4.0) / 7.0
        row_h = max(44.0, (h - grid_y - 2.0) / 6.0)

        # Weekday header row
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(base_fs)
        for ci, wd in enumerate(self.WEEKDAYS_ZH):
            cx = grid_x + ci * col_w
            ext = ctx.text_extents(wd)
            rgb = (0.98, 0.56, 0.48) if ci >= 5 else fg_rgb
            self._draw_text(ctx, cx + (col_w - ext.width) / 2.0 - ext.x_bearing, header_h * 0.72, wd, rgb, 0.92)

        today = date.today()
        if not self.cell_dates:
            self._compute_month_cells()

        self._cell_rects = []
        self._task_hit_rects = []

        pill_fs = max(8.5, base_fs - 1.2)
        pill_h = max(14.0, pill_fs + 5.0)
        pill_gap = 2.5

        for idx, d_obj in enumerate(self.cell_dates):
            r_idx = idx // 7
            c_idx = idx % 7
            cx = grid_x + c_idx * col_w + 2.0
            cy = grid_y + r_idx * row_h + 2.0
            cw = col_w - 4.0
            ch = row_h - 4.0
            self._cell_rects.append((cx, cy, cw, ch, d_obj))

            d_str = d_obj.strftime("%Y-%m-%d")
            is_cur_m = d_obj.month == self.view_month
            is_today = d_obj == today
            p_list = self._by_date_pending.get(d_str, [])
            c_list = self._by_date_completed.get(d_str, []) if show_comp else []

            # Date cell background
            draw_rounded_rect(ctx, cx, cy, cw, ch, 6.0)
            if is_today:
                ctx.set_source_rgba(today_bg_rgb[0], today_bg_rgb[1], today_bg_rgb[2], max(0.28, cell_a * 0.95))
            elif is_cur_m:
                ctx.set_source_rgba(cur_m_bg_rgb[0], cur_m_bg_rgb[1], cur_m_bg_rgb[2], cell_a)
            else:
                ctx.set_source_rgba(other_m_bg_rgb[0], other_m_bg_rgb[1], other_m_bg_rgb[2], cell_a * 0.45)
            ctx.fill_preserve()

            if is_today:
                ctx.set_source_rgba(today_bd_rgb[0], today_bd_rgb[1], today_bd_rgb[2], 0.95)
                ctx.set_line_width(1.6)
            else:
                ctx.set_source_rgba(fg_rgb[0], fg_rgb[1], fg_rgb[2], 0.16 if is_cur_m else 0.07)
                ctx.set_line_width(0.8)
            ctx.stroke()

            # Top row inside cell: Day number (left) + 7-day weather (middle-right) + count badge (far right)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(base_fs)
            if is_today:
                d_rgb, d_a = (0.35, 0.98, 0.75), 1.0
            elif not is_cur_m:
                d_rgb, d_a = fg_rgb, 0.38
            elif c_idx >= 5:
                d_rgb, d_a = (0.98, 0.60, 0.54), 0.96
            else:
                d_rgb, d_a = fg_rgb, 0.95
            day_txt = str(d_obj.day)
            day_ext = ctx.text_extents(day_txt)
            self._draw_text(ctx, cx + 5.0, cy + base_fs + 2.5, day_txt, d_rgb, d_a)
            left_end = cx + 5.0 + max(day_ext.width, day_ext.x_advance) + 4.0

            right_limit = cx + cw - 4.0
            total_items = len(p_list) + len(c_list)
            if total_items > 0:
                cnt_s = str(len(p_list)) if p_list else f"✓{len(c_list)}"
                ctx.set_font_size(max(8.0, base_fs - 2.0))
                cext = ctx.text_extents(cnt_s)
                c_rgb = (0.38, 0.80, 1.0) if p_list else (0.30, 0.84, 0.58)
                cnt_x = cx + cw - cext.width - 5.0 - cext.x_bearing
                self._draw_text(ctx, cnt_x, cy + base_fs + 2.0, cnt_s, c_rgb, 0.90)
                right_limit = cnt_x - 4.0

            # 7-day weather info beside day number (shared cache, adaptive to cell width)
            if show_weather:
                day_fc = daily_map.get(d_str)
                if isinstance(day_fc, dict):
                    avail_w = right_limit - left_end
                    ico_sz = max(13, min(17, int(round(base_fs + 3.0))))
                    if avail_w >= ico_sz:
                        w_fs = max(7.5, base_fs - 2.5)
                        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
                        ctx.set_font_size(w_fs)

                        tmax = day_fc.get("t_max")
                        tmin = day_fc.get("t_min")
                        temp_s = ""
                        if tmax is not None and tmin is not None:
                            temp_s = f"{int(round(float(tmax)))}°/{int(round(float(tmin)))}°"
                        elif tmax is not None:
                            temp_s = f"{int(round(float(tmax)))}°"
                        temp_ext = ctx.text_extents(temp_s) if temp_s else None
                        temp_w = (temp_ext.width + 2.0) if temp_ext else 0.0

                        pop = day_fc.get("precip_prob")
                        pop_s = f"{int(round(float(pop)))}%" if pop is not None else ""
                        pop_ext = ctx.text_extents(pop_s) if pop_s else None
                        drop_sz = max(7.0, w_fs * 0.95)
                        drop_w = drop_sz * 0.72
                        rain_w = (drop_w + 1.5 + pop_ext.width + 2.5) if pop_ext else 0.0

                        ico_name = wmo_to_icon_name(day_fc.get("code"))
                        pb = self._get_weather_pixbuf(ico_name, ico_sz)
                        has_ico = pb is not None
                        ico_w = float(ico_sz) if has_ico else 0.0

                        has_aqi = is_today and (today_aqi_rgb is not None)
                        aqi_r = max(3.2, min(4.6, base_fs * 0.34))
                        aqi_w = (aqi_r * 2.0 + 3.0) if has_aqi else 0.0

                        # Decide what fits cleanly: full (icon+temp+rain[+aqi]) -> icon+temp[+aqi] -> icon[+aqi]
                        draw_rain = False
                        draw_temp = False
                        draw_aqi = False
                        full_w = ico_w + (2.0 if has_ico and temp_s else 0.0) + temp_w + (2.0 if (has_ico or temp_s) and pop_s else 0.0) + rain_w
                        mid_w = ico_w + (2.0 if has_ico and temp_s else 0.0) + temp_w
                        if pop_s and temp_s and (full_w + aqi_w) <= avail_w:
                            draw_temp = True
                            draw_rain = True
                            draw_aqi = has_aqi
                            block_w = full_w + aqi_w
                        elif pop_s and temp_s and full_w <= avail_w:
                            draw_temp = True
                            draw_rain = True
                            block_w = full_w
                        elif temp_s and (mid_w + aqi_w) <= avail_w:
                            draw_temp = True
                            draw_aqi = has_aqi
                            block_w = mid_w + aqi_w
                        elif temp_s and mid_w <= avail_w:
                            draw_temp = True
                            block_w = mid_w
                        elif has_ico and (ico_w + aqi_w) <= avail_w:
                            draw_aqi = has_aqi
                            block_w = ico_w + aqi_w
                        elif has_ico and ico_w <= avail_w:
                            block_w = ico_w
                        else:
                            block_w = 0.0

                        if block_w > 0.0:
                            ideal_x = cx + (cw - block_w) / 2.0
                            wx = max(left_end, min(right_limit - block_w, ideal_x))
                            w_alpha = 0.94 if is_cur_m else 0.45
                            top_mid_y = cy + (base_fs + 5.0) / 2.0
                            text_base_y = cy + base_fs + 2.0

                            if has_ico:
                                ctx.save()
                                Gdk.cairo_set_source_pixbuf(ctx, pb, wx, max(cy + 1.0, top_mid_y - ico_sz / 2.0))
                                ctx.paint_with_alpha(w_alpha)
                                ctx.restore()
                                wx += ico_w + 2.0

                            if draw_temp and temp_s:
                                t_rgb = (0.98, 0.86, 0.54) if is_today else fg_rgb
                                self._draw_text(ctx, wx - temp_ext.x_bearing, text_base_y, temp_s, t_rgb, w_alpha * 0.92)
                                wx += temp_w + 2.0

                            if draw_rain and pop_s:
                                ctx.save()
                                if not is_cur_m:
                                    ctx.push_group()
                                draw_raindrop(ctx, wx + drop_w * 0.5, top_mid_y + 0.5, drop_sz, shadow=use_shadow)
                                if not is_cur_m:
                                    ctx.pop_group_to_source()
                                    ctx.paint_with_alpha(0.45)
                                ctx.restore()
                                wx += drop_w + 1.5
                                self._draw_text(ctx, wx - pop_ext.x_bearing, text_base_y, pop_s, (0.50, 0.88, 1.0), w_alpha * 0.95)
                                wx += pop_ext.width + 2.5

                            if draw_aqi and today_aqi_rgb is not None:
                                ctx.save()
                                ctx.arc(wx + aqi_r, top_mid_y + 0.5, aqi_r, 0, 2 * math.pi)
                                if use_shadow:
                                    ctx.set_source_rgba(0.0, 0.0, 0.0, 0.65)
                                    ctx.set_line_width(1.2)
                                    ctx.stroke_preserve()
                                ctx.set_source_rgba(today_aqi_rgb[0], today_aqi_rgb[1], today_aqi_rgb[2], 0.98)
                                ctx.fill()
                                ctx.restore()

            # Inline Task Pills directly inside the date cell!
            combined = [(t, False) for t in p_list] + [(t, True) for t in c_list]
            if not combined:
                continue

            cur_y = cy + base_fs + 7.0
            avail_h = (cy + ch - 3.0) - cur_y
            if avail_h < pill_h:
                continue

            max_slots = max(1, int((avail_h + pill_gap) // (pill_h + pill_gap)))
            has_overflow = len(combined) > max_slots
            draw_slots = max(1, max_slots - 1) if (has_overflow and max_slots >= 2) else max_slots

            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            ctx.set_font_size(pill_fs)

            for t, is_comp in combined[:draw_slots]:
                px = cx + 3.5
                pw = cw - 7.0
                custom_hex = self.task_colors.get(t.get("uuid", ""))
                txt_rgb = (1.0, 1.0, 1.0)
                if custom_hex:
                    cr, cg, cb = hex_to_rgb(custom_hex, default=(0.09, 0.45, 0.72))
                    p_bg = (cr, cg, cb, 0.68 if is_comp else 0.90)
                    if rgb_luminance((cr, cg, cb)) > 0.62:
                        txt_rgb = (0.06, 0.08, 0.12)
                    if is_comp:
                        label_txt = "✓ " + str(t.get("description", ""))
                    else:
                        pri = t.get("priority", "")
                        pri_pfx = f"[{pri}] " if pri else ""
                        label_txt = pri_pfx + str(t.get("description", ""))
                elif is_comp:
                    p_bg = (0.06, 0.44, 0.32, 0.72)
                    label_txt = "✓ " + str(t.get("description", ""))
                else:
                    due_lvl = t.get("_due_level", "normal")
                    pri = t.get("priority", "")
                    if due_lvl == "crit" or pri == "H":
                        p_bg = (0.78, 0.16, 0.20, 0.88)
                    elif due_lvl == "warn" or pri == "M":
                        p_bg = (0.80, 0.48, 0.08, 0.88)
                    else:
                        p_bg = (0.09, 0.45, 0.72, 0.86)
                    pri_pfx = f"[{pri}] " if pri else ""
                    label_txt = pri_pfx + str(t.get("description", ""))

                draw_rounded_rect(ctx, px, cur_y, pw, pill_h, 3.5)
                ctx.set_source_rgba(*p_bg)
                ctx.fill()

                disp = self._truncate_text(ctx, label_txt, max(12.0, pw - 8.0))
                ext = ctx.text_extents(disp)
                ty = cur_y + (pill_h - ext.height) / 2.0 - ext.y_bearing
                ctx.set_source_rgba(txt_rgb[0], txt_rgb[1], txt_rgb[2], 0.80 if is_comp else 0.98)
                ctx.move_to(px + 4.0 - ext.x_bearing, ty)
                ctx.show_text(disp)

                self._task_hit_rects.append((px, cur_y, pw, pill_h, t, is_comp))
                cur_y += pill_h + pill_gap

            if has_overflow:
                rem = len(combined) - draw_slots
                more_s = f"+{rem} 更多..."
                ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                ctx.set_font_size(max(8.0, pill_fs - 0.5))
                m_ext = ctx.text_extents(more_s)
                self._draw_text(
                    ctx,
                    cx + 5.0 - m_ext.x_bearing,
                    min(cy + ch - 3.5, cur_y + m_ext.height + 1.0),
                    more_s,
                    (0.55, 0.85, 1.0),
                    0.95,
                )
        return False

    # =========================================================================
    # Mouse Interactions (Click Task -> Edit, Click Cell -> Day Popover, Right-click -> Menu)
    # =========================================================================

    def _on_canvas_click(self, widget, event):
        alloc = widget.get_allocation()
        # Bottom-right resize grip when unlocked
        if (
            event.button == 1
            and not self.config.get("locked", False)
            and event.x >= alloc.width - 18
            and event.y >= alloc.height - 18
        ):
            self.begin_resize_drag(Gdk.WindowEdge.SOUTH_EAST, event.button, int(event.x_root), int(event.y_root), event.time)
            return True

        if event.button == 3:
            self._show_context_menu(event)
            return True

        if event.button != 1:
            return False

        # 1. Check if user clicked directly on a task pill
        for rx, ry, rw, rh, task, is_comp in self._task_hit_rects:
            if rx <= event.x <= rx + rw and ry <= event.y <= ry + rh:
                self._open_edit_dialog(task, is_completed=is_comp)
                return True

        # 2. Otherwise check which date cell was clicked -> open Day Popover
        for cx, cy, cw, ch, d_obj in self._cell_rects:
            if cx <= event.x <= cx + cw and cy <= event.y <= cy + ch:
                self._open_day_popover(d_obj, int(cx + cw / 2), int(cy + ch / 2))
                return True
        return False

    def _open_day_popover(self, d_obj, px, py):
        pop = Gtk.Popover.new(self.cal_canvas)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = px, py, 2, 2
        pop.set_pointing_to(rect)
        pop.set_position(Gtk.PositionType.BOTTOM)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(310, -1)

        d_str = d_obj.strftime("%Y-%m-%d")
        wd = self.WEEKDAYS_ZH[d_obj.weekday()]
        hdr = Gtk.Label(xalign=0.0)
        hdr.set_markup(f"<b>📅 {d_obj.year}/{d_obj.month:02d}/{d_obj.day:02d} {wd}</b>")
        box.pack_start(hdr, False, False, 0)

        # Quick add entry right inside the popover
        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        e_add = Gtk.Entry()
        e_add.set_hexpand(True)
        e_add.set_placeholder_text("新增此日任務 (Enter)...")
        btn_add = Gtk.Button(label="＋")

        def _do_add(*_):
            txt = e_add.get_text().strip()
            if not txt:
                return
            TaskwarriorBackend.add_task_structured(description=txt, due=d_str)
            pop.popdown()
            self.reload_tasks()

        e_add.connect("activate", _do_add)
        btn_add.connect("clicked", _do_add)
        add_row.pack_start(e_add, True, True, 0)
        add_row.pack_start(btn_add, False, False, 0)
        box.pack_start(add_row, False, False, 0)

        p_list = self._by_date_pending.get(d_str, [])
        c_list = self._by_date_completed.get(d_str, [])
        if not p_list and not c_list:
            lb = Gtk.Label(xalign=0.0)
            lb.set_markup("<small><span foreground='#94a3b8'>此日期目前沒有任務。</span></small>")
            box.pack_start(lb, False, False, 2)
        else:
            for t in p_list:
                box.pack_start(self._make_popover_task_row(t, False, pop), False, False, 0)
            if c_list:
                sep = Gtk.Label(xalign=0.0)
                sep.set_markup(f"<small><span foreground='#64748b'>已完成 ({len(c_list)})</span></small>")
                box.pack_start(sep, False, False, 2)
                for t in c_list:
                    box.pack_start(self._make_popover_task_row(t, True, pop), False, False, 0)

        pop.add(box)
        pop.show_all()
        pop.popup()
        e_add.grab_focus()

    def _open_unscheduled_popover(self, btn):
        pop = Gtk.Popover.new(btn)
        pop.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(320, -1)

        hdr = Gtk.Label(xalign=0.0)
        hdr.set_markup(f"<b>📌 未排定到期日的待辦 ({len(self._unscheduled)})</b>")
        box.pack_start(hdr, False, False, 0)
        for t in self._unscheduled[:20]:
            box.pack_start(self._make_popover_task_row(t, False, pop), False, False, 0)

        pop.add(box)
        pop.show_all()
        pop.popup()

    def _make_popover_task_row(self, task, is_comp, pop):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        uuid_str = task.get("uuid", "")
        desc = html.escape(str(task.get("description", "")))

        btn_done = Gtk.Button(label="↩" if is_comp else "✓")
        btn_done.set_relief(Gtk.ReliefStyle.NONE)
        if is_comp:
            btn_done.connect("clicked", lambda *_: (TaskwarriorBackend.restore_task(uuid_str), pop.popdown(), self.reload_tasks()))
        else:
            btn_done.connect("clicked", lambda *_: (TaskwarriorBackend.mark_done(uuid_str), pop.popdown(), self.reload_tasks()))
        row.pack_start(btn_done, False, False, 0)

        lb = Gtk.Label(xalign=0.0)
        lb.set_line_wrap(True)
        lb.set_max_width_chars(26)
        lb.set_markup(f"<s><span foreground='#64748b'>{desc}</span></s>" if is_comp else f"<b>{desc}</b>")
        row.pack_start(lb, True, True, 0)

        btn_edit = Gtk.Button(label="✎")
        btn_edit.set_relief(Gtk.ReliefStyle.NONE)
        btn_edit.connect("clicked", lambda *_: (pop.popdown(), self._open_edit_dialog(task, is_completed=is_comp)))
        row.pack_end(btn_edit, False, False, 0)

        btn_del = Gtk.Button(label="✕")
        btn_del.set_relief(Gtk.ReliefStyle.NONE)
        btn_del.connect("clicked", lambda *_: (TaskwarriorBackend.delete_task(uuid_str), pop.popdown(), self.reload_tasks()))
        row.pack_end(btn_del, False, False, 0)
        return row

    def _build_color_picker_row(self, initial_hex=""):
        """Build a compact per-task color selector: 8 presets + Gtk.ColorButton + Auto reset."""
        state = {"hex": initial_hex or ""}
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        preset_btns = []
        cbtn = Gtk.ColorButton()
        cbtn.set_tooltip_text("自訂任意顏色")

        lbl_mode = Gtk.Label()
        lbl_mode.set_width_chars(5)
        lbl_mode.set_xalign(0.0)

        def rgba_to_hex(rgba):
            r = max(0, min(255, int(round(rgba.red * 255))))
            g = max(0, min(255, int(round(rgba.green * 255))))
            b = max(0, min(255, int(round(rgba.blue * 255))))
            return f"#{r:02x}{g:02x}{b:02x}"

        def refresh_visuals():
            cur = (state["hex"] or "").lower()
            for hx, btn in preset_btns:
                is_sel = cur == hx.lower()
                border = "2px solid #ffffff" if is_sel else "1px solid rgba(255,255,255,0.28)"
                css = (
                    f"button {{ background-image: none; background-color: {hx}; "
                    f"border: {border}; border-radius: 4px; padding: 0; "
                    f"min-width: 22px; min-height: 22px; }}"
                )
                prov = Gtk.CssProvider()
                prov.load_from_data(css.encode("utf-8"))
                btn.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            rgba = Gdk.RGBA()
            if cur and rgba.parse(cur):
                cbtn.set_rgba(rgba)
                lbl_mode.set_markup(f"<span foreground='{cur}'><b>●</b></span> 自訂")
            else:
                rgba.parse("#2563eb")
                cbtn.set_rgba(rgba)
                lbl_mode.set_text("自動")

        for hx, name in TASK_COLOR_PRESETS:
            b = Gtk.Button()
            b.set_tooltip_text(f"快速選色：{name} ({hx})")
            b.set_size_request(22, 22)
            b.connect("clicked", lambda _w, h=hx: (state.__setitem__("hex", h), refresh_visuals()))
            preset_btns.append((hx, b))
            outer.pack_start(b, False, False, 0)

        def _on_color_set(_w):
            state["hex"] = rgba_to_hex(cbtn.get_rgba())
            refresh_visuals()

        cbtn.connect("color-set", _on_color_set)
        outer.pack_start(cbtn, False, False, 2)

        btn_auto = Gtk.Button(label="↺ 自動")
        btn_auto.set_tooltip_text("恢復依到期日與優先級自動上色")
        btn_auto.connect("clicked", lambda *_: (state.__setitem__("hex", ""), refresh_visuals()))
        outer.pack_start(btn_auto, False, False, 0)
        outer.pack_start(lbl_mode, False, False, 2)

        refresh_visuals()
        return outer, (lambda: state["hex"])

    def _open_add_dialog(self, default_date):
        dlg = Gtk.Dialog(title="新增 Taskwarrior 任務", transient_for=None, flags=Gtk.DialogFlags.MODAL)
        dlg.set_position(Gtk.WindowPosition.CENTER)
        dlg.set_keep_above(True)
        dlg.add_button("取消", Gtk.ResponseType.CANCEL)
        dlg.add_button("新增", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)

        area = dlg.get_content_area()
        area.set_spacing(8)
        area.set_margin_top(10)
        area.set_margin_bottom(10)
        area.set_margin_start(14)
        area.set_margin_end(14)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        e_desc = Gtk.Entry()
        e_desc.set_hexpand(True)
        e_desc.set_width_chars(32)
        e_desc.set_activates_default(True)
        e_proj = Gtk.Entry()
        c_pri = Gtk.ComboBoxText()
        for pid, plbl in (("", "無"), ("H", "高 (H)"), ("M", "中 (M)"), ("L", "低 (L)")):
            c_pri.append(pid, plbl)
        c_pri.set_active_id("")
        e_tags = Gtk.Entry()
        e_tags.set_placeholder_text("空白分隔，如 work urgent")
        e_due = Gtk.Entry()
        e_due.set_text(default_date.strftime("%Y-%m-%d"))
        e_due.set_placeholder_text("YYYY-MM-DD（留空不設到期日）")
        color_row, get_color_hex = self._build_color_picker_row("")

        for ri, (lt, wc) in enumerate(
            [
                ("任務標題：", e_desc),
                ("專案：", e_proj),
                ("優先級：", c_pri),
                ("標籤：", e_tags),
                ("到期日：", e_due),
                ("標籤顏色：", color_row),
            ]
        ):
            grid.attach(Gtk.Label(label=lt, xalign=0.0), 0, ri, 1, 1)
            grid.attach(wc, 1, ri, 1, 1)

        area.pack_start(grid, True, True, 0)
        dlg.show_all()
        e_desc.grab_focus()
        if dlg.run() == Gtk.ResponseType.OK:
            desc = e_desc.get_text().strip()
            chosen_hex = get_color_hex()
            if desc:
                before_uuids = {t.get("uuid", "") for t in self.pending_tasks if t.get("uuid")}
                tags = [t.strip().lstrip("+") for t in e_tags.get_text().replace(",", " ").split() if t.strip()]
                TaskwarriorBackend.add_task_structured(
                    description=desc,
                    project=e_proj.get_text().strip(),
                    priority=c_pri.get_active_id() or "",
                    tags=tags,
                    due=e_due.get_text().strip(),
                )
                self.reload_tasks()
                if chosen_hex:
                    new_tasks = [t for t in self.pending_tasks if t.get("uuid") and t.get("uuid") not in before_uuids]
                    target_uuid = ""
                    if len(new_tasks) == 1:
                        target_uuid = new_tasks[0].get("uuid", "")
                    elif new_tasks:
                        for nt in new_tasks:
                            if str(nt.get("description", "")).strip() == desc:
                                target_uuid = nt.get("uuid", "")
                                break
                        if not target_uuid:
                            target_uuid = new_tasks[-1].get("uuid", "")
                    if target_uuid:
                        self.task_colors[target_uuid] = chosen_hex
                        self._save_task_colors()
                        self.cal_canvas.queue_draw()
        dlg.destroy()

    def _open_edit_dialog(self, task, is_completed=False):
        RESP_DONE = 101
        RESP_DEL = 102
        dlg = Gtk.Dialog(title="編輯 Taskwarrior 任務", transient_for=None, flags=Gtk.DialogFlags.MODAL)
        dlg.set_position(Gtk.WindowPosition.CENTER)
        dlg.set_keep_above(True)
        dlg.add_button("🗑 刪除", RESP_DEL)
        dlg.add_button("↩ 恢復待辦" if is_completed else "✓ 標記完成", RESP_DONE)
        dlg.add_button("取消", Gtk.ResponseType.CANCEL)
        dlg.add_button("儲存", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)

        area = dlg.get_content_area()
        area.set_spacing(8)
        area.set_margin_top(10)
        area.set_margin_bottom(10)
        area.set_margin_start(14)
        area.set_margin_end(14)

        uuid_str = task.get("uuid", "")
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        e_desc = Gtk.Entry()
        e_desc.set_hexpand(True)
        e_desc.set_width_chars(32)
        e_desc.set_text(str(task.get("description", "")))
        e_desc.set_activates_default(True)

        e_proj = Gtk.Entry()
        e_proj.set_text(str(task.get("project") or ""))

        c_pri = Gtk.ComboBoxText()
        for pid, plbl in (("", "無"), ("H", "高 (H)"), ("M", "中 (M)"), ("L", "低 (L)")):
            c_pri.append(pid, plbl)
        c_pri.set_active_id(str(task.get("priority") or ""))

        e_tags = Gtk.Entry()
        e_tags.set_text(" ".join(task.get("tags") or []))

        e_due = Gtk.Entry()
        e_due.set_text(str(task.get("_due_date") or ""))
        e_due.set_placeholder_text("YYYY-MM-DD（留空清除到期日）")

        color_row, get_color_hex = self._build_color_picker_row(self.task_colors.get(uuid_str, ""))

        for ri, (lt, wc) in enumerate(
            [
                ("任務標題：", e_desc),
                ("專案：", e_proj),
                ("優先級：", c_pri),
                ("標籤：", e_tags),
                ("到期日：", e_due),
                ("標籤顏色：", color_row),
            ]
        ):
            grid.attach(Gtk.Label(label=lt, xalign=0.0), 0, ri, 1, 1)
            grid.attach(wc, 1, ri, 1, 1)

        area.pack_start(grid, True, True, 0)
        dlg.show_all()
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            desc = e_desc.get_text().strip()
            chosen_hex = get_color_hex()
            if uuid_str:
                if chosen_hex:
                    self.task_colors[uuid_str] = chosen_hex
                else:
                    self.task_colors.pop(uuid_str, None)
                self._save_task_colors()
            if desc:
                TaskwarriorBackend.modify_task_structured(
                    uuid_str=uuid_str,
                    description=desc,
                    project=e_proj.get_text().strip(),
                    priority=c_pri.get_active_id() or "",
                    tags_str=e_tags.get_text().strip(),
                    due=e_due.get_text().strip(),
                    orig_tags=task.get("tags") or [],
                )
            self.reload_tasks()
        elif resp == RESP_DONE:
            if is_completed:
                TaskwarriorBackend.restore_task(uuid_str)
            else:
                TaskwarriorBackend.mark_done(uuid_str)
            self.reload_tasks()
        elif resp == RESP_DEL:
            if uuid_str and uuid_str in self.task_colors:
                self.task_colors.pop(uuid_str, None)
                self._save_task_colors()
            TaskwarriorBackend.delete_task(uuid_str)
            self.reload_tasks()
        dlg.destroy()

    def _on_undo(self, *_args):
        if TaskwarriorBackend.is_available():
            TaskwarriorBackend.undo_last()
            self.reload_tasks()

    def _show_context_menu(self, event):
        menu = Gtk.Menu()
        items = [
            ("＋ 新增任務...", lambda *_: self._open_add_dialog(date.today())),
            ("🔄 立即同步 Taskwarrior", lambda *_: self.reload_tasks()),
            ("↩ 復原上一步 (Undo)", self._on_undo),
            (None, None),
            ("🔓 解鎖拖曳與縮放" if self.config.get("locked") else "🔒 鎖定桌面位置", self._on_toggle_lock),
            ("— 縮小為桌面小圖示", self.minimize_to_icon),
            ("⚙ 外觀與位置設定...", self.show_preferences_dialog),
            (None, None),
            ("✕ 關閉桌面月曆", lambda *_: Gtk.main_quit()),
        ]
        for lbl, cb in items:
            if lbl is None:
                menu.append(Gtk.SeparatorMenuItem())
            else:
                mi = Gtk.MenuItem(label=lbl)
                mi.connect("activate", cb)
                menu.append(mi)
        menu.show_all()
        menu.popup_at_pointer(event)

    # =========================================================================
    # Preferences Dialog
    # =========================================================================

    def show_preferences_dialog(self, *_args):
        if self.prefs_win is not None:
            self.prefs_win.present()
            return

        win = Gtk.Window(title="Taskwarrior 桌面嵌入式月曆 — 外觀與位置設定")
        win.set_default_size(460, 380)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_keep_above(True)
        self.prefs_win = win
        win.connect("destroy", lambda *_: setattr(self, "prefs_win", None))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)

        def make_cbtn(key, default_hex):
            b = Gtk.ColorButton()
            r = Gdk.RGBA()
            if not r.parse(self.config.get(key, default_hex)):
                r.parse(default_hex)
            b.set_rgba(r)
            return b

        def rgba_to_hex(rgba):
            r = max(0, min(255, int(round(rgba.red * 255))))
            g = max(0, min(255, int(round(rgba.green * 255))))
            b = max(0, min(255, int(round(rgba.blue * 255))))
            return f"#{r:02x}{g:02x}{b:02x}"

        col_grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        bg_btn = make_cbtn("bg_color", "#0f172a")
        fg_btn = make_cbtn("fg_color", "#f1f5f9")
        today_bg_btn = make_cbtn("today_bg_color", "#1a5c4d")
        today_bd_btn = make_cbtn("today_border_color", "#38e0a6")
        cur_m_bg_btn = make_cbtn("cur_month_bg_color", "#141f2e")
        other_m_bg_btn = make_cbtn("other_month_bg_color", "#0d141f")

        col_grid.attach(Gtk.Label(label="整體背景底色：", xalign=0.0), 0, 0, 1, 1)
        col_grid.attach(bg_btn, 1, 0, 1, 1)
        col_grid.attach(Gtk.Label(label="文字顏色：", xalign=0.0), 2, 0, 1, 1)
        col_grid.attach(fg_btn, 3, 0, 1, 1)

        col_grid.attach(Gtk.Label(label="今日背景顏色：", xalign=0.0), 0, 1, 1, 1)
        col_grid.attach(today_bg_btn, 1, 1, 1, 1)
        col_grid.attach(Gtk.Label(label="今日邊框顏色：", xalign=0.0), 2, 1, 1, 1)
        col_grid.attach(today_bd_btn, 3, 1, 1, 1)

        col_grid.attach(Gtk.Label(label="本月日期背景：", xalign=0.0), 0, 2, 1, 1)
        col_grid.attach(cur_m_bg_btn, 1, 2, 1, 1)
        col_grid.attach(Gtk.Label(label="非本月日期背景：", xalign=0.0), 2, 2, 1, 1)
        col_grid.attach(other_m_bg_btn, 3, 2, 1, 1)
        box.pack_start(col_grid, False, False, 0)

        op_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scale_bg_op = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 2.0)
        scale_bg_op.set_digits(0)
        scale_bg_op.set_draw_value(False)
        scale_bg_op.set_value(float(self.config.get("bg_opacity", 58)))
        lbl_bg_op = Gtk.Label(label=f"{int(self.config.get('bg_opacity', 58))}%")
        lbl_bg_op.set_width_chars(4)
        op_row1.pack_start(Gtk.Label(label="整體背景透明度：", xalign=0.0), False, False, 0)
        op_row1.pack_start(scale_bg_op, True, True, 0)
        op_row1.pack_start(lbl_bg_op, False, False, 0)
        box.pack_start(op_row1, False, False, 0)

        op_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scale_cell_op = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 2.0)
        scale_cell_op.set_digits(0)
        scale_cell_op.set_draw_value(False)
        scale_cell_op.set_value(float(self.config.get("cell_opacity", 42)))
        lbl_cell_op = Gtk.Label(label=f"{int(self.config.get('cell_opacity', 42))}%")
        lbl_cell_op.set_width_chars(4)
        op_row2.pack_start(Gtk.Label(label="日期格子透明度：", xalign=0.0), False, False, 0)
        op_row2.pack_start(scale_cell_op, True, True, 0)
        op_row2.pack_start(lbl_cell_op, False, False, 0)
        box.pack_start(op_row2, False, False, 0)

        fs_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fs_row.pack_start(Gtk.Label(label="字型大小 (pt)：", xalign=0.0), False, False, 0)
        spin_fs = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(float(self.config.get("font_size", 11)), 9.0, 18.0, 1.0, 2.0, 0.0),
            climb_rate=1.0,
            digits=0,
        )
        fs_row.pack_start(spin_fs, False, False, 0)
        chk_shadow = Gtk.CheckButton(label="文字陰影")
        chk_shadow.set_active(self.config.get("text_shadow", True))
        chk_comp = Gtk.CheckButton(label="顯示已完成任務")
        chk_comp.set_active(self.config.get("show_completed", True))
        fs_row.pack_start(chk_shadow, False, False, 0)
        fs_row.pack_start(chk_comp, False, False, 0)
        box.pack_start(fs_row, False, False, 0)

        chk_weather = Gtk.CheckButton(label="在日期列顯示 7 天天氣預報（共用天氣小工具快取）")
        chk_weather.set_active(self.config.get("show_weather", True))
        box.pack_start(chk_weather, False, False, 0)

        chk_lock = Gtk.CheckButton(label="🔒 鎖定桌面位置與大小（取消勾選即可拖曳頂部移動、右下角縮放）")
        chk_lock.set_active(self.config.get("locked", False))
        box.pack_start(chk_lock, False, False, 0)

        def apply_all(*_):
            self.config["bg_color"] = rgba_to_hex(bg_btn.get_rgba())
            self.config["fg_color"] = rgba_to_hex(fg_btn.get_rgba())
            self.config["today_bg_color"] = rgba_to_hex(today_bg_btn.get_rgba())
            self.config["today_border_color"] = rgba_to_hex(today_bd_btn.get_rgba())
            self.config["cur_month_bg_color"] = rgba_to_hex(cur_m_bg_btn.get_rgba())
            self.config["other_month_bg_color"] = rgba_to_hex(other_m_bg_btn.get_rgba())
            bo = int(round(scale_bg_op.get_value()))
            co = int(round(scale_cell_op.get_value()))
            self.config["bg_opacity"] = bo
            self.config["cell_opacity"] = co
            lbl_bg_op.set_text(f"{bo}%")
            lbl_cell_op.set_text(f"{co}%")
            self.config["font_size"] = int(spin_fs.get_value_as_int())
            self.config["text_shadow"] = chk_shadow.get_active()
            self.config["show_completed"] = chk_comp.get_active()
            self.config["show_weather"] = chk_weather.get_active()
            self.config["locked"] = chk_lock.get_active()
            self._save_config()
            self._refresh_view()

        for cb in (bg_btn, fg_btn, today_bg_btn, today_bd_btn, cur_m_bg_btn, other_m_bg_btn):
            cb.connect("color-set", apply_all)
        scale_bg_op.connect("value-changed", apply_all)
        scale_cell_op.connect("value-changed", apply_all)
        spin_fs.connect("value-changed", apply_all)
        for c in (chk_shadow, chk_comp, chk_weather, chk_lock):
            c.connect("toggled", apply_all)

        btn_close = Gtk.Button(label="關閉")
        btn_close.connect("clicked", lambda *_: win.destroy())
        box.pack_end(btn_close, False, False, 0)

        win.add(box)
        win.show_all()

    def _on_destroy(self, *_args):
        if self._monitor_debounce_id is not None:
            try:
                GLib.source_remove(self._monitor_debounce_id)
            except Exception:
                pass
        if self._file_monitor is not None:
            try:
                self._file_monitor.cancel()
            except Exception:
                pass
        if self._config_debounce_id is not None:
            try:
                GLib.source_remove(self._config_debounce_id)
            except Exception:
                pass
        if self._config_monitor is not None:
            try:
                self._config_monitor.cancel()
            except Exception:
                pass
        if self.mini_win is not None:
            try:
                self.mini_win.destroy()
            except Exception:
                pass
        Gtk.main_quit()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    TaskwarriorDesktopCalendar()
    Gtk.main()


if __name__ == "__main__":
    main()
