#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
MATE Panel CPU & GPU Temperature + Utilization Monitor Applet (TempMonitorApplet)

Features:
- Zero-subprocess sensor polling:
  - CPU & motherboard/NVMe sensors read directly from /sys/class/hwmon
  - NVIDIA GPU temperature, Volatile GPU-Util (%), Memory-Usage (MiB / %),
    Fan speed (%), and Power usage (W) read directly via ctypes & libnvidia-ml.so.1 (NVML)
  - Fallback support for AMD/Intel GPU via /sys/class/hwmon and /sys/class/drm
- Switchable panel display modes & GPU metric styles:
  1. 'badge'     : Dynamic Cairo-rendered rounded badges
     - 'separate': Separate badges [CPU 39°] [GPU 41°] [UTL 34%] [MEM 26% / 1.1G]
     - 'compact' : Compact 2-badge view [CPU 39°] [GPU 41° with embedded Util & Mem mini progress bars]
  2. 'icon_text' : Compact chip icons + text ([C] 39°C  [G] 41°C · 34% · 26% / 1.1G)
- Customizable 3-Stage Panel Font Colors & Badge Background Style to adapt to light/dark/transparent MATE panels:
  - Normal / Moderate Load (Warning) / High Load & High Temp (Critical) custom font colors via Gtk.ColorButton
  - Quick presets for Dark Panel and Light Panel themes
  - Optional text outline/shadow for crisp readability on any panel background
- Left-click popup menu on the applet + Right-click MATE Panel native context menu + Preferences window.
"""

import ctypes
from datetime import datetime
import glob
import html
import json
import math
import os
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, GObject, Gtk
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
    "display_mode": "badge",            # "badge" (方塊圖示) or "icon_text" (晶片圖示+文字)
    "gpu_badge_layout": "compact",      # "separate" (獨立方塊 [CPU][GPU][UTL][MEM]) or "compact" (GPU方塊內嵌雙進度條)
    "show_panel_gpu_util": True,        # 在面板顯示 Volatile GPU-Util
    "show_panel_gpu_mem": True,         # 在面板顯示 GPU Memory-Usage
    "gpu_mem_panel_unit": "percent",    # "percent" (26%) or "gib" (1.1G)
    "font_color_mode": "dynamic",       # "dynamic" (預設內建色階) or "custom" (自訂三段式字形顏色)
    "custom_font_color": "#ffffff",     # 自訂「正常狀態」字形顏色
    "custom_warn_font_color": "#fbbf24",# 自訂「中度負載」字形顏色
    "custom_crit_font_color": "#f87171",# 自訂「高溫/高負載警戒」字形顏色
    "badge_bg_style": "dark",           # "dark" (深色半透明底), "light" (淺色半透明底), "transparent" (透明無底色)
    "custom_badge_style": True,         # 分別自訂 CPU / GPU 方塊底色、框線色與底色透明度
    "cpu_bg_color": "#ffffff",          # CPU 方塊底色
    "cpu_border_color": "#ffffff",      # CPU 方塊框線顏色
    "cpu_bg_opacity": 44,               # CPU 方塊底色不透明度 (0..100%)
    "gpu_bg_color": "#26a269",          # GPU 方塊底色
    "gpu_border_color": "#113a2b",      # GPU 方塊框線顏色
    "gpu_bg_opacity": 56,               # GPU 方塊底色不透明度 (0..100%)
    "text_shadow": False,               # 為面板字形加上對比描邊陰影
    "show_hover_tooltip": False,        # 滑鼠懸停圖示時顯示詳細資訊 (Tooltip)
    "hide_panel_border": True,          # 取消 MATE Panel 圖示框線
    "show_other_sensors": True,         # 在選單中顯示 NVMe / 主機板 / 風扇等其他感測器
    "cpu_avg_cores_only": True,         # True: 僅平均實體核心 Core 0..N; False: 包含 Package id
    "warn_temp_c": 55.0,                # 中度負載溫度閾值 (°C)
    "crit_temp_c": 75.0,                # 高溫警戒溫度閾值 (°C)
    "warn_pct": 60.0,                   # GPU/顯存 中度負載百分比閾值 (%)
    "crit_pct": 85.0,                   # GPU/顯存 高負載警戒百分比閾值 (%)
}


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [
        ("gpu", ctypes.c_uint),
        ("memory", ctypes.c_uint),
    ]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class NVMLReader:
    """Zero-overhead NVIDIA GPU metrics reader using libnvidia-ml.so.1 via ctypes."""

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
                temp_val = None
                if (
                    self.lib.nvmlDeviceGetTemperature(
                        handle, self.NVML_TEMPERATURE_GPU, ctypes.byref(temp)
                    )
                    == 0
                ):
                    temp_val = float(temp.value)

                if temp_val is None:
                    continue

                gpu_util = None
                mem_ctrl_util = None
                util_st = _NvmlUtilization()
                if self.lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util_st)) == 0:
                    gpu_util = float(util_st.gpu)
                    mem_ctrl_util = float(util_st.memory)

                mem_used_mib = None
                mem_total_mib = None
                mem_pct = None
                mem_st = _NvmlMemory()
                if self.lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem_st)) == 0 and mem_st.total > 0:
                    mem_used_mib = mem_st.used / (1024.0 * 1024.0)
                    mem_total_mib = mem_st.total / (1024.0 * 1024.0)
                    mem_pct = (mem_st.used * 100.0) / mem_st.total

                fan_pct = None
                fan_val = ctypes.c_uint()
                if self.lib.nvmlDeviceGetFanSpeed(handle, ctypes.byref(fan_val)) == 0:
                    fan_pct = float(fan_val.value)

                power_w = None
                power_mw = ctypes.c_uint()
                if self.lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(power_mw)) == 0:
                    power_w = power_mw.value / 1000.0

                label = name if count.value == 1 else f"[{idx}] {name}"
                gpus.append(
                    {
                        "chip": "nvml",
                        "label": label,
                        "temp": temp_val,
                        "high": 83.0,
                        "crit": 93.0,
                        "gpu_util": gpu_util,
                        "mem_ctrl_util": mem_ctrl_util,
                        "mem_used_mib": mem_used_mib,
                        "mem_total_mib": mem_total_mib,
                        "mem_pct": mem_pct,
                        "fan_pct": fan_pct,
                        "power_w": power_w,
                    }
                )
        except Exception:
            pass
        return gpus


class HardwareSensorManager:
    """Reads CPU, GPU, and auxiliary hardware temperatures & metrics directly from sysfs and NVML."""

    CPU_DRIVERS = {"coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz_cpu"}
    GPU_DRIVERS = {"amdgpu", "radeon", "nouveau"}

    def __init__(self):
        self.nvml = NVMLReader()
        self._prev_cpu_total = None
        self._prev_cpu_idle = None
        # Prime initial /proc/stat snapshot so the first poll has a valid baseline
        self._read_cpu_usage_pct()

    def close(self):
        self.nvml.shutdown()

    def _read_cpu_usage_pct(self):
        """
        Zero-overhead CPU average loading (%) read directly from /proc/stat line 1.
        Returns float (0.0..100.0) or None on the very first prime call.
        """
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                line = f.readline()
            parts = line.split()
            if len(parts) < 5 or parts[0] != "cpu":
                return None
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            total = sum(vals)
            prev_t = self._prev_cpu_total
            prev_i = self._prev_cpu_idle
            self._prev_cpu_total = total
            self._prev_cpu_idle = idle
            if prev_t is None or prev_i is None:
                return None
            dt = total - prev_t
            di = idle - prev_i
            if dt <= 0:
                return 0.0
            pct = (1.0 - (float(di) / float(dt))) * 100.0
            return max(0.0, min(100.0, pct))
        except OSError:
            return None

    @staticmethod
    def _read_loadavg_str():
        try:
            with open("/proc/loadavg", "r", encoding="utf-8") as f:
                parts = f.read().split()
            if len(parts) >= 3:
                return f"{parts[0]}, {parts[1]}, {parts[2]}"
        except OSError:
            pass
        return None

    @staticmethod
    def _read_ram_usage():
        """
        Zero-overhead System RAM usage read directly from /proc/meminfo.
        Returns (ram_pct, ram_used_gib, ram_total_gib).
        """
        mem_total_kb = None
        mem_avail_kb = None
        mem_free_kb = None
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        mem_avail_kb = int(line.split()[1])
                    elif line.startswith("MemFree:"):
                        mem_free_kb = int(line.split()[1])
                    if mem_total_kb is not None and mem_avail_kb is not None:
                        break
            if mem_total_kb and mem_total_kb > 0:
                avail = mem_avail_kb if mem_avail_kb is not None else (mem_free_kb or 0)
                used_kb = max(0, mem_total_kb - avail)
                pct = (float(used_kb) / float(mem_total_kb)) * 100.0
                return (
                    max(0.0, min(100.0, pct)),
                    used_kb / (1024.0 * 1024.0),
                    mem_total_kb / (1024.0 * 1024.0),
                )
        except OSError:
            pass
        return (None, None, None)

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

    def _read_sysfs_drm_gpu_metrics(self, hwmon_dir):
        dev_dir = os.path.realpath(os.path.join(hwmon_dir, "device"))
        gpu_util = self._read_file_float(os.path.join(dev_dir, "gpu_busy_percent"), 1.0)
        vram_used = self._read_file_float(os.path.join(dev_dir, "mem_info_vram_used"), 1.0 / (1024.0 * 1024.0))
        vram_total = self._read_file_float(os.path.join(dev_dir, "mem_info_vram_total"), 1.0 / (1024.0 * 1024.0))
        mem_pct = (vram_used * 100.0 / vram_total) if (vram_used is not None and vram_total) else None
        power_uw = self._read_file_float(os.path.join(hwmon_dir, "power1_average"), 1e-6)
        return gpu_util, vram_used, vram_total, mem_pct, power_uw

    def poll(self, cpu_avg_cores_only=True):
        cpu_sensors = []
        gpu_sensors = []
        other_sensors = []

        hwmon_dirs = sorted(glob.glob("/sys/class/hwmon/hwmon*"))
        for hdir in hwmon_dirs:
            chip_name = self._read_file_str(os.path.join(hdir, "name")) or os.path.basename(hdir)

            temp_inputs = sorted(glob.glob(os.path.join(hdir, "temp*_input")))
            for t_in in temp_inputs:
                prefix = t_in[:-6]
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
                    g_util, m_used, m_tot, m_pct, p_w = self._read_sysfs_drm_gpu_metrics(hdir)
                    gpu_sensors.append(
                        {
                            "chip": chip_name,
                            "label": f"{chip_name} ({label})",
                            "temp": temp_val,
                            "high": high_val,
                            "crit": crit_val,
                            "gpu_util": g_util,
                            "mem_ctrl_util": None,
                            "mem_used_mib": m_used,
                            "mem_total_mib": m_tot,
                            "mem_pct": m_pct,
                            "fan_pct": None,
                            "power_w": p_w,
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

        nv_gpus = self.nvml.read_gpus()
        gpu_sensors.extend(nv_gpus)

        cpu_avg = None
        if cpu_sensors:
            if cpu_avg_cores_only:
                core_vals = [s["temp"] for s in cpu_sensors if s["is_core"]]
                if not core_vals:
                    core_vals = [s["temp"] for s in cpu_sensors]
            else:
                core_vals = [s["temp"] for s in cpu_sensors]
            cpu_avg = sum(core_vals) / len(core_vals)

        gpu_avg = None
        gpu_util_avg = None
        gpu_mem_pct_avg = None
        gpu_mem_used_mib = None
        gpu_mem_total_mib = None

        if gpu_sensors:
            gpu_vals = [s["temp"] for s in gpu_sensors if s.get("temp") is not None]
            if gpu_vals:
                gpu_avg = sum(gpu_vals) / len(gpu_vals)

            util_vals = [s["gpu_util"] for s in gpu_sensors if s.get("gpu_util") is not None]
            if util_vals:
                gpu_util_avg = sum(util_vals) / len(util_vals)

            mem_pct_vals = [s["mem_pct"] for s in gpu_sensors if s.get("mem_pct") is not None]
            if mem_pct_vals:
                gpu_mem_pct_avg = sum(mem_pct_vals) / len(mem_pct_vals)

            used_vals = [s["mem_used_mib"] for s in gpu_sensors if s.get("mem_used_mib") is not None]
            total_vals = [s["mem_total_mib"] for s in gpu_sensors if s.get("mem_total_mib") is not None]
            if used_vals and total_vals:
                gpu_mem_used_mib = sum(used_vals)
                gpu_mem_total_mib = sum(total_vals)

        cpu_load_pct = self._read_cpu_usage_pct()
        loadavg_str = self._read_loadavg_str()
        ram_pct, ram_used_gib, ram_total_gib = self._read_ram_usage()

        return {
            "cpu_avg": cpu_avg,
            "cpu_load_pct": cpu_load_pct,
            "loadavg_str": loadavg_str,
            "ram_pct": ram_pct,
            "ram_used_gib": ram_used_gib,
            "ram_total_gib": ram_total_gib,
            "gpu_avg": gpu_avg,
            "gpu_util_avg": gpu_util_avg,
            "gpu_mem_pct_avg": gpu_mem_pct_avg,
            "gpu_mem_used_mib": gpu_mem_used_mib,
            "gpu_mem_total_mib": gpu_mem_total_mib,
            "cpu_sensors": cpu_sensors,
            "gpu_sensors": gpu_sensors,
            "other_sensors": other_sensors,
        }


def hex_to_rgb(hex_str, default=(1.0, 1.0, 1.0)):
    """Convert '#rrggbb' hex string to normalized (r, g, b) floats in 0.0..1.0."""
    if not hex_str or not isinstance(hex_str, str):
        return default
    s = hex_str.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
        return (r, g, b)
    except ValueError:
        return default


def rgb_luminance(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def temp_to_colors(temp, enabled=True, warn_c=55.0, crit_c=75.0):
    """
    Return (bg_rgb, border_rgb, text_rgb, accent_hex, level) for a given temperature in Celsius.
    level: 'off', 'normal', 'warn', or 'crit'
    """
    if not enabled or temp is None:
        return (
            (0.24, 0.26, 0.29),
            (0.45, 0.48, 0.52),
            (0.78, 0.80, 0.82),
            "#9aa0a6",
            "off",
        )
    if temp < warn_c:
        # Cool emerald/teal
        return (
            (0.11, 0.36, 0.27),
            (0.20, 0.78, 0.54),
            (0.95, 1.00, 0.97),
            "#34d399",
            "normal",
        )
    elif temp < crit_c:
        # Warm amber/orange
        return (
            (0.45, 0.28, 0.07),
            (0.96, 0.65, 0.14),
            (1.00, 0.98, 0.92),
            "#fbbf24",
            "warn",
        )
    else:
        # Hot crimson/red
        return (
            (0.48, 0.11, 0.14),
            (0.96, 0.28, 0.32),
            (1.00, 0.95, 0.95),
            "#f87171",
            "crit",
        )


def pct_to_colors(pct, enabled=True, base_hue="cyan", warn_pct=60.0, crit_pct=85.0):
    """
    Return (bg_rgb, border_rgb, text_rgb, accent_hex, level) for percentage metrics (0..100%).
    level: 'off', 'normal', 'warn', or 'crit'
    """
    if not enabled or pct is None:
        return (
            (0.24, 0.26, 0.29),
            (0.45, 0.48, 0.52),
            (0.78, 0.80, 0.82),
            "#9aa0a6",
            "off",
        )
    if pct < warn_pct:
        if base_hue == "purple":
            return (
                (0.22, 0.16, 0.42),
                (0.62, 0.48, 0.98),
                (0.96, 0.95, 1.00),
                "#a78bfa",
                "normal",
            )
        return (
            (0.10, 0.29, 0.42),
            (0.22, 0.74, 0.97),
            (0.94, 0.99, 1.00),
            "#38bdf8",
            "normal",
        )
    elif pct < crit_pct:
        return (
            (0.45, 0.28, 0.07),
            (0.96, 0.65, 0.14),
            (1.00, 0.98, 0.92),
            "#fbbf24",
            "warn",
        )
    else:
        return (
            (0.48, 0.11, 0.14),
            (0.96, 0.28, 0.32),
            (1.00, 0.95, 0.95),
            "#f87171",
            "crit",
        )


def make_ascii_bar(pct, width=10):
    if pct is None:
        return "░" * width
    filled = max(0, min(width, int(round((pct / 100.0) * width))))
    return ("█" * filled) + ("░" * (width - filled))


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
    """

    INTERVAL_PRESETS = [1, 2, 3, 5, 10, 30]
    # 3-stage color presets: (Name, normal_hex, warn_hex, crit_hex)
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
        self.sensor_mgr = HardwareSensorManager()
        self.latest_data = {
            "cpu_avg": None,
            "cpu_load_pct": None,
            "loadavg_str": None,
            "ram_pct": None,
            "ram_used_gib": None,
            "ram_total_gib": None,
            "gpu_avg": None,
            "gpu_util_avg": None,
            "gpu_mem_pct_avg": None,
            "gpu_mem_used_mib": None,
            "gpu_mem_total_mib": None,
            "cpu_sensors": [],
            "gpu_sensors": [],
            "other_sensors": [],
        }
        self.timer_id = None
        self.panel_size = 28
        self.prefs_win = None
        self.active_menu = None

        self.canvas = Gtk.DrawingArea()
        self.canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self.canvas.connect("draw", self._on_draw)
        self.add(self.canvas)

        self.connect("button-press-event", self._on_button_press)
        self.canvas.connect("button-press-event", self._on_button_press)
        self.connect("destroy", self._on_destroy)

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

    def _get_temp_colors(self, temp, enabled=True):
        return temp_to_colors(
            temp,
            enabled=enabled,
            warn_c=float(self.config.get("warn_temp_c", 55.0)),
            crit_c=float(self.config.get("crit_temp_c", 75.0)),
        )

    def _get_pct_colors(self, pct, enabled=True, base_hue="cyan"):
        return pct_to_colors(
            pct,
            enabled=enabled,
            base_hue=base_hue,
            warn_pct=float(self.config.get("warn_pct", 60.0)),
            crit_pct=float(self.config.get("crit_pct", 85.0)),
        )

    def _resolve_panel_text_rgb(self, level, default_rgb):
        """
        Return the actual (r, g, b) to use for panel text based on `font_color_mode` and severity `level`.
        level: 'normal', 'warn', 'crit', or 'off'.
        When font_color_mode == 'custom':
          - 'normal' -> custom_font_color
          - 'warn'   -> custom_warn_font_color
          - 'crit'   -> custom_crit_font_color
          - 'off'    -> default_rgb (gray)
        """
        if level == "off":
            return default_rgb
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
        """Draw text with optional contrast shadow/outline so it stays legible on any panel background."""
        if self.config.get("text_shadow", False):
            lum = rgb_luminance(rgb)
            if lum > 0.45:
                s_rgb = (0.0, 0.0, 0.0, 0.82)
            else:
                s_rgb = (1.0, 1.0, 1.0, 0.82)
            ctx.set_source_rgba(*s_rgb)
            for dx, dy in ((-0.8, 0.0), (0.8, 0.0), (0.0, -0.8), (0.0, 0.8)):
                ctx.move_to(x + dx, y + dy)
                ctx.show_text(text)

        ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], 1.0)
        ctx.move_to(x, y)
        ctx.show_text(text)

    def _format_mem_short(self, enabled=True):
        if not enabled:
            return "OFF"
        unit = self.config.get("gpu_mem_panel_unit", "percent")
        if unit == "gib":
            used = self.latest_data.get("gpu_mem_used_mib")
            if used is None:
                return "--G"
            return f"{used / 1024.0:.1f}G"
        else:
            pct = self.latest_data.get("gpu_mem_pct_avg")
            if pct is None:
                return "--%"
            return f"{int(round(pct))}%"

    def set_panel_size(self, size):
        self.panel_size = max(22, min(64, int(size)))
        self._update_canvas_size()

    def _get_badge_count(self):
        layout = self.config.get("gpu_badge_layout", "separate")
        if layout == "compact":
            return 2
        count = 2
        if self.config.get("show_panel_gpu_util", True):
            count += 1
        if self.config.get("show_panel_gpu_mem", True):
            count += 1
        return count

    def _update_canvas_size(self):
        h = max(24, self.panel_size)
        mode = self.config.get("display_mode", "badge")
        show_util = self.config.get("show_panel_gpu_util", True)
        show_mem = self.config.get("show_panel_gpu_mem", True)
        layout = self.config.get("gpu_badge_layout", "separate")

        if mode == "badge":
            n_badges = self._get_badge_count()
            width_factor = 1.28 if layout == "compact" else 1.18
            badge_w = max(32 if layout == "compact" else 30, int(h * width_factor))
            gap = 4
            total_w = badge_w * n_badges + gap * (n_badges - 1) + 4
        else:
            extra_slots = (1 if show_util else 0) + (1 if show_mem else 0)
            base_w = max(116, int(h * 4.2))
            extra_w = extra_slots * max(40, int(h * 1.45))
            total_w = base_w + extra_w
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
        cpu_load = self.latest_data.get("cpu_load_pct")
        loadavg = self.latest_data.get("loadavg_str")
        ram_pct = self.latest_data.get("ram_pct")
        ram_used = self.latest_data.get("ram_used_gib")
        ram_tot = self.latest_data.get("ram_total_gib")
        gpu_avg = self.latest_data.get("gpu_avg")
        gpu_util = self.latest_data.get("gpu_util_avg")
        mem_pct = self.latest_data.get("gpu_mem_pct_avg")
        mem_used = self.latest_data.get("gpu_mem_used_mib")
        mem_tot = self.latest_data.get("gpu_mem_total_mib")

        cpu_str = f"{cpu_avg:.1f}°C" if (enabled and cpu_avg is not None) else ("已關閉" if not enabled else "N/A")
        if enabled and cpu_load is not None:
            load_extra = f" (Load: {loadavg})" if loadavg else ""
            cpu_load_str = f"{cpu_load:.1f}%{load_extra}"
        else:
            cpu_load_str = "已關閉" if not enabled else "N/A"
        if enabled and ram_used is not None and ram_tot is not None and ram_pct is not None:
            ram_str = f"{ram_used:.1f} / {ram_tot:.1f} GiB ({ram_pct:.1f}%)"
        else:
            ram_str = "已關閉" if not enabled else "N/A"

        gpu_str = f"{gpu_avg:.1f}°C" if (enabled and gpu_avg is not None) else ("已關閉" if not enabled else "N/A")
        util_str = f"{gpu_util:.0f}%" if (enabled and gpu_util is not None) else ("已關閉" if not enabled else "N/A")
        if enabled and mem_used is not None and mem_tot is not None:
            mem_str = f"{mem_used:.0f} / {mem_tot:.0f} MiB ({mem_pct:.1f}%)"
        else:
            mem_str = "已關閉" if not enabled else "N/A"

        status_str = f"啟用中 (每 {interval} 秒更新)" if enabled else "已暫停監控"
        if not self.config.get("show_hover_tooltip", True):
            self.set_tooltip_markup(None)
            self.set_has_tooltip(False)
            return
        self.set_has_tooltip(True)
        self.set_tooltip_markup(
            f"<b>CPU / GPU 溫度與負載監控 Plus</b>\n"
            f"• CPU 平均溫度: <b>{cpu_str}</b>\n"
            f"• CPU 平均負載: <b>{cpu_load_str}</b>\n"
            f"• 系統記憶體 (RAM): <b>{ram_str}</b>\n"
            f"• GPU 平均溫度: <b>{gpu_str}</b>\n"
            f"• Volatile GPU-Util: <b>{util_str}</b>\n"
            f"• GPU Memory-Usage: <b>{mem_str}</b>\n"
            f"<small>{status_str} — 左鍵點選查看所有監控單元 | 右鍵開啟偏好設定</small>"
        )

    def _on_draw(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        enabled = self.config.get("enabled", True)
        mode = self.config.get("display_mode", "badge")

        if mode == "badge":
            self._draw_badge_mode(ctx, w, h, enabled)
        else:
            self._draw_icon_text_mode(ctx, w, h, enabled)
        return False

    def _draw_generic_badge(
        self,
        ctx,
        x,
        y,
        bw,
        bh,
        title,
        val_str,
        colors,
        side_bars=None,
        hide_title=False,
        custom_style=None,
    ):
        """
        Draw a single rounded badge with optional title (or Scheme B ultra-large centered value),
        custom per-badge background/border/opacity, and optional vertical side progress bars.
        colors: (bg_rgb, border_rgb, text_rgb, accent_hex, level)
        custom_style: optional tuple (bg_hex, border_hex, opacity_0_100)
        """
        bg_rgb, border_rgb, text_rgb, _, level = colors
        bg_style = self.config.get("badge_bg_style", "dark")

        draw_rounded_rect(ctx, x + 0.5, y + 0.5, bw - 1.0, bh - 1.0, 4.5)
        eff_bg_rgb = bg_rgb
        eff_bg_alpha = 0.92
        eff_border_rgb = border_rgb

        if custom_style is not None:
            c_bg_hex, c_border_hex, c_opacity = custom_style
            eff_bg_rgb = hex_to_rgb(c_bg_hex, default=bg_rgb)
            eff_border_rgb = hex_to_rgb(c_border_hex, default=border_rgb)
            eff_bg_alpha = max(0.0, min(1.0, float(c_opacity) / 100.0))
            if eff_bg_alpha > 0.0:
                ctx.set_source_rgba(eff_bg_rgb[0], eff_bg_rgb[1], eff_bg_rgb[2], eff_bg_alpha)
                ctx.fill_preserve()
        elif bg_style == "dark":
            ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], 0.92)
            ctx.fill_preserve()
        elif bg_style == "light":
            l_r = min(1.0, 0.88 + bg_rgb[0] * 0.22)
            l_g = min(1.0, 0.88 + bg_rgb[1] * 0.22)
            l_b = min(1.0, 0.88 + bg_rgb[2] * 0.22)
            eff_bg_rgb = (l_r, l_g, l_b)
            ctx.set_source_rgba(l_r, l_g, l_b, 0.92)
            ctx.fill_preserve()
        else:
            eff_bg_alpha = 0.0

        if not self.config.get("hide_panel_border", False):
            ctx.set_source_rgba(eff_border_rgb[0], eff_border_rgb[1], eff_border_rgb[2], 0.95)
            ctx.set_line_width(1.25)
            ctx.stroke()
        else:
            ctx.new_path()

        # Draw vertical side progress bars inside left and right edges (bottom-up fill)
        has_left = bool(side_bars and side_bars.get("left") is not None)
        has_right = bool(side_bars and side_bars.get("right") is not None)
        bar_w = max(2.4, min(3.4, bw * 0.095))
        bar_pad_y = 3.2
        bar_h = max(6.0, bh - bar_pad_y * 2.0)
        bar_y = y + bar_pad_y

        if has_left:
            l_pct, l_rgb = side_bars["left"]
            lx = x + 2.5
            ctx.set_source_rgba(0.15, 0.18, 0.22, 0.72)
            ctx.rectangle(lx, bar_y, bar_w, bar_h)
            ctx.fill()
            if l_pct is not None and l_pct > 0:
                fill_h = max(1.5, min(bar_h, bar_h * (l_pct / 100.0)))
                ctx.set_source_rgba(l_rgb[0], l_rgb[1], l_rgb[2], 0.98)
                ctx.rectangle(lx, bar_y + bar_h - fill_h, bar_w, fill_h)
                ctx.fill()

        if has_right:
            r_pct, r_rgb = side_bars["right"]
            rx = x + bw - 2.5 - bar_w
            ctx.set_source_rgba(0.15, 0.18, 0.22, 0.72)
            ctx.rectangle(rx, bar_y, bar_w, bar_h)
            ctx.fill()
            if r_pct is not None and r_pct > 0:
                fill_h = max(1.5, min(bar_h, bar_h * (r_pct / 100.0)))
                ctx.set_source_rgba(r_rgb[0], r_rgb[1], r_rgb[2], 0.98)
                ctx.rectangle(rx, bar_y + bar_h - fill_h, bar_w, fill_h)
                ctx.fill()

        # Resolve title & value font colors according to 3-stage severity level ('normal', 'warn', 'crit')
        if self.config.get("font_color_mode", "dynamic") == "custom":
            stage_rgb = self._resolve_panel_text_rgb(level, text_rgb)
            title_rgb = stage_rgb
            val_rgb = stage_rgb
        else:
            title_rgb = eff_border_rgb
            is_light_fill = eff_bg_alpha >= 0.30 and rgb_luminance(eff_bg_rgb) > 0.55
            if is_light_fill:
                val_rgb = (0.07, 0.09, 0.15)
            else:
                val_rgb = text_rgb

        has_sides = has_left or has_right
        # Center text within the inner area between left and right vertical bars
        inner_left = x + (bar_w + 3.0 if has_left else 1.5)
        inner_right = x + bw - (bar_w + 3.0 if has_right else 1.5)
        inner_w = max(10.0, inner_right - inner_left)

        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        if hide_title:
            # Large centered temperature with ° symbol (no CPU/GPU header) + dynamic width clamp
            val_size = max(10.0, bh * 0.64)
            ctx.set_font_size(val_size)
            v_ext = ctx.text_extents(val_str)
            max_w = max(8.0, inner_w - 2.5)
            if v_ext.width > max_w and v_ext.width > 0:
                val_size *= max_w / v_ext.width
                ctx.set_font_size(val_size)
                v_ext = ctx.text_extents(val_str)
            vx = inner_left + (inner_w - v_ext.width) / 2.0 - v_ext.x_bearing
            vy = y + (bh - v_ext.height) / 2.0 - v_ext.y_bearing
            self._draw_styled_text(ctx, vx, vy, val_str, val_rgb)
        else:
            header_size = max(6.5, bh * (0.24 if has_sides else 0.27))
            ctx.set_font_size(header_size)
            ext = ctx.text_extents(title)
            tx = inner_left + (inner_w - ext.width) / 2.0 - ext.x_bearing
            ty = y + header_size + 1.5
            self._draw_styled_text(ctx, tx, ty, title, title_rgb)

            val_size = max(8.5, bh * (0.37 if has_sides else 0.41))
            if len(val_str) >= 4:
                val_size *= 0.88
            ctx.set_font_size(val_size)
            v_ext = ctx.text_extents(val_str)
            vx = inner_left + (inner_w - v_ext.width) / 2.0 - v_ext.x_bearing
            vy = y + bh - 3.0
            self._draw_styled_text(ctx, vx, vy, val_str, val_rgb)

    def _draw_badge_mode(self, ctx, w, h, enabled):
        cpu_avg = self.latest_data.get("cpu_avg") if enabled else None
        cpu_load = self.latest_data.get("cpu_load_pct") if enabled else None
        ram_pct = self.latest_data.get("ram_pct") if enabled else None
        gpu_avg = self.latest_data.get("gpu_avg") if enabled else None
        gpu_util = self.latest_data.get("gpu_util_avg") if enabled else None
        mem_pct = self.latest_data.get("gpu_mem_pct_avg") if enabled else None

        show_util = self.config.get("show_panel_gpu_util", True)
        show_mem = self.config.get("show_panel_gpu_mem", True)
        layout = self.config.get("gpu_badge_layout", "separate")
        use_custom_style = bool(self.config.get("custom_badge_style", True))

        badges = []

        # Large centered temperature with ° symbol (CPU/GPU titles hidden, distinguished by badge colors)
        cpu_val_str = "OFF" if not enabled else ("--°" if cpu_avg is None else f"{int(round(cpu_avg))}°")
        cpu_side_bars = None
        if layout == "compact":
            _, cl_border, _, _, cl_lvl = self._get_pct_colors(cpu_load, enabled, base_hue="cyan")
            cl_bar_rgb = self._resolve_panel_text_rgb(cl_lvl, cl_border)
            _, rm_border, _, _, rm_lvl = self._get_pct_colors(ram_pct, enabled, base_hue="purple")
            rm_bar_rgb = self._resolve_panel_text_rgb(rm_lvl, rm_border)
            cpu_side_bars = {
                "left": (cpu_load if enabled else 0.0, cl_bar_rgb),
                "right": (ram_pct if enabled else 0.0, rm_bar_rgb),
            }
        cpu_custom = (
            (
                self.config.get("cpu_bg_color", "#113a2b"),
                self.config.get("cpu_border_color", "#34d399"),
                int(self.config.get("cpu_bg_opacity", 92)),
            )
            if use_custom_style
            else None
        )
        badges.append(("CPU", cpu_val_str, self._get_temp_colors(cpu_avg, enabled), cpu_side_bars, True, cpu_custom))

        gpu_val_str = "OFF" if not enabled else ("--°" if gpu_avg is None else f"{int(round(gpu_avg))}°")
        gpu_side_bars = None
        if layout == "compact":
            _, u_border, _, _, u_lvl = self._get_pct_colors(gpu_util, enabled, base_hue="cyan")
            u_bar_rgb = self._resolve_panel_text_rgb(u_lvl, u_border)
            _, m_border, _, _, m_lvl = self._get_pct_colors(mem_pct, enabled, base_hue="purple")
            m_bar_rgb = self._resolve_panel_text_rgb(m_lvl, m_border)
            gpu_side_bars = {
                "left": (gpu_util if enabled else 0.0, u_bar_rgb),
                "right": (mem_pct if enabled else 0.0, m_bar_rgb),
            }
        gpu_custom = (
            (
                self.config.get("gpu_bg_color", "#162944"),
                self.config.get("gpu_border_color", "#38bdf8"),
                int(self.config.get("gpu_bg_opacity", 92)),
            )
            if use_custom_style
            else None
        )
        badges.append(("GPU", gpu_val_str, self._get_temp_colors(gpu_avg, enabled), gpu_side_bars, True, gpu_custom))

        if layout == "separate":
            if show_util:
                util_str = "OFF" if not enabled else ("--%" if gpu_util is None else f"{int(round(gpu_util))}%")
                badges.append(("UTL", util_str, self._get_pct_colors(gpu_util, enabled, base_hue="cyan"), None, False, None))
            if show_mem:
                mem_str = self._format_mem_short(enabled)
                badges.append(("MEM", mem_str, self._get_pct_colors(mem_pct, enabled, base_hue="purple"), None, False, None))

        n = len(badges)
        pad_y = 1.0
        bh = max(20.0, h - pad_y * 2)
        gap = 4.0
        bw = (w - gap * (n - 1) - 2.0) / float(n)

        cur_x = 1.0
        for title, val_str, colors, sbars, hide_t, c_style in badges:
            self._draw_generic_badge(
                ctx,
                cur_x,
                pad_y,
                bw,
                bh,
                title,
                val_str,
                colors,
                side_bars=sbars,
                hide_title=hide_t,
                custom_style=c_style,
            )
            cur_x += bw + gap

    def _draw_mini_chip_icon(self, ctx, x, y, size, label_char, border_rgb, bg_rgb):
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 0.9)
        ctx.set_line_width(1.2)
        for p in (0.25, 0.5, 0.75):
            px = x + size * p
            py = y + size * p
            ctx.move_to(px, y - 1.5)
            ctx.line_to(px, y + 1.5)
            ctx.move_to(px, y + size - 1.5)
            ctx.line_to(px, y + size + 1.5)
            ctx.move_to(x - 1.5, py)
            ctx.line_to(x + 1.5, py)
            ctx.move_to(x + size - 1.5, py)
            ctx.line_to(x + size + 1.5, py)
        ctx.stroke()

        draw_rounded_rect(ctx, x + 1, y + 1, size - 2, size - 2, 3.0)
        ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], 0.95)
        ctx.fill_preserve()
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 1.0)
        ctx.set_line_width(1.2)
        ctx.stroke()

        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(size * 0.56)
        ext = ctx.text_extents(label_char)
        ctx.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        ctx.move_to(
            x + (size - ext.width) / 2.0 - ext.x_bearing,
            y + (size + ext.height) / 2.0,
        )
        ctx.show_text(label_char)

    def _draw_icon_text_mode(self, ctx, w, h, enabled):
        cpu_avg = self.latest_data.get("cpu_avg") if enabled else None
        gpu_avg = self.latest_data.get("gpu_avg") if enabled else None
        gpu_util = self.latest_data.get("gpu_util_avg") if enabled else None
        mem_pct = self.latest_data.get("gpu_mem_pct_avg") if enabled else None

        show_util = self.config.get("show_panel_gpu_util", True)
        show_mem = self.config.get("show_panel_gpu_mem", True)

        icon_size = min(18.0, max(14.0, h - 8.0))
        icon_y = (h - icon_size) / 2.0
        font_sz = min(12.5, max(9.5, h * 0.42))
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(font_sz)

        # 1. Draw CPU Icon + Temp
        cur_x = 3.0
        c_bg, c_border, _, _, c_level = self._get_temp_colors(cpu_avg, enabled)
        self._draw_mini_chip_icon(ctx, cur_x, icon_y, icon_size, "C", c_border, c_bg)
        cur_x += icon_size + 5.0

        c_str = "OFF" if not enabled else ("--°C" if cpu_avg is None else f"{cpu_avg:.0f}°C")
        c_ext = ctx.text_extents(c_str)
        c_text_rgb = self._resolve_panel_text_rgb(c_level, c_border)
        self._draw_styled_text(ctx, cur_x, (h + c_ext.height) / 2.0, c_str, c_text_rgb)
        cur_x += c_ext.width + 10.0

        # 2. Draw GPU Icon + Temp
        g_bg, g_border, _, _, g_level = self._get_temp_colors(gpu_avg, enabled)
        self._draw_mini_chip_icon(ctx, cur_x, icon_y, icon_size, "G", g_border, g_bg)
        cur_x += icon_size + 5.0

        g_str = "OFF" if not enabled else ("--°C" if gpu_avg is None else f"{gpu_avg:.0f}°C")
        g_ext = ctx.text_extents(g_str)
        g_text_rgb = self._resolve_panel_text_rgb(g_level, g_border)
        self._draw_styled_text(ctx, cur_x, (h + g_ext.height) / 2.0, g_str, g_text_rgb)
        cur_x += g_ext.width + 6.0

        # 3. Optional GPU-Util text
        if show_util and enabled:
            _, u_border, _, _, u_level = self._get_pct_colors(gpu_util, enabled, base_hue="cyan")
            u_str = "--%" if gpu_util is None else f"{gpu_util:.0f}%"
            u_ext = ctx.text_extents(u_str)
            u_text_rgb = self._resolve_panel_text_rgb(u_level, u_border)
            self._draw_styled_text(ctx, cur_x, (h + u_ext.height) / 2.0, u_str, u_text_rgb)
            cur_x += u_ext.width + 6.0

        # 4. Optional GPU Memory-Usage text
        if show_mem and enabled:
            _, m_border, _, _, m_level = self._get_pct_colors(mem_pct, enabled, base_hue="purple")
            m_str = self._format_mem_short(enabled)
            m_ext = ctx.text_extents(m_str)
            m_text_rgb = self._resolve_panel_text_rgb(m_level, m_border)
            self._draw_styled_text(ctx, cur_x, (h + m_ext.height) / 2.0, m_str, m_text_rgb)

    def handle_applet_button_press(self, widget, event):
        if event.button == 1:
            GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.popup_sensor_menu(event)
            return True
        return False

    def _on_button_press(self, widget, event):
        if event.button == 1 or (event.button == 3 and self.applet is None):
            if self.applet is not None:
                GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.popup_sensor_menu(event)
            return True
        return False

    def popup_sensor_menu(self, event=None):
        self.refresh_readings(force_poll=self.config.get("enabled", True))
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
        enabled = self.config.get("enabled", True)
        cpu_avg = self.latest_data.get("cpu_avg")
        gpu_avg = self.latest_data.get("gpu_avg")
        gpu_util = self.latest_data.get("gpu_util_avg")
        mem_pct = self.latest_data.get("gpu_mem_pct_avg")

        # 1. Header summary
        _, _, _, cpu_hex, _ = self._get_temp_colors(cpu_avg, enabled)
        _, _, _, gpu_hex, _ = self._get_temp_colors(gpu_avg, enabled)
        _, _, _, util_hex, _ = self._get_pct_colors(gpu_util, enabled, base_hue="cyan")
        _, _, _, mem_hex, _ = self._get_pct_colors(mem_pct, enabled, base_hue="purple")

        cpu_avg_txt = f"{cpu_avg:.1f}°C" if (enabled and cpu_avg is not None) else ("已暫停" if not enabled else "N/A")
        gpu_avg_txt = f"{gpu_avg:.1f}°C" if (enabled and gpu_avg is not None) else ("已暫停" if not enabled else "N/A")
        util_avg_txt = f"{gpu_util:.0f}%" if (enabled and gpu_util is not None) else "--%"
        mem_avg_txt = f"{mem_pct:.1f}%" if (enabled and mem_pct is not None) else "--%"

        header_item = self._create_markup_menu_item(
            f"<b>🌡 硬體溫度與 GPU 負載總覽</b>   "
            f"<span foreground='{cpu_hex}'><b>CPU: {cpu_avg_txt}</b></span>  |  "
            f"<span foreground='{gpu_hex}'><b>GPU: {gpu_avg_txt}</b></span>  "
            f"(<span foreground='{util_hex}'><b>Util: {util_avg_txt}</b></span> · "
            f"<span foreground='{mem_hex}'><b>Mem: {mem_avg_txt}</b></span>)",
            sensitive=False,
        )
        menu.append(header_item)
        menu.append(Gtk.SeparatorMenuItem())

        # 2. CPU Sensor Units & System Load/RAM Section
        cpu_title = self._create_markup_menu_item(
            f"<b>🖥 CPU 與系統資源監控 (平均溫度: {cpu_avg_txt})</b>", sensitive=False
        )
        menu.append(cpu_title)

        cpu_load = self.latest_data.get("cpu_load_pct")
        loadavg = self.latest_data.get("loadavg_str")
        if cpu_load is not None:
            _, _, _, cl_col, _ = self._get_pct_colors(cpu_load, True, base_hue="cyan")
            cl_bar = make_ascii_bar(cpu_load, 10)
            la_str = f" <small><span foreground='#888888'>(LoadAvg: {loadavg})</span></small>" if loadavg else ""
            menu.append(
                self._create_markup_menu_item(
                    f"   ├─ <tt>CPU 平均負載 (Load) :</tt> <tt><span foreground='{cl_col}'>[{cl_bar}]</span></tt> "
                    f"<span foreground='{cl_col}'><b>{cpu_load:.1f}%</b></span>{la_str}",
                    sensitive=True,
                )
            )

        ram_pct = self.latest_data.get("ram_pct")
        ram_used = self.latest_data.get("ram_used_gib")
        ram_tot = self.latest_data.get("ram_total_gib")
        if ram_pct is not None and ram_used is not None and ram_tot is not None:
            _, _, _, rm_col, _ = self._get_pct_colors(ram_pct, True, base_hue="purple")
            rm_bar = make_ascii_bar(ram_pct, 10)
            menu.append(
                self._create_markup_menu_item(
                    f"   └─ <tt>系統記憶體 (RAM)    :</tt> <tt><span foreground='{rm_col}'>[{rm_bar}]</span></tt> "
                    f"<span foreground='{rm_col}'><b>{ram_used:.1f} GiB / {ram_tot:.1f} GiB ({ram_pct:.1f}%)</b></span>",
                    sensitive=True,
                )
            )

        cpu_sensors = self.latest_data.get("cpu_sensors", [])
        if not cpu_sensors:
            menu.append(self._create_markup_menu_item("   <i>無感測資料</i>", sensitive=False))
        else:
            for s in cpu_sensors:
                _, _, _, col, _ = self._get_temp_colors(s["temp"], True)
                extra = ""
                if s.get("high") and s.get("crit"):
                    extra = f" <small><span foreground='#888888'>(High: {s['high']:.0f}°C / Crit: {s['crit']:.0f}°C)</span></small>"
                row = self._create_markup_menu_item(
                    f"   • <tt>{s['label']:<14}</tt> <span foreground='{col}'><b>{s['temp']:.1f}°C</b></span>{extra}",
                    sensitive=True,
                )
                menu.append(row)

        menu.append(Gtk.SeparatorMenuItem())

        # 3. GPU Sensor Units Section
        gpu_title = self._create_markup_menu_item(
            f"<b>🎮 GPU 監控單元 (平均溫度: {gpu_avg_txt})</b>", sensitive=False
        )
        menu.append(gpu_title)

        gpu_sensors = self.latest_data.get("gpu_sensors", [])
        if not gpu_sensors:
            menu.append(self._create_markup_menu_item("   <i>未偵測到獨立或內建 GPU 感測器</i>", sensitive=False))
        else:
            for s in gpu_sensors:
                _, _, _, t_col, _ = self._get_temp_colors(s["temp"], True)
                extra_t = ""
                if s.get("high") and s.get("crit"):
                    extra_t = f" <small><span foreground='#888888'>(High: {s['high']:.0f}°C / Crit: {s['crit']:.0f}°C)</span></small>"
                menu.append(
                    self._create_markup_menu_item(
                        f"   • <tt><b>{s['label']}</b></tt>   <span foreground='{t_col}'><b>{s['temp']:.1f}°C</b></span>{extra_t}",
                        sensitive=True,
                    )
                )

                g_u = s.get("gpu_util")
                if g_u is not None:
                    _, _, _, u_col, _ = self._get_pct_colors(g_u, True, base_hue="cyan")
                    u_bar = make_ascii_bar(g_u, 10)
                    mc_u = s.get("mem_ctrl_util")
                    mc_str = f" <small><span foreground='#888888'>(MemCtrl: {mc_u:.0f}%)</span></small>" if mc_u is not None else ""
                    menu.append(
                        self._create_markup_menu_item(
                            f"      ├─ <tt>Volatile GPU-Util :</tt> <tt><span foreground='{u_col}'>[{u_bar}]</span></tt> "
                            f"<span foreground='{u_col}'><b>{g_u:.0f}%</b></span>{mc_str}",
                            sensitive=True,
                        )
                    )

                m_used = s.get("mem_used_mib")
                m_tot = s.get("mem_total_mib")
                m_p = s.get("mem_pct")
                if m_used is not None and m_tot is not None and m_p is not None:
                    _, _, _, m_col, _ = self._get_pct_colors(m_p, True, base_hue="purple")
                    m_bar = make_ascii_bar(m_p, 10)
                    menu.append(
                        self._create_markup_menu_item(
                            f"      ├─ <tt>Memory-Usage      :</tt> <tt><span foreground='{m_col}'>[{m_bar}]</span></tt> "
                            f"<span foreground='{m_col}'><b>{m_used:.0f} MiB / {m_tot:.0f} MiB ({m_p:.1f}%)</b></span>",
                            sensitive=True,
                        )
                    )

                fan_p = s.get("fan_pct")
                pwr_w = s.get("power_w")
                if fan_p is not None or pwr_w is not None:
                    fan_txt = f"<b>{fan_p:.0f}%</b>" if fan_p is not None else "N/A"
                    pwr_txt = f"<b>{pwr_w:.1f} W</b>" if pwr_w is not None else "N/A"
                    menu.append(
                        self._create_markup_menu_item(
                            f"      └─ <tt>Fan Speed / Power :</tt> 🌀 風扇 {fan_txt}   |   ⚡ 功耗 {pwr_txt}",
                            sensitive=True,
                        )
                    )

        # 4. Optional Auxiliary Hardware Sensors
        if self.config.get("show_other_sensors", True):
            other_sensors = self.latest_data.get("other_sensors", [])
            if other_sensors:
                menu.append(Gtk.SeparatorMenuItem())
                other_title = self._create_markup_menu_item(
                    "<b>💾 其他系統感測器 (NVMe / 主機板 / 風扇)</b>", sensitive=False
                )
                menu.append(other_title)
                for s in other_sensors:
                    t_val = s.get("temp")
                    if t_val is not None:
                        _, _, _, col, _ = self._get_temp_colors(t_val, True)
                        v_markup = f"<span foreground='{col}'><b>{s['value_str']}</b></span>"
                    else:
                        v_markup = f"<b>{s['value_str']}</b>"
                    row = self._create_markup_menu_item(
                        f"   • <tt>{s['label']:<22}</tt> {v_markup}",
                        sensitive=True,
                    )
                    menu.append(row)

        menu.append(Gtk.SeparatorMenuItem())

        # 5. Control Options
        toggle_mon = Gtk.CheckMenuItem(label="啟用自動監控 (關閉時停止背景輪詢)")
        toggle_mon.set_active(enabled)
        toggle_mon.connect("toggled", self._on_toggle_monitoring)
        menu.append(toggle_mon)

        refresh_now = Gtk.MenuItem(label="🔄 立即手動更新一次數值")
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
        custom_int_item.connect("activate", lambda *_: self.show_preferences_dialog())
        freq_sub.append(custom_int_item)
        freq_item.set_submenu(freq_sub)
        menu.append(freq_item)

        # 7. Submenu: Panel Icon Display Style & GPU Metrics Layout
        style_item = Gtk.MenuItem(label="🎨 面板圖示與 GPU 指標樣式")
        style_sub = Gtk.Menu()
        cur_mode = self.config.get("display_mode", "badge")

        mode_badge = Gtk.RadioMenuItem.new_with_label_from_widget(
            None, "模式 A：動態方塊圖示 (Badge Mode)"
        )
        mode_icon_text = Gtk.RadioMenuItem.new_with_label_from_widget(
            mode_badge, "模式 B：晶片小圖示 + 文字並排 ([C] 39°C [G] 41°C ...)"
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

        cur_layout = self.config.get("gpu_badge_layout", "separate")
        layout_sep = Gtk.RadioMenuItem.new_with_label_from_widget(
            None, "方塊佈局 1：獨立方塊 ([CPU] [GPU] [UTL] [MEM])"
        )
        layout_comp = Gtk.RadioMenuItem.new_with_label_from_widget(
            layout_sep, "方塊佈局 2：緊湊二合一 ([CPU 左右內嵌 Load/RAM] [GPU 左右內嵌 Util/VRAM])"
        )
        if cur_layout == "separate":
            layout_sep.set_active(True)
        else:
            layout_comp.set_active(True)
        layout_sep.connect("toggled", self._on_gpu_layout_toggled, "separate")
        layout_comp.connect("toggled", self._on_gpu_layout_toggled, "compact")
        style_sub.append(layout_sep)
        style_sub.append(layout_comp)

        style_sub.append(Gtk.SeparatorMenuItem())

        chk_panel_util = Gtk.CheckMenuItem(label="在面板顯示 GPU 使用率 (Volatile GPU-Util)")
        chk_panel_util.set_active(self.config.get("show_panel_gpu_util", True))
        chk_panel_util.connect("toggled", self._on_toggle_panel_gpu_util)
        style_sub.append(chk_panel_util)

        chk_panel_mem = Gtk.CheckMenuItem(label="在面板顯示 GPU 顯存使用量 (Memory-Usage)")
        chk_panel_mem.set_active(self.config.get("show_panel_gpu_mem", True))
        chk_panel_mem.connect("toggled", self._on_toggle_panel_gpu_mem)
        style_sub.append(chk_panel_mem)

        cur_unit = self.config.get("gpu_mem_panel_unit", "percent")
        unit_pct = Gtk.RadioMenuItem.new_with_label_from_widget(
            None, "面板顯存單位：百分比 (例如 26%)"
        )
        unit_gib = Gtk.RadioMenuItem.new_with_label_from_widget(
            unit_pct, "面板顯存單位：GiB 容量 (例如 1.1G)"
        )
        if cur_unit == "percent":
            unit_pct.set_active(True)
        else:
            unit_gib.set_active(True)
        unit_pct.connect("toggled", self._on_mem_unit_toggled, "percent")
        unit_gib.connect("toggled", self._on_mem_unit_toggled, "gib")
        style_sub.append(unit_pct)
        style_sub.append(unit_gib)

        style_item.set_submenu(style_sub)
        menu.append(style_item)

        # 8. Submenu: 3-Stage Font Colors & Panel Background Adaptation
        color_item = Gtk.MenuItem(label="🖌 面板三段字形顏色與底色適應")
        color_sub = Gtk.Menu()

        f_mode = self.config.get("font_color_mode", "dynamic")
        rm_dyn = Gtk.RadioMenuItem.new_with_label_from_widget(
            None, "預設內建動態字形色階"
        )
        c_norm = self.config.get("custom_font_color", "#ffffff")
        c_warn = self.config.get("custom_warn_font_color", "#fbbf24")
        c_crit = self.config.get("custom_crit_font_color", "#f87171")
        rm_cus = Gtk.RadioMenuItem.new_with_label_from_widget(
            rm_dyn, f"自訂三段式字形顏色 (正常:{c_norm} / 中度:{c_warn} / 警戒:{c_crit})"
        )
        if f_mode == "dynamic":
            rm_dyn.set_active(True)
        else:
            rm_cus.set_active(True)
        rm_dyn.connect("toggled", self._on_font_color_mode_toggled, "dynamic")
        rm_cus.connect("toggled", self._on_font_color_mode_toggled, "custom")
        color_sub.append(rm_dyn)
        color_sub.append(rm_cus)

        color_sub.append(Gtk.SeparatorMenuItem())
        for label_txt, hx_n, hx_w, hx_c in self.FONT_THEME_PRESETS:
            p_item = self._create_markup_menu_item(
                f"套用色票：<span foreground='{hx_n}'><b>■</b></span> "
                f"<span foreground='{hx_w}'><b>■</b></span> "
                f"<span foreground='{hx_c}'><b>■</b></span>  {label_txt}",
                sensitive=True,
                callback=lambda _w, n=hx_n, w=hx_w, c=hx_c: self._apply_preset_font_theme(n, w, c),
            )
            color_sub.append(p_item)

        color_sub.append(Gtk.SeparatorMenuItem())
        bg_style = self.config.get("badge_bg_style", "dark")
        bg_dark = Gtk.RadioMenuItem.new_with_label_from_widget(None, "方塊底色：深色半透明底 (預設)")
        bg_light = Gtk.RadioMenuItem.new_with_label_from_widget(bg_dark, "方塊底色：淺色半透明底 (適合淺色面板)")
        bg_trans = Gtk.RadioMenuItem.new_with_label_from_widget(bg_dark, "方塊底色：完全透明無底色 (僅保留邊框與文字)")
        if bg_style == "dark":
            bg_dark.set_active(True)
        elif bg_style == "light":
            bg_light.set_active(True)
        else:
            bg_trans.set_active(True)
        bg_dark.connect("toggled", self._on_badge_bg_toggled, "dark")
        bg_light.connect("toggled", self._on_badge_bg_toggled, "light")
        bg_trans.connect("toggled", self._on_badge_bg_toggled, "transparent")
        color_sub.append(bg_dark)
        color_sub.append(bg_light)
        color_sub.append(bg_trans)

        color_sub.append(Gtk.SeparatorMenuItem())
        chk_shadow = Gtk.CheckMenuItem(label="啟用文字高對比描邊陰影 (透明面板推薦)")
        chk_shadow.set_active(self.config.get("text_shadow", False))
        chk_shadow.connect("toggled", self._on_toggle_text_shadow)
        color_sub.append(chk_shadow)

        pick_color_item = Gtk.MenuItem(label="🎨 開啟偏好設定自訂三段調色盤...")
        pick_color_item.connect("activate", lambda *_: self.show_preferences_dialog())
        color_sub.append(pick_color_item)

        color_item.set_submenu(color_sub)
        menu.append(color_item)

        # 9. Direct Preferences Dialog Item
        menu.append(Gtk.SeparatorMenuItem())
        prefs_item = Gtk.MenuItem(label="⚙️ 偏好設定 (Preferences)...")
        prefs_item.connect("activate", lambda *_: self.show_preferences_dialog())
        menu.append(prefs_item)

        menu.show_all()
        return menu

    def show_preferences_dialog(self, *_args):
        """Show a comprehensive Preferences window (also accessible from MATE Panel right-click menu)."""
        if self.prefs_win is not None:
            self.prefs_win.present()
            return

        win = Gtk.Window(title="CPU/GPU 溫度與負載監控 Plus — 偏好設定")
        win.set_default_size(560, 440)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_resizable(True)
        self.prefs_win = win

        def on_win_destroy(*_):
            self.prefs_win = None

        win.connect("destroy", on_win_destroy)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(14)
        outer.set_margin_end(14)

        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 0)

        def _make_tab_box(title):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(14)
            box.set_margin_end(14)
            notebook.append_page(box, Gtk.Label(label=title))
            return box

        tab_mon = _make_tab_box("⚙️ 監控與閾值")
        tab_disp = _make_tab_box("📊 面板顯示")
        tab_style = _make_tab_box("🎨 外觀與顏色")

        # Section 1: Monitoring Status & Frequency (Tab 1)
        sec1_lbl = Gtk.Label(xalign=0.0)
        sec1_lbl.set_markup("<b>1. 監控開關與時間頻率</b>")
        tab_mon.pack_start(sec1_lbl, False, False, 0)

        chk_enable = Gtk.CheckButton(label="啟用自動監控 (關閉時完全停止背景輪詢)")
        chk_enable.set_active(self.config.get("enabled", True))
        tab_mon.pack_start(chk_enable, False, False, 0)

        freq_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        freq_lbl = Gtk.Label(label="監控更新間隔（秒）：", xalign=0.0)
        freq_adj = Gtk.Adjustment(
            value=float(self.config.get("interval_sec", 2)),
            lower=1.0,
            upper=3600.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin_freq = Gtk.SpinButton(adjustment=freq_adj, climb_rate=1.0, digits=0)
        freq_box.pack_start(freq_lbl, True, True, 0)
        freq_box.pack_start(spin_freq, False, False, 0)
        tab_mon.pack_start(freq_box, False, False, 0)

        tab_mon.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Section 2: Panel Icon Appearance (Tab 2)
        sec2_lbl = Gtk.Label(xalign=0.0)
        sec2_lbl.set_markup("<b>1. 面板圖示顯示樣式</b>")
        tab_disp.pack_start(sec2_lbl, False, False, 0)

        rb_badge = Gtk.RadioButton.new_with_label_from_widget(
            None, "模式 A：動態方塊圖示 (Badge Mode)"
        )
        rb_icontext = Gtk.RadioButton.new_with_label_from_widget(
            rb_badge, "模式 B：晶片小圖示 + 文字並排 ([C] 39°C  [G] 41°C ...)"
        )
        if self.config.get("display_mode", "badge") == "badge":
            rb_badge.set_active(True)
        else:
            rb_icontext.set_active(True)
        tab_disp.pack_start(rb_badge, False, False, 0)
        tab_disp.pack_start(rb_icontext, False, False, 0)

        tab_disp.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Section 3: NVIDIA / GPU Utilization & Memory-Usage Panel Options (Tab 2)
        sec_gpu_lbl = Gtk.Label(xalign=0.0)
        sec_gpu_lbl.set_markup("<b>2. GPU 使用率 (GPU-Util) 與顯存 (Memory-Usage) 面板呈現</b>")
        tab_disp.pack_start(sec_gpu_lbl, False, False, 0)

        chk_gpu_util = Gtk.CheckButton(label="在面板上顯示 Volatile GPU-Util (GPU 核心使用率 %)")
        chk_gpu_util.set_active(self.config.get("show_panel_gpu_util", True))
        tab_disp.pack_start(chk_gpu_util, False, False, 0)

        chk_gpu_mem = Gtk.CheckButton(label="在面板上顯示 Memory-Usage (GPU 顯存使用量)")
        chk_gpu_mem.set_active(self.config.get("show_panel_gpu_mem", True))
        tab_disp.pack_start(chk_gpu_mem, False, False, 0)

        layout_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        layout_box.set_margin_start(12)
        rb_layout_sep = Gtk.RadioButton.new_with_label_from_widget(
            None, "方塊模式佈局 1：獨立方塊 ([CPU 39°] [GPU 41°] [UTL 34%] [MEM 26%])"
        )
        rb_layout_comp = Gtk.RadioButton.new_with_label_from_widget(
            rb_layout_sep, "方塊模式佈局 2：緊湊二合一 (CPU: 左Load/右RAM  |  GPU: 左Util/右VRAM)"
        )
        if self.config.get("gpu_badge_layout", "separate") == "separate":
            rb_layout_sep.set_active(True)
        else:
            rb_layout_comp.set_active(True)
        layout_box.pack_start(rb_layout_sep, False, False, 0)
        layout_box.pack_start(rb_layout_comp, False, False, 0)
        tab_disp.pack_start(layout_box, False, False, 0)

        unit_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        unit_box.set_margin_start(12)
        unit_lbl = Gtk.Label(label="面板顯存顯示單位：", xalign=0.0)
        rb_unit_pct = Gtk.RadioButton.new_with_label_from_widget(None, "百分比 (26%)")
        rb_unit_gib = Gtk.RadioButton.new_with_label_from_widget(rb_unit_pct, "容量 GiB (1.1G)")
        if self.config.get("gpu_mem_panel_unit", "percent") == "percent":
            rb_unit_pct.set_active(True)
        else:
            rb_unit_gib.set_active(True)
        unit_box.pack_start(unit_lbl, False, False, 0)
        unit_box.pack_start(rb_unit_pct, False, False, 0)
        unit_box.pack_start(rb_unit_gib, False, False, 0)
        tab_disp.pack_start(unit_box, False, False, 0)

        chk_hover_tip = Gtk.CheckButton(label="滑鼠懸停面板圖示時顯示詳細資訊 (Tooltip)")
        chk_hover_tip.set_active(self.config.get("show_hover_tooltip", True))
        tab_disp.pack_start(chk_hover_tip, False, False, 0)

        # Section 4: 3-Stage Panel Font Color & Background Adaptation (Tab 3)
        sec_col_lbl = Gtk.Label(xalign=0.0)
        sec_col_lbl.set_markup("<b>面板三段式字形顏色與底色適應 (正常 / 中度負載 / 高溫高負載警戒)</b>")
        tab_style.pack_start(sec_col_lbl, False, False, 0)

        rb_font_dyn = Gtk.RadioButton.new_with_label_from_widget(
            None, "使用預設內建動態字形色階"
        )
        rb_font_cus = Gtk.RadioButton.new_with_label_from_widget(
            rb_font_dyn, "自訂三段式字形顏色 (當溫度或 GPU/顯存使用率升高時自動切換字色)："
        )
        if self.config.get("font_color_mode", "dynamic") == "dynamic":
            rb_font_dyn.set_active(True)
        else:
            rb_font_cus.set_active(True)
        tab_style.pack_start(rb_font_dyn, False, False, 0)
        tab_style.pack_start(rb_font_cus, False, False, 0)

        def make_color_btn(hex_val, title):
            cb = Gtk.ColorButton()
            cb.set_title(title)
            rgba = Gdk.RGBA()
            if not rgba.parse(hex_val):
                rgba.parse("#ffffff")
            cb.set_rgba(rgba)
            return cb

        color_norm_btn = make_color_btn(
            self.config.get("custom_font_color", "#ffffff"), "選擇正常狀態字形顏色"
        )
        color_warn_btn = make_color_btn(
            self.config.get("custom_warn_font_color", "#fbbf24"), "選擇中度負載字形顏色"
        )
        color_crit_btn = make_color_btn(
            self.config.get("custom_crit_font_color", "#f87171"), "選擇高溫/高負載警戒字形顏色"
        )

        c_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        c_grid.set_margin_start(16)
        c_grid.attach(Gtk.Label(label="🟢 正常狀態字形色：", xalign=0.0), 0, 0, 1, 1)
        c_grid.attach(color_norm_btn, 1, 0, 1, 1)
        c_grid.attach(Gtk.Label(label="🟡 中度負載字形色：", xalign=0.0), 2, 0, 1, 1)
        c_grid.attach(color_warn_btn, 3, 0, 1, 1)
        c_grid.attach(Gtk.Label(label="🔴 高溫/高負載警戒字形色：", xalign=0.0), 0, 1, 1, 1)
        c_grid.attach(color_crit_btn, 1, 1, 1, 1)

        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_dark_theme = Gtk.Button(label="深色面板色票")
        btn_light_theme = Gtk.Button(label="淺色面板色票")
        preset_box.pack_start(btn_dark_theme, False, False, 0)
        preset_box.pack_start(btn_light_theme, False, False, 0)
        c_grid.attach(preset_box, 2, 1, 2, 1)
        tab_style.pack_start(c_grid, False, False, 0)

        chk_custom_badge = Gtk.CheckButton(
            label="分別自訂 CPU 與 GPU 方塊的底色、框線色與底色透明度（取消勾選則隨溫度變色）"
        )
        chk_custom_badge.set_active(self.config.get("custom_badge_style", True))
        tab_style.pack_start(chk_custom_badge, False, False, 0)

        cg_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        cg_grid.set_margin_start(14)

        def make_color_btn(cfg_key, default_hex):
            btn = Gtk.ColorButton()
            r = Gdk.RGBA()
            if not r.parse(self.config.get(cfg_key, default_hex)):
                r.parse(default_hex)
            btn.set_rgba(r)
            return btn

        cpu_bg_btn = make_color_btn("cpu_bg_color", "#113a2b")
        cpu_bd_btn = make_color_btn("cpu_border_color", "#34d399")
        cpu_op_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 2.0)
        cpu_op_scale.set_digits(0)
        cpu_op_scale.set_draw_value(False)
        cpu_op_scale.set_size_request(110, -1)
        cpu_op_init = max(0, min(100, int(self.config.get("cpu_bg_opacity", 92))))
        cpu_op_scale.set_value(float(cpu_op_init))
        lbl_cpu_op = Gtk.Label(label=f"{cpu_op_init}%")
        lbl_cpu_op.set_width_chars(4)

        gpu_bg_btn = make_color_btn("gpu_bg_color", "#162944")
        gpu_bd_btn = make_color_btn("gpu_border_color", "#38bdf8")
        gpu_op_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 2.0)
        gpu_op_scale.set_digits(0)
        gpu_op_scale.set_draw_value(False)
        gpu_op_scale.set_size_request(110, -1)
        gpu_op_init = max(0, min(100, int(self.config.get("gpu_bg_opacity", 92))))
        gpu_op_scale.set_value(float(gpu_op_init))
        lbl_gpu_op = Gtk.Label(label=f"{gpu_op_init}%")
        lbl_gpu_op.set_width_chars(4)

        cg_grid.attach(Gtk.Label(label="CPU 底色：", xalign=0.0), 0, 0, 1, 1)
        cg_grid.attach(cpu_bg_btn, 1, 0, 1, 1)
        cg_grid.attach(Gtk.Label(label="框線：", xalign=0.0), 2, 0, 1, 1)
        cg_grid.attach(cpu_bd_btn, 3, 0, 1, 1)
        cg_grid.attach(Gtk.Label(label="透明度：", xalign=0.0), 4, 0, 1, 1)
        cg_grid.attach(cpu_op_scale, 5, 0, 1, 1)
        cg_grid.attach(lbl_cpu_op, 6, 0, 1, 1)

        cg_grid.attach(Gtk.Label(label="GPU 底色：", xalign=0.0), 0, 1, 1, 1)
        cg_grid.attach(gpu_bg_btn, 1, 1, 1, 1)
        cg_grid.attach(Gtk.Label(label="框線：", xalign=0.0), 2, 1, 1, 1)
        cg_grid.attach(gpu_bd_btn, 3, 1, 1, 1)
        cg_grid.attach(Gtk.Label(label="透明度：", xalign=0.0), 4, 1, 1, 1)
        cg_grid.attach(gpu_op_scale, 5, 1, 1, 1)
        cg_grid.attach(lbl_gpu_op, 6, 1, 1, 1)
        tab_style.pack_start(cg_grid, False, False, 0)

        bg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bg_lbl = Gtk.Label(label="動態模式底色樣式：", xalign=0.0)
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
        tab_style.pack_start(bg_box, False, False, 0)

        chk_shadow = Gtk.CheckButton(label="啟用文字高對比描邊陰影 (透明或花紋面板底色時保持清晰可讀)")
        chk_shadow.set_active(self.config.get("text_shadow", False))
        tab_style.pack_start(chk_shadow, False, False, 0)

        chk_no_border = Gtk.CheckButton(label="取消 MATE Panel 圖示框線")
        chk_no_border.set_active(self.config.get("hide_panel_border", False))
        tab_style.pack_start(chk_no_border, False, False, 0)

        # Section 5: Calculation & Thresholds (Tab 1)
        sec5_lbl = Gtk.Label(xalign=0.0)
        sec5_lbl.set_markup("<b>2. 感測器計算與中度/高溫警戒變色閾值</b>")
        tab_mon.pack_start(sec5_lbl, False, False, 0)

        chk_cores_only = Gtk.CheckButton(label="CPU 平均溫度僅計算實體核心 (Core 0..N，不含 Package)")
        chk_cores_only.set_active(self.config.get("cpu_avg_cores_only", True))
        tab_mon.pack_start(chk_cores_only, False, False, 0)

        chk_other = Gtk.CheckButton(label="在點選選單中顯示其他感測器 (NVMe SSD / 主機板 / 風扇)")
        chk_other.set_active(self.config.get("show_other_sensors", True))
        tab_mon.pack_start(chk_other, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        lbl_warn = Gtk.Label(label="溫度中度負載閾值 (°C)：", xalign=0.0)
        adj_warn = Gtk.Adjustment(
            value=float(self.config.get("warn_temp_c", 55.0)),
            lower=30.0,
            upper=95.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin_warn = Gtk.SpinButton(adjustment=adj_warn, climb_rate=1.0, digits=0)

        lbl_crit = Gtk.Label(label="溫度高溫警戒閾值 (°C)：", xalign=0.0)
        adj_crit = Gtk.Adjustment(
            value=float(self.config.get("crit_temp_c", 75.0)),
            lower=40.0,
            upper=110.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin_crit = Gtk.SpinButton(adjustment=adj_crit, climb_rate=1.0, digits=0)

        lbl_warn_p = Gtk.Label(label="GPU/顯存 中度負載 (%)：", xalign=0.0)
        adj_warn_p = Gtk.Adjustment(
            value=float(self.config.get("warn_pct", 60.0)),
            lower=20.0,
            upper=95.0,
            step_increment=5.0,
            page_increment=10.0,
            page_size=0.0,
        )
        spin_warn_p = Gtk.SpinButton(adjustment=adj_warn_p, climb_rate=1.0, digits=0)

        lbl_crit_p = Gtk.Label(label="GPU/顯存 高負載警戒 (%)：", xalign=0.0)
        adj_crit_p = Gtk.Adjustment(
            value=float(self.config.get("crit_pct", 85.0)),
            lower=30.0,
            upper=99.0,
            step_increment=5.0,
            page_increment=10.0,
            page_size=0.0,
        )
        spin_crit_p = Gtk.SpinButton(adjustment=adj_crit_p, climb_rate=1.0, digits=0)

        grid.attach(lbl_warn, 0, 0, 1, 1)
        grid.attach(spin_warn, 1, 0, 1, 1)
        grid.attach(lbl_crit, 2, 0, 1, 1)
        grid.attach(spin_crit, 3, 0, 1, 1)
        grid.attach(lbl_warn_p, 0, 1, 1, 1)
        grid.attach(spin_warn_p, 1, 1, 1, 1)
        grid.attach(lbl_crit_p, 2, 1, 1, 1)
        grid.attach(spin_crit_p, 3, 1, 1, 1)
        tab_mon.pack_start(grid, False, False, 0)

        # Live apply bindings
        def rgba_to_hex(rgba):
            r = max(0, min(255, int(round(rgba.red * 255))))
            g = max(0, min(255, int(round(rgba.green * 255))))
            b = max(0, min(255, int(round(rgba.blue * 255))))
            return f"#{r:02x}{g:02x}{b:02x}"

        def apply_changes(*_):
            self.config["enabled"] = chk_enable.get_active()
            self.config["interval_sec"] = max(1, int(spin_freq.get_value_as_int()))
            self.config["display_mode"] = "badge" if rb_badge.get_active() else "icon_text"
            self.config["show_panel_gpu_util"] = chk_gpu_util.get_active()
            self.config["show_panel_gpu_mem"] = chk_gpu_mem.get_active()
            self.config["gpu_badge_layout"] = "separate" if rb_layout_sep.get_active() else "compact"
            self.config["gpu_mem_panel_unit"] = "percent" if rb_unit_pct.get_active() else "gib"
            self.config["font_color_mode"] = "dynamic" if rb_font_dyn.get_active() else "custom"
            self.config["custom_font_color"] = rgba_to_hex(color_norm_btn.get_rgba())
            self.config["custom_warn_font_color"] = rgba_to_hex(color_warn_btn.get_rgba())
            self.config["custom_crit_font_color"] = rgba_to_hex(color_crit_btn.get_rgba())
            self.config["custom_badge_style"] = chk_custom_badge.get_active()
            self.config["cpu_bg_color"] = rgba_to_hex(cpu_bg_btn.get_rgba())
            self.config["cpu_border_color"] = rgba_to_hex(cpu_bd_btn.get_rgba())
            c_op = max(0, min(100, int(round(cpu_op_scale.get_value()))))
            self.config["cpu_bg_opacity"] = c_op
            lbl_cpu_op.set_text(f"{c_op}%")
            self.config["gpu_bg_color"] = rgba_to_hex(gpu_bg_btn.get_rgba())
            self.config["gpu_border_color"] = rgba_to_hex(gpu_bd_btn.get_rgba())
            g_op = max(0, min(100, int(round(gpu_op_scale.get_value()))))
            self.config["gpu_bg_opacity"] = g_op
            lbl_gpu_op.set_text(f"{g_op}%")
            cg_grid.set_sensitive(chk_custom_badge.get_active())
            if rb_bg_dark.get_active():
                self.config["badge_bg_style"] = "dark"
            elif rb_bg_light.get_active():
                self.config["badge_bg_style"] = "light"
            else:
                self.config["badge_bg_style"] = "transparent"
            self.config["text_shadow"] = chk_shadow.get_active()
            self.config["show_hover_tooltip"] = chk_hover_tip.get_active()
            self.config["hide_panel_border"] = chk_no_border.get_active()
            self.config["cpu_avg_cores_only"] = chk_cores_only.get_active()
            self.config["show_other_sensors"] = chk_other.get_active()
            self.config["warn_temp_c"] = float(spin_warn.get_value())
            self.config["crit_temp_c"] = max(
                self.config["warn_temp_c"] + 5.0, float(spin_crit.get_value())
            )
            self.config["warn_pct"] = float(spin_warn_p.get_value())
            self.config["crit_pct"] = max(
                self.config["warn_pct"] + 5.0, float(spin_crit_p.get_value())
            )
            self._save_config()
            self._sync_timer()
            self.refresh_readings(force_poll=self.config["enabled"])

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
        for cg_btn in (cpu_bg_btn, cpu_bd_btn, gpu_bg_btn, gpu_bd_btn):
            cg_btn.connect("color-set", lambda *_: (chk_custom_badge.set_active(True), apply_changes()))
        for cg_sc in (cpu_op_scale, gpu_op_scale):
            cg_sc.connect("value-changed", apply_changes)
        cg_grid.set_sensitive(chk_custom_badge.get_active())
        btn_dark_theme.connect(
            "clicked", lambda *_: set_quick_theme("#ffffff", "#fbbf24", "#f87171")
        )
        btn_light_theme.connect(
            "clicked", lambda *_: set_quick_theme("#111827", "#b45309", "#b91c1c")
        )

        for w_ctrl in (
            chk_enable,
            rb_badge,
            rb_icontext,
            chk_gpu_util,
            chk_gpu_mem,
            rb_layout_sep,
            rb_layout_comp,
            rb_unit_pct,
            rb_unit_gib,
            rb_font_dyn,
            rb_font_cus,
            chk_custom_badge,
            rb_bg_dark,
            rb_bg_light,
            rb_bg_trans,
            chk_shadow,
            chk_hover_tip,
            chk_no_border,
            chk_cores_only,
            chk_other,
        ):
            w_ctrl.connect("toggled", apply_changes)
        for sp_ctrl in (spin_freq, spin_warn, spin_crit, spin_warn_p, spin_crit_p):
            sp_ctrl.connect("value-changed", apply_changes)

        # Bottom buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_sensors = Gtk.Button(label="🌡 檢視所有感測器清單")
        btn_sensors.connect("clicked", lambda *_: self.popup_sensor_menu(None))
        btn_exp_cfg = Gtk.Button(label="📤 匯出設定...")
        btn_imp_cfg = Gtk.Button(label="📥 匯入設定...")
        btn_close = Gtk.Button(label="關閉")
        btn_close.connect("clicked", lambda *_: win.destroy())

        def on_export_config(*_):
            apply_changes()
            today_str = datetime.now().strftime("%Y%m%d")
            dlg = Gtk.FileChooserDialog(
                title="匯出 CPU/GPU 溫度監控設定檔",
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
            dlg.set_current_name(f"temp-monitor-config-{today_str}.json")
            resp = dlg.run()
            out_path = dlg.get_filename() if resp == Gtk.ResponseType.OK else None
            dlg.destroy()
            if not out_path:
                return
            if not out_path.lower().endswith(".json"):
                out_path += ".json"
            bundle = {
                "format": "matepanel-applet-plus.temp-monitor.config",
                "version": 1,
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "config": dict(self.config),
            }
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(bundle, f, indent=2, ensure_ascii=False)
                md = Gtk.MessageDialog(
                    transient_for=win,
                    flags=0,
                    message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="✅ 已成功匯出設定",
                )
                md.format_secondary_text(f"已儲存至：{out_path}")
                md.run()
                md.destroy()
            except Exception as exc:
                md = Gtk.MessageDialog(
                    transient_for=win,
                    flags=0,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="❌ 匯出設定失敗",
                )
                md.format_secondary_text(str(exc))
                md.run()
                md.destroy()

        def on_import_config(*_):
            dlg = Gtk.FileChooserDialog(
                title="匯入 CPU/GPU 溫度監控設定檔",
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
                if raw.get("format") == "matepanel-applet-plus.temp-monitor.config" and isinstance(raw.get("config"), dict):
                    imp_cfg = raw["config"]
                else:
                    imp_cfg = raw
                valid_keys = set(DEFAULT_CONFIG.keys())
                matched = {k: v for k, v in imp_cfg.items() if k in valid_keys}
                if not matched:
                    raise ValueError("找不到任何相容的溫度監控設定欄位")
                new_cfg = dict(DEFAULT_CONFIG)
                new_cfg.update(self.config)
                new_cfg.update(matched)
                self.config = new_cfg
                self._save_config()
                self._sync_timer()
                self.refresh_readings(force_poll=bool(self.config.get("enabled", True)))
                win.destroy()
                self.show_preferences_dialog()
                md = Gtk.MessageDialog(
                    transient_for=None,
                    flags=0,
                    message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="✅ 已成功匯入設定",
                )
                md.format_secondary_text(
                    f"已套用來自「{os.path.basename(in_path)}」的 {len(matched)} 項設定並重新載入偏好設定視窗。"
                )
                md.run()
                md.destroy()
            except Exception as exc:
                md = Gtk.MessageDialog(
                    transient_for=win,
                    flags=0,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="❌ 匯入設定失敗",
                )
                md.format_secondary_text(str(exc))
                md.run()
                md.destroy()

        btn_exp_cfg.connect("clicked", on_export_config)
        btn_imp_cfg.connect("clicked", on_import_config)
        btn_box.pack_start(btn_sensors, False, False, 0)
        btn_box.pack_start(btn_exp_cfg, False, False, 0)
        btn_box.pack_start(btn_imp_cfg, False, False, 0)
        btn_box.pack_end(btn_close, False, False, 0)
        outer.pack_end(btn_box, False, False, 0)

        win.add(outer)
        win.show_all()

    def show_about_dialog(self, *_args):
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("CPU/GPU 溫度與負載監控 Plus")
        dlg.set_version("1.4.0")
        dlg.set_comments(
            "零外部子行程開銷的 MATE 面板 CPU/GPU 溫度與負載監控 Applet。\n"
            "直讀 /sys/class/hwmon 與 NVIDIA NVML，支援自訂正常/中度負載/高溫警戒三段式字形顏色。"
        )
        dlg.set_website("https://github.com/sophAi/matepanel-applet-plus")
        dlg.run()
        dlg.destroy()

    def toggle_monitoring_action(self, *_args):
        self.config["enabled"] = not self.config.get("enabled", True)
        self._save_config()
        self._sync_timer()
        self.refresh_readings()

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

    def _on_display_mode_toggled(self, radio_item, mode):
        if not radio_item.get_active():
            return
        self.config["display_mode"] = mode
        self._save_config()
        self._update_canvas_size()

    def _on_gpu_layout_toggled(self, radio_item, layout):
        if not radio_item.get_active():
            return
        self.config["gpu_badge_layout"] = layout
        self._save_config()
        self._update_canvas_size()

    def _on_toggle_panel_gpu_util(self, check_item):
        self.config["show_panel_gpu_util"] = check_item.get_active()
        self._save_config()
        self._update_canvas_size()

    def _on_toggle_panel_gpu_mem(self, check_item):
        self.config["show_panel_gpu_mem"] = check_item.get_active()
        self._save_config()
        self._update_canvas_size()

    def _on_mem_unit_toggled(self, radio_item, unit):
        if not radio_item.get_active():
            return
        self.config["gpu_mem_panel_unit"] = unit
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

    def _on_toggle_text_shadow(self, check_item):
        self.config["text_shadow"] = check_item.get_active()
        self._save_config()
        self.canvas.queue_draw()

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
        if self.prefs_win is not None:
            self.prefs_win.destroy()
            self.prefs_win = None
        self.sensor_mgr.close()


def setup_applet_context_menu(applet, widget):
    """Register standard right-click context menu actions on the MATE Panel Applet."""
    action_group = Gtk.ActionGroup(name="TempMonitorAppletActions")
    action_group.add_actions(
        [
            (
                "PreferencesAction",
                Gtk.STOCK_PREFERENCES,
                "偏好設定 (_Preferences)...",
                None,
                "設定監控頻率、三段式字形顏色、GPU-Util/顯存顯示樣式與警戒閾值",
                widget.show_preferences_dialog,
            ),
            (
                "ShowSensorsAction",
                Gtk.STOCK_INFO,
                "所有監控單元溫度與 GPU 狀態 (_Sensors)...",
                None,
                "顯示所有 CPU 核心與 GPU 溫度、使用率、顯存、風扇與功耗",
                lambda *_: widget.popup_sensor_menu(None),
            ),
            (
                "ToggleMonitorAction",
                Gtk.STOCK_MEDIA_PAUSE,
                "啟用 / 暫停監控 (_Toggle)",
                None,
                "切換啟用或關閉背景輪詢",
                widget.toggle_monitoring_action,
            ),
            (
                "AboutAction",
                Gtk.STOCK_ABOUT,
                "關於 (_About)...",
                None,
                "關於 CPU/GPU 溫度與負載監控 Plus",
                widget.show_about_dialog,
            ),
        ]
    )

    menu_xml = (
        '<menuitem name="Preferences" action="PreferencesAction"/>'
        '<menuitem name="ShowSensors" action="ShowSensorsAction"/>'
        '<menuitem name="ToggleMonitor" action="ToggleMonitorAction"/>'
        "<separator/>"
        '<menuitem name="About" action="AboutAction"/>'
        "<separator/>"
    )
    applet.setup_menu(menu_xml, action_group)


def applet_fill(applet):
    """Initialize and populate the MATE Panel Applet container."""
    os.chdir(os.path.expanduser("~"))
    applet.set_flags(MatePanelApplet.AppletFlags.EXPAND_MINOR)
    applet.set_background_widget(applet)
    applet.set_events(
        applet.get_events()
        | Gdk.EventMask.BUTTON_PRESS_MASK
        | Gdk.EventMask.BUTTON_RELEASE_MASK
    )

    widget = TempMonitorWidget(applet=applet)
    init_size = applet.get_size()
    widget.set_panel_size(init_size if init_size else 32)

    setup_applet_context_menu(applet, widget)

    applet.connect("button-press-event", widget.handle_applet_button_press)
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
    win = Gtk.Window(title="MATE CPU/GPU Temp & Load Applet - 預覽測試視窗")
    win.set_default_size(540, 160)
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
        "<small>• <b>左鍵點選</b>下方圖示：開啟監控清單、快速切換<b>三段式字形顏色與底色適應</b>\n"
        "• 點選「⚙️ 偏好設定」：分別自訂<b>正常 / 中度負載 / 高溫高負載警戒</b>三種狀態的字形顏色與變色閾值</small>"
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

    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    hbox.set_margin_top(4)
    hbox.set_margin_bottom(4)
    hbox.set_margin_start(10)
    hbox.set_margin_end(10)

    panel_lbl = Gtk.Label()
    panel_lbl.set_markup("<span foreground='#9aa0a6'><small>模擬面板 👉</small></span>")
    hbox.pack_start(panel_lbl, False, False, 0)

    widget = TempMonitorWidget(applet=None)
    widget.set_panel_size(32)
    hbox.pack_start(widget, False, False, 0)

    btn_prefs = Gtk.Button(label="⚙️ 偏好設定")
    btn_prefs.connect("clicked", widget.show_preferences_dialog)
    hbox.pack_end(btn_prefs, False, False, 0)

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

    btn_bg = Gtk.Button(label="🌓 切換深/淺模擬面板")
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
