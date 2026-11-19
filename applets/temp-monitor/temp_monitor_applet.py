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
- Left-click popup menu on the applet showing:
  - Detailed readings of all CPU cores/packages
  - Detailed GPU temperature, Volatile GPU-Util progress bar, Memory-Usage progress bar (MiB / Total MiB),
    Fan speed (%), and Power draw (W)
  - Optional auxiliary sensors (NVMe SSD, ACPI, CPU fans)
  - Quick controls for monitoring toggle, refresh interval, and display styles
- Right-click MATE Panel native context menu + full Preferences window.
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
    "display_mode": "badge",          # "badge" (方塊圖示) or "icon_text" (晶片圖示+文字)
    "gpu_badge_layout": "separate",   # "separate" (獨立方塊 [CPU][GPU][UTL][MEM]) or "compact" (GPU方塊內嵌雙進度條)
    "show_panel_gpu_util": True,      # 在面板顯示 Volatile GPU-Util
    "show_panel_gpu_mem": True,       # 在面板顯示 GPU Memory-Usage
    "gpu_mem_panel_unit": "percent",  # "percent" (26%) or "gib" (1.1G)
    "show_other_sensors": True,       # 在選單中顯示 NVMe / 主機板 / 風扇等其他感測器
    "cpu_avg_cores_only": True,       # True: 僅平均實體核心 Core 0..N; False: 包含 Package id
    "warn_temp_c": 55.0,              # 中度負載溫度閾值 (°C)
    "crit_temp_c": 75.0,              # 高溫警戒溫度閾值 (°C)
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
        """
        Return a list of dicts with comprehensive NVIDIA GPU metrics:
        [
          {
            'chip': 'nvml',
            'label': 'NVIDIA GeForce GTX 1650',
            'temp': 41.0,
            'high': 83.0,
            'crit': 93.0,
            'gpu_util': 34.0,       # Volatile GPU-Util (%)
            'mem_ctrl_util': 25.0,  # Memory controller utilization (%)
            'mem_used_mib': 1072.0, # VRAM Used (MiB)
            'mem_total_mib': 4096.0,# VRAM Total (MiB)
            'mem_pct': 26.2,        # VRAM Usage (%)
            'fan_pct': 64.0,        # Fan speed (%) or None
            'power_w': 18.5,        # Power draw (W) or None
          }
        ]
        """
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

                # Volatile GPU-Util & Memory Controller Util
                gpu_util = None
                mem_ctrl_util = None
                util_st = _NvmlUtilization()
                if self.lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util_st)) == 0:
                    gpu_util = float(util_st.gpu)
                    mem_ctrl_util = float(util_st.memory)

                # Memory-Usage (VRAM Used / Total)
                mem_used_mib = None
                mem_total_mib = None
                mem_pct = None
                mem_st = _NvmlMemory()
                if self.lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem_st)) == 0 and mem_st.total > 0:
                    mem_used_mib = mem_st.used / (1024.0 * 1024.0)
                    mem_total_mib = mem_st.total / (1024.0 * 1024.0)
                    mem_pct = (mem_st.used * 100.0) / mem_st.total

                # Fan Speed (%)
                fan_pct = None
                fan_val = ctypes.c_uint()
                if self.lib.nvmlDeviceGetFanSpeed(handle, ctypes.byref(fan_val)) == 0:
                    fan_pct = float(fan_val.value)

                # Power Usage (mW -> W)
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

    def _read_sysfs_drm_gpu_metrics(self, hwmon_dir):
        """Fallback reader for AMD/Intel DRM sysfs busy percent & VRAM usage."""
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

        # Poll NVIDIA GPUs via NVML
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

        return {
            "cpu_avg": cpu_avg,
            "gpu_avg": gpu_avg,
            "gpu_util_avg": gpu_util_avg,
            "gpu_mem_pct_avg": gpu_mem_pct_avg,
            "gpu_mem_used_mib": gpu_mem_used_mib,
            "gpu_mem_total_mib": gpu_mem_total_mib,
            "cpu_sensors": cpu_sensors,
            "gpu_sensors": gpu_sensors,
            "other_sensors": other_sensors,
        }


def temp_to_colors(temp, enabled=True, warn_c=55.0, crit_c=75.0):
    """Return (bg_rgb, border_rgb, text_rgb, accent_hex) for a given temperature in Celsius."""
    if not enabled or temp is None:
        return (
            (0.24, 0.26, 0.29),
            (0.45, 0.48, 0.52),
            (0.78, 0.80, 0.82),
            "#9aa0a6",
        )
    if temp < warn_c:
        # Cool emerald/teal
        return (
            (0.11, 0.36, 0.27),
            (0.20, 0.78, 0.54),
            (0.95, 1.00, 0.97),
            "#34d399",
        )
    elif temp < crit_c:
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


def pct_to_colors(pct, enabled=True, base_hue="cyan"):
    """
    Return (bg_rgb, border_rgb, text_rgb, accent_hex) for percentage metrics (0..100%).
    base_hue: 'cyan' (for GPU-Util) or 'purple' (for Memory-Usage) when below warning threshold.
    """
    if not enabled or pct is None:
        return (
            (0.24, 0.26, 0.29),
            (0.45, 0.48, 0.52),
            (0.78, 0.80, 0.82),
            "#9aa0a6",
        )
    if pct < 60.0:
        if base_hue == "purple":
            return (
                (0.22, 0.16, 0.42),
                (0.62, 0.48, 0.98),
                (0.96, 0.95, 1.00),
                "#a78bfa",
            )
        return (
            (0.10, 0.29, 0.42),
            (0.22, 0.74, 0.97),
            (0.94, 0.99, 1.00),
            "#38bdf8",
        )
    elif pct < 85.0:
        return (
            (0.45, 0.28, 0.07),
            (0.96, 0.65, 0.14),
            (1.00, 0.98, 0.92),
            "#fbbf24",
        )
    else:
        return (
            (0.48, 0.11, 0.14),
            (0.96, 0.28, 0.32),
            (1.00, 0.95, 0.95),
            "#f87171",
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

        if mode == "badge":
            n_badges = self._get_badge_count()
            badge_w = max(30, int(h * 1.18))
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
        gpu_avg = self.latest_data.get("gpu_avg")
        gpu_util = self.latest_data.get("gpu_util_avg")
        mem_pct = self.latest_data.get("gpu_mem_pct_avg")
        mem_used = self.latest_data.get("gpu_mem_used_mib")
        mem_tot = self.latest_data.get("gpu_mem_total_mib")

        cpu_str = f"{cpu_avg:.1f}°C" if (enabled and cpu_avg is not None) else ("已關閉" if not enabled else "N/A")
        gpu_str = f"{gpu_avg:.1f}°C" if (enabled and gpu_avg is not None) else ("已關閉" if not enabled else "N/A")
        util_str = f"{gpu_util:.0f}%" if (enabled and gpu_util is not None) else ("已關閉" if not enabled else "N/A")
        if enabled and mem_used is not None and mem_tot is not None:
            mem_str = f"{mem_used:.0f} / {mem_tot:.0f} MiB ({mem_pct:.1f}%)"
        else:
            mem_str = "已關閉" if not enabled else "N/A"

        status_str = f"啟用中 (每 {interval} 秒更新)" if enabled else "已暫停監控"
        self.set_tooltip_markup(
            f"<b>CPU / GPU 溫度與負載監控 Plus</b>\n"
            f"• CPU 平均溫度: <b>{cpu_str}</b>\n"
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
        mini_bars=None,
    ):
        """
        Draw a single rounded badge with a title, value text, and optional bottom mini progress bars.
        mini_bars: list of (pct_float_0_100, (r, g, b))
        """
        bg_rgb, border_rgb, text_rgb, _ = colors

        draw_rounded_rect(ctx, x + 0.5, y + 0.5, bw - 1.0, bh - 1.0, 4.5)
        ctx.set_source_rgba(bg_rgb[0], bg_rgb[1], bg_rgb[2], 0.92)
        ctx.fill_preserve()
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 0.95)
        ctx.set_line_width(1.2)
        ctx.stroke()

        has_bars = bool(mini_bars)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        header_size = max(6.5, bh * (0.24 if has_bars else 0.27))
        ctx.set_font_size(header_size)
        ext = ctx.text_extents(title)
        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 1.0)
        ctx.move_to(x + (bw - ext.width) / 2.0 - ext.x_bearing, y + header_size + 1.2)
        ctx.show_text(title)

        val_size = max(8.5, bh * (0.35 if has_bars else 0.41))
        if len(val_str) >= 4:
            val_size *= 0.88
        ctx.set_font_size(val_size)
        v_ext = ctx.text_extents(val_str)
        ctx.set_source_rgba(text_rgb[0], text_rgb[1], text_rgb[2], 1.0)
        bottom_offset = (3.5 + len(mini_bars) * 2.8) if has_bars else 3.0
        ctx.move_to(
            x + (bw - v_ext.width) / 2.0 - v_ext.x_bearing,
            y + bh - bottom_offset,
        )
        ctx.show_text(val_str)

        # Draw embedded mini bars at bottom of badge (for compact mode)
        if has_bars:
            bar_x = x + 3.0
            bar_w = max(6.0, bw - 6.0)
            bar_h = 2.0
            cur_y = y + bh - 2.6 - (len(mini_bars) - 1) * 2.8
            for pct, bar_rgb in mini_bars:
                # Track background
                ctx.set_source_rgba(0.08, 0.10, 0.12, 0.85)
                ctx.rectangle(bar_x, cur_y, bar_w, bar_h)
                ctx.fill()
                # Active fill
                if pct is not None and pct > 0:
                    fill_w = max(1.0, min(bar_w, bar_w * (pct / 100.0)))
                    ctx.set_source_rgba(bar_rgb[0], bar_rgb[1], bar_rgb[2], 0.98)
                    ctx.rectangle(bar_x, cur_y, fill_w, bar_h)
                    ctx.fill()
                cur_y += 2.8

    def _draw_badge_mode(self, ctx, w, h, enabled):
        cpu_avg = self.latest_data.get("cpu_avg") if enabled else None
        gpu_avg = self.latest_data.get("gpu_avg") if enabled else None
        gpu_util = self.latest_data.get("gpu_util_avg") if enabled else None
        mem_pct = self.latest_data.get("gpu_mem_pct_avg") if enabled else None

        show_util = self.config.get("show_panel_gpu_util", True)
        show_mem = self.config.get("show_panel_gpu_mem", True)
        layout = self.config.get("gpu_badge_layout", "separate")

        badges = []

        # 1. CPU Temp Badge
        cpu_val_str = "OFF" if not enabled else ("--°" if cpu_avg is None else f"{int(round(cpu_avg))}°")
        badges.append(("CPU", cpu_val_str, self._get_temp_colors(cpu_avg, enabled), None))

        # 2. GPU Temp Badge (with optional embedded mini bars in compact layout)
        gpu_val_str = "OFF" if not enabled else ("--°" if gpu_avg is None else f"{int(round(gpu_avg))}°")
        mini_bars = None
        if layout == "compact":
            mini_bars = []
            if show_util:
                _, u_border, _, _ = pct_to_colors(gpu_util, enabled, base_hue="cyan")
                mini_bars.append((gpu_util if enabled else 0.0, u_border))
            if show_mem:
                _, m_border, _, _ = pct_to_colors(mem_pct, enabled, base_hue="purple")
                mini_bars.append((mem_pct if enabled else 0.0, m_border))
        badges.append(("GPU", gpu_val_str, self._get_temp_colors(gpu_avg, enabled), mini_bars))

        # 3 & 4. Separate UTL and MEM badges when layout == "separate"
        if layout == "separate":
            if show_util:
                util_str = "OFF" if not enabled else ("--%" if gpu_util is None else f"{int(round(gpu_util))}%")
                badges.append(("UTL", util_str, pct_to_colors(gpu_util, enabled, base_hue="cyan"), None))
            if show_mem:
                mem_str = self._format_mem_short(enabled)
                badges.append(("MEM", mem_str, pct_to_colors(mem_pct, enabled, base_hue="purple"), None))

        n = len(badges)
        pad_y = 1.0
        bh = max(20.0, h - pad_y * 2)
        gap = 4.0
        bw = (w - gap * (n - 1) - 2.0) / float(n)

        cur_x = 1.0
        for title, val_str, colors, mbars in badges:
            self._draw_generic_badge(ctx, cur_x, pad_y, bw, bh, title, val_str, colors, mini_bars=mbars)
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
        c_bg, c_border, _, _ = self._get_temp_colors(cpu_avg, enabled)
        self._draw_mini_chip_icon(ctx, cur_x, icon_y, icon_size, "C", c_border, c_bg)
        cur_x += icon_size + 5.0

        c_str = "OFF" if not enabled else ("--°C" if cpu_avg is None else f"{cpu_avg:.0f}°C")
        c_ext = ctx.text_extents(c_str)
        ctx.set_source_rgba(c_border[0], c_border[1], c_border[2], 1.0)
        ctx.move_to(cur_x, (h + c_ext.height) / 2.0)
        ctx.show_text(c_str)
        cur_x += c_ext.width + 10.0

        # 2. Draw GPU Icon + Temp
        g_bg, g_border, _, _ = self._get_temp_colors(gpu_avg, enabled)
        self._draw_mini_chip_icon(ctx, cur_x, icon_y, icon_size, "G", g_border, g_bg)
        cur_x += icon_size + 5.0

        g_str = "OFF" if not enabled else ("--°C" if gpu_avg is None else f"{gpu_avg:.0f}°C")
        g_ext = ctx.text_extents(g_str)
        ctx.set_source_rgba(g_border[0], g_border[1], g_border[2], 1.0)
        ctx.move_to(cur_x, (h + g_ext.height) / 2.0)
        ctx.show_text(g_str)
        cur_x += g_ext.width + 6.0

        # 3. Optional GPU-Util text
        if show_util and enabled:
            _, u_border, _, _ = pct_to_colors(gpu_util, enabled, base_hue="cyan")
            u_str = "--%" if gpu_util is None else f"{gpu_util:.0f}%"
            u_ext = ctx.text_extents(u_str)
            ctx.set_source_rgba(u_border[0], u_border[1], u_border[2], 1.0)
            ctx.move_to(cur_x, (h + u_ext.height) / 2.0)
            ctx.show_text(u_str)
            cur_x += u_ext.width + 6.0

        # 4. Optional GPU Memory-Usage text
        if show_mem and enabled:
            _, m_border, _, _ = pct_to_colors(mem_pct, enabled, base_hue="purple")
            m_str = self._format_mem_short(enabled)
            m_ext = ctx.text_extents(m_str)
            ctx.set_source_rgba(m_border[0], m_border[1], m_border[2], 1.0)
            ctx.move_to(cur_x, (h + m_ext.height) / 2.0)
            ctx.show_text(m_str)

    def handle_applet_button_press(self, widget, event):
        """Handler bound directly to MatePanelApplet button-press-event."""
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
        """Build and pop up the detailed sensor readings & quick control menu."""
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
        _, _, _, cpu_hex = self._get_temp_colors(cpu_avg, enabled)
        _, _, _, gpu_hex = self._get_temp_colors(gpu_avg, enabled)
        _, _, _, util_hex = pct_to_colors(gpu_util, enabled, base_hue="cyan")
        _, _, _, mem_hex = pct_to_colors(mem_pct, enabled, base_hue="purple")

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

        # 2. CPU Sensor Units Section
        cpu_title = self._create_markup_menu_item(
            f"<b>🖥 CPU 監控單元 (平均: {cpu_avg_txt})</b>", sensitive=False
        )
        menu.append(cpu_title)

        cpu_sensors = self.latest_data.get("cpu_sensors", [])
        if not cpu_sensors:
            menu.append(self._create_markup_menu_item("   <i>無感測資料</i>", sensitive=False))
        else:
            for s in cpu_sensors:
                _, _, _, col = self._get_temp_colors(s["temp"], True)
                extra = ""
                if s.get("high") and s.get("crit"):
                    extra = f" <small><span foreground='#888888'>(High: {s['high']:.0f}°C / Crit: {s['crit']:.0f}°C)</span></small>"
                row = self._create_markup_menu_item(
                    f"   • <tt>{s['label']:<14}</tt> <span foreground='{col}'><b>{s['temp']:.1f}°C</b></span>{extra}",
                    sensitive=True,
                )
                menu.append(row)

        menu.append(Gtk.SeparatorMenuItem())

        # 3. GPU Sensor Units Section (Temperature + Volatile GPU-Util + Memory-Usage + Fan + Power)
        gpu_title = self._create_markup_menu_item(
            f"<b>🎮 GPU 監控單元 (平均溫度: {gpu_avg_txt})</b>", sensitive=False
        )
        menu.append(gpu_title)

        gpu_sensors = self.latest_data.get("gpu_sensors", [])
        if not gpu_sensors:
            menu.append(self._create_markup_menu_item("   <i>未偵測到獨立或內建 GPU 感測器</i>", sensitive=False))
        else:
            for s in gpu_sensors:
                _, _, _, t_col = self._get_temp_colors(s["temp"], True)
                extra_t = ""
                if s.get("high") and s.get("crit"):
                    extra_t = f" <small><span foreground='#888888'>(High: {s['high']:.0f}°C / Crit: {s['crit']:.0f}°C)</span></small>"
                menu.append(
                    self._create_markup_menu_item(
                        f"   • <tt><b>{s['label']}</b></tt>   <span foreground='{t_col}'><b>{s['temp']:.1f}°C</b></span>{extra_t}",
                        sensitive=True,
                    )
                )

                # Volatile GPU-Util row
                g_u = s.get("gpu_util")
                if g_u is not None:
                    _, _, _, u_col = pct_to_colors(g_u, True, base_hue="cyan")
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

                # Memory-Usage row
                m_used = s.get("mem_used_mib")
                m_tot = s.get("mem_total_mib")
                m_p = s.get("mem_pct")
                if m_used is not None and m_tot is not None and m_p is not None:
                    _, _, _, m_col = pct_to_colors(m_p, True, base_hue="purple")
                    m_bar = make_ascii_bar(m_p, 10)
                    menu.append(
                        self._create_markup_menu_item(
                            f"      ├─ <tt>Memory-Usage      :</tt> <tt><span foreground='{m_col}'>[{m_bar}]</span></tt> "
                            f"<span foreground='{m_col}'><b>{m_used:.0f} MiB / {m_tot:.0f} MiB ({m_p:.1f}%)</b></span>",
                            sensitive=True,
                        )
                    )

                # Fan Speed & Power Draw row
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

        # 4. Optional Auxiliary Hardware Sensors (NVMe, ACPI, Fans)
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
                        _, _, _, col = self._get_temp_colors(t_val, True)
                        v_markup = f"<span foreground='{col}'><b>{s['value_str']}</b></span>"
                    else:
                        v_markup = f"<b>{s['value_str']}</b>"
                    row = self._create_markup_menu_item(
                        f"   • <tt>{s['label']:<22}</tt> {v_markup}",
                        sensitive=True,
                    )
                    menu.append(row)

        menu.append(Gtk.SeparatorMenuItem())

        # 5. Control Options: Enable/Disable Monitoring
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
            layout_sep, "方塊佈局 2：緊湊二合一 ([CPU] [GPU 底部內嵌雙進度條])"
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

        # 8. Direct Preferences Dialog Item
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
        win.set_default_size(460, 520)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_resizable(False)
        self.prefs_win = win

        def on_win_destroy(*_):
            self.prefs_win = None

        win.connect("destroy", on_win_destroy)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(18)
        outer.set_margin_end(18)

        # Section 1: Monitoring Status & Frequency
        sec1_lbl = Gtk.Label(xalign=0.0)
        sec1_lbl.set_markup("<b>1. 監控開關與時間頻率</b>")
        outer.pack_start(sec1_lbl, False, False, 0)

        chk_enable = Gtk.CheckButton(label="啟用自動監控 (關閉時完全停止背景輪詢)")
        chk_enable.set_active(self.config.get("enabled", True))
        outer.pack_start(chk_enable, False, False, 0)

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
        outer.pack_start(freq_box, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Section 2: Panel Icon Appearance
        sec2_lbl = Gtk.Label(xalign=0.0)
        sec2_lbl.set_markup("<b>2. 面板圖示顯示樣式</b>")
        outer.pack_start(sec2_lbl, False, False, 0)

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
        outer.pack_start(rb_badge, False, False, 0)
        outer.pack_start(rb_icontext, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Section 3: NVIDIA / GPU Utilization & Memory-Usage Panel Options
        sec_gpu_lbl = Gtk.Label(xalign=0.0)
        sec_gpu_lbl.set_markup("<b>3. GPU 使用率 (GPU-Util) 與顯存 (Memory-Usage) 面板呈現</b>")
        outer.pack_start(sec_gpu_lbl, False, False, 0)

        chk_gpu_util = Gtk.CheckButton(label="在面板上顯示 Volatile GPU-Util (GPU 核心使用率 %)")
        chk_gpu_util.set_active(self.config.get("show_panel_gpu_util", True))
        outer.pack_start(chk_gpu_util, False, False, 0)

        chk_gpu_mem = Gtk.CheckButton(label="在面板上顯示 Memory-Usage (GPU 顯存使用量)")
        chk_gpu_mem.set_active(self.config.get("show_panel_gpu_mem", True))
        outer.pack_start(chk_gpu_mem, False, False, 0)

        layout_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        layout_box.set_margin_start(12)
        rb_layout_sep = Gtk.RadioButton.new_with_label_from_widget(
            None, "方塊模式佈局 1：獨立方塊 ([CPU 39°] [GPU 41°] [UTL 34%] [MEM 26%])"
        )
        rb_layout_comp = Gtk.RadioButton.new_with_label_from_widget(
            rb_layout_sep, "方塊模式佈局 2：緊湊二合一 ([CPU 39°] [GPU 41° + 底部內嵌雙進度條])"
        )
        if self.config.get("gpu_badge_layout", "separate") == "separate":
            rb_layout_sep.set_active(True)
        else:
            rb_layout_comp.set_active(True)
        layout_box.pack_start(rb_layout_sep, False, False, 0)
        layout_box.pack_start(rb_layout_comp, False, False, 0)
        outer.pack_start(layout_box, False, False, 0)

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
        outer.pack_start(unit_box, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # Section 4: Calculation & Color Thresholds
        sec3_lbl = Gtk.Label(xalign=0.0)
        sec3_lbl.set_markup("<b>4. 感測器計算與溫度警戒色階</b>")
        outer.pack_start(sec3_lbl, False, False, 0)

        chk_cores_only = Gtk.CheckButton(label="CPU 平均溫度僅計算實體核心 (Core 0..N，不含 Package)")
        chk_cores_only.set_active(self.config.get("cpu_avg_cores_only", True))
        outer.pack_start(chk_cores_only, False, False, 0)

        chk_other = Gtk.CheckButton(label="在點選選單中顯示其他感測器 (NVMe SSD / 主機板 / 風扇)")
        chk_other.set_active(self.config.get("show_other_sensors", True))
        outer.pack_start(chk_other, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        lbl_warn = Gtk.Label(label="中度負載變色閾值 (琥珀橘 °C)：", xalign=0.0)
        adj_warn = Gtk.Adjustment(
            value=float(self.config.get("warn_temp_c", 55.0)),
            lower=30.0,
            upper=95.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin_warn = Gtk.SpinButton(adjustment=adj_warn, climb_rate=1.0, digits=0)

        lbl_crit = Gtk.Label(label="高溫警戒變色閾值 (警戒紅 °C)：", xalign=0.0)
        adj_crit = Gtk.Adjustment(
            value=float(self.config.get("crit_temp_c", 75.0)),
            lower=40.0,
            upper=110.0,
            step_increment=1.0,
            page_increment=5.0,
            page_size=0.0,
        )
        spin_crit = Gtk.SpinButton(adjustment=adj_crit, climb_rate=1.0, digits=0)

        grid.attach(lbl_warn, 0, 0, 1, 1)
        grid.attach(spin_warn, 1, 0, 1, 1)
        grid.attach(lbl_crit, 0, 1, 1, 1)
        grid.attach(spin_crit, 1, 1, 1, 1)
        outer.pack_start(grid, False, False, 0)

        # Live apply bindings
        def apply_changes(*_):
            self.config["enabled"] = chk_enable.get_active()
            self.config["interval_sec"] = max(1, int(spin_freq.get_value_as_int()))
            self.config["display_mode"] = "badge" if rb_badge.get_active() else "icon_text"
            self.config["show_panel_gpu_util"] = chk_gpu_util.get_active()
            self.config["show_panel_gpu_mem"] = chk_gpu_mem.get_active()
            self.config["gpu_badge_layout"] = "separate" if rb_layout_sep.get_active() else "compact"
            self.config["gpu_mem_panel_unit"] = "percent" if rb_unit_pct.get_active() else "gib"
            self.config["cpu_avg_cores_only"] = chk_cores_only.get_active()
            self.config["show_other_sensors"] = chk_other.get_active()
            self.config["warn_temp_c"] = float(spin_warn.get_value())
            self.config["crit_temp_c"] = max(
                self.config["warn_temp_c"] + 5.0, float(spin_crit.get_value())
            )
            self._save_config()
            self._sync_timer()
            self.refresh_readings(force_poll=self.config["enabled"])

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
            chk_cores_only,
            chk_other,
        ):
            w_ctrl.connect("toggled", apply_changes)
        for sp_ctrl in (spin_freq, spin_warn, spin_crit):
            sp_ctrl.connect("value-changed", apply_changes)

        # Bottom buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_sensors = Gtk.Button(label="🌡 檢視所有感測器清單")
        btn_sensors.connect("clicked", lambda *_: self.popup_sensor_menu(None))
        btn_close = Gtk.Button(label="關閉")
        btn_close.connect("clicked", lambda *_: win.destroy())
        btn_box.pack_start(btn_sensors, False, False, 0)
        btn_box.pack_end(btn_close, False, False, 0)
        outer.pack_end(btn_box, False, False, 0)

        win.add(outer)
        win.show_all()

    def show_about_dialog(self, *_args):
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("CPU/GPU 溫度與負載監控 Plus")
        dlg.set_version("1.2.0")
        dlg.set_comments(
            "零外部子行程開銷的 MATE 面板 CPU/GPU 溫度與負載監控 Applet。\n"
            "直讀 /sys/class/hwmon 與 NVIDIA NVML (Temperature, Volatile GPU-Util, Memory-Usage, Fan, Power)。"
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
                "設定監控頻率、GPU-Util/顯存顯示樣式與警戒溫度",
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
    if init_size:
        widget.set_panel_size(init_size)

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
    win.set_default_size(520, 145)
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
        "<small>• <b>左鍵點選</b>下方圖示：檢視 CPU 各核心溫度、GPU 溫度、<b>Volatile GPU-Util</b>、<b>Memory-Usage</b>、風扇與功耗\n"
        "• 點選「⚙️ 偏好設定」：自由切換<b>獨立四方塊 ([CPU][GPU][UTL][MEM])</b> 或 <b>緊湊雙進度條方塊</b></small>"
    )
    vbox.pack_start(info_lbl, False, False, 0)

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

    btn_prefs = Gtk.Button(label="⚙️ 偏好設定")
    btn_prefs.connect("clicked", widget.show_preferences_dialog)
    hbox.pack_end(btn_prefs, False, False, 0)

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
