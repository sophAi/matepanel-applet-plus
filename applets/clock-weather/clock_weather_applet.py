#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
MATE Panel Clock, Weather Calendar & Forecast Applet (ClockWeatherApplet)

Features:
- Replaces MATE Panel's built-in clock/calendar/weather applet with unified custom styling:
  - Displays current desktop GTK Icon Theme weather icon (weather-clear, weather-showers,
    weather-storm, weather-fog, etc.), temperature, date/weekday, and HH:MM time (no seconds).
  - Exact minute-boundary timer alignment (0.0% idle CPU overhead, zero subprocesses).
  - Customizable badge background color & opacity (0..100%), font color, font opacity & shadow (borderless).
- Free, API-key-free Open-Meteo Weather + Air Quality (PM2.5 / US AQI) backend:
  - Default city: Taipei (25.0330, 121.5654); built-in Geocoding city search in Preferences.
  - Local JSON cache (~/.config/matepanel-applet-plus/weather-cache.json) for instant startup.
- Interactive 7x6 Weather Calendar Dashboard:
  - Embeds desktop theme weather icons, high/low temps, and rain probability directly inside
    today + next 6 days calendar cells.
  - Click any date in the 7-day window to view detailed forecast (instant 0ms switch):
    current conditions + PM2.5/AQI (for today) and 3-hourly temperature & rain probability bars.
  - Optional World Clock strip using Python stdlib `zoneinfo`.
"""

import calendar
from datetime import date, datetime, timedelta
import html
import json
import math
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GLib, GObject, Gtk
import cairo

try:
    gi.require_version("MatePanelApplet", "4.0")
    from gi.repository import MatePanelApplet
    HAS_MATE_PANEL = True
except (ValueError, ImportError):
    HAS_MATE_PANEL = False


CONFIG_DIR = os.path.expanduser("~/.config/matepanel-applet-plus")
CONFIG_PATH = os.path.join(CONFIG_DIR, "clock-weather.json")
WEATHER_CACHE_PATH = os.path.join(CONFIG_DIR, "weather-cache.json")

DEFAULT_CONFIG = {
    "city_name": "台北",
    "latitude": 25.0330,
    "longitude": 121.5654,
    "weather_interval_min": 20,          # 天氣更新間隔 (分鐘)
    "show_weather_icon": True,           # 面板顯示當前桌面主題天氣圖示
    "show_weather_temp": True,           # 面板顯示即時溫度
    "show_date": True,                   # 面板顯示日期 (MM/DD)
    "show_weekday": True,                # 面板顯示星期
    "use_24h": True,                     # 24小時制 (True) 或 12小時制 (False)
    "calendar_font_size": 11,            # 彈窗月曆與預報字型大小 (pt)
    "show_world_clock": True,            # 彈窗底部顯示多時區世界時鐘
    "world_clocks": "Asia/Tokyo, Europe/London, America/New_York",
    "badge_bg_color": "#182234",         # 面板背景顏色
    "badge_bg_opacity": 90,              # 面板背景不透明度 (0..100%)
    "font_color_mode": "dynamic",        # "dynamic" or "custom"
    "custom_font_color": "#ffffff",      # 自訂面板字形顏色
    "font_opacity": 100,                 # 面板文字不透明度 (10..100%)
    "text_shadow": False,                # 面板文字高對比描邊陰影
}

WORLD_CLOCK_PRESETS = [
    ("東京", "Asia/Tokyo"),
    ("首爾", "Asia/Seoul"),
    ("香港", "Asia/Hong_Kong"),
    ("新加坡", "Asia/Singapore"),
    ("曼谷", "Asia/Bangkok"),
    ("雪梨", "Australia/Sydney"),
    ("杜拜", "Asia/Dubai"),
    ("倫敦", "Europe/London"),
    ("巴黎", "Europe/Paris"),
    ("柏林", "Europe/Berlin"),
    ("紐約", "America/New_York"),
    ("芝加哥", "America/Chicago"),
    ("洛杉磯", "America/Los_Angeles"),
    ("溫哥華", "America/Vancouver"),
    ("夏威夷", "Pacific/Honolulu"),
    ("UTC", "UTC"),
]
WORLD_CLOCK_TZ_LABELS = {tz: label for label, tz in WORLD_CLOCK_PRESETS}

TW_CITY_PRESETS = [
    ("台北", 25.0330, 121.5654),
    ("新北", 25.0120, 121.4657),
    ("桃園", 24.9936, 121.3010),
    ("新竹", 24.8138, 120.9675),
    ("台中", 24.1477, 120.6736),
    ("台南", 22.9999, 120.2269),
    ("高雄", 22.6273, 120.3014),
]

WEEKDAY_ZH_SHORT = ["一", "二", "三", "四", "五", "六", "日"]


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


def draw_rounded_rect(ctx, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def wmo_to_info(code, is_day=True):
    """
    Map WMO weather interpretation code (0..99) to:
      (zh_description, gtk_icon_name)
    Uses standard freedesktop/MATE icon theme weather names verified on desktop.
    """
    try:
        c = int(code)
    except (TypeError, ValueError):
        return ("未知", "weather-few-clouds")

    if c == 0:
        return ("晴朗", "weather-clear" if is_day else "weather-clear-night")
    if c in (1, 2):
        return ("晴時多雲" if c == 1 else "多雲", "weather-few-clouds" if is_day else "weather-few-clouds-night")
    if c == 3:
        return ("陰天", "weather-overcast")
    if c in (45, 48):
        return ("有霧" if c == 45 else "濃霧", "weather-fog")
    if c in (51, 53, 55, 56, 57):
        return ("毛毛雨", "weather-showers-scattered")
    if c in (61, 80):
        return ("局部陣雨", "weather-showers-scattered")
    if c in (63, 65, 66, 67, 81, 82):
        return ("陣雨" if c in (63, 81) else "大雨", "weather-showers")
    if c in (71, 73, 75, 77, 85, 86):
        return ("降雪", "weather-snow")
    if c in (95, 96, 99):
        return ("雷雨" if c == 95 else "強雷陣雨", "weather-storm")
    return ("多雲", "weather-few-clouds" if is_day else "weather-few-clouds-night")


def aqi_to_label_color(aqi):
    if aqi is None:
        return ("--", "#94a3b8")
    v = int(round(aqi))
    if v <= 50:
        return (f"良好 ({v})", "#34d399")
    if v <= 100:
        return (f"普通 ({v})", "#fbbf24")
    if v <= 150:
        return (f"對敏感族群不健康 ({v})", "#fb923c")
    return (f"不健康 ({v})", "#f87171")


# =============================================================================
# 1. Open-Meteo Free Weather & Air Quality Client (Zero Dependencies)
# =============================================================================

class OpenMeteoClient:
    """Fetches 7-day daily + hourly forecast and current US AQI / PM2.5 from Open-Meteo (no API key)."""

    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    AQI_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
    GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"

    @classmethod
    def _get_json(cls, url, timeout=8.0):
        req = urllib.request.Request(url, headers={"User-Agent": "MatePanel-ClockWeather/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    @classmethod
    def search_cities_sync(cls, query, timeout=6.0):
        q = (query or "").strip()
        if not q:
            return [], "請輸入城市名稱"
        params = urllib.parse.urlencode({"name": q, "count": 8, "language": "zh", "format": "json"})
        try:
            data = cls._get_json(f"{cls.GEO_URL}?{params}", timeout=timeout)
            results = []
            for it in data.get("results") or []:
                name = str(it.get("name", ""))
                admin1 = str(it.get("admin1", "") or "")
                country = str(it.get("country", "") or "")
                lat = float(it.get("latitude", 0.0))
                lon = float(it.get("longitude", 0.0))
                label = name
                extra = [x for x in (admin1, country) if x and x != name]
                if extra:
                    label = f"{name} ({', '.join(extra)})"
                results.append({"name": name, "label": label, "lat": lat, "lon": lon})
            return results, None
        except Exception as exc:
            return [], f"搜尋城市失敗：{exc}"

    @classmethod
    def fetch_weather_sync(cls, lat, lon, city_name="台北", timeout=10.0):
        """
        Fetch current conditions, 7-day daily forecast, 7-day hourly forecast, and current AQI/PM2.5.
        Returns (structured_dict, error_str_or_None)
        """
        f_params = urllib.parse.urlencode(
            {
                "latitude": f"{float(lat):.4f}",
                "longitude": f"{float(lon):.4f}",
                "timezone": "auto",
                "forecast_days": 7,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,uv_index_max,wind_speed_10m_max,sunrise,sunset",
                "hourly": "temperature_2m,precipitation_probability,weather_code,relative_humidity_2m",
            }
        )
        try:
            fdata = cls._get_json(f"{cls.FORECAST_URL}?{f_params}", timeout=timeout)
        except Exception as exc:
            return None, f"天氣資料抓取失敗：{exc}"

        aqi_val = None
        pm25_val = None
        try:
            a_params = urllib.parse.urlencode(
                {
                    "latitude": f"{float(lat):.4f}",
                    "longitude": f"{float(lon):.4f}",
                    "timezone": "auto",
                    "current": "us_aqi,pm2_5",
                }
            )
            adata = cls._get_json(f"{cls.AQI_URL}?{a_params}", timeout=6.0)
            acur = adata.get("current") or {}
            aqi_val = acur.get("us_aqi")
            pm25_val = acur.get("pm2_5")
        except Exception:
            pass

        cur = fdata.get("current") or {}
        daily = fdata.get("daily") or {}
        hourly = fdata.get("hourly") or {}

        # Index hourly data by date 'YYYY-MM-DD'
        hourly_by_date = {}
        h_times = hourly.get("time") or []
        h_temps = hourly.get("temperature_2m") or []
        h_probs = hourly.get("precipitation_probability") or []
        h_codes = hourly.get("weather_code") or []
        h_hums = hourly.get("relative_humidity_2m") or []
        for idx, ts in enumerate(h_times):
            if len(ts) >= 16:
                d_key = ts[:10]
                hh = ts[11:16]
                hr_int = int(ts[11:13])
                hourly_by_date.setdefault(d_key, []).append(
                    {
                        "time": hh,
                        "hour": hr_int,
                        "temp": h_temps[idx] if idx < len(h_temps) else None,
                        "precip_prob": h_probs[idx] if idx < len(h_probs) else None,
                        "code": h_codes[idx] if idx < len(h_codes) else 0,
                        "humidity": h_hums[idx] if idx < len(h_hums) else None,
                    }
                )

        # Index daily data by date 'YYYY-MM-DD'
        daily_by_date = {}
        d_times = daily.get("time") or []
        d_codes = daily.get("weather_code") or []
        d_tmax = daily.get("temperature_2m_max") or []
        d_tmin = daily.get("temperature_2m_min") or []
        d_pprob = daily.get("precipitation_probability_max") or []
        d_psum = daily.get("precipitation_sum") or []
        d_uv = daily.get("uv_index_max") or []
        d_wind = daily.get("wind_speed_10m_max") or []
        d_rise = daily.get("sunrise") or []
        d_set = daily.get("sunset") or []
        for idx, d_key in enumerate(d_times):
            sr = str(d_rise[idx])[11:16] if idx < len(d_rise) and len(str(d_rise[idx])) >= 16 else "--:--"
            ss = str(d_set[idx])[11:16] if idx < len(d_set) and len(str(d_set[idx])) >= 16 else "--:--"
            daily_by_date[d_key] = {
                "date": d_key,
                "code": d_codes[idx] if idx < len(d_codes) else 0,
                "t_max": d_tmax[idx] if idx < len(d_tmax) else None,
                "t_min": d_tmin[idx] if idx < len(d_tmin) else None,
                "precip_prob": d_pprob[idx] if idx < len(d_pprob) else None,
                "precip_sum": d_psum[idx] if idx < len(d_psum) else None,
                "uv_max": d_uv[idx] if idx < len(d_uv) else None,
                "wind_max": d_wind[idx] if idx < len(d_wind) else None,
                "sunrise": sr,
                "sunset": ss,
                "hourly": hourly_by_date.get(d_key, []),
            }

        result = {
            "city": city_name,
            "latitude": float(lat),
            "longitude": float(lon),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "current": {
                "temp": cur.get("temperature_2m"),
                "apparent": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"),
                "wind": cur.get("wind_speed_10m"),
                "code": cur.get("weather_code", 0),
                "is_day": bool(cur.get("is_day", 1)),
                "us_aqi": aqi_val,
                "pm2_5": pm25_val,
            },
            "daily_by_date": daily_by_date,
        }
        return result, None


# =============================================================================
# 2. Interactive Weather Calendar & Detailed Forecast Dashboard Window
# =============================================================================

class ClockWeatherPopupWindow(Gtk.Window):
    """
    Two-pane Weather Calendar & Detailed Daily Forecast Dashboard:
      - Left: 7x6 interactive Cairo calendar embedding desktop-theme weather icons,
        high/low temperatures, and rain probability for today + next 6 days.
      - Right: Instant (0ms) detailed forecast for the selected date (current live stats +
        PM2.5/AQI for today, 3-hourly temperature & rain probability timeline for all 7 days,
        plus optional World Clock bar).
    """

    WEEKDAYS_ZH = ["一", "二", "三", "四", "五", "六", "日"]

    def __init__(self, parent_widget):
        super().__init__(title="📅 月曆與天氣預報")
        self.parent_widget = parent_widget
        self.set_default_size(830, 480)
        self.set_skip_taskbar_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.connect("delete-event", self._on_delete_hide)
        self.connect("key-press-event", self._on_key_press)

        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.selected_date = today
        self.cell_dates = []
        self._cal_grid_rect = (0, 0, 1, 1)

        self._build_ui()

    def _on_delete_hide(self, *_args):
        self.hide()
        return True

    def _on_key_press(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(12)
        root.set_margin_end(12)

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

        btn_close = Gtk.Button(label="✕ 關閉")
        btn_close.set_tooltip_text("關閉天氣月曆視窗 (Esc)")
        btn_close.connect("clicked", lambda *_: self.hide())

        nav.pack_start(btn_prev, False, False, 0)
        nav.pack_start(btn_today, False, False, 0)
        nav.pack_start(btn_next, False, False, 0)
        nav.pack_start(self.month_lbl, True, True, 0)
        nav.pack_end(btn_close, False, False, 0)
        root.pack_start(nav, False, False, 0)

        # Main two-column split: Left = 7x6 Weather Calendar, Right = Selected Day Detailed Forecast
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        # Left Calendar Canvas
        self.cal_canvas = Gtk.DrawingArea()
        self.cal_canvas.set_size_request(500, 390)
        self.cal_canvas.set_hexpand(True)
        self.cal_canvas.set_vexpand(True)
        self.cal_canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.cal_canvas.connect("draw", self._on_draw_calendar)
        self.cal_canvas.connect("button-press-event", self._on_calendar_click)
        content.pack_start(self.cal_canvas, True, True, 0)

        # Right Detailed Forecast Pane
        right_frame = Gtk.Frame()
        right_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        right_scroll.set_size_request(300, -1)

        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.detail_box.set_margin_top(10)
        self.detail_box.set_margin_bottom(10)
        self.detail_box.set_margin_start(12)
        self.detail_box.set_margin_end(12)

        right_scroll.add(self.detail_box)
        right_frame.add(right_scroll)
        content.pack_start(right_frame, False, False, 0)

        root.pack_start(content, True, True, 0)

        # Bottom World Clock & Status Footer
        footer_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.footer_lbl = Gtk.Label(xalign=0.0)
        self.footer_lbl.set_line_wrap(True)
        self.footer_lbl.set_max_width_chars(86)
        footer_row.pack_start(self.footer_lbl, True, True, 0)

        self.btn_world_cities = Gtk.Button(label="🌍 選擇城市")
        self.btn_world_cities.set_tooltip_text("自行勾選底部顯示的世界時鐘城市")
        self.btn_world_cities.connect("clicked", self._open_world_clock_popover)
        footer_row.pack_end(self.btn_world_cities, False, False, 0)
        root.pack_start(footer_row, False, False, 0)

        self.add(root)
        self.show_all()

    def _open_world_clock_popover(self, btn):
        pop = Gtk.Popover.new(btn)
        pop.set_position(Gtk.PositionType.TOP)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        hdr = Gtk.Label(xalign=0.0)
        hdr.set_markup("<b>🌍 選擇世界時鐘城市</b>")
        box.pack_start(hdr, False, False, 0)

        raw = self.parent_widget.config.get("world_clocks", "")
        cur_list = [s.strip() for s in raw.split(",") if s.strip()]
        cur_set = set(cur_list)
        preset_tzs = {tz for _, tz in WORLD_CLOCK_PRESETS}
        custom_tzs = [tz for tz in cur_list if tz not in preset_tzs]

        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        chk_items = []
        for idx, (label, tz_id) in enumerate(WORLD_CLOCK_PRESETS):
            cb = Gtk.CheckButton(label=label)
            cb.set_active(tz_id in cur_set)
            grid.attach(cb, idx % 4, idx // 4, 1, 1)
            chk_items.append((cb, tz_id))
        box.pack_start(grid, False, False, 0)

        custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        custom_row.pack_start(Gtk.Label(label="其他時區：", xalign=0.0), False, False, 0)
        entry_custom = Gtk.Entry()
        entry_custom.set_hexpand(True)
        entry_custom.set_placeholder_text("如 Asia/Kolkata, Pacific/Auckland")
        entry_custom.set_text(", ".join(custom_tzs))
        custom_row.pack_start(entry_custom, True, True, 0)
        box.pack_start(custom_row, False, False, 0)

        def _sync_world(*_):
            selected = [tz for cb, tz in chk_items if cb.get_active()]
            extras = [s.strip() for s in entry_custom.get_text().split(",") if s.strip()]
            for ex in extras:
                if ex not in selected:
                    selected.append(ex)
            self.parent_widget.config["show_world_clock"] = True
            self.parent_widget.config["world_clocks"] = ", ".join(selected)
            self.parent_widget._save_config()
            self._update_world_clock_footer()

        for cb, _tz in chk_items:
            cb.connect("toggled", _sync_world)
        entry_custom.connect("activate", _sync_world)
        entry_custom.connect("focus-out-event", lambda *_: (_sync_world(), False)[1])

        pop.add(box)
        pop.show_all()
        pop.popup()

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
        self.refresh_view()

    def _jump_today(self):
        today = date.today()
        self.view_year = today.year
        self.view_month = today.month
        self.selected_date = today
        self.refresh_view()

    def refresh_view(self):
        self._compute_month_cells()
        wdata = self.parent_widget.weather_data or {}
        city = wdata.get("city") or self.parent_widget.config.get("city_name", "台北")
        upd = wdata.get("updated_at", "尚未更新")
        fs = max(8, min(20, int(self.parent_widget.config.get("calendar_font_size", 11))))
        t_pt = int((fs + 2.5) * 1024)
        s_pt = int(max(8.5, fs - 1.0) * 1024)

        self.month_lbl.set_markup(
            f"<span size='{t_pt}'><b>{self.view_year} 年 {self.view_month:02d} 月</b></span>   "
            f"<span size='{s_pt}' foreground='#94a3b8'>📍 {html.escape(city)} · 更新：{html.escape(upd)}</span>"
        )
        self.cal_canvas.queue_draw()
        self._populate_detail_pane()
        self._update_world_clock_footer()

    def _update_world_clock_footer(self):
        if not self.parent_widget.config.get("show_world_clock", True):
            self.footer_lbl.set_markup(
                "<small><span foreground='#94a3b8'>點選月曆中今日起一週內的日期可查看詳細逐時預報 | Esc 關閉</span></small>"
            )
            return
        raw_tzs = self.parent_widget.config.get("world_clocks", "")
        parts = []
        now_utc = datetime.now(ZoneInfo("UTC"))
        for tz_str in [s.strip() for s in raw_tzs.split(",") if s.strip()]:
            try:
                dt = now_utc.astimezone(ZoneInfo(tz_str))
                short_name = WORLD_CLOCK_TZ_LABELS.get(tz_str) or tz_str.split("/")[-1].replace("_", " ")
                parts.append(f"<b>{html.escape(short_name)}</b> {dt.strftime('%m/%d %H:%M')}")
            except Exception:
                continue
        if parts:
            self.footer_lbl.set_markup(
                f"<small>🌍 {'   |   '.join(parts)}</small>"
            )
        else:
            self.footer_lbl.set_markup(
                "<small><span foreground='#94a3b8'>點選月曆中今日起一週內的日期可查看詳細逐時預報 | Esc 關閉</span></small>"
            )

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
                self.refresh_view()
                return True
        return False

    def _on_draw_calendar(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)

        base_fs = float(max(8, min(20, int(self.parent_widget.config.get("calendar_font_size", 11)))))
        draw_rounded_rect(ctx, 0, 0, w, h, 8.0)
        ctx.set_source_rgba(0.10, 0.13, 0.17, 0.96)
        ctx.fill()

        header_h = max(24.0, base_fs * 2.3)
        grid_x = 4.0
        grid_y = header_h + 2.0
        col_w = (w - 8.0) / 7.0
        row_h = max(30.0, (h - grid_y - 4.0) / 6.0)
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
        daily_map = (self.parent_widget.weather_data or {}).get("daily_by_date") or {}
        icon_sz = max(14, min(22, int(min(col_w * 0.34, row_h * 0.36))))

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
            day_fc = daily_map.get(d_str)

            draw_rounded_rect(ctx, cx, cy, cw, ch, 5.0)
            if is_sel:
                ctx.set_source_rgba(0.16, 0.34, 0.54, 0.96)
            elif is_today:
                ctx.set_source_rgba(0.12, 0.28, 0.26, 0.95)
            elif day_fc is not None:
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
            elif day_fc is not None:
                ctx.set_source_rgba(0.30, 0.45, 0.62, 0.70)
                ctx.set_line_width(1.0)
            else:
                ctx.set_source_rgba(0.22, 0.26, 0.32, 0.60)
                ctx.set_line_width(0.8)
            ctx.stroke()

            # Day number (top-left)
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

            # Weather forecast overlay for today + next 6 days!
            if day_fc is not None:
                _desc, icon_name = wmo_to_info(day_fc.get("code", 0), is_day=True)
                pb = self.parent_widget.get_weather_pixbuf(icon_name, icon_sz)
                if pb is not None:
                    Gdk.cairo_set_source_pixbuf(ctx, pb, cx + cw - icon_sz - 4.0, cy + 3.0)
                    ctx.paint()

                tmax = day_fc.get("t_max")
                tmin = day_fc.get("t_min")
                pprob = day_fc.get("precip_prob")

                if tmax is not None and tmin is not None:
                    t_str = f"{int(round(tmax))}°/{int(round(tmin))}°"
                    t_fs = max(7.5, min(base_fs - 1.2, cw * 0.175))
                    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                    ctx.set_font_size(t_fs)
                    ctx.set_source_rgba(0.96, 0.98, 1.00, 0.95)
                    t_ext = ctx.text_extents(t_str)
                    ty = cy + ch - (t_fs + 5.0 if (pprob is not None and pprob >= 30 and ch >= 50) else 5.0)
                    ctx.move_to(cx + (cw - t_ext.width) / 2.0 - t_ext.x_bearing, ty)
                    ctx.show_text(t_str)

                if pprob is not None and pprob >= 30 and ch >= 50:
                    p_str = f"💧{int(round(pprob))}%"
                    p_fs = max(7.0, min(base_fs - 2.0, cw * 0.16))
                    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
                    ctx.set_font_size(p_fs)
                    ctx.set_source_rgba(0.45, 0.82, 1.00, 0.95)
                    p_ext = ctx.text_extents(p_str)
                    ctx.move_to(cx + (cw - p_ext.width) / 2.0 - p_ext.x_bearing, cy + ch - 3.5)
                    ctx.show_text(p_str)
        return False

    def _populate_detail_pane(self):
        for child in list(self.detail_box.get_children()):
            self.detail_box.remove(child)

        fs = max(8, min(20, int(self.parent_widget.config.get("calendar_font_size", 11))))
        p_lg = int((fs + 2.5) * 1024)
        p_md = int(fs * 1024)
        p_sm = int(max(8.5, fs - 1.0) * 1024)

        d_obj = self.selected_date
        d_str = d_obj.strftime("%Y-%m-%d")
        wd_zh = self.WEEKDAYS_ZH[d_obj.weekday()]
        is_today = d_obj == date.today()
        wdata = self.parent_widget.weather_data or {}
        daily_map = wdata.get("daily_by_date") or {}
        day_fc = daily_map.get(d_str)
        cur = wdata.get("current") or {}

        # Date Title
        hdr = Gtk.Label(xalign=0.0)
        tag_today = " <span foreground='#34d399'>(今天)</span>" if is_today else ""
        hdr.set_markup(
            f"<span size='{p_lg}'><b>{d_obj.month}/{d_obj.day} 週{wd_zh}</b></span>{tag_today}"
        )
        self.detail_box.pack_start(hdr, False, False, 0)

        if day_fc is None:
            no_lbl = Gtk.Label(xalign=0.0)
            no_lbl.set_line_wrap(True)
            no_lbl.set_markup(
                f"<span size='{p_md}' foreground='#94a3b8'>\n"
                f"此日期超出 7 天天氣預報範圍。\n\n"
                f"💡 點選左側月曆中「今天起往後一週內」帶有天氣圖示的日期，即可檢視詳細預測與每 3 小時降雨機率。</span>"
            )
            self.detail_box.pack_start(no_lbl, False, False, 0)
            self.detail_box.show_all()
            return

        # Hero weather row (Desktop Theme Icon + Summary Temp)
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        if is_today and cur.get("temp") is not None:
            desc, icon_name = wmo_to_info(cur.get("code", day_fc.get("code", 0)), is_day=cur.get("is_day", True))
            pb = self.parent_widget.get_weather_pixbuf(icon_name, 48)
            if pb is not None:
                hero.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            t_now = cur.get("temp")
            t_app = cur.get("apparent")
            hero_lbl = Gtk.Label(xalign=0.0)
            hero_lbl.set_markup(
                f"<span size='{int((fs + 7) * 1024)}'><b>{t_now:.1f}°C</b></span>  "
                f"<span size='{p_md}'>{html.escape(desc)}</span>\n"
                f"<span size='{p_sm}' foreground='#94a3b8'>體感 {t_app:.1f}°C · 濕度 {cur.get('humidity', '--')}% · 風速 {cur.get('wind', '--')} km/h</span>"
                if t_app is not None
                else f"<span size='{int((fs + 7) * 1024)}'><b>{t_now:.1f}°C</b></span>  {html.escape(desc)}"
            )
            hero.pack_start(hero_lbl, True, True, 0)
        else:
            desc, icon_name = wmo_to_info(day_fc.get("code", 0), is_day=True)
            pb = self.parent_widget.get_weather_pixbuf(icon_name, 48)
            if pb is not None:
                hero.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            tmax = day_fc.get("t_max")
            tmin = day_fc.get("t_min")
            t_range = f"{tmax:.0f}° / {tmin:.0f}°C" if (tmax is not None and tmin is not None) else "--"
            hero_lbl = Gtk.Label(xalign=0.0)
            hero_lbl.set_markup(
                f"<span size='{int((fs + 5) * 1024)}'><b>{t_range}</b></span>\n"
                f"<span size='{p_md}'>{html.escape(desc)}</span>"
            )
            hero.pack_start(hero_lbl, True, True, 0)
        self.detail_box.pack_start(hero, False, False, 0)

        # Daily Metrics Grid
        pprob = day_fc.get("precip_prob")
        psum = day_fc.get("precip_sum")
        uv_max = day_fc.get("uv_max")
        w_max = day_fc.get("wind_max")
        sr = day_fc.get("sunrise", "--:--")
        ss = day_fc.get("sunset", "--:--")
        tmax = day_fc.get("t_max")
        tmin = day_fc.get("t_min")

        metric_lines = []
        if tmax is not None and tmin is not None:
            metric_lines.append(f"🌡️ 高低溫：<b>{tmax:.1f}°C / {tmin:.1f}°C</b>")
        metric_lines.append(
            f"💧 降雨機率：<b>{int(round(pprob)) if pprob is not None else '--'}%</b>"
            + (f"  (雨量 {psum:.1f} mm)" if psum is not None else "")
        )
        metric_lines.append(
            f"☀️ 紫外線 UV：<b>{uv_max:.1f}</b>   💨 最大風速：<b>{w_max:.0f} km/h</b>"
            if (uv_max is not None and w_max is not None)
            else f"☀️ 日出 {sr}  ·  🌇 日落 {ss}"
        )
        metric_lines.append(f"🌅 日出 <b>{sr}</b>   ·   🌇 日落 <b>{ss}</b>")

        if is_today and (cur.get("us_aqi") is not None or cur.get("pm2_5") is not None):
            aqi_lbl, aqi_col = aqi_to_label_color(cur.get("us_aqi"))
            pm25 = cur.get("pm2_5")
            pm_s = f" · PM2.5: <b>{pm25:.1f}</b> μg/m³" if pm25 is not None else ""
            metric_lines.append(
                f"🍃 空氣品質：<span foreground='{aqi_col}'><b>{html.escape(aqi_lbl)}</b></span>{pm_s}"
            )

        m_lbl = Gtk.Label(xalign=0.0)
        m_lbl.set_line_wrap(True)
        m_lbl.set_markup(f"<span size='{p_sm}'>" + "\n".join(metric_lines) + "</span>")
        self.detail_box.pack_start(m_lbl, False, False, 0)

        self.detail_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # 3-Hourly Timeline for selected day (00, 03, 06, 09, 12, 15, 18, 21)
        hr_title = Gtk.Label(xalign=0.0)
        hr_title.set_markup(f"<span size='{p_md}'><b>⏱ 逐時預報（每 3 小時）</b></span>")
        self.detail_box.pack_start(hr_title, False, False, 0)

        hourly_list = day_fc.get("hourly") or []
        slots = [h_it for h_it in hourly_list if h_it.get("hour", 0) % 3 == 0]
        if slots:
            hr_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
            for row_i, h_it in enumerate(slots):
                hr_val = h_it.get("hour", 0)
                is_d = 6 <= hr_val < 18
                _hdesc, h_icon = wmo_to_info(h_it.get("code", 0), is_day=is_d)
                pb_s = self.parent_widget.get_weather_pixbuf(h_icon, 16)

                lbl_t = Gtk.Label(xalign=0.0)
                lbl_t.set_markup(f"<span size='{p_sm}'><tt>{h_it['time']}</tt></span>")
                hr_grid.attach(lbl_t, 0, row_i, 1, 1)

                if pb_s is not None:
                    hr_grid.attach(Gtk.Image.new_from_pixbuf(pb_s), 1, row_i, 1, 1)

                ht = h_it.get("temp")
                lbl_temp = Gtk.Label(xalign=1.0)
                lbl_temp.set_markup(
                    f"<span size='{p_sm}'><b>{ht:.0f}°C</b></span>" if ht is not None else "--"
                )
                hr_grid.attach(lbl_temp, 2, row_i, 1, 1)

                hp = h_it.get("precip_prob")
                hp_int = int(round(hp)) if hp is not None else 0
                bar_cells = max(0, min(6, int(round(hp_int / 100.0 * 6))))
                bar_txt = ("█" * bar_cells) + ("░" * (6 - bar_cells))
                p_col = "#38bdf8" if hp_int >= 30 else "#64748b"
                lbl_prob = Gtk.Label(xalign=0.0)
                lbl_prob.set_markup(
                    f"<span size='{p_sm}' foreground='{p_col}'><tt>{bar_txt}</tt> {hp_int:2d}%</span>"
                )
                hr_grid.attach(lbl_prob, 3, row_i, 1, 1)
            self.detail_box.pack_start(hr_grid, False, False, 0)

        self.detail_box.show_all()

    def position_and_show(self):
        self.refresh_view()
        display = Gdk.Display.get_default()
        gdk_win = self.parent_widget.get_window()
        if display is not None and gdk_win is not None:
            _, ox, oy = gdk_win.get_origin()
            alloc = self.parent_widget.get_allocation()
            cx = int(ox + max(1, alloc.width) / 2)
            cy = int(oy + max(1, alloc.height) / 2)
            mon = display.get_monitor_at_point(cx, cy) or display.get_monitor_at_window(gdk_win)
            if mon is not None:
                geo = mon.get_geometry()
                wa = mon.get_workarea()
                mon_l = max(geo.x, wa.x)
                mon_r = min(geo.x + geo.width, wa.x + wa.width)
                mon_t = max(geo.y, wa.y)
                mon_b = min(geo.y + geo.height, wa.y + wa.height)
                win_w, win_h = self.get_size()
                win_w = max(win_w, 830)
                win_h = max(win_h, 480)
                px = max(mon_l + 8, min(ox + alloc.width - win_w, mon_r - win_w - 8))
                if oy + alloc.height + win_h + 10 < mon_b:
                    py = oy + alloc.height + 6
                else:
                    py = max(mon_t + 8, oy - win_h - 6)
                self.move(int(px), int(py))
        else:
            self.set_position(Gtk.WindowPosition.CENTER)
        self.show_all()
        self.present()


# =============================================================================
# 3. Main MATE Panel Widget (Desktop-Theme Weather Icon + HH:MM Clock)
# =============================================================================

class ClockWeatherWidget(Gtk.EventBox):
    """MATE Panel widget displaying desktop-theme weather icon, temperature, date & HH:MM clock."""

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
        self.weather_data = self._load_weather_cache()
        self.panel_size = 28
        self.popup_win = None
        self.prefs_win = None
        self._minute_timer_id = None
        self._weather_timer_id = None
        self._icon_cache = {}
        self._fetching_weather = False

        self.icon_theme = Gtk.IconTheme.get_default()
        if self.icon_theme is not None:
            self.icon_theme.connect("changed", self._on_icon_theme_changed)

        self.canvas = Gtk.DrawingArea()
        self.canvas.connect("draw", self._on_draw)
        self.add(self.canvas)

        self.connect("button-press-event", self._on_button_press)
        self.connect("destroy", self._on_destroy)

        self._update_canvas_size()
        self._update_tooltip()
        self._schedule_next_minute_tick()
        self._sync_weather_timer()
        # Fetch fresh weather asynchronously shortly after startup
        GLib.timeout_add(1200, lambda: (self.fetch_weather_async(force=False), False)[-1])
        self.show_all()

    def _on_icon_theme_changed(self, *_args):
        self._icon_cache.clear()
        self.canvas.queue_draw()
        if self.popup_win is not None and self.popup_win.is_visible():
            self.popup_win.refresh_view()

    def get_weather_pixbuf(self, icon_name, size):
        """Load & cache weather icon from the current desktop GTK Icon Theme."""
        sz = max(12, min(64, int(size)))
        key = (icon_name, sz)
        if key in self._icon_cache:
            return self._icon_cache[key]
        if self.icon_theme is None:
            return None
        pb = None
        for cand in (icon_name, "weather-few-clouds", "weather-clear"):
            try:
                if self.icon_theme.has_icon(cand):
                    pb = self.icon_theme.load_icon(cand, sz, Gtk.IconLookupFlags.FORCE_SIZE)
                    break
            except Exception:
                continue
        self._icon_cache[key] = pb
        return pb

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

    def _save_weather_cache(self, data):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(WEATHER_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # =========================================================================
    # Exact Minute-Boundary Clock Timer & Periodic Weather Refresh
    # =========================================================================

    def _schedule_next_minute_tick(self):
        if self._minute_timer_id is not None:
            try:
                GLib.source_remove(self._minute_timer_id)
            except Exception:
                pass
            self._minute_timer_id = None
        now = datetime.now()
        ms_to_next_min = max(200, (60 - now.second) * 1000 - (now.microsecond // 1000) + 60)
        self._minute_timer_id = GLib.timeout_add(ms_to_next_min, self._on_minute_tick)

    def _on_minute_tick(self):
        self._minute_timer_id = None
        self._update_canvas_size()
        self._update_tooltip()
        if self.popup_win is not None and self.popup_win.is_visible():
            self.popup_win.refresh_view()
        self._schedule_next_minute_tick()
        return False

    def _sync_weather_timer(self):
        if self._weather_timer_id is not None:
            try:
                GLib.source_remove(self._weather_timer_id)
            except Exception:
                pass
            self._weather_timer_id = None
        mins = max(10, min(180, int(self.config.get("weather_interval_min", 20))))
        self._weather_timer_id = GLib.timeout_add_seconds(
            mins * 60, lambda: (self.fetch_weather_async(force=True), True)[-1]
        )

    def fetch_weather_async(self, force=False, on_done_cb=None):
        if self._fetching_weather:
            return
        lat = float(self.config.get("latitude", 25.0330))
        lon = float(self.config.get("longitude", 121.5654))
        city = self.config.get("city_name", "台北")

        # Skip immediate network fetch if cache is fresh (<10 min) and matches current coordinates
        if not force and self.weather_data:
            try:
                if (
                    abs(float(self.weather_data.get("latitude", 0)) - lat) < 0.01
                    and abs(float(self.weather_data.get("longitude", 0)) - lon) < 0.01
                ):
                    upd_dt = datetime.strptime(self.weather_data.get("updated_at", ""), "%Y-%m-%d %H:%M")
                    if (datetime.now() - upd_dt).total_seconds() < 600:
                        if on_done_cb:
                            on_done_cb(None)
                        return
            except Exception:
                pass

        self._fetching_weather = True

        def _worker():
            data, err = OpenMeteoClient.fetch_weather_sync(lat, lon, city_name=city)

            def _apply():
                self._fetching_weather = False
                if data is not None and err is None:
                    self.weather_data = data
                    self._save_weather_cache(data)
                    self._update_canvas_size()
                    self._update_tooltip()
                    if self.popup_win is not None and self.popup_win.is_visible():
                        self.popup_win.refresh_view()
                if on_done_cb:
                    on_done_cb(err)
                return False

            GLib.idle_add(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    # =========================================================================
    # Panel Layout, Drawing & Events
    # =========================================================================

    def set_panel_size(self, size):
        self.panel_size = max(22, min(64, int(size)))
        self._update_canvas_size()

    def _format_panel_text(self):
        now = datetime.now()
        parts = []
        if self.config.get("show_weather_temp", True):
            cur = (self.weather_data or {}).get("current") or {}
            t = cur.get("temp")
            parts.append(f"{int(round(t))}°" if t is not None else "--°")
        if self.config.get("show_date", True):
            d_s = f"{now.month:02d}/{now.day:02d}"
            if self.config.get("show_weekday", True):
                d_s += f"({WEEKDAY_ZH_SHORT[now.weekday()]})"
            parts.append(d_s)
        elif self.config.get("show_weekday", True):
            parts.append(f"週{WEEKDAY_ZH_SHORT[now.weekday()]}")

        if self.config.get("use_24h", True):
            t_s = now.strftime("%H:%M")
        else:
            t_s = now.strftime("%I:%M %p").lstrip("0")
        parts.append(t_s)
        return "  ".join(parts)

    def _compute_panel_font_size(self, ctx, txt, h):
        """Maximize bold panel text height while guaranteeing zero vertical clipping."""
        max_h = max(12.0, float(h) - 2.0)
        fs = max(13.0, min(40.0, float(h) * 0.72))
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(fs)
        ext = ctx.text_extents(txt)
        if ext.height > max_h and ext.height > 1.0:
            fs *= max_h / ext.height
            ctx.set_font_size(fs)
            ext = ctx.text_extents(txt)
        return fs, ext

    def _update_canvas_size(self):
        h = max(24, self.panel_size)
        txt = self._format_panel_text()
        show_icon = bool(self.config.get("show_weather_icon", True))
        icon_sz = max(16, min(36, int(h - 2)))

        # Measure exact Cairo text width using a tiny image surface
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        ctx = cairo.Context(surf)
        _fs, ext = self._compute_panel_font_size(ctx, txt, h)
        text_w = max(ext.x_advance, ext.width)

        pad_x = 4
        icon_w = (icon_sz + 4) if show_icon else 0
        total_w = int(math.ceil(pad_x * 2 + icon_w + text_w + 2))
        self.canvas.set_size_request(max(56, total_w), h)
        self.canvas.queue_draw()

    def _update_tooltip(self):
        now = datetime.now()
        city = self.config.get("city_name", "台北")
        cur = (self.weather_data or {}).get("current") or {}
        daily_map = (self.weather_data or {}).get("daily_by_date") or {}
        today_fc = daily_map.get(now.strftime("%Y-%m-%d")) or {}
        upd = (self.weather_data or {}).get("updated_at", "更新中...")

        t = cur.get("temp")
        app = cur.get("apparent")
        hum = cur.get("humidity")
        desc, _ = wmo_to_info(cur.get("code", 0), is_day=cur.get("is_day", True))
        pprob = today_fc.get("precip_prob")
        aqi_s, _ = aqi_to_label_color(cur.get("us_aqi"))

        lines = [
            f"<b>📅 {now.strftime('%Y 年 %m 月 %d 日')} 週{WEEKDAY_ZH_SHORT[now.weekday()]}  {now.strftime('%H:%M')}</b>",
            f"• 地點：<b>{html.escape(city)}</b> ({html.escape(desc)})",
        ]
        if t is not None:
            app_s = f"（體感 {app:.1f}°C）" if app is not None else ""
            lines.append(f"• 即時氣溫：<b>{t:.1f}°C</b>{app_s} · 濕度 {hum if hum is not None else '--'}%")
        if pprob is not None:
            lines.append(f"• 今日最高降雨機率：<b>{int(round(pprob))}%</b> · AQI：{html.escape(aqi_s)}")
        lines.append(f"<small>更新：{html.escape(upd)} | 左鍵：開啟天氣月曆 | 中鍵：更新天氣 | 右鍵：設定</small>")
        self.set_tooltip_markup("\n".join(lines))

    def _draw_styled_text(self, ctx, x, y, text, rgb):
        font_alpha = max(0.10, min(1.0, int(self.config.get("font_opacity", 100)) / 100.0))
        if self.config.get("text_shadow", False):
            lum = rgb_luminance(rgb)
            s_a = 0.85 * font_alpha
            s_rgb = (0.0, 0.0, 0.0, s_a) if lum > 0.45 else (1.0, 1.0, 1.0, s_a)
            ctx.set_source_rgba(*s_rgb)
            for dx, dy in ((-0.9, 0.0), (0.9, 0.0), (0.0, -0.9), (0.0, 0.9)):
                ctx.move_to(x + dx, y + dy)
                ctx.show_text(text)
        ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], font_alpha)
        ctx.move_to(x, y)
        ctx.show_text(text)

    def _on_draw(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)
        bh = max(20.0, h)
        bw = max(40.0, w)

        bg_hex = self.config.get("badge_bg_color", "#182234")
        opacity = max(0, min(100, int(self.config.get("badge_bg_opacity", 90))))
        alpha = opacity / 100.0
        bg_rgb = hex_to_rgb(bg_hex, default=(0.094, 0.133, 0.204))

        if alpha > 0.0:
            draw_rounded_rect(ctx, 0.0, 0.0, bw, bh, 4.0)
            ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], alpha)
            ctx.fill()

        # Text color
        if self.config.get("font_color_mode", "dynamic") == "custom":
            fg_rgb = hex_to_rgb(self.config.get("custom_font_color", "#ffffff"), default=(1.0, 1.0, 1.0))
        else:
            is_light = opacity >= 30 and rgb_luminance(bg_rgb) > 0.55
            fg_rgb = (0.07, 0.09, 0.15) if is_light else (0.95, 0.98, 1.00)

        cur_x = 4.0
        if self.config.get("show_weather_icon", True):
            cur = (self.weather_data or {}).get("current") or {}
            _desc, icon_name = wmo_to_info(cur.get("code", 0), is_day=cur.get("is_day", True))
            icon_sz = max(16, min(36, int(bh - 2)))
            pb = self.get_weather_pixbuf(icon_name, icon_sz)
            if pb is not None:
                iy = (bh - icon_sz) / 2.0
                Gdk.cairo_set_source_pixbuf(ctx, pb, cur_x, iy)
                ctx.paint()
            cur_x += icon_sz + 4.0

        txt = self._format_panel_text()
        _fs, ext = self._compute_panel_font_size(ctx, txt, bh)
        ty = (bh - ext.height) / 2.0 - ext.y_bearing
        self._draw_styled_text(ctx, cur_x - ext.x_bearing, ty, txt, fg_rgb)
        return False

    def handle_applet_button_press(self, widget, event):
        if event.button == 1:
            GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.toggle_popup_window()
            return True
        elif event.button == 2:
            GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.fetch_weather_async(force=True)
            return True
        return False

    def _on_button_press(self, widget, event):
        if event.button == 1:
            if self.applet is not None:
                GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.toggle_popup_window()
            return True
        elif event.button == 2:
            if self.applet is not None:
                GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.fetch_weather_async(force=True)
            return True
        elif event.button == 3 and self.applet is None:
            self.show_preferences_dialog()
            return True
        return False

    def toggle_popup_window(self, *_args):
        if self.popup_win is None:
            self.popup_win = ClockWeatherPopupWindow(self)
        if self.popup_win.is_visible():
            self.popup_win.hide()
        else:
            self.popup_win.position_and_show()

    # =========================================================================
    # Preferences Window
    # =========================================================================

    def show_preferences_dialog(self, *_args):
        if self.prefs_win is not None:
            self.prefs_win.present()
            return

        win = Gtk.Window(title="月曆、時鐘與天氣預報 Plus — 偏好設定")
        win.set_default_size(560, 580)
        win.set_position(Gtk.WindowPosition.CENTER)
        self.prefs_win = win
        win.connect("destroy", lambda *_: setattr(self, "prefs_win", None))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(14)
        outer.set_margin_end(14)

        # 1. Weather City & Refresh
        s1 = Gtk.Label(xalign=0.0)
        s1.set_markup("<b>1. 天氣預報地點與更新設定（Open-Meteo 免 API Key）</b>")
        outer.pack_start(s1, False, False, 0)

        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        preset_row.set_margin_start(10)
        preset_row.pack_start(Gtk.Label(label="快速城市：", xalign=0.0), False, False, 0)
        preset_btns = []
        for cname, clat, clon in TW_CITY_PRESETS:
            b = Gtk.Button(label=cname)
            preset_btns.append((b, cname, clat, clon))
            preset_row.pack_start(b, False, False, 0)
        outer.pack_start(preset_row, False, False, 0)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_row.set_margin_start(10)
        entry_search = Gtk.Entry()
        entry_search.set_hexpand(True)
        entry_search.set_placeholder_text("搜尋全球城市名稱（如 Tokyo, London, Hualien）...")
        btn_search = Gtk.Button(label="🔎 搜尋")
        search_row.pack_start(entry_search, True, True, 0)
        search_row.pack_start(btn_search, False, False, 0)
        outer.pack_start(search_row, False, False, 0)

        combo_city_res = Gtk.ComboBoxText()
        combo_city_res.set_margin_start(10)
        combo_city_res.append("cur", f"目前地點：{self.config.get('city_name', '台北')} ({self.config.get('latitude', 25.033):.4f}, {self.config.get('longitude', 121.5654):.4f})")
        combo_city_res.set_active_id("cur")
        outer.pack_start(combo_city_res, False, False, 0)
        found_cities = {}

        freq_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        freq_row.set_margin_start(10)
        freq_row.pack_start(Gtk.Label(label="自動更新間隔 (分鐘)：", xalign=0.0), False, False, 0)
        spin_interval = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("weather_interval_min", 20)),
                lower=10.0,
                upper=180.0,
                step_increment=5.0,
                page_increment=15.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        btn_now_fetch = Gtk.Button(label="🔄 立即抓取天氣")
        freq_row.pack_start(spin_interval, False, False, 0)
        freq_row.pack_end(btn_now_fetch, False, False, 0)
        outer.pack_start(freq_row, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # 2. Panel Display Content (NO seconds option!)
        s2 = Gtk.Label(xalign=0.0)
        s2.set_markup("<b>2. 面板顯示項目與月曆設定</b>")
        outer.pack_start(s2, False, False, 0)

        chk_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        chk_row1.set_margin_start(10)
        chk_w_icon = Gtk.CheckButton(label="顯示桌面主題天氣圖示")
        chk_w_icon.set_active(self.config.get("show_weather_icon", True))
        chk_w_temp = Gtk.CheckButton(label="顯示即時溫度")
        chk_w_temp.set_active(self.config.get("show_weather_temp", True))
        chk_date = Gtk.CheckButton(label="顯示日期")
        chk_date.set_active(self.config.get("show_date", True))
        chk_wd = Gtk.CheckButton(label="顯示星期")
        chk_wd.set_active(self.config.get("show_weekday", True))
        for c in (chk_w_icon, chk_w_temp, chk_date, chk_wd):
            chk_row1.pack_start(c, False, False, 0)
        outer.pack_start(chk_row1, False, False, 0)

        chk_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        chk_row2.set_margin_start(10)
        chk_24h = Gtk.CheckButton(label="使用 24 小時制 (HH:MM)")
        chk_24h.set_active(self.config.get("use_24h", True))
        chk_row2.pack_start(chk_24h, False, False, 0)
        chk_row2.pack_start(Gtk.Label(label="月曆與預報字型大小 (pt)：", xalign=0.0), False, False, 0)
        spin_cal_fs = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("calendar_font_size", 11)),
                lower=8.0,
                upper=20.0,
                step_increment=1.0,
                page_increment=2.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        chk_row2.pack_start(spin_cal_fs, False, False, 0)
        outer.pack_start(chk_row2, False, False, 0)

        wc_hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        wc_hdr_row.set_margin_start(10)
        chk_world = Gtk.CheckButton(label="在天氣月曆底部顯示世界時鐘（自行勾選城市）：")
        chk_world.set_active(self.config.get("show_world_clock", True))
        wc_hdr_row.pack_start(chk_world, False, False, 0)
        outer.pack_start(wc_hdr_row, False, False, 0)

        raw_wc = self.config.get("world_clocks", "Asia/Tokyo, Europe/London, America/New_York")
        cur_wc_list = [s.strip() for s in raw_wc.split(",") if s.strip()]
        cur_wc_set = set(cur_wc_list)
        preset_tz_set = {tz for _, tz in WORLD_CLOCK_PRESETS}
        custom_wc_list = [tz for tz in cur_wc_list if tz not in preset_tz_set]

        wc_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        wc_grid.set_margin_start(24)
        wc_checkboxes = []
        for idx, (label, tz_id) in enumerate(WORLD_CLOCK_PRESETS):
            cb = Gtk.CheckButton(label=label)
            cb.set_active(tz_id in cur_wc_set)
            wc_grid.attach(cb, idx % 4, idx // 4, 1, 1)
            wc_checkboxes.append((cb, tz_id))
        outer.pack_start(wc_grid, False, False, 0)

        wc_custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        wc_custom_row.set_margin_start(24)
        wc_custom_row.pack_start(Gtk.Label(label="其他自訂時區 (IANA)：", xalign=0.0), False, False, 0)
        entry_world_custom = Gtk.Entry()
        entry_world_custom.set_hexpand(True)
        entry_world_custom.set_placeholder_text("選填，逗號分隔，如 Asia/Kolkata, Pacific/Auckland")
        entry_world_custom.set_text(", ".join(custom_wc_list))
        wc_custom_row.pack_start(entry_world_custom, True, True, 0)
        outer.pack_start(wc_custom_row, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # 3. Panel Appearance (Custom BG / Font Color / Font Opacity / Shadow — Borderless)
        s3 = Gtk.Label(xalign=0.0)
        s3.set_markup("<b>3. 面板背景底色、文字顏色與透明度設定</b>")
        outer.pack_start(s3, False, False, 0)

        def make_cbtn(key, default_hex):
            b = Gtk.ColorButton()
            r = Gdk.RGBA()
            if not r.parse(self.config.get(key, default_hex)):
                r.parse(default_hex)
            b.set_rgba(r)
            return b

        bg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bg_row.set_margin_start(10)
        bg_btn = make_cbtn("badge_bg_color", "#182234")
        scale_op = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 2.0)
        scale_op.set_digits(0)
        scale_op.set_draw_value(False)
        scale_op.set_size_request(140, -1)
        init_op = max(0, min(100, int(self.config.get("badge_bg_opacity", 90))))
        scale_op.set_value(float(init_op))
        lbl_op = Gtk.Label(label=f"{init_op}%")
        lbl_op.set_width_chars(4)
        bg_row.pack_start(Gtk.Label(label="背景底色：", xalign=0.0), False, False, 0)
        bg_row.pack_start(bg_btn, False, False, 0)
        bg_row.pack_start(Gtk.Label(label="背景透明度：", xalign=0.0), False, False, 0)
        bg_row.pack_start(scale_op, True, True, 0)
        bg_row.pack_start(lbl_op, False, False, 0)
        outer.pack_start(bg_row, False, False, 0)

        fc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fc_row.set_margin_start(10)
        rb_fc_dyn = Gtk.RadioButton.new_with_label_from_widget(None, "自動字色")
        rb_fc_cus = Gtk.RadioButton.new_with_label_from_widget(rb_fc_dyn, "自訂字色：")
        if self.config.get("font_color_mode", "dynamic") == "dynamic":
            rb_fc_dyn.set_active(True)
        else:
            rb_fc_cus.set_active(True)
        fc_btn = make_cbtn("custom_font_color", "#ffffff")
        chk_shadow = Gtk.CheckButton(label="文字高對比描邊陰影")
        chk_shadow.set_active(self.config.get("text_shadow", False))
        fc_row.pack_start(rb_fc_dyn, False, False, 0)
        fc_row.pack_start(rb_fc_cus, False, False, 0)
        fc_row.pack_start(fc_btn, False, False, 0)
        fc_row.pack_end(chk_shadow, False, False, 0)
        outer.pack_start(fc_row, False, False, 0)

        fop_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fop_row.set_margin_start(10)
        scale_font_op = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10.0, 100.0, 2.0)
        scale_font_op.set_digits(0)
        scale_font_op.set_draw_value(False)
        scale_font_op.set_size_request(140, -1)
        init_fop = max(10, min(100, int(self.config.get("font_opacity", 100))))
        scale_font_op.set_value(float(init_fop))
        lbl_font_op = Gtk.Label(label=f"{init_fop}%")
        lbl_font_op.set_width_chars(4)
        fop_row.pack_start(Gtk.Label(label="文字透明度：", xalign=0.0), False, False, 0)
        fop_row.pack_start(scale_font_op, True, True, 0)
        fop_row.pack_start(lbl_font_op, False, False, 0)
        outer.pack_start(fop_row, False, False, 0)

        lbl_status = Gtk.Label(xalign=0.0)
        lbl_status.set_markup("<small><span foreground='#94a3b8'>所有變更即時套用至 MATE Panel。</span></small>")
        outer.pack_start(lbl_status, False, False, 0)

        def rgba_to_hex(rgba):
            r = max(0, min(255, int(round(rgba.red * 255))))
            g = max(0, min(255, int(round(rgba.green * 255))))
            b = max(0, min(255, int(round(rgba.blue * 255))))
            return f"#{r:02x}{g:02x}{b:02x}"

        def apply_all(*_):
            self.config["weather_interval_min"] = int(spin_interval.get_value_as_int())
            self.config["show_weather_icon"] = chk_w_icon.get_active()
            self.config["show_weather_temp"] = chk_w_temp.get_active()
            self.config["show_date"] = chk_date.get_active()
            self.config["show_weekday"] = chk_wd.get_active()
            self.config["use_24h"] = chk_24h.get_active()
            self.config["calendar_font_size"] = int(spin_cal_fs.get_value_as_int())
            self.config["show_world_clock"] = chk_world.get_active()
            sel_wc = [tz for cb, tz in wc_checkboxes if cb.get_active()]
            for ex in [s.strip() for s in entry_world_custom.get_text().split(",") if s.strip()]:
                if ex not in sel_wc:
                    sel_wc.append(ex)
            self.config["world_clocks"] = ", ".join(sel_wc)
            self.config["badge_bg_color"] = rgba_to_hex(bg_btn.get_rgba())
            op_v = max(0, min(100, int(round(scale_op.get_value()))))
            self.config["badge_bg_opacity"] = op_v
            lbl_op.set_text(f"{op_v}%")
            self.config["font_color_mode"] = "dynamic" if rb_fc_dyn.get_active() else "custom"
            self.config["custom_font_color"] = rgba_to_hex(fc_btn.get_rgba())
            fop_v = max(10, min(100, int(round(scale_font_op.get_value()))))
            self.config["font_opacity"] = fop_v
            lbl_font_op.set_text(f"{fop_v}%")
            self.config["text_shadow"] = chk_shadow.get_active()
            self._save_config()
            self._sync_weather_timer()
            self._update_canvas_size()
            self._update_tooltip()
            if self.popup_win is not None and self.popup_win.is_visible():
                self.popup_win.refresh_view()

        def set_city(name, lat, lon):
            self.config["city_name"] = name
            self.config["latitude"] = float(lat)
            self.config["longitude"] = float(lon)
            self._save_config()
            combo_city_res.remove_all()
            found_cities.clear()
            combo_city_res.append("cur", f"目前地點：{name} ({lat:.4f}, {lon:.4f})")
            combo_city_res.set_active_id("cur")
            lbl_status.set_markup(f"<small>⏳ 正在抓取「{html.escape(name)}」最新 7 天天氣與 AQI...</small>")
            self.fetch_weather_async(
                force=True,
                on_done_cb=lambda err: lbl_status.set_markup(
                    f"<small><span foreground='#f87171'>❌ {html.escape(str(err))}</span></small>"
                    if err
                    else f"<small><span foreground='#34d399'>✔ 已更新「{html.escape(name)}」最新 7 天天氣預報</span></small>"
                ),
            )

        def on_search_city(*_):
            q = entry_search.get_text().strip()
            if not q:
                return
            btn_search.set_sensitive(False)
            lbl_status.set_markup(f"<small>⏳ 正在搜尋城市「{html.escape(q)}」...</small>")

            def _sw():
                res, err = OpenMeteoClient.search_cities_sync(q)

                def _sdone():
                    btn_search.set_sensitive(True)
                    if err or not res:
                        lbl_status.set_markup(
                            f"<small><span foreground='#f87171'>❌ {html.escape(str(err or '找不到符合的城市'))}</span></small>"
                        )
                        return False
                    combo_city_res.remove_all()
                    found_cities.clear()
                    for idx, it in enumerate(res):
                        cid = f"c{idx}"
                        found_cities[cid] = it
                        combo_city_res.append(cid, f"{it['label']} ({it['lat']:.3f}, {it['lon']:.3f})")
                    combo_city_res.set_active_id("c0")
                    lbl_status.set_markup(
                        f"<small><span foreground='#34d399'>✔ 找到 {len(res)} 個地點，請從下拉選單選取</span></small>"
                    )
                    return False

                GLib.idle_add(_sdone)

            threading.Thread(target=_sw, daemon=True).start()

        def on_city_combo_changed(*_):
            cid = combo_city_res.get_active_id()
            if cid in found_cities:
                it = found_cities[cid]
                set_city(it["name"], it["lat"], it["lon"])

        for b, cn, cla, clo in preset_btns:
            b.connect("clicked", lambda _b, n=cn, la=cla, lo=clo: set_city(n, la, lo))
        btn_search.connect("clicked", on_search_city)
        entry_search.connect("activate", on_search_city)
        combo_city_res.connect("changed", on_city_combo_changed)
        btn_now_fetch.connect(
            "clicked",
            lambda *_: (
                lbl_status.set_markup("<small>⏳ 正在更新最新天氣...</small>"),
                self.fetch_weather_async(
                    force=True,
                    on_done_cb=lambda err: lbl_status.set_markup(
                        f"<small><span foreground='#f87171'>❌ {html.escape(str(err))}</span></small>"
                        if err
                        else "<small><span foreground='#34d399'>✔ 天氣預報已更新</span></small>"
                    ),
                ),
            ),
        )
        bg_btn.connect("color-set", apply_all)
        fc_btn.connect("color-set", lambda *_: (rb_fc_cus.set_active(True), apply_all()))
        scale_op.connect("value-changed", apply_all)
        scale_font_op.connect("value-changed", apply_all)
        entry_world_custom.connect("activate", apply_all)
        entry_world_custom.connect("focus-out-event", lambda *_: (apply_all(), False)[1])
        for cb, _tz in wc_checkboxes:
            cb.connect("toggled", apply_all)
        for w in (chk_w_icon, chk_w_temp, chk_date, chk_wd, chk_24h, chk_world, rb_fc_dyn, rb_fc_cus, chk_shadow):
            w.connect("toggled", apply_all)
        for sp in (spin_interval, spin_cal_fs):
            sp.connect("value-changed", apply_all)

        # Bottom Row
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_cal = Gtk.Button(label="📅 開啟天氣月曆")
        btn_cal.connect("clicked", lambda *_: self.toggle_popup_window())
        btn_close = Gtk.Button(label="關閉")
        btn_close.connect("clicked", lambda *_: win.destroy())
        btn_row.pack_start(btn_cal, False, False, 0)
        btn_row.pack_end(btn_close, False, False, 0)
        outer.pack_end(btn_row, False, False, 0)

        win.add(outer)
        win.show_all()

    def show_about_dialog(self, *_args):
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("月曆、時鐘與天氣預報 Plus")
        dlg.set_version("1.0.0")
        dlg.set_comments(
            "取代 MATE Panel 內建時鐘與月曆的整合小程式。\n"
            "採用桌面主題天氣圖示、Open-Meteo 免 Key 7 天天氣與 AQI 預報，支援互動天氣月曆與點擊詳情。"
        )
        dlg.set_website("https://github.com/sophAi/matepanel-applet-plus")
        dlg.run()
        dlg.destroy()

    def _on_destroy(self, *_args):
        for tid_attr in ("_minute_timer_id", "_weather_timer_id"):
            tid = getattr(self, tid_attr, None)
            if tid is not None:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                setattr(self, tid_attr, None)
        if self.popup_win is not None:
            self.popup_win.destroy()
            self.popup_win = None
        if self.prefs_win is not None:
            self.prefs_win.destroy()
            self.prefs_win = None


def setup_applet_context_menu(applet, widget):
    action_group = Gtk.ActionGroup(name="ClockWeatherAppletActions")
    action_group.add_actions(
        [
            (
                "OpenCalendarAction",
                Gtk.STOCK_INDEX,
                "開啟天氣月曆 (_Weather Calendar)",
                None,
                "開啟互動月曆與 7 天詳細天氣預報",
                widget.toggle_popup_window,
            ),
            (
                "RefreshWeatherAction",
                Gtk.STOCK_REFRESH,
                "立即更新天氣 (_Refresh Weather)",
                None,
                "立即從 Open-Meteo 更新最新天氣與空氣品質",
                lambda *_: widget.fetch_weather_async(force=True),
            ),
            (
                "PreferencesAction",
                Gtk.STOCK_PREFERENCES,
                "偏好設定 (_Preferences)...",
                None,
                "設定地點、外觀顏色、透明度與世界時鐘",
                widget.show_preferences_dialog,
            ),
            (
                "AboutAction",
                Gtk.STOCK_ABOUT,
                "關於 (_About)...",
                None,
                "關於月曆、時鐘與天氣預報 Plus",
                widget.show_about_dialog,
            ),
        ]
    )
    menu_xml = (
        '<menuitem name="OpenCalendar" action="OpenCalendarAction"/>'
        '<menuitem name="RefreshWeather" action="RefreshWeatherAction"/>'
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

    widget = ClockWeatherWidget(applet=applet)
    init_size = applet.get_size()
    if init_size:
        widget.set_panel_size(init_size)

    setup_applet_context_menu(applet, widget)
    applet.connect("button-press-event", widget.handle_applet_button_press)
    applet.connect("change-size", lambda _ap, sz: widget.set_panel_size(sz))

    applet.add(widget)
    applet.show_all()


def applet_factory(applet, iid, _data):
    if iid not in ("ClockWeatherApplet", "MateClockWeatherApplet"):
        return False
    applet_fill(applet)
    return True


def run_standalone_window():
    win = Gtk.Window(title="MATE 月曆、時鐘與天氣預報 Plus - 預覽測試視窗")
    win.set_default_size(520, 140)
    win.set_position(Gtk.WindowPosition.CENTER)
    win.connect("destroy", Gtk.main_quit)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    vbox.set_margin_top(16)
    vbox.set_margin_bottom(16)
    vbox.set_margin_start(16)
    vbox.set_margin_end(16)

    lbl = Gtk.Label(xalign=0.0)
    lbl.set_markup(
        "<b>MATE Panel 模擬預覽列 — 月曆、時鐘與天氣預報 Plus</b>\n"
        "<small>左鍵點擊開啟 7 天天氣互動月曆 | 中鍵立即更新天氣 | 右鍵偏好設定</small>"
    )
    vbox.pack_start(lbl, False, False, 0)

    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    widget = ClockWeatherWidget(applet=None)
    widget.set_panel_size(32)
    hbox.pack_start(widget, False, False, 0)

    btn_prefs = Gtk.Button(label="⚙️ 偏好設定")
    btn_prefs.connect("clicked", widget.show_preferences_dialog)
    hbox.pack_end(btn_prefs, False, False, 0)
    vbox.pack_start(hbox, False, False, 0)

    win.add(vbox)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    if "--window" in sys.argv or "-w" in sys.argv:
        run_standalone_window()
    else:
        if not HAS_MATE_PANEL:
            sys.stderr.write("Error: MatePanelApplet GI bindings not available.\n")
            sys.exit(1)
        MatePanelApplet.Applet.factory_main(
            "ClockWeatherAppletFactory",
            True,
            MatePanelApplet.Applet.__gtype__,
            applet_factory,
            None,
        )
