#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
MATE Panel CPU & GPU Temperature Monitor Applet (TempMonitorApplet)

Features:
- Zero-subprocess sensor polling:
  - CPU & motherboard/NVMe sensors read directly from /sys/class/hwmon
  - NVIDIA GPU temperature read directly via ctypes & libnvidia-ml.so.1 (NVML)
  - Fallback support for AMD/Intel GPU via /sys/class/hwmon (amdgpu, radeon, i915, xe)
- Two switchable panel display modes:
  1. 'badge'     : Dual dynamic Cairo-rendered icons embedding CPU and GPU average temperatures
  2. 'icon_text' : Compact CPU icon + temperature text | GPU icon + temperature text
- Left-click popup menu showing:
  - Detailed readings of all CPU cores/packages, GPU devices, and optional auxiliary sensors
  - Toggle to enable/disable temperature monitoring (0% background polling when disabled)
  - Configurable monitoring interval (1s, 2s, 3s, 5s, 10s, 30s, or custom)
  - Display style switcher ('badge' vs 'icon_text')
- Standalone test window mode (`--window`) for instant preview and debugging without restarting mate-panel.
"""

import ctypes
import glob
import json
import math
import os
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango
import cairo

try:
    gi.require_version("MatePanelApplet", "4.0")
    from gi.repository import MatePanelApplet
    HAS_MATE_PANEL = True
except (ValueError, ImportError):
    HAS_MATE_PANEL = False


CONFIG_DIR = os.path.expanduser("~/.config/matepanel-applet-plus")
CONFIG_PATH = os.path.join(CONFIG_DIR, "temp-monitor.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "interval_sec": 2,
    "display_mode": "badge",  # "badge" (雙圖示內嵌溫度) or "icon_text" (圖示+溫度文字)
    "show_other_sensors": True,
    "cpu_avg_cores_only": True,  # True: average across Core 0..N; False: include Package id
}


class NVMLReader:
    """Zero-overhead NVIDIA GPU temperature reader using libnvidia-ml.so.1 via ctypes."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self):
        self.lib = None
        self.initialized = False
        self._init_nvml()

    def _init_nvml(self):
        if self.initialized:
            return True
        try:
            self.lib = ctypes.CDLL("libnvidia-ml.so.1")
            res = self.lib.nvmlInit_v2()
            if res == 0:
                self.initialized = True
                return True
        except OSError:
            self.lib = None
        return False

    def shutdown(self):
        if self.initialized and self.lib is not None:
            try:
                self.lib.nvmlShutdown()
            except Exception:
                pass
        self.initialized = False

    def read_gpus(self):
        """Return a list of dicts: [{'label': 'NVIDIA GeForce GTX 1650', 'temp': 41.0, 'high': 85.0, 'crit': 95.0}]"""
        if not self.initialized and not self._init_nvml():
            return []

        gpus = []
        try:
            count = ctypes.c_uint()
            if self.lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
                return []

            for idx in range(count.value):
                handle = ctypes.c_void_p()
                if self.lib.nvmlDeviceGetHandleByIndex_v2(idx, ctypes.byref(handle)) != 0:
                    continue

                name_buf = ctypes.create_string_buffer(96)
                name = f"NVIDIA GPU {idx}"
                if self.lib.nvmlDeviceGetName(handle, name_buf, ctypes.c_uint(96)) == 0:
                    name = name_buf.value.decode("utf-8", errors="replace").strip()

                temp = ctypes.c_uint()
                if (
                    self.lib.nvmlDeviceGetTemperature(
                        handle, self.NVML_TEMPERATURE_GPU, ctypes.byref(temp)
                    )
                    == 0
                ):
                    label = name if count.value == 1 else f"[{idx}] {name}"
                    gpus.append(
                        {
                            "chip": "nvml",
                            "label": label,
                            "temp": float(temp.value),
                            "high": 83.0,
                            "crit": 93.0,
                        }
                    )
        except Exception:
            pass
        return gpus


class HardwareSensorManager:
    """Reads CPU, GPU, and auxiliary hardware temperatures directly from sysfs and NVML."""

    CPU_DRIVERS = {"coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz_cpu"}
    GPU_DRIVERS = {"amdgpu", "radeon", "nouveau"}

    def __init__(self):
        self.nvml = NVMLReader()

    def close(self):
        self.nvml.shutdown()

    @staticmethod
    def _read_file_str(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def _read_file_float(path, scale=0.001):
        val_str = HardwareSensorManager._read_file_str(path)
        if val_str is None:
            return None
        try:
            return float(val_str) * scale
        except ValueError:
            return None

    def poll(self, cpu_avg_cores_only=True):
        """
        Poll all sensors and return a structured dictionary:
        {
            "cpu_avg": float or None,
            "gpu_avg": float or None,
            "cpu_sensors": [ {"chip": str, "label": str, "temp": float, "high": float|None, "crit": float|None, "is_core": bool} ],
            "gpu_sensors": [ {"chip": str, "label": str, "temp": float, "high": float|None, "crit": float|None} ],
            "other_sensors": [ {"chip": str, "label": str, "value_str": str, "temp": float|None} ],
        }
        """
        cpu_sensors = []
        gpu_sensors = []
        other_sensors = []

        hwmon_dirs = sorted(glob.glob("/sys/class/hwmon/hwmon*"))
        for hdir in hwmon_dirs:
            chip_name = self._read_file_str(os.path.join(hdir, "name")) or os.path.basename(hdir)

            # Check temperatures
            temp_inputs = sorted(glob.glob(os.path.join(hdir, "temp*_input")))
            for t_in in temp_inputs:
                prefix = t_in[:-6]  # strip '_input'
                temp_val = self._read_file_float(t_in, 0.001)
                if temp_val is None or temp_val <= -100.0 or temp_val >= 200.0:
                    continue

                raw_label = self._read_file_str(prefix + "_label")
                label = raw_label if raw_label else os.path.basename(prefix)
                high_val = self._read_file_float(prefix + "_max", 0.001)
                crit_val = self._read_file_float(prefix + "_crit", 0.001)

                if chip_name in self.CPU_DRIVERS:
                    is_core = label.lower().startswith("core") or label.lower().startswith("tccd")
                    cpu_sensors.append(
                        {
                            "chip": chip_name,
                            "label": label,
                            "temp": temp_val,
                            "high": high_val,
                            "crit": crit_val,
                            "is_core": is_core,
                        }
                    )
                elif chip_name in self.GPU_DRIVERS:
                    gpu_sensors.append(
                        {
                            "chip": chip_name,
                            "label": f"{chip_name} ({label})",
                            "temp": temp_val,
                            "high": high_val,
                            "crit": crit_val,
                        }
                    )
                else:
                    other_sensors.append(
                        {
                            "chip": chip_name,
                            "label": f"{chip_name}: {label}",
                            "value_str": f"{temp_val:.1f}°C",
                            "temp": temp_val,
                        }
                    )

            # Also check fan speeds (e.g., cpu_fan)
            fan_inputs = sorted(glob.glob(os.path.join(hdir, "fan*_input")))
            for f_in in fan_inputs:
                prefix = f_in[:-6]
                rpm = self._read_file_float(f_in, 1.0)
                if rpm is None:
                    continue
                raw_label = self._read_file_str(prefix + "_label") or os.path.basename(prefix)
                other_sensors.append(
                    {
                        "chip": chip_name,
                        "label": f"{chip_name}: {raw_label}",
                        "value_str": f"{int(rpm)} RPM",
                        "temp": None,
                    }
                )

        # Poll NVIDIA GPUs via NVML
        nv_gpus = self.nvml.read_gpus()
        gpu_sensors.extend(nv_gpus)

        # Compute CPU average
        cpu_avg = None
        if cpu_sensors:
            if cpu_avg_cores_only:
                core_vals = [s["temp"] for s in cpu_sensors if s["is_core"]]
                if not core_vals:
                    core_vals = [s["temp"] for s in cpu_sensors]
            else:
                core_vals = [s["temp"] for s in cpu_sensors]
            cpu_avg = sum(core_vals) / len(core_vals)

        # Compute GPU average
        gpu_avg = None
        if gpu_sensors:
            gpu_vals = [s["temp"] for s in gpu_sensors]
            gpu_avg = sum(gpu_vals) / len(gpu_vals)

        return {
            "cpu_avg": cpu_avg,
            "gpu_avg": gpu_avg,
            "cpu_sensors": cpu_sensors,
            "gpu_sensors": gpu_sensors,
            "other_sensors": other_sensors,
        }


def temp_to_colors(temp, enabled=True):
    """
    Return (bg_rgb, border_rgb, text_rgb, accent_hex) for a given temperature in Celsius.
    """
    if not enabled or temp is None:
        return (
            (0.24, 0.26, 0.29),
            (0.45, 0.48, 0.52),
            (0.78, 0.80, 0.82),
            "#9aa0a6",
        )
    if temp < 55.0:
        # Cool emerald/teal
        return (
            (0.11, 0.36, 0.27),
            (0.20, 0.78, 0.54),
            (0.95, 1.00, 0.97),
            "#34d399",
        )
    elif temp < 75.0:
        # Warm amber/orange
        return (
            (0.45, 0.28, 0.07),
            (0.96, 0.65, 0.14),
            (1.00, 0.98, 0.92),
            "#fbbf24",
        )
    else:
        # Hot crimson/red
        return (
            (0.48, 0.11, 0.14),
            (0.96, 0.28, 0.32),
            (1.00, 0.95, 0.95),
            "#f87171",
        )


def draw_rounded_rect(ctx, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


class TempMonitorWidget(Gtk.EventBox):
    """
    Main widget placed inside either a MatePanelApplet or a standalone Gtk.Window.
    Supports two switchable visual modes ('badge' and 'icon_text') and popup menu on click.
    """

    INTERVAL_PRESETS = [1, 2, 3, 5, 10, 30]

    def __init__(self, applet=None):
        super().__init__()
        self.applet = applet
        self.set_visible_window(False)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )

        self.config = self._load_config()
        self.sensor_mgr = HardwareSensorManager()
        self.latest_data = {
            "cpu_avg": None,
            "gpu_avg": None,
            "cpu_sensors": [],
            "gpu_sensors": [],
            "other_sensors": [],
        }
        self.timer_id = None
        self.panel_size = 28

        # DrawingArea used for both 'badge' and 'icon_text' rendering via Cairo
        self.canvas = Gtk.DrawingArea()
        self.canvas.connect("draw", self._on_draw)
        self.add(self.canvas)

        self.connect("button-press-event", self._on_button_press)
        self.connect("destroy", self._on_destroy)

        # Initial poll & timer start
        self.refresh_readings()
        self._sync_timer()
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

    def set_panel_size(self, size):
        self.panel_size = max(22, min(64, int(size)))
        self._update_canvas_size()

    def _update_canvas_size(self):
        h = max(24, self.panel_size)
        mode = self.config.get("display_mode", "badge")
        if mode == "badge":
            # Two square-ish badges side by side
            badge_w = max(28, int(h * 1.12))
            total_w = badge_w * 2 + 6
        else:
            # Icon + text for CPU and GPU side by side
            total_w = max(124, int(h * 4.5))
        self.canvas.set_size_request(total_w, h)
        self.canvas.queue_draw()

    def _sync_timer(self):
        if self.timer_id is not None:
            GLib.source_remove(self.timer_id)
            self.timer_id = None

        if self.config.get("enabled", True):
            interval = max(1, int(self.config.get("interval_sec", 2)))
            self.timer_id = GLib.timeout_add_seconds(interval, self._on_timer_tick)
        else:
            # Shutdown NVML handle when monitoring is disabled
            self.sensor_mgr.close()

    def _on_timer_tick(self):
        if not self.config.get("enabled", True):
            self.timer_id = None
            return False
        self.refresh_readings()
        return True

    def refresh_readings(self, force_poll=False):
        if self.config.get("enabled", True) or force_poll:
            self.latest_data = self.sensor_mgr.poll(
                cpu_avg_cores_only=self.config.get("cpu_avg_cores_only", True)
            )
        self._update_tooltip()
        self._update_canvas_size()

    def _update_tooltip(self):
        enabled = self.config.get("enabled", True)
        interval = self.config.get("interval_sec", 2)
        cpu_avg = self.latest_data.get("cpu_avg")
        gpu_avg = self.latest_data.get("gpu_avg")

        cpu_str = f"{cpu_avg:.1f}°C" if (enabled and cpu_avg is not None) else ("已關閉" if not enabled else "N/A")
        gpu_str = f"{gpu_avg:.1f}°C" if (enabled and gpu_avg is not None) else ("已關閉" if not enabled else "N/A")
        status_str = f"啟用中 (每 {interval} 秒更新)" if enabled else "已暫停監控 (點擊選單可啟用)"
        self.set_tooltip_markup(
            f"<b>CPU / GPU 溫度監控</b>\n"
            f"• CPU 平均溫度: <b>{cpu_str}</b>\n"
            f"• GPU 平均溫度: <b>{gpu_str}</b>\n"
            f"<small>{status_str} — 點擊查看所有感測器與設定</small>"
        )

    def _on_draw(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        enabled = self.config.get("enabled", True)
        mode = self.config.get("display_mode", "badge")

        cpu_avg = self.latest_data.get("cpu_avg") if enabled else None
        gpu_avg = self.latest_data.get("gpu_avg") if enabled else None

        if mode == "badge":
            self._draw_badge_mode(ctx, w, h, cpu_avg, gpu_avg, enabled)
        else:
            self._draw_icon_text_mode(ctx, w, h, cpu_avg, gpu_avg, enabled)
        return False

    def _draw_single_badge(self, ctx, x, y, bw, bh, title, temp, enabled):
        bg_rgb, border_rgb, text_rgb, _ = temp_to_colors(temp, enabled)

        # Rounded badge background
        draw_rounded_rect(ctx, x + 0.5, y + 0.5, bw - 1.0, bh - 1.0, 4.5)
        ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], 0.92)
        ctx.fill_preserve()
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 0.95)
        ctx.set_line_width(1.2)
        ctx.stroke()

        # Top header bar inside badge
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        header_size = max(7.0, bh * 0.27)
        ctx.set_font_size(header_size)
        ext = ctx.text_extents(title)
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 1.0)
        ctx.move_to(x + (bw - ext.width) / 2.0 - ext.x_bearing, y + header_size + 1.5)
        ctx.show_text(title)

        # Temperature value text
        if not enabled:
            val_str = "OFF"
        elif temp is None:
            val_str = "--°"
        else:
            val_str = f"{int(round(temp))}°"

        val_size = max(9.5, bh * 0.42)
        ctx.set_font_size(val_size)
        v_ext = ctx.text_extents(val_str)
        ctx.set_source_rgba(text_rgb[0], text_rgb[1], text_rgb[2], 1.0)
        ctx.move_to(
            x + (bw - v_ext.width) / 2.0 - v_ext.x_bearing,
            y + bh - 3.0,
        )
        ctx.show_text(val_str)

    def _draw_badge_mode(self, ctx, w, h, cpu_avg, gpu_avg, enabled):
        pad_y = 1.0
        bh = max(20.0, h - pad_y * 2)
        gap = 4.0
        bw = (w - gap - 2.0) / 2.0

        self._draw_single_badge(ctx, 1.0, pad_y, bw, bh, "CPU", cpu_avg, enabled)
        self._draw_single_badge(ctx, 1.0 + bw + gap, pad_y, bw, bh, "GPU", gpu_avg, enabled)

    def _draw_mini_chip_icon(self, ctx, x, y, size, label_char, border_rgb, bg_rgb):
        # Draw chip pins
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 0.9)
        ctx.set_line_width(1.2)
        pin_offsets = [0.25, 0.5, 0.75]
        for p in pin_offsets:
            px = x + size * p
            py = y + size * p
            # top/bottom pins
            ctx.move_to(px, y - 1.5)
            ctx.line_to(px, y + 1.5)
            ctx.move_to(px, y + size - 1.5)
            ctx.line_to(px, y + size + 1.5)
            # left/right pins
            ctx.move_to(x - 1.5, py)
            ctx.line_to(x + 1.5, py)
            ctx.move_to(x + size - 1.5, py)
            ctx.line_to(x + size + 1.5, py)
        ctx.stroke()

        # Chip body
        draw_rounded_rect(ctx, x + 1, y + 1, size - 2, size - 2, 3.0)
        ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], 0.95)
        ctx.fill_preserve()
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 1.0)
        ctx.set_line_width(1.2)
        ctx.stroke()

        # Letter inside chip ('C' or 'G')
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(size * 0.56)
        ext = ctx.text_extents(label_char)
        ctx.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        ctx.move_to(
            x + (size - ext.width) / 2.0 - ext.x_bearing,
            y + (size + ext.height) / 2.0,
        )
        ctx.show_text(label_char)

    def _draw_icon_text_mode(self, ctx, w, h, cpu_avg, gpu_avg, enabled):
        half_w = w / 2.0
        items = [
            ("C", cpu_avg, 2.0),
            ("G", gpu_avg, half_w + 2.0),
        ]

        icon_size = min(18.0, max(14.0, h - 8.0))
        icon_y = (h - icon_size) / 2.0

        for char, temp, start_x in items:
            bg_rgb, border_rgb, _, _ = temp_to_colors(temp, enabled)
            self._draw_mini_chip_icon(ctx, start_x + 2.0, icon_y, icon_size, char, border_rgb, bg_rgb)

            if not enabled:
                t_str = "OFF"
            elif temp is None:
                t_str = "--°C"
            else:
                t_str = f"{temp:.0f}°C"

            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            font_sz = min(13.0, max(10.0, h * 0.44))
            ctx.set_font_size(font_sz)
            ext = ctx.text_extents(t_str)
            ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 1.0)
            ctx.move_to(start_x + icon_size + 6.0, (h + ext.height) / 2.0)
            ctx.show_text(t_str)

    def _on_button_press(self, widget, event):
        # Left click (1) opens the detailed sensor and control menu
        # In standalone mode, right click (3) also opens it; on MATE Panel, button 3 is reserved for panel applet menu
        if event.button == 1 or (event.button == 3 and self.applet is None):
            self.refresh_readings(force_poll=self.config.get("enabled", True))
            menu = self._build_popup_menu()
            menu.popup_at_widget(
                self,
                Gdk.Gravity.SOUTH_WEST,
                Gdk.Gravity.NORTH_WEST,
                event,
            )
            return True
        return False

    def _create_Markup_menu_item(self, markup, sensitive=True, callback=None):
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
        enabled = self.config.get("enabled", True)
        cpu_avg = self.latest_data.get("cpu_avg")
        gpu_avg = self.latest_data.get("gpu_avg")

        # 1. Header summary
        _, _, _, cpu_hex = temp_to_colors(cpu_avg, enabled)
        _, _, _, gpu_hex = temp_to_colors(gpu_avg, enabled)
        cpu_avg_txt = f"{cpu_avg:.1f}°C" if (enabled and cpu_avg is not None) else ("已暫停" if not enabled else "N/A")
        gpu_avg_txt = f"{gpu_avg:.1f}°C" if (enabled and gpu_avg is not None) else ("已暫停" if not enabled else "N/A")

        header_item = self._create_Markup_menu_item(
            f"<b>🌡 硬體溫度監控總覽</b>   "
            f"<span foreground='{cpu_hex}'><b>CPU: {cpu_avg_txt}</b></span>  |  "
            f"<span foreground='{gpu_hex}'><b>GPU: {gpu_avg_txt}</b></span>",
            sensitive=False,
        )
        menu.append(header_item)
        menu.append(Gtk.SeparatorMenuItem())

        # 2. CPU Sensor Units Section
        cpu_title = self._create_Markup_menu_item(
            f"<b>🖥 CPU 監控單元 (平均: {cpu_avg_txt})</b>", sensitive=False
        )
        menu.append(cpu_title)

        cpu_sensors = self.latest_data.get("cpu_sensors", [])
        if not cpu_sensors:
            menu.append(self._create_Markup_menu_item("   <i>無感測資料</i>", sensitive=False))
        else:
            for s in cpu_sensors:
                _, _, _, col = temp_to_colors(s["temp"], True)
                extra = ""
                if s.get("high") and s.get("crit"):
                    extra = f" <small><span foreground='#888888'>(High: {s['high']:.0f}°C / Crit: {s['crit']:.0f}°C)</span></small>"
                row = self._create_Markup_menu_item(
                    f"   • <tt>{s['label']:<14}</tt> <span foreground='{col}'><b>{s['temp']:.1f}°C</b></span>{extra}",
                    sensitive=True,
                )
                menu.append(row)

        menu.append(Gtk.SeparatorMenuItem())

        # 3. GPU Sensor Units Section
        gpu_title = self._create_Markup_menu_item(
            f"<b>🎮 GPU 監控單元 (平均: {gpu_avg_txt})</b>", sensitive=False
        )
        menu.append(gpu_title)

        gpu_sensors = self.latest_data.get("gpu_sensors", [])
        if not gpu_sensors:
            menu.append(self._create_Markup_menu_item("   <i>未偵測到獨立或內建 GPU 溫度感測器</i>", sensitive=False))
        else:
            for s in gpu_sensors:
                _, _, _, col = temp_to_colors(s["temp"], True)
                row = self._create_Markup_menu_item(
                    f"   • <tt>{s['label']}</tt>   <span foreground='{col}'><b>{s['temp']:.1f}°C</b></span>",
                    sensitive=True,
                )
                menu.append(row)

        # 4. Optional Auxiliary Hardware Sensors (NVMe, ACPI, Fans)
        if self.config.get("show_other_sensors", True):
            other_sensors = self.latest_data.get("other_sensors", [])
            if other_sensors:
                menu.append(Gtk.SeparatorMenuItem())
                other_title = self._create_Markup_menu_item(
                    "<b>💾 其他系統感測器 (NVMe / 主機板 / 風扇)</b>", sensitive=False
                )
                menu.append(other_title)
                for s in other_sensors:
                    t_val = s.get("temp")
                    if t_val is not None:
                        _, _, _, col = temp_to_colors(t_val, True)
                        v_markup = f"<span foreground='{col}'><b>{s['value_str']}</b></span>"
                    else:
                        v_markup = f"<b>{s['value_str']}</b>"
                    row = self._create_Markup_menu_item(
                        f"   • <tt>{s['label']:<22}</tt> {v_markup}",
                        sensitive=True,
                    )
                    menu.append(row)

        menu.append(Gtk.SeparatorMenuItem())

        # 5. Control Options: Enable/Disable Monitoring
        toggle_mon = Gtk.CheckMenuItem(label="啟用自動溫度監控 (關閉時停止背景輪詢)")
        toggle_mon.set_active(enabled)
        toggle_mon.connect("toggled", self._on_toggle_monitoring)
        menu.append(toggle_mon)

        refresh_now = Gtk.MenuItem(label="🔄 立即手動更新一次溫度")
        refresh_now.connect("activate", lambda *_: self.refresh_readings(force_poll=True))
        menu.append(refresh_now)

        # 6. Submenu: Monitoring Frequency
        freq_item = Gtk.MenuItem(label=f"⏱ 監控時間頻率 (目前: {self.config.get('interval_sec', 2)} 秒)")
        freq_sub = Gtk.Menu()
        current_int = int(self.config.get("interval_sec", 2))
        group_leader = None
        for sec in self.INTERVAL_PRESETS:
            r_item = Gtk.RadioMenuItem.new_with_label_from_widget(
                group_leader, f"每 {sec} 秒更新" + (" (預設)" if sec == 2 else "")
            )
            if group_leader is None:
                group_leader = r_item
            if sec == current_int:
                r_item.set_active(True)
            r_item.connect("toggled", self._on_interval_selected, sec)
            freq_sub.append(r_item)

        freq_sub.append(Gtk.SeparatorMenuItem())
        custom_int_item = Gtk.MenuItem(label="自訂秒數...")
        custom_int_item.connect("activate", self._on_custom_interval_dialog)
        freq_sub.append(custom_int_item)
        freq_item.set_submenu(freq_sub)
        menu.append(freq_item)

        # 7. Submenu: Panel Icon Display Style
        style_item = Gtk.MenuItem(label="🎨 面板圖示顯示樣式")
        style_sub = Gtk.Menu()
        cur_mode = self.config.get("display_mode", "badge")

        mode_badge = Gtk.RadioMenuItem.new_with_label_from_widget(
            None, "雙動態溫度方塊圖示 (CPU / GPU 內嵌平均溫度)"
        )
        mode_icon_text = Gtk.RadioMenuItem.new_with_label_from_widget(
            mode_badge, "晶片小圖示 + 溫度文字並排 (C 39°C | G 41°C)"
        )
        if cur_mode == "badge":
            mode_badge.set_active(True)
        else:
            mode_icon_text.set_active(True)

        mode_badge.connect("toggled", self._on_display_mode_toggled, "badge")
        mode_icon_text.connect("toggled", self._on_display_mode_toggled, "icon_text")
        style_sub.append(mode_badge)
        style_sub.append(mode_icon_text)

        style_sub.append(Gtk.SeparatorMenuItem())
        show_other_chk = Gtk.CheckMenuItem(label="在選單中顯示其他感測器 (NVMe / 風扇)")
        show_other_chk.set_active(self.config.get("show_other_sensors", True))
        show_other_chk.connect("toggled", self._on_toggle_other_sensors)
        style_sub.append(show_other_chk)

        cores_only_chk = Gtk.CheckMenuItem(label="CPU 平均溫度僅計算實體核心 (Core 0..N)")
        cores_only_chk.set_active(self.config.get("cpu_avg_cores_only", True))
        cores_only_chk.connect("toggled", self._on_toggle_cores_only)
        style_sub.append(cores_only_chk)

        style_item.set_submenu(style_sub)
        menu.append(style_item)

        menu.show_all()
        return menu

    def _on_toggle_monitoring(self, check_item):
        self.config["enabled"] = check_item.get_active()
        self._save_config()
        self._sync_timer()
        self.refresh_readings()

    def _on_interval_selected(self, radio_item, sec):
        if not radio_item.get_active():
            return
        self.config["interval_sec"] = int(sec)
        self._save_config()
        self._sync_timer()
        self._update_tooltip()

    def _on_custom_interval_dialog(self, _item):
        dialog = Gtk.Dialog(
            title="設定監控時間頻率",
            transient_for=self.get_toplevel() if isinstance(self.get_toplevel(), Gtk.Window) else None,
            flags=0,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        dialog.set_default_size(280, 120)

        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

        lbl = Gtk.Label(label="請輸入溫度監控更新間隔（秒）：", xalign=0.0)
        adj = Gtk.Adjustment(
            value=float(self.config.get("interval_sec", 2)),
            lower=1.0,
            upper=3600.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin = Gtk.SpinButton(adjustment=adj, climb_rate=1.0, digits=0)
        box.pack_start(lbl, False, False, 0)
        box.pack_start(spin, False, False, 0)
        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            self.config["interval_sec"] = max(1, int(spin.get_value_as_int()))
            self._save_config()
            self._sync_timer()
            self._update_tooltip()
        dialog.destroy()

    def _on_display_mode_toggled(self, radio_item, mode):
        if not radio_item.get_active():
            return
        self.config["display_mode"] = mode
        self._save_config()
        self._update_canvas_size()

    def _on_toggle_other_sensors(self, check_item):
        self.config["show_other_sensors"] = check_item.get_active()
        self._save_config()

    def _on_toggle_cores_only(self, check_item):
        self.config["cpu_avg_cores_only"] = check_item.get_active()
        self._save_config()
        self.refresh_readings(force_poll=True)

    def _on_destroy(self, *_args):
        if self.timer_id is not None:
            GLib.source_remove(self.timer_id)
            self.timer_id = None
        self.sensor_mgr.close()


def applet_fill(applet):
    """Initialize and populate the MATE Panel Applet container."""
    os.chdir(os.path.expanduser("~"))
    applet.set_flags(MatePanelApplet.AppletFlags.EXPAND_MINOR)
    applet.set_background_widget(applet)

    widget = TempMonitorWidget(applet=applet)
    init_size = applet.get_size()
    if init_size:
        widget.set_panel_size(init_size)

    applet.connect("change-size", lambda _ap, sz: widget.set_panel_size(sz))
    applet.add(widget)
    applet.show_all()


def applet_factory(applet, iid, _data):
    if iid not in ("TempMonitorApplet", "MateTempMonitorApplet"):
        return False
    applet_fill(applet)
    return True


def run_standalone_window():
    """Run the applet inside a standalone GTK test window for preview & debugging."""
    win = Gtk.Window(title="MATE CPU/GPU Temp Applet - 預覽測試視窗")
    win.set_default_size(420, 120)
    win.set_position(Gtk.WindowPosition.CENTER)
    win.connect("destroy", Gtk.main_quit)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    vbox.set_margin_top(16)
    vbox.set_margin_bottom(16)
    vbox.set_margin_start(16)
    vbox.set_margin_end(16)

    info_lbl = Gtk.Label(xalign=0.0)
    info_lbl.set_markup(
        "<b>MATE Panel 模擬預覽列</b>\n"
        "<small>請直接<b>左鍵點選</b>下方溫度圖示，開啟所有監控單元清單、切換圖示樣式、調整頻率或關閉監控：</small>"
    )
    vbox.pack_start(info_lbl, False, False, 0)

    # Dark simulated panel bar
    panel_frame = Gtk.EventBox()
    css_provider = Gtk.CssProvider()
    css_provider.load_from_data(
        b".simulated-panel { background-color: #1e2229; border-radius: 6px; padding: 6px; }"
    )
    panel_frame.get_style_context().add_provider(
        css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    panel_frame.get_style_context().add_class("simulated-panel")

    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    hbox.set_margin_top(4)
    hbox.set_margin_bottom(4)
    hbox.set_margin_start(10)
    hbox.set_margin_end(10)

    panel_lbl = Gtk.Label()
    panel_lbl.set_markup("<span foreground='#9aa0a6'><small>模擬 MATE 面板 (32px) 👉</small></span>")
    hbox.pack_start(panel_lbl, False, False, 0)

    widget = TempMonitorWidget(applet=None)
    widget.set_panel_size(32)
    hbox.pack_start(widget, False, False, 0)

    panel_frame.add(hbox)
    vbox.pack_start(panel_frame, False, False, 0)

    win.add(vbox)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    if "--window" in sys.argv or "-w" in sys.argv:
        run_standalone_window()
    elif "--dump" in sys.argv:
        mgr = HardwareSensorManager()
        data = mgr.poll()
        mgr.close()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        if not HAS_MATE_PANEL:
            sys.stderr.write("Error: MatePanelApplet GI bindings not available.\n")
            sys.exit(1)
        MatePanelApplet.Applet.factory_main(
            "TempMonitorAppletFactory",
            True,
            MatePanelApplet.Applet.__gtype__,
            applet_factory,
            None,
        )
