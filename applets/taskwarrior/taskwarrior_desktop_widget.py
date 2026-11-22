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
import glob
import html
import json
import math
import os
import signal
import subprocess
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk
import cairo

_COMMON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)
from taskwarrior_shared import TaskwarriorBackend, parse_tw_timestamp

CONFIG_DIR = os.path.expanduser("~/.config/matepanel-applet-plus")
CONFIG_PATH = os.path.join(CONFIG_DIR, "taskwarrior-desktop.json")
TASK_COLORS_PATH = os.path.join(CONFIG_DIR, "task-colors.json")
WEATHER_CACHE_PATH = os.path.join(CONFIG_DIR, "weather-cache.json")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
AUTOSTART_PATH = os.path.expanduser("~/.config/autostart/taskwarrior-desktop-calendar.desktop")


def is_autostart_enabled():
    """Return True if the XDG/MATE autostart desktop entry exists and is enabled."""
    if not os.path.isfile(AUTOSTART_PATH):
        return False
    try:
        with open(AUTOSTART_PATH, "r", encoding="utf-8") as f:
            txt = f.read()
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("X-MATE-Autostart-enabled=") and s.split("=", 1)[1].strip().lower() == "false":
                return False
            if s.startswith("Hidden=") and s.split("=", 1)[1].strip().lower() == "true":
                return False
        return True
    except Exception:
        return False


def set_autostart_enabled(enabled):
    """Create or remove the XDG/MATE autostart entry for this desktop widget."""
    try:
        if enabled:
            os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
            script_path = os.path.abspath(__file__)
            entry = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Taskwarrior Desktop Calendar\n"
                "Comment=Interactive Taskwarrior monthly calendar embedded on MATE desktop\n"
                f"Exec=/usr/bin/python3 {script_path}\n"
                "Terminal=false\n"
                "X-MATE-Autostart-enabled=true\n"
                "X-MATE-Autostart-Delay=2\n"
            )
            with open(AUTOSTART_PATH, "w", encoding="utf-8") as f:
                f.write(entry)
        else:
            if os.path.exists(AUTOSTART_PATH):
                os.remove(AUTOSTART_PATH)
    except Exception:
        pass

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
    "x": 457,
    "y": 51,
    "width": 1419,
    "height": 956,
    "mini_x": None,
    "mini_y": None,
    "locked": False,
    "bg_color": "#0f172a",
    "bg_opacity": 58,           # 0..100% 整體背景透明度
    "cell_opacity": 42,         # 0..100% 日期格子底色透明度
    "today_bg_color": "#1a5c4d",        # 今日日期背景顏色
    "today_border_color": "#38e0a6",    # 今日日期邊框顏色
    "cur_month_bg_color": "#393939",    # 本月日期背景顏色
    "other_month_bg_color": "#000000",  # 非本月日期背景顏色
    "fg_color": "#f1f5f9",      # 文字顏色
    "text_shadow": True,        # 透明背景時文字陰影增強可讀性
    "font_size": 18,            # 9..24 介面字型大小（頂部列、星期列、日期數字、天氣）
    "task_font_size": 18,       # 6..28 任務膠囊全域預設字型大小
    "detail_font_size": 16,     # 9..24 詳細任務內容視窗字型大小
    "task_columns": 1,          # 1 = 全域預設單欄, 2 = 全域預設垂直分割成兩列
    "date_cell_overrides": {},  # 單日獨立排版覆寫: {"YYYY-MM-DD": {"task_columns": 2, "task_font_size": 9}}
    "retention_days": 180,      # 啟動時保留天數（0 = 不自動刪除；>0 時清理超過此天數的任務與單日設定）
    "auto_backup_enabled": True,   # 每日自動備份（保留最近 3 天）
    "auto_backup_keep_days": 3,    # 自動備份保留份數（預設最近 3 天）
    "auto_shrink_task_font": True,  # 當任務超過日期框大小時，自動縮小任務字型以完整顯示
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
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
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
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
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
        self._detail_rebuilders = []

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
        self._backup_timer_id = None
        self._is_minimized = False
        self._is_maximized = False
        self._pre_max_geom = None
        self.mini_win = None
        self._drag_mode = None      # None, "move", "se", "e", "s"
        self._drag_start_root = None
        self._drag_orig_pos = None
        self._drag_orig_size = None
        self._last_cursor_name = None

        # Enable RGBA transparency
        screen = self.get_screen()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None and screen.is_composited():
                self.set_visual(visual)
        self.set_app_paintable(True)

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("draw", self._on_draw_bg)
        self.connect("configure-event", self._on_configure)
        self.connect("button-press-event", self._on_win_button_press)
        self.connect("motion-notify-event", self._on_win_motion)
        self.connect("button-release-event", self._on_win_button_release)
        self.connect("leave-notify-event", self._on_win_leave)
        self.connect("destroy", self._on_destroy)

        self._build_ui()

        w = max(520, int(self.config.get("width", 940)))
        h = max(380, int(self.config.get("height", 620)))
        x = int(self.config.get("x", 80))
        y = int(self.config.get("y", 60))
        self.set_default_size(w, h)
        self.resize(w, h)
        self.move(x, y)

        self._run_auto_backup_if_needed()
        self._run_startup_cleanup()
        self._start_file_monitor()
        self._start_config_monitor()
        self._backup_timer_id = GLib.timeout_add_seconds(3600, self._on_auto_backup_timer)
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

    def _run_startup_cleanup(self):
        """
        Run ONCE on widget startup when retention_days > 0:
        1. Remove expired date_cell_overrides (< today - retention_days) from taskwarrior-desktop.json.
        2. Remove expired Taskwarrior tasks (including completed, deleted, pending, waiting, recurring)
           older than retention_days via official `task` CLI (`delete` -> `purge`), and prune their
           UUIDs from task-colors.json.
        When retention_days == 0, performs NO deletion at all.
        """
        try:
            days = int(self.config.get("retention_days", 180))
        except (TypeError, ValueError):
            days = 180
        if days <= 0:
            return

        cutoff_date = date.today() - timedelta(days=days)
        cutoff_str = cutoff_date.strftime("%Y-%m-%d")

        # 1. Clean expired date_cell_overrides in taskwarrior-desktop.json
        overrides = self.config.get("date_cell_overrides")
        if isinstance(overrides, dict) and overrides:
            expired_keys = [k for k in list(overrides.keys()) if isinstance(k, str) and len(k) == 10 and k < cutoff_str]
            if expired_keys:
                for k in expired_keys:
                    overrides.pop(k, None)
                self._save_config()

        # 2. Clean expired tasks in ~/.task via official Taskwarrior CLI
        if not TaskwarriorBackend.is_available():
            return
        try:
            proc = subprocess.run(
                ["task", "rc.verbose=nothing", "export"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            raw = (proc.stdout or "").strip()
            if not raw:
                return
            all_tasks = json.loads(raw)
            if not isinstance(all_tasks, list):
                return
        except Exception:
            return

        to_delete = []
        to_purge = []
        for t in all_tasks:
            if not isinstance(t, dict):
                continue
            uid = str(t.get("uuid") or "").strip()
            if not uid:
                continue
            status = str(t.get("status") or "").lower()
            # Determine task's effective reference date
            ref_dt = None
            if status in ("completed", "deleted"):
                for field in ("end", "due", "modified", "entry"):
                    ref_dt = parse_tw_timestamp(t.get(field))
                    if ref_dt is not None:
                        break
            else:
                for field in ("due", "wait", "scheduled", "entry"):
                    ref_dt = parse_tw_timestamp(t.get(field))
                    if ref_dt is not None:
                        break
            if ref_dt is None:
                continue
            if ref_dt.date() < cutoff_date:
                to_purge.append(uid)
                if status != "deleted":
                    to_delete.append(uid)

        if not to_purge:
            return

        # Delete non-deleted tasks first (with recurrence confirmation disabled), then purge all expired UUIDs
        try:
            if to_delete:
                subprocess.run(
                    [
                        "task",
                        "rc.confirmation=no",
                        "rc.recurrence.confirmation=no",
                        "rc.bulk=0",
                        "rc.verbose=nothing",
                        ",".join(to_delete),
                        "delete",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            subprocess.run(
                [
                    "task",
                    "rc.confirmation=no",
                    "rc.bulk=0",
                    "rc.verbose=nothing",
                    ",".join(to_purge),
                    "purge",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except Exception:
            pass

        # Prune expired UUIDs from task-colors.json
        color_changed = False
        for uid in to_purge:
            if uid in self.task_colors:
                self.task_colors.pop(uid, None)
                color_changed = True
        if color_changed:
            self._save_task_colors()

    def _get_date_cell_settings(self, d_str):
        """Return (task_font_size_float, task_columns_int, has_manual_fs) for a specific YYYY-MM-DD cell."""
        g_fs = max(6, min(28, int(self.config.get("task_font_size", 10))))
        g_cols = 2 if int(self.config.get("task_columns", 1)) >= 2 else 1
        has_manual_fs = False
        overrides = self.config.get("date_cell_overrides")
        if isinstance(overrides, dict):
            ov = overrides.get(d_str)
            if isinstance(ov, dict):
                if "task_font_size" in ov:
                    try:
                        g_fs = max(6, min(28, int(ov["task_font_size"])))
                        has_manual_fs = True
                    except (TypeError, ValueError):
                        pass
                if "task_columns" in ov:
                    try:
                        g_cols = 2 if int(ov["task_columns"]) >= 2 else 1
                    except (TypeError, ValueError):
                        pass
        return float(g_fs), g_cols, has_manual_fs

    def _set_date_cell_override(self, d_str, key, val):
        """Set a per-date override; automatically prune if it matches global default."""
        overrides = self.config.setdefault("date_cell_overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
            self.config["date_cell_overrides"] = overrides
        g_default = (
            (2 if int(self.config.get("task_columns", 1)) >= 2 else 1)
            if key == "task_columns"
            else max(6, min(28, int(self.config.get("task_font_size", 10))))
        )
        ov = overrides.get(d_str)
        if not isinstance(ov, dict):
            ov = {}
        if val == g_default:
            ov.pop(key, None)
        else:
            ov[key] = val
        if ov:
            overrides[d_str] = ov
        else:
            overrides.pop(d_str, None)
        self._save_config()
        self._refresh_view()

    def _reset_date_cell_override(self, d_str):
        overrides = self.config.get("date_cell_overrides")
        if isinstance(overrides, dict) and d_str in overrides:
            overrides.pop(d_str, None)
            self._save_config()
            self._refresh_view()

    def _reset_all_date_overrides(self, *_args):
        if self.config.get("date_cell_overrides"):
            self.config["date_cell_overrides"] = {}
            self._save_config()
            self._refresh_view()

    # =========================================================================
    # Backup Bundle Export / Import & Rolling 3-Day Automatic Backup
    # =========================================================================

    def _export_all_tasks_raw(self):
        if not TaskwarriorBackend.is_available():
            return []
        try:
            proc = subprocess.run(
                ["task", "rc.verbose=nothing", "export"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            raw = (proc.stdout or "").strip()
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _build_backup_bundle(self):
        tasks = self._export_all_tasks_raw()
        return {
            "format": "matepanel-taskwarrior-bundle",
            "version": 1,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "desktop_config": dict(self.config),
            "task_colors": dict(self.task_colors),
            "tasks": tasks,
        }

    def _write_backup_bundle(self, filepath):
        try:
            parent = os.path.dirname(os.path.abspath(filepath))
            if parent:
                os.makedirs(parent, exist_ok=True)
            bundle = self._build_backup_bundle()
            tmp_path = filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(bundle, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, filepath)
            return True, len(bundle["tasks"]), ""
        except Exception as e:
            return False, 0, str(e)

    def _run_auto_backup_if_needed(self):
        """Maintain rolling daily backups (default: newest 3 days) in BACKUP_DIR."""
        if not self.config.get("auto_backup_enabled", True):
            return
        try:
            keep_days = max(1, int(self.config.get("auto_backup_keep_days", 3)))
        except (TypeError, ValueError):
            keep_days = 3

        today_str = date.today().strftime("%Y-%m-%d")
        today_file = os.path.join(BACKUP_DIR, f"auto-backup-{today_str}.json")
        if not os.path.isfile(today_file):
            ok, _cnt, _err = self._write_backup_bundle(today_file)
            if not ok:
                return

        # Rotate: keep only the newest `keep_days` auto-backup files, delete oldest beyond limit
        try:
            files = sorted(glob.glob(os.path.join(BACKUP_DIR, "auto-backup-????-??-??.json")), reverse=True)
            for old_f in files[keep_days:]:
                try:
                    os.remove(old_f)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_auto_backup_timer(self):
        self._run_auto_backup_if_needed()
        return True

    def _clamp_geometry_to_workarea(self):
        """Ensure imported x/y/width/height and mini_x/mini_y stay inside current screen workarea."""
        display = Gdk.Display.get_default()
        if display is None:
            return
        gdk_win = self.get_window()
        mon = display.get_monitor_at_window(gdk_win) if gdk_win is not None else display.get_primary_monitor()
        if mon is None and display.get_n_monitors() > 0:
            mon = display.get_monitor(0)
        if mon is None:
            return
        wa = mon.get_workarea()
        if wa.width <= 0 or wa.height <= 0:
            return

        w = max(520, min(int(wa.width), int(self.config.get("width", 940))))
        h = max(380, min(int(wa.height), int(self.config.get("height", 620))))
        x = max(int(wa.x), min(int(wa.x + wa.width - w), int(self.config.get("x", wa.x + 40))))
        y = max(int(wa.y), min(int(wa.y + wa.height - h), int(self.config.get("y", wa.y + 40))))
        self.config["x"] = x
        self.config["y"] = y
        self.config["width"] = w
        self.config["height"] = h

        mx = self.config.get("mini_x")
        my = self.config.get("mini_y")
        if mx is not None and my is not None:
            sz = MiniDesktopIconWindow.ICON_SIZE
            self.config["mini_x"] = max(int(wa.x), min(int(wa.x + wa.width - sz), int(mx)))
            self.config["mini_y"] = max(int(wa.y), min(int(wa.y + wa.height - sz), int(my)))

    def _show_info_dialog(self, title, text, msg_type=Gtk.MessageType.INFO):
        md = Gtk.MessageDialog(
            transient_for=None,
            flags=Gtk.DialogFlags.MODAL,
            message_type=msg_type,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        md.format_secondary_text(text)
        md.set_position(Gtk.WindowPosition.CENTER)
        md.set_keep_above(True)
        md.run()
        md.destroy()

    def _open_export_dialog(self, *_args):
        dlg = Gtk.FileChooserDialog(
            title="匯出 Taskwarrior 任務與桌面月曆設定",
            transient_for=None,
            action=Gtk.FileChooserAction.SAVE,
        )
        dlg.set_position(Gtk.WindowPosition.CENTER)
        dlg.set_keep_above(True)
        dlg.set_do_overwrite_confirmation(True)
        dlg.add_button("取消", Gtk.ResponseType.CANCEL)
        dlg.add_button("匯出", Gtk.ResponseType.ACCEPT)
        dlg.set_current_name(f"taskwarrior-backup-{date.today().strftime('%Y-%m-%d')}.json")
        flt = Gtk.FileFilter()
        flt.set_name("Taskwarrior 備份封包 (*.json)")
        flt.add_pattern("*.json")
        dlg.add_filter(flt)

        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            dlg.destroy()
            if path:
                if not path.endswith(".json"):
                    path += ".json"
                ok, n_tasks, err = self._write_backup_bundle(path)
                if ok:
                    self._show_info_dialog(
                        "匯出成功",
                        f"已將 {n_tasks} 筆任務、{len(self.task_colors)} 筆任務顏色與完整桌面設定匯出至：\n{path}",
                    )
                else:
                    self._show_info_dialog("匯出失敗", err, Gtk.MessageType.ERROR)
        else:
            dlg.destroy()

    def _open_import_dialog(self, *_args):
        dlg = Gtk.FileChooserDialog(
            title="匯入 Taskwarrior 任務與桌面月曆設定",
            transient_for=None,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.set_position(Gtk.WindowPosition.CENTER)
        dlg.set_keep_above(True)
        dlg.add_button("取消", Gtk.ResponseType.CANCEL)
        dlg.add_button("下一步", Gtk.ResponseType.ACCEPT)
        if os.path.isdir(BACKUP_DIR):
            dlg.set_current_folder(BACKUP_DIR)
        flt = Gtk.FileFilter()
        flt.set_name("Taskwarrior 備份封包 (*.json)")
        flt.add_pattern("*.json")
        dlg.add_filter(flt)

        if dlg.run() != Gtk.ResponseType.ACCEPT:
            dlg.destroy()
            return
        path = dlg.get_filename()
        dlg.destroy()
        if not path or not os.path.isfile(path):
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                bundle = json.load(f)
            if not isinstance(bundle, dict) or bundle.get("format") != "matepanel-taskwarrior-bundle":
                raise ValueError("檔案格式不符（非 matepanel-taskwarrior-bundle 備份檔）")
        except Exception as e:
            self._show_info_dialog("無法讀取備份檔", str(e), Gtk.MessageType.ERROR)
            return

        b_tasks = bundle.get("tasks") if isinstance(bundle.get("tasks"), list) else []
        b_colors = bundle.get("task_colors") if isinstance(bundle.get("task_colors"), dict) else {}
        b_cfg = bundle.get("desktop_config") if isinstance(bundle.get("desktop_config"), dict) else {}
        b_ov = b_cfg.get("date_cell_overrides") if isinstance(b_cfg.get("date_cell_overrides"), dict) else {}
        exp_at = str(bundle.get("exported_at") or "未知時間")

        cdlg = Gtk.Dialog(title="確認匯入備份封包", transient_for=None, flags=Gtk.DialogFlags.MODAL)
        cdlg.set_position(Gtk.WindowPosition.CENTER)
        cdlg.set_keep_above(True)
        cdlg.add_button("取消", Gtk.ResponseType.CANCEL)
        cdlg.add_button("開始匯入", Gtk.ResponseType.OK)
        cdlg.set_default_response(Gtk.ResponseType.OK)

        area = cdlg.get_content_area()
        area.set_spacing(8)
        area.set_margin_top(12)
        area.set_margin_bottom(12)
        area.set_margin_start(14)
        area.set_margin_end(14)

        lbl_sum = Gtk.Label(xalign=0.0)
        lbl_sum.set_markup(
            f"<b>備份檔案：</b>{html.escape(os.path.basename(path))}\n"
            f"<b>匯出時間：</b>{html.escape(exp_at)}\n"
            f"<b>包含內容：</b>{len(b_tasks)} 筆任務、{len(b_colors)} 筆任務顏色、{len(b_ov)} 筆單日排版設定"
        )
        area.pack_start(lbl_sum, False, False, 0)

        rb_merge = Gtk.RadioButton.new_with_label_from_widget(
            None, "合併匯入（推薦：保留本機其他任務，同 UUID 自動更新並合併顏色與設定）"
        )
        rb_replace = Gtk.RadioButton.new_with_label_from_widget(
            rb_merge, "完整覆蓋還原（先清空本機 ~/.task 與設定，再完全還原此備份檔）"
        )
        rb_merge.set_active(True)
        area.pack_start(rb_merge, False, False, 0)
        area.pack_start(rb_replace, False, False, 0)

        chk_apply_geom = Gtk.CheckButton(label="同時套用備份中的視窗位置與大小（自動夾限至目前螢幕範圍）")
        chk_apply_geom.set_active(True)
        area.pack_start(chk_apply_geom, False, False, 0)

        cdlg.show_all()
        resp = cdlg.run()
        is_replace = rb_replace.get_active()
        apply_geom = chk_apply_geom.get_active()
        cdlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return

        # 1. Import Taskwarrior tasks via official CLI
        imported_ok = True
        err_detail = ""
        if TaskwarriorBackend.is_available():
            try:
                if is_replace:
                    existing = self._export_all_tasks_raw()
                    ex_del = [str(t["uuid"]) for t in existing if isinstance(t, dict) and t.get("uuid") and str(t.get("status", "")).lower() != "deleted"]
                    ex_all = [str(t["uuid"]) for t in existing if isinstance(t, dict) and t.get("uuid")]
                    if ex_del:
                        subprocess.run(
                            ["task", "rc.confirmation=no", "rc.recurrence.confirmation=no", "rc.bulk=0", "rc.verbose=nothing", ",".join(ex_del), "delete"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
                        )
                    if ex_all:
                        subprocess.run(
                            ["task", "rc.confirmation=no", "rc.bulk=0", "rc.verbose=nothing", ",".join(ex_all), "purge"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
                        )
                if b_tasks:
                    proc = subprocess.run(
                        ["task", "rc.verbose=nothing", "import"],
                        input=json.dumps(b_tasks, ensure_ascii=False),
                        text=True,
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
                    if proc.returncode != 0:
                        imported_ok = False
                        err_detail = (proc.stderr or proc.stdout or "").strip()
            except Exception as e:
                imported_ok = False
                err_detail = str(e)

        # 2. Import task_colors
        clean_colors = {str(k): str(v) for k, v in b_colors.items() if isinstance(v, str) and v.startswith("#")}
        if is_replace:
            self.task_colors = clean_colors
        else:
            self.task_colors.update(clean_colors)
        self._save_task_colors()

        # 3. Import desktop_config
        geom_keys = {"x", "y", "width", "height", "mini_x", "mini_y"}
        if is_replace:
            new_cfg = dict(DEFAULT_CONFIG)
            for k, v in b_cfg.items():
                if not apply_geom and k in geom_keys:
                    new_cfg[k] = self.config.get(k, new_cfg.get(k))
                else:
                    new_cfg[k] = v
            self.config = new_cfg
        else:
            merged_ov = dict(self.config.get("date_cell_overrides") or {})
            merged_ov.update(b_ov)
            for k, v in b_cfg.items():
                if k == "date_cell_overrides":
                    continue
                if not apply_geom and k in geom_keys:
                    continue
                self.config[k] = v
            self.config["date_cell_overrides"] = merged_ov

        self._clamp_geometry_to_workarea()
        self._save_config()

        if apply_geom and not self._is_minimized and not self._is_maximized:
            self.resize(int(self.config["width"]), int(self.config["height"]))
            self.move(int(self.config["x"]), int(self.config["y"]))

        self.reload_tasks()
        if imported_ok:
            self._show_info_dialog(
                "匯入完成",
                f"已成功匯入 {len(b_tasks)} 筆任務、{len(clean_colors)} 筆任務顏色與桌面月曆設定！",
            )
        else:
            self._show_info_dialog(
                "設定已匯入，但任務匯入出現警告",
                err_detail or "請確認 Taskwarrior (`task`) 狀態。",
                Gtk.MessageType.WARNING,
            )

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
        if self._is_minimized or self._is_maximized or not self.get_visible():
            return False
        if self._geom_save_id is not None:
            try:
                GLib.source_remove(self._geom_save_id)
            except Exception:
                pass

        def _commit():
            self._geom_save_id = None
            if self._is_minimized or self._is_maximized or not self.get_visible():
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
        self.hdr_ev.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.hdr_ev.connect("button-press-event", self._on_header_button_press)
        self.hdr_ev.connect("motion-notify-event", self._on_win_motion)
        self.hdr_ev.connect("button-release-event", self._on_win_button_release)

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

        self.btn_max = _make_btn("□", "放大至工作區全螢幕（不與 MATE Panel 重疊）")
        self.btn_max.connect("clicked", self.toggle_maximize_workarea)

        self.btn_mini = _make_btn("—", "縮小為桌面小圖示（方便操作桌面檔案）")
        self.btn_mini.connect("clicked", self.minimize_to_icon)

        self._nav_buttons = [
            self.btn_prev, self.btn_today, self.btn_next,
            self.btn_unsched, self.btn_add, self.btn_undo, self.btn_lock, self.btn_prefs, self.btn_max, self.btn_mini,
        ]

        nav.pack_start(self.btn_prev, False, False, 0)
        nav.pack_start(self.btn_today, False, False, 0)
        nav.pack_start(self.btn_next, False, False, 0)
        nav.pack_start(self.month_lbl, True, True, 0)
        nav.pack_end(self.btn_mini, False, False, 0)
        nav.pack_end(self.btn_max, False, False, 0)
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
        self.cal_canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.cal_canvas.connect("draw", self._on_draw_calendar)
        self.cal_canvas.connect("button-press-event", self._on_canvas_click)
        self.cal_canvas.connect("motion-notify-event", self._on_win_motion)
        self.cal_canvas.connect("button-release-event", self._on_win_button_release)
        root.pack_start(self.cal_canvas, True, True, 0)

        self.add(root)
        self._style_nav_labels()

    def _style_nav_labels(self):
        fg = html.escape(self.config.get("fg_color", "#f1f5f9"))
        locked = bool(self.config.get("locked", False))
        self.btn_lock._raw_txt = "🔒" if locked else "🔓"
        self.btn_max._raw_txt = "❐" if self._is_maximized else "□"
        self.btn_max.set_tooltip_text(
            "還原視窗大小" if self._is_maximized else "放大至工作區全螢幕（不與 MATE Panel 重疊）"
        )
        self.btn_unsched._raw_txt = f"📌 未排期 ({len(self._unscheduled)})"
        for b in self._nav_buttons:
            b._inner_lbl.set_markup(f"<span foreground='{fg}'><b>{html.escape(b._raw_txt)}</b></span>")
        self.btn_unsched.set_visible(len(self._unscheduled) > 0)

    def _set_cursor_name(self, name):
        if name == self._last_cursor_name:
            return
        self._last_cursor_name = name
        gdk_win = self.get_window()
        if gdk_win is None:
            return
        try:
            if name:
                cur = Gdk.Cursor.new_from_name(gdk_win.get_display(), name)
                gdk_win.set_cursor(cur)
            else:
                gdk_win.set_cursor(None)
        except Exception:
            pass

    def _detect_resize_edge(self, x_root, y_root):
        """Return 'se', 'e', 's', or None based on pointer position relative to window."""
        if self.config.get("locked", False):
            return None
        wx, wy = self.get_position()
        ww, wh = self.get_size()
        lx = float(x_root) - float(wx)
        ly = float(y_root) - float(wy)
        on_right = lx >= (ww - 12.0) and lx <= (ww + 4.0)
        on_bottom = ly >= (wh - 12.0) and ly <= (wh + 4.0)
        in_se = lx >= (ww - 24.0) and ly >= (wh - 24.0)
        if in_se or (on_right and on_bottom):
            return "se"
        if on_right:
            return "e"
        if on_bottom:
            return "s"
        return None

    def _start_manual_drag(self, mode, x_root, y_root):
        if self._is_maximized:
            self._is_maximized = False
            self._style_nav_labels()
        self._drag_mode = mode
        self._drag_start_root = (float(x_root), float(y_root))
        self._drag_orig_pos = self.get_position()
        self._drag_orig_size = self.get_size()

    def _on_win_button_press(self, _w, event):
        if event.button == 1 and not self.config.get("locked", False):
            edge = self._detect_resize_edge(event.x_root, event.y_root)
            if edge:
                self._start_manual_drag(edge, event.x_root, event.y_root)
                return True
        if event.button == 3:
            self._show_context_menu(event, clicked_date=None)
            return True
        return False

    def _on_win_motion(self, _w, event):
        if self._drag_mode is not None and self._drag_start_root is not None:
            if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
                self._finish_manual_drag()
                return False
            dx = float(event.x_root) - self._drag_start_root[0]
            dy = float(event.y_root) - self._drag_start_root[1]
            if self._drag_mode == "move":
                nx = int(round(self._drag_orig_pos[0] + dx))
                ny = int(round(self._drag_orig_pos[1] + dy))
                self.move(nx, ny)
            else:
                ow, oh = self._drag_orig_size
                nw = max(520, int(round(ow + dx))) if self._drag_mode in ("se", "e") else ow
                nh = max(380, int(round(oh + dy))) if self._drag_mode in ("se", "s") else oh
                self.resize(nw, nh)
            return True

        edge = self._detect_resize_edge(event.x_root, event.y_root)
        cursor_map = {"se": "nwse-resize", "e": "ew-resize", "s": "ns-resize"}
        self._set_cursor_name(cursor_map.get(edge))
        return False

    def _finish_manual_drag(self):
        if self._drag_mode is None:
            return
        self._drag_mode = None
        self._drag_start_root = None
        self._drag_orig_pos = None
        self._drag_orig_size = None
        px, py = self.get_position()
        pw, ph = self.get_size()
        self.config["x"] = int(px)
        self.config["y"] = int(py)
        self.config["width"] = int(pw)
        self.config["height"] = int(ph)
        self._save_config()

    def _on_win_button_release(self, _w, event):
        if event.button == 1 and self._drag_mode is not None:
            self._finish_manual_drag()
            return True
        return False

    def _on_win_leave(self, _w, _event):
        if self._drag_mode is None:
            self._set_cursor_name(None)
        return False

    def _on_header_button_press(self, _w, event):
        if event.button == 1 and not self.config.get("locked", False):
            self._start_manual_drag("move", event.x_root, event.y_root)
            return True
        if event.button == 3:
            self._show_context_menu(event, clicked_date=None)
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

    def toggle_maximize_workarea(self, *_args):
        """Toggle maximizing the widget to the monitor's usable workarea (never overlapping MATE Panel)."""
        if self._is_maximized:
            self._is_maximized = False
            if self._pre_max_geom:
                x, y, w, h = self._pre_max_geom
            else:
                x = int(self.config.get("x", 80))
                y = int(self.config.get("y", 60))
                w = max(520, int(self.config.get("width", 940)))
                h = max(380, int(self.config.get("height", 620)))
            self.resize(w, h)
            self.move(x, y)
            self._style_nav_labels()
            self.cal_canvas.queue_draw()
            return

        px, py = self.get_position()
        pw, ph = self.get_size()
        self._pre_max_geom = (int(px), int(py), int(pw), int(ph))
        self.config["x"] = int(px)
        self.config["y"] = int(py)
        self.config["width"] = int(pw)
        self.config["height"] = int(ph)
        self._save_config()

        wa = None
        display = Gdk.Display.get_default()
        if display is not None:
            gdk_win = self.get_window()
            mon = display.get_monitor_at_window(gdk_win) if gdk_win is not None else display.get_primary_monitor()
            if mon is None and display.get_n_monitors() > 0:
                mon = display.get_monitor(0)
            if mon is not None:
                wa = mon.get_workarea()

        if wa is not None and wa.width > 0 and wa.height > 0:
            self._is_maximized = True
            self.move(int(wa.x), int(wa.y))
            self.resize(int(wa.width), int(wa.height))
            self._style_nav_labels()
            self.cal_canvas.queue_draw()

    def minimize_to_icon(self, *_args):
        if self._is_minimized:
            return
        px, py = self.get_position()
        pw, ph = self.get_size()
        if not self._is_maximized:
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
        self._is_minimized = False
        if self._is_maximized:
            self._is_maximized = False
            self.show_all()
            self.toggle_maximize_workarea()
            self._refresh_view()
            GLib.idle_add(self._assert_desktop_layer)
            return
        w = max(520, int(self.config.get("width", 940)))
        h = max(380, int(self.config.get("height", 620)))
        x = int(self.config.get("x", 80))
        y = int(self.config.get("y", 60))
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
        base_fs = float(max(9, min(24, int(self.config.get("font_size", 11)))))
        task_fs = float(max(6, min(28, int(self.config.get("task_font_size", 10)))))
        task_cols_cfg = 2 if int(self.config.get("task_columns", 1)) >= 2 else 1
        auto_shrink = bool(self.config.get("auto_shrink_task_font", True))
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

        pill_fs = task_fs
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

            start_y = cy + base_fs + 7.0
            avail_h = (cy + ch - 3.0) - start_y
            d_task_fs, d_task_cols, has_manual_fs = self._get_date_cell_settings(d_str)
            c_pill_fs = d_task_fs
            c_pill_h = max(12.0, c_pill_fs + 5.0)
            c_pill_gap = pill_gap
            n_items = len(combined)
            n_cols = 2 if (d_task_cols == 2 and cw >= 92.0) else 1
            rows_needed = (n_items + n_cols - 1) // n_cols

            if auto_shrink and not has_manual_fs and rows_needed > 0 and avail_h > 0.0:
                needed_h = rows_needed * c_pill_h + max(0, rows_needed - 1) * c_pill_gap
                if needed_h > avail_h:
                    c_pill_gap = 1.5 if rows_needed <= 8 else 1.0
                    slot_h = (avail_h - max(0, rows_needed - 1) * c_pill_gap) / float(rows_needed)
                    min_pill_h = 9.0
                    if slot_h >= min_pill_h:
                        c_pill_h = slot_h
                        c_pill_fs = max(6.0, min(d_task_fs, c_pill_h - 3.2))
                    else:
                        c_pill_h = min_pill_h
                        c_pill_fs = 6.0

            if avail_h < c_pill_h:
                continue

            max_rows = max(1, int((avail_h + c_pill_gap + 0.01) // (c_pill_h + c_pill_gap)))
            max_slots = max_rows * n_cols
            has_overflow = n_items > max_slots
            if has_overflow and max_rows >= 2:
                draw_rows = max_rows - 1
                draw_slots = draw_rows * n_cols
            else:
                draw_rows = max_rows
                draw_slots = max_slots

            col_gap = 3.0 if n_cols == 2 else 0.0
            col_pw = (cw - 7.0 - col_gap) / float(n_cols)

            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            ctx.set_font_size(c_pill_fs)

            drawn_count = 0
            for item_i, (t, is_comp) in enumerate(combined[:draw_slots]):
                row_i = item_i // n_cols
                col_i = item_i % n_cols
                px = cx + 3.5 + col_i * (col_pw + col_gap)
                py = start_y + row_i * (c_pill_h + c_pill_gap)
                pw = col_pw
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

                draw_rounded_rect(ctx, px, py, pw, c_pill_h, min(3.5, c_pill_h / 3.0))
                ctx.set_source_rgba(*p_bg)
                ctx.fill()

                disp = self._truncate_text(ctx, label_txt, max(10.0, pw - 6.0))
                ext = ctx.text_extents(disp)
                ty = py + (c_pill_h - ext.height) / 2.0 - ext.y_bearing
                ctx.set_source_rgba(txt_rgb[0], txt_rgb[1], txt_rgb[2], 0.80 if is_comp else 0.98)
                ctx.move_to(px + 3.0 - ext.x_bearing, ty)
                ctx.show_text(disp)

                self._task_hit_rects.append((px, py, pw, c_pill_h, t, is_comp))
                drawn_count += 1

            if has_overflow:
                rem = n_items - drawn_count
                more_s = f"+{rem} 更多..."
                ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                ctx.set_font_size(max(7.0, c_pill_fs - 0.5))
                m_ext = ctx.text_extents(more_s)
                more_y = start_y + draw_rows * (c_pill_h + c_pill_gap)
                self._draw_text(
                    ctx,
                    cx + 5.0 - m_ext.x_bearing,
                    min(cy + ch - 3.5, more_y + m_ext.height + 1.0),
                    more_s,
                    (0.55, 0.85, 1.0),
                    0.95,
                )
        return False

    # =========================================================================
    # Mouse Interactions (Click Task -> Edit, Click Cell -> Day Popover, Right-click -> Menu)
    # =========================================================================

    def _on_canvas_click(self, widget, event):
        # Bottom-right / edge resize grip when unlocked (WM-independent, works on Marco & Compiz)
        if event.button == 1 and not self.config.get("locked", False):
            edge = self._detect_resize_edge(event.x_root, event.y_root)
            if edge:
                self._start_manual_drag(edge, event.x_root, event.y_root)
                return True

        if event.button == 3:
            clicked_date = None
            for cx, cy, cw, ch, d_obj in self._cell_rects:
                if cx <= event.x <= cx + cw and cy <= event.y <= cy + ch:
                    clicked_date = d_obj
                    break
            self._show_context_menu(event, clicked_date=clicked_date)
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

    def _toggle_task_columns(self, *_args):
        cur = 2 if int(self.config.get("task_columns", 1)) >= 2 else 1
        self.config["task_columns"] = 1 if cur == 2 else 2
        self._save_config()
        self._refresh_view()

    def _adjust_task_font_size(self, delta):
        cur = int(self.config.get("task_font_size", 10))
        nxt = max(6, min(28, cur + int(delta)))
        if nxt != cur:
            self.config["task_font_size"] = nxt
            self._save_config()
            self._refresh_view()

    def _format_tw_dt(self, raw_ts):
        dt = parse_tw_timestamp(raw_ts)
        if dt is None:
            return ""
        return dt.strftime("%Y-%m-%d %H:%M")

    def _get_task_accent_hex(self, task, is_comp=False):
        uid = str(task.get("uuid") or "")
        custom_hex = self.task_colors.get(uid)
        if custom_hex:
            return custom_hex
        if is_comp:
            return "#10b981"
        due_lvl = task.get("_due_level", "normal")
        pri = task.get("priority", "")
        if due_lvl == "crit" or pri == "H":
            return "#dc2626"
        if due_lvl == "warn" or pri == "M":
            return "#d97706"
        return "#2563eb"

    def _open_day_detail_window(self, d_obj):
        """Open a dedicated, resizable, scrollable detail window for all tasks on `d_obj`."""
        win = Gtk.Window(title="任務詳細內容")
        win.set_default_size(580, 480)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_keep_above(True)

        state = {"date": d_obj}

        def _on_key(_w, ev):
            if ev.keyval == Gdk.KEY_Escape:
                win.destroy()
                return True
            return False

        win.connect("key-press-event", _on_key)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(14)
        outer.set_margin_end(14)

        # Top header bar
        hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_prev = Gtk.Button(label="◀ 前一天")
        btn_next = Gtk.Button(label="後一天 ▶")
        lbl_title = Gtk.Label(xalign=0.0)
        lbl_title.set_margin_start(6)
        btn_add = Gtk.Button(label="＋ 新增任務")
        hdr_row.pack_start(btn_prev, False, False, 0)
        hdr_row.pack_start(btn_next, False, False, 0)
        hdr_row.pack_start(lbl_title, True, True, 0)
        hdr_row.pack_end(btn_add, False, False, 0)
        outer.pack_start(hdr_row, False, False, 0)

        lbl_sub = Gtk.Label(xalign=0.0)
        outer.pack_start(lbl_sub, False, False, 0)

        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        list_box.set_margin_top(4)
        list_box.set_margin_bottom(4)
        list_box.set_margin_end(6)
        sc.add(list_box)
        outer.pack_start(sc, True, True, 0)

        btm = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_clear_day = Gtk.Button(label="🗑 清空當日任務")
        btn_clear_day.set_tooltip_text("清空此日期的所有待辦與已完成任務（會先跳出確認視窗）")
        btn_close = Gtk.Button(label="關閉 (Esc)")
        btn_close.connect("clicked", lambda *_: win.destroy())
        btm.pack_start(btn_clear_day, False, False, 0)
        btm.pack_end(btn_close, False, False, 0)
        outer.pack_end(btm, False, False, 0)

        def rebuild():
            cur_d = state["date"]
            d_str = cur_d.strftime("%Y-%m-%d")
            wd = self.WEEKDAYS_ZH[cur_d.weekday()]
            dfs = max(9, min(24, int(self.config.get("detail_font_size", 12))))
            title_sz = int((dfs + 2) * 1024)
            body_sz = int(dfs * 1024)
            meta_sz = int(max(8, dfs - 1.5) * 1024)

            win.set_title(f"任務詳細內容 — {d_str} ({wd})")
            lbl_title.set_markup(f"<span size='{title_sz}'><b>📅 {d_str}（{wd}）</b></span>")

            p_list = list(self._by_date_pending.get(d_str, []))
            c_list = list(self._by_date_completed.get(d_str, []))
            btn_clear_day.set_label(f"🗑 清空當日任務（{len(p_list) + len(c_list)} 筆）")

            sub_parts = [f"待辦 <b>{len(p_list)}</b> 筆 · 已完成 <b>{len(c_list)}</b> 筆"]
            daily_map = (self.weather_data or {}).get("daily_by_date") or {}
            day_fc = daily_map.get(d_str)
            if isinstance(day_fc, dict):
                tmax = day_fc.get("t_max")
                tmin = day_fc.get("t_min")
                pop = day_fc.get("precip_prob")
                w_bits = []
                if tmax is not None and tmin is not None:
                    w_bits.append(f"{int(round(float(tmax)))}°/{int(round(float(tmin)))}°")
                if pop is not None:
                    w_bits.append(f"降雨 {int(round(float(pop)))}%")
                if w_bits:
                    sub_parts.append("天氣：" + " · ".join(w_bits))
            lbl_sub.set_markup(f"<span size='{meta_sz}'>{'   |   '.join(sub_parts)}</span>")

            for ch in list_box.get_children():
                list_box.remove(ch)

            def _add_card(task, is_comp):
                uid = str(task.get("uuid") or "")
                accent = self._get_task_accent_hex(task, is_comp=is_comp)
                frame = Gtk.Frame()
                frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                hbox.set_margin_top(8)
                hbox.set_margin_bottom(8)
                hbox.set_margin_start(8)
                hbox.set_margin_end(8)

                # Vertical color bar
                bar = Gtk.DrawingArea()
                bar.set_size_request(5, -1)
                bar_rgb = hex_to_rgb(accent, default=(0.15, 0.39, 0.92))

                def _draw_bar(w_bar, ctx, rgb=bar_rgb):
                    a = w_bar.get_allocation()
                    draw_rounded_rect(ctx, 0, 0, max(2.0, float(a.width)), max(2.0, float(a.height)), 2.5)
                    ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.95)
                    ctx.fill()
                    return False

                bar.connect("draw", _draw_bar)
                hbox.pack_start(bar, False, False, 0)

                # Main text body
                vcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                desc_esc = html.escape(str(task.get("description", "")))
                pri = str(task.get("priority") or "")
                pri_badge = f"<span foreground='{accent}'><b>[{html.escape(pri)}]</b></span> " if pri else ""
                status_txt = "已完成" if is_comp else ("逾期" if task.get("_due_level") == "crit" else "待辦")
                title_lbl = Gtk.Label(xalign=0.0)
                title_lbl.set_selectable(True)
                title_lbl.set_line_wrap(True)
                if is_comp:
                    title_lbl.set_markup(
                        f"<span size='{body_sz}'><s>{desc_esc}</s></span>  "
                        f"<span size='{meta_sz}' foreground='#10b981'>[{status_txt}]</span>"
                    )
                else:
                    title_lbl.set_markup(
                        f"<span size='{body_sz}'>{pri_badge}<b>{desc_esc}</b></span>  "
                        f"<span size='{meta_sz}' alpha='70%'>[{status_txt}]</span>"
                    )
                vcol.pack_start(title_lbl, False, False, 0)

                meta = []
                proj = str(task.get("project") or "").strip()
                if proj:
                    meta.append(f"專案：{proj}")
                tags = task.get("tags") or []
                if tags:
                    meta.append("標籤：" + " ".join(f"+{t}" for t in tags))
                due_s = self._format_tw_dt(task.get("due"))
                if due_s:
                    meta.append(f"到期：{due_s}")
                entry_s = self._format_tw_dt(task.get("entry"))
                if entry_s:
                    meta.append(f"建立：{entry_s}")
                if is_comp:
                    end_s = self._format_tw_dt(task.get("end"))
                    if end_s:
                        meta.append(f"完成：{end_s}")
                urg = task.get("urgency")
                if urg is not None and not is_comp:
                    try:
                        meta.append(f"緊急度：{float(urg):.1f}")
                    except (TypeError, ValueError):
                        pass
                if meta:
                    meta_lbl = Gtk.Label(xalign=0.0)
                    meta_lbl.set_selectable(True)
                    meta_lbl.set_line_wrap(True)
                    meta_lbl.set_markup(f"<span size='{meta_sz}' alpha='78%'>{html.escape('  |  '.join(meta))}</span>")
                    vcol.pack_start(meta_lbl, False, False, 0)

                anns = task.get("annotations") or []
                if isinstance(anns, list) and anns:
                    ann_lines = []
                    for a in anns:
                        if not isinstance(a, dict):
                            continue
                        a_ts = self._format_tw_dt(a.get("entry"))
                        a_desc = str(a.get("description") or "").strip()
                        if a_desc:
                            ann_lines.append(f"  • [{a_ts}] {a_desc}" if a_ts else f"  • {a_desc}")
                    if ann_lines:
                        ann_lbl = Gtk.Label(xalign=0.0)
                        ann_lbl.set_selectable(True)
                        ann_lbl.set_line_wrap(True)
                        ann_lbl.set_markup(
                            f"<span size='{meta_sz}'><b>📝 註解：</b>\n{html.escape(chr(10).join(ann_lines))}</span>"
                        )
                        vcol.pack_start(ann_lbl, False, False, 0)

                hbox.pack_start(vcol, True, True, 0)

                # Action buttons
                act_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
                act_box.set_valign(Gtk.Align.START)

                btn_done = Gtk.Button(label="↩" if is_comp else "✓")
                btn_done.set_tooltip_text("恢復為待辦" if is_comp else "標記為完成")
                btn_done.set_relief(Gtk.ReliefStyle.NONE)
                if is_comp:
                    btn_done.connect("clicked", lambda *_, u=uid: (TaskwarriorBackend.restore_task(u), self.reload_tasks(), rebuild()))
                else:
                    btn_done.connect("clicked", lambda *_, u=uid: (TaskwarriorBackend.mark_done(u), self.reload_tasks(), rebuild()))

                btn_edit = Gtk.Button(label="✎")
                btn_edit.set_tooltip_text("編輯任務與顏色")
                btn_edit.set_relief(Gtk.ReliefStyle.NONE)
                btn_edit.connect("clicked", lambda *_, tk=task, ic=is_comp: (self._open_edit_dialog(tk, is_completed=ic), rebuild()))

                btn_del = Gtk.Button(label="🗑")
                btn_del.set_tooltip_text("刪除任務")
                btn_del.set_relief(Gtk.ReliefStyle.NONE)
                btn_del.connect("clicked", lambda *_, u=uid: (TaskwarriorBackend.delete_task(u), self.reload_tasks(), rebuild()))

                act_box.pack_start(btn_done, False, False, 0)
                act_box.pack_start(btn_edit, False, False, 0)
                act_box.pack_start(btn_del, False, False, 0)
                hbox.pack_end(act_box, False, False, 0)

                frame.add(hbox)
                list_box.pack_start(frame, False, False, 0)

            if not p_list and not c_list:
                empty_lbl = Gtk.Label(xalign=0.5)
                empty_lbl.set_margin_top(30)
                empty_lbl.set_markup(f"<span size='{body_sz}' alpha='65%'>這一天目前沒有任何待辦或已完成任務。</span>")
                list_box.pack_start(empty_lbl, False, False, 0)
            else:
                if p_list:
                    sh1 = Gtk.Label(xalign=0.0)
                    sh1.set_markup(f"<span size='{body_sz}'><b>📌 待辦任務（{len(p_list)}）</b></span>")
                    list_box.pack_start(sh1, False, False, 0)
                    for t in p_list:
                        _add_card(t, False)
                if c_list:
                    sh2 = Gtk.Label(xalign=0.0)
                    sh2.set_margin_top(6 if p_list else 0)
                    sh2.set_markup(f"<span size='{body_sz}'><b>✅ 已完成任務（{len(c_list)}）</b></span>")
                    list_box.pack_start(sh2, False, False, 0)
                    for t in c_list:
                        _add_card(t, True)

            list_box.show_all()

        btn_prev.connect("clicked", lambda *_: (state.__setitem__("date", state["date"] - timedelta(days=1)), rebuild()))
        btn_next.connect("clicked", lambda *_: (state.__setitem__("date", state["date"] + timedelta(days=1)), rebuild()))
        btn_add.connect("clicked", lambda *_: (self._open_add_dialog(state["date"]), rebuild()))
        btn_clear_day.connect("clicked", lambda *_: (self._clear_date_tasks(state["date"]), rebuild()))

        self._detail_rebuilders.append(rebuild)
        win.connect("destroy", lambda *_: self._detail_rebuilders.remove(rebuild) if rebuild in self._detail_rebuilders else None)

        win.add(outer)
        rebuild()
        win.show_all()

    def _clear_date_tasks(self, d_obj):
        """Clear all pending & completed tasks for a specific date after explicit user confirmation."""
        d_str = d_obj.strftime("%Y-%m-%d")
        p_list = list(self._by_date_pending.get(d_str, []))
        c_list = list(self._by_date_completed.get(d_str, []))
        all_items = p_list + c_list
        uuids = [str(t.get("uuid") or "").strip() for t in all_items if t.get("uuid")]
        if not uuids:
            self._show_info_dialog(f"{d_str} 沒有任務", f"{d_str} 目前沒有任何待辦或已完成任務可清空。")
            return

        md = Gtk.MessageDialog(
            transient_for=None,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"確定要清空 {d_str} 的全部任務嗎？",
        )
        md.format_secondary_text(
            f"即將刪除 {d_str} 共 {len(uuids)} 筆任務（待辦 {len(p_list)} 筆、已完成 {len(c_list)} 筆）。\n"
            f"此操作將透過 Taskwarrior 永久移除該日任務，請確認是否繼續。"
        )
        md.add_button("取消", Gtk.ResponseType.CANCEL)
        md.add_button("確認清空", Gtk.ResponseType.OK)
        md.set_default_response(Gtk.ResponseType.CANCEL)
        md.set_position(Gtk.WindowPosition.CENTER)
        md.set_keep_above(True)
        resp = md.run()
        md.destroy()
        if resp != Gtk.ResponseType.OK:
            return

        if TaskwarriorBackend.is_available():
            try:
                subprocess.run(
                    [
                        "task",
                        "rc.confirmation=no",
                        "rc.recurrence.confirmation=no",
                        "rc.bulk=0",
                        "rc.verbose=nothing",
                        ",".join(uuids),
                        "delete",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
                subprocess.run(
                    [
                        "task",
                        "rc.confirmation=no",
                        "rc.bulk=0",
                        "rc.verbose=nothing",
                        ",".join(uuids),
                        "purge",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except Exception:
                pass

        color_changed = False
        for uid in uuids:
            if uid in self.task_colors:
                self.task_colors.pop(uid, None)
                color_changed = True
        if color_changed:
            self._save_task_colors()
        self.reload_tasks()

    def _show_context_menu(self, event, clicked_date=None):
        menu = Gtk.Menu()
        add_date = clicked_date or date.today()
        add_lbl = f"＋ 新增任務 ({add_date.strftime('%m/%d')})..." if clicked_date else "＋ 新增任務..."

        if clicked_date is not None:
            d_str = clicked_date.strftime("%Y-%m-%d")
            d_lbl = clicked_date.strftime("%m/%d")
            d_fs_f, d_cols, _has_mfs = self._get_date_cell_settings(d_str)
            d_fs = int(round(d_fs_f))
            has_ov = isinstance(self.config.get("date_cell_overrides"), dict) and d_str in self.config["date_cell_overrides"]
            n_day_tasks = len(self._by_date_pending.get(d_str, [])) + len(self._by_date_completed.get(d_str, []))
            layout_items = [
                (
                    f"✓ {d_lbl} 垂直分割成兩列（切回單欄）" if d_cols >= 2 else f"◫ {d_lbl} 任務垂直分割成兩列",
                    lambda *_, ds=d_str, nc=(1 if d_cols >= 2 else 2): self._set_date_cell_override(ds, "task_columns", nc),
                ),
                (
                    f"A+ 放大 {d_lbl} 任務字型（目前 {d_fs} pt）",
                    lambda *_, ds=d_str, nfs=min(28, d_fs + 1): self._set_date_cell_override(ds, "task_font_size", nfs),
                ),
                (
                    f"A- 縮小 {d_lbl} 任務字型（目前 {d_fs} pt）",
                    lambda *_, ds=d_str, nfs=max(6, d_fs - 1): self._set_date_cell_override(ds, "task_font_size", nfs),
                ),
            ]
            if has_ov:
                layout_items.append(
                    (f"↺ 恢復 {d_lbl} 預設排版", lambda *_, ds=d_str: self._reset_date_cell_override(ds))
                )
            items = [
                (
                    f"📋 顯示 {d_lbl} 詳細任務內容（{n_day_tasks} 筆）...",
                    lambda *_, dobj=clicked_date: self._open_day_detail_window(dobj),
                ),
                (add_lbl, lambda *_: self._open_add_dialog(add_date)),
                (None, None),
                *layout_items,
                (None, None),
                (
                    f"🗑 清空 {d_lbl} 任務（{n_day_tasks} 筆）...",
                    lambda *_, dobj=clicked_date: self._clear_date_tasks(dobj),
                ),
                (None, None),
                ("🔄 立即同步 Taskwarrior", lambda *_: self.reload_tasks()),
                ("↩ 復原上一步 (Undo)", self._on_undo),
            ]
        else:
            is_two_col = int(self.config.get("task_columns", 1)) >= 2
            cur_tfs = max(6, min(28, int(self.config.get("task_font_size", 10))))
            items = [
                (add_lbl, lambda *_: self._open_add_dialog(add_date)),
                ("🔄 立即同步 Taskwarrior", lambda *_: self.reload_tasks()),
                ("↩ 復原上一步 (Undo)", self._on_undo),
                (None, None),
                ("✓ 全域預設雙欄（點擊切回單欄）" if is_two_col else "◫ 全域任務垂直分割成兩列", self._toggle_task_columns),
                (f"A+ 放大全域任務字型（目前 {cur_tfs} pt）", lambda *_: self._adjust_task_font_size(+1)),
                (f"A- 縮小全域任務字型（目前 {cur_tfs} pt）", lambda *_: self._adjust_task_font_size(-1)),
                (None, None),
                ("🔓 解鎖拖曳與縮放" if self.config.get("locked") else "🔒 鎖定桌面位置", self._on_toggle_lock),
                ("❐ 還原視窗大小" if self._is_maximized else "□ 放大至工作區全螢幕", self.toggle_maximize_workarea),
                ("— 縮小為桌面小圖示", self.minimize_to_icon),
                ("⚙ 外觀與位置設定...", self.show_preferences_dialog),
                ("📤 匯出備份...", self._open_export_dialog),
                ("📥 匯入還原...", self._open_import_dialog),
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

        win = Gtk.Window(title="Taskwarrior 桌面嵌入式月曆 — 偏好設定")
        win.set_default_size(520, 390)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_keep_above(True)
        self.prefs_win = win
        win.connect("destroy", lambda *_: setattr(self, "prefs_win", None))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(14)
        outer.set_margin_end(14)

        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 0)

        def make_tab(title):
            tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            tb.set_margin_top(12)
            tb.set_margin_bottom(12)
            tb.set_margin_start(14)
            tb.set_margin_end(14)
            notebook.append_page(tb, Gtk.Label(label=title))
            return tb

        tab_appear = make_tab("🎨 外觀與顏色")
        tab_layout = make_tab("📅 月曆與排版")
        tab_detail = make_tab("📋 詳細任務視窗")
        tab_sys = make_tab("⚙ 系統與備份")

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

        # --- Tab 1: 外觀與顏色 ---
        col_grid = Gtk.Grid(column_spacing=10, row_spacing=8)
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
        tab_appear.pack_start(col_grid, False, False, 0)

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
        tab_appear.pack_start(op_row1, False, False, 0)

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
        tab_appear.pack_start(op_row2, False, False, 0)

        chk_shadow = Gtk.CheckButton(label="文字陰影（透明背景時增強可讀性）")
        chk_shadow.set_active(self.config.get("text_shadow", True))
        tab_appear.pack_start(chk_shadow, False, False, 0)

        # --- Tab 2: 月曆與排版 ---
        fs_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fs_row.pack_start(Gtk.Label(label="介面字型 (pt)：", xalign=0.0), False, False, 0)
        spin_fs = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(float(self.config.get("font_size", 11)), 9.0, 24.0, 1.0, 2.0, 0.0),
            climb_rate=1.0,
            digits=0,
        )
        fs_row.pack_start(spin_fs, False, False, 0)
        fs_row.pack_start(Gtk.Label(label="全域任務字型 (pt)：", xalign=0.0), False, False, 6)
        spin_task_fs = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(float(self.config.get("task_font_size", 10)), 6.0, 28.0, 1.0, 2.0, 0.0),
            climb_rate=1.0,
            digits=0,
        )
        fs_row.pack_start(spin_task_fs, False, False, 0)
        tab_layout.pack_start(fs_row, False, False, 0)

        chk_comp = Gtk.CheckButton(label="在日期格子內顯示已完成任務")
        chk_comp.set_active(self.config.get("show_completed", True))
        tab_layout.pack_start(chk_comp, False, False, 0)

        chk_weather = Gtk.CheckButton(label="在日期列顯示 7 天天氣預報與今日 AQI 圓點（共用天氣小工具快取）")
        chk_weather.set_active(self.config.get("show_weather", True))
        tab_layout.pack_start(chk_weather, False, False, 0)

        chk_two_col = Gtk.CheckButton(label="全域預設：任務垂直分割成兩列顯示（容納更多任務）")
        chk_two_col.set_active(int(self.config.get("task_columns", 1)) >= 2)
        tab_layout.pack_start(chk_two_col, False, False, 0)

        chk_auto_shrink = Gtk.CheckButton(label="當任務超過日期框大小時，自動縮小任務字型以完整顯示所有任務")
        chk_auto_shrink.set_active(self.config.get("auto_shrink_task_font", True))
        tab_layout.pack_start(chk_auto_shrink, False, False, 0)

        ov_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_reset_ov = Gtk.Button(label="↺ 重置全部單日獨立排版")
        btn_reset_ov.set_tooltip_text("清除所有透過右鍵單獨設定的日期字型與雙欄排版，恢復全域預設")
        btn_reset_ov.connect("clicked", self._reset_all_date_overrides)
        ov_row.pack_start(btn_reset_ov, False, False, 0)
        tab_layout.pack_start(ov_row, False, False, 2)

        # --- Tab 3: 詳細任務視窗 ---
        dt_fs_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dt_fs_row.pack_start(Gtk.Label(label="詳細任務內容視窗字型大小 (pt)：", xalign=0.0), False, False, 0)
        spin_detail_fs = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(float(self.config.get("detail_font_size", 12)), 9.0, 24.0, 1.0, 2.0, 0.0),
            climb_rate=1.0,
            digits=0,
        )
        dt_fs_row.pack_start(spin_detail_fs, False, False, 0)
        tab_detail.pack_start(dt_fs_row, False, False, 0)

        lbl_dt_hint = Gtk.Label(xalign=0.0)
        lbl_dt_hint.set_line_wrap(True)
        lbl_dt_hint.set_markup(
            "<small><span alpha='75%'>調整此字型大小會即時套用於「詳細任務內容」視窗的標題、任務內文、中繼欄位與註解。\n"
            "在任一日期框上按右鍵點選「📋 顯示詳細任務內容...」即可開啟該視窗。</span></small>"
        )
        tab_detail.pack_start(lbl_dt_hint, False, False, 0)

        # --- Tab 4: 系統與備份 ---
        chk_lock = Gtk.CheckButton(label="🔒 鎖定桌面位置與大小（取消勾選即可拖曳頂部移動、右下角縮放）")
        chk_lock.set_active(self.config.get("locked", False))
        tab_sys.pack_start(chk_lock, False, False, 0)

        chk_autostart = Gtk.CheckButton(label="🚀 開機進入桌面時自動啟動桌面月曆")
        chk_autostart.set_active(is_autostart_enabled())
        chk_autostart.connect("toggled", lambda c: set_autostart_enabled(c.get_active()))
        tab_sys.pack_start(chk_autostart, False, False, 0)

        ret_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ret_row.pack_start(Gtk.Label(label="啟動時資料保留天數（0 = 不自動刪除）：", xalign=0.0), False, False, 0)
        spin_ret = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(float(max(0, int(self.config.get("retention_days", 180)))), 0.0, 3650.0, 10.0, 30.0, 0.0),
            climb_rate=1.0,
            digits=0,
        )
        spin_ret.set_tooltip_text("每次重啟 Widget 時，自動透過 Taskwarrior CLI 清除超過此天數的歷史任務與單日排版；設為 0 則完全停用")
        ret_row.pack_start(spin_ret, False, False, 0)
        tab_sys.pack_start(ret_row, False, False, 0)

        chk_auto_backup = Gtk.CheckButton(
            label="💾 每日自動備份任務與設定（保留最近 3 天，自動刪除最舊備份）"
        )
        chk_auto_backup.set_tooltip_text(f"備份存放位置：{BACKUP_DIR}")
        chk_auto_backup.set_active(self.config.get("auto_backup_enabled", True))
        tab_sys.pack_start(chk_auto_backup, False, False, 0)

        bk_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_export = Gtk.Button(label="📤 匯出備份...")
        btn_export.connect("clicked", self._open_export_dialog)
        btn_import = Gtk.Button(label="📥 匯入還原...")
        btn_import.connect("clicked", self._open_import_dialog)
        bk_row.pack_start(btn_export, False, False, 0)
        bk_row.pack_start(btn_import, False, False, 0)
        tab_sys.pack_start(bk_row, False, False, 4)

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
            self.config["task_font_size"] = int(spin_task_fs.get_value_as_int())
            self.config["detail_font_size"] = int(spin_detail_fs.get_value_as_int())
            self.config["task_columns"] = 2 if chk_two_col.get_active() else 1
            self.config["auto_shrink_task_font"] = chk_auto_shrink.get_active()
            self.config["retention_days"] = max(0, int(spin_ret.get_value_as_int()))
            self.config["auto_backup_enabled"] = chk_auto_backup.get_active()
            self.config["text_shadow"] = chk_shadow.get_active()
            self.config["show_completed"] = chk_comp.get_active()
            self.config["show_weather"] = chk_weather.get_active()
            self.config["locked"] = chk_lock.get_active()
            self._save_config()
            self._refresh_view()
            for rb in list(self._detail_rebuilders):
                try:
                    rb()
                except Exception:
                    pass

        for cb in (bg_btn, fg_btn, today_bg_btn, today_bd_btn, cur_m_bg_btn, other_m_bg_btn):
            cb.connect("color-set", apply_all)
        scale_bg_op.connect("value-changed", apply_all)
        scale_cell_op.connect("value-changed", apply_all)
        spin_fs.connect("value-changed", apply_all)
        spin_task_fs.connect("value-changed", apply_all)
        spin_detail_fs.connect("value-changed", apply_all)
        spin_ret.connect("value-changed", apply_all)
        for c in (chk_shadow, chk_comp, chk_two_col, chk_auto_shrink, chk_weather, chk_lock, chk_auto_backup):
            c.connect("toggled", apply_all)

        btm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_exp_pref = Gtk.Button(label="📤 匯出設定...")
        btn_exp_pref.set_tooltip_text("僅匯出偏好設定參數（不含任務資料與單日日期框樣式覆寫）")
        btn_imp_pref = Gtk.Button(label="📥 匯入設定...")
        btn_imp_pref.set_tooltip_text("僅匯入偏好設定參數（保留現有任務資料與單日日期框樣式覆寫）")
        btn_close = Gtk.Button(label="關閉")
        btn_close.connect("clicked", lambda *_: win.destroy())

        pref_exclude_keys = {"date_cell_overrides"}

        def on_export_prefs(*_):
            apply_all()
            today_str = datetime.now().strftime("%Y%m%d")
            dlg = Gtk.FileChooserDialog(
                title="匯出桌面月曆偏好設定檔（僅偏好設定）",
                parent=win,
                action=Gtk.FileChooserAction.SAVE,
            )
            dlg.set_do_overwrite_confirmation(True)
            dlg.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
            )
            flt = Gtk.FileFilter()
            flt.set_name("JSON 設定檔 (*.json)")
            flt.add_pattern("*.json")
            dlg.add_filter(flt)
            dlg.set_current_folder(os.path.expanduser("~"))
            dlg.set_current_name(f"taskwarrior-desktop-prefs-{today_str}.json")
            resp = dlg.run()
            out_path = dlg.get_filename() if resp == Gtk.ResponseType.OK else None
            dlg.destroy()
            if not out_path:
                return
            if not out_path.lower().endswith(".json"):
                out_path += ".json"

            pref_only = {
                k: v for k, v in self.config.items()
                if k in DEFAULT_CONFIG and k not in pref_exclude_keys
            }
            bundle = {
                "format": "matepanel-applet-plus.taskwarrior-desktop.prefs",
                "version": 1,
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "config": pref_only,
            }
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(bundle, f, indent=2, ensure_ascii=False)
                md = Gtk.MessageDialog(
                    transient_for=win,
                    flags=0,
                    message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="✅ 已成功匯出偏好設定",
                )
                md.format_secondary_text(f"已儲存 {len(pref_only)} 項偏好參數至：{out_path}\n（不含任務與單日日期框樣式）")
                md.run()
                md.destroy()
            except Exception as exc:
                md = Gtk.MessageDialog(
                    transient_for=win,
                    flags=0,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="❌ 匯出偏好設定失敗",
                )
                md.format_secondary_text(str(exc))
                md.run()
                md.destroy()

        def on_import_prefs(*_):
            dlg = Gtk.FileChooserDialog(
                title="匯入桌面月曆偏好設定檔（僅偏好設定）",
                parent=win,
                action=Gtk.FileChooserAction.OPEN,
            )
            dlg.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
            )
            flt = Gtk.FileFilter()
            flt.set_name("JSON 設定檔 (*.json)")
            flt.add_pattern("*.json")
            dlg.add_filter(flt)
            dlg.set_current_folder(os.path.expanduser("~"))
            resp = dlg.run()
            in_path = dlg.get_filename() if resp == Gtk.ResponseType.OK else None
            dlg.destroy()
            if not in_path:
                return
            try:
                with open(in_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("檔案格式無效（非 JSON 物件）")
                if raw.get("format") == "matepanel-applet-plus.taskwarrior-desktop.prefs" and isinstance(raw.get("config"), dict):
                    imp_cfg = raw["config"]
                elif isinstance(raw.get("desktop_config"), dict):
                    imp_cfg = raw["desktop_config"]
                else:
                    imp_cfg = raw
                valid_keys = set(DEFAULT_CONFIG.keys()) - pref_exclude_keys
                matched = {k: v for k, v in imp_cfg.items() if k in valid_keys}
                if not matched:
                    raise ValueError("找不到任何相容的桌面月曆偏好設定欄位")
                # Preserve existing date_cell_overrides untouched
                saved_ov = dict(self.config.get("date_cell_overrides") or {})
                new_cfg = dict(DEFAULT_CONFIG)
                new_cfg.update(self.config)
                new_cfg.update(matched)
                new_cfg["date_cell_overrides"] = saved_ov
                self.config = new_cfg
                self._save_config()
                self._refresh_view()
                for rb in list(self._detail_rebuilders):
                    try:
                        rb()
                    except Exception:
                        pass
                win.destroy()
                self.show_preferences_dialog()
                md = Gtk.MessageDialog(
                    transient_for=None,
                    flags=0,
                    message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="✅ 已成功匯入偏好設定",
                )
                md.format_secondary_text(
                    f"已套用來自「{os.path.basename(in_path)}」的 {len(matched)} 項偏好參數（已保留既有任務與單日日期框樣式）。"
                )
                md.run()
                md.destroy()
            except Exception as exc:
                md = Gtk.MessageDialog(
                    transient_for=win,
                    flags=0,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="❌ 匯入偏好設定失敗",
                )
                md.format_secondary_text(str(exc))
                md.run()
                md.destroy()

        btn_exp_pref.connect("clicked", on_export_prefs)
        btn_imp_pref.connect("clicked", on_import_prefs)
        btm_row.pack_start(btn_exp_pref, False, False, 0)
        btm_row.pack_start(btn_imp_pref, False, False, 0)
        btm_row.pack_end(btn_close, False, False, 0)
        outer.pack_end(btm_row, False, False, 0)

        win.add(outer)
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
        if self._backup_timer_id is not None:
            try:
                GLib.source_remove(self._backup_timer_id)
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
