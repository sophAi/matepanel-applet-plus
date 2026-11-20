#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Shared Taskwarrior Backend & Compact Calendar+Editor Pane (`taskwarrior_shared.py`).

Used by:
- applets/taskwarrior/taskwarrior_applet.py (Backend CLI wrapper & date parsers)
- applets/clock-weather/clock_weather_applet.py (Embedded Taskwarrior Calendar tab without Pomodoro)
"""

import calendar
from datetime import date, datetime, timedelta, timezone
import html
import json
import math
import os
import subprocess

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk
import cairo


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
    def is_available(cls):
        return os.path.isfile(cls.TASK_BIN) and os.access(cls.TASK_BIN, os.X_OK)

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
    def set_active_state(cls, uuid_str, should_start):
        if not uuid_str:
            return 0, "", ""
        action = "start" if should_start else "stop"
        return cls._run_task([f"uuid:{uuid_str}", action])

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


def _draw_rounded_rect(ctx, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


class TaskwarriorCalendarPane(Gtk.Box):
    """
    Clean, Pomodoro-free dual-column Taskwarrior Monthly Calendar + Click-Date Task Editor Pane.
    Designed to embed cleanly as a Notebook tab inside ClockWeatherPopupWindow.
    """

    WEEKDAYS_ZH = ["一", "二", "三", "四", "五", "六", "日"]

    def __init__(self, font_size_cb=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_margin_top(8)
        self.set_margin_bottom(6)
        self.set_margin_start(8)
        self.set_margin_end(8)

        self._font_size_cb = font_size_cb
        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.selected_date = today
        self.cell_dates = []
        self._cal_grid_rect = (0, 0, 1, 1)

        self.pending_tasks = []
        self.completed_tasks = []
        self._by_date_pending = {}
        self._by_date_completed = {}
        self._unscheduled = []

        # Top navigation bar
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_prev = Gtk.Button(label="◀")
        btn_today = Gtk.Button(label="今天")
        btn_next = Gtk.Button(label="▶")
        btn_prev.connect("clicked", lambda *_: self._shift_month(-1))
        btn_today.connect("clicked", lambda *_: self._jump_today())
        btn_next.connect("clicked", lambda *_: self._shift_month(1))

        self.month_lbl = Gtk.Label(xalign=0.0)
        self.month_lbl.set_margin_start(6)

        btn_undo = Gtk.Button(label="↩ 復原")
        btn_undo.set_tooltip_text("執行 task undo 復原上一步變更")
        btn_undo.connect("clicked", self._on_undo)
        btn_reload = Gtk.Button(label="🔄 更新")
        btn_reload.connect("clicked", lambda *_: self.reload_tasks())

        nav.pack_start(btn_prev, False, False, 0)
        nav.pack_start(btn_today, False, False, 0)
        nav.pack_start(btn_next, False, False, 0)
        nav.pack_start(self.month_lbl, True, True, 0)
        nav.pack_end(btn_reload, False, False, 0)
        nav.pack_end(btn_undo, False, False, 0)
        self.pack_start(nav, False, False, 0)

        # Dual-column split: Left 7x6 Calendar, Right Date Task Editor
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        self.cal_canvas = Gtk.DrawingArea()
        self.cal_canvas.set_size_request(470, 380)
        self.cal_canvas.set_hexpand(True)
        self.cal_canvas.set_vexpand(True)
        self.cal_canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.cal_canvas.connect("draw", self._on_draw_calendar)
        self.cal_canvas.connect("button-press-event", self._on_calendar_click)
        content.pack_start(self.cal_canvas, True, True, 0)

        # Right Date Task Editor Pane
        right_frame = Gtk.Frame()
        right_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        right_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_vbox.set_margin_top(8)
        right_vbox.set_margin_bottom(8)
        right_vbox.set_margin_start(10)
        right_vbox.set_margin_end(10)
        right_vbox.set_size_request(320, -1)

        self.day_hdr_lbl = Gtk.Label(xalign=0.0)
        right_vbox.pack_start(self.day_hdr_lbl, False, False, 0)

        # Quick add bar (automatically binds due date to selected_date)
        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.entry_quick_add = Gtk.Entry()
        self.entry_quick_add.set_hexpand(True)
        self.entry_quick_add.set_placeholder_text("新增此日任務 (Enter)...")
        self.entry_quick_add.connect("activate", self._on_quick_add)
        btn_add = Gtk.Button(label="＋")
        btn_add.set_tooltip_text("新增任務至所選日期")
        btn_add.connect("clicked", self._on_quick_add)
        add_row.pack_start(self.entry_quick_add, True, True, 0)
        add_row.pack_start(btn_add, False, False, 0)
        right_vbox.pack_start(add_row, False, False, 0)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.task_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        right_scroll.add(self.task_list_box)
        right_vbox.pack_start(right_scroll, True, True, 0)

        right_frame.add(right_vbox)
        content.pack_start(right_frame, False, False, 0)
        self.pack_start(content, True, True, 0)

    def _get_font_size(self):
        if callable(self._font_size_cb):
            try:
                return max(8, min(18, int(self._font_size_cb())))
            except Exception:
                pass
        return 11

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
        max_d = calendar.monthrange(y, m)[1]
        self.selected_date = date(y, m, min(self.selected_date.day, max_d))
        self._refresh_ui()

    def _jump_today(self):
        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.selected_date = today
        self._refresh_ui()

    def reload_tasks(self):
        if not TaskwarriorBackend.is_available():
            self.pending_tasks = []
            self.completed_tasks = []
            self._by_date_pending = {}
            self._by_date_completed = {}
            self._unscheduled = []
            self._refresh_ui()
            return

        data = TaskwarriorBackend.fetch_all(due_soon_days=3, completed_limit=50)
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
        self._refresh_ui()

    def _refresh_ui(self):
        self._compute_month_cells()
        fs = self._get_font_size()
        t_pt = int((fs + 2.0) * 1024)
        s_pt = int(max(8.5, fs - 1.0) * 1024)
        self.month_lbl.set_markup(
            f"<span size='{t_pt}'><b>{self.view_year} 年 {self.view_month:02d} 月</b></span>   "
            f"<span size='{s_pt}' foreground='#94a3b8'>待辦 {len(self.pending_tasks)} 筆</span>"
        )
        self.cal_canvas.queue_draw()
        self._populate_day_tasks()

    def _on_calendar_click(self, _widget, event):
        if event.button != 1 or not self.cell_dates:
            return False
        gx, gy, col_w, row_h = self._cal_grid_rect
        if event.x < gx or event.y < gy:
            return False
        c_idx = int((event.x - gx) // col_w)
        r_idx = int((event.y - gy) // row_h)
        if 0 <= c_idx < 7 and 0 <= r_idx < 6:
            idx = r_idx * 7 + c_idx
            if idx < len(self.cell_dates):
                clicked_d = self.cell_dates[idx]
                self.selected_date = clicked_d
                if clicked_d.month != self.view_month or clicked_d.year != self.view_year:
                    self.view_year = clicked_d.year
                    self.view_month = clicked_d.month
                self._refresh_ui()
                return True
        return False

    def _on_draw_calendar(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)
        base_fs = float(max(8, min(16, self._get_font_size())))

        _draw_rounded_rect(ctx, 0, 0, w, h, 8.0)
        ctx.set_source_rgba(0.10, 0.13, 0.17, 0.96)
        ctx.fill()

        header_h = max(22.0, base_fs * 2.1)
        grid_x = 4.0
        grid_y = header_h + 2.0
        col_w = (w - 8.0) / 7.0
        row_h = max(28.0, (h - grid_y - 4.0) / 6.0)
        self._cal_grid_rect = (grid_x, grid_y, col_w, row_h)

        # Weekday header
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(base_fs)
        for ci, wd in enumerate(self.WEEKDAYS_ZH):
            cx = grid_x + ci * col_w
            ext = ctx.text_extents(wd)
            if ci >= 5:
                ctx.set_source_rgba(0.97, 0.56, 0.48, 0.95)
            else:
                ctx.set_source_rgba(0.76, 0.82, 0.88, 0.95)
            ctx.move_to(cx + (col_w - ext.width) / 2.0 - ext.x_bearing, header_h * 0.68)
            ctx.show_text(wd)

        today = date.today()
        if not self.cell_dates:
            self._compute_month_cells()

        for idx, d_obj in enumerate(self.cell_dates):
            r_idx = idx // 7
            c_idx = idx % 7
            cx = grid_x + c_idx * col_w + 1.5
            cy = grid_y + r_idx * row_h + 1.5
            cw = col_w - 3.0
            ch = row_h - 3.0

            d_str = d_obj.strftime("%Y-%m-%d")
            is_cur_m = d_obj.month == self.view_month
            is_today = d_obj == today
            is_sel = d_obj == self.selected_date
            p_list = self._by_date_pending.get(d_str, [])
            c_list = self._by_date_completed.get(d_str, [])
            has_crit = any(t.get("_due_level") == "crit" for t in p_list)

            _draw_rounded_rect(ctx, cx, cy, cw, ch, 5.0)
            if is_sel:
                ctx.set_source_rgba(0.16, 0.34, 0.54, 0.96)
            elif is_today:
                ctx.set_source_rgba(0.12, 0.28, 0.26, 0.95)
            elif p_list:
                ctx.set_source_rgba(0.16, 0.20, 0.27, 0.95)
            elif is_cur_m:
                ctx.set_source_rgba(0.13, 0.16, 0.20, 0.90)
            else:
                ctx.set_source_rgba(0.09, 0.11, 0.14, 0.55)
            ctx.fill_preserve()

            if is_sel:
                ctx.set_source_rgba(0.32, 0.76, 1.00, 1.0)
                ctx.set_line_width(1.8)
            elif is_today:
                ctx.set_source_rgba(0.20, 0.85, 0.62, 0.95)
                ctx.set_line_width(1.5)
            elif has_crit:
                ctx.set_source_rgba(0.96, 0.38, 0.38, 0.85)
                ctx.set_line_width(1.1)
            else:
                ctx.set_source_rgba(0.22, 0.26, 0.32, 0.60)
                ctx.set_line_width(0.8)
            ctx.stroke()

            # Day number
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(base_fs)
            if is_today:
                ctx.set_source_rgba(0.25, 0.95, 0.65, 1.0)
            elif not is_cur_m:
                ctx.set_source_rgba(0.45, 0.50, 0.58, 0.70)
            elif c_idx >= 5:
                ctx.set_source_rgba(0.98, 0.62, 0.55, 0.96)
            else:
                ctx.set_source_rgba(0.92, 0.96, 1.00, 0.96)
            ctx.move_to(cx + 5.0, cy + base_fs + 3.0)
            ctx.show_text(str(d_obj.day))

            # Task count badges (pure Cairo text, no emojis)
            if p_list or c_list:
                b_fs = max(7.5, min(base_fs - 1.5, cw * 0.22))
                ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                ctx.set_font_size(b_fs)
                by = cy + ch - 4.0
                cur_x = cx + 4.5
                if p_list:
                    txt = f"{len(p_list)}"
                    ext = ctx.text_extents(txt)
                    pill_w = ext.width + 8.0
                    pill_h = b_fs + 3.5
                    _draw_rounded_rect(ctx, cur_x, by - pill_h + 1.5, pill_w, pill_h, 3.0)
                    if has_crit:
                        ctx.set_source_rgba(0.92, 0.28, 0.32, 0.92)
                    else:
                        ctx.set_source_rgba(0.18, 0.56, 0.88, 0.92)
                    ctx.fill()
                    ctx.set_source_rgba(1.0, 1.0, 1.0, 1.0)
                    ctx.move_to(cur_x + 4.0 - ext.x_bearing, by - 1.0)
                    ctx.show_text(txt)
                    cur_x += pill_w + 3.0
                if c_list:
                    txt = f"✓{len(c_list)}"
                    ext = ctx.text_extents(txt)
                    if cur_x + ext.width + 4.0 <= cx + cw:
                        ctx.set_source_rgba(0.28, 0.84, 0.58, 0.95)
                        ctx.move_to(cur_x - ext.x_bearing, by - 1.0)
                        ctx.show_text(txt)
        return False

    def _populate_day_tasks(self):
        for child in list(self.task_list_box.get_children()):
            self.task_list_box.remove(child)

        fs = self._get_font_size()
        p_lg = int((fs + 1.5) * 1024)
        p_sm = int(max(8.5, fs - 1.0) * 1024)

        d_obj = self.selected_date
        d_str = d_obj.strftime("%Y-%m-%d")
        wd_zh = self.WEEKDAYS_ZH[d_obj.weekday()]
        is_today = d_obj == date.today()
        tag_today = " <span foreground='#34d399'>(今天)</span>" if is_today else ""
        self.day_hdr_lbl.set_markup(
            f"<span size='{p_lg}'><b>{d_obj.month}/{d_obj.day} 週{wd_zh}</b></span>{tag_today}"
        )

        if not TaskwarriorBackend.is_available():
            lb = Gtk.Label(xalign=0.0)
            lb.set_line_wrap(True)
            lb.set_markup(
                f"<span size='{p_sm}' foreground='#f87171'>尚未安裝 Taskwarrior (/usr/bin/task)。\n"
                f"安裝後即可於此分頁瀏覽與編輯待辦事項。</span>"
            )
            self.task_list_box.pack_start(lb, False, False, 4)
            self.task_list_box.show_all()
            return

        p_list = self._by_date_pending.get(d_str, [])
        c_list = self._by_date_completed.get(d_str, [])

        if not p_list and not c_list:
            empty_lbl = Gtk.Label(xalign=0.0)
            empty_lbl.set_markup(
                f"<span size='{p_sm}' foreground='#94a3b8'>此日期目前沒有排定任務。\n可直接於上方輸入框新增任務。</span>"
            )
            self.task_list_box.pack_start(empty_lbl, False, False, 4)
        else:
            for t in p_list:
                self.task_list_box.pack_start(self._build_task_row(t, completed=False, p_sm=p_sm), False, False, 0)
            if c_list:
                sep = Gtk.Label(xalign=0.0)
                sep.set_markup(f"<span size='{p_sm}' foreground='#64748b'><b>已完成 ({len(c_list)})</b></span>")
                self.task_list_box.pack_start(sep, False, False, 3)
                for t in c_list:
                    self.task_list_box.pack_start(self._build_task_row(t, completed=True, p_sm=p_sm), False, False, 0)

        if is_today and self._unscheduled:
            self.task_list_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
            u_hdr = Gtk.Label(xalign=0.0)
            u_hdr.set_markup(
                f"<span size='{p_sm}' foreground='#94a3b8'><b>未排定到期日 ({len(self._unscheduled)})</b></span>"
            )
            self.task_list_box.pack_start(u_hdr, False, False, 2)
            for t in self._unscheduled[:12]:
                self.task_list_box.pack_start(self._build_task_row(t, completed=False, p_sm=p_sm), False, False, 0)

        self.task_list_box.show_all()

    def _build_task_row(self, task, completed, p_sm):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        uuid_str = task.get("uuid", "")
        desc = html.escape(str(task.get("description", "")))
        proj = task.get("project") or ""
        pri = task.get("priority") or ""
        tags = task.get("tags") or []

        btn_done = Gtk.Button(label="↩" if completed else "✓")
        btn_done.set_relief(Gtk.ReliefStyle.NONE)
        btn_done.set_tooltip_text("恢復為待辦" if completed else "標記為已完成")
        if completed:
            btn_done.connect("clicked", lambda *_, u=uuid_str: (TaskwarriorBackend.restore_task(u), self.reload_tasks()))
        else:
            btn_done.connect("clicked", lambda *_, u=uuid_str: (TaskwarriorBackend.mark_done(u), self.reload_tasks()))
        row.pack_start(btn_done, False, False, 0)

        meta = []
        if pri:
            p_col = "#f87171" if pri == "H" else ("#fbbf24" if pri == "M" else "#38bdf8")
            meta.append(f"<span foreground='{p_col}'><b>[{html.escape(pri)}]</b></span>")
        if proj:
            meta.append(f"<span foreground='#94a3b8'>({html.escape(proj)})</span>")
        if tags:
            meta.append(f"<span foreground='#38bdf8'>{' '.join('+' + html.escape(x) for x in tags[:3])}</span>")

        title_markup = f"<s><span foreground='#64748b'>{desc}</span></s>" if completed else f"<b>{desc}</b>"
        if meta:
            title_markup += "\n" + " ".join(meta)

        lbl = Gtk.Label(xalign=0.0)
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(24)
        lbl.set_markup(f"<span size='{p_sm}'>{title_markup}</span>")
        row.pack_start(lbl, True, True, 0)

        if not completed:
            btn_edit = Gtk.Button(label="✎")
            btn_edit.set_relief(Gtk.ReliefStyle.NONE)
            btn_edit.set_tooltip_text("編輯此任務")
            btn_edit.connect("clicked", lambda *_, tk=task: self._open_edit_dialog(tk))
            row.pack_end(btn_edit, False, False, 0)

        btn_del = Gtk.Button(label="✕")
        btn_del.set_relief(Gtk.ReliefStyle.NONE)
        btn_del.set_tooltip_text("刪除此任務")
        btn_del.connect("clicked", lambda *_, u=uuid_str: (TaskwarriorBackend.delete_task(u), self.reload_tasks()))
        row.pack_end(btn_del, False, False, 0)
        return row

    def _on_quick_add(self, *_args):
        text = self.entry_quick_add.get_text().strip()
        if not text or not TaskwarriorBackend.is_available():
            return
        due_str = self.selected_date.strftime("%Y-%m-%d")
        TaskwarriorBackend.add_task_structured(description=text, due=due_str)
        self.entry_quick_add.set_text("")
        self.reload_tasks()

    def _on_undo(self, *_args):
        if TaskwarriorBackend.is_available():
            TaskwarriorBackend.undo_last()
            self.reload_tasks()

    def _open_edit_dialog(self, task):
        toplevel = self.get_toplevel()
        parent = toplevel if isinstance(toplevel, Gtk.Window) else None
        dlg = Gtk.Dialog(
            title="編輯 Taskwarrior 任務",
            transient_for=parent,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
        )
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
        e_tags.set_placeholder_text("空白分隔，如 work urgent")

        e_due = Gtk.Entry()
        e_due.set_text(str(task.get("_due_date") or self.selected_date.strftime("%Y-%m-%d")))
        e_due.set_placeholder_text("YYYY-MM-DD（留空清除到期日）")

        rows = [
            ("任務標題：", e_desc),
            ("專案：", e_proj),
            ("優先級：", c_pri),
            ("標籤：", e_tags),
            ("到期日：", e_due),
        ]
        for ri, (lbl_txt, w_ctrl) in enumerate(rows):
            grid.attach(Gtk.Label(label=lbl_txt, xalign=0.0), 0, ri, 1, 1)
            grid.attach(w_ctrl, 1, ri, 1, 1)

        area.pack_start(grid, True, True, 0)
        dlg.show_all()

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            new_desc = e_desc.get_text().strip()
            if new_desc:
                TaskwarriorBackend.modify_task_structured(
                    uuid_str=task.get("uuid", ""),
                    description=new_desc,
                    project=e_proj.get_text().strip(),
                    priority=c_pri.get_active_id() or "",
                    tags_str=e_tags.get_text().strip(),
                    due=e_due.get_text().strip(),
                    orig_tags=task.get("tags") or [],
                )
                self.reload_tasks()
        dlg.destroy()
