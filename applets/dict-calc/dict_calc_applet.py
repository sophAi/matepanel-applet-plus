#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
MATE Panel Offline StarDict Dictionary & Scientific Calculator Applet (DictCalcApplet)

Features:
- Offline StarDict Dictionary Backend using `/usr/bin/sdcv -n -j` (C++ native speed, 0 MB idle index RAM):
  - Automatically discovers all StarDict dictionaries (.ifo) in `/usr/share/stardict/dic` and `~/.stardict/dic`
  - Dictionary group quick filters (常用群組 / 英漢・漢英 / 日漢・漢日 / 專業科技・英英 / 全部詞典)
  - Full Dictionary Manager inside Preferences to toggle any of the 40+ installed dictionaries
- Built-in Safe Scientific Calculator Engine (Python `ast` evaluator, zero `eval` risk):
  - Auto-detects math expressions (or explicit `=` prefix), supports `^` -> `**`, `pi`, `e`, `ans`,
    `sqrt`, `sin`, `cos`, `tan`, `log`, `log10`, `ln`, `exp`, `abs`, `pow`, `rad`, `deg`, `factorial`, `hex`, `bin`
  - **Auto-copies calculation results to system CLIPBOARD (`Ctrl+V` ready)** by default!
- Two switchable MATE Panel display modes:
  1. 'badge'       : Compact Cairo rounded icon [DICT] with active Scan-Selection green indicator dot
  2. 'panel_entry' : Inline Gtk.Entry search/calc box directly embedded on the MATE Panel
- Scan Selection (滑鼠劃詞查詢):
  - Event-driven `Gdk.SELECTION_PRIMARY` clipboard monitor (0% idle CPU overhead)
  - Optional modifier key requirement (None / Ctrl / Alt / Shift) to prevent accidental popups
  - Instant floating result popup near mouse cursor for both dictionary words AND highlighted math formulas
- Global Hotkeys via `Keybinder 3.0`:
  - Configurable hotkey to summon/focus the Query & Calculator window (default `<Ctrl><Alt>d`)
  - Configurable hotkey to immediately query/calculate the current PRIMARY selection (`Scan Selection` hotkey, default `<Ctrl><Alt>s`)
- Customizable panel font color & badge background style (dark / light / transparent + contrast shadow).
"""

import ast
from collections import OrderedDict
import glob
import html
import json
import math
import operator
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk
import cairo

try:
    gi.require_version("MatePanelApplet", "4.0")
    from gi.repository import MatePanelApplet
    HAS_MATE_PANEL = True
except (ValueError, ImportError):
    HAS_MATE_PANEL = False

try:
    gi.require_version("Keybinder", "3.0")
    from gi.repository import Keybinder
    HAS_KEYBINDER = True
except (ValueError, ImportError):
    HAS_KEYBINDER = False


CONFIG_DIR = os.path.expanduser("~/.config/matepanel-applet-plus")
CONFIG_PATH = os.path.join(CONFIG_DIR, "dict-calc.json")

DICT_SEARCH_DIRS = [
    "/usr/share/stardict/dic",
    os.path.expanduser("~/.stardict/dic"),
]

# Sensible default enabled dictionaries (Chinese-English, Japanese-Chinese, English-English)
DEFAULT_PREFERRED_DICTS = [
    "懶蟲簡明英漢詞典",
    "懶蟲簡明漢英詞典",
    "DrEye4in1詞典",
    "XDICT漢英辭典",
    "XiaoXueTang-jc",
    "XiaoXueTang-cj",
    "DrEye-jc",
    "DrEye-cj",
    "Oxford English Collocation Dictionary",
    "WordNet",
    "英文字根字典",
]

DEFAULT_CONFIG = {
    "display_mode": "panel_entry",       # "panel_entry" (純面板查詢輸入框，預設) or "badge" (精簡方塊圖示)
    "result_output_mode": "window",      # "window" (無焦點精簡結果視窗), "dbus_popup" (系統 D-Bus 通知泡泡), "calc_dbus_dict_window" (混合模式)
    "dbus_popup_style": "separate",      # "separate" (每本詞典獨立泡泡，最多 N 個) or "merged" (單一泡泡以粗體分隔線區隔)
    "dbus_popup_max_dicts": 3,           # 氣泡框最多顯示幾本命中的詞典 (1..5)
    "dbus_popup_timeout_sec": 8,         # 氣泡框停留秒數 (1..60)
    "dbus_popup_font_size": 11,          # 氣泡框字型大小 (pt)
    "dbus_popup_position": "top_center", # "top_center"(置中上方), "top_left"(左上角), "top_right"(右上角), "bottom_right"(右下角), "bottom_left"(左下角)
    "dbus_popup_x_offset": 0,            # 氣泡框水平偏移 (-500..+500 px)
    "dbus_popup_y_offset": 36,           # 氣泡框垂直偏移 (-300..+500 px)
    "dbus_popup_bg_color": "#182234",    # 氣泡框背景顏色
    "dbus_popup_fg_color": "#f0f6ff",    # 氣泡框字形顏色
    "dbus_popup_bg_opacity": 92,         # 氣泡框背景不透明度 (0..100%)
    "panel_entry_width": 170,            # 面板內嵌查詢框寬度 (px)
    "scan_selection_enabled": False,     # 是否啟用滑鼠反白自動劃詞查詢 (Selection Primary)
    "scan_modifier": "ctrl",             # 劃詞觸發修飾鍵: "none", "ctrl", "alt", "shift"
    "google_translate_enabled": True,    # 句子/段落自動改用 Google 翻譯 (雙向翻譯)
    "google_translate_fallback": True,   # 離線詞典查無短片語時自動 Fallback 至 Google 翻譯
    "google_translate_target": "zh-TW",  # 外文翻譯目標語言 ("zh-TW", "zh-CN", "ja", "en")
    "google_translate_reverse": "en",    # 中文反向翻譯目標語言 ("en", "ja")
    "hotkey_open_window": "<Ctrl><Alt>d",# 聚焦面板輸入框 / 呼叫查詢的全域快捷鍵
    "hotkey_scan_selection": "<Ctrl><Alt>s", # 立即查詢目前滑鼠反白文字 (Scan Selection) 的全域快捷鍵
    "calc_auto_copy": True,              # 數學計算模式預設自動將結果存入系統剪貼簿 (可直接 Ctrl+V)
    "active_dict_group": "custom",       # "custom" (已勾選常用), "en_zh", "ja_zh", "sci_en", "all"
    "enabled_dicts": list(DEFAULT_PREFERRED_DICTS),           # 無焦點查詢視窗詞典清單 (含順位)
    "enabled_dicts_dbus": list(DEFAULT_PREFERRED_DICTS[:5]),  # D-Bus 氣泡框詞典清單 (含順位)
    "enabled_dicts_scan": list(DEFAULT_PREFERRED_DICTS[:5]),  # Scan Selection 劃詞詞典清單 (含順位)
    "result_font_size": 11,              # 結果視窗字型大小 (pt)
    "font_color_mode": "dynamic",        # "dynamic" or "custom"
    "custom_font_color": "#ffffff",      # 自訂面板字形顏色
    "badge_bg_style": "dark",            # (舊版相容欄位)
    "badge_bg_color": "#182234",         # 自訂方塊/輸入框背景底色
    "badge_bg_opacity": 90,              # 背景不透明度 (0=完全透明 ~ 100=完全不透明)
    "text_shadow": False,                # 面板文字描邊陰影
}


class DBusNotifier:
    """
    Zero-subprocess system D-Bus notification sender (`org.freedesktop.Notifications`).
    Tracks a list of active notification IDs (`_last_ids`) so consecutive single or multi-bubble
    queries replace previous bubbles in-place and close any leftover extra bubbles without flooding.
    """

    def __init__(self):
        self._proxy = None
        self._last_ids = []

    def _get_proxy(self):
        if self._proxy is None:
            try:
                self._proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SESSION,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    None,
                )
            except Exception:
                self._proxy = None
        return self._proxy

    def _close_id(self, proxy, nid):
        if not nid:
            return
        try:
            proxy.call_sync(
                "CloseNotification",
                GLib.Variant("(u)", (int(nid),)),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
        except Exception:
            pass

    def show_batch(self, items, timeout_sec=8, positions=None):
        """
        Show 1 or more notifications: items = [(summary, body, icon), ...]
        Optional positions = [(x, y), ...] passed via standard Desktop Notifications x/y hints
        so bubbles never overlap mate-panel or the inline search box.
        Reuses previous notification IDs in-place and closes any excess old bubbles.
        """
        proxy = self._get_proxy()
        if proxy is None:
            return False
        timeout_ms = max(1000, int(timeout_sec) * 1000)
        new_ids = []
        for idx, (summary, body, icon) in enumerate(items):
            replace_id = self._last_ids[idx] if idx < len(self._last_ids) else 0
            hints = {}
            if positions is not None and idx < len(positions) and positions[idx] is not None:
                px, py = positions[idx]
                hints["x"] = GLib.Variant("i", int(px))
                hints["y"] = GLib.Variant("i", int(py))
            try:
                args = GLib.Variant(
                    "(susssasa{sv}i)",
                    (
                        "StarDict & Calc Plus",
                        int(replace_id),
                        icon,
                        str(summary),
                        str(body),
                        [],
                        hints,
                        timeout_ms,
                    ),
                )
                res = proxy.call_sync("Notify", args, Gio.DBusCallFlags.NONE, 2000, None)
                nid = int(res.unpack()[0]) if res is not None else 0
                new_ids.append(nid)
            except Exception:
                new_ids.append(0)

        # Close any leftover bubbles from a previous query that had more dictionaries
        for old_id in self._last_ids[len(items):]:
            self._close_id(proxy, old_id)

        self._last_ids = new_ids
        return True

    def show(self, summary, body, icon="accessories-dictionary", timeout_sec=8, position=None):
        positions = [position] if position is not None else None
        return self.show_batch([(summary, body, icon)], timeout_sec=timeout_sec, positions=positions)


class CustomBubbleWindow(Gtk.Window):
    """
    Borderless, focus-free, RGBA-composited overlay notification bubble rendered with Cairo.
    Provides pixel-exact multi-monitor clamping (zero cross-monitor bleed), custom background
    color, custom background opacity (0..100%), custom font color, click-to-dismiss, and
    hover-to-pause auto-dismiss.
    """

    def __init__(self, summary, body_markup, bg_hex, fg_hex, bg_opacity, font_size, on_close_cb=None):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_decorated(False)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.set_resizable(False)

        self._on_close_cb = on_close_cb
        self._bg_rgb = hex_to_rgb(bg_hex, default=(0.094, 0.133, 0.204))
        self._bg_alpha = max(0.0, min(1.0, float(bg_opacity) / 100.0))
        self._fg_hex = fg_hex or "#f0f6ff"
        self._timeout_id = None
        self._timeout_sec = 8
        self._hovered = False

        screen = self.get_screen()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None and screen.is_composited():
                self.set_visual(visual)
        self.set_app_paintable(True)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_click)
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(14)
        box.set_margin_end(14)

        fs = max(8, min(22, int(font_size)))
        title_pango_sz = (fs + 1) * 1024
        body_pango_sz = fs * 1024
        max_chars = max(32, min(52, int(520.0 / max(8.5, fs * 0.9))))

        lbl_title = Gtk.Label(xalign=0.0)
        lbl_title.set_line_wrap(True)
        lbl_title.set_max_width_chars(max_chars)
        lbl_title.set_markup(
            f"<span foreground='{html.escape(self._fg_hex)}' size='{title_pango_sz}'><b>{html.escape(str(summary))}</b></span>"
        )
        box.pack_start(lbl_title, False, False, 0)

        lbl_body = Gtk.Label(xalign=0.0)
        lbl_body.set_line_wrap(True)
        lbl_body.set_max_width_chars(max_chars)
        lbl_body.set_markup(
            f"<span foreground='{html.escape(self._fg_hex)}' size='{body_pango_sz}'>{body_markup}</span>"
        )
        box.pack_start(lbl_body, False, False, 0)

        self.add(box)
        self.show_all()

    def start_timer(self, timeout_sec):
        self._timeout_sec = max(1, int(timeout_sec))
        self._cancel_timer()
        self._timeout_id = GLib.timeout_add(self._timeout_sec * 1000, self._on_timeout)

    def _cancel_timer(self):
        if self._timeout_id is not None:
            try:
                GLib.source_remove(self._timeout_id)
            except Exception:
                pass
            self._timeout_id = None

    def _on_timeout(self):
        self._timeout_id = None
        if not self._hovered:
            self.dismiss()
        return False

    def _on_enter(self, *_args):
        self._hovered = True
        self._cancel_timer()
        return False

    def _on_leave(self, *_args):
        self._hovered = False
        self.start_timer(max(2, min(self._timeout_sec, 4)))
        return False

    def _on_click(self, *_args):
        self.dismiss()
        return True

    def dismiss(self):
        self._cancel_timer()
        if self._on_close_cb is not None:
            cb = self._on_close_cb
            self._on_close_cb = None
            cb(self)
        self.destroy()

    def _on_draw(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)
        ctx.save()
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        r, g, b = self._bg_rgb
        alpha = self._bg_alpha
        if alpha > 0.0:
            draw_rounded_rect(ctx, 0.0, 0.0, max(2.0, w), max(2.0, h), 10.0)
            ctx.set_source_rgba(r, g, b, alpha)
            ctx.fill()
        ctx.restore()
        return False


class CustomBubbleManager:
    """
    Manages one or more CustomBubbleWindow popups with pixel-exact multi-monitor positioning.
    Strictly clamps every bubble inside the active monitor's bounds so it never spills across
    adjacent monitors.
    """

    def __init__(self):
        self._windows = []

    def close_all(self):
        for w in list(self._windows):
            try:
                w.dismiss()
            except Exception:
                pass
        self._windows = []

    def _on_bubble_closed(self, win):
        if win in self._windows:
            self._windows.remove(win)

    def show_batch(
        self,
        items,
        mon_bounds,
        position="top_center",
        offset_x=0,
        offset_y=36,
        bg_color="#182234",
        fg_color="#f0f6ff",
        bg_opacity=92,
        font_size=11,
        timeout_sec=8,
    ):
        self.close_all()
        if not items:
            return False

        mon_left, mon_top, mon_right, mon_bottom = mon_bounds
        mon_w = max(240, mon_right - mon_left)
        mon_h = max(200, mon_bottom - mon_top)
        pad = 12
        stack_gap = 10

        created = []
        for summary, body_markup, _icon in items:
            win = CustomBubbleWindow(
                summary=summary,
                body_markup=body_markup,
                bg_hex=bg_color,
                fg_hex=fg_color,
                bg_opacity=bg_opacity,
                font_size=font_size,
                on_close_cb=self._on_bubble_closed,
            )
            _, nat = win.get_preferred_size()
            bw = max(180, min(nat.width, mon_w - pad * 2))
            bh = max(48, min(nat.height, mon_h - pad * 2))
            win.resize(bw, bh)
            created.append((win, bw, bh))

        is_bottom = position in ("bottom_left", "bottom_right")
        if is_bottom:
            cur_bottom = mon_bottom - offset_y
            for win, bw, bh in created:
                if position == "bottom_left":
                    px = mon_left + pad + offset_x
                else:
                    px = mon_right - bw - pad + offset_x
                px = max(mon_left + pad, min(px, mon_right - bw - pad))
                py = max(mon_top + pad, min(cur_bottom - bh, mon_bottom - bh - pad))
                win.move(int(px), int(py))
                win.start_timer(timeout_sec)
                self._windows.append(win)
                cur_bottom = py - stack_gap
        else:
            cur_y = mon_top + offset_y
            for win, bw, bh in created:
                if position == "top_left":
                    px = mon_left + pad + offset_x
                elif position == "top_right":
                    px = mon_right - bw - pad + offset_x
                else:  # top_center
                    px = mon_left + (mon_w - bw) // 2 + offset_x
                px = max(mon_left + pad, min(px, mon_right - bw - pad))
                py = max(mon_top + pad, min(cur_y, mon_bottom - bh - pad))
                win.move(int(px), int(py))
                win.start_timer(timeout_sec)
                self._windows.append(win)
                cur_y = py + bh + stack_gap

        return True


class GoogleTranslator:
    """
    Zero-dependency, zero-subprocess Google Translate GTX client using Python stdlib `urllib.request`.
    Features:
      - Smart sentence/paragraph detection vs single word/short phrase
      - Bidirectional language routing (non-Chinese -> primary_tl; Chinese -> reverse_tl)
      - POST form submission supporting multi-line paragraphs up to 2,000 chars
      - Thread-safe 20-entry LRU memory cache
    """

    PRIMARY_ENDPOINT = "https://clients5.google.com/translate_a/t"
    FALLBACK_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
    SENTENCE_PUNCT_RE = re.compile(r"[。，！？；：,.!?;:]")
    LANG_LABELS = {
        "zh": "中文",
        "zh-TW": "繁體中文",
        "zh-CN": "簡體中文",
        "en": "英文",
        "ja": "日文",
        "ko": "韓文",
        "fr": "法文",
        "de": "德文",
        "es": "西班牙文",
        "auto": "自動偵測",
    }

    def __init__(self, cache_size=20):
        self._cache = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    @classmethod
    def is_sentence_or_paragraph(cls, text):
        """Return True if text looks like a sentence or paragraph rather than a single word/short phrase."""
        if not text:
            return False
        if "\n" in text:
            return True
        if len(text) >= 40:
            return True
        # Count CJK (Chinese/Japanese/Korean) characters
        cjk_count = sum(
            1
            for ch in text
            if ("\u4e00" <= ch <= "\u9fff")
            or ("\u3040" <= ch <= "\u30ff")
            or ("\uac00" <= ch <= "\ud7af")
        )
        if cjk_count >= 8:
            return True
        # Sentence punctuation with meaningful length
        if len(text) >= 6 and cls.SENTENCE_PUNCT_RE.search(text):
            return True
        # English / whitespace-delimited words >= 3
        words = [w for w in text.split() if w]
        if len(words) >= 3:
            return True
        return False

    @classmethod
    def detect_target_lang(cls, text, primary_tl="zh-TW", reverse_tl="en"):
        """Bidirectional target language selection: Chinese -> reverse_tl, others -> primary_tl."""
        has_kana = any("\u3040" <= ch <= "\u30ff" for ch in text)
        if has_kana:
            return primary_tl
        non_ws = [ch for ch in text if not ch.isspace()]
        if not non_ws:
            return primary_tl
        han_count = sum(1 for ch in non_ws if "\u4e00" <= ch <= "\u9fff")
        ascii_alpha = sum(1 for ch in non_ws if ("a" <= ch.lower() <= "z"))
        is_chinese = (han_count / float(len(non_ws)) >= 0.40) or (han_count >= 2 and ascii_alpha <= 2)
        if is_chinese and primary_tl.startswith("zh"):
            return reverse_tl
        return primary_tl

    @classmethod
    def lang_label(cls, code):
        return cls.LANG_LABELS.get(code, code)

    def translate_sync(self, text, primary_tl="zh-TW", reverse_tl="en", timeout=6.0):
        """
        Translate `text` synchronously. Intended to be called from a background thread.
        Returns: (translated_text, detected_src_lang, target_lang, error_msg)
        """
        tl = self.detect_target_lang(text, primary_tl=primary_tl, reverse_tl=reverse_tl)
        cache_key = (text, tl)
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                tr_text, src_lang = self._cache[cache_key]
                return tr_text, src_lang, tl, None

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        payload = urllib.parse.urlencode({"q": text}).encode("utf-8")
        last_err = None

        # Try primary endpoint (clients5 dict-chrome-ex) first, then fallback to gtx
        endpoints = [
            ("chrome_ex", f"{self.PRIMARY_ENDPOINT}?client=dict-chrome-ex&sl=auto&tl={urllib.parse.quote(tl)}"),
            ("gtx", f"{self.FALLBACK_ENDPOINT}?client=gtx&sl=auto&tl={urllib.parse.quote(tl)}&dt=t"),
        ]
        for ep_kind, url in endpoints:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                translated = ""
                src_lang = "auto"
                if ep_kind == "chrome_ex":
                    # Response format: [["translated text", "src_lang"]] or ["translated text"]
                    if isinstance(data, list) and data:
                        first = data[0]
                        if isinstance(first, list) and first:
                            translated = str(first[0] or "")
                            if len(first) > 1 and first[1]:
                                src_lang = str(first[1])
                        elif isinstance(first, str):
                            translated = first
                else:
                    segments = data[0] if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list) else []
                    translated = "".join(
                        str(seg[0]) for seg in segments if isinstance(seg, list) and seg and seg[0]
                    )
                    if isinstance(data, list) and len(data) > 2 and data[2]:
                        src_lang = str(data[2])

                if translated.strip():
                    if src_lang == tl and any("\u4e00" <= ch <= "\u9fff" for ch in text):
                        src_lang = "zh"
                    with self._lock:
                        self._cache[cache_key] = (translated, src_lang)
                        self._cache.move_to_end(cache_key)
                        while len(self._cache) > self._cache_size:
                            self._cache.popitem(last=False)
                    return translated, src_lang, tl, None
                last_err = "Google 翻譯未回傳譯文"
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP 錯誤 ({exc.code})，請稍後再試"
            except urllib.error.URLError as exc:
                last_err = f"網路連線失敗：{exc.reason}"
            except Exception as exc:
                last_err = f"翻譯失敗：{exc}"

        return None, "auto", tl, last_err or "翻譯失敗"


# =============================================================================
# 1. Safe Scientific Calculator Engine (AST-based, zero eval() risk)
# =============================================================================

class SafeMathCalculator:
    """Safe mathematical expression evaluator supporting scientific functions, `^`, and `ans`."""

    BIN_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.BitXor: operator.xor,
        ast.BitAnd: operator.and_,
        ast.BitOr: operator.or_,
        ast.LShift: operator.lshift,
        ast.RShift: operator.rshift,
    }

    UNARY_OPS = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
        ast.Invert: operator.invert,
    }

    FUNCS = {
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "pow": pow,
        "sqrt": math.sqrt,
        "cbrt": lambda x: math.copysign(abs(x) ** (1.0 / 3.0), x),
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "asin": math.asin,
        "acos": math.acos,
        "atan": math.atan,
        "atan2": math.atan2,
        "sinh": math.sinh,
        "cosh": math.cosh,
        "tanh": math.tanh,
        "log": math.log,
        "ln": math.log,
        "log10": math.log10,
        "log2": math.log2,
        "exp": math.exp,
        "floor": math.floor,
        "ceil": math.ceil,
        "rad": math.radians,
        "deg": math.degrees,
        "radians": math.radians,
        "degrees": math.degrees,
        "factorial": math.factorial,
        "gcd": math.gcd,
        "hex": lambda x: hex(int(x)),
        "bin": lambda x: bin(int(x)),
        "oct": lambda x: oct(int(x)),
    }

    CONSTS = {
        "pi": math.pi,
        "e": math.e,
        "tau": math.tau,
        "inf": math.inf,
    }

    def __init__(self):
        self.last_ans = 0.0
        self.history = []  # list of (expr, formatted_result)

    @staticmethod
    def _looks_like_math(raw_text):
        """
        Heuristic to decide whether user input should be evaluated as a math formula.
        Returns (is_math, cleaned_expr)
        """
        s = raw_text.strip()
        if not s:
            return (False, "")
        # Explicit '=' prefix always triggers calculator
        if s.startswith("="):
            return (True, s[1:].strip())

        # Replace common unicode math symbols
        s_norm = (
            s.replace("×", "*")
            .replace("÷", "/")
            .replace("−", "-")
            .replace("（", "(")
            .replace("）", ")")
            .replace("π", "pi")
        )

        # Pure plain word without digits/operators/parentheses -> dictionary lookup
        if not re.search(r"[0-9+\-*/^%()]", s_norm):
            return (False, "")

        # Single plain number without any operator/function (e.g. "100" or "2026")
        # Wait: allow single hex/bin like 0xff or 0b1010, or expressions with operators/parens
        if re.fullmatch(r"0[xXbBoO][0-9a-fA-F]+", s_norm):
            return (True, s_norm)
        if re.fullmatch(r"[+-]?\d+(\.\d+)?", s_norm):
            return (False, "")

        # Check allowed characters for math expressions
        if not re.fullmatch(r"[0-9a-zA-Z_.,+\-*/^%() \t]+", s_norm):
            return (False, "")

        # Check all identifiers are known math functions, constants, 'ans', or scientific notation 'e'
        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", s_norm)
        allowed_ids = set(SafeMathCalculator.FUNCS.keys()) | set(SafeMathCalculator.CONSTS.keys()) | {"ans"}
        for tok in tokens:
            if tok.lower() in allowed_ids:
                continue
            # Allow hex literals like 0xff or scientific notation like 1e5
            if re.fullmatch(r"[xXoObB][0-9a-fA-F]+", tok):
                continue
            if re.fullmatch(r"[eE]", tok):
                continue
            return (False, "")

        return (True, s_norm)

    def _eval_node(self, node):
        if isinstance(node, ast.Expression):
            return self._eval_node(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Unsupported constant")
        if isinstance(node, ast.Name):
            name = node.id.lower()
            if name == "ans":
                return self.last_ans
            if name in self.CONSTS:
                return self.CONSTS[name]
            raise ValueError(f"Unknown symbol: {node.id}")
        if isinstance(node, ast.UnaryOp):
            op_func = self.UNARY_OPS.get(type(node.op))
            if op_func is None:
                raise ValueError("Unsupported unary operator")
            return op_func(self._eval_node(node.operand))
        if isinstance(node, ast.BinOp):
            op_func = self.BIN_OPS.get(type(node.op))
            if op_func is None:
                raise ValueError("Unsupported binary operator")
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > 10000:
                raise ValueError("Exponent too large")
            return op_func(left, right)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Unsupported call")
            fn_name = node.func.id.lower()
            fn = self.FUNCS.get(fn_name)
            if fn is None:
                raise ValueError(f"Unknown function: {node.func.id}")
            args = [self._eval_node(a) for a in node.args]
            return fn(*args)
        raise ValueError(f"Unsupported expression node: {type(node).__name__}")

    @staticmethod
    def format_result(val):
        if isinstance(val, str):
            return val
        if isinstance(val, bool):
            return str(val)
        if isinstance(val, int):
            return str(val)
        if isinstance(val, float):
            if math.isnan(val) or math.isinf(val):
                return str(val)
            # Clean floating point representation (e.g. 0.30000000000000004 -> 0.3)
            if abs(val - round(val)) < 1e-12 and abs(val) < 1e15:
                return str(int(round(val)))
            return f"{val:.12g}"
        return str(val)

    def try_evaluate(self, raw_text, record_history=False):
        """
        Attempt to evaluate `raw_text` as a mathematical expression.
        Returns (is_math, formatted_result_str, error_str_or_None)
        """
        is_math, expr = self._looks_like_math(raw_text)
        if not is_math or not expr:
            return (False, None, None)

        # Convert '^' to '**' for intuitive power syntax
        expr_py = expr.replace("^", "**")
        # Support implicit multiplication like '2pi' or '3(4+5)'
        expr_py = re.sub(r"(\d)\s*(pi\b|e\b|\()", r"\1*\2", expr_py)
        expr_py = re.sub(r"\)\s*(\d|\()", r")*\1", expr_py)

        try:
            tree = ast.parse(expr_py, mode="eval")
            val = self._eval_node(tree)
            res_str = self.format_result(val)
            if isinstance(val, (int, float)):
                self.last_ans = val
            if record_history:
                self.history.insert(0, (expr, res_str))
                self.history = self.history[:20]
            return (True, res_str, None)
        except Exception as exc:
            return (True, None, str(exc))


# =============================================================================
# 2. StarDict Dictionary Discovery & SDCV Native JSON Query Engine
# =============================================================================

class StarDictManager:
    """Discovers StarDict .ifo dictionaries and queries them asynchronously via `/usr/bin/sdcv -n -j`."""

    SDCV_BIN = "/usr/bin/sdcv"

    def __init__(self):
        self.all_dicts = self.discover_dictionaries()

    @staticmethod
    def _classify_dict(bookname, dirname):
        low = f"{bookname} {dirname}".lower()
        if any(k in low for k in ("-jc", "-cj", "xiaoxuetang", "drey", "naver", "kr")):
            if any(k in low for k in ("-jc", "-cj", "xiaoxuetang")):
                return "ja_zh"
        if any(k in low for k in ("英漢", "漢英", "英汉", "汉英", "lazyworm", "xdict", "drey", "zigen", "字根", "本草")):
            if any(k in low for k in ("科技", "數學", "化學", "物理", "生物", "大氣", "powerword", "ncce")):
                return "sci_en"
            return "en_zh"
        return "sci_en"

    @classmethod
    def discover_dictionaries(cls):
        """
        Scan /usr/share/stardict/dic and ~/.stardict/dic for .ifo files.
        Returns list of dicts: [{'bookname': str, 'wordcount': int, 'group': str, 'path': str}]
        """
        found = []
        seen_names = set()
        for base_dir in DICT_SEARCH_DIRS:
            if not os.path.isdir(base_dir):
                continue
            for ifo_path in sorted(glob.glob(os.path.join(base_dir, "*", "*.ifo"))):
                bookname = None
                wordcount = 0
                try:
                    with open(ifo_path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("bookname="):
                                bookname = line.split("=", 1)[1].strip()
                            elif line.startswith("wordcount="):
                                try:
                                    wordcount = int(line.split("=", 1)[1].strip())
                                except ValueError:
                                    pass
                except OSError:
                    continue
                if bookname and bookname not in seen_names:
                    seen_names.add(bookname)
                    dirname = os.path.basename(os.path.dirname(ifo_path))
                    found.append(
                        {
                            "bookname": bookname,
                            "wordcount": wordcount,
                            "group": cls._classify_dict(bookname, dirname),
                            "path": ifo_path,
                        }
                    )
        found.sort(key=lambda d: (d["group"], d["bookname"]))
        return found

    def get_ordered_enabled_dicts(self, custom_enabled_list):
        """Return enabled dictionary names strictly preserving the user's configured priority order."""
        available = {d["bookname"] for d in self.all_dicts}
        ordered = [name for name in custom_enabled_list if name in available]
        return ordered

    def get_dicts_for_group(self, group_name, custom_enabled_list):
        if group_name == "all":
            return []  # empty means query all dictionaries in sdcv
        if group_name == "custom":
            return self.get_ordered_enabled_dicts(custom_enabled_list)
        return [d["bookname"] for d in self.all_dicts if d["group"] == group_name]

    @classmethod
    def clean_definition(cls, raw_def):
        """Clean up HTML/XML tags occasionally present in Babylon/PowerWord StarDict entries."""
        if not raw_def:
            return ""
        text = raw_def.replace("\r\n", "\n").replace("\r", "\n")
        # Convert <br> / <p> to newlines, strip other HTML tags cleanly
        if "<" in text and ">" in text:
            text = re.sub(r"(?i)<br\s*/?>", "\n", text)
            text = re.sub(r"(?i)</p\s*>", "\n", text)
            text = re.sub(r"<[^>]+>", "", text)
            text = html.unescape(text)
        # Collapse excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def query_sync(self, word, dict_names=None, timeout=6.0):
        """
        Query StarDict via `sdcv -n -j`.
        Returns list of [{'dict': str, 'word': str, 'definition': str}]
        """
        w = word.strip()
        if not w:
            return []
        cmd = [self.SDCV_BIN, "-n", "-j"]
        if dict_names:
            for dn in dict_names:
                cmd.extend(["-u", dn])
        cmd.append(w)

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            out = proc.stdout.strip()
            if not out:
                return []
            # sdcv -j outputs a JSON array (sometimes prefixed by non-JSON lines if warnings occur)
            idx_bracket = out.find("[")
            if idx_bracket < 0:
                return []
            items = json.loads(out[idx_bracket:])
            results = []
            for it in items:
                d_name = it.get("dict", "Unknown")
                w_res = it.get("word", w)
                defn = self.clean_definition(it.get("definition", ""))
                if defn:
                    results.append({"dict": d_name, "word": w_res, "definition": defn})
            # Sort returned entries strictly according to user's dictionary priority order
            if dict_names:
                order_map = {name: i for i, name in enumerate(dict_names)}
                results.sort(key=lambda r: order_map.get(r["dict"], 999999))
            return results
        except Exception:
            return []


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


# =============================================================================
# 3. Query & Calculator Popup Window (Shared by Manual Query & Scan Selection)
# =============================================================================

class DictCalcPopupWindow(Gtk.Window):
    """
    Focus-free Compact Dictionary & Scientific Calculator Result Window.
    - No inner search entry: all keyboard input stays locked in the MATE Panel search box!
    - `set_accept_focus(False)` & `set_focus_on_map(False)` ensure showing results NEVER steals focus.
    - Supports instant math formula results (auto-copied to system clipboard!) & ordered StarDict results.
    """

    def __init__(self, parent_widget):
        super().__init__(title="📖 StarDict 查詢結果")
        self.parent_widget = parent_widget
        self.set_default_size(430, 400)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_keep_above(True)
        self.connect("delete-event", self._on_delete_hide)

        self._last_calc_result = None
        self._build_ui()

    def _on_delete_hide(self, *_args):
        self.hide()
        return True

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(10)
        root.set_margin_end(10)

        # Compact Header Bar (Query title + tiny action buttons; NO search entry!)
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.query_title_lbl = Gtk.Label(xalign=0.0)
        self.query_title_lbl.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.query_title_lbl.set_markup("<b>📖 StarDict &amp; 🧮 計算機結果</b>")
        hdr_box.pack_start(self.query_title_lbl, True, True, 0)

        self.chk_scan = Gtk.ToggleButton(label="✂")
        self.chk_scan.set_can_focus(False)
        self.chk_scan.set_tooltip_text("開關滑鼠反白自動劃詞查詢 (Scan Selection)")
        self.chk_scan.set_active(self.parent_widget.config.get("scan_selection_enabled", False))
        self.chk_scan.connect("toggled", self._on_scan_toggled)
        hdr_box.pack_start(self.chk_scan, False, False, 0)

        btn_prefs = Gtk.Button(label="⚙️")
        btn_prefs.set_can_focus(False)
        btn_prefs.set_tooltip_text("偏好設定（詞典順位 / 字型大小 / 顯示方式 / 快捷鍵）")
        btn_prefs.connect("clicked", lambda *_: self.parent_widget.show_preferences_dialog())
        hdr_box.pack_start(btn_prefs, False, False, 0)

        btn_close = Gtk.Button(label="✖")
        btn_close.set_can_focus(False)
        btn_close.set_tooltip_text("隱藏結果視窗 (或於面板輸入框按 Esc)")
        btn_close.connect("clicked", lambda *_: self.hide())
        hdr_box.pack_start(btn_close, False, False, 0)

        root.pack_start(hdr_box, False, False, 0)

        # Highlighted Calculator Result Banner (auto-shown when formula evaluated)
        self.calc_frame = Gtk.Frame()
        self.calc_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        calc_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        calc_hbox.set_margin_top(6)
        calc_hbox.set_margin_bottom(6)
        calc_hbox.set_margin_start(10)
        calc_hbox.set_margin_end(10)

        self.calc_result_lbl = Gtk.Label(xalign=0.0)
        self.calc_result_lbl.set_selectable(True)
        calc_hbox.pack_start(self.calc_result_lbl, True, True, 0)

        self.btn_copy_calc = Gtk.Button(label="📋 複製")
        self.btn_copy_calc.set_can_focus(False)
        self.btn_copy_calc.connect("clicked", self._on_copy_calc_clicked)
        calc_hbox.pack_end(self.btn_copy_calc, False, False, 0)

        self.calc_frame.add(calc_hbox)
        root.pack_start(self.calc_frame, False, False, 0)

        # Scrollable Dictionary Results TextView
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self.result_view = Gtk.TextView()
        self.result_view.set_editable(False)
        self.result_view.set_cursor_visible(False)
        self.result_view.set_can_focus(False)
        self.result_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.result_view.set_left_margin(8)
        self.result_view.set_right_margin(8)
        self.result_view.set_top_margin(6)
        self.result_view.set_bottom_margin(6)
        self.result_buf = self.result_view.get_buffer()
        self._init_text_tags()

        scroll.add(self.result_view)
        root.pack_start(scroll, True, True, 0)

        # Status Footer
        self.status_lbl = Gtk.Label(xalign=0.0)
        self._update_status_footer()
        root.pack_start(self.status_lbl, False, False, 0)

        self.add(root)
        self.show_all()
        self.calc_frame.hide()

    def _init_text_tags(self):
        buf = self.result_buf
        fs = int(self.parent_widget.config.get("result_font_size", 11))
        self.tag_dict_hdr = buf.create_tag(
            "dict_hdr",
            weight=800,
            foreground="#38bdf8",
            size_points=fs + 2.0,
            pixels_above_lines=8,
            pixels_below_lines=3,
        )
        self.tag_word = buf.create_tag(
            "word_title",
            weight=700,
            foreground="#fbbf24",
            size_points=fs + 1.0,
        )
        self.tag_body = buf.create_tag(
            "body_text",
            size_points=fs,
            pixels_below_lines=2,
        )
        self.tag_muted = buf.create_tag(
            "muted",
            foreground="#94a3b8",
            size_points=max(8.5, fs - 1.0),
        )

    def update_font_size(self):
        fs = int(self.parent_widget.config.get("result_font_size", 11))
        self.tag_dict_hdr.set_property("size-points", fs + 1.5)
        self.tag_word.set_property("size-points", fs + 1.0)
        self.tag_body.set_property("size-points", fs)
        self.tag_muted.set_property("size-points", max(8.5, fs - 1.0))

    def _update_status_footer(self, extra_msg=""):
        hk_open = self.parent_widget.config.get("hotkey_open_window", "<Ctrl><Alt>d")
        hk_scan = self.parent_widget.config.get("hotkey_scan_selection", "<Ctrl><Alt>s")
        n_en = len(self.parent_widget.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS))
        msg = extra_msg if extra_msg else (
            f"已啟用 {n_en} 套詞典 | 聚焦:{hk_open} | 劃詞:{hk_scan} | Esc 隱藏"
        )
        self.status_lbl.set_markup(f"<small><span foreground='#94a3b8'>{html.escape(msg)}</span></small>")

    def sync_controls(self):
        self.chk_scan.set_active(self.parent_widget.config.get("scan_selection_enabled", False))
        self.update_font_size()
        self._update_status_footer()

    def _on_scan_toggled(self, chk):
        self.parent_widget.set_scan_selection_enabled(chk.get_active())

    def _on_copy_calc_clicked(self, *_args):
        if self._last_calc_result:
            self.parent_widget.copy_to_clipboard(self._last_calc_result)
            self._update_status_footer(f"✅ 已將「{self._last_calc_result}」存入剪貼簿 (Ctrl+V 可直接貼上)")

    def position_and_show(self, near_pointer=False):
        self.sync_controls()
        if near_pointer:
            display = Gdk.Display.get_default()
            if display is not None:
                seat = display.get_default_seat()
                if seat is not None and seat.get_pointer() is not None:
                    _, px, py = seat.get_pointer().get_position()
                    self.move(max(10, px - 100), max(36, py + 18))
        else:
            # Position cleanly near the MATE Panel search box if realized
            gdk_win = self.parent_widget.get_window()
            if gdk_win is not None:
                _, ox, oy = gdk_win.get_origin()
                alloc = self.parent_widget.get_allocation()
                screen_h = Gdk.Screen.get_default().get_height() if Gdk.Screen.get_default() else 1080
                win_h = self.get_size()[1] or 400
                if oy + alloc.height + win_h + 10 < screen_h:
                    self.move(max(10, ox), oy + alloc.height + 4)
                else:
                    self.move(max(10, ox), max(10, oy - win_h - 4))
            else:
                self.set_position(Gtk.WindowPosition.CENTER)

        self.show_all()
        if not self._last_calc_result:
            self.calc_frame.hide()

    def show_loading(self, q, near_pointer=False, is_translate=False):
        short_q = q.replace("\n", " ")
        if len(short_q) > 28:
            short_q = short_q[:26] + "…"
        if is_translate:
            self.query_title_lbl.set_markup(f"<b>🌐 Google 翻譯：{html.escape(short_q)}</b>")
            loading_msg = "🌐 正在使用 Google 翻譯中..."
        else:
            self.query_title_lbl.set_markup(f"<b>📖 查詢：{html.escape(short_q)}</b>")
            loading_msg = f"🔎 正在查詢「{short_q}」..."
        self.calc_frame.hide()
        self._last_calc_result = None
        self.result_buf.set_text("")
        self.result_buf.insert_with_tags(
            self.result_buf.get_end_iter(), loading_msg, self.tag_muted
        )
        self.position_and_show(near_pointer=near_pointer)

    def show_calc_result(self, expr_str, res_str, auto_copied, err=None, near_pointer=False):
        self.query_title_lbl.set_markup(f"<b>🧮 計算：{html.escape(expr_str)}</b>")
        if err is not None:
            self._last_calc_result = None
            self.calc_result_lbl.set_markup(
                f"🧮 <span foreground='#f87171'>算式語法錯誤：{html.escape(err)}</span>"
            )
            self.calc_frame.show_all()
            self.position_and_show(near_pointer=near_pointer)
            return

        self._last_calc_result = res_str
        copied_badge = (
            "   <span foreground='#34d399'><small>✔ 已存入剪貼簿 (Ctrl+V)</small></span>"
            if auto_copied
            else ""
        )
        self.calc_result_lbl.set_markup(
            f"🧮 <tt>{html.escape(expr_str)}</tt> = "
            f"<span size='x-large' foreground='#34d399'><b>{html.escape(res_str)}</b></span>"
            f"{copied_badge}"
        )
        self.calc_frame.show_all()

        buf = self.result_buf
        buf.set_text("")
        buf.insert_with_tags(buf.get_end_iter(), "🧮 科學計算機模式\n", self.tag_dict_hdr)
        buf.insert_with_tags(
            buf.get_end_iter(),
            f"算式：{expr_str}\n結果：{res_str}\n"
            + ("狀態：✔ 已自動存入系統剪貼簿，可直接按 Ctrl+V 貼上\n\n" if auto_copied else "\n"),
            self.tag_body,
        )
        if self.parent_widget.calc.history:
            buf.insert_with_tags(buf.get_end_iter(), "📜 最近計算歷史紀錄：\n", self.tag_word)
            for h_expr, h_res in self.parent_widget.calc.history[:10]:
                buf.insert_with_tags(
                    buf.get_end_iter(), f"  • {h_expr}  =  {h_res}\n", self.tag_body
                )
        self._update_status_footer(
            f"🧮 {expr_str} = {res_str}" + ("（已自動複製）" if auto_copied else "")
        )
        self.position_and_show(near_pointer=near_pointer)

    def show_dict_results(self, query_word, results, near_pointer=False):
        self.query_title_lbl.set_markup(f"<b>📖 查詢：{html.escape(query_word)}</b>")
        self.calc_frame.hide()
        self._last_calc_result = None

        buf = self.result_buf
        buf.set_text("")

        if not results:
            buf.insert_with_tags(
                buf.get_end_iter(),
                f"找不到「{query_word}」的詞條釋義。\n\n"
                f"💡 建議：\n"
                f"• 點選右上角「⚙️ 偏好設定 → 詞典管理」勾選更多詞典\n"
                f"• 若要進行數學計算，可在算式最前面加上「=」（例如 =2^10）\n",
                self.tag_muted,
            )
            self._update_status_footer(f"查詢「{query_word}」：0 筆結果")
            self.position_and_show(near_pointer=near_pointer)
            return

        for idx, item in enumerate(results):
            if idx > 0:
                buf.insert_with_tags(buf.get_end_iter(), "\n" + ("─" * 38) + "\n", self.tag_muted)
            buf.insert_with_tags(
                buf.get_end_iter(), f"📖 {item['dict']}\n", self.tag_dict_hdr
            )
            buf.insert_with_tags(
                buf.get_end_iter(), f"【{item['word']}】\n", self.tag_word
            )
            buf.insert_with_tags(
                buf.get_end_iter(), f"{item['definition']}\n", self.tag_body
            )

        self.result_view.scroll_to_iter(buf.get_start_iter(), 0.0, False, 0.0, 0.0)
        self._update_status_footer(f"查詢「{query_word}」：共找到 {len(results)} 筆詞典釋義")
        self.position_and_show(near_pointer=near_pointer)

    def show_translate_result(
        self,
        original_text,
        translated_text,
        src_lang,
        target_lang,
        err=None,
        is_fallback=False,
        near_pointer=False,
    ):
        short_q = original_text.replace("\n", " ")
        if len(short_q) > 26:
            short_q = short_q[:24] + "…"
        self.query_title_lbl.set_markup(f"<b>🌐 Google 翻譯：{html.escape(short_q)}</b>")

        buf = self.result_buf
        buf.set_text("")

        if err is not None or not translated_text:
            self._last_calc_result = None
            self.calc_frame.hide()
            buf.insert_with_tags(
                buf.get_end_iter(),
                f"⚠️ Google 翻譯失敗：{err or '無回傳內容'}\n\n",
                self.tag_word,
            )
            buf.insert_with_tags(buf.get_end_iter(), f"原文：\n{original_text}\n", self.tag_muted)
            self._update_status_footer("🌐 Google 翻譯失敗")
            self.position_and_show(near_pointer=near_pointer)
            return

        src_lbl = GoogleTranslator.lang_label(src_lang)
        tgt_lbl = GoogleTranslator.lang_label(target_lang)
        fb_note = " (離線無結果自動切換)" if is_fallback else ""

        self._last_calc_result = translated_text
        self.calc_result_lbl.set_markup(
            f"🌐 <b>Google 翻譯</b>  <span foreground='#38bdf8'>{html.escape(src_lbl)} → {html.escape(tgt_lbl)}</span>"
            f"<small><span foreground='#94a3b8'>{html.escape(fb_note)}</span></small>"
        )
        self.calc_frame.show_all()

        buf.insert_with_tags(buf.get_end_iter(), f"🌐 譯文 ({tgt_lbl})\n", self.tag_dict_hdr)
        buf.insert_with_tags(buf.get_end_iter(), f"{translated_text}\n", self.tag_body)
        buf.insert_with_tags(buf.get_end_iter(), "\n" + ("─" * 38) + "\n", self.tag_muted)
        buf.insert_with_tags(buf.get_end_iter(), f"📝 原文 ({src_lbl})\n", self.tag_word)
        buf.insert_with_tags(buf.get_end_iter(), f"{original_text}\n", self.tag_muted)

        self.result_view.scroll_to_iter(buf.get_start_iter(), 0.0, False, 0.0, 0.0)
        self._update_status_footer(
            f"🌐 Google 翻譯 ({src_lbl} → {tgt_lbl}) | 共 {len(original_text)} 字 | 點右上 📋 複製譯文"
        )
        self.position_and_show(near_pointer=near_pointer)


# =============================================================================
# 4. Main MATE Panel Applet Widget (Badge / Inline Panel Entry + Hotkeys + Scan)
# =============================================================================

class DictCalcWidget(Gtk.EventBox):
    """
    Main widget placed inside either a MatePanelApplet or a standalone Gtk.Window.
    """

    FONT_COLOR_PRESETS = [
        ("純白 (深色面板)", "#ffffff"),
        ("炭黑 (淺色面板)", "#111827"),
        ("科技青", "#38bdf8"),
        ("翠綠", "#34d399"),
        ("琥珀金", "#fbbf24"),
    ]

    def __init__(self, applet=None):
        super().__init__()
        self.applet = applet
        # IMPORTANT: Do NOT set_above_child(True) on the EventBox so child Gtk.Entry receives clicks & focus directly!
        self.set_visible_window(False)
        self.set_above_child(False)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )

        self.config = self._load_config()
        self.dict_mgr = StarDictManager()
        self.calc = SafeMathCalculator()
        self.dbus_notifier = DBusNotifier()
        self.bubble_mgr = CustomBubbleManager()
        self.translator = GoogleTranslator()

        self.panel_size = 28
        self.popup_win = None
        self.prefs_win = None
        self.active_menu = None
        self._bound_hotkeys = []
        self._last_scanned_text = ""
        self._query_seq = 0

        # Container supporting both 'badge' mode and 'panel_entry' inline search box mode
        self.root_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.add(self.root_box)

        # 1. Cairo Badge Canvas (set_no_show_all so applet.show_all() never forces both Mode A & B visible)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_no_show_all(True)
        self.canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self.canvas.connect("draw", self._on_draw_badge)
        self.canvas.connect("button-press-event", self._on_button_press)
        self.root_box.pack_start(self.canvas, False, False, 0)

        # 2. Pure Inline Panel Search/Calc Entry (click-to-type with request_focus + left scan icon + right-click menu)
        self.panel_entry = Gtk.Entry()
        self.panel_entry.set_no_show_all(True)
        self.panel_entry.set_name("dict-calc-panel-entry")
        self.entry_css_provider = Gtk.CssProvider()
        self.panel_entry.get_style_context().add_provider(
            self.entry_css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        self.panel_entry.set_placeholder_text("查詞 / 算式...")
        self.panel_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.PRIMARY, "edit-find-symbolic"
        )
        self.panel_entry.set_icon_tooltip_text(
            Gtk.EntryIconPosition.PRIMARY, "點此開關滑鼠劃詞查詢 (Scan Selection)"
        )
        self.panel_entry.connect("activate", self._on_panel_entry_activate)
        self.panel_entry.connect("button-press-event", self._on_panel_entry_button_press)
        self.panel_entry.connect("icon-press", self._on_panel_entry_icon_press)
        self.panel_entry.connect("populate-popup", self._on_panel_entry_populate_popup)
        self.panel_entry.connect("key-press-event", self._on_panel_entry_key_press)
        self.root_box.pack_start(self.panel_entry, False, False, 0)

        self.connect("button-press-event", self._on_button_press)
        self.connect("destroy", self._on_destroy)

        # Clipboards: CLIPBOARD (Ctrl+V) and PRIMARY (Mouse Scan Selection)
        self._scan_poll_id = None
        self._scan_debounce_id = None
        self._scan_pending_retries = 0
        self.clipboard_main = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clipboard_primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
        self.clipboard_primary.connect("owner-change", self._on_primary_selection_changed)
        # Seed _last_scanned_text silently on startup so stale PRIMARY doesn't auto-popup on reload
        self.clipboard_primary.request_text(self._on_seed_primary_text, None)
        self._sync_scan_poll_timer()

        # Initialize Keybinder global hotkeys
        if HAS_KEYBINDER:
            try:
                Keybinder.init()
            except Exception:
                pass
        self.rebind_global_hotkeys()

        self._apply_panel_layout()
        self._update_tooltip()
        self.show_all()
        self._sync_entry_visibility()

    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        user_cfg = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    user_cfg = loaded
                    cfg.update(user_cfg)
            except Exception:
                pass
        # Backward-compatible migration: if separate D-Bus / Scan profiles aren't saved yet,
        # inherit from the user's existing enabled_dicts list.
        if "enabled_dicts_dbus" not in user_cfg and "enabled_dicts" in user_cfg:
            cfg["enabled_dicts_dbus"] = list(cfg["enabled_dicts"])
        if "enabled_dicts_scan" not in user_cfg and "enabled_dicts" in user_cfg:
            cfg["enabled_dicts_scan"] = list(cfg["enabled_dicts"])
        # Backward-compatible migration for legacy badge_bg_style -> custom color + opacity
        if "badge_bg_color" not in user_cfg and "badge_bg_opacity" not in user_cfg and "badge_bg_style" in user_cfg:
            legacy_bg = user_cfg.get("badge_bg_style", "dark")
            if legacy_bg == "light":
                cfg["badge_bg_color"] = "#f2f5fa"
                cfg["badge_bg_opacity"] = 92
            elif legacy_bg == "transparent":
                cfg["badge_bg_color"] = "#182234"
                cfg["badge_bg_opacity"] = 0
            else:
                cfg["badge_bg_color"] = "#182234"
                cfg["badge_bg_opacity"] = 90
        return cfg

    def _save_config(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def copy_to_clipboard(self, text):
        """Copy text to system CLIPBOARD so user can immediately paste with Ctrl+V."""
        if not text:
            return
        self.clipboard_main.set_text(str(text), -1)
        self.clipboard_main.store()

    # =========================================================================
    # Global Hotkeys (Keybinder 3.0)
    # =========================================================================

    def rebind_global_hotkeys(self):
        if not HAS_KEYBINDER:
            return
        for old_hk in self._bound_hotkeys:
            try:
                Keybinder.unbind(old_hk)
            except Exception:
                pass
        self._bound_hotkeys = []

        hk_open = self.config.get("hotkey_open_window", "").strip()
        if hk_open:
            try:
                if Keybinder.bind(hk_open, self._on_hotkey_open_window):
                    self._bound_hotkeys.append(hk_open)
            except Exception:
                pass

        hk_scan = self.config.get("hotkey_scan_selection", "").strip()
        if hk_scan and hk_scan != hk_open:
            try:
                if Keybinder.bind(hk_scan, self._on_hotkey_scan_selection):
                    self._bound_hotkeys.append(hk_scan)
            except Exception:
                pass

    def _on_hotkey_open_window(self, *_args):
        GLib.idle_add(self.focus_panel_search_box)

    def _on_hotkey_scan_selection(self, *_args):
        GLib.idle_add(self.trigger_scan_selection_now)

    def focus_panel_search_box(self, timestamp=None):
        """Force keyboard focus directly into the MATE Panel search box so the cursor stays there."""
        if self.config.get("display_mode", "panel_entry") != "panel_entry":
            # Ensure inline entry is visible when focusing via hotkey
            self.config["display_mode"] = "panel_entry"
            self._save_config()
            self._apply_panel_layout()
        ts = timestamp if timestamp is not None else Gdk.CURRENT_TIME
        if self.applet is not None:
            try:
                self.applet.request_focus(ts)
            except Exception:
                pass
        self.panel_entry.grab_focus()
        self.panel_entry.select_region(0, -1)

    # =========================================================================
    # Scan Selection (Mouse Highlight PRIMARY Clipboard Lookup)
    # =========================================================================

    def set_scan_selection_enabled(self, enabled):
        self.config["scan_selection_enabled"] = bool(enabled)
        self._save_config()
        self._sync_scan_poll_timer()
        self._update_entry_icons()
        self._update_tooltip()
        self.canvas.queue_draw()

    def toggle_scan_selection(self, *_args):
        self.set_scan_selection_enabled(not self.config.get("scan_selection_enabled", False))

    def _on_seed_primary_text(self, _clipboard, text, *_args):
        if text:
            self._last_scanned_text = text.strip()

    def _get_pointer_modifier_state(self):
        """Return current Gdk.ModifierType bitmask of pointer/keyboard modifiers."""
        display = Gdk.Display.get_default()
        if display is None:
            return 0
        seat = display.get_default_seat()
        if seat is None or seat.get_pointer() is None:
            return 0
        root_win = Gdk.get_default_root_window()
        if root_win is None:
            return 0
        _, _, _, state = root_win.get_device_position(seat.get_pointer())
        return state

    def _is_scan_modifier_pressed(self, state=None):
        req_mod = self.config.get("scan_modifier", "ctrl")
        if req_mod == "none":
            return True
        if state is None:
            state = self._get_pointer_modifier_state()
        if req_mod == "ctrl":
            return bool(state & Gdk.ModifierType.CONTROL_MASK)
        if req_mod == "alt":
            return bool(state & Gdk.ModifierType.MOD1_MASK)
        if req_mod == "shift":
            return bool(state & Gdk.ModifierType.SHIFT_MASK)
        return True

    def _sync_scan_poll_timer(self):
        """
        Run a lightweight 300ms monitor ONLY when Scan Selection is enabled.
        Fixes the classic X11 PRIMARY bug where Chromium/Firefox/VS Code/Terminal do not
        re-emit `owner-change` when selecting new text inside the same window, and allows
        highlighting text first then tapping Ctrl/Alt/Shift after releasing the mouse.
        """
        want = bool(self.config.get("scan_selection_enabled", False))
        if want and self._scan_poll_id is None:
            self._scan_poll_id = GLib.timeout_add(300, self._on_scan_poll_tick)
        elif not want and self._scan_poll_id is not None:
            try:
                GLib.source_remove(self._scan_poll_id)
            except Exception:
                pass
            self._scan_poll_id = None

    def _on_scan_poll_tick(self):
        if not self.config.get("scan_selection_enabled", False):
            self._scan_poll_id = None
            return False
        state = self._get_pointer_modifier_state()
        # Wait until left mouse button is released so we never scan half-dragged text
        if state & Gdk.ModifierType.BUTTON1_MASK:
            return True
        if not self._is_scan_modifier_pressed(state):
            return True
        self.clipboard_primary.request_text(self._on_primary_text_received, False)
        return True

    def _on_primary_selection_changed(self, _clipboard, _event):
        if not self.config.get("scan_selection_enabled", False):
            return
        # Debounce owner-change until mouse drag finishes (BUTTON1 released)
        if self._scan_debounce_id is not None:
            try:
                GLib.source_remove(self._scan_debounce_id)
            except Exception:
                pass
        self._scan_pending_retries = 0
        self._scan_debounce_id = GLib.timeout_add(120, self._on_primary_debounced_check)

    def _on_primary_debounced_check(self):
        self._scan_debounce_id = None
        if not self.config.get("scan_selection_enabled", False):
            return False
        state = self._get_pointer_modifier_state()
        if state & Gdk.ModifierType.BUTTON1_MASK:
            self._scan_pending_retries += 1
            if self._scan_pending_retries < 25:
                self._scan_debounce_id = GLib.timeout_add(120, self._on_primary_debounced_check)
            return False
        if not self._is_scan_modifier_pressed(state):
            return False
        self.clipboard_primary.request_text(self._on_primary_text_received, False)
        return False

    def trigger_scan_selection_now(self, *_args):
        """Triggered via global Scan Selection hotkey (works even if auto-scan is OFF)."""
        self.clipboard_primary.request_text(self._on_primary_text_received, True)
        return False

    def _on_primary_text_received(self, _clipboard, text, force_from_hotkey):
        if not text:
            if force_from_hotkey:
                # Fallback to main CLIPBOARD if PRIMARY is empty
                txt2 = self.clipboard_main.wait_for_text() or ""
                text = txt2
        cleaned = (text or "").strip()
        if not cleaned:
            if force_from_hotkey:
                self.open_popup_window("")
            return
        # Allow multi-line sentences & paragraphs up to 2,000 chars for Google Translate;
        # if Google Translate is disabled, keep strict single-line word limit (160 chars).
        gt_enabled = bool(self.config.get("google_translate_enabled", True))
        if not gt_enabled:
            if len(cleaned) > 160 or "\n" in cleaned:
                if force_from_hotkey:
                    self.open_popup_window("")
                return
        else:
            if len(cleaned) > 2000:
                cleaned = cleaned[:2000].rstrip()
        if not force_from_hotkey and cleaned == self._last_scanned_text:
            return
        self._last_scanned_text = cleaned
        self.open_popup_window(cleaned, near_pointer=True, source="scan")

    # =========================================================================
    # Panel Layout, Drawing & Events
    # =========================================================================

    def set_panel_size(self, size):
        self.panel_size = max(22, min(64, int(size)))
        self._apply_panel_layout()

    def _update_entry_icons(self):
        scan_on = self.config.get("scan_selection_enabled", False)
        if scan_on:
            self.panel_entry.set_icon_from_icon_name(
                Gtk.EntryIconPosition.PRIMARY, "edit-cut-symbolic"
            )
            self.panel_entry.set_icon_tooltip_text(
                Gtk.EntryIconPosition.PRIMARY, "✂ 滑鼠劃詞查詢已開啟（點此關閉）"
            )
            self.panel_entry.set_placeholder_text("✂ 查詞 / 算式...")
        else:
            self.panel_entry.set_icon_from_icon_name(
                Gtk.EntryIconPosition.PRIMARY, "edit-find-symbolic"
            )
            self.panel_entry.set_icon_tooltip_text(
                Gtk.EntryIconPosition.PRIMARY, "🔎 點此開啟滑鼠劃詞查詢 (Scan Selection)"
            )
            self.panel_entry.set_placeholder_text("查詞 / 算式...")

    def _sync_entry_visibility(self):
        if self.config.get("display_mode", "badge") == "panel_entry":
            # Pure Inline Entry Mode: hide the left [DICT] badge and show only the compact Entry box
            self.canvas.hide()
            self.panel_entry.show()
            self._update_entry_icons()
        else:
            self.panel_entry.hide()
            self.canvas.show()

    def _apply_entry_css(self):
        """Apply custom badge_bg_color, badge_bg_opacity, custom_font_color, and text_shadow to the inline Gtk.Entry."""
        bg_hex = self.config.get("badge_bg_color", "#182234")
        opacity = max(0, min(100, int(self.config.get("badge_bg_opacity", 90))))
        alpha = opacity / 100.0
        bg_rgb = hex_to_rgb(bg_hex, default=(0.094, 0.133, 0.204))
        r255 = int(round(bg_rgb[0] * 255))
        g255 = int(round(bg_rgb[1] * 255))
        b255 = int(round(bg_rgb[2] * 255))
        is_light_bg = opacity >= 30 and rgb_luminance(bg_rgb) > 0.55

        font_mode = self.config.get("font_color_mode", "dynamic")
        custom_hex = self.config.get("custom_font_color", "#ffffff")
        shadow_on = bool(self.config.get("text_shadow", False))

        border_col = "rgba(56, 130, 210, 0.78)" if is_light_bg else "rgba(72, 184, 250, 0.78)"
        if opacity == 0:
            bg_css = (
                f"background-color: transparent; background-image: none; "
                f"border: 1px solid {border_col}; box-shadow: none;"
            )
            default_fg = "#ffffff"
        else:
            bg_css = (
                f"background-color: rgba({r255}, {g255}, {b255}, {alpha:.2f}); "
                f"background-image: none; border: 1px solid {border_col}; box-shadow: none;"
            )
            default_fg = "#111827" if is_light_bg else "#f0f6ff"

        fg_hex = custom_hex if font_mode == "custom" else default_fg
        fg_rgb = hex_to_rgb(fg_hex, default=(1.0, 1.0, 1.0))
        shadow_css = ""
        if shadow_on:
            sh_col = "rgba(0, 0, 0, 0.88)" if rgb_luminance(fg_rgb) > 0.45 else "rgba(255, 255, 255, 0.88)"
            shadow_css = f"text-shadow: 0 0 2px {sh_col}, 1px 1px 1px {sh_col};"
        else:
            shadow_css = "text-shadow: none;"

        css_str = (
            f"#dict-calc-panel-entry {{\n"
            f"  {bg_css}\n"
            f"  color: {fg_hex};\n"
            f"  caret-color: {fg_hex};\n"
            f"  border-radius: 4px;\n"
            f"  padding-top: 1px;\n"
            f"  padding-bottom: 1px;\n"
            f"  min-height: 0px;\n"
            f"  {shadow_css}\n"
            f"}}\n"
            f"#dict-calc-panel-entry image {{\n"
            f"  color: {fg_hex};\n"
            f"}}\n"
        )
        try:
            self.entry_css_provider.load_from_data(css_str.encode("utf-8"))
        except Exception:
            pass

    def _apply_panel_layout(self):
        h = max(24, self.panel_size)
        bw = max(34, int(h * 1.25))
        self.canvas.set_size_request(bw, h)
        entry_w = max(90, min(320, int(self.config.get("panel_entry_width", 170))))
        self.panel_entry.set_size_request(entry_w, max(20, h - 4))
        self._apply_entry_css()
        self._sync_entry_visibility()
        self.canvas.queue_draw()

    def _on_panel_entry_button_press(self, entry, event):
        """Request X11 keyboard focus from mate-panel so clicking the inline entry lets you type immediately!"""
        if event.button == 1:
            if self.applet is not None:
                try:
                    self.applet.request_focus(event.time)
                except Exception:
                    pass
            entry.grab_focus()
        elif event.button == 2:
            # Middle click toggles Scan Selection
            self.toggle_scan_selection()
            return True
        return False

    def _on_panel_entry_key_press(self, entry, event):
        if event.keyval == Gdk.KEY_Escape:
            entry.set_text("")
            if self.popup_win is not None and self.popup_win.is_visible():
                self.popup_win.hide()
            self.bubble_mgr.close_all()
            return True
        return False

    def _on_panel_entry_icon_press(self, entry, icon_pos, event):
        if icon_pos == Gtk.EntryIconPosition.PRIMARY:
            # Left icon toggles Scan Selection
            self.toggle_scan_selection()

    def _on_panel_entry_populate_popup(self, _entry, menu):
        """Integrate Applet controls (Preferences, Scan Selection, Open Window) into the Entry right-click menu."""
        menu.prepend(Gtk.SeparatorMenuItem())

        mi_prefs = Gtk.MenuItem(label="⚙️ 偏好設定與詞典順位管理...")
        mi_prefs.connect("activate", lambda *_: self.show_preferences_dialog())
        menu.prepend(mi_prefs)

        scan_on = self.config.get("scan_selection_enabled", False)
        mi_scan_chk = Gtk.CheckMenuItem(label="✂ 啟用滑鼠劃詞查詢 (Scan Selection)")
        mi_scan_chk.set_active(scan_on)
        mi_scan_chk.connect("toggled", lambda w: self.set_scan_selection_enabled(w.get_active()))
        menu.prepend(mi_scan_chk)

        mi_scan_now = Gtk.MenuItem(label="🔎 立即查詢反白文字 (Scan Now)")
        mi_scan_now.connect("activate", lambda *_: self.trigger_scan_selection_now())
        menu.prepend(mi_scan_now)

        mi_open = Gtk.MenuItem(label="📖 開啟獨立查詢 / 計算機視窗...")
        mi_open.connect("activate", lambda *_: self.open_popup_window(self.panel_entry.get_text().strip()))
        menu.prepend(mi_open)

        menu.show_all()

    def _on_panel_entry_activate(self, entry):
        txt = entry.get_text().strip()
        if not txt:
            return
        self.execute_query(txt, near_pointer=False, keep_panel_focus=True)
        entry.select_region(0, -1)

    def _wrap_dbus_font(self, markup_body):
        """Return clean Pango markup body (font size & foreground color are applied by CustomBubbleWindow)."""
        return markup_body

    def _get_active_monitor_bounds(self):
        """Return strict (mon_left, mon_top, mon_right, mon_bottom) of the monitor containing the applet."""
        display = Gdk.Display.get_default()
        if display is None:
            return (0, 0, 1920, 1080)
        gdk_win = self.get_window()
        if gdk_win is not None:
            _, ox, oy = gdk_win.get_origin()
            alloc = self.get_allocation()
            cx = int(ox + max(1, alloc.width) / 2)
            cy = int(oy + max(1, alloc.height) / 2)
            monitor = display.get_monitor_at_point(cx, cy) or display.get_monitor_at_window(gdk_win)
        else:
            monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return (0, 0, 1920, 1080)
        geo = monitor.get_geometry()
        wa = monitor.get_workarea()
        mon_left = max(geo.x, wa.x)
        mon_right = min(geo.x + geo.width, wa.x + wa.width)
        mon_top = max(geo.y, wa.y)
        mon_bottom = min(geo.y + geo.height, wa.y + wa.height)
        return (mon_left, mon_top, mon_right, mon_bottom)

    def _notify_dbus_batch(self, items, timeout_sec=None):
        t_sec = int(self.config.get("dbus_popup_timeout_sec", 8)) if timeout_sec is None else int(timeout_sec)
        mon_bounds = self._get_active_monitor_bounds()
        return self.bubble_mgr.show_batch(
            items,
            mon_bounds=mon_bounds,
            position=self.config.get("dbus_popup_position", "top_center"),
            offset_x=int(self.config.get("dbus_popup_x_offset", 0)),
            offset_y=int(self.config.get("dbus_popup_y_offset", 36)),
            bg_color=self.config.get("dbus_popup_bg_color", "#182234"),
            fg_color=self.config.get("dbus_popup_fg_color", "#f0f6ff"),
            bg_opacity=int(self.config.get("dbus_popup_bg_opacity", 92)),
            font_size=int(self.config.get("dbus_popup_font_size", 11)),
            timeout_sec=t_sec,
        )

    def _notify_dbus(self, summary, body, icon="accessories-dictionary", timeout_sec=None):
        return self._notify_dbus_batch([(summary, body, icon)], timeout_sec=timeout_sec)

    def execute_query(self, raw_text, near_pointer=False, keep_panel_focus=False, source="panel"):
        """
        Central query & calculator dispatcher.
        Routes results to either:
          - Focus-free Compact Result Window ('window')
          - System D-Bus Notification Popup ('dbus_popup')
          - Hybrid Mode: Calculator via D-Bus Popup, Dictionary via Window ('calc_dbus_dict_window')
        Selects dictionary profile independently for:
          - Scan Selection ('enabled_dicts_scan')
          - D-Bus Notification Popup ('enabled_dicts_dbus')
          - Focus-free Result Window ('enabled_dicts')
        Always keeps keyboard focus locked in the MATE Panel search box!
        """
        q = raw_text.strip()
        if not q:
            return

        out_mode = self.config.get("result_output_mode", "window")
        timeout_sec = int(self.config.get("dbus_popup_timeout_sec", 8))

        # 1. Check Scientific Calculator first
        is_math, res_str, err = self.calc.try_evaluate(q, record_history=True)
        if is_math:
            auto_copy = bool(self.config.get("calc_auto_copy", True))
            if res_str is not None and auto_copy:
                self.copy_to_clipboard(res_str)

            use_dbus = out_mode in ("dbus_popup", "calc_dbus_dict_window")
            if use_dbus:
                if err is not None:
                    self._notify_dbus(
                        f"🧮 算式錯誤：{q}",
                        self._wrap_dbus_font(html.escape(str(err))),
                        icon="dialog-error",
                        timeout_sec=timeout_sec,
                    )
                else:
                    body_markup = f"{html.escape(q)} = <b>{html.escape(str(res_str))}</b>" + (
                        "\n✔ 已自動存入系統剪貼簿 (Ctrl+V 可直接貼上)" if auto_copy else ""
                    )
                    self._notify_dbus(
                        f"🧮 計算結果：{res_str}",
                        self._wrap_dbus_font(body_markup),
                        icon="accessories-calculator",
                        timeout_sec=timeout_sec,
                    )
            else:
                win = self._ensure_popup_window()
                win.show_calc_result(q, res_str, auto_copy, err=err, near_pointer=near_pointer)

            if keep_panel_focus:
                GLib.idle_add(self._refocus_panel_entry)
            return

        # 2. Smart Routing: Sentence / Paragraph -> Google Translate (Scheme A)
        use_dbus_dict = out_mode == "dbus_popup"
        gt_enabled = bool(self.config.get("google_translate_enabled", True))
        self._query_seq += 1
        seq = self._query_seq

        if gt_enabled and GoogleTranslator.is_sentence_or_paragraph(q):
            self._start_google_translate(
                seq,
                q,
                near_pointer=near_pointer,
                use_dbus_dict=use_dbus_dict,
                keep_panel_focus=keep_panel_focus,
                is_fallback=False,
            )
            return

        # 3. Offline StarDict Asynchronous Lookup (Single word / short phrase)
        if not use_dbus_dict:
            win = self._ensure_popup_window()
            win.show_loading(q, near_pointer=near_pointer, is_translate=False)
            if keep_panel_focus:
                GLib.idle_add(self._refocus_panel_entry)

        if source == "scan":
            custom_list = self.config.get(
                "enabled_dicts_scan",
                self.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS),
            )
        elif use_dbus_dict:
            custom_list = self.config.get(
                "enabled_dicts_dbus",
                self.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS),
            )
        else:
            custom_list = self.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS)
        dict_names = self.dict_mgr.get_ordered_enabled_dicts(custom_list)

        def worker():
            results = self.dict_mgr.query_sync(q, dict_names=dict_names)
            GLib.idle_add(
                self._on_query_dict_done,
                seq,
                q,
                results,
                near_pointer,
                use_dbus_dict,
                keep_panel_focus,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _start_google_translate(self, seq, text, near_pointer, use_dbus_dict, keep_panel_focus, is_fallback=False):
        """Run Google Translate asynchronously; follows user's result_output_mode (focus-free window or D-Bus bubble)."""
        use_dbus_tr = use_dbus_dict

        if not use_dbus_tr:
            win = self._ensure_popup_window()
            win.show_loading(text, near_pointer=near_pointer, is_translate=True)
            if keep_panel_focus:
                GLib.idle_add(self._refocus_panel_entry)

        primary_tl = self.config.get("google_translate_target", "zh-TW")
        reverse_tl = self.config.get("google_translate_reverse", "en")

        def tr_worker():
            translated, src_lang, tgt_lang, err = self.translator.translate_sync(
                text, primary_tl=primary_tl, reverse_tl=reverse_tl
            )
            GLib.idle_add(
                self._on_translate_done,
                seq,
                text,
                translated,
                src_lang,
                tgt_lang,
                err,
                near_pointer,
                use_dbus_tr,
                keep_panel_focus,
                is_fallback,
            )

        threading.Thread(target=tr_worker, daemon=True).start()

    def _on_translate_done(
        self,
        seq,
        original_text,
        translated_text,
        src_lang,
        target_lang,
        err,
        near_pointer,
        use_dbus_tr,
        keep_panel_focus,
        is_fallback,
    ):
        if seq != self._query_seq:
            return False

        if use_dbus_tr:
            timeout_sec = int(self.config.get("dbus_popup_timeout_sec", 8))
            if err is not None or not translated_text:
                self._notify_dbus(
                    "🌐 Google 翻譯失敗",
                    self._wrap_dbus_font(html.escape(str(err or "無回傳內容"))),
                    icon="dialog-error",
                    timeout_sec=timeout_sec,
                )
            else:
                src_lbl = GoogleTranslator.lang_label(src_lang)
                tgt_lbl = GoogleTranslator.lang_label(target_lang)
                fb_tag = " · 自動切換" if is_fallback else ""
                short_orig = original_text.replace("\n", " ")
                if len(short_orig) > 65:
                    short_orig = short_orig[:63] + "…"
                tr_disp = translated_text.strip()
                if len(tr_disp) > 900:
                    tr_disp = tr_disp[:897] + "…"
                body = self._wrap_dbus_font(
                    f"<b>{html.escape(tr_disp)}</b>\n<i>{html.escape(short_orig)}</i>"
                )
                self._notify_dbus(
                    f"🌐 Google 翻譯 ({src_lbl} → {tgt_lbl}{fb_tag})",
                    body,
                    icon="accessories-dictionary",
                    timeout_sec=timeout_sec,
                )
        else:
            win = self._ensure_popup_window()
            win.show_translate_result(
                original_text,
                translated_text,
                src_lang,
                target_lang,
                err=err,
                is_fallback=is_fallback,
                near_pointer=near_pointer,
            )

        if keep_panel_focus:
            GLib.idle_add(self._refocus_panel_entry)
        return False

    def _refocus_panel_entry(self):
        if self.config.get("display_mode", "panel_entry") == "panel_entry":
            self.panel_entry.grab_focus()
        return False

    def _on_query_dict_done(self, seq, query_word, results, near_pointer, use_dbus_dict, keep_panel_focus):
        if seq != self._query_seq:
            return False

        # Auto-fallback to Google Translate when offline StarDict returns 0 hits
        if (
            not results
            and self.config.get("google_translate_enabled", True)
            and self.config.get("google_translate_fallback", True)
        ):
            self._start_google_translate(
                seq,
                query_word,
                near_pointer=near_pointer,
                use_dbus_dict=use_dbus_dict,
                keep_panel_focus=keep_panel_focus,
                is_fallback=True,
            )
            return False

        if use_dbus_dict:
            timeout_sec = int(self.config.get("dbus_popup_timeout_sec", 8))
            dbus_style = self.config.get("dbus_popup_style", "separate")
            max_dicts = max(1, min(5, int(self.config.get("dbus_popup_max_dicts", 3))))
            book_icons = ["📘", "📗", "📙", "📕", "📓"]

            if not results:
                self._notify_dbus(
                    f"📖 查無詞條：{query_word}",
                    self._wrap_dbus_font("請檢查拼字，或在偏好設定的「詞典管理」勾選更多詞典。"),
                    icon="accessories-dictionary",
                    timeout_sec=timeout_sec,
                )
            elif dbus_style == "separate":
                # Mode 1: Each dictionary gets its own crisp independent D-Bus bubble (replaced in-place next query)
                batch = []
                shown = results[:max_dicts]
                for idx, item in enumerate(shown):
                    b_emo = book_icons[idx % len(book_icons)]
                    d_name = item["dict"]
                    w_str = item["word"]
                    summary = f"{b_emo} {w_str} — {d_name}"
                    raw_lines = [ln.strip() for ln in item["definition"].splitlines() if ln.strip()]
                    raw_body = "\n".join(raw_lines[:8])
                    if len(raw_lines) > 8:
                        raw_body += "\n…"
                    if len(raw_body) > 460:
                        raw_body = raw_body[:457] + "…"
                    body = self._wrap_dbus_font(html.escape(raw_body))
                    batch.append((summary, body, "accessories-dictionary"))
                self._notify_dbus_batch(batch, timeout_sec=timeout_sec)
            else:
                # Mode 2: Single merged D-Bus bubble with clear bold divider banners and indented definitions
                blocks = []
                shown = results[:max_dicts]
                total_chars = 0
                for idx, item in enumerate(shown):
                    b_emo = book_icons[idx % len(book_icons)]
                    d_esc = html.escape(item["dict"])
                    w_esc = html.escape(item["word"])
                    hdr = f"<b>━━━ {b_emo} {d_esc} ({w_esc}) ━━━</b>"
                    raw_lines = [ln.strip() for ln in item["definition"].splitlines() if ln.strip()]
                    kept_lines = []
                    for ln in raw_lines[:6]:
                        if total_chars + len(ln) > 760 and kept_lines:
                            kept_lines.append("  …")
                            break
                        kept_lines.append(f"  {html.escape(ln)}")
                        total_chars += len(ln)
                    blocks.append("\n".join([hdr] + kept_lines))
                    if total_chars >= 760:
                        break
                if len(results) > max_dicts:
                    blocks.append(f"<i>…（另有 {len(results) - max_dicts} 套詞典命中）</i>")
                body = self._wrap_dbus_font("\n\n".join(blocks).strip())
                self._notify_dbus(
                    f"📖 {query_word}",
                    body,
                    icon="accessories-dictionary",
                    timeout_sec=timeout_sec,
                )
        else:
            win = self._ensure_popup_window()
            win.show_dict_results(query_word, results, near_pointer=near_pointer)

        if keep_panel_focus:
            GLib.idle_add(self._refocus_panel_entry)
        return False

    def _update_tooltip(self):
        scan_on = self.config.get("scan_selection_enabled", False)
        mod_map = {"none": "純反白", "ctrl": "按住 Ctrl", "alt": "按住 Alt", "shift": "按住 Shift"}
        mod_txt = mod_map.get(self.config.get("scan_modifier", "ctrl"), "Ctrl")
        scan_str = f"啟用中 ({mod_txt})" if scan_on else "已關閉 (中鍵點擊可切換)"
        hk_open = self.config.get("hotkey_open_window", "<Ctrl><Alt>d")
        hk_scan = self.config.get("hotkey_scan_selection", "<Ctrl><Alt>s")
        n_dicts = len(self.dict_mgr.all_dicts)

        self.set_tooltip_markup(
            f"<b>📖 StarDict 離線詞典 &amp; 🧮 科學計算機 Plus</b>\n"
            f"• 離線詞庫數: <b>{n_dicts}</b> 套\n"
            f"• 滑鼠劃詞查詢 (Scan Selection): <b>{scan_str}</b>\n"
            f"• 計算機結果自動存入剪貼簿: <b>{'開啟 (可直接 Ctrl+V)' if self.config.get('calc_auto_copy', True) else '關閉'}</b>\n"
            f"• 全域快捷鍵: 呼叫視窗 <b>{html.escape(hk_open)}</b> | 劃詞查詢 <b>{html.escape(hk_scan)}</b>\n"
            f"<small>左鍵: 開啟查詢/計算視窗 | 中鍵: 開關劃詞取詞 | 右鍵: 偏好設定</small>"
        )

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

    def _on_draw_badge(self, widget, ctx):
        alloc = widget.get_allocation()
        w, h = float(alloc.width), float(alloc.height)
        pad_y = 1.0
        bh = max(20.0, h - pad_y * 2)
        bw = max(28.0, w - 2.0)
        x, y = 1.0, pad_y

        scan_on = self.config.get("scan_selection_enabled", False)
        bg_hex = self.config.get("badge_bg_color", "#182234")
        opacity = max(0, min(100, int(self.config.get("badge_bg_opacity", 90))))
        alpha = opacity / 100.0
        custom_bg_rgb = hex_to_rgb(bg_hex, default=(0.094, 0.133, 0.204))
        is_light_bg = opacity >= 30 and rgb_luminance(custom_bg_rgb) > 0.55

        if scan_on:
            border_rgb = (0.20, 0.84, 0.62)
            text_rgb = (0.94, 1.00, 0.98)
        else:
            border_rgb = (0.28, 0.72, 0.98)
            text_rgb = (0.94, 0.98, 1.00)

        draw_rounded_rect(ctx, x + 0.5, y + 0.5, bw - 1.0, bh - 1.0, 4.5)
        if alpha > 0.0:
            ctx.set_source_rgba(custom_bg_rgb[0], custom_bg_rgb[1], custom_bg_rgb[2], alpha)
            ctx.fill_preserve()

        ctx.set_source_rgba(border_rgb[0], border_rgb[1], border_rgb[2], 0.95)
        ctx.set_line_width(1.2)
        ctx.stroke()

        # Scan Selection active green indicator dot in top-right corner
        if scan_on:
            ctx.arc(x + bw - 4.5, y + 4.5, 2.3, 0, 2 * math.pi)
            ctx.set_source_rgba(0.25, 0.98, 0.58, 1.0)
            ctx.fill()

        if self.config.get("font_color_mode", "dynamic") == "custom":
            c_rgb = hex_to_rgb(self.config.get("custom_font_color", "#ffffff"), default=text_rgb)
            title_rgb = c_rgb
            sub_rgb = c_rgb
        else:
            title_rgb = border_rgb
            sub_rgb = (0.07, 0.09, 0.15) if is_light_bg else text_rgb

        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        t_sz = max(6.5, bh * 0.27)
        ctx.set_font_size(t_sz)
        t_str = "DICT"
        ext = ctx.text_extents(t_str)
        self._draw_styled_text(
            ctx,
            x + (bw - ext.width) / 2.0 - ext.x_bearing,
            y + t_sz + 1.5,
            t_str,
            title_rgb,
        )

        s_sz = max(7.0, bh * 0.30)
        ctx.set_font_size(s_sz)
        s_str = "SCAN" if scan_on else "CALC"
        s_ext = ctx.text_extents(s_str)
        self._draw_styled_text(
            ctx,
            x + (bw - s_ext.width) / 2.0 - s_ext.x_bearing,
            y + bh - 3.2,
            s_str,
            sub_rgb,
        )
        return False

    def handle_applet_button_press(self, widget, event):
        # In pure inline entry mode, let Gtk.Entry handle clicks and request_focus directly
        if self.config.get("display_mode", "badge") == "panel_entry":
            if event.button == 1:
                try:
                    self.applet.request_focus(event.time)
                except Exception:
                    pass
                self.panel_entry.grab_focus()
            return False
        if event.button == 1:
            GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.toggle_popup_window()
            return True
        elif event.button == 2:
            GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.toggle_scan_selection()
            return True
        return False

    def _on_button_press(self, widget, event):
        if self.config.get("display_mode", "badge") == "panel_entry" and widget is not self.canvas:
            return False
        if event.button == 1:
            if self.applet is not None:
                GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.toggle_popup_window()
            return True
        elif event.button == 2:
            if self.applet is not None:
                GObject.signal_stop_emission_by_name(widget, "button-press-event")
            self.toggle_scan_selection()
            return True
        elif event.button == 3 and self.applet is None:
            self.show_preferences_dialog()
            return True
        return False

    def _ensure_popup_window(self):
        if self.popup_win is None:
            self.popup_win = DictCalcPopupWindow(self)
        return self.popup_win

    def toggle_popup_window(self, *_args):
        if self.config.get("display_mode", "panel_entry") == "panel_entry":
            self.focus_panel_search_box()
            return
        win = self._ensure_popup_window()
        if win.is_visible():
            win.hide()
        else:
            win.position_and_show(near_pointer=False)

    def open_popup_window(self, text="", near_pointer=False, source="panel"):
        if text:
            # Keep the compact panel entry clean when scanning multi-line paragraphs
            if "\n" not in text and len(text) <= 80:
                self.panel_entry.set_text(text)
            self.execute_query(text, near_pointer=near_pointer, keep_panel_focus=False, source=source)
        else:
            self.focus_panel_search_box()

    # =========================================================================
    # Preferences Window (Hotkeys + Scan Selection + Calculator + 41 Dicts)
    # =========================================================================

    def show_preferences_dialog(self, *_args):
        if self.prefs_win is not None:
            self.prefs_win.present()
            return

        win = Gtk.Window(title="StarDict 離線詞典 & 計算機 Plus — 偏好設定")
        win.set_default_size(600, 720)
        win.set_position(Gtk.WindowPosition.CENTER)
        self.prefs_win = win
        win.connect("destroy", lambda *_: setattr(self, "prefs_win", None))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(14)
        outer.set_margin_end(14)

        notebook = Gtk.Notebook()

        # ---------------- Tab 1: General, Hotkeys, Scan Selection & Appearance ----------------
        tab1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab1.set_margin_top(12)
        tab1.set_margin_bottom(12)
        tab1.set_margin_start(14)
        tab1.set_margin_end(14)

        # 1. Global Hotkeys
        s1_lbl = Gtk.Label(xalign=0.0)
        s1_lbl.set_markup(
            "<b>1. 全域快捷鍵設定</b> "
            + (
                "<small><span foreground='#34d399'>(Keybinder 3.0 已啟用)</span></small>"
                if HAS_KEYBINDER
                else "<small><span foreground='#f87171'>(未安裝 Keybinder 3.0)</span></small>"
            )
        )
        tab1.pack_start(s1_lbl, False, False, 0)

        hk_grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        hk_grid.set_margin_start(12)

        entry_hk_open = Gtk.Entry()
        entry_hk_open.set_text(self.config.get("hotkey_open_window", "<Ctrl><Alt>d"))
        entry_hk_open.set_placeholder_text("例如 <Ctrl><Alt>d 或 <Super>d")

        entry_hk_scan = Gtk.Entry()
        entry_hk_scan.set_text(self.config.get("hotkey_scan_selection", "<Ctrl><Alt>s"))
        entry_hk_scan.set_placeholder_text("例如 <Ctrl><Alt>s 或 <Alt>q")

        btn_apply_hk = Gtk.Button(label="套用快捷鍵")

        hk_grid.attach(Gtk.Label(label="呼叫查詢/計算視窗快捷鍵：", xalign=0.0), 0, 0, 1, 1)
        hk_grid.attach(entry_hk_open, 1, 0, 1, 1)
        hk_grid.attach(Gtk.Label(label="Scan Selection 劃詞查詢快捷鍵：", xalign=0.0), 0, 1, 1, 1)
        hk_grid.attach(entry_hk_scan, 1, 1, 1, 1)
        hk_grid.attach(btn_apply_hk, 2, 0, 1, 2)
        tab1.pack_start(hk_grid, False, False, 0)

        tab1.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # 2. Scan Selection & Calculator Behavior
        s2_lbl = Gtk.Label(xalign=0.0)
        s2_lbl.set_markup("<b>2. 滑鼠劃詞查詢 (Scan Selection) 與計算機設定</b>")
        tab1.pack_start(s2_lbl, False, False, 0)

        chk_scan_en = Gtk.CheckButton(label="啟用滑鼠反白自動劃詞查詢 (監聽 PRIMARY 選取區)")
        chk_scan_en.set_active(self.config.get("scan_selection_enabled", False))
        tab1.pack_start(chk_scan_en, False, False, 0)

        mod_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        mod_box.set_margin_start(14)
        mod_box.pack_start(Gtk.Label(label="自動劃詞觸發條件：", xalign=0.0), False, False, 0)
        combo_mod = Gtk.ComboBoxText()
        for m_id, m_lbl in (
            ("ctrl", "反白時需按住 Ctrl 鍵 (避免誤觸，推薦)"),
            ("alt", "反白時需按住 Alt 鍵"),
            ("shift", "反白時需按住 Shift 鍵"),
            ("none", "純滑鼠反白即立刻彈出查詢"),
        ):
            combo_mod.append(m_id, m_lbl)
        combo_mod.set_active_id(self.config.get("scan_modifier", "ctrl"))
        mod_box.pack_start(combo_mod, True, True, 0)
        tab1.pack_start(mod_box, False, False, 0)

        chk_auto_copy = Gtk.CheckButton(
            label="數學計算模式下，預設將計算結果自動存入系統剪貼簿 (可直接 Ctrl+V 貼上)"
        )
        chk_auto_copy.set_active(self.config.get("calc_auto_copy", True))
        tab1.pack_start(chk_auto_copy, False, False, 0)

        chk_gt_en = Gtk.CheckButton(
            label="🌐 句子 / 段落自動使用 Google 翻譯 (雙向翻譯，依上方顯示方式呈現)"
        )
        chk_gt_en.set_active(self.config.get("google_translate_enabled", True))
        tab1.pack_start(chk_gt_en, False, False, 0)

        chk_gt_fb = Gtk.CheckButton(
            label="離線詞典查無單字 / 短片語時，自動 Fallback 至 Google 翻譯"
        )
        chk_gt_fb.set_margin_start(14)
        chk_gt_fb.set_active(self.config.get("google_translate_fallback", True))
        tab1.pack_start(chk_gt_fb, False, False, 0)

        gt_lang_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gt_lang_box.set_margin_start(14)
        gt_lang_box.pack_start(Gtk.Label(label="外文翻譯目標：", xalign=0.0), False, False, 0)
        combo_gt_tl = Gtk.ComboBoxText()
        for l_id, l_lbl in (
            ("zh-TW", "繁體中文 (zh-TW)"),
            ("zh-CN", "簡體中文 (zh-CN)"),
            ("ja", "日文 (ja)"),
            ("en", "英文 (en)"),
        ):
            combo_gt_tl.append(l_id, l_lbl)
        combo_gt_tl.set_active_id(self.config.get("google_translate_target", "zh-TW"))
        gt_lang_box.pack_start(combo_gt_tl, True, True, 0)

        gt_lang_box.pack_start(Gtk.Label(label="中文反向翻譯：", xalign=0.0), False, False, 0)
        combo_gt_rev = Gtk.ComboBoxText()
        for l_id, l_lbl in (
            ("en", "英文 (en)"),
            ("ja", "日文 (ja)"),
        ):
            combo_gt_rev.append(l_id, l_lbl)
        combo_gt_rev.set_active_id(self.config.get("google_translate_reverse", "en"))
        gt_lang_box.pack_start(combo_gt_rev, False, False, 0)
        tab1.pack_start(gt_lang_box, False, False, 0)

        out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        out_box.set_margin_start(14)
        out_box.pack_start(Gtk.Label(label="查詢/計算結果顯示方式：", xalign=0.0), False, False, 0)
        combo_out = Gtk.ComboBoxText()
        for o_id, o_lbl in (
            ("window", "無焦點精簡結果視窗 (可捲動看完整詞典，不搶面板游標)"),
            ("dbus_popup", "氣泡框彈出模式 (可於「氣泡框設定」自訂位置、底色、字色與透明度)"),
            ("calc_dbus_dict_window", "混合模式 (計算機用氣泡框秒出，查詞與翻譯用無焦點視窗)"),
        ):
            combo_out.append(o_id, o_lbl)
        combo_out.set_active_id(self.config.get("result_output_mode", "window"))
        out_box.pack_start(combo_out, True, True, 0)
        tab1.pack_start(out_box, False, False, 0)

        tab1.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        # 3. Panel Mode & Appearance
        s3_lbl = Gtk.Label(xalign=0.0)
        s3_lbl.set_markup("<b>3. 面板顯示樣式、字形顏色與底色適應</b>")
        tab1.pack_start(s3_lbl, False, False, 0)

        rb_badge = Gtk.RadioButton.new_with_label_from_widget(
            None, "模式 A：精簡圓角方塊圖示 ([DICT / CALC])"
        )
        rb_entry = Gtk.RadioButton.new_with_label_from_widget(
            rb_badge, "模式 B：純面板查詢/計算輸入框 (隱藏方塊，隨點即打字，左右內建劃詞與設定圖示)"
        )
        if self.config.get("display_mode", "badge") == "badge":
            rb_badge.set_active(True)
        else:
            rb_entry.set_active(True)
        tab1.pack_start(rb_badge, False, False, 0)
        tab1.pack_start(rb_entry, False, False, 0)

        size_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        size_grid.set_margin_start(14)
        spin_entry_w = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("panel_entry_width", 170)),
                lower=90.0,
                upper=320.0,
                step_increment=10.0,
                page_increment=20.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        spin_res_fs = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("result_font_size", 11)),
                lower=8.0,
                upper=22.0,
                step_increment=1.0,
                page_increment=2.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        size_grid.attach(Gtk.Label(label="面板內嵌輸入框寬度 (px)：", xalign=0.0), 0, 0, 1, 1)
        size_grid.attach(spin_entry_w, 1, 0, 1, 1)
        size_grid.attach(Gtk.Label(label="查詢結果視窗字型大小 (pt)：", xalign=0.0), 2, 0, 1, 1)
        size_grid.attach(spin_res_fs, 3, 0, 1, 1)
        tab1.pack_start(size_grid, False, False, 0)

        # Font color & badge background
        col_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rb_col_dyn = Gtk.RadioButton.new_with_label_from_widget(None, "自動字色")
        rb_col_cus = Gtk.RadioButton.new_with_label_from_widget(rb_col_dyn, "自訂面板字色：")
        if self.config.get("font_color_mode", "dynamic") == "dynamic":
            rb_col_dyn.set_active(True)
        else:
            rb_col_cus.set_active(True)
        col_btn = Gtk.ColorButton()
        rgba0 = Gdk.RGBA()
        if not rgba0.parse(self.config.get("custom_font_color", "#ffffff")):
            rgba0.parse("#ffffff")
        col_btn.set_rgba(rgba0)
        col_box.pack_start(rb_col_dyn, False, False, 0)
        col_box.pack_start(rb_col_cus, False, False, 0)
        col_box.pack_start(col_btn, False, False, 0)
        tab1.pack_start(col_box, False, False, 0)

        bg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bg_box.pack_start(Gtk.Label(label="方塊背景底色：", xalign=0.0), False, False, 0)
        bg_col_btn = Gtk.ColorButton()
        bg_rgba0 = Gdk.RGBA()
        if not bg_rgba0.parse(self.config.get("badge_bg_color", "#182234")):
            bg_rgba0.parse("#182234")
        bg_col_btn.set_rgba(bg_rgba0)
        bg_col_btn.set_tooltip_text("點擊自訂方塊 / 輸入框背景底色")
        bg_box.pack_start(bg_col_btn, False, False, 0)

        bg_box.pack_start(Gtk.Label(label="不透明度：", xalign=0.0), False, False, 0)
        scale_bg_alpha = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 5.0)
        scale_bg_alpha.set_digits(0)
        scale_bg_alpha.set_draw_value(False)
        scale_bg_alpha.set_size_request(115, -1)
        init_op = max(0, min(100, int(self.config.get("badge_bg_opacity", 90))))
        scale_bg_alpha.set_value(float(init_op))
        scale_bg_alpha.set_tooltip_text("0% = 完全透明，100% = 完全不透明")
        lbl_bg_alpha = Gtk.Label(label=f"{init_op}%")
        lbl_bg_alpha.set_width_chars(4)
        bg_box.pack_start(scale_bg_alpha, True, True, 0)
        bg_box.pack_start(lbl_bg_alpha, False, False, 0)

        btn_bg_dark = Gtk.Button(label="深色")
        btn_bg_light = Gtk.Button(label="淺色")
        btn_bg_trans = Gtk.Button(label="透明")
        bg_box.pack_start(btn_bg_dark, False, False, 0)
        bg_box.pack_start(btn_bg_light, False, False, 0)
        bg_box.pack_start(btn_bg_trans, False, False, 0)
        tab1.pack_start(bg_box, False, False, 0)

        chk_shadow = Gtk.CheckButton(label="啟用面板文字高對比描邊陰影")
        chk_shadow.set_active(self.config.get("text_shadow", False))
        tab1.pack_start(chk_shadow, False, False, 0)

        tab1_scroll = Gtk.ScrolledWindow()
        tab1_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        tab1_scroll.add(tab1)
        notebook.append_page(tab1_scroll, Gtk.Label(label="⚙️ 一般、快捷鍵與外觀"))

        # ---------------- Tab 2: Dedicated Bubble Popup Settings Page ----------------
        tab_bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        tab_bubble.set_margin_top(14)
        tab_bubble.set_margin_bottom(14)
        tab_bubble.set_margin_start(16)
        tab_bubble.set_margin_end(16)

        b_s1 = Gtk.Label(xalign=0.0)
        b_s1.set_markup(
            "<b>1. 氣泡框螢幕位置與微調偏移</b> "
            "<small><span foreground='#94a3b8'>(依實際渲染寬高精準鎖定於目前螢幕內，不跨螢幕溢出)</span></small>"
        )
        tab_bubble.pack_start(b_s1, False, False, 0)

        b_pos_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        b_pos_box.set_margin_start(12)
        b_pos_box.pack_start(Gtk.Label(label="氣泡框彈出位置：", xalign=0.0), False, False, 0)
        combo_bubble_pos = Gtk.ComboBoxText()
        for p_id, p_lbl in (
            ("top_center", "置中上方 (推薦，不擋兩側視窗與面板)"),
            ("top_left", "左上角"),
            ("top_right", "右上角"),
            ("bottom_right", "右下角"),
            ("bottom_left", "左下角"),
        ):
            combo_bubble_pos.append(p_id, p_lbl)
        combo_bubble_pos.set_active_id(self.config.get("dbus_popup_position", "top_center"))
        b_pos_box.pack_start(combo_bubble_pos, True, True, 0)
        tab_bubble.pack_start(b_pos_box, False, False, 0)

        b_off_grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        b_off_grid.set_margin_start(12)
        spin_dbus_xoff = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("dbus_popup_x_offset", 0)),
                lower=-500.0,
                upper=500.0,
                step_increment=10.0,
                page_increment=50.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        spin_dbus_xoff.set_tooltip_text("正值向右偏移，負值向左偏移 (px)")
        spin_dbus_yoff = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("dbus_popup_y_offset", 36)),
                lower=-300.0,
                upper=500.0,
                step_increment=8.0,
                page_increment=24.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        spin_dbus_yoff.set_tooltip_text("上方位置時正值向下推移；下方位置時正值向上推移 (px)")
        b_off_grid.attach(Gtk.Label(label="水平偏移 Offset X (px)：", xalign=0.0), 0, 0, 1, 1)
        b_off_grid.attach(spin_dbus_xoff, 1, 0, 1, 1)
        b_off_grid.attach(Gtk.Label(label="垂直偏移 Offset Y (px)：", xalign=0.0), 2, 0, 1, 1)
        b_off_grid.attach(spin_dbus_yoff, 3, 0, 1, 1)
        tab_bubble.pack_start(b_off_grid, False, False, 0)

        tab_bubble.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        b_s2 = Gtk.Label(xalign=0.0)
        b_s2.set_markup("<b>2. 氣泡框背景顏色、字形顏色與背景透明度</b>")
        tab_bubble.pack_start(b_s2, False, False, 0)

        b_col_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        b_col_box.set_margin_start(12)
        b_col_box.pack_start(Gtk.Label(label="背景顏色：", xalign=0.0), False, False, 0)
        bubble_bg_btn = Gtk.ColorButton()
        b_bg_rgba0 = Gdk.RGBA()
        if not b_bg_rgba0.parse(self.config.get("dbus_popup_bg_color", "#182234")):
            b_bg_rgba0.parse("#182234")
        bubble_bg_btn.set_rgba(b_bg_rgba0)
        b_col_box.pack_start(bubble_bg_btn, False, False, 0)

        b_col_box.pack_start(Gtk.Label(label="字形顏色：", xalign=0.0), False, False, 0)
        bubble_fg_btn = Gtk.ColorButton()
        b_fg_rgba0 = Gdk.RGBA()
        if not b_fg_rgba0.parse(self.config.get("dbus_popup_fg_color", "#f0f6ff")):
            b_fg_rgba0.parse("#f0f6ff")
        bubble_fg_btn.set_rgba(b_fg_rgba0)
        b_col_box.pack_start(bubble_fg_btn, False, False, 0)

        btn_b_dark = Gtk.Button(label="深色毛玻璃")
        btn_b_light = Gtk.Button(label="淺色卡片")
        btn_b_oled = Gtk.Button(label="半透明黑")
        b_col_box.pack_end(btn_b_oled, False, False, 0)
        b_col_box.pack_end(btn_b_light, False, False, 0)
        b_col_box.pack_end(btn_b_dark, False, False, 0)
        tab_bubble.pack_start(b_col_box, False, False, 0)

        b_op_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        b_op_box.set_margin_start(12)
        b_op_box.pack_start(Gtk.Label(label="背景不透明度：", xalign=0.0), False, False, 0)
        scale_bubble_alpha = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 2.0)
        scale_bubble_alpha.set_digits(0)
        scale_bubble_alpha.set_draw_value(False)
        init_b_op = max(0, min(100, int(self.config.get("dbus_popup_bg_opacity", 92))))
        scale_bubble_alpha.set_value(float(init_b_op))
        lbl_bubble_alpha = Gtk.Label(label=f"{init_b_op}%")
        lbl_bubble_alpha.set_width_chars(4)
        b_op_box.pack_start(scale_bubble_alpha, True, True, 0)
        b_op_box.pack_start(lbl_bubble_alpha, False, False, 0)
        tab_bubble.pack_start(b_op_box, False, False, 0)

        tab_bubble.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        b_s3 = Gtk.Label(xalign=0.0)
        b_s3.set_markup("<b>3. 多詞典呈現方式、字型大小與停留時間</b>")
        tab_bubble.pack_start(b_s3, False, False, 0)

        dbus_t_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dbus_t_box.set_margin_start(12)
        dbus_t_box.pack_start(Gtk.Label(label="多詞典呈現：", xalign=0.0), False, False, 0)
        combo_dbus_style = Gtk.ComboBoxText()
        combo_dbus_style.append("separate", "每本詞典獨立泡泡 (不洗版，最清晰)")
        combo_dbus_style.append("merged", "單一泡泡以粗體分隔線區隔 (━━━ 📘 字典 ━━━)")
        combo_dbus_style.set_active_id(self.config.get("dbus_popup_style", "separate"))
        dbus_t_box.pack_start(combo_dbus_style, True, True, 0)
        tab_bubble.pack_start(dbus_t_box, False, False, 0)

        dbus_num_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        dbus_num_box.set_margin_start(12)
        dbus_num_box.pack_start(Gtk.Label(label="最多詞典數：", xalign=0.0), False, False, 0)
        spin_dbus_max = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("dbus_popup_max_dicts", 3)),
                lower=1.0,
                upper=5.0,
                step_increment=1.0,
                page_increment=1.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        dbus_num_box.pack_start(spin_dbus_max, False, False, 0)
        dbus_num_box.pack_start(Gtk.Label(label="停留秒數：", xalign=0.0), False, False, 0)
        spin_dbus_sec = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("dbus_popup_timeout_sec", 8)),
                lower=1.0,
                upper=60.0,
                step_increment=1.0,
                page_increment=5.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        dbus_num_box.pack_start(spin_dbus_sec, False, False, 0)
        dbus_num_box.pack_start(Gtk.Label(label="氣泡字型 (pt)：", xalign=0.0), False, False, 0)
        spin_dbus_fs = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=float(self.config.get("dbus_popup_font_size", 11)),
                lower=8.0,
                upper=22.0,
                step_increment=1.0,
                page_increment=2.0,
                page_size=0.0,
            ),
            climb_rate=1.0,
            digits=0,
        )
        dbus_num_box.pack_start(spin_dbus_fs, False, False, 0)
        tab_bubble.pack_start(dbus_num_box, False, False, 0)

        b_test_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        b_test_box.set_margin_start(12)
        b_test_box.set_margin_top(6)
        btn_test_dbus = Gtk.Button(label="🔔 預覽測試氣泡（單擊氣泡可立即關閉）")
        btn_test_dbus.connect(
            "clicked",
            lambda *_: self._notify_dbus(
                f"📘 氣泡預覽 ({self.config.get('dbus_popup_font_size', 11)} pt)",
                self._wrap_dbus_font(
                    "<b>━━━ 📘 DrEye4.1 (example) ━━━</b>\n"
                    "  n. 範例；實例；榜樣\n"
                    "  滑鼠移入氣泡會暫停自動消失，點擊可立即關閉。"
                ),
                icon="accessories-dictionary",
                timeout_sec=6,
            ),
        )
        btn_close_bubbles = Gtk.Button(label="✖ 關閉目前氣泡")
        btn_close_bubbles.connect("clicked", lambda *_: self.bubble_mgr.close_all())
        b_test_box.pack_start(btn_test_dbus, False, False, 0)
        b_test_box.pack_start(btn_close_bubbles, False, False, 0)
        tab_bubble.pack_start(b_test_box, False, False, 0)

        notebook.append_page(tab_bubble, Gtk.Label(label="💬 氣泡框設定"))

        # ---------------- Tab 2: StarDict Dictionaries Manager (3 Independent Profiles) ----------------
        tab2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tab2.set_margin_top(10)
        tab2.set_margin_bottom(10)
        tab2.set_margin_start(12)
        tab2.set_margin_end(12)

        d_hdr = Gtk.Label(xalign=0.0)
        d_hdr.set_markup(
            f"<b>已掃描到 {len(self.dict_mgr.all_dicts)} 套離線 StarDict 詞典（三種查詢情境獨立管理）</b>\n"
            f"<small>可分別為「無焦點查詢視窗」、「D-Bus 氣泡框」與「Scan Selection 劃詞」設定不同詞典與顯示順位（支援右側按鈕與滑鼠拖曳）</small>"
        )
        tab2.pack_start(d_hdr, False, False, 0)

        dict_by_name = {d["bookname"]: d for d in self.dict_mgr.all_dicts}
        grp_labels = {"en_zh": "英漢/漢英", "ja_zh": "日漢/漢日", "sci_en": "科技/英英"}
        dict_notebook = Gtk.Notebook()

        def build_dict_profile_page(config_key, profile_desc, default_preset, allow_copy_win=False):
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            page.set_margin_top(8)
            page.set_margin_bottom(6)
            page.set_margin_start(8)
            page.set_margin_end(8)

            info_lbl = Gtk.Label(xalign=0.0)
            page.pack_start(info_lbl, False, False, 0)

            store = Gtk.ListStore(bool, str, str, int)

            def populate_store_from_list(enabled_list):
                store.clear()
                added = set()
                for name in enabled_list:
                    if name in dict_by_name and name not in added:
                        d = dict_by_name[name]
                        store.append([True, d["bookname"], grp_labels.get(d["group"], d["group"]), d["wordcount"]])
                        added.add(name)
                for d in self.dict_mgr.all_dicts:
                    if d["bookname"] not in added:
                        store.append([False, d["bookname"], grp_labels.get(d["group"], d["group"]), d["wordcount"]])
                        added.add(d["bookname"])

            def update_count_label():
                n_en = sum(1 for row in store if row[0])
                info_lbl.set_markup(
                    f"<small>{html.escape(profile_desc)} — 目前已啟用 <b>{n_en}</b> 套詞典</small>"
                )

            saved_enabled = list(self.config.get(config_key, default_preset))
            populate_store_from_list(saved_enabled)
            update_count_label()

            rebuilding = [False]

            def save_order(*_):
                if rebuilding[0]:
                    return False
                self.config[config_key] = [row[1] for row in store if row[0]]
                self._save_config()
                update_count_label()
                if self.popup_win is not None:
                    self.popup_win.sync_controls()
                return False

            tree = Gtk.TreeView(model=store)
            tree.set_reorderable(True)
            store.connect("rows-reordered", lambda *_: GLib.idle_add(save_order))
            store.connect("row-deleted", lambda *_: GLib.idle_add(save_order))

            rend_toggle = Gtk.CellRendererToggle()

            def on_row_toggled(_cell, path_str):
                store[path_str][0] = not store[path_str][0]
                save_order()

            rend_toggle.connect("toggled", on_row_toggled)
            tree.append_column(Gtk.TreeViewColumn("啟用", rend_toggle, active=0))
            col_name = Gtk.TreeViewColumn("詞典名稱 (Bookname)", Gtk.CellRendererText(), text=1)
            col_name.set_expand(True)
            tree.append_column(col_name)
            tree.append_column(Gtk.TreeViewColumn("分類", Gtk.CellRendererText(), text=2))
            tree.append_column(Gtk.TreeViewColumn("詞條數", Gtk.CellRendererText(), text=3))

            list_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            d_scroll = Gtk.ScrolledWindow()
            d_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            d_scroll.set_vexpand(True)
            d_scroll.set_hexpand(True)
            d_scroll.add(tree)
            list_hbox.pack_start(d_scroll, True, True, 0)

            order_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            btn_top = Gtk.Button(label="⏫ 置頂")
            btn_up = Gtk.Button(label="▲ 上移")
            btn_down = Gtk.Button(label="▼ 下移")
            btn_bottom = Gtk.Button(label="⏬ 置底")

            def move_selected_row(direction):
                sel = tree.get_selection()
                model, it = sel.get_selected()
                if it is None:
                    return
                path = model.get_path(it)
                idx = path.get_indices()[0]
                total = len(model)
                if direction == "up" and idx > 0:
                    prev_it = model.get_iter(Gtk.TreePath.new_from_indices([idx - 1]))
                    model.swap(it, prev_it)
                    new_idx = idx - 1
                elif direction == "down" and idx < total - 1:
                    next_it = model.get_iter(Gtk.TreePath.new_from_indices([idx + 1]))
                    model.swap(it, next_it)
                    new_idx = idx + 1
                elif direction == "top" and idx > 0:
                    first_it = model.get_iter_first()
                    model.move_before(it, first_it)
                    new_idx = 0
                elif direction == "bottom" and idx < total - 1:
                    last_it = model.get_iter(Gtk.TreePath.new_from_indices([total - 1]))
                    model.move_after(it, last_it)
                    new_idx = total - 1
                else:
                    return
                new_path = Gtk.TreePath.new_from_indices([new_idx])
                sel.select_path(new_path)
                tree.scroll_to_cell(new_path, None, False, 0.0, 0.0)
                save_order()

            btn_top.connect("clicked", lambda *_: move_selected_row("top"))
            btn_up.connect("clicked", lambda *_: move_selected_row("up"))
            btn_down.connect("clicked", lambda *_: move_selected_row("down"))
            btn_bottom.connect("clicked", lambda *_: move_selected_row("bottom"))
            for b_ord in (btn_top, btn_up, btn_down, btn_bottom):
                order_vbox.pack_start(b_ord, False, False, 0)
            list_hbox.pack_start(order_vbox, False, False, 0)

            page.pack_start(list_hbox, True, True, 0)

            d_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            btn_sel_all = Gtk.Button(label="全部勾選")
            btn_sel_none = Gtk.Button(label="全部取消")
            btn_sel_def = Gtk.Button(label="恢復推薦詞典")

            def set_dict_selection(mode_sel):
                if mode_sel == "copy_win":
                    rebuilding[0] = True
                    try:
                        populate_store_from_list(
                            list(self.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS))
                        )
                    finally:
                        rebuilding[0] = False
                    save_order()
                    return
                for row in store:
                    if mode_sel == "all":
                        row[0] = True
                    elif mode_sel == "none":
                        row[0] = False
                    else:
                        row[0] = row[1] in default_preset
                save_order()

            btn_sel_all.connect("clicked", lambda *_: set_dict_selection("all"))
            btn_sel_none.connect("clicked", lambda *_: set_dict_selection("none"))
            btn_sel_def.connect("clicked", lambda *_: set_dict_selection("default"))
            d_btn_box.pack_start(btn_sel_all, False, False, 0)
            d_btn_box.pack_start(btn_sel_none, False, False, 0)
            d_btn_box.pack_start(btn_sel_def, False, False, 0)
            if allow_copy_win:
                btn_copy_win = Gtk.Button(label="📋 複製自查詢視窗")
                btn_copy_win.connect("clicked", lambda *_: set_dict_selection("copy_win"))
                d_btn_box.pack_end(btn_copy_win, False, False, 0)
            page.pack_start(d_btn_box, False, False, 0)
            return page

        dict_notebook.append_page(
            build_dict_profile_page(
                "enabled_dicts",
                "用於面板輸入框以「無焦點查詢視窗」顯示結果時",
                DEFAULT_PREFERRED_DICTS,
                allow_copy_win=False,
            ),
            Gtk.Label(label="🪟 無焦點查詢視窗"),
        )
        dict_notebook.append_page(
            build_dict_profile_page(
                "enabled_dicts_dbus",
                "用於面板輸入框以「D-Bus 氣泡框」顯示結果時（建議精選 2~5 套簡明詞典）",
                DEFAULT_PREFERRED_DICTS[:5],
                allow_copy_win=True,
            ),
            Gtk.Label(label="💬 D-Bus 氣泡框"),
        )
        dict_notebook.append_page(
            build_dict_profile_page(
                "enabled_dicts_scan",
                "用於滑鼠反白自動劃詞或快捷鍵 Scan Selection 查詢時",
                DEFAULT_PREFERRED_DICTS[:5],
                allow_copy_win=True,
            ),
            Gtk.Label(label="✂ Scan Selection 劃詞"),
        )
        tab2.pack_start(dict_notebook, True, True, 0)

        notebook.append_page(tab2, Gtk.Label(label=f"📚 詞典管理 ({len(self.dict_mgr.all_dicts)} 套)"))

        outer.pack_start(notebook, True, True, 0)

        # Live apply bindings for Tab 1
        def rgba_to_hex(rgba):
            r = max(0, min(255, int(round(rgba.red * 255))))
            g = max(0, min(255, int(round(rgba.green * 255))))
            b = max(0, min(255, int(round(rgba.blue * 255))))
            return f"#{r:02x}{g:02x}{b:02x}"

        def apply_tab1(*_):
            self.config["hotkey_open_window"] = entry_hk_open.get_text().strip()
            self.config["hotkey_scan_selection"] = entry_hk_scan.get_text().strip()
            self.config["scan_selection_enabled"] = chk_scan_en.get_active()
            self.config["scan_modifier"] = combo_mod.get_active_id() or "ctrl"
            self.config["calc_auto_copy"] = chk_auto_copy.get_active()
            self.config["google_translate_enabled"] = chk_gt_en.get_active()
            self.config["google_translate_fallback"] = chk_gt_fb.get_active()
            self.config["google_translate_target"] = combo_gt_tl.get_active_id() or "zh-TW"
            self.config["google_translate_reverse"] = combo_gt_rev.get_active_id() or "en"
            chk_gt_fb.set_sensitive(chk_gt_en.get_active())
            self.config["result_output_mode"] = combo_out.get_active_id() or "window"
            self.config["dbus_popup_style"] = combo_dbus_style.get_active_id() or "separate"
            self.config["dbus_popup_max_dicts"] = int(spin_dbus_max.get_value_as_int())
            self.config["dbus_popup_timeout_sec"] = int(spin_dbus_sec.get_value_as_int())
            self.config["dbus_popup_font_size"] = int(spin_dbus_fs.get_value_as_int())
            self.config["dbus_popup_position"] = combo_bubble_pos.get_active_id() or "top_center"
            self.config["dbus_popup_x_offset"] = int(spin_dbus_xoff.get_value_as_int())
            self.config["dbus_popup_y_offset"] = int(spin_dbus_yoff.get_value_as_int())
            self.config["dbus_popup_bg_color"] = rgba_to_hex(bubble_bg_btn.get_rgba())
            self.config["dbus_popup_fg_color"] = rgba_to_hex(bubble_fg_btn.get_rgba())
            b_op = max(0, min(100, int(round(scale_bubble_alpha.get_value()))))
            self.config["dbus_popup_bg_opacity"] = b_op
            lbl_bubble_alpha.set_text(f"{b_op}%")
            self.config["display_mode"] = "badge" if rb_badge.get_active() else "panel_entry"
            self.config["panel_entry_width"] = int(spin_entry_w.get_value_as_int())
            self.config["result_font_size"] = int(spin_res_fs.get_value_as_int())
            self.config["font_color_mode"] = "dynamic" if rb_col_dyn.get_active() else "custom"
            self.config["custom_font_color"] = rgba_to_hex(col_btn.get_rgba())
            self.config["badge_bg_color"] = rgba_to_hex(bg_col_btn.get_rgba())
            bg_op = max(0, min(100, int(round(scale_bg_alpha.get_value()))))
            self.config["badge_bg_opacity"] = bg_op
            lbl_bg_alpha.set_text(f"{bg_op}%")
            self.config["text_shadow"] = chk_shadow.get_active()
            self._save_config()
            self._sync_scan_poll_timer()
            self.rebind_global_hotkeys()
            self._apply_panel_layout()
            self._update_tooltip()
            if self.popup_win is not None:
                self.popup_win.sync_controls()

        def apply_bg_preset(hex_col, opacity_val):
            r_tmp = Gdk.RGBA()
            if r_tmp.parse(hex_col):
                bg_col_btn.set_rgba(r_tmp)
            scale_bg_alpha.set_value(float(opacity_val))
            apply_tab1()

        def apply_bubble_preset(bg_hex, fg_hex, opacity_val):
            r_bg = Gdk.RGBA()
            if r_bg.parse(bg_hex):
                bubble_bg_btn.set_rgba(r_bg)
            r_fg = Gdk.RGBA()
            if r_fg.parse(fg_hex):
                bubble_fg_btn.set_rgba(r_fg)
            scale_bubble_alpha.set_value(float(opacity_val))
            apply_tab1()

        btn_apply_hk.connect("clicked", apply_tab1)
        combo_mod.connect("changed", apply_tab1)
        combo_gt_tl.connect("changed", apply_tab1)
        combo_gt_rev.connect("changed", apply_tab1)
        combo_out.connect("changed", apply_tab1)
        combo_dbus_style.connect("changed", apply_tab1)
        combo_bubble_pos.connect("changed", apply_tab1)
        col_btn.connect("color-set", lambda *_: (rb_col_cus.set_active(True), apply_tab1()))
        bg_col_btn.connect("color-set", apply_tab1)
        bubble_bg_btn.connect("color-set", apply_tab1)
        bubble_fg_btn.connect("color-set", apply_tab1)
        scale_bg_alpha.connect("value-changed", apply_tab1)
        scale_bubble_alpha.connect("value-changed", apply_tab1)
        btn_bg_dark.connect("clicked", lambda *_: apply_bg_preset("#182234", 90))
        btn_bg_light.connect("clicked", lambda *_: apply_bg_preset("#f2f5fa", 92))
        btn_bg_trans.connect("clicked", lambda *_: apply_bg_preset("#182234", 0))
        btn_b_dark.connect("clicked", lambda *_: apply_bubble_preset("#182234", "#f0f6ff", 92))
        btn_b_light.connect("clicked", lambda *_: apply_bubble_preset("#f8fafc", "#111827", 95))
        btn_b_oled.connect("clicked", lambda *_: apply_bubble_preset("#0b0f17", "#ffffff", 82))
        for w_c in (
            chk_scan_en,
            chk_auto_copy,
            chk_gt_en,
            chk_gt_fb,
            rb_badge,
            rb_entry,
            rb_col_dyn,
            rb_col_cus,
            chk_shadow,
        ):
            w_c.connect("toggled", apply_tab1)
        for sp_c in (
            spin_dbus_max,
            spin_dbus_sec,
            spin_dbus_fs,
            spin_dbus_xoff,
            spin_dbus_yoff,
            spin_entry_w,
            spin_res_fs,
        ):
            sp_c.connect("value-changed", apply_tab1)

        # Bottom Close Button
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_open_q = Gtk.Button(label="🔎 開啟查詢/計算視窗")
        btn_open_q.connect("clicked", lambda *_: self.open_popup_window(""))
        btn_close = Gtk.Button(label="關閉")
        btn_close.connect("clicked", lambda *_: win.destroy())
        btn_row.pack_start(btn_open_q, False, False, 0)
        btn_row.pack_end(btn_close, False, False, 0)
        outer.pack_end(btn_row, False, False, 0)

        win.add(outer)
        win.show_all()

    def show_about_dialog(self, *_args):
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("StarDict 離線詞典 & 科學計算機 Plus")
        dlg.set_version("1.0.0")
        dlg.set_comments(
            "MATE Panel 離線 StarDict 多詞典查詢與科學計算機 Applet。\n"
            "直連系統 sdcv 離線詞庫、支援滑鼠劃詞取詞、全域快捷鍵與計算結果自動存入剪貼簿。"
        )
        dlg.set_website("https://github.com/sophAi/matepanel-applet-plus")
        dlg.run()
        dlg.destroy()

    def _on_destroy(self, *_args):
        for tid_attr in ("_scan_poll_id", "_scan_debounce_id"):
            tid = getattr(self, tid_attr, None)
            if tid is not None:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                setattr(self, tid_attr, None)
        if HAS_KEYBINDER:
            for hk in self._bound_hotkeys:
                try:
                    Keybinder.unbind(hk)
                except Exception:
                    pass
        if self.popup_win is not None:
            self.popup_win.destroy()
            self.popup_win = None
        if hasattr(self, "bubble_mgr") and self.bubble_mgr is not None:
            self.bubble_mgr.close_all()
        if self.prefs_win is not None:
            self.prefs_win.destroy()
            self.prefs_win = None


def setup_applet_context_menu(applet, widget):
    action_group = Gtk.ActionGroup(name="DictCalcAppletActions")
    action_group.add_actions(
        [
            (
                "OpenQueryAction",
                Gtk.STOCK_FIND,
                "開啟查詢 / 計算機視窗 (_Search & Calc)...",
                None,
                "開啟離線詞典查詢與科學計算機視窗",
                lambda *_: widget.open_popup_window(""),
            ),
            (
                "ScanNowAction",
                Gtk.STOCK_SELECT_ALL,
                "立即查詢反白文字 (_Scan Selection Now)",
                None,
                "查詢或計算目前滑鼠反白選取的文字",
                widget.trigger_scan_selection_now,
            ),
            (
                "ToggleScanAction",
                Gtk.STOCK_YES,
                "開關自動劃詞取詞 (_Toggle Auto Scan)",
                None,
                "切換啟用或關閉滑鼠反白自動劃詞查詢",
                widget.toggle_scan_selection,
            ),
            (
                "PreferencesAction",
                Gtk.STOCK_PREFERENCES,
                "偏好設定與詞典管理 (_Preferences)...",
                None,
                "設定快捷鍵、劃詞取詞、自動複製答案與管理 41 套離線詞典",
                widget.show_preferences_dialog,
            ),
            (
                "AboutAction",
                Gtk.STOCK_ABOUT,
                "關於 (_About)...",
                None,
                "關於 StarDict 離線詞典 & 科學計算機 Plus",
                widget.show_about_dialog,
            ),
        ]
    )

    menu_xml = (
        '<menuitem name="OpenQuery" action="OpenQueryAction"/>'
        '<menuitem name="ScanNow" action="ScanNowAction"/>'
        '<menuitem name="ToggleScan" action="ToggleScanAction"/>'
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

    widget = DictCalcWidget(applet=applet)
    init_size = applet.get_size()
    if init_size:
        widget.set_panel_size(init_size)

    setup_applet_context_menu(applet, widget)

    applet.connect("button-press-event", widget.handle_applet_button_press)
    applet.connect("change-size", lambda _ap, sz: widget.set_panel_size(sz))

    applet.add(widget)
    applet.show_all()
    widget._sync_entry_visibility()


def applet_factory(applet, iid, _data):
    if iid not in ("DictCalcApplet", "MateDictCalcApplet"):
        return False
    applet_fill(applet)
    return True


def run_standalone_window():
    win = Gtk.Window(title="MATE StarDict & Calculator Applet - 預覽測試視窗")
    win.set_default_size(680, 175)
    win.set_position(Gtk.WindowPosition.CENTER)
    win.connect("destroy", Gtk.main_quit)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    vbox.set_margin_top(16)
    vbox.set_margin_bottom(16)
    vbox.set_margin_start(16)
    vbox.set_margin_end(16)

    info_lbl = Gtk.Label(xalign=0.0)
    info_lbl.set_markup(
        "<b>MATE Panel 模擬預覽列 — StarDict 離線詞典 &amp; 🧮 科學計算機 Plus</b>\n"
        "<small>• <b>左鍵點選</b> <tt>[DICT]</tt>（或按預設快捷鍵 <tt>Ctrl+Alt+D</tt>）：開啟查詢與計算機視窗\n"
        "• <b>中鍵點選</b> <tt>[DICT]</tt>：開關滑鼠劃詞查詢 | 快捷鍵 <tt>Ctrl+Alt+S</tt>：立即查詢滑鼠反白文字</small>"
    )
    vbox.pack_start(info_lbl, False, False, 0)

    panel_frame = Gtk.EventBox()
    css_provider = Gtk.CssProvider()
    css_provider.load_from_data(
        b".simulated-panel-dark { background-color: #1e2229; border-radius: 6px; padding: 6px; }"
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

    widget = DictCalcWidget(applet=None)
    widget.set_panel_size(32)
    hbox.pack_start(widget, False, False, 0)

    btn_prefs = Gtk.Button(label="⚙️ 偏好設定 & 詞典管理")
    btn_prefs.connect("clicked", widget.show_preferences_dialog)
    hbox.pack_end(btn_prefs, False, False, 0)

    btn_open = Gtk.Button(label="🔎 開啟查詢/計算視窗")
    btn_open.connect("clicked", lambda *_: widget.open_popup_window(""))
    hbox.pack_end(btn_open, False, False, 0)

    panel_frame.add(hbox)
    vbox.pack_start(panel_frame, False, False, 0)

    win.add(vbox)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    if "--window" in sys.argv or "-w" in sys.argv:
        run_standalone_window()
    elif "--test-query" in sys.argv:
        idx = sys.argv.index("--test-query")
        q = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "hello"
        calc = SafeMathCalculator()
        is_m, res, err = calc.try_evaluate(q, record_history=True)
        if is_m:
            print(json.dumps({"mode": "calc", "expr": q, "result": res, "error": err}, ensure_ascii=False, indent=2))
        else:
            mgr = StarDictManager()
            d_names = mgr.get_dicts_for_group("custom", DEFAULT_PREFERRED_DICTS)
            hits = mgr.query_sync(q, dict_names=d_names)
            print(json.dumps({"mode": "dict", "query": q, "total_dicts": len(mgr.all_dicts), "hits": len(hits), "first_hit": hits[0] if hits else None}, ensure_ascii=False, indent=2))
    else:
        if not HAS_MATE_PANEL:
            sys.stderr.write("Error: MatePanelApplet GI bindings not available.\n")
            sys.exit(1)
        MatePanelApplet.Applet.factory_main(
            "DictCalcAppletFactory",
            True,
            MatePanelApplet.Applet.__gtype__,
            applet_factory,
            None,
        )
