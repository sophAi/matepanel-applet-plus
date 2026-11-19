#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
MATE Panel Taskwarrior Task Manager Applet (TaskwarriorApplet)

Features:
- Event-driven synchronization with ~/.task directory using Gio.FileMonitor (0% idle polling overhead)
  plus periodic 60s due-time recalculation.
- Two switchable panel display modes:
  1. 'badge'      : Compact rounded badge [TASK / count] with optional active-task indicator dot
  2. 'badge_text' : Badge + top urgency task summary [TASK / count] [H] 加強mate-panel功能 (4d)
- Customizable 3-stage panel font colors (Normal / Warning: High Priority or Due Soon / Critical: Overdue or Due Today),
  badge background style ('dark', 'light', 'transparent'), and text contrast outline shadow.
- Dual-column Interactive Monthly Calendar Dashboard (`TaskCalendarWindow`):
  - Left column: Custom 7x6 color-coded monthly calendar grid showing:
    - Pending tasks due on each date (color dots/count badges by priority & urgency + mini task title preview)
    - Historical completed tasks (`status:completed` mapped by `end` completion date, toggleable via checkbox)
    - Single-click a date to inspect its due/completed tasks on the right panel
    - Double-click any date cell to immediately add a new task with `Due` pre-filled to that date
  - Right column:
    - Selected Date tasks list (Due pending tasks + Completed tasks on that day) with one-click Done, Start/Stop timer,
      Reschedule (`+1d`, `+3d`, or assign to selected date), Edit, Annotate, Delete, or Restore to Pending
    - Unscheduled / All Pending tasks list (tasks without `due` date or all pending tasks, with one-click
      "📅 指派至選取日期" button)
- Switchable Left-Click default action ('calendar' vs 'menu') in Preferences, while both are always accessible.
"""

import calendar
from datetime import date, datetime, timedelta, timezone
import html
import json
import math
import os
import subprocess
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango
import cairo

try:
    gi.require_version("MatePanelApplet", "4.0")
    from gi.repository import MatePanelApplet
    HAS_MATE_PANEL = True
except (ValueError, ImportError):
    HAS_MATE_PANEL = False


CONFIG_DIR = os.path.expanduser("~/.config/matepanel-applet-plus")
CONFIG_PATH = os.path.join(CONFIG_DIR, "taskwarrior.json")
TASK_DATA_DIR = os.path.expanduser("~/.task")

DEFAULT_CONFIG = {
    "display_mode": "badge",             # "badge" (僅方塊圖示) or "badge_text" (方塊 + 首要任務標題)
    "left_click_action": "calendar",     # "calendar" (開啟雙欄月曆任務看板) or "menu" (開啟傳統下拉快速選單)
    "calendar_show_completed": True,     # 在月曆格子與右側清單中顯示歷史已完成任務 (依 end 完成日期對應)
    "calendar_font_size": 11,            # 月曆看板基準字型大小 (pt/px, 範圍 8..20，同步縮放月曆格子與右側清單)
    "max_title_chars": 22,               # badge_text 模式下顯示的最大字元數
    "show_due_in_panel": True,           # badge_text 模式下是否在標題後附加上到期倒數
    "filter_project": "",                # 空字串代表顯示全部專案
    "filter_tag": "",                    # 空字串代表顯示全部標籤
    "due_soon_days": 3,                  # 幾天內到期視為「中度警戒 (Warning)」
    "font_color_mode": "dynamic",        # "dynamic" (預設內建色階) or "custom" (自訂三段式字形顏色)
    "custom_font_color": "#ffffff",      # 自訂「一般待辦」字形顏色
    "custom_warn_font_color": "#fbbf24", # 自訂「高優先級 / 即將到期」字形顏色
    "custom_crit_font_color": "#f87171", # 自訂「已逾期 / 今日到期」字形顏色
    "badge_bg_style": "dark",            # "dark", "light", "transparent"
    "text_shadow": False,                # 文字高對比描邊陰影
    "completed_limit": 20,               # 下拉選單最近已完成任務顯示筆數
}


def parse_tw_timestamp(ts_str):
    """Parse Taskwarrior ISO-8601 UTC timestamp like '20261122T160000Z' into local datetime."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        dt_utc = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone()
    except ValueError:
        return None


def format_due_status(due_str, due_soon_days=3):
    """
    Analyze a Taskwarrior `due` timestamp string.
    Returns (due_date_str, relative_str, severity_level)
    severity_level: 'normal', 'warn', or 'crit'
    """
    dt_local = parse_tw_timestamp(due_str)
    if dt_local is None:
        return ("", "", "normal")

    now = datetime.now().astimezone()
    date_str = dt_local.strftime("%Y-%m-%d")
    delta_sec = (dt_local - now).total_seconds()
    delta_days = delta_sec / 86400.0

    if delta_sec < 0:
        abs_days = abs(int(round(delta_days)))
        rel = "已逾期 (<1天)" if abs_days == 0 else f"已逾期 {abs_days} 天"
        return (date_str, rel, "crit")

    if dt_local.date() == now.date():
        hours_left = max(0, int(delta_sec // 3600))
        return (date_str, f"今日到期 ({hours_left}h)", "crit")

    days_left = (dt_local.date() - now.date()).days
    rel = "明天到期" if days_left == 1 else f"剩 {days_left} 天"
    lvl = "warn" if days_left <= due_soon_days else "normal"
    return (date_str, rel, lvl)


class TaskwarriorBackend:
    """Wrapper around `/usr/bin/task` CLI for zero-confirmation JSON export and task mutations."""

    TASK_BIN = "/usr/bin/task"

    @classmethod
    def _run_task(cls, args, timeout=5.0):
        cmd = [
            cls.TASK_BIN,
            "rc.confirmation=off",
            "rc.verbose=nothing",
            "rc.hooks=on",
        ] + list(args)
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except Exception as e:
            return 1, "", str(e)

    @classmethod
    def fetch_all(cls, due_soon_days=3, completed_limit=20):
        """
        Export all tasks from Taskwarrior and organize into:
        {
            "pending": [ ... sorted by urgency desc ... ],
            "completed": [ ... most recent completed_limit tasks ... ],
            "all_completed": [ ... all completed tasks with _end_date for calendar mapping ... ],
            "projects": [ ... all known project names ... ],
            "tags": [ ... all known user tags ... ],
        }
        """
        rc, out, _ = cls._run_task(["export"])
        if rc != 0 or not out:
            return {
                "pending": [],
                "completed": [],
                "all_completed": [],
                "projects": [],
                "tags": [],
            }

        try:
            raw_tasks = json.loads(out)
        except Exception:
            return {
                "pending": [],
                "completed": [],
                "all_completed": [],
                "projects": [],
                "tags": [],
            }

        pending = []
        all_completed = []
        projects_set = set()
        tags_set = set()

        for t in raw_tasks:
            proj = t.get("project")
            if proj:
                projects_set.add(proj)
            for tg in t.get("tags", []):
                tags_set.add(tg)

            status = t.get("status", "")
            if status == "pending":
                due_date, due_rel, due_lvl = format_due_status(t.get("due"), due_soon_days)
                t["_due_date"] = due_date
                t["_due_rel"] = due_rel
                t["_due_level"] = due_lvl
                t["_is_active"] = bool(t.get("start"))
                pending.append(t)
            elif status == "completed":
                end_dt = parse_tw_timestamp(t.get("end"))
                t["_end_date"] = end_dt.strftime("%Y-%m-%d") if end_dt else ""
                t["_end_time"] = end_dt.strftime("%H:%M") if end_dt else ""
                due_dt = parse_tw_timestamp(t.get("due"))
                t["_due_date"] = due_dt.strftime("%Y-%m-%d") if due_dt else ""
                all_completed.append(t)

        pending.sort(key=lambda x: (-float(x.get("urgency", 0.0)), int(x.get("id", 999999))))
        all_completed.sort(key=lambda x: x.get("end", ""), reverse=True)
        completed = all_completed[: max(1, int(completed_limit))]

        return {
            "pending": pending,
            "completed": completed,
            "all_completed": all_completed,
            "projects": sorted(projects_set),
            "tags": sorted(tags_set),
        }

    @classmethod
    def mark_done(cls, uuid_str):
        return cls._run_task([f"uuid:{uuid_str}", "done"])

    @classmethod
    def toggle_start_stop(cls, uuid_str, is_active):
        action = "stop" if is_active else "start"
        return cls._run_task([f"uuid:{uuid_str}", action])

    @classmethod
    def set_priority(cls, uuid_str, pri_code):
        return cls._run_task([f"uuid:{uuid_str}", "modify", f"priority:{pri_code}"])

    @classmethod
    def set_due_date(cls, uuid_str, due_str):
        return cls._run_task([f"uuid:{uuid_str}", "modify", f"due:{due_str}"])

    @classmethod
    def annotate_task(cls, uuid_str, text):
        if not text.strip():
            return 0, "", ""
        return cls._run_task([f"uuid:{uuid_str}", "annotate", text.strip()])

    @classmethod
    def delete_task(cls, uuid_str):
        return cls._run_task([f"uuid:{uuid_str}", "delete"])

    @classmethod
    def restore_task(cls, uuid_str):
        return cls._run_task([f"uuid:{uuid_str}", "modify", "status:pending"])

    @classmethod
    def undo_last(cls):
        return cls._run_task(["undo"])

    @classmethod
    def add_task_structured(cls, description, project="", priority="", tags=None, due=""):
        args = ["add", description.strip()]
        if project.strip():
            args.append(f"project:{project.strip()}")
        if priority in ("H", "M", "L"):
            args.append(f"priority:{priority}")
        if tags:
            for tg in tags:
                tg_clean = tg.strip().lstrip("+")
                if tg_clean:
                    args.append(f"+{tg_clean}")
        if due.strip():
            args.append(f"due:{due.strip()}")
        return cls._run_task(args)

    @classmethod
    def modify_task_structured(
        cls, uuid_str, description, project="", priority="", tags_str="", due="", orig_tags=None
    ):
        args = [f"uuid:{uuid_str}", "modify", description.strip()]
        args.append(f"project:{project.strip()}")
        args.append(f"priority:{priority if priority in ('H', 'M', 'L') else ''}")
        args.append(f"due:{due.strip()}")

        new_tags = {
            t.strip().lstrip("+")
            for t in tags_str.replace(",", " ").split()
            if t.strip().lstrip("+")
        }
        old_tags = set(orig_tags or [])
        for added in sorted(new_tags - old_tags):
            args.append(f"+{added}")
        for removed in sorted(old_tags - new_tags):
            args.append(f"-{removed}")

        return cls._run_task(args)


def hex_to_rgb(hex_str, default=(1.0, 1.0, 1.0)):
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
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def level_to_colors(level):
    """
    Return (bg_rgb, border_rgb, text_rgb, accent_hex, level) for task summary level:
    - 'zero'  : No pending tasks (slate/cool gray-teal)
    - 'normal': Normal pending tasks (emerald/cyan)
    - 'warn'  : Has High priority (H) or Due soon tasks (amber/orange)
    - 'crit'  : Has Overdue or Due Today tasks (crimson/red)
    """
    if level == "zero":
        return (
            (0.20, 0.24, 0.28),
            (0.45, 0.54, 0.62),
            (0.82, 0.86, 0.90),
            "#94a3b8",
            "normal",
        )
    elif level == "normal":
        return (
            (0.11, 0.35, 0.28),
            (0.20, 0.78, 0.56),
            (0.95, 1.00, 0.98),
            "#34d399",
            "normal",
        )
    elif level == "warn":
        return (
            (0.45, 0.28, 0.07),
            (0.96, 0.65, 0.14),
            (1.00, 0.98, 0.92),
            "#fbbf24",
            "warn",
        )
    else:  # 'crit'
        return (
            (0.48, 0.11, 0.14),
            (0.96, 0.28, 0.32),
            (1.00, 0.95, 0.95),
            "#f87171",
            "crit",
        )


def draw_rounded_rect(ctx, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


class TaskCalendarWindow(Gtk.Window):
    """
    Dual-column Interactive Monthly Calendar Dashboard for Taskwarrior:
    - Left Column : Custom Cairo-rendered 7x6 Monthly Calendar Grid with task badges, mini task titles,
                    and historical completed task markers.
    - Right Column:
      1. Tasks on the Selected Date (Due pending tasks + Completed tasks on that date) with direct action buttons.
      2. Unscheduled Pending Tasks (no due date) & All Pending Tasks with one-click "Assign to selected date".
    """

    WEEKDAYS_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

    def __init__(self, parent_widget):
        super().__init__(title="📅 Taskwarrior 月曆任務看板")
        self.parent_widget = parent_widget
        self.set_default_size(940, 580)
        self.set_position(Gtk.WindowPosition.CENTER)

        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.selected_date = today
        self.cell_dates = []  # 42 date objects for the 6x7 grid

        self._build_ui()
        self.refresh_view()

    def _build_ui(self):
        root_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root_vbox.set_margin_top(12)
        root_vbox.set_margin_bottom(12)
        root_vbox.set_margin_start(14)
        root_vbox.set_margin_end(14)

        # Top Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        btn_prev_m = Gtk.Button(label="◀ 上個月")
        btn_prev_m.connect("clicked", lambda *_: self._shift_month(-1))
        btn_today = Gtk.Button(label="📍 回到今天")
        btn_today.connect("clicked", lambda *_: self._jump_to_today())
        btn_next_m = Gtk.Button(label="下個月 ▶")
        btn_next_m.connect("clicked", lambda *_: self._shift_month(1))

        self.month_title_lbl = Gtk.Label()
        self.month_title_lbl.set_margin_start(10)
        self.month_title_lbl.set_margin_end(10)

        toolbar.pack_start(btn_prev_m, False, False, 0)
        toolbar.pack_start(btn_today, False, False, 0)
        toolbar.pack_start(btn_next_m, False, False, 0)
        toolbar.pack_start(self.month_title_lbl, False, False, 0)

        # Right side of toolbar: Show Completed toggle, Add Task, Undo, Preferences
        btn_prefs = Gtk.Button(label="⚙️ 偏好設定")
        btn_prefs.connect("clicked", lambda *_: self.parent_widget.show_preferences_dialog())
        toolbar.pack_end(btn_prefs, False, False, 0)

        btn_undo = Gtk.Button(label="↩️ 復原 (Undo)")
        btn_undo.set_tooltip_text("執行 task undo 復原上一步變更")
        btn_undo.connect(
            "clicked",
            lambda *_: self.parent_widget._run_and_refresh(TaskwarriorBackend.undo_last),
        )
        toolbar.pack_end(btn_undo, False, False, 0)

        btn_add_sel = Gtk.Button(label="➕ 在選取日新增任務")
        btn_add_sel.connect(
            "clicked",
            lambda *_: self.parent_widget.show_add_or_edit_task_dialog(
                task=None, default_due=self.selected_date.strftime("%Y-%m-%d")
            ),
        )
        toolbar.pack_end(btn_add_sel, False, False, 0)

        self.chk_show_comp = Gtk.CheckButton(label="在月曆顯示已完成紀錄 (✓)")
        self.chk_show_comp.set_active(
            self.parent_widget.config.get("calendar_show_completed", True)
        )
        self.chk_show_comp.connect("toggled", self._on_toggle_show_completed)
        toolbar.pack_end(self.chk_show_comp, False, False, 6)

        root_vbox.pack_start(toolbar, False, False, 0)
        root_vbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Main Dual-Column Paned Area
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(520)

        # Left Column: Custom Cairo 7x6 Calendar Canvas + Legend
        left_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_vbox.set_margin_end(8)

        self.cal_canvas = Gtk.DrawingArea()
        self.cal_canvas.set_size_request(500, 440)
        self.cal_canvas.set_hexpand(True)
        self.cal_canvas.set_vexpand(True)
        self.cal_canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.cal_canvas.connect("draw", self._on_draw_calendar)
        self.cal_canvas.connect("button-press-event", self._on_calendar_button_press)
        left_vbox.pack_start(self.cal_canvas, True, True, 0)

        legend_lbl = Gtk.Label(xalign=0.0)
        legend_lbl.set_markup(
            "<small>💡 <b>月曆圖例</b>： "
            "<span foreground='#f87171'>● 逾期/今日/高優先(H)</span>   "
            "<span foreground='#fbbf24'>● 中優先(M)/近期到期</span>   "
            "<span foreground='#38bdf8'>● 一般待辦</span>   "
            "<span foreground='#34d399'>✓ 已完成任務</span>   "
            "<i>(單擊選取日期，雙擊日期格直接新增任務)</i></small>"
        )
        left_vbox.pack_start(legend_lbl, False, False, 0)
        paned.pack1(left_vbox, resize=True, shrink=False)

        # Right Column: Selected Day Tasks + Unscheduled/All Pending Tasks
        right_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right_vbox.set_margin_start(8)

        # Top right: Selected date header + quick add
        sel_hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.sel_date_lbl = Gtk.Label(xalign=0.0)
        sel_hdr_box.pack_start(self.sel_date_lbl, True, True, 0)
        right_vbox.pack_start(sel_hdr_box, False, False, 0)

        # Scrolled list for Selected Date tasks
        sel_scroll = Gtk.ScrolledWindow()
        sel_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sel_scroll.set_min_content_height(210)
        sel_scroll.set_vexpand(True)
        self.sel_tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sel_scroll.add(self.sel_tasks_box)
        right_vbox.pack_start(sel_scroll, True, True, 0)

        right_vbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Bottom right: Unscheduled & Other Pending Tasks section
        self.unsched_hdr_lbl = Gtk.Label(xalign=0.0)
        right_vbox.pack_start(self.unsched_hdr_lbl, False, False, 0)

        unsched_scroll = Gtk.ScrolledWindow()
        unsched_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        unsched_scroll.set_min_content_height(175)
        unsched_scroll.set_vexpand(True)
        self.unsched_tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        unsched_scroll.add(self.unsched_tasks_box)
        right_vbox.pack_start(unsched_scroll, True, True, 0)

        paned.pack2(right_vbox, resize=True, shrink=False)
        root_vbox.pack_start(paned, True, True, 0)

        self.add(root_vbox)
        self.show_all()

    def _on_toggle_show_completed(self, chk):
        self.parent_widget.config["calendar_show_completed"] = chk.get_active()
        self.parent_widget._save_config()
        self.refresh_view()

    def _shift_month(self, delta_months):
        m = self.view_month + delta_months
        y = self.view_year
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        self.view_year = y
        self.view_month = m
        max_d = calendar.monthrange(y, m)[1]
        sel_d = min(self.selected_date.day, max_d)
        self.selected_date = date(y, m, sel_d)
        self.refresh_view()

    def _jump_to_today(self):
        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.selected_date = today
        self.refresh_view()

    def _compute_month_cells(self):
        cal = calendar.Calendar(firstweekday=0)  # Monday first
        days = list(cal.itermonthdates(self.view_year, self.view_month))
        while len(days) < 42:
            last = days[-1]
            days.append(last + timedelta(days=1))
        self.cell_dates = days[:42]

    def _group_tasks_by_date(self):
        """
        Return (due_map, completed_map, unscheduled_pending)
        due_map: {'YYYY-MM-DD': [pending_task, ...]}
        completed_map: {'YYYY-MM-DD': [completed_task, ...]}
        """
        due_map = {}
        completed_map = {}
        unscheduled = []

        for t in self.parent_widget.filtered_pending:
            d_str = t.get("_due_date", "")
            if d_str:
                due_map.setdefault(d_str, []).append(t)
            else:
                unscheduled.append(t)

        if self.parent_widget.config.get("calendar_show_completed", True):
            f_proj = self.parent_widget.config.get("filter_project", "").strip()
            f_tag = self.parent_widget.config.get("filter_tag", "").strip()
            for ct in self.parent_widget.task_data.get("all_completed", []):
                if f_proj and ct.get("project", "") != f_proj:
                    continue
                if f_tag and f_tag not in ct.get("tags", []):
                    continue
                # Map completed task by its completion date (_end_date), or _due_date fallback
                c_date = ct.get("_end_date") or ct.get("_due_date")
                if c_date:
                    completed_map.setdefault(c_date, []).append(ct)

        return due_map, completed_map, unscheduled

    def refresh_view(self):
        self._compute_month_cells()
        self.due_map, self.completed_map, self.unscheduled = self._group_tasks_by_date()

        # Update month header label
        ym_prefix = f"{self.view_year:04d}-{self.view_month:02d}"
        m_pending_cnt = sum(
            len(v) for k, v in self.due_map.items() if k.startswith(ym_prefix)
        )
        m_comp_cnt = sum(
            len(v) for k, v in self.completed_map.items() if k.startswith(ym_prefix)
        )
        base_fs = max(8, min(22, int(self.parent_widget.config.get("calendar_font_size", 11))))
        title_pt = int(round((base_fs + 3.5) * 1024))
        sub_pt = int(round(max(8.5, base_fs - 0.5) * 1024))
        self.month_title_lbl.set_markup(
            f"<span size='{title_pt}'><b>{self.view_year} 年 {self.view_month:02d} 月</b></span>   "
            f"<span size='{sub_pt}'><span foreground='#38bdf8'>本月排程待辦: {m_pending_cnt} 筆</span>"
            + (
                f"  |  <span foreground='#34d399'>本月完成: {m_comp_cnt} 筆</span></span>"
                if self.parent_widget.config.get("calendar_show_completed", True)
                else "</span>"
            )
        )

        self.cal_canvas.queue_draw()
        self._populate_right_panels()

    def _on_draw_calendar(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)

        base_fs = float(max(8, min(22, int(self.parent_widget.config.get("calendar_font_size", 11)))))
        scale = base_fs / 11.0

        # Dark slate theme background for calendar grid
        draw_rounded_rect(ctx, 0, 0, w, h, 6.0)
        ctx.set_source_rgba(0.11, 0.13, 0.16, 0.98)
        ctx.fill()

        header_h = max(26.0, 28.0 * scale)
        grid_y = header_h + 2.0
        grid_h = max(120.0, h - grid_y - 4.0)
        col_w = (w - 8.0) / 7.0
        row_h = grid_h / 6.0
        start_x = 4.0

        # Draw Weekday Headers
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(base_fs + 0.5)
        for col_idx, wd_name in enumerate(self.WEEKDAYS_ZH):
            cx = start_x + col_idx * col_w
            ext = ctx.text_extents(wd_name)
            if col_idx >= 5:
                ctx.set_source_rgba(0.96, 0.55, 0.45, 0.95)
            else:
                ctx.set_source_rgba(0.75, 0.80, 0.86, 0.95)
            ctx.move_to(cx + (col_w - ext.width) / 2.0 - ext.x_bearing, header_h * 0.68)
            ctx.show_text(wd_name)

        today = date.today()
        if not self.cell_dates:
            self._compute_month_cells()

        day_num_fs = base_fs + 0.5
        pill_fs = max(7.5, base_fs - 1.5)
        comp_fs = max(7.5, base_fs - 2.0)
        task_fs = max(7.5, base_fs - 1.2)
        line_step = max(11.0, task_fs + 3.5)

        for idx, d_obj in enumerate(self.cell_dates):
            r_idx = idx // 7
            c_idx = idx % 7
            cx = start_x + c_idx * col_w + 1.5
            cy = grid_y + r_idx * row_h + 1.5
            cw = col_w - 3.0
            ch = row_h - 3.0

            d_str = d_obj.strftime("%Y-%m-%d")
            is_cur_month = d_obj.month == self.view_month
            is_today = d_obj == today
            is_selected = d_obj == self.selected_date

            day_pending = self.due_map.get(d_str, [])
            day_comp = self.completed_map.get(d_str, [])

            # Cell background
            draw_rounded_rect(ctx, cx, cy, cw, ch, 4.5)
            if is_selected:
                ctx.set_source_rgba(0.18, 0.32, 0.52, 0.95)
            elif is_today:
                ctx.set_source_rgba(0.14, 0.26, 0.24, 0.95)
            elif day_pending:
                ctx.set_source_rgba(0.18, 0.20, 0.25, 0.95)
            elif is_cur_month:
                ctx.set_source_rgba(0.14, 0.16, 0.20, 0.92)
            else:
                ctx.set_source_rgba(0.10, 0.11, 0.14, 0.65)
            ctx.fill_preserve()

            # Cell border
            if is_selected:
                ctx.set_source_rgba(0.38, 0.72, 0.98, 1.0)
                ctx.set_line_width(1.8)
            elif is_today:
                ctx.set_source_rgba(0.20, 0.85, 0.60, 0.95)
                ctx.set_line_width(1.5)
            else:
                ctx.set_source_rgba(0.24, 0.28, 0.34, 0.7)
                ctx.set_line_width(0.8)
            ctx.stroke()

            # Day number in top-left
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(day_num_fs)
            day_num_str = f"{d_obj.day}"
            if is_today:
                day_num_str += " 今"
                ctx.set_source_rgba(0.25, 0.96, 0.68, 1.0)
            elif not is_cur_month:
                ctx.set_source_rgba(0.45, 0.48, 0.54, 0.75)
            elif c_idx >= 5:
                ctx.set_source_rgba(0.96, 0.62, 0.55, 0.95)
            else:
                ctx.set_source_rgba(0.92, 0.94, 0.96, 0.95)
            day_baseline_y = cy + day_num_fs + 3.0
            ctx.move_to(cx + 5.0, day_baseline_y)
            ctx.show_text(day_num_str)

            # Top-right badges: Pending count pill & Completed count checkmark
            badge_right_x = cx + cw - 4.0
            if day_pending:
                has_crit = any(t.get("_due_level") == "crit" or t.get("priority") == "H" for t in day_pending)
                has_warn = any(t.get("_due_level") == "warn" or t.get("priority") == "M" for t in day_pending)
                if has_crit:
                    p_rgb = (0.96, 0.30, 0.34)
                elif has_warn:
                    p_rgb = (0.96, 0.65, 0.14)
                else:
                    p_rgb = (0.22, 0.74, 0.97)

                p_txt = str(len(day_pending))
                ctx.set_font_size(pill_fs)
                p_ext = ctx.text_extents(p_txt)
                pill_w = max(14.0 * scale, p_ext.width + 7.0)
                pill_h = max(13.0, pill_fs + 4.5)
                px = badge_right_x - pill_w
                py = cy + 3.0
                draw_rounded_rect(ctx, px, py, pill_w, pill_h, 3.5)
                ctx.set_source_rgba(p_rgb[0], p_rgb[1], p_rgb[2], 0.92)
                ctx.fill()

                ctx.set_source_rgba(0.08, 0.09, 0.11, 1.0)
                ctx.move_to(
                    px + (pill_w - p_ext.width) / 2.0 - p_ext.x_bearing,
                    py + (pill_h + p_ext.height) / 2.0,
                )
                ctx.show_text(p_txt)
                badge_right_x = px - 3.0

            if day_comp:
                c_txt = f"✓{len(day_comp)}"
                ctx.set_font_size(comp_fs)
                c_ext = ctx.text_extents(c_txt)
                ctx.set_source_rgba(0.25, 0.85, 0.58, 0.92)
                ctx.move_to(badge_right_x - c_ext.width, day_baseline_y - 1.0)
                ctx.show_text(c_txt)

            # Mini Task Title Previews inside cell (up to 2 lines)
            line_y = day_baseline_y + line_step + 1.5
            char_px = max(6.5, task_fs * 0.86)
            max_chars = max(4, int((cw - 8.0) / char_px))
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            ctx.set_font_size(task_fs)

            shown_lines = 0
            for t in day_pending[:2]:
                if line_y + 3.0 > cy + ch:
                    break
                desc = t.get("description", "").strip()
                if len(desc) > max_chars:
                    desc = desc[: max_chars - 1] + "…"
                if t.get("priority") == "H" or t.get("_due_level") == "crit":
                    ctx.set_source_rgba(0.98, 0.62, 0.62, 1.0)
                elif t.get("priority") == "M" or t.get("_due_level") == "warn":
                    ctx.set_source_rgba(0.98, 0.82, 0.45, 1.0)
                else:
                    ctx.set_source_rgba(0.65, 0.88, 0.98, 1.0)
                ctx.move_to(cx + 4.5, line_y)
                ctx.show_text(f"• {desc}")
                line_y += line_step
                shown_lines += 1

            # If space remains, show up to 1 completed task preview in muted green
            if shown_lines < 2 and day_comp and (line_y + 3.0 <= cy + ch):
                ct = day_comp[0]
                cdesc = ct.get("description", "").strip()
                if len(cdesc) > max_chars:
                    cdesc = cdesc[: max_chars - 1] + "…"
                ctx.set_source_rgba(0.45, 0.82, 0.64, 0.85)
                ctx.move_to(cx + 4.5, line_y)
                ctx.show_text(f"✓ {cdesc}")

        return False

    def _on_calendar_button_press(self, widget, event):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)
        base_fs = float(max(8, min(22, int(self.parent_widget.config.get("calendar_font_size", 11)))))
        scale = base_fs / 11.0
        header_h = max(26.0, 28.0 * scale)
        grid_y = header_h + 2.0
        grid_h = max(120.0, h - grid_y - 4.0)
        col_w = (w - 8.0) / 7.0
        row_h = grid_h / 6.0
        start_x = 4.0

        if event.y < grid_y or event.x < start_x or event.x > w - 4.0:
            return False

        c_idx = int((event.x - start_x) // col_w)
        r_idx = int((event.y - grid_y) // row_h)
        if not (0 <= c_idx < 7 and 0 <= r_idx < 6):
            return False

        cell_idx = r_idx * 7 + c_idx
        if cell_idx >= len(self.cell_dates):
            return False

        clicked_date = self.cell_dates[cell_idx]
        self.selected_date = clicked_date
        if clicked_date.month != self.view_month or clicked_date.year != self.view_year:
            self.view_year = clicked_date.year
            self.view_month = clicked_date.month

        self.refresh_view()

        # Double-click on a date cell opens the Add Task dialog with Due pre-filled to that date!
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            self.parent_widget.show_add_or_edit_task_dialog(
                task=None, default_due=clicked_date.strftime("%Y-%m-%d")
            )
        return True

    def _clear_box(self, box):
        for child in box.get_children():
            box.remove(child)

    def _build_task_row_widget(self, t, is_completed=False, allow_assign_to_selected=False):
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_margin_top(6)
        vbox.set_margin_bottom(6)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)

        base_fs = max(8, min(22, int(self.parent_widget.config.get("calendar_font_size", 11))))
        main_pt = int(round(base_fs * 1024))
        sub_pt = int(round(max(7.5, base_fs - 1.5) * 1024))

        # Row 1: Task ID / Status / Priority / Description / Project & Tags
        top_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(38)

        desc = html.escape(t.get("description", ""))
        proj = t.get("project", "")
        tags = t.get("tags", [])
        pri = t.get("priority", "")
        uuid_str = t.get("uuid", "")

        meta_parts = []
        if proj:
            meta_parts.append(f"<span foreground='#60a5fa'>{html.escape(proj)}</span>")
        for tg in tags:
            meta_parts.append(f"<span foreground='#a78bfa'>+{html.escape(tg)}</span>")
        meta_m = f" <span size='{sub_pt}'>({ ' · '.join(meta_parts) })</span>" if meta_parts else ""

        if is_completed:
            end_d = t.get("_end_date", "")
            end_t = t.get("_end_time", "")
            lbl.set_markup(
                f"<span size='{main_pt}'><span foreground='#34d399'><b>✓ [已完成]</b></span> <s>{desc}</s></span>{meta_m} "
                f"<span size='{sub_pt}'><span foreground='#888888'>({end_d} {end_t})</span></span>"
            )
        else:
            tid = t.get("id", "?")
            is_act = t.get("_is_active", False)
            pri_m = (
                "<span foreground='#f87171'><b>[H]</b></span> "
                if pri == "H"
                else (
                    "<span foreground='#fbbf24'><b>[M]</b></span> "
                    if pri == "M"
                    else ("<span foreground='#38bdf8'><b>[L]</b></span> " if pri == "L" else "")
                )
            )
            act_m = "<span foreground='#34d399'><b>▶️[計時中]</b></span> " if is_act else ""
            due_rel = t.get("_due_rel", "")
            due_lvl = t.get("_due_level", "normal")
            due_col = "#f87171" if due_lvl == "crit" else ("#fbbf24" if due_lvl == "warn" else "#34d399")
            due_m = f"  <span size='{sub_pt}'><span foreground='{due_col}'>({html.escape(due_rel)})</span></span>" if due_rel else ""
            lbl.set_markup(
                f"<span size='{main_pt}'><tt><b>#{tid}</b></tt> {act_m}{pri_m}<b>{desc}</b></span>{meta_m}{due_m}"
            )

        top_hbox.pack_start(lbl, True, True, 0)
        vbox.pack_start(top_hbox, False, False, 0)

        # Row 2: Quick Action Buttons
        act_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        if is_completed:
            btn_restore = Gtk.Button(label="↩️ 復原為待辦")
            btn_restore.connect(
                "clicked",
                lambda _b, u=uuid_str: self.parent_widget._run_and_refresh(
                    TaskwarriorBackend.restore_task, u
                ),
            )
            act_hbox.pack_start(btn_restore, False, False, 0)
        else:
            btn_done = Gtk.Button(label="✅ 完成")
            btn_done.connect(
                "clicked",
                lambda _b, u=uuid_str: self.parent_widget._run_and_refresh(
                    TaskwarriorBackend.mark_done, u
                ),
            )
            act_hbox.pack_start(btn_done, False, False, 0)

            is_act = t.get("_is_active", False)
            btn_timer = Gtk.Button(label="⏹ 停止" if is_act else "▶️ 計時")
            btn_timer.connect(
                "clicked",
                lambda _b, u=uuid_str, act=is_act: self.parent_widget._run_and_refresh(
                    TaskwarriorBackend.toggle_start_stop, u, act
                ),
            )
            act_hbox.pack_start(btn_timer, False, False, 0)

            if allow_assign_to_selected:
                sel_str = self.selected_date.strftime("%Y-%m-%d")
                btn_assign = Gtk.Button(label=f"📅 排入 {self.selected_date.strftime('%m/%d')}")
                btn_assign.set_tooltip_text(f"將此任務到期日 (Due) 設定為 {sel_str}")
                btn_assign.connect(
                    "clicked",
                    lambda _b, u=uuid_str, ds=sel_str: self.parent_widget._run_and_refresh(
                        TaskwarriorBackend.set_due_date, u, ds
                    ),
                )
                act_hbox.pack_start(btn_assign, False, False, 0)
            else:
                btn_plus1d = Gtk.Button(label="+1天")
                btn_plus1d.set_tooltip_text("將任務到期日延後 1 天")
                next_day_str = (self.selected_date + timedelta(days=1)).strftime("%Y-%m-%d")
                btn_plus1d.connect(
                    "clicked",
                    lambda _b, u=uuid_str, ds=next_day_str: self.parent_widget._run_and_refresh(
                        TaskwarriorBackend.set_due_date, u, ds
                    ),
                )
                act_hbox.pack_start(btn_plus1d, False, False, 0)

            btn_edit = Gtk.Button(label="✏️ 編輯")
            btn_edit.connect(
                "clicked",
                lambda _b, task_obj=t: self.parent_widget.show_add_or_edit_task_dialog(
                    task=task_obj
                ),
            )
            act_hbox.pack_start(btn_edit, False, False, 0)

            btn_del = Gtk.Button(label="🗑")
            btn_del.set_tooltip_text("刪除任務")
            btn_del.connect(
                "clicked",
                lambda _b, task_obj=t: self.parent_widget.confirm_delete_task(task_obj),
            )
            act_hbox.pack_end(btn_del, False, False, 0)

        vbox.pack_start(act_hbox, False, False, 0)
        frame.add(vbox)
        return frame

    def _populate_right_panels(self):
        self._clear_box(self.sel_tasks_box)
        self._clear_box(self.unsched_tasks_box)

        base_fs = max(8, min(22, int(self.parent_widget.config.get("calendar_font_size", 11))))
        hdr_pt = int(round((base_fs + 1.0) * 1024))
        sub_pt = int(round(max(8.0, base_fs - 0.5) * 1024))

        sel_str = self.selected_date.strftime("%Y-%m-%d")
        wd_str = self.WEEKDAYS_ZH[self.selected_date.weekday()]
        day_pending = self.due_map.get(sel_str, [])
        day_comp = self.completed_map.get(sel_str, [])

        self.sel_date_lbl.set_markup(
            f"<span size='{hdr_pt}'><b>📌 {sel_str} ({wd_str})</b></span>   "
            f"<span size='{sub_pt}'><span foreground='#38bdf8'>到期: {len(day_pending)} 筆</span>"
            + (
                f"  |  <span foreground='#34d399'>當日完成: {len(day_comp)} 筆</span></span>"
                if self.parent_widget.config.get("calendar_show_completed", True)
                else "</span>"
            )
        )

        if not day_pending and not day_comp:
            empty_lbl = Gtk.Label(xalign=0.0)
            empty_lbl.set_margin_top(12)
            empty_lbl.set_margin_start(8)
            empty_lbl.set_markup(
                f"<span size='{sub_pt}'><i>此日期 ({sel_str}) 尚無排程到期或已完成的任務。\n"
                f"可點選上方「➕ 在選取日新增任務」或雙擊左側月曆格新增。</i></span>"
            )
            self.sel_tasks_box.pack_start(empty_lbl, False, False, 0)
        else:
            for t in day_pending:
                self.sel_tasks_box.pack_start(
                    self._build_task_row_widget(t, is_completed=False, allow_assign_to_selected=False),
                    False,
                    False,
                    0,
                )
            for ct in day_comp:
                self.sel_tasks_box.pack_start(
                    self._build_task_row_widget(ct, is_completed=True),
                    False,
                    False,
                    0,
                )

        # Populate bottom-right: Unscheduled pending tasks + Other dates pending tasks
        other_scheduled = [
            t for t in self.parent_widget.filtered_pending
            if t.get("_due_date") and t.get("_due_date") != sel_str
        ]
        self.unsched_hdr_lbl.set_markup(
            f"<span size='{hdr_pt}'><b>📋 未設到期日待辦 ({len(self.unscheduled)} 筆) 與其他日期排程 ({len(other_scheduled)} 筆)</b></span>"
        )

        if not self.unscheduled and not other_scheduled:
            u_empty = Gtk.Label(xalign=0.0)
            u_empty.set_margin_top(8)
            u_empty.set_margin_start(8)
            u_empty.set_markup(f"<span size='{sub_pt}'><i>目前沒有未排程或其他日期的待辦任務。</i></span>")
            self.unsched_tasks_box.pack_start(u_empty, False, False, 0)
        else:
            for t in self.unscheduled:
                self.unsched_tasks_box.pack_start(
                    self._build_task_row_widget(t, is_completed=False, allow_assign_to_selected=True),
                    False,
                    False,
                    0,
                )
            for t in other_scheduled:
                self.unsched_tasks_box.pack_start(
                    self._build_task_row_widget(t, is_completed=False, allow_assign_to_selected=True),
                    False,
                    False,
                    0,
                )

        self.sel_tasks_box.show_all()
        self.unsched_tasks_box.show_all()


class TaskwarriorWidget(Gtk.EventBox):
    """
    Main widget placed inside either a MatePanelApplet or a standalone Gtk.Window.
    """

    FONT_THEME_PRESETS = [
        ("深色面板高對比 (純白 / 琥珀橘 / 鮮紅)", "#ffffff", "#fbbf24", "#f87171"),
        ("深色面板柔和風 (亮灰 / 金黃 / 珊瑚紅)", "#e5e7eb", "#f59e0b", "#ef4444"),
        ("淺色/白色面板專用 (炭黑 / 深琥珀棕 / 深赤紅)", "#111827", "#b45309", "#b91c1c"),
        ("淺灰面板專用 (深石板灰 / 焦糖橘 / 暗紅)", "#334155", "#c2410c", "#991b1b"),
        ("終端機綠客風 (螢光綠 / 亮黃 / 洋紅)", "#4ade80", "#facc15", "#f43f5e"),
    ]

    def __init__(self, applet=None):
        super().__init__()
        self.applet = applet
        self.set_visible_window(False)
        self.set_above_child(True)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )

        self.config = self._load_config()
        self.task_data = {
            "pending": [],
            "completed": [],
            "all_completed": [],
            "projects": [],
            "tags": [],
        }
        self.filtered_pending = []
        self.summary_level = "zero"
        self.has_active_task = False
        self.panel_size = 28
        self.prefs_win = None
        self.calendar_win = None
        self.active_menu = None
        self.file_monitor = None
        self.debounce_id = None
        self.periodic_timer_id = None

        self.canvas = Gtk.DrawingArea()
        self.canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self.canvas.connect("draw", self._on_draw)
        self.add(self.canvas)

        self.connect("button-press-event", self._on_button_press)
        self.canvas.connect("button-press-event", self._on_button_press)
        self.connect("destroy", self._on_destroy)

        self._setup_file_monitor()
        self.periodic_timer_id = GLib.timeout_add_seconds(60, self._on_periodic_refresh)
        self.refresh_tasks()
        self.show_all()

    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                if isinstance(user_cfg, dict):
                    cfg.update(user_cfg)
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

    def _setup_file_monitor(self):
        if not os.path.isdir(TASK_DATA_DIR):
            return
        try:
            gfile = Gio.File.new_for_path(TASK_DATA_DIR)
            self.file_monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self.file_monitor.connect("changed", self._on_task_dir_changed)
        except Exception:
            self.file_monitor = None

    def _on_task_dir_changed(self, _monitor, file_obj, _other_file, event_type):
        if file_obj is not None:
            bname = file_obj.get_basename() or ""
            if not bname.endswith(".data"):
                return
        if event_type in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.MOVED,
        ):
            if self.debounce_id is not None:
                GLib.source_remove(self.debounce_id)
            self.debounce_id = GLib.timeout_add(150, self._on_debounced_reload)

    def _on_debounced_reload(self):
        self.debounce_id = None
        self.refresh_tasks()
        return False

    def _on_periodic_refresh(self):
        self.refresh_tasks()
        return True

    def refresh_tasks(self):
        self.task_data = TaskwarriorBackend.fetch_all(
            due_soon_days=int(self.config.get("due_soon_days", 3)),
            completed_limit=int(self.config.get("completed_limit", 20)),
        )

        f_proj = self.config.get("filter_project", "").strip()
        f_tag = self.config.get("filter_tag", "").strip()

        filtered = []
        for t in self.task_data.get("pending", []):
            if f_proj and t.get("project", "") != f_proj:
                continue
            if f_tag and f_tag not in t.get("tags", []):
                continue
            filtered.append(t)

        self.filtered_pending = filtered
        self.has_active_task = any(t.get("_is_active") for t in filtered)

        if not filtered:
            self.summary_level = "zero"
        else:
            has_crit = any(t.get("_due_level") == "crit" for t in filtered)
            has_warn = any(
                t.get("_due_level") == "warn" or t.get("priority") == "H"
                for t in filtered
            )
            if has_crit:
                self.summary_level = "crit"
            elif has_warn:
                self.summary_level = "warn"
            else:
                self.summary_level = "normal"

        self._update_tooltip()
        self._update_canvas_size()

        if self.calendar_win is not None:
            self.calendar_win.refresh_view()

    def _get_top_task_summary_text(self):
        if not self.filtered_pending:
            return "無待辦事項"
        top = self.filtered_pending[0]
        desc = top.get("description", "").strip()
        max_c = max(6, int(self.config.get("max_title_chars", 22)))
        if len(desc) > max_c:
            desc = desc[: max_c - 1] + "…"
        pri = top.get("priority", "")
        pri_prefix = f"[{pri}] " if pri else ""
        due_suffix = ""
        if self.config.get("show_due_in_panel", True) and top.get("_due_rel"):
            due_suffix = f" ({top['_due_rel']})"
        return f"{pri_prefix}{desc}{due_suffix}"

    def set_panel_size(self, size):
        self.panel_size = max(22, min(64, int(size)))
        self._update_canvas_size()

    def _update_canvas_size(self):
        h = max(24, self.panel_size)
        mode = self.config.get("display_mode", "badge")
        badge_w = max(32, int(h * 1.22))

        if mode == "badge":
            total_w = badge_w + 4
        else:
            top_txt = self._get_top_task_summary_text()
            est_char_w = max(7.5, h * 0.34)
            text_w = int(len(top_txt) * est_char_w) + 16
            total_w = badge_w + 6 + min(380, max(90, text_w))
        self.canvas.set_size_request(total_w, h)
        self.canvas.queue_draw()

    def _update_tooltip(self):
        total_all = len(self.task_data.get("pending", []))
        count = len(self.filtered_pending)
        f_proj = self.config.get("filter_project", "")
        f_tag = self.config.get("filter_tag", "")
        filter_notes = []
        if f_proj:
            filter_notes.append(f"專案:{f_proj}")
        if f_tag:
            filter_notes.append(f"+{f_tag}")
        f_str = f" (篩選: {', '.join(filter_notes)})" if filter_notes else ""

        lines = [
            f"<b>📋 Taskwarrior 任務管理 Plus{html.escape(f_str)}</b>",
            f"• 待辦任務數: <b>{count}</b>" + (f" (總計 {total_all} 筆)" if f_str else ""),
        ]
        for t in self.filtered_pending[:5]:
            tid = t.get("id", "?")
            pri = f"[{t['priority']}] " if t.get("priority") else ""
            desc = html.escape(t.get("description", ""))
            proj = f" ({html.escape(t['project'])})" if t.get("project") else ""
            due_r = f" — <b>{html.escape(t['_due_rel'])}</b>" if t.get("_due_rel") else ""
            act = " ▶️[計時中]" if t.get("_is_active") else ""
            lines.append(f"  #{tid} {pri}{desc}{proj}{due_r}{act}")
        if count > 5:
            lines.append(f"  <small>...及其他 {count - 5} 筆待辦任務</small>")
        lc_mode = "開啟月曆任務看板" if self.config.get("left_click_action", "calendar") == "calendar" else "開啟任務下拉選單"
        lines.append(f"<small>左鍵點選: {lc_mode} | 右鍵選單可切換檢視與偏好設定</small>")
        self.set_tooltip_markup("\n".join(lines))

    def _resolve_panel_text_rgb(self, level, default_rgb):
        if self.config.get("font_color_mode", "dynamic") == "custom":
            if level == "crit":
                hx = self.config.get("custom_crit_font_color", "#f87171")
            elif level == "warn":
                hx = self.config.get("custom_warn_font_color", "#fbbf24")
            else:
                hx = self.config.get("custom_font_color", "#ffffff")
            return hex_to_rgb(hx, default=default_rgb)
        return default_rgb

    def _draw_styled_text(self, ctx, x, y, text, rgb):
        if self.config.get("text_shadow", False):
            lum = rgb_luminance(rgb)
            s_rgb = (0.0, 0.0, 0.0, 0.82) if lum > 0.45 else (1.0, 1.0, 1.0, 0.82)
            ctx.set_source_rgba(*s_rgb)
            for dx, dy in ((-0.8, 0.0), (0.8, 0.0), (0.0, -0.8), (0.0, 0.8)):
                ctx.move_to(x + dx, y + dy)
                ctx.show_text(text)

        ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], 1.0)
        ctx.move_to(x, y)
        ctx.show_text(text)

    def _on_draw(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        mode = self.config.get("display_mode", "badge")

        pad_y = 1.0
        bh = max(20.0, h - pad_y * 2)
        bw = max(30.0, min(float(w - 2), h * 1.22))

        colors = level_to_colors(self.summary_level)
        bg_rgb, border_rgb, text_rgb, _, sev_lvl = colors
        bg_style = self.config.get("badge_bg_style", "dark")

        x = 1.0
        y = pad_y
        draw_rounded_rect(ctx, x + 0.5, y + 0.5, bw - 1.0, bh - 1.0, 4.5)
        if bg_style == "dark":
            ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], 0.92)
            ctx.fill_preserve()
        elif bg_style == "light":
            l_r = min(1.0, 0.88 + bg_rgb[0] * 0.22)
            l_g = min(1.0, 0.88 + bg_rgb[1] * 0.22)
            l_b = min(1.0, 0.88 + bg_rgb[2] * 0.22)
            ctx.set_source_rgba(l_r, l_g, l_b, 0.92)
            ctx.fill_preserve()

        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 0.95)
        ctx.set_line_width(1.2)
        ctx.stroke()

        if self.has_active_task:
            ctx.arc(x + bw - 4.5, y + 4.5, 2.3, 0, 2 * math.pi)
            ctx.set_source_rgba(0.20, 0.95, 0.50, 1.0)
            ctx.fill()

        if self.config.get("font_color_mode", "dynamic") == "custom":
            stage_rgb = self._resolve_panel_text_rgb(sev_lvl, text_rgb)
            title_rgb = stage_rgb
            val_rgb = stage_rgb
        else:
            title_rgb = border_rgb
            val_rgb = (bg_rgb[0] * 0.85, bg_rgb[1] * 0.85, bg_rgb[2] * 0.85) if bg_style == "light" else text_rgb

        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        header_size = max(6.5, bh * 0.26)
        ctx.set_font_size(header_size)
        title = "TASK"
        ext = ctx.text_extents(title)
        tx = x + (bw - ext.width) / 2.0 - ext.x_bearing
        ty = y + header_size + 1.5
        self._draw_styled_text(ctx, tx, ty, title, title_rgb)

        count_str = str(len(self.filtered_pending))
        val_size = max(9.0, bh * 0.42)
        ctx.set_font_size(val_size)
        v_ext = ctx.text_extents(count_str)
        vx = x + (bw - v_ext.width) / 2.0 - v_ext.x_bearing
        vy = y + bh - 3.0
        self._draw_styled_text(ctx, vx, vy, count_str, val_rgb)

        if mode == "badge_text":
            top_txt = self._get_top_task_summary_text()
            font_sz = min(12.5, max(9.5, h * 0.42))
            ctx.set_font_size(font_sz)
            t_ext = ctx.text_extents(top_txt)
            summary_rgb = self._resolve_panel_text_rgb(sev_lvl, border_rgb)
            self._draw_styled_text(
                ctx,
                x + bw + 6.0,
                (h + t_ext.height) / 2.0,
                top_txt,
                summary_rgb,
            )

        return False

    def handle_applet_button_press(self, widget, event):
        if event.button == 1:
            GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self._trigger_left_click_action(event)
            return True
        return False

    def _on_button_press(self, widget, event):
        if event.button == 1:
            if self.applet is not None:
                GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self._trigger_left_click_action(event)
            return True
        elif event.button == 3 and self.applet is None:
            self.popup_task_menu(event)
            return True
        return False

    def _trigger_left_click_action(self, event=None):
        if self.config.get("left_click_action", "calendar") == "calendar":
            self.toggle_calendar_window()
        else:
            self.popup_task_menu(event)

    def toggle_calendar_window(self, *_args):
        if self.calendar_win is not None:
            if self.calendar_win.is_visible():
                self.calendar_win.destroy()
                self.calendar_win = None
                return
            self.calendar_win.present()
            return
        self.show_calendar_window()

    def show_calendar_window(self, *_args):
        self.refresh_tasks()
        if self.calendar_win is not None:
            self.calendar_win.refresh_view()
            self.calendar_win.present()
            return
        win = TaskCalendarWindow(self)
        win.connect("destroy", lambda *_: setattr(self, "calendar_win", None))
        self.calendar_win = win
        win.show_all()

    def popup_task_menu(self, event=None):
        self.refresh_tasks()
        self.active_menu = self._build_popup_menu()
        if event is not None:
            self.active_menu.popup(None, None, None, None, event.button, event.time)
        else:
            self.active_menu.popup(None, None, None, None, 0, Gtk.get_current_event_time())

    def _create_markup_menu_item(self, markup, sensitive=True, callback=None):
        item = Gtk.MenuItem()
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_use_markup(True)
        lbl.set_markup(markup)
        item.add(lbl)
        item.set_sensitive(sensitive)
        if callback is not None:
            item.connect("activate", callback)
        return item

    def _build_popup_menu(self):
        menu = Gtk.Menu()
        pending = self.filtered_pending
        all_pending = self.task_data.get("pending", [])
        high_cnt = sum(1 for t in pending if t.get("priority") == "H")
        overdue_cnt = sum(1 for t in pending if t.get("_due_level") == "crit")

        _, _, _, accent_hex, _ = level_to_colors(self.summary_level)
        f_proj = self.config.get("filter_project", "")
        f_tag = self.config.get("filter_tag", "")
        filter_badge = ""
        if f_proj or f_tag:
            parts = [p for p in (f"專案:{f_proj}" if f_proj else "", f"+{f_tag}" if f_tag else "") if p]
            filter_badge = f"  <span foreground='#38bdf8'>[篩選: {' '.join(parts)}]</span>"

        # 1. Header & Open Calendar Dashboard item
        header_item = self._create_markup_menu_item(
            f"<b>📋 Taskwarrior 任務管理中心</b>{filter_badge}   "
            f"<span foreground='{accent_hex}'><b>待辦: {len(pending)} 筆</b></span>  "
            f"<small>(高優先: {high_cnt} | 逾期/今日: {overdue_cnt})</small>",
            sensitive=False,
        )
        menu.append(header_item)

        cal_open_item = self._create_markup_menu_item(
            "<b>📅 開啟互動式月曆任務看板 (Calendar View)...</b>",
            sensitive=True,
            callback=lambda *_: self.show_calendar_window(),
        )
        menu.append(cal_open_item)
        menu.append(Gtk.SeparatorMenuItem())

        # 2. Pending Tasks List
        if not pending:
            empty_msg = "🎉 目前沒有符合篩選條件的待辦任務！" if (f_proj or f_tag) else "🎉 目前沒有任何待辦任務！"
            menu.append(self._create_markup_menu_item(f"   <i>{empty_msg}</i>", sensitive=False))
        else:
            for t in pending:
                menu.append(self._build_single_task_menu_item(t))

        menu.append(Gtk.SeparatorMenuItem())

        # 3. Quick Add Task
        add_item = self._create_markup_menu_item(
            "<b>➕ 快速新增任務 (Add New Task)...</b>",
            sensitive=True,
            callback=lambda *_: self.show_add_or_edit_task_dialog(task=None),
        )
        menu.append(add_item)

        # 4. Filter by Project / Tag Submenu
        projects = self.task_data.get("projects", [])
        tags = self.task_data.get("tags", [])
        filter_label = "🔍 依專案 / 標籤篩選檢視"
        if f_proj or f_tag:
            filter_label += f" (目前: {f_proj or '全部'} / {('+' + f_tag) if f_tag else '全部'})"
        filter_item = Gtk.MenuItem(label=filter_label)
        filter_sub = Gtk.Menu()

        clear_f = Gtk.MenuItem(label="顯示全部待辦任務 (清除篩選)")
        clear_f.connect("activate", lambda *_: self._set_filter("", ""))
        filter_sub.append(clear_f)
        filter_sub.append(Gtk.SeparatorMenuItem())

        proj_hdr = self._create_markup_menu_item("<b>📁 依專案 (Project) 篩選</b>", sensitive=False)
        filter_sub.append(proj_hdr)
        all_p_item = Gtk.RadioMenuItem.new_with_label_from_widget(None, "全部專案")
        grp_p = all_p_item
        if not f_proj:
            all_p_item.set_active(True)
        all_p_item.connect("toggled", self._on_project_filter_toggled, "")
        filter_sub.append(all_p_item)

        for p_name in projects:
            p_cnt = sum(1 for t in all_pending if t.get("project") == p_name)
            r_p = Gtk.RadioMenuItem.new_with_label_from_widget(
                grp_p, f"{p_name} ({p_cnt} 筆待辦)"
            )
            if f_proj == p_name:
                r_p.set_active(True)
            r_p.connect("toggled", self._on_project_filter_toggled, p_name)
            filter_sub.append(r_p)

        filter_sub.append(Gtk.SeparatorMenuItem())

        tag_hdr = self._create_markup_menu_item("<b>🏷 依標籤 (Tag) 篩選</b>", sensitive=False)
        filter_sub.append(tag_hdr)
        all_t_item = Gtk.RadioMenuItem.new_with_label_from_widget(None, "全部標籤")
        grp_t = all_t_item
        if not f_tag:
            all_t_item.set_active(True)
        all_t_item.connect("toggled", self._on_tag_filter_toggled, "")
        filter_sub.append(all_t_item)

        for t_name in tags:
            t_cnt = sum(1 for t in all_pending if t_name in t.get("tags", []))
            r_t = Gtk.RadioMenuItem.new_with_label_from_widget(
                grp_t, f"+{t_name} ({t_cnt} 筆待辦)"
            )
            if f_tag == t_name:
                r_t.set_active(True)
            r_t.connect("toggled", self._on_tag_filter_toggled, t_name)
            filter_sub.append(r_t)

        filter_item.set_submenu(filter_sub)
        menu.append(filter_item)

        # 5. Recent Completed Tasks & Undo Submenu
        completed = self.task_data.get("completed", [])
        comp_item = Gtk.MenuItem(label=f"📜 最近已完成任務 ({len(completed)} 筆) 與復原")
        comp_sub = Gtk.Menu()

        undo_item = self._create_markup_menu_item(
            "↩️ <b>復原上一步操作 (task undo)</b>",
            sensitive=True,
            callback=lambda *_: self._run_and_refresh(TaskwarriorBackend.undo_last),
        )
        comp_sub.append(undo_item)
        comp_sub.append(Gtk.SeparatorMenuItem())

        if not completed:
            comp_sub.append(self._create_markup_menu_item("   <i>無已完成任務紀錄</i>", sensitive=False))
        else:
            comp_sub.append(
                self._create_markup_menu_item(
                    "<small>點選下方已完成任務可將其重新標記為「待辦 (pending)」：</small>",
                    sensitive=False,
                )
            )
            for ct in completed:
                c_uuid = ct.get("uuid", "")
                c_desc = html.escape(ct.get("description", ""))
                c_proj = f" <span foreground='#38bdf8'>[{html.escape(ct['project'])}]</span>" if ct.get("project") else ""
                end_d = ct.get("_end_date", "")
                end_str = f" <small><span foreground='#888888'>({end_d})</span></small>" if end_d else ""
                c_row = self._create_markup_menu_item(
                    f"✓ {c_desc}{c_proj}{end_str}",
                    sensitive=True,
                    callback=lambda _w, u=c_uuid: self._run_and_refresh(
                        TaskwarriorBackend.restore_task, u
                    ),
                )
                comp_sub.append(c_row)

        comp_item.set_submenu(comp_sub)
        menu.append(comp_item)

        menu.append(Gtk.SeparatorMenuItem())

        # 6. Submenu: Display Style & 3-Stage Font Colors
        style_item = Gtk.MenuItem(label="🎨 面板顯示樣式與三段字形顏色")
        style_sub = Gtk.Menu()

        cur_lc = self.config.get("left_click_action", "calendar")
        lc_cal = Gtk.RadioMenuItem.new_with_label_from_widget(
            None, "左鍵預設動作：開啟互動式月曆任務看板"
        )
        lc_menu = Gtk.RadioMenuItem.new_with_label_from_widget(
            lc_cal, "左鍵預設動作：開啟傳統下拉快速選單"
        )
        if cur_lc == "calendar":
            lc_cal.set_active(True)
        else:
            lc_menu.set_active(True)
        lc_cal.connect("toggled", self._on_left_click_action_toggled, "calendar")
        lc_menu.connect("toggled", self._on_left_click_action_toggled, "menu")
        style_sub.append(lc_cal)
        style_sub.append(lc_menu)

        style_sub.append(Gtk.SeparatorMenuItem())

        cur_mode = self.config.get("display_mode", "badge")
        m_badge = Gtk.RadioMenuItem.new_with_label_from_widget(
            None, "模式 A：精簡任務計數方塊 ([TASK 1])"
        )
        m_text = Gtk.RadioMenuItem.new_with_label_from_widget(
            m_badge, "模式 B：方塊 + 最高緊急度任務標題 ([TASK 1] [H] 加強mate-panel...)"
        )
        if cur_mode == "badge":
            m_badge.set_active(True)
        else:
            m_text.set_active(True)
        m_badge.connect("toggled", self._on_display_mode_toggled, "badge")
        m_text.connect("toggled", self._on_display_mode_toggled, "badge_text")
        style_sub.append(m_badge)
        style_sub.append(m_text)

        style_sub.append(Gtk.SeparatorMenuItem())
        f_mode = self.config.get("font_color_mode", "dynamic")
        rm_dyn = Gtk.RadioMenuItem.new_with_label_from_widget(None, "預設內建動態字形色階")
        rm_cus = Gtk.RadioMenuItem.new_with_label_from_widget(rm_dyn, "自訂三段式字形顏色")
        if f_mode == "dynamic":
            rm_dyn.set_active(True)
        else:
            rm_cus.set_active(True)
        rm_dyn.connect("toggled", self._on_font_color_mode_toggled, "dynamic")
        rm_cus.connect("toggled", self._on_font_color_mode_toggled, "custom")
        style_sub.append(rm_dyn)
        style_sub.append(rm_cus)

        for label_txt, hx_n, hx_w, hx_c in self.FONT_THEME_PRESETS:
            p_item = self._create_markup_menu_item(
                f"套用色票：<span foreground='{hx_n}'><b>■</b></span> "
                f"<span foreground='{hx_w}'><b>■</b></span> "
                f"<span foreground='{hx_c}'><b>■</b></span>  {label_txt}",
                sensitive=True,
                callback=lambda _w, n=hx_n, w=hx_w, c=hx_c: self._apply_preset_font_theme(n, w, c),
            )
            style_sub.append(p_item)

        style_sub.append(Gtk.SeparatorMenuItem())
        bg_style = self.config.get("badge_bg_style", "dark")
        bg_dark = Gtk.RadioMenuItem.new_with_label_from_widget(None, "方塊底色：深色半透明底")
        bg_light = Gtk.RadioMenuItem.new_with_label_from_widget(bg_dark, "方塊底色：淺色半透明底")
        bg_trans = Gtk.RadioMenuItem.new_with_label_from_widget(bg_dark, "方塊底色：完全透明無底色")
        if bg_style == "dark":
            bg_dark.set_active(True)
        elif bg_style == "light":
            bg_light.set_active(True)
        else:
            bg_trans.set_active(True)
        bg_dark.connect("toggled", self._on_badge_bg_toggled, "dark")
        bg_light.connect("toggled", self._on_badge_bg_toggled, "light")
        bg_trans.connect("toggled", self._on_badge_bg_toggled, "transparent")
        style_sub.append(bg_dark)
        style_sub.append(bg_light)
        style_sub.append(bg_trans)

        style_item.set_submenu(style_sub)
        menu.append(style_item)

        # 7. Preferences
        prefs_item = Gtk.MenuItem(label="⚙️ 偏好設定 (Preferences)...")
        prefs_item.connect("activate", lambda *_: self.show_preferences_dialog())
        menu.append(prefs_item)

        menu.show_all()
        return menu

    def _build_single_task_menu_item(self, t):
        tid = t.get("id", "?")
        uuid_str = t.get("uuid", "")
        desc = html.escape(t.get("description", ""))
        pri = t.get("priority", "")
        proj = t.get("project", "")
        tags = t.get("tags", [])
        urg = float(t.get("urgency", 0.0))
        is_active = t.get("_is_active", False)
        due_date = t.get("_due_date", "")
        due_rel = t.get("_due_rel", "")
        due_lvl = t.get("_due_level", "normal")

        if pri == "H":
            pri_m = "<span foreground='#f87171'><b>[H]</b></span> "
        elif pri == "M":
            pri_m = "<span foreground='#fbbf24'><b>[M]</b></span> "
        elif pri == "L":
            pri_m = "<span foreground='#38bdf8'><b>[L]</b></span> "
        else:
            pri_m = ""

        active_m = "<span foreground='#34d399'><b>▶️[執行中]</b></span> " if is_active else ""

        meta_parts = []
        if proj:
            meta_parts.append(f"<span foreground='#60a5fa'>{html.escape(proj)}</span>")
        for tg in tags:
            meta_parts.append(f"<span foreground='#a78bfa'>+{html.escape(tg)}</span>")
        meta_m = f"  ({ ' · '.join(meta_parts) })" if meta_parts else ""

        due_m = ""
        if due_date:
            d_col = "#f87171" if due_lvl == "crit" else ("#fbbf24" if due_lvl == "warn" else "#34d399")
            due_m = f"   <span foreground='{d_col}'>📅 {due_date} ({html.escape(due_rel)})</span>"

        urg_m = f"  <small><span foreground='#888888'>[Urg: {urg:.1f}]</span></small>"

        row_item = self._create_markup_menu_item(
            f"<tt><b>#{tid:<2}</b></tt> {active_m}{pri_m}<b>{desc}</b>{meta_m}{due_m}{urg_m}",
            sensitive=True,
        )

        sub = Gtk.Menu()

        ann_list = t.get("annotations", [])
        if ann_list:
            for ann in ann_list:
                a_dt = parse_tw_timestamp(ann.get("entry"))
                a_dstr = a_dt.strftime("%m-%d %H:%M") if a_dt else ""
                a_txt = html.escape(ann.get("description", ""))
                sub.append(
                    self._create_markup_menu_item(
                        f"📌 <small><span foreground='#94a3b8'>[{a_dstr}]</span> {a_txt}</small>",
                        sensitive=False,
                    )
                )
            sub.append(Gtk.SeparatorMenuItem())

        done_item = self._create_markup_menu_item(
            f"✅ <b>標記為完成 (task {tid} done)</b>",
            sensitive=True,
            callback=lambda _w, u=uuid_str: self._run_and_refresh(TaskwarriorBackend.mark_done, u),
        )
        sub.append(done_item)

        start_lbl = (
            f"⏹ 停止計時 (task {tid} stop)"
            if is_active
            else f"▶️ 開始執行計時 (task {tid} start)"
        )
        start_item = Gtk.MenuItem(label=start_lbl)
        start_item.connect(
            "activate",
            lambda _w, u=uuid_str, act=is_active: self._run_and_refresh(
                TaskwarriorBackend.toggle_start_stop, u, act
            ),
        )
        sub.append(start_item)

        sub.append(Gtk.SeparatorMenuItem())

        pri_item = Gtk.MenuItem(label=f"⚡ 快速切換優先級 (目前: {pri or '無'})")
        pri_sub = Gtk.Menu()
        for p_code, p_label in (
            ("H", "🔴 高優先級 (High - H)"),
            ("M", "🟡 中優先級 (Medium - M)"),
            ("L", "🔵 低優先級 (Low - L)"),
            ("", "⚪ 無優先級 (None)"),
        ):
            p_mi = Gtk.MenuItem(label=p_label)
            p_mi.connect(
                "activate",
                lambda _w, u=uuid_str, pc=p_code: self._run_and_refresh(
                    TaskwarriorBackend.set_priority, u, pc
                ),
            )
            pri_sub.append(p_mi)
        pri_item.set_submenu(pri_sub)
        sub.append(pri_item)

        edit_item = Gtk.MenuItem(label="✏️ 編輯任務 (描述 / 專案 / 標籤 / 到期日)...")
        edit_item.connect(
            "activate", lambda _w, task_obj=t: self.show_add_or_edit_task_dialog(task=task_obj)
        )
        sub.append(edit_item)

        ann_item = Gtk.MenuItem(label="📝 新增任務註解 (Annotate)...")
        ann_item.connect("activate", lambda _w, task_obj=t: self.show_annotate_dialog(task_obj))
        sub.append(ann_item)

        sub.append(Gtk.SeparatorMenuItem())

        del_item = self._create_markup_menu_item(
            f"<span foreground='#f87171'>🗑 刪除任務 (task {tid} delete)</span>",
            sensitive=True,
            callback=lambda _w, task_obj=t: self.confirm_delete_task(task_obj),
        )
        sub.append(del_item)

        row_item.set_submenu(sub)
        return row_item

    def _run_and_refresh(self, func, *args):
        func(*args)
        self.refresh_tasks()

    def _set_filter(self, proj, tag):
        self.config["filter_project"] = proj
        self.config["filter_tag"] = tag
        self._save_config()
        self.refresh_tasks()

    def _on_project_filter_toggled(self, radio_item, proj):
        if not radio_item.get_active():
            return
        self.config["filter_project"] = proj
        self._save_config()
        self.refresh_tasks()

    def _on_tag_filter_toggled(self, radio_item, tag):
        if not radio_item.get_active():
            return
        self.config["filter_tag"] = tag
        self._save_config()
        self.refresh_tasks()

    def show_add_or_edit_task_dialog(self, task=None, default_due=""):
        """
        Show dialog to Add a new task (task=None) or Edit an existing task (task=dict).
        """
        is_edit = task is not None
        title = f"編輯任務 #{task.get('id', '')}" if is_edit else "新增 Taskwarrior 任務"

        parent_win = self.calendar_win
        if parent_win is None and isinstance(self.get_toplevel(), Gtk.Window):
            parent_win = self.get_toplevel()

        dlg = Gtk.Dialog(
            title=title,
            transient_for=parent_win,
            flags=0,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE if is_edit else Gtk.STOCK_ADD,
            Gtk.ResponseType.OK,
        )
        dlg.set_default_size(450, 360)

        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(16)
        box.set_margin_end(16)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)

        # 1. Description
        lbl_desc = Gtk.Label(label="任務描述 (必填)：", xalign=0.0)
        entry_desc = Gtk.Entry()
        entry_desc.set_hexpand(True)
        entry_desc.set_placeholder_text("例如：加強 mate-panel 功能")
        if is_edit:
            entry_desc.set_text(task.get("description", ""))
        grid.attach(lbl_desc, 0, 0, 1, 1)
        grid.attach(entry_desc, 1, 0, 2, 1)

        # 2. Project
        lbl_proj = Gtk.Label(label="專案 (Project)：", xalign=0.0)
        combo_proj = Gtk.ComboBoxText.new_with_entry()
        for p in self.task_data.get("projects", []):
            combo_proj.append_text(p)
        proj_entry = combo_proj.get_child()
        proj_entry.set_placeholder_text("可選填或從下拉選單挑選 (如 Linux, HPC, JS...)")
        if is_edit and task.get("project"):
            proj_entry.set_text(task.get("project", ""))
        elif not is_edit and self.config.get("filter_project"):
            proj_entry.set_text(self.config.get("filter_project", ""))
        grid.attach(lbl_proj, 0, 1, 1, 1)
        grid.attach(combo_proj, 1, 1, 2, 1)

        # 3. Priority
        lbl_pri = Gtk.Label(label="優先級 (Priority)：", xalign=0.0)
        combo_pri = Gtk.ComboBoxText()
        pri_options = [
            ("", "無 (None)"),
            ("H", "H - 高優先級 (High)"),
            ("M", "M - 中優先級 (Medium)"),
            ("L", "L - 低優先級 (Low)"),
        ]
        cur_pri = task.get("priority", "") if is_edit else "H"
        active_idx = 0
        for idx, (code, label) in enumerate(pri_options):
            combo_pri.append(code, label)
            if code == cur_pri:
                active_idx = idx
        combo_pri.set_active(active_idx)
        grid.attach(lbl_pri, 0, 2, 1, 1)
        grid.attach(combo_pri, 1, 2, 2, 1)

        # 4. Tags
        lbl_tags = Gtk.Label(label="標籤 (Tags)：", xalign=0.0)
        entry_tags = Gtk.Entry()
        entry_tags.set_placeholder_text("以空白分隔，例如：WORK")
        if is_edit:
            entry_tags.set_text(" ".join(task.get("tags", [])))
        elif self.config.get("filter_tag"):
            entry_tags.set_text(self.config.get("filter_tag", ""))
        else:
            entry_tags.set_text("WORK")
        grid.attach(lbl_tags, 0, 3, 1, 1)
        grid.attach(entry_tags, 1, 3, 1, 1)

        tag_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for known_tg in self.task_data.get("tags", ["WORK", "HOBBY"])[:4]:
            b_tg = Gtk.Button(label=f"+{known_tg}")

            def on_add_tag(_b, tg=known_tg):
                cur_tokens = entry_tags.get_text().split()
                if tg not in cur_tokens:
                    cur_tokens.append(tg)
                    entry_tags.set_text(" ".join(cur_tokens))

            b_tg.connect("clicked", on_add_tag)
            tag_btn_box.pack_start(b_tg, False, False, 0)
        grid.attach(tag_btn_box, 2, 3, 1, 1)

        # 5. Due Date
        lbl_due = Gtk.Label(label="到期日 (Due)：", xalign=0.0)
        entry_due = Gtk.Entry()
        entry_due.set_placeholder_text("YYYY-MM-DD 或 tomorrow, +3d, eow (可留空)")
        if is_edit and task.get("_due_date"):
            entry_due.set_text(task.get("_due_date", ""))
        elif default_due:
            entry_due.set_text(default_due)

        btn_cal = Gtk.Button(label="📅 選擇日期")

        def on_pick_date(_btn):
            cal_dlg = Gtk.Dialog(
                title="選擇到期日",
                transient_for=dlg,
                flags=Gtk.DialogFlags.MODAL,
            )
            cal_dlg.add_buttons(
                "清除日期",
                Gtk.ResponseType.REJECT,
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OK,
                Gtk.ResponseType.OK,
            )
            cal = Gtk.Calendar()
            cal_dlg.get_content_area().pack_start(cal, True, True, 8)
            cal_dlg.show_all()
            res = cal_dlg.run()
            if res == Gtk.ResponseType.OK:
                yr, mo, dy = cal.get_date()
                entry_due.set_text(f"{yr:04d}-{mo + 1:02d}-{dy:02d}")
            elif res == Gtk.ResponseType.REJECT:
                entry_due.set_text("")
            cal_dlg.destroy()

        btn_cal.connect("clicked", on_pick_date)
        grid.attach(lbl_due, 0, 4, 1, 1)
        grid.attach(entry_due, 1, 4, 1, 1)
        grid.attach(btn_cal, 2, 4, 1, 1)

        box.pack_start(grid, False, False, 0)
        dlg.show_all()

        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            desc_val = entry_desc.get_text().strip()
            if not desc_val:
                err = Gtk.MessageDialog(
                    transient_for=dlg,
                    flags=0,
                    message_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.OK,
                    text="請輸入任務描述！",
                )
                err.run()
                err.destroy()
                continue

            proj_val = proj_entry.get_text().strip()
            pri_val = combo_pri.get_active_id() or ""
            tags_val = entry_tags.get_text().strip()
            due_val = entry_due.get_text().strip()

            if is_edit:
                TaskwarriorBackend.modify_task_structured(
                    uuid_str=task.get("uuid", ""),
                    description=desc_val,
                    project=proj_val,
                    priority=pri_val,
                    tags_str=tags_val,
                    due=due_val,
                    orig_tags=task.get("tags", []),
                )
            else:
                tag_list = [t for t in tags_val.replace(",", " ").split() if t]
                TaskwarriorBackend.add_task_structured(
                    description=desc_val,
                    project=proj_val,
                    priority=pri_val,
                    tags=tag_list,
                    due=due_val,
                )
            self.refresh_tasks()
            break

        dlg.destroy()

    def show_annotate_dialog(self, task):
        dlg = Gtk.Dialog(
            title=f"為任務 #{task.get('id', '')} 新增註解",
            transient_for=self.calendar_win,
            flags=0,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        dlg.set_default_size(380, 130)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)

        lbl = Gtk.Label(
            label=f"任務：{task.get('description', '')}\n請輸入註解內容：", xalign=0.0
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text("輸入註解...")
        box.pack_start(lbl, False, False, 0)
        box.pack_start(entry, False, False, 0)
        dlg.show_all()

        if dlg.run() == Gtk.ResponseType.OK:
            txt = entry.get_text().strip()
            if txt:
                TaskwarriorBackend.annotate_task(task.get("uuid", ""), txt)
                self.refresh_tasks()
        dlg.destroy()

    def confirm_delete_task(self, task):
        dlg = Gtk.MessageDialog(
            transient_for=self.calendar_win,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"確定要刪除任務 #{task.get('id', '')} 嗎？",
        )
        dlg.format_secondary_text(f"任務內容：「{task.get('description', '')}」")
        if dlg.run() == Gtk.ResponseType.YES:
            TaskwarriorBackend.delete_task(task.get("uuid", ""))
            self.refresh_tasks()
        dlg.destroy()

    def show_preferences_dialog(self, *_args):
        if self.prefs_win is not None:
            self.prefs_win.present()
            return

        win = Gtk.Window(title="Taskwarrior 任務管理 Plus — 偏好設定")
        win.set_default_size(510, 620)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_resizable(False)
        self.prefs_win = win

        def on_win_destroy(*_):
            self.prefs_win = None

        win.connect("destroy", on_win_destroy)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(14)
        outer.set_margin_bottom(14)
        outer.set_margin_start(18)
        outer.set_margin_end(18)

        # Section 1: Left-click Action & Calendar Options
        sec0_lbl = Gtk.Label(xalign=0.0)
        sec0_lbl.set_markup("<b>1. 左鍵點選行為與月曆視圖設定</b>")
        outer.pack_start(sec0_lbl, False, False, 0)

        rb_lc_cal = Gtk.RadioButton.new_with_label_from_widget(
            None, "左鍵點選面板圖示時：直接開啟「📅 雙欄式月曆任務看板」(推薦)"
        )
        rb_lc_menu = Gtk.RadioButton.new_with_label_from_widget(
            rb_lc_cal, "左鍵點選面板圖示時：開啟「📋 傳統下拉式快速選單」"
        )
        if self.config.get("left_click_action", "calendar") == "calendar":
            rb_lc_cal.set_active(True)
        else:
            rb_lc_menu.set_active(True)
        outer.pack_start(rb_lc_cal, False, False, 0)
        outer.pack_start(rb_lc_menu, False, False, 0)

        chk_cal_comp = Gtk.CheckButton(
            label="在月曆格子與當日清單中顯示歷史「已完成任務 (✓)」(依完成日期歸檔)"
        )
        chk_cal_comp.set_active(self.config.get("calendar_show_completed", True))
        outer.pack_start(chk_cal_comp, False, False, 0)

        cal_fs_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        cal_fs_box.set_margin_start(14)
        lbl_cal_fs = Gtk.Label(
            label="月曆看板字型大小 (pt/px，同步調整月曆格子與右側任務清單)：", xalign=0.0
        )
        adj_cal_fs = Gtk.Adjustment(
            value=float(self.config.get("calendar_font_size", 11)),
            lower=8.0,
            upper=22.0,
            step_increment=1.0,
            page_increment=2.0,
            page_size=0.0,
        )
        spin_cal_fs = Gtk.SpinButton(adjustment=adj_cal_fs, climb_rate=1.0, digits=0)
        cal_fs_box.pack_start(lbl_cal_fs, True, True, 0)
        cal_fs_box.pack_start(spin_cal_fs, False, False, 0)
        outer.pack_start(cal_fs_box, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Section 2: Panel Display Mode
        sec1_lbl = Gtk.Label(xalign=0.0)
        sec1_lbl.set_markup("<b>2. 面板圖示顯示模式</b>")
        outer.pack_start(sec1_lbl, False, False, 0)

        rb_badge = Gtk.RadioButton.new_with_label_from_widget(
            None, "模式 A：精簡任務計數方塊 ([TASK 1])"
        )
        rb_text = Gtk.RadioButton.new_with_label_from_widget(
            rb_badge, "模式 B：方塊 + 最高緊急度任務標題 ([TASK 1] [H] 加強mate-panel功能)"
        )
        if self.config.get("display_mode", "badge") == "badge":
            rb_badge.set_active(True)
        else:
            rb_text.set_active(True)
        outer.pack_start(rb_badge, False, False, 0)
        outer.pack_start(rb_text, False, False, 0)

        opt_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        opt_grid.set_margin_start(14)
        lbl_maxc = Gtk.Label(label="模式 B 任務標題最大字數：", xalign=0.0)
        adj_maxc = Gtk.Adjustment(
            value=float(self.config.get("max_title_chars", 22)),
            lower=8.0,
            upper=80.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin_maxc = Gtk.SpinButton(adjustment=adj_maxc, climb_rate=1.0, digits=0)

        lbl_duesoon = Gtk.Label(label="即將到期警戒天數 (Due <= N 天變橘色)：", xalign=0.0)
        adj_duesoon = Gtk.Adjustment(
            value=float(self.config.get("due_soon_days", 3)),
            lower=1.0,
            upper=30.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin_duesoon = Gtk.SpinButton(adjustment=adj_duesoon, climb_rate=1.0, digits=0)

        opt_grid.attach(lbl_maxc, 0, 0, 1, 1)
        opt_grid.attach(spin_maxc, 1, 0, 1, 1)
        opt_grid.attach(lbl_duesoon, 0, 1, 1, 1)
        opt_grid.attach(spin_duesoon, 1, 1, 1, 1)
        outer.pack_start(opt_grid, False, False, 0)

        chk_due_panel = Gtk.CheckButton(label="在模式 B 面板標題後方附上到期倒數 (例如 剩 4 天)")
        chk_due_panel.set_margin_start(14)
        chk_due_panel.set_active(self.config.get("show_due_in_panel", True))
        outer.pack_start(chk_due_panel, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Section 3: 3-Stage Font Colors & Background Style
        sec2_lbl = Gtk.Label(xalign=0.0)
        sec2_lbl.set_markup("<b>3. 面板三段式字形顏色與底色適應 (一般 / 高優先或即將到期 / 逾期或今日到期)</b>")
        outer.pack_start(sec2_lbl, False, False, 0)

        rb_font_dyn = Gtk.RadioButton.new_with_label_from_widget(
            None, "使用預設內建動態字形色階"
        )
        rb_font_cus = Gtk.RadioButton.new_with_label_from_widget(
            rb_font_dyn, "自訂三段式字形顏色 (隨任務緊急程度與到期日自動變換字色)："
        )
        if self.config.get("font_color_mode", "dynamic") == "dynamic":
            rb_font_dyn.set_active(True)
        else:
            rb_font_cus.set_active(True)
        outer.pack_start(rb_font_dyn, False, False, 0)
        outer.pack_start(rb_font_cus, False, False, 0)

        def make_color_btn(hex_val, title):
            cb = Gtk.ColorButton()
            cb.set_title(title)
            rgba = Gdk.RGBA()
            if not rgba.parse(hex_val):
                rgba.parse("#ffffff")
            cb.set_rgba(rgba)
            return cb

        color_norm_btn = make_color_btn(
            self.config.get("custom_font_color", "#ffffff"), "選擇一般狀態字形顏色"
        )
        color_warn_btn = make_color_btn(
            self.config.get("custom_warn_font_color", "#fbbf24"), "選擇高優先/即將到期字形顏色"
        )
        color_crit_btn = make_color_btn(
            self.config.get("custom_crit_font_color", "#f87171"), "選擇已逾期/今日到期字形顏色"
        )

        c_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        c_grid.set_margin_start(14)
        c_grid.attach(Gtk.Label(label="🟢 一般待辦字形色：", xalign=0.0), 0, 0, 1, 1)
        c_grid.attach(color_norm_btn, 1, 0, 1, 1)
        c_grid.attach(Gtk.Label(label="🟡 高優先/近期到期色：", xalign=0.0), 2, 0, 1, 1)
        c_grid.attach(color_warn_btn, 3, 0, 1, 1)
        c_grid.attach(Gtk.Label(label="🔴 已逾期/今日到期色：", xalign=0.0), 0, 1, 1, 1)
        c_grid.attach(color_crit_btn, 1, 1, 1, 1)

        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_dark_theme = Gtk.Button(label="深色面板色票")
        btn_light_theme = Gtk.Button(label="淺色面板色票")
        preset_box.pack_start(btn_dark_theme, False, False, 0)
        preset_box.pack_start(btn_light_theme, False, False, 0)
        c_grid.attach(preset_box, 2, 1, 2, 1)
        outer.pack_start(c_grid, False, False, 0)

        bg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bg_lbl = Gtk.Label(label="方塊背景底色：", xalign=0.0)
        rb_bg_dark = Gtk.RadioButton.new_with_label_from_widget(None, "深色底")
        rb_bg_light = Gtk.RadioButton.new_with_label_from_widget(rb_bg_dark, "淺色底")
        rb_bg_trans = Gtk.RadioButton.new_with_label_from_widget(rb_bg_dark, "完全透明 (無底色)")
        cur_bg = self.config.get("badge_bg_style", "dark")
        if cur_bg == "dark":
            rb_bg_dark.set_active(True)
        elif cur_bg == "light":
            rb_bg_light.set_active(True)
        else:
            rb_bg_trans.set_active(True)
        bg_box.pack_start(bg_lbl, False, False, 0)
        bg_box.pack_start(rb_bg_dark, False, False, 0)
        bg_box.pack_start(rb_bg_light, False, False, 0)
        bg_box.pack_start(rb_bg_trans, False, False, 0)
        outer.pack_start(bg_box, False, False, 0)

        chk_shadow = Gtk.CheckButton(label="啟用文字高對比描邊陰影 (透明或花紋面板底色時保持清晰可讀)")
        chk_shadow.set_active(self.config.get("text_shadow", False))
        outer.pack_start(chk_shadow, False, False, 0)

        def rgba_to_hex(rgba):
            r = max(0, min(255, int(round(rgba.red * 255))))
            g = max(0, min(255, int(round(rgba.green * 255))))
            b = max(0, min(255, int(round(rgba.blue * 255))))
            return f"#{r:02x}{g:02x}{b:02x}"

        def apply_changes(*_):
            self.config["left_click_action"] = "calendar" if rb_lc_cal.get_active() else "menu"
            self.config["calendar_show_completed"] = chk_cal_comp.get_active()
            self.config["calendar_font_size"] = int(spin_cal_fs.get_value_as_int())
            self.config["display_mode"] = "badge" if rb_badge.get_active() else "badge_text"
            self.config["max_title_chars"] = int(spin_maxc.get_value_as_int())
            self.config["due_soon_days"] = int(spin_duesoon.get_value_as_int())
            self.config["show_due_in_panel"] = chk_due_panel.get_active()
            self.config["font_color_mode"] = "dynamic" if rb_font_dyn.get_active() else "custom"
            self.config["custom_font_color"] = rgba_to_hex(color_norm_btn.get_rgba())
            self.config["custom_warn_font_color"] = rgba_to_hex(color_warn_btn.get_rgba())
            self.config["custom_crit_font_color"] = rgba_to_hex(color_crit_btn.get_rgba())
            if rb_bg_dark.get_active():
                self.config["badge_bg_style"] = "dark"
            elif rb_bg_light.get_active():
                self.config["badge_bg_style"] = "light"
            else:
                self.config["badge_bg_style"] = "transparent"
            self.config["text_shadow"] = chk_shadow.get_active()
            self._save_config()
            self.refresh_tasks()

        def on_color_picked(_btn):
            rb_font_cus.set_active(True)
            apply_changes()

        def set_quick_theme(hx_n, hx_w, hx_c):
            for btn, hx in ((color_norm_btn, hx_n), (color_warn_btn, hx_w), (color_crit_btn, hx_c)):
                c = Gdk.RGBA()
                c.parse(hx)
                btn.set_rgba(c)
            rb_font_cus.set_active(True)
            apply_changes()

        for c_btn in (color_norm_btn, color_warn_btn, color_crit_btn):
            c_btn.connect("color-set", on_color_picked)
        btn_dark_theme.connect(
            "clicked", lambda *_: set_quick_theme("#ffffff", "#fbbf24", "#f87171")
        )
        btn_light_theme.connect(
            "clicked", lambda *_: set_quick_theme("#111827", "#b45309", "#b91c1c")
        )

        for w_ctrl in (
            rb_lc_cal,
            rb_lc_menu,
            chk_cal_comp,
            rb_badge,
            rb_text,
            chk_due_panel,
            rb_font_dyn,
            rb_font_cus,
            rb_bg_dark,
            rb_bg_light,
            rb_bg_trans,
            chk_shadow,
        ):
            w_ctrl.connect("toggled", apply_changes)
        for sp_ctrl in (spin_cal_fs, spin_maxc, spin_duesoon):
            sp_ctrl.connect("value-changed", apply_changes)

        # Bottom buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_cal = Gtk.Button(label="📅 開啟月曆看板")
        btn_cal.connect("clicked", lambda *_: self.show_calendar_window())
        btn_add = Gtk.Button(label="➕ 新增任務")
        btn_add.connect("clicked", lambda *_: self.show_add_or_edit_task_dialog(task=None))
        btn_close = Gtk.Button(label="關閉")
        btn_close.connect("clicked", lambda *_: win.destroy())
        btn_box.pack_start(btn_cal, False, False, 0)
        btn_box.pack_start(btn_add, False, False, 0)
        btn_box.pack_end(btn_close, False, False, 0)
        outer.pack_end(btn_box, False, False, 0)

        win.add(outer)
        win.show_all()

    def show_about_dialog(self, *_args):
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("Taskwarrior 任務管理 Plus")
        dlg.set_version("1.1.0")
        dlg.set_comments(
            "事件驅動 (`Gio.FileMonitor` 監聽 ~/.task) 的 MATE 面板 Taskwarrior 任務管理 Applet。\n"
            "內建雙欄式互動彩色月曆任務看板、歷史完成紀錄歸檔回顧與三段式自訂字形顏色。"
        )
        dlg.set_website("https://github.com/sophAi/matepanel-applet-plus")
        dlg.run()
        dlg.destroy()

    def _on_left_click_action_toggled(self, radio_item, action_name):
        if not radio_item.get_active():
            return
        self.config["left_click_action"] = action_name
        self._save_config()
        self._update_tooltip()

    def _on_display_mode_toggled(self, radio_item, mode):
        if not radio_item.get_active():
            return
        self.config["display_mode"] = mode
        self._save_config()
        self._update_canvas_size()

    def _on_font_color_mode_toggled(self, radio_item, mode):
        if not radio_item.get_active():
            return
        self.config["font_color_mode"] = mode
        self._save_config()
        self.canvas.queue_draw()

    def _apply_preset_font_theme(self, hx_norm, hx_warn, hx_crit):
        self.config["font_color_mode"] = "custom"
        self.config["custom_font_color"] = hx_norm
        self.config["custom_warn_font_color"] = hx_warn
        self.config["custom_crit_font_color"] = hx_crit
        self._save_config()
        self.canvas.queue_draw()

    def _on_badge_bg_toggled(self, radio_item, bg_style):
        if not radio_item.get_active():
            return
        self.config["badge_bg_style"] = bg_style
        self._save_config()
        self.canvas.queue_draw()

    def _on_destroy(self, *_args):
        if self.debounce_id is not None:
            GLib.source_remove(self.debounce_id)
            self.debounce_id = None
        if self.periodic_timer_id is not None:
            GLib.source_remove(self.periodic_timer_id)
            self.periodic_timer_id = None
        if self.file_monitor is not None:
            self.file_monitor.cancel()
            self.file_monitor = None
        if self.calendar_win is not None:
            self.calendar_win.destroy()
            self.calendar_win = None
        if self.prefs_win is not None:
            self.prefs_win.destroy()
            self.prefs_win = None


def setup_applet_context_menu(applet, widget):
    action_group = Gtk.ActionGroup(name="TaskwarriorAppletActions")
    action_group.add_actions(
        [
            (
                "CalendarViewAction",
                Gtk.STOCK_INDEX,
                "月曆任務看板 (_Calendar View)...",
                None,
                "開啟雙欄式互動月曆任務看板",
                widget.show_calendar_window,
            ),
            (
                "AddTaskAction",
                Gtk.STOCK_ADD,
                "新增任務 (_Add Task)...",
                None,
                "新增一筆 Taskwarrior 待辦任務",
                lambda *_: widget.show_add_or_edit_task_dialog(task=None),
            ),
            (
                "ShowTasksMenuAction",
                Gtk.STOCK_INFO,
                "傳統下拉任務選單 (_Quick Menu)...",
                None,
                "顯示傳統下拉式待辦任務與操作選單",
                lambda *_: widget.popup_task_menu(None),
            ),
            (
                "PreferencesAction",
                Gtk.STOCK_PREFERENCES,
                "偏好設定 (_Preferences)...",
                None,
                "設定左鍵預設行為、面板顯示樣式與三段字形顏色",
                widget.show_preferences_dialog,
            ),
            (
                "AboutAction",
                Gtk.STOCK_ABOUT,
                "關於 (_About)...",
                None,
                "關於 Taskwarrior 任務管理 Plus",
                widget.show_about_dialog,
            ),
        ]
    )

    menu_xml = (
        '<menuitem name="CalendarView" action="CalendarViewAction"/>'
        '<menuitem name="AddTask" action="AddTaskAction"/>'
        '<menuitem name="ShowTasksMenu" action="ShowTasksMenuAction"/>'
        "<separator/>"
        '<menuitem name="Preferences" action="PreferencesAction"/>'
        '<menuitem name="About" action="AboutAction"/>'
        "<separator/>"
    )
    applet.setup_menu(menu_xml, action_group)


def applet_fill(applet):
    os.chdir(os.path.expanduser("~"))
    applet.set_flags(MatePanelApplet.AppletFlags.EXPAND_MINOR)
    applet.set_background_widget(applet)
    applet.set_events(
        applet.get_events()
        | Gdk.EventMask.BUTTON_PRESS_MASK
        | Gdk.EventMask.BUTTON_RELEASE_MASK
    )

    widget = TaskwarriorWidget(applet=applet)
    init_size = applet.get_size()
    if init_size:
        widget.set_panel_size(init_size)

    setup_applet_context_menu(applet, widget)

    applet.connect("button-press-event", widget.handle_applet_button_press)
    applet.connect("change-size", lambda _ap, sz: widget.set_panel_size(sz))

    applet.add(widget)
    applet.show_all()


def applet_factory(applet, iid, _data):
    if iid not in ("TaskwarriorApplet", "MateTaskwarriorApplet"):
        return False
    applet_fill(applet)
    return True


def run_standalone_window():
    win = Gtk.Window(title="MATE Taskwarrior Applet - 預覽測試視窗")
    win.set_default_size(640, 170)
    win.set_position(Gtk.WindowPosition.CENTER)
    win.connect("destroy", Gtk.main_quit)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    vbox.set_margin_top(16)
    vbox.set_margin_bottom(16)
    vbox.set_margin_start(16)
    vbox.set_margin_end(16)

    info_lbl = Gtk.Label(xalign=0.0)
    info_lbl.set_markup(
        "<b>MATE Panel 模擬預覽列 — Taskwarrior 任務管理 Plus (含雙欄月曆看板)</b>\n"
        "<small>• <b>左鍵點選</b>下方 <tt>[TASK]</tt> 圖示：直接開啟/收合<b>📅 雙欄式互動月曆任務看板</b>\n"
        "• <b>右鍵點選</b>下方圖示（或按右側按鈕）：開啟傳統下拉式快速選單或偏好設定</small>"
    )
    vbox.pack_start(info_lbl, False, False, 0)

    panel_frame = Gtk.EventBox()
    css_provider = Gtk.CssProvider()
    css_provider.load_from_data(
        b".simulated-panel-dark { background-color: #1e2229; border-radius: 6px; padding: 6px; }"
        b".simulated-panel-light { background-color: #e5e7eb; border-radius: 6px; padding: 6px; }"
    )
    panel_frame.get_style_context().add_provider(
        css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    panel_frame.get_style_context().add_class("simulated-panel-dark")

    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    hbox.set_margin_top(4)
    hbox.set_margin_bottom(4)
    hbox.set_margin_start(10)
    hbox.set_margin_end(10)

    panel_lbl = Gtk.Label()
    panel_lbl.set_markup("<span foreground='#9aa0a6'><small>模擬面板 👉</small></span>")
    hbox.pack_start(panel_lbl, False, False, 0)

    widget = TaskwarriorWidget(applet=None)
    widget.set_panel_size(32)
    hbox.pack_start(widget, False, False, 0)

    btn_prefs = Gtk.Button(label="⚙️ 偏好設定")
    btn_prefs.connect("clicked", widget.show_preferences_dialog)
    hbox.pack_end(btn_prefs, False, False, 0)

    btn_cal = Gtk.Button(label="📅 月曆看板")
    btn_cal.connect("clicked", widget.show_calendar_window)
    hbox.pack_end(btn_cal, False, False, 0)

    btn_add = Gtk.Button(label="➕ 新增任務")
    btn_add.connect("clicked", lambda *_: widget.show_add_or_edit_task_dialog(task=None))
    hbox.pack_end(btn_add, False, False, 0)

    is_dark = [True]

    def toggle_sim_bg(_btn):
        ctx_style = panel_frame.get_style_context()
        if is_dark[0]:
            ctx_style.remove_class("simulated-panel-dark")
            ctx_style.add_class("simulated-panel-light")
            panel_lbl.set_markup("<span foreground='#374151'><small>模擬淺色面板 👉</small></span>")
            is_dark[0] = False
        else:
            ctx_style.remove_class("simulated-panel-light")
            ctx_style.add_class("simulated-panel-dark")
            panel_lbl.set_markup("<span foreground='#9aa0a6'><small>模擬深色面板 👉</small></span>")
            is_dark[0] = True

    btn_bg = Gtk.Button(label="🌓 深/淺面板")
    btn_bg.connect("clicked", toggle_sim_bg)
    hbox.pack_end(btn_bg, False, False, 0)

    panel_frame.add(hbox)
    vbox.pack_start(panel_frame, False, False, 0)

    win.add(vbox)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    if "--window" in sys.argv or "-w" in sys.argv:
        run_standalone_window()
    elif "--calendar" in sys.argv or "-c" in sys.argv:
        w = TaskwarriorWidget(applet=None)
        w.show_calendar_window()
        w.calendar_win.connect("destroy", Gtk.main_quit)
        Gtk.main()
    elif "--dump" in sys.argv:
        data = TaskwarriorBackend.fetch_all()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        if not HAS_MATE_PANEL:
            sys.stderr.write("Error: MatePanelApplet GI bindings not available.\n")
            sys.exit(1)
        MatePanelApplet.Applet.factory_main(
            "TaskwarriorAppletFactory",
            True,
            MatePanelApplet.Applet.__gtype__,
            applet_factory,
            None,
        )
