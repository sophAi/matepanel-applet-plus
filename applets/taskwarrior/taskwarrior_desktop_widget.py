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

DEFAULT_CONFIG = {
    "x": 80,
    "y": 60,
    "width": 940,
    "height": 620,
    "locked": False,
    "bg_color": "#0f172a",
    "bg_opacity": 58,           # 0..100% 整體背景透明度
    "cell_opacity": 42,         # 0..100% 日期格子底色透明度
    "fg_color": "#f1f5f9",      # 文字顏色
    "text_shadow": True,        # 透明背景時文字陰影增強可讀性
    "font_size": 11,            # 9..18
    "show_completed": True,     # 在日期格子內顯示已完成任務
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


def draw_rounded_rect(ctx, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


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
        self._geom_save_id = None

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

    def _on_configure(self, _w, event):
        if self._geom_save_id is not None:
            try:
                GLib.source_remove(self._geom_save_id)
            except Exception:
                pass

        def _commit():
            self._geom_save_id = None
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

        self._nav_buttons = [
            self.btn_prev, self.btn_today, self.btn_next,
            self.btn_unsched, self.btn_add, self.btn_undo, self.btn_lock, self.btn_prefs,
        ]

        nav.pack_start(self.btn_prev, False, False, 0)
        nav.pack_start(self.btn_today, False, False, 0)
        nav.pack_start(self.btn_next, False, False, 0)
        nav.pack_start(self.month_lbl, True, True, 0)
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
        cell_a = max(0.0, min(1.0, float(self.config.get("cell_opacity", 42)) / 100.0))
        base_fs = float(max(9, min(18, int(self.config.get("font_size", 11)))))
        show_comp = bool(self.config.get("show_completed", True))

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
                ctx.set_source_rgba(0.10, 0.36, 0.30, max(0.28, cell_a * 0.95))
            elif is_cur_m:
                ctx.set_source_rgba(0.08, 0.12, 0.18, cell_a)
            else:
                ctx.set_source_rgba(0.05, 0.08, 0.12, cell_a * 0.45)
            ctx.fill_preserve()

            if is_today:
                ctx.set_source_rgba(0.22, 0.88, 0.65, 0.95)
                ctx.set_line_width(1.6)
            else:
                ctx.set_source_rgba(fg_rgb[0], fg_rgb[1], fg_rgb[2], 0.16 if is_cur_m else 0.07)
                ctx.set_line_width(0.8)
            ctx.stroke()

            # Top row inside cell: Day number (left) + count badge (right)
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
            self._draw_text(ctx, cx + 5.0, cy + base_fs + 2.5, str(d_obj.day), d_rgb, d_a)

            total_items = len(p_list) + len(c_list)
            if total_items > 0:
                cnt_s = str(len(p_list)) if p_list else f"✓{len(c_list)}"
                ctx.set_font_size(max(8.0, base_fs - 2.0))
                cext = ctx.text_extents(cnt_s)
                c_rgb = (0.38, 0.80, 1.0) if p_list else (0.30, 0.84, 0.58)
                self._draw_text(ctx, cx + cw - cext.width - 5.0 - cext.x_bearing, cy + base_fs + 2.0, cnt_s, c_rgb, 0.90)

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
                if is_comp:
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
                ctx.set_source_rgba(1.0, 1.0, 1.0, 0.78 if is_comp else 0.98)
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

        for ri, (lt, wc) in enumerate(
            [("任務標題：", e_desc), ("專案：", e_proj), ("優先級：", c_pri), ("標籤：", e_tags), ("到期日：", e_due)]
        ):
            grid.attach(Gtk.Label(label=lt, xalign=0.0), 0, ri, 1, 1)
            grid.attach(wc, 1, ri, 1, 1)

        area.pack_start(grid, True, True, 0)
        dlg.show_all()
        e_desc.grab_focus()
        if dlg.run() == Gtk.ResponseType.OK:
            desc = e_desc.get_text().strip()
            if desc:
                tags = [t.strip().lstrip("+") for t in e_tags.get_text().replace(",", " ").split() if t.strip()]
                TaskwarriorBackend.add_task_structured(
                    description=desc,
                    project=e_proj.get_text().strip(),
                    priority=c_pri.get_active_id() or "",
                    tags=tags,
                    due=e_due.get_text().strip(),
                )
                self.reload_tasks()
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

        for ri, (lt, wc) in enumerate(
            [("任務標題：", e_desc), ("專案：", e_proj), ("優先級：", c_pri), ("標籤：", e_tags), ("到期日：", e_due)]
        ):
            grid.attach(Gtk.Label(label=lt, xalign=0.0), 0, ri, 1, 1)
            grid.attach(wc, 1, ri, 1, 1)

        area.pack_start(grid, True, True, 0)
        dlg.show_all()
        uuid_str = task.get("uuid", "")
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            desc = e_desc.get_text().strip()
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

        col_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bg_btn = make_cbtn("bg_color", "#0f172a")
        fg_btn = make_cbtn("fg_color", "#f1f5f9")
        col_row.pack_start(Gtk.Label(label="背景底色：", xalign=0.0), False, False, 0)
        col_row.pack_start(bg_btn, False, False, 0)
        col_row.pack_start(Gtk.Label(label="文字顏色：", xalign=0.0), False, False, 0)
        col_row.pack_start(fg_btn, False, False, 0)
        box.pack_start(col_row, False, False, 0)

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

        chk_lock = Gtk.CheckButton(label="🔒 鎖定桌面位置與大小（取消勾選即可拖曳頂部移動、右下角縮放）")
        chk_lock.set_active(self.config.get("locked", False))
        box.pack_start(chk_lock, False, False, 0)

        def apply_all(*_):
            self.config["bg_color"] = rgba_to_hex(bg_btn.get_rgba())
            self.config["fg_color"] = rgba_to_hex(fg_btn.get_rgba())
            bo = int(round(scale_bg_op.get_value()))
            co = int(round(scale_cell_op.get_value()))
            self.config["bg_opacity"] = bo
            self.config["cell_opacity"] = co
            lbl_bg_op.set_text(f"{bo}%")
            lbl_cell_op.set_text(f"{co}%")
            self.config["font_size"] = int(spin_fs.get_value_as_int())
            self.config["text_shadow"] = chk_shadow.get_active()
            self.config["show_completed"] = chk_comp.get_active()
            self.config["locked"] = chk_lock.get_active()
            self._save_config()
            self._refresh_view()

        bg_btn.connect("color-set", apply_all)
        fg_btn.connect("color-set", apply_all)
        scale_bg_op.connect("value-changed", apply_all)
        scale_cell_op.connect("value-changed", apply_all)
        spin_fs.connect("value-changed", apply_all)
        for c in (chk_shadow, chk_comp, chk_lock):
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
        Gtk.main_quit()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    TaskwarriorDesktopCalendar()
    Gtk.main()


if __name__ == "__main__":
    main()
