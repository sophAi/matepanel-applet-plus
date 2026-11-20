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
import datetime
import glob
import html
import json
import math
import operator
import os
import random
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

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
VOCAB_PATH = os.path.join(CONFIG_DIR, "vocab-history.json")

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

# Virtual online translation engines that can be enabled & prioritized alongside offline StarDict dicts
VIRTUAL_TRANSLATE_DICTS = [
    {"bookname": "🌐 Google 翻譯", "engine": "google", "group": "online_tr", "wordcount": 0},
    {"bookname": "🌐 Gemini AI 翻譯", "engine": "gemini", "group": "online_tr", "wordcount": 0},
    {"bookname": "🌐 OpenAI 相容 (Groq/Ollama)", "engine": "openai_compat", "group": "online_tr", "wordcount": 0},
    {"bookname": "🌐 DeepL 翻譯", "engine": "deepl", "group": "online_tr", "wordcount": 0},
]
VIRTUAL_DICT_ENGINE_MAP = {d["bookname"]: d["engine"] for d in VIRTUAL_TRANSLATE_DICTS}

DEFAULT_CONFIG = {
    "display_mode": "panel_entry",       # "panel_entry" (純面板查詢輸入框，預設) or "badge" (精簡方塊圖示)
    "result_output_mode": "window",      # "window" (無焦點精簡結果視窗) or "dbus_popup" (氣泡框彈出模式)
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
    "google_translate_enabled": True,    # 句子/段落自動使用線上/本機翻譯 (雙向翻譯)
    "google_translate_fallback": True,   # 離線詞典查無短片語時自動 Fallback 至翻譯引擎
    "google_translate_target": "zh-TW",  # 外文翻譯目標語言 ("zh-TW", "zh-CN", "ja", "en")
    "google_translate_reverse": "en",    # 中文反向翻譯目標語言 ("en", "ja")
    "translate_engine": "google",        # "google", "gemini", "openai_compat", "deepl"
    "translate_fallback_google": True,   # 所選引擎超額或連線失敗時自動降級回 Google 翻譯
    "gemini_api_key": "",                # Gemini API Free Tier Key (Google AI Studio)
    "gemini_model": "gemini-3.1-flash-lite",
    "openai_base_url": "https://api.groq.com/openai/v1",  # 支援 Groq / OpenRouter / 本機 Ollama
    "openai_api_key": "",
    "openai_model": "llama-3.3-70b-versatile",
    "deepl_api_key": "",                 # DeepL API Free (:fx) 或 Pro Key
    "hotkey_open_window": "<Ctrl><Alt>d",# 聚焦面板輸入框 / 呼叫查詢的全域快捷鍵
    "hotkey_scan_selection": "<Ctrl><Alt>s", # 立即查詢目前滑鼠反白文字 (Scan Selection) 的全域快捷鍵
    "calc_auto_copy": True,              # 數學計算模式預設自動將結果存入系統剪貼簿 (可直接 Ctrl+V)
    "active_dict_group": "custom",       # "custom" (已勾選常用), "en_zh", "ja_zh", "sci_en", "all"
    "dict_root_dir": "/usr/share/stardict/dic",  # StarDict 詞典根目錄路徑 (留空則掃描系統預設路徑)
    "enabled_dicts": list(DEFAULT_PREFERRED_DICTS),           # 無焦點查詢視窗詞典清單 (含順位)
    "enabled_dicts_dbus": list(DEFAULT_PREFERRED_DICTS[:5]),  # D-Bus 氣泡框詞典清單 (含順位)
    "enabled_dicts_scan": list(DEFAULT_PREFERRED_DICTS[:5]),  # Scan Selection 劃詞詞典清單 (含順位)
    "vocab_record_enabled": True,        # 自動紀錄傳統 StarDict 詞典查詢命中的單字
    "vocab_card_dicts": ["DrEye4in1詞典", "Collins Cobuild English Dictionary", "英文字根字典"],
    "vocab_sort_mode": "recent",         # "recent", "freq", "alpha", "random"
    "vocab_epub_layout": "per_word",     # "per_word" (一字一頁，預設) or "az_chapters" (依字母 A-Z 分章)
    "result_font_size": 11,              # 結果視窗字型大小 (pt)
    "font_color_mode": "dynamic",        # "dynamic" or "custom"
    "custom_font_color": "#ffffff",      # 自訂面板字形顏色
    "badge_bg_style": "dark",            # (舊版相容欄位)
    "badge_bg_color": "#182234",         # 自訂方塊/輸入框背景底色
    "badge_border_color": "#48b8fa",     # 自訂方塊/輸入框外框顏色
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
        self._cancel_timer()
        if timeout_sec is None or int(timeout_sec) <= 0:
            # Pinned mode: never auto-dismiss (click bubble or press Esc to close)
            self._timeout_sec = 0
            return
        self._timeout_sec = max(1, int(timeout_sec))
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
        if not self._hovered and self._timeout_sec > 0:
            self.dismiss()
        return False

    def _on_enter(self, *_args):
        self._hovered = True
        self._cancel_timer()
        return False

    def _on_leave(self, *_args):
        self._hovered = False
        if self._timeout_sec > 0:
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

    def _build_llm_prompt(self, target_lang, concise_word=False):
        tgt_name = self.lang_label(target_lang)
        if target_lang == "zh-TW":
            style = "自然流暢的台灣繁體中文，貼近母語語氣與慣用語，保留專有名詞與技術縮寫"
        elif target_lang == "zh-CN":
            style = "自然流畅的简体中文，保留专有名词与技术缩写"
        else:
            style = f"natural, idiomatic {tgt_name}, preserving proper nouns and technical terms"
        if concise_word:
            return (
                f"You are a concise bilingual dictionary. Explain/translate the given word or short phrase into {style}. "
                f"List the main part(s) of speech and 1-3 concise meanings, followed by 1 short natural example sentence with translation. "
                f"Keep the entire response compact (within 4-6 short lines) with no preamble."
            )
        return (
            f"You are a professional translator. Translate the user's text into {style}. "
            f"Output ONLY the translation itself, with no explanations, quotes, or extra commentary. "
            f"Preserve original paragraph breaks."
        )

    def _translate_google(self, text, tl, timeout=6.0):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        payload = urllib.parse.urlencode({"q": text}).encode("utf-8")
        last_err = None
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
                    return translated.strip(), src_lang, None
                last_err = "Google 翻譯未回傳譯文"
            except urllib.error.HTTPError as exc:
                last_err = f"Google HTTP 錯誤 ({exc.code})"
            except urllib.error.URLError as exc:
                last_err = f"Google 網路連線失敗：{exc.reason}"
            except Exception as exc:
                last_err = f"Google 翻譯失敗：{exc}"
        return None, "auto", last_err or "Google 翻譯失敗"

    @classmethod
    def list_gemini_models_sync(cls, api_key, timeout=8.0):
        """Query Gemini `/v1beta/models` and return sorted text-capable models (newest flash-lite first)."""
        api_key = (api_key or "").strip()
        if not api_key:
            return [], "請先輸入 Gemini API Key"
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}"
        req = urllib.request.Request(url, headers={"User-Agent": "MatePanel-DictCalc/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            return [], f"取得 Gemini 模型清單失敗 (HTTP {exc.code})"
        except Exception as exc:
            return [], f"取得 Gemini 模型清單失敗：{exc}"

        skip_kw = ("image", "tts", "audio", "embedding", "aqa", "vision", "veo", "imagen", "robotics", "computer-use")
        models = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            raw_name = str(m.get("name", ""))
            mid = raw_name.split("/", 1)[-1] if "/" in raw_name else raw_name
            low = mid.lower()
            if "gemini" not in low or any(k in low for k in skip_kw):
                continue
            models.append(mid)

        def sort_key(mid):
            low = mid.lower()
            ver_m = re.search(r"gemini-(\d+)(?:\.(\d+))?", low)
            ver = (int(ver_m.group(1)), int(ver_m.group(2) or 0)) if ver_m else (0, 0)
            is_not_latest = 0 if low.endswith("-latest") else 1
            is_flash_lite = 1 if "flash-lite" in low else 0
            is_flash = 1 if "flash" in low else 0
            is_stable = 0 if ("preview" in low or "exp" in low) else 1
            return (is_not_latest, is_flash_lite, is_flash, is_stable, ver, mid)

        models = sorted(set(models), key=sort_key, reverse=True)
        return models, None

    @classmethod
    def list_openai_models_sync(cls, base_url, api_key, timeout=8.0):
        """Query OpenAI-compatible `/models` endpoint (Groq / OpenRouter / Local Ollama)."""
        base = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[:-len("/chat/completions")]
        url = base + "/models"
        is_local = ("127.0.0.1" in base) or ("localhost" in base)
        api_key = (api_key or "").strip()
        if not api_key and not is_local:
            return [], "請先輸入 API Key（本機 Ollama 可留空）"
        headers = {"User-Agent": "MatePanel-DictCalc/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            return [], f"取得模型清單失敗 (HTTP {exc.code})"
        except Exception as exc:
            return [], f"取得模型清單失敗：{exc}"

        skip_kw = ("whisper", "tts", "embed", "dall-e", "moderation", "guard", "audio", "speech")
        items = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        models = []
        for it in (items or []):
            mid = str(it.get("id", "")) if isinstance(it, dict) else str(it)
            if mid and not any(k in mid.lower() for k in skip_kw):
                models.append(mid)
        models = sorted(set(models))
        return models, None

    def _translate_gemini(self, text, tl, api_key, model, timeout=12.0, on_model_migrated=None, concise_word=False):
        api_key = (api_key or "").strip()
        if not api_key:
            return None, "auto", "尚未設定 Gemini API Key"
        primary_model = (model or "gemini-3.1-flash-lite").strip()

        def try_model(model_id):
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{urllib.parse.quote(model_id)}:generateContent?key={urllib.parse.quote(api_key)}"
            )
            gen_cfg = {"temperature": 0.2}
            if "flash" in model_id:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
            body_obj = {
                "system_instruction": {"parts": [{"text": self._build_llm_prompt(tl, concise_word=concise_word)}]},
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": gen_cfg,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body_obj).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                cands = data.get("candidates") or []
                if cands:
                    parts = cands[0].get("content", {}).get("parts") or []
                    translated = "".join(str(p.get("text", "")) for p in parts).strip()
                    if translated:
                        src = "zh" if tl == "en" and any("\u4e00" <= c <= "\u9fff" for c in text) else "auto"
                        return translated, src, None, 200
                return None, "auto", "Gemini 未回傳譯文", 200
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    err_j = json.loads(exc.read().decode("utf-8", errors="replace"))
                    detail = str(err_j.get("error", {}).get("message", ""))[:120]
                except Exception:
                    pass
                return None, "auto", f"Gemini HTTP 錯誤 ({exc.code})" + (f": {detail}" if detail else ""), exc.code
            except urllib.error.URLError as exc:
                return None, "auto", f"Gemini 連線失敗：{exc.reason}", 0
            except Exception as exc:
                return None, "auto", f"Gemini 錯誤：{exc}", 0

        tr, src, err, code = try_model(primary_model)
        if err is None and tr:
            return tr, src, None

        # Self-healing on HTTP 404 (retired/deprecated model): dynamically discover latest available model
        if code == 404:
            avail, _ = self.list_gemini_models_sync(api_key, timeout=8.0)
            candidates = [m for m in avail if m != primary_model][:3]
            if not candidates and primary_model != "gemini-3.1-flash-lite":
                candidates = ["gemini-3.1-flash-lite"]
            for cand_id in candidates:
                tr2, src2, err2, _code2 = try_model(cand_id)
                if err2 is None and tr2:
                    if on_model_migrated is not None:
                        try:
                            on_model_migrated(cand_id)
                        except Exception:
                            pass
                    return tr2, src2, None
                err = err2

        return None, "auto", err or "Gemini 翻譯失敗"

    def _translate_openai_compat(self, text, tl, base_url, api_key, model, timeout=12.0, concise_word=False):
        base = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
        if not base.endswith("/chat/completions"):
            url = base + "/chat/completions"
        else:
            url = base
        is_local = ("127.0.0.1" in base) or ("localhost" in base)
        api_key = (api_key or "").strip()
        if not api_key and not is_local:
            return None, "auto", "尚未設定 OpenAI 相容 API Key"
        model_id = (model or "llama-3.3-70b-versatile").strip()
        body_obj = {
            "model": model_id,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": self._build_llm_prompt(tl, concise_word=concise_word)},
                {"role": "user", "content": text},
            ],
        }
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "MatePanel-DictCalc/1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body_obj).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            choices = data.get("choices") or []
            if choices:
                translated = str(choices[0].get("message", {}).get("content", "")).strip()
                # Strip accidental <think>...</think> reasoning blocks if a reasoning model is selected
                translated = re.sub(r"<think>.*?</think>\s*", "", translated, flags=re.DOTALL).strip()
                if translated:
                    src = "zh" if tl == "en" and any("\u4e00" <= c <= "\u9fff" for c in text) else "auto"
                    return translated, src, None
            return None, "auto", "OpenAI 相容端點未回傳譯文"
        except urllib.error.HTTPError as exc:
            return None, "auto", f"LLM API HTTP 錯誤 ({exc.code})"
        except urllib.error.URLError as exc:
            return None, "auto", f"LLM API 連線失敗：{exc.reason}"
        except Exception as exc:
            return None, "auto", f"LLM API 錯誤：{exc}"

    def _translate_deepl(self, text, tl, api_key, timeout=8.0):
        api_key = (api_key or "").strip()
        if not api_key:
            return None, "auto", "尚未設定 DeepL API Key"
        url = (
            "https://api-free.deepl.com/v2/translate"
            if api_key.endswith(":fx")
            else "https://api.deepl.com/v2/translate"
        )
        tl_map = {"zh-TW": "ZH-HANT", "zh-CN": "ZH-HANS", "en": "EN-US", "ja": "JA"}
        dl_tl = tl_map.get(tl, tl.upper())
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": [text], "target_lang": dl_tl}).encode("utf-8"),
            headers={
                "Authorization": f"DeepL-Auth-Key {api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            trs = data.get("translations") or []
            if trs:
                translated = str(trs[0].get("text", "")).strip()
                src = str(trs[0].get("detected_source_language", "auto")).lower()
                if translated:
                    return translated, src, None
            return None, "auto", "DeepL 未回傳譯文"
        except urllib.error.HTTPError as exc:
            return None, "auto", f"DeepL HTTP 錯誤 ({exc.code})"
        except urllib.error.URLError as exc:
            return None, "auto", f"DeepL 連線失敗：{exc.reason}"
        except Exception as exc:
            return None, "auto", f"DeepL 錯誤：{exc}"

    def translate_sync(
        self,
        text,
        primary_tl="zh-TW",
        reverse_tl="en",
        config=None,
        timeout=8.0,
        on_model_migrated=None,
        engine_override=None,
        concise_word=False,
    ):
        """
        Multi-engine synchronous translator (Google / Gemini / OpenAI-compat / DeepL)
        with automatic 404 model discovery and automatic fallback to Google Translate.
        Returns: (translated_text, detected_src_lang, target_lang, engine_label, error_msg)
        """
        cfg = config or {}
        engine = engine_override or cfg.get("translate_engine", "google")
        fallback_google = bool(cfg.get("translate_fallback_google", True))
        tl = self.detect_target_lang(text, primary_tl=primary_tl, reverse_tl=reverse_tl)

        model_tag = ""
        if engine == "gemini":
            model_tag = cfg.get("gemini_model", "gemini-3.1-flash-lite")
        elif engine == "openai_compat":
            model_tag = cfg.get("openai_model", "llama-3.3-70b-versatile")
        cache_key = (engine, model_tag, bool(concise_word), text, tl)
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                tr_text, src_lang, eng_lbl = self._cache[cache_key]
                return tr_text, src_lang, tl, eng_lbl, None

        eng_lbl = "Google 翻譯"
        if engine == "gemini":
            eng_lbl = "Gemini"
            tr_text, src_lang, err = self._translate_gemini(
                text,
                tl,
                cfg.get("gemini_api_key", ""),
                cfg.get("gemini_model", "gemini-3.1-flash-lite"),
                timeout=max(timeout, 12.0),
                on_model_migrated=on_model_migrated,
                concise_word=concise_word,
            )
        elif engine == "openai_compat":
            base_u = cfg.get("openai_base_url", "https://api.groq.com/openai/v1")
            if "groq.com" in base_u:
                eng_lbl = "Groq LLM"
            elif "127.0.0.1" in base_u or "localhost" in base_u:
                eng_lbl = "本機 Ollama"
            elif "openrouter.ai" in base_u:
                eng_lbl = "OpenRouter"
            else:
                eng_lbl = "OpenAI 相容"
            tr_text, src_lang, err = self._translate_openai_compat(
                text,
                tl,
                base_u,
                cfg.get("openai_api_key", ""),
                cfg.get("openai_model", "llama-3.3-70b-versatile"),
                timeout=max(timeout, 12.0),
                concise_word=concise_word,
            )
        elif engine == "deepl":
            eng_lbl = "DeepL"
            tr_text, src_lang, err = self._translate_deepl(
                text, tl, cfg.get("deepl_api_key", ""), timeout=timeout
            )
        else:
            tr_text, src_lang, err = self._translate_google(text, tl, timeout=timeout)

        # Automatic fallback to Google Translate if primary engine fails
        if (err is not None or not tr_text) and engine != "google" and fallback_google:
            g_text, g_src, g_err = self._translate_google(text, tl, timeout=6.0)
            if g_err is None and g_text:
                tr_text, src_lang, err = g_text, g_src, None
                eng_lbl = "Google 翻譯 (備援)"

        if err is None and tr_text:
            with self._lock:
                self._cache[cache_key] = (tr_text, src_lang, eng_lbl)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            return tr_text, src_lang, tl, eng_lbl, None

        return None, "auto", tl, eng_lbl, err or "翻譯失敗"


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

    def __init__(self, root_dir=""):
        self.root_dir = (root_dir or "").strip()
        self.all_dicts = self.discover_dictionaries(self.root_dir)

    def reload(self, root_dir=""):
        self.root_dir = (root_dir or "").strip()
        self.all_dicts = self.discover_dictionaries(self.root_dir)
        return self.all_dicts

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
    def discover_dictionaries(cls, root_dir=""):
        """
        Scan custom `root_dir` (if provided) or default `/usr/share/stardict/dic` and `~/.stardict/dic`
        for StarDict `.ifo` files (up to 3 levels deep).
        Returns list of dicts: [{'bookname': str, 'wordcount': int, 'group': str, 'path': str}]
        """
        found = []
        seen_names = set()
        custom = os.path.expanduser((root_dir or "").strip())
        search_dirs = [custom] if custom else list(DICT_SEARCH_DIRS)

        for base_dir in search_dirs:
            if not base_dir or not os.path.isdir(base_dir):
                continue
            ifo_paths = []
            base_depth = base_dir.rstrip(os.sep).count(os.sep)
            for cur_root, dirnames, filenames in os.walk(base_dir):
                depth = cur_root.rstrip(os.sep).count(os.sep) - base_depth
                if depth >= 3:
                    dirnames[:] = []
                for fn in sorted(filenames):
                    if fn.endswith(".ifo"):
                        ifo_paths.append(os.path.join(cur_root, fn))
            for ifo_path in sorted(ifo_paths):
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

    def query_sync(self, word, dict_names=None, timeout=6.0, exact=True):
        """
        Query StarDict via `sdcv -n -j [-e]`.
        Uses `--exact-search` (`-e`) by default so misspelled words do NOT flood with unrelated
        fuzzy matches and instead cleanly trigger aspell spelling suggestions.
        Returns list of [{'dict': str, 'word': str, 'definition': str}]
        """
        w = word.strip()
        if not w:
            return []

        def _run_sdcv(target_word):
            cmd = [self.SDCV_BIN, "-n", "-j"]
            if exact:
                cmd.append("-e")
            custom_dir = os.path.expanduser(self.root_dir) if self.root_dir else ""
            if custom_dir and os.path.isdir(custom_dir):
                cmd.extend(["-2", custom_dir, "-x"])
            if dict_names:
                for dn in dict_names:
                    cmd.extend(["-u", dn])
            cmd.append(target_word)
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
            idx_bracket = out.find("[")
            if idx_bracket < 0:
                return []
            items = json.loads(out[idx_bracket:])
            results = []
            for it in items:
                d_name = it.get("dict", "Unknown")
                w_res = it.get("word", target_word)
                defn = self.clean_definition(it.get("definition", ""))
                if defn:
                    results.append({"dict": d_name, "word": w_res, "definition": defn})
            if dict_names:
                order_map = {name: i for i, name in enumerate(dict_names)}
                results.sort(key=lambda r: order_map.get(r["dict"], 999999))
            return results

        try:
            res = _run_sdcv(w)
            if not res and w != w.lower():
                res = _run_sdcv(w.lower())
            return res
        except Exception:
            return []

    @staticmethod
    def is_single_english_word(text):
        """Return True iff `text` is a single pure-English word (length >= 3, no whitespace)."""
        s = (text or "").strip()
        return len(s) >= 3 and bool(re.fullmatch(r"[A-Za-z][A-Za-z'-]{1,40}", s))

    @classmethod
    def suggest_spelling_sync(cls, word, max_suggestions=6, timeout=2.0):
        """
        Use system `/usr/bin/aspell -a --lang=en` (~15ms, 0 MB idle RAM) to suggest
        likely correct spellings for a misspelled single English word.
        """
        w = (word or "").strip()
        if not cls.is_single_english_word(w):
            return []
        aspell_bin = "/usr/bin/aspell"
        if not os.path.isfile(aspell_bin):
            return []
        try:
            proc = subprocess.run(
                [aspell_bin, "-a", "--lang=en"],
                input=w + "\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            suggestions = []
            w_low = w.lower()
            for line in proc.stdout.splitlines():
                if line.startswith("& ") and ":" in line:
                    rhs = line.split(":", 1)[1].strip()
                    for cand in rhs.split(","):
                        c = cand.strip()
                        if c and c.lower() != w_low and c not in suggestions:
                            suggestions.append(c)
                            if len(suggestions) >= max_suggestions:
                                break
                    break
            return suggestions
        except Exception:
            return []


class VocabHistoryManager:
    """
    Lightweight vocabulary history store for traditional StarDict dictionary lookups,
    plus zero-dependency HTML (interactive flashcard) & EPUB 3.0 (e-reader) exporters.
    """

    def __init__(self, path=VOCAB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._words = {}  # key (lowercased) -> {'word': str, 'count': int, 'first_seen': str, 'last_seen': str}
        self.load()

    def load(self):
        with self._lock:
            self._words = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get("words"), dict):
                        self._words = data["words"]
                except Exception:
                    self._words = {}

    def _save_unlocked(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"words": self._words}, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def record_word(self, word):
        w = (word or "").strip()
        if not w or "\n" in w or len(w) > 80:
            return
        key = w.lower()
        now_iso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            entry = self._words.get(key)
            if entry is None:
                self._words[key] = {
                    "word": w,
                    "count": 1,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                }
            else:
                entry["count"] = int(entry.get("count", 0)) + 1
                entry["last_seen"] = now_iso
            self._save_unlocked()

    def get_entries(self, filter_mode="all", sort_mode="recent"):
        with self._lock:
            items = [dict(v) for v in self._words.values()]

        now = datetime.datetime.now()
        if filter_mode in ("7d", "30d"):
            days = 7 if filter_mode == "7d" else 30
            cutoff = now - datetime.timedelta(days=days)
            filtered = []
            for it in items:
                try:
                    dt = datetime.datetime.strptime(it.get("last_seen", "")[:19], "%Y-%m-%d %H:%M:%S")
                    if dt >= cutoff:
                        filtered.append(it)
                except Exception:
                    pass
            items = filtered
        elif filter_mode == "freq2":
            items = [it for it in items if int(it.get("count", 1)) >= 2]

        if sort_mode == "freq":
            items.sort(key=lambda x: (-int(x.get("count", 1)), x.get("last_seen", "")), reverse=False)
        elif sort_mode == "alpha":
            items.sort(key=lambda x: x.get("word", "").lower())
        elif sort_mode == "random":
            random.shuffle(items)
        else:  # recent
            items.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
        return items

    def delete_words(self, words_list):
        if not words_list:
            return
        keys = {w.strip().lower() for w in words_list if w}
        with self._lock:
            for k in keys:
                self._words.pop(k, None)
            self._save_unlocked()

    def clear_all(self):
        with self._lock:
            self._words = {}
            self._save_unlocked()

    @staticmethod
    def export_html(filepath, cards, title="StarDict 單字卡"):
        """
        Export cards = [{'word': str, 'count': int, 'last_seen': str, 'entries': [{'dict': str, 'word': str, 'definition': str}]}]
        into a single self-contained interactive HTML flashcard file (flip mode + list mode + live shuffle + search).
        """
        gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        card_html_parts = []
        for idx, c in enumerate(cards, start=1):
            w_esc = html.escape(c["word"])
            cnt = int(c.get("count", 1))
            entries_parts = []
            for e in c.get("entries", []):
                d_esc = html.escape(e["dict"])
                ew_esc = html.escape(e["word"])
                def_esc = html.escape(e["definition"]).replace("\n", "<br>")
                entries_parts.append(
                    f'<div class="dict-block">'
                    f'<div class="dict-name">📖 {d_esc} <span class="headword">【{ew_esc}】</span></div>'
                    f'<div class="dict-def">{def_esc}</div>'
                    f'</div>'
                )
            if not entries_parts:
                entries_parts.append('<div class="dict-empty">（所選詞典無此詞條釋義）</div>')
            card_html_parts.append(
                f'<article class="card" data-word="{w_esc.lower()}" onclick="this.classList.toggle(\'revealed\')">'
                f'<div class="card-head">'
                f'<span class="idx">#{idx}</span>'
                f'<span class="word">{w_esc}</span>'
                f'<span class="badge">查詢 {cnt} 次</span>'
                f'</div>'
                f'<div class="card-body">{"".join(entries_parts)}</div>'
                f'</article>'
            )

        doc = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  --bg: #f4f6f9; --card: #ffffff; --text: #111827; --muted: #64748b;
  --accent: #0284c7; --border: #e2e8f0; --badge-bg: #e0f2fe; --badge-fg: #0369a1;
}}
body.dark {{
  --bg: #0f172a; --card: #1e293b; --text: #f1f5f9; --muted: #94a3b8;
  --accent: #38bdf8; --border: #334155; --badge-bg: #0c4a6e; --badge-fg: #7dd3fc;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 18px; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Noto Sans CJK TC", "PingFang TC", sans-serif;
  line-height: 1.65;
}}
header {{
  max-width: 860px; margin: 0 auto 18px; padding: 14px 18px;
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
}}
header h1 {{ margin: 0 0 6px; font-size: 1.3rem; }}
.meta {{ color: var(--muted); font-size: 0.88rem; margin-bottom: 10px; }}
.toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.toolbar input {{
  flex: 1; min-width: 180px; padding: 7px 11px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
}}
.toolbar button {{
  padding: 7px 12px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); cursor: pointer; font-size: 0.9rem;
}}
.toolbar button:hover {{ border-color: var(--accent); color: var(--accent); }}
main {{ max-width: 860px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }}
.card {{
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 18px; cursor: pointer; transition: border-color 0.15s;
}}
.card:hover {{ border-color: var(--accent); }}
.card-head {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
.idx {{ color: var(--muted); font-size: 0.85rem; }}
.word {{ font-size: 1.35rem; font-weight: 700; color: var(--accent); }}
.badge {{
  margin-left: auto; font-size: 0.78rem; padding: 2px 8px; border-radius: 999px;
  background: var(--badge-bg); color: var(--badge-fg);
}}
.card-body {{ margin-top: 10px; border-top: 1px dashed var(--border); padding-top: 10px; }}
body.flip-mode .card .card-body {{ display: none; }}
body.flip-mode .card.revealed .card-body {{ display: block; }}
body.flip-mode .card:not(.revealed)::after {{
  content: "（點擊卡片顯示釋義）"; display: block; margin-top: 6px;
  color: var(--muted); font-size: 0.84rem;
}}
.dict-block {{ margin-bottom: 10px; }}
.dict-block:last-child {{ margin-bottom: 0; }}
.dict-name {{ font-weight: 700; font-size: 0.92rem; color: var(--accent); margin-bottom: 3px; }}
.headword {{ color: var(--muted); font-weight: 500; }}
.dict-def {{ font-size: 0.96rem; white-space: normal; word-break: break-word; }}
.dict-empty {{ color: var(--muted); font-style: italic; }}
</style>
</head>
<body class="flip-mode">
<header>
  <h1>🗂 {html.escape(title)}</h1>
  <div class="meta">共 {len(cards)} 個單字 · 產生時間：{gen_time}</div>
  <div class="toolbar">
    <input id="q" type="search" placeholder="🔎 篩選單字..." oninput="filterCards()">
    <button onclick="toggleFlip()" id="btnFlip">📖 切換為全部展開</button>
    <button onclick="shuffleCards()">🎲 隨機洗牌</button>
    <button onclick="document.body.classList.toggle('dark')">🌙 深色/淺色</button>
  </div>
</header>
<main id="deck">
{"".join(card_html_parts)}
</main>
<script>
function toggleFlip() {{
  const isFlip = document.body.classList.toggle('flip-mode');
  document.getElementById('btnFlip').textContent = isFlip ? '📖 切換為全部展開' : '🃏 切換為翻卡測驗';
}}
function filterCards() {{
  const q = document.getElementById('q').value.trim().toLowerCase();
  document.querySelectorAll('.card').forEach(el => {{
    el.style.display = (!q || el.dataset.word.includes(q)) ? '' : 'none';
  }});
}}
function shuffleCards() {{
  const deck = document.getElementById('deck');
  const arr = Array.from(deck.children);
  for (let i = arr.length - 1; i > 0; i--) {{
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }}
  arr.forEach((el, idx) => {{
    el.classList.remove('revealed');
    const idxEl = el.querySelector('.idx');
    if (idxEl) idxEl.textContent = '#' + (idx + 1);
    deck.appendChild(el);
  }});
}}
</script>
</body>
</html>
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(doc)

    @staticmethod
    def export_epub(filepath, cards, layout="per_word", title="StarDict 單字卡"):
        """
        Export cards into a standards-compliant EPUB 3.0 ebook using Python stdlib `zipfile`.
        layout:
          - 'per_word'    : One word per XHTML page (default, ideal for e-ink flashcard flipping!)
          - 'az_chapters' : Grouped into A-Z alphabetical chapters
        """
        book_uuid = f"urn:uuid:{uuid.uuid4()}"
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        title_esc = html.escape(title)

        style_css = """body {
  font-family: serif; line-height: 1.65; margin: 1em; color: #111;
}
h1.word-title {
  font-size: 1.6em; margin: 0 0 0.25em 0; border-bottom: 2px solid #222; padding-bottom: 0.2em;
}
.meta { font-size: 0.85em; color: #555; margin-bottom: 0.9em; }
.dict-block { margin-bottom: 1em; }
.dict-name { font-weight: bold; font-size: 1.02em; margin-bottom: 0.25em; }
.dict-def { margin: 0; white-space: pre-wrap; }
.card-sep { border: 0; border-top: 1px dashed #999; margin: 1.2em 0; }
"""

        def render_card_xhtml_fragment(c):
            w_esc = html.escape(c["word"])
            cnt = int(c.get("count", 1))
            parts = [
                f'<h1 class="word-title">{w_esc}</h1>',
                f'<div class="meta">查詢次數：{cnt}</div>',
            ]
            entries = c.get("entries", [])
            if not entries:
                parts.append('<p class="meta">（所選詞典無此詞條釋義）</p>')
            else:
                for e in entries:
                    d_esc = html.escape(e["dict"])
                    ew_esc = html.escape(e["word"])
                    def_esc = html.escape(e["definition"]).replace("\n", "<br/>")
                    parts.append(
                        f'<div class="dict-block">'
                        f'<div class="dict-name">{d_esc}（{ew_esc}）</div>'
                        f'<p class="dict-def">{def_esc}</p>'
                        f'</div>'
                    )
            return "\n".join(parts)

        pages = []  # list of (filename, nav_label, xhtml_content)
        if layout == "az_chapters":
            groups = OrderedDict()
            for c in cards:
                ch = c["word"][0].upper() if c["word"] else "#"
                if not ("A" <= ch <= "Z"):
                    ch = "#"
                groups.setdefault(ch, []).append(c)
            for idx, (ch_lbl, grp_cards) in enumerate(groups.items(), start=1):
                fname = f"ch_{idx:03d}.xhtml"
                body_frags = [render_card_xhtml_fragment(c) for c in grp_cards]
                body_html = '\n<hr class="card-sep"/>\n'.join(body_frags)
                xhtml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    '<!DOCTYPE html>\n'
                    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-Hant">\n'
                    f'<head><title>{html.escape(ch_lbl)}</title>'
                    '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
                    f'<body>\n{body_html}\n</body>\n</html>\n'
                )
                pages.append((fname, f"{ch_lbl} ({len(grp_cards)})", xhtml))
        else:
            # Default: per_word (one word per page!)
            for idx, c in enumerate(cards, start=1):
                fname = f"word_{idx:04d}.xhtml"
                body_html = render_card_xhtml_fragment(c)
                xhtml = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    '<!DOCTYPE html>\n'
                    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-Hant">\n'
                    f'<head><title>{html.escape(c["word"])}</title>'
                    '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
                    f'<body>\n{body_html}\n</body>\n</html>\n'
                )
                pages.append((fname, c["word"], xhtml))

        # Navigation XHTML (EPUB 3) & NCX (EPUB 2 compatibility)
        nav_li = "\n".join(
            f'      <li><a href="{fname}">{html.escape(lbl)}</a></li>'
            for fname, lbl, _ in pages
        )
        nav_xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-Hant">\n'
            f'<head><title>{title_esc}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            '<body>\n'
            f'  <nav epub:type="toc" id="toc"><h1>{title_esc}</h1>\n    <ol>\n{nav_li}\n    </ol>\n  </nav>\n'
            '</body>\n</html>\n'
        )

        ncx_points = "\n".join(
            f'    <navPoint id="np-{i}" playOrder="{i}">'
            f'<navLabel><text>{html.escape(lbl)}</text></navLabel>'
            f'<content src="{fname}"/></navPoint>'
            for i, (fname, lbl, _) in enumerate(pages, start=1)
        )
        toc_ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            '  <head>\n'
            f'    <meta name="dtb:uid" content="{book_uuid}"/>\n'
            '    <meta name="dtb:depth" content="1"/>\n'
            '    <meta name="dtb:totalPageCount" content="0"/>\n'
            '    <meta name="dtb:maxPageNumber" content="0"/>\n'
            '  </head>\n'
            f'  <docTitle><text>{title_esc}</text></docTitle>\n'
            f'  <navMap>\n{ncx_points}\n  </navMap>\n'
            '</ncx>\n'
        )

        manifest_items = [
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="css" href="style.css" media-type="text/css"/>',
        ]
        spine_items = []
        for i, (fname, _lbl, _xhtml) in enumerate(pages, start=1):
            iid = f"p{i:04d}"
            manifest_items.append(f'    <item id="{iid}" href="{fname}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'    <itemref idref="{iid}"/>')

        content_opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="bookid">{book_uuid}</dc:identifier>\n'
            f'    <dc:title>{title_esc}</dc:title>\n'
            '    <dc:language>zh-Hant</dc:language>\n'
            '    <dc:creator>StarDict &amp; Calc Plus</dc:creator>\n'
            f'    <meta property="dcterms:modified">{now_utc}</meta>\n'
            '  </metadata>\n'
            '  <manifest>\n' + "\n".join(manifest_items) + '\n  </manifest>\n'
            '  <spine toc="ncx">\n' + "\n".join(spine_items) + '\n  </spine>\n'
            '</package>\n'
        )

        container_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>\n'
        )

        with zipfile.ZipFile(filepath, "w") as zf:
            # EPUB spec requires `mimetype` as the first uncompressed entry
            zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("OEBPS/style.css", style_css, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("OEBPS/toc.ncx", toc_ncx, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("OEBPS/content.opf", content_opf, compress_type=zipfile.ZIP_DEFLATED)
            for fname, _lbl, xhtml in pages:
                zf.writestr(f"OEBPS/{fname}", xhtml, compress_type=zipfile.ZIP_DEFLATED)


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

        # Compact Header Bar (Query title + close button only; NO search entry / scan / prefs icons)
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.query_title_lbl = Gtk.Label(xalign=0.0)
        self.query_title_lbl.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.query_title_lbl.set_markup("<b>📖 StarDict &amp; 🧮 計算機結果</b>")
        hdr_box.pack_start(self.query_title_lbl, True, True, 0)

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

    def show_dict_results(self, query_word, results, near_pointer=False, spelling_info=None):
        if spelling_info:
            orig_w, sugs, auto_w = spelling_info
            if auto_w:
                self.query_title_lbl.set_markup(
                    f"<b>📖 拼字修正：{html.escape(orig_w)} → {html.escape(auto_w)}</b>"
                )
            else:
                self.query_title_lbl.set_markup(
                    f"<b>❓ 查無「{html.escape(orig_w)}」— 拼字建議</b>"
                )
        else:
            self.query_title_lbl.set_markup(f"<b>📖 查詢：{html.escape(query_word)}</b>")
        self.calc_frame.hide()
        self._last_calc_result = None

        buf = self.result_buf
        buf.set_text("")

        if not results:
            if spelling_info:
                orig_w, sugs, _aw = spelling_info
                top_sug = sugs[0] if sugs else ""
                buf.insert_with_tags(
                    buf.get_end_iter(),
                    f"查無單字「{orig_w}」。\n\n",
                    self.tag_muted,
                )
                buf.insert_with_tags(
                    buf.get_end_iter(),
                    f"💡 您是不是要找以下拼法？（輸入框已自動填入「{top_sug}」，按 Enter 即可查詢）\n",
                    self.tag_word,
                )
                buf.insert_with_tags(
                    buf.get_end_iter(),
                    "  • " + "\n  • ".join(sugs) + "\n",
                    self.tag_body,
                )
                self._update_status_footer(f"查無「{orig_w}」：輸入框已填入「{top_sug}」，按 Enter 查詢")
            else:
                buf.insert_with_tags(
                    buf.get_end_iter(),
                    f"找不到「{query_word}」的詞條釋義。\n\n"
                    f"💡 建議：\n"
                    f"• 於面板輸入框按右鍵進入「⚙️ 偏好設定 → 詞典管理」勾選更多詞典\n"
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
        engine_label="Google 翻譯",
        err=None,
        is_fallback=False,
        near_pointer=False,
    ):
        short_q = original_text.replace("\n", " ")
        if len(short_q) > 26:
            short_q = short_q[:24] + "…"
        self.query_title_lbl.set_markup(f"<b>🌐 {html.escape(engine_label)}：{html.escape(short_q)}</b>")

        buf = self.result_buf
        buf.set_text("")

        if err is not None or not translated_text:
            self._last_calc_result = None
            self.calc_frame.hide()
            buf.insert_with_tags(
                buf.get_end_iter(),
                f"⚠️ {engine_label} 失敗：{err or '無回傳內容'}\n\n",
                self.tag_word,
            )
            buf.insert_with_tags(buf.get_end_iter(), f"原文：\n{original_text}\n", self.tag_muted)
            self._update_status_footer(f"🌐 {engine_label} 失敗")
            self.position_and_show(near_pointer=near_pointer)
            return

        src_lbl = GoogleTranslator.lang_label(src_lang)
        tgt_lbl = GoogleTranslator.lang_label(target_lang)
        fb_note = " (離線無結果自動切換)" if is_fallback else ""

        self._last_calc_result = translated_text
        self.calc_result_lbl.set_markup(
            f"🌐 <b>{html.escape(engine_label)}</b>  <span foreground='#38bdf8'>{html.escape(src_lbl)} → {html.escape(tgt_lbl)}</span>"
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
            f"🌐 {engine_label} ({src_lbl} → {tgt_lbl}) | 共 {len(original_text)} 字 | 點右上 📋 複製譯文"
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
        self.dict_mgr = StarDictManager(root_dir=self.config.get("dict_root_dir", "/usr/share/stardict/dic"))
        self.vocab_mgr = VocabHistoryManager()
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
        self._last_bubble_items = []
        self._last_result_was_bubble = False
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

        # 2. Clean Inline Panel Search/Calc Entry (click-to-type with request_focus + right-click menu)
        self.panel_entry = Gtk.Entry()
        self.panel_entry.set_no_show_all(True)
        # Override GTK3 default 20-char (~170px) minimum width so panel_entry_width shrinks/grows accurately!
        self.panel_entry.set_width_chars(1)
        self.panel_entry.set_max_width_chars(1)
        self.panel_entry.set_name("dict-calc-panel-entry")
        self.entry_css_provider = Gtk.CssProvider()
        self.panel_entry.get_style_context().add_provider(
            self.entry_css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        self.panel_entry.set_placeholder_text("查詞 / 算式...")
        self.panel_entry.connect("activate", self._on_panel_entry_activate)
        self.panel_entry.connect("button-press-event", self._on_panel_entry_button_press)
        self.panel_entry.connect("populate-popup", self._on_panel_entry_populate_popup)
        self.panel_entry.connect("key-press-event", self._on_panel_entry_key_press)
        self.root_box.pack_start(self.panel_entry, False, False, 0)

        self.connect("button-press-event", self._on_button_press)
        self.connect("destroy", self._on_destroy)

        # Clipboards: CLIPBOARD (Ctrl+V) and PRIMARY (Mouse Scan Selection)
        self._scan_poll_id = None
        self._scan_debounce_id = None
        self._scan_pending_retries = 0
        self._btn1_was_down = False
        self._last_mouse_select_ts = 0
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
        if cfg.get("result_output_mode") not in ("window", "dbus_popup"):
            cfg["result_output_mode"] = "window"
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
        now_us = GLib.get_monotonic_time()
        # Track mouse button 1 drag -> release so keyboard-only Ctrl combos (e.g. tmux Ctrl+b)
        # never misfire Scan Selection on stale PRIMARY text!
        if state & Gdk.ModifierType.BUTTON1_MASK:
            self._btn1_was_down = True
            return True
        if self._btn1_was_down:
            self._btn1_was_down = False
            self._last_mouse_select_ts = now_us
        # Only allow auto-scan within 1.5s of an actual mouse selection release / PRIMARY change
        if (now_us - self._last_mouse_select_ts) > 1_500_000:
            return True
        if not self._is_scan_modifier_pressed(state):
            return True
        # Reset timestamp immediately so we don't send repeated X11 PRIMARY requests during the 1.5s window
        self._last_mouse_select_ts = 0
        self.clipboard_primary.request_text(self._on_primary_text_received, False)
        return True

    def _on_primary_selection_changed(self, _clipboard, _event):
        if not self.config.get("scan_selection_enabled", False):
            return
        self._last_mouse_select_ts = GLib.get_monotonic_time()
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
        # Keep the inline panel entry completely clean (no inner toggle icons; toggle Scan Selection via right-click menu)
        self.panel_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, None)
        self.panel_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
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

        bd_hex = self.config.get("badge_border_color", "#48b8fa")
        bd_rgb = hex_to_rgb(bd_hex, default=(0.282, 0.722, 0.980))
        br255 = int(round(bd_rgb[0] * 255))
        bg255 = int(round(bd_rgb[1] * 255))
        bb255 = int(round(bd_rgb[2] * 255))
        border_col = f"rgba({br255}, {bg255}, {bb255}, 0.85)"
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

        entry_w = max(60, min(320, int(self.config.get("panel_entry_width", 170))))
        css_str = (
            f"#dict-calc-panel-entry {{\n"
            f"  {bg_css}\n"
            f"  color: {fg_hex};\n"
            f"  caret-color: {fg_hex};\n"
            f"  border-radius: 4px;\n"
            f"  padding-top: 1px;\n"
            f"  padding-bottom: 1px;\n"
            f"  padding-left: 5px;\n"
            f"  padding-right: 5px;\n"
            f"  min-height: 0px;\n"
            f"  min-width: {entry_w}px;\n"
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
        entry_w = max(60, min(320, int(self.config.get("panel_entry_width", 170))))
        self.panel_entry.set_width_chars(1)
        self.panel_entry.set_max_width_chars(1)
        self.panel_entry.set_size_request(entry_w, max(20, h - 4))
        self._apply_entry_css()
        self._sync_entry_visibility()
        self.panel_entry.queue_resize()
        self.queue_resize()
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

    def set_result_output_mode(self, mode):
        self.config["result_output_mode"] = "dbus_popup" if mode == "dbus_popup" else "window"
        self._save_config()
        self._update_tooltip()

    def toggle_result_output_mode(self, *_args):
        cur = self.config.get("result_output_mode", "window")
        self.set_result_output_mode("window" if cur == "dbus_popup" else "dbus_popup")

    def _on_panel_entry_populate_popup(self, _entry, menu):
        """Replace default Gtk.Entry context menu items (Cut/Copy/Paste/Delete/Emoji) with clean Applet controls."""
        for child in list(menu.get_children()):
            menu.remove(child)

        mi_last = Gtk.MenuItem(label="📌 顯示上次查詢結果 (不自動關閉)")
        mi_last.connect("activate", lambda *_: self.show_last_query_result())
        menu.append(mi_last)

        is_bubble = self.config.get("result_output_mode", "window") == "dbus_popup"
        mi_mode = Gtk.MenuItem(
            label="🪟 切換為無焦點視窗顯示 (目前：氣泡框)"
            if is_bubble
            else "💬 切換為氣泡框顯示 (目前：無焦點視窗)"
        )
        mi_mode.connect("activate", lambda *_: self.toggle_result_output_mode())
        menu.append(mi_mode)

        menu.append(Gtk.SeparatorMenuItem())

        mi_scan_now = Gtk.MenuItem(label="🔎 立即查詢反白文字 (Scan Now)")
        mi_scan_now.connect("activate", lambda *_: self.trigger_scan_selection_now())
        menu.append(mi_scan_now)

        scan_on = self.config.get("scan_selection_enabled", False)
        mi_scan_chk = Gtk.CheckMenuItem(label="✂ 啟用滑鼠劃詞查詢 (Scan Selection)")
        mi_scan_chk.set_active(scan_on)
        mi_scan_chk.connect("toggled", lambda w: self.set_scan_selection_enabled(w.get_active()))
        menu.append(mi_scan_chk)

        menu.append(Gtk.SeparatorMenuItem())

        mi_prefs = Gtk.MenuItem(label="⚙️ 偏好設定與詞典順位管理...")
        mi_prefs.connect("activate", lambda *_: self.show_preferences_dialog())
        menu.append(mi_prefs)

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

    def _notify_dbus_batch(self, items, timeout_sec=None, record_last=True):
        if record_last and items:
            self._last_bubble_items = list(items)
            self._last_result_was_bubble = True
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

    def _notify_dbus(self, summary, body, icon="accessories-dictionary", timeout_sec=None, record_last=True):
        return self._notify_dbus_batch([(summary, body, icon)], timeout_sec=timeout_sec, record_last=record_last)

    def show_last_query_result(self, *_args):
        """Re-display the most recent query result pinned (no auto-dismiss countdown)."""
        if self._last_result_was_bubble and self._last_bubble_items:
            # timeout_sec=0 pins the bubble indefinitely until clicked or Esc pressed
            self._notify_dbus_batch(self._last_bubble_items, timeout_sec=0, record_last=False)
        elif self.popup_win is not None:
            self.popup_win.position_and_show(near_pointer=False)
        elif self._last_bubble_items:
            self._notify_dbus_batch(self._last_bubble_items, timeout_sec=0, record_last=False)
        else:
            self._notify_dbus(
                "📌 尚無上次查詢紀錄",
                "請先查詢單字、句子或算式。",
                timeout_sec=3,
                record_last=False,
            )

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

            use_dbus = out_mode == "dbus_popup"
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

        # 2. Resolve Active Dictionary Profile (Offline StarDict + Virtual Online Translation Engines)
        use_dbus_dict = out_mode == "dbus_popup"
        gt_enabled = bool(self.config.get("google_translate_enabled", True))
        self._query_seq += 1
        seq = self._query_seq

        if source == "scan":
            custom_list = list(
                self.config.get(
                    "enabled_dicts_scan",
                    self.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS),
                )
            )
        elif use_dbus_dict:
            custom_list = list(
                self.config.get(
                    "enabled_dicts_dbus",
                    self.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS),
                )
            )
        else:
            custom_list = list(self.config.get("enabled_dicts", DEFAULT_PREFERRED_DICTS))

        sdcv_names = self.dict_mgr.get_ordered_enabled_dicts(
            [n for n in custom_list if n not in VIRTUAL_DICT_ENGINE_MAP]
        )
        virtual_tr_names = [n for n in custom_list if n in VIRTUAL_DICT_ENGINE_MAP]
        priority_map = {name: i for i, name in enumerate(custom_list)}

        # 2a. Sentence / Paragraph Routing: skip offline StarDict, run online translation engine(s)
        is_sentence = GoogleTranslator.is_sentence_or_paragraph(q)
        if is_sentence:
            if len(virtual_tr_names) == 1:
                self._start_google_translate(
                    seq,
                    q,
                    near_pointer=near_pointer,
                    use_dbus_dict=use_dbus_dict,
                    keep_panel_focus=keep_panel_focus,
                    is_fallback=False,
                    engine_override=VIRTUAL_DICT_ENGINE_MAP[virtual_tr_names[0]],
                )
                return
            elif len(virtual_tr_names) > 1:
                # Multiple online translation engines enabled for this profile: query in parallel
                sdcv_names = []
            elif gt_enabled:
                self._start_google_translate(
                    seq,
                    q,
                    near_pointer=near_pointer,
                    use_dbus_dict=use_dbus_dict,
                    keep_panel_focus=keep_panel_focus,
                    is_fallback=False,
                )
                return

        # 3. Parallel Offline StarDict + Virtual Translation Engines with Progressive Offline-First Rendering
        if not use_dbus_dict:
            win = self._ensure_popup_window()
            win.show_loading(q, near_pointer=near_pointer, is_translate=(not sdcv_names and bool(virtual_tr_names)))
            if keep_panel_focus:
                GLib.idle_add(self._refocus_panel_entry)

        state = {
            "results": [],
            "pending": (1 if sdcv_names else 0) + len(virtual_tr_names),
            "has_virtual": bool(virtual_tr_names),
            "spelling_info": None,
        }

        def merge_and_render(new_items, spelling_info=None):
            if seq != self._query_seq:
                return False
            if spelling_info is not None:
                state["spelling_info"] = spelling_info
            if new_items:
                state["results"].extend(new_items)
                state["results"].sort(key=lambda r: priority_map.get(r["dict"], 999999))
            state["pending"] -= 1
            is_final = state["pending"] <= 0
            # Progressive display: show offline results immediately if non-empty; wait for final if still empty
            if state["results"] or state["spelling_info"] or is_final:
                self._on_query_dict_done(
                    seq,
                    q,
                    list(state["results"]),
                    near_pointer,
                    use_dbus_dict,
                    keep_panel_focus,
                    allow_fallback=(is_final and not state["has_virtual"] and state["spelling_info"] is None),
                    spelling_info=state["spelling_info"],
                )
            return False

        if state["pending"] == 0:
            GLib.idle_add(
                self._on_query_dict_done,
                seq,
                q,
                [],
                near_pointer,
                use_dbus_dict,
                keep_panel_focus,
                True,
                None,
            )
            return

        def launch_virtual_translators():
            if not virtual_tr_names:
                return
            primary_tl = self.config.get("google_translate_target", "zh-TW")
            reverse_tl = self.config.get("google_translate_reverse", "en")
            cfg_snapshot = dict(self.config)

            def on_gemini_migrated(new_model_id):
                def _save_new_model():
                    self.config["gemini_model"] = new_model_id
                    self._save_config()
                    return False
                GLib.idle_add(_save_new_model)

            def _kickoff_virtual():
                if seq != self._query_seq:
                    return False
                for v_name in virtual_tr_names:
                    v_eng = VIRTUAL_DICT_ENGINE_MAP[v_name]

                    def v_worker(bookname=v_name, eng=v_eng):
                        tr_txt, src_l, tgt_l, _eng_lbl, err_m = self.translator.translate_sync(
                            q,
                            primary_tl=primary_tl,
                            reverse_tl=reverse_tl,
                            config=cfg_snapshot,
                            on_model_migrated=on_gemini_migrated,
                            engine_override=eng,
                            concise_word=(not is_sentence),
                        )
                        items_out = []
                        if err_m is None and tr_txt:
                            src_lbl = GoogleTranslator.lang_label(src_l)
                            tgt_lbl = GoogleTranslator.lang_label(tgt_l)
                            items_out.append(
                                {
                                    "dict": bookname,
                                    "word": f"{q[:36] + '…' if len(q) > 36 else q} ({src_lbl} → {tgt_lbl})",
                                    "definition": tr_txt,
                                }
                            )
                        elif err_m:
                            items_out.append(
                                {
                                    "dict": bookname,
                                    "word": q[:36] + "…" if len(q) > 36 else q,
                                    "definition": f"⚠️ {err_m}",
                                }
                            )
                        GLib.idle_add(merge_and_render, items_out)

                    threading.Thread(target=v_worker, daemon=True).start()
                return False

            # 250ms debounce avoids wasted API calls on rapid consecutive scans/queries
            GLib.timeout_add(250, _kickoff_virtual)

        # For a single English word, run fast (~15-30ms) offline sdcv + aspell check BEFORE
        # launching online translation engines (Gemini/OpenAI/Google/DeepL) so misspelled words
        # show ONLY aspell suggestions and never get overwritten by translation engines!
        if not is_sentence and StarDictManager.is_single_english_word(q):
            def gated_english_worker():
                res = self.dict_mgr.query_sync(q, dict_names=sdcv_names) if sdcv_names else []
                if res and self.config.get("vocab_record_enabled", True):
                    self.vocab_mgr.record_word(q)
                sp_info = None
                if not res:
                    sugs = StarDictManager.suggest_spelling_sync(q, max_suggestions=8)
                    if sugs:
                        sp_info = (q, sugs, None)

                def _after_check():
                    if seq != self._query_seq:
                        return False
                    if sp_info is not None:
                        # Misspelled single English word: suppress all online translation engines!
                        state["pending"] = 1
                        merge_and_render([], spelling_info=sp_info)
                        return False
                    # Valid word (or proper noun not flagged by aspell): show offline hits & start translators
                    if sdcv_names:
                        merge_and_render(res, spelling_info=None)
                    launch_virtual_translators()
                    return False

                GLib.idle_add(_after_check)

            threading.Thread(target=gated_english_worker, daemon=True).start()
            return

        if sdcv_names:
            def sdcv_worker():
                res = self.dict_mgr.query_sync(q, dict_names=sdcv_names)
                if res and self.config.get("vocab_record_enabled", True):
                    self.vocab_mgr.record_word(q)
                GLib.idle_add(merge_and_render, res, None)

            threading.Thread(target=sdcv_worker, daemon=True).start()

        launch_virtual_translators()

    def _start_google_translate(
        self,
        seq,
        text,
        near_pointer,
        use_dbus_dict,
        keep_panel_focus,
        is_fallback=False,
        engine_override=None,
    ):
        """Run selected translation engine asynchronously; follows user's result_output_mode."""
        use_dbus_tr = use_dbus_dict

        if not use_dbus_tr:
            win = self._ensure_popup_window()
            win.show_loading(text, near_pointer=near_pointer, is_translate=True)
            if keep_panel_focus:
                GLib.idle_add(self._refocus_panel_entry)

        primary_tl = self.config.get("google_translate_target", "zh-TW")
        reverse_tl = self.config.get("google_translate_reverse", "en")
        cfg_snapshot = dict(self.config)

        def on_gemini_migrated(new_model_id):
            def _save_new_model():
                self.config["gemini_model"] = new_model_id
                self._save_config()
                return False
            GLib.idle_add(_save_new_model)

        def tr_worker():
            translated, src_lang, tgt_lang, eng_lbl, err = self.translator.translate_sync(
                text,
                primary_tl=primary_tl,
                reverse_tl=reverse_tl,
                config=cfg_snapshot,
                on_model_migrated=on_gemini_migrated,
                engine_override=engine_override,
            )
            GLib.idle_add(
                self._on_translate_done,
                seq,
                text,
                translated,
                src_lang,
                tgt_lang,
                eng_lbl,
                err,
                near_pointer,
                use_dbus_tr,
                keep_panel_focus,
                is_fallback,
            )

        def _kickoff_translate():
            if seq != self._query_seq:
                return False
            threading.Thread(target=tr_worker, daemon=True).start()
            return False

        # 250ms debounce prevents wasted online translation API calls on rapid consecutive triggers
        GLib.timeout_add(250, _kickoff_translate)

    def _on_translate_done(
        self,
        seq,
        original_text,
        translated_text,
        src_lang,
        target_lang,
        engine_label,
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
                    f"🌐 {engine_label} 失敗",
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
                    f"{html.escape(tr_disp)}\n"
                    f"<span alpha='45%'>────────────────────</span>\n"
                    f"<small><span alpha='72%'><i>{html.escape(short_orig)}</i></span></small>"
                )
                self._notify_dbus(
                    f"🌐 {engine_label} ({src_lbl} → {tgt_lbl}{fb_tag})",
                    body,
                    icon="accessories-dictionary",
                    timeout_sec=timeout_sec,
                )
        else:
            self._last_result_was_bubble = False
            win = self._ensure_popup_window()
            win.show_translate_result(
                original_text,
                translated_text,
                src_lang,
                target_lang,
                engine_label=engine_label,
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

    def _on_query_dict_done(
        self,
        seq,
        query_word,
        results,
        near_pointer,
        use_dbus_dict,
        keep_panel_focus,
        allow_fallback=True,
        spelling_info=None,
    ):
        if seq != self._query_seq:
            return False

        # When aspell returns spelling suggestions, auto-fill the #1 suggestion into the panel entry
        # and select it so pressing Enter queries it immediately (or typing replaces it).
        if spelling_info:
            _orig_w, _sugs, _auto_w = spelling_info
            if _sugs:
                self.panel_entry.set_text(_sugs[0])
                self.panel_entry.select_region(0, -1)

        # Auto-fallback to Google Translate when offline StarDict returns 0 hits
        if (
            allow_fallback
            and not results
            and spelling_info is None
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

            sp_banner = ""
            title_word = query_word
            if spelling_info:
                orig_w, sugs, auto_w = spelling_info
                if auto_w:
                    title_word = f"{orig_w} → {auto_w}"
                    others = [s for s in sugs[1:5]]
                    other_str = f"（其他：{' · '.join(others)}）" if others else ""
                    sp_banner = f"<i>💡 拼字建議：{html.escape(orig_w)} → <b>{html.escape(auto_w)}</b>{html.escape(other_str)}</i>"

            if not results:
                if spelling_info:
                    orig_w, sugs, _aw = spelling_info
                    top_sug = sugs[0] if sugs else ""
                    self._notify_dbus(
                        f"❓ 查無「{orig_w}」— 可能拼法",
                        self._wrap_dbus_font(
                            f"您是不是要找：\n{html.escape('  ·  '.join(sugs))}\n"
                            f"<span alpha='45%'>────────────────────</span>\n"
                            f"<small><span alpha='75%'>輸入框已自動填入「{html.escape(top_sug)}」，按 Enter 即可查詢</span></small>"
                        ),
                        icon="accessories-dictionary",
                        timeout_sec=timeout_sec,
                    )
                else:
                    self._notify_dbus(
                        f"📖 查無詞條：{query_word}",
                        self._wrap_dbus_font("請檢查拼字，或在偏好設定的「詞典管理」勾選更多詞典。"),
                        icon="accessories-dictionary",
                        timeout_sec=timeout_sec,
                    )
            elif dbus_style == "separate":
                # Mode 1: Each dictionary gets its own crisp independent bubble
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
                    esc_body = html.escape(raw_body)
                    if idx == 0 and sp_banner:
                        esc_body = f"{sp_banner}\n<span alpha='45%'>────────────────────</span>\n{esc_body}"
                    body = self._wrap_dbus_font(esc_body)
                    batch.append((summary, body, "accessories-dictionary"))
                self._notify_dbus_batch(batch, timeout_sec=timeout_sec)
            else:
                # Mode 2: Single merged bubble with clear bold divider banners and indented definitions
                blocks = []
                if sp_banner:
                    blocks.append(sp_banner)
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
                    f"📖 {title_word}",
                    body,
                    icon="accessories-dictionary",
                    timeout_sec=timeout_sec,
                )
        else:
            self._last_result_was_bubble = False
            win = self._ensure_popup_window()
            win.show_dict_results(query_word, results, near_pointer=near_pointer, spelling_info=spelling_info)

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

        bd_hex = self.config.get("badge_border_color", "#48b8fa")
        default_bd = (0.20, 0.84, 0.62) if scan_on else (0.28, 0.72, 0.98)
        border_rgb = hex_to_rgb(bd_hex, default=default_bd)
        text_rgb = (0.94, 1.00, 0.98) if scan_on else (0.94, 0.98, 1.00)

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

        out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        out_box.set_margin_start(14)
        out_box.pack_start(Gtk.Label(label="查詢/計算結果顯示方式：", xalign=0.0), False, False, 0)
        combo_out = Gtk.ComboBoxText()
        for o_id, o_lbl in (
            ("window", "無焦點精簡結果視窗 (可捲動看完整詞典，不搶面板游標)"),
            ("dbus_popup", "氣泡框彈出模式 (可於「氣泡框設定」自訂位置、底色、字色與透明度)"),
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
                lower=60.0,
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

        bg_box.pack_start(Gtk.Label(label="外框：", xalign=0.0), False, False, 0)
        bd_col_btn = Gtk.ColorButton()
        bd_rgba0 = Gdk.RGBA()
        if not bd_rgba0.parse(self.config.get("badge_border_color", "#48b8fa")):
            bd_rgba0.parse("#48b8fa")
        bd_col_btn.set_rgba(bd_rgba0)
        bd_col_btn.set_tooltip_text("點擊自訂圖示方塊與輸入框的外框顏色")
        bg_box.pack_start(bd_col_btn, False, False, 0)

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

        # ---------------- Tab 3: Multi-Engine Translation Settings ----------------
        tab_tr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_tr.set_margin_top(12)
        tab_tr.set_margin_bottom(12)
        tab_tr.set_margin_start(14)
        tab_tr.set_margin_end(14)

        tr_s1 = Gtk.Label(xalign=0.0)
        tr_s1.set_markup("<b>1. 智慧分流與雙向翻譯設定</b>")
        tab_tr.pack_start(tr_s1, False, False, 0)

        chk_gt_en = Gtk.CheckButton(
            label="🌐 句子 / 段落自動翻譯 (雙向翻譯，依「顯示方式」以氣泡框或視窗呈現)"
        )
        chk_gt_en.set_active(self.config.get("google_translate_enabled", True))
        tab_tr.pack_start(chk_gt_en, False, False, 0)

        chk_gt_fb = Gtk.CheckButton(
            label="離線詞典查無單字 / 短片語時，自動 Fallback 至翻譯引擎"
        )
        chk_gt_fb.set_margin_start(14)
        chk_gt_fb.set_active(self.config.get("google_translate_fallback", True))
        tab_tr.pack_start(chk_gt_fb, False, False, 0)

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
        tab_tr.pack_start(gt_lang_box, False, False, 0)

        tab_tr.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        tr_s2 = Gtk.Label(xalign=0.0)
        tr_s2.set_markup("<b>2. 翻譯引擎選擇與自動備援</b>")
        tab_tr.pack_start(tr_s2, False, False, 0)

        eng_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        eng_box.set_margin_start(12)
        eng_box.pack_start(Gtk.Label(label="主力翻譯引擎：", xalign=0.0), False, False, 0)
        combo_tr_engine = Gtk.ComboBoxText()
        for e_id, e_lbl in (
            ("google", "Google 翻譯 (免 Key，預設)"),
            ("gemini", "Gemini API Free (最通順、免綁卡，Google AI Studio)"),
            ("openai_compat", "OpenAI 相容端點 (Groq 極速免費 / OpenRouter / 本機 Ollama)"),
            ("deepl", "DeepL API Free / Pro (每月 50 萬字)"),
        ):
            combo_tr_engine.append(e_id, e_lbl)
        combo_tr_engine.set_active_id(self.config.get("translate_engine", "google"))
        eng_box.pack_start(combo_tr_engine, True, True, 0)
        tab_tr.pack_start(eng_box, False, False, 0)

        chk_tr_fb_google = Gtk.CheckButton(
            label="所選引擎超額、未填 Key 或連線逾時時，自動降級備援回 Google 翻譯"
        )
        chk_tr_fb_google.set_margin_start(12)
        chk_tr_fb_google.set_active(self.config.get("translate_fallback_google", True))
        tab_tr.pack_start(chk_tr_fb_google, False, False, 0)

        # Engine credential frames
        chk_show_keys = Gtk.CheckButton(label="顯示 API Key 明文")
        chk_show_keys.set_margin_start(12)
        tab_tr.pack_start(chk_show_keys, False, False, 0)

        # Gemini Frame
        gem_frame = Gtk.Frame(label=" ✨ Gemini API (Google AI Studio 免費 Key) ")
        gem_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        gem_grid.set_margin_top(6)
        gem_grid.set_margin_bottom(6)
        gem_grid.set_margin_start(10)
        gem_grid.set_margin_end(10)
        entry_gem_key = Gtk.Entry()
        entry_gem_key.set_visibility(False)
        entry_gem_key.set_hexpand(True)
        entry_gem_key.set_placeholder_text("AIza...")
        entry_gem_key.set_text(self.config.get("gemini_api_key", ""))
        combo_gem_model = Gtk.ComboBoxText.new_with_entry()
        combo_gem_model.set_hexpand(True)
        entry_gem_model = combo_gem_model.get_child()
        cur_gem_m = self.config.get("gemini_model", "gemini-3.1-flash-lite")
        for m_opt in (cur_gem_m, "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-lite-latest"):
            if m_opt:
                combo_gem_model.append_text(m_opt)
        entry_gem_model.set_text(cur_gem_m)
        btn_fetch_gem = Gtk.Button(label="🔄 載入模型")
        btn_fetch_gem.set_tooltip_text("向 Gemini API 即時查詢您金鑰目前可用的文字模型清單")
        gem_m_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        gem_m_box.set_hexpand(True)
        gem_m_box.pack_start(combo_gem_model, True, True, 0)
        gem_m_box.pack_start(btn_fetch_gem, False, False, 0)
        gem_grid.attach(Gtk.Label(label="API Key：", xalign=0.0), 0, 0, 1, 1)
        gem_grid.attach(entry_gem_key, 1, 0, 1, 1)
        gem_grid.attach(Gtk.Label(label="模型：", xalign=0.0), 0, 1, 1, 1)
        gem_grid.attach(gem_m_box, 1, 1, 1, 1)
        gem_frame.add(gem_grid)
        tab_tr.pack_start(gem_frame, False, False, 0)

        # OpenAI-Compatible Frame (Groq / OpenRouter / Local Ollama)
        oai_frame = Gtk.Frame(label=" ⚡ OpenAI 相容端點 (Groq / OpenRouter / 本機 Ollama) ")
        oai_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        oai_vbox.set_margin_top(6)
        oai_vbox.set_margin_bottom(6)
        oai_vbox.set_margin_start(10)
        oai_vbox.set_margin_end(10)

        oai_preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        oai_preset_box.pack_start(Gtk.Label(label="快速套用：", xalign=0.0), False, False, 0)
        btn_oai_groq = Gtk.Button(label="Groq 免費極速")
        btn_oai_ollama = Gtk.Button(label="本機 Ollama (免 Key)")
        btn_oai_or = Gtk.Button(label="OpenRouter")
        oai_preset_box.pack_start(btn_oai_groq, False, False, 0)
        oai_preset_box.pack_start(btn_oai_ollama, False, False, 0)
        oai_preset_box.pack_start(btn_oai_or, False, False, 0)
        oai_vbox.pack_start(oai_preset_box, False, False, 0)

        oai_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        entry_oai_url = Gtk.Entry()
        entry_oai_url.set_hexpand(True)
        entry_oai_url.set_text(self.config.get("openai_base_url", "https://api.groq.com/openai/v1"))
        entry_oai_key = Gtk.Entry()
        entry_oai_key.set_visibility(False)
        entry_oai_key.set_hexpand(True)
        entry_oai_key.set_placeholder_text("gsk_... (本機 Ollama 可留空)")
        entry_oai_key.set_text(self.config.get("openai_api_key", ""))
        combo_oai_model = Gtk.ComboBoxText.new_with_entry()
        combo_oai_model.set_hexpand(True)
        entry_oai_model = combo_oai_model.get_child()
        cur_oai_m = self.config.get("openai_model", "llama-3.3-70b-versatile")
        for m_opt in (cur_oai_m, "llama-3.3-70b-versatile", "qwen/qwen3-32b", "qwen2.5:7b"):
            if m_opt:
                combo_oai_model.append_text(m_opt)
        entry_oai_model.set_text(cur_oai_m)
        btn_fetch_oai = Gtk.Button(label="🔄 載入模型")
        btn_fetch_oai.set_tooltip_text("向此 OpenAI 相容端點即時查詢可用模型清單")
        oai_m_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        oai_m_box.set_hexpand(True)
        oai_m_box.pack_start(combo_oai_model, True, True, 0)
        oai_m_box.pack_start(btn_fetch_oai, False, False, 0)
        oai_grid.attach(Gtk.Label(label="Base URL：", xalign=0.0), 0, 0, 1, 1)
        oai_grid.attach(entry_oai_url, 1, 0, 1, 1)
        oai_grid.attach(Gtk.Label(label="API Key：", xalign=0.0), 0, 1, 1, 1)
        oai_grid.attach(entry_oai_key, 1, 1, 1, 1)
        oai_grid.attach(Gtk.Label(label="模型：", xalign=0.0), 0, 2, 1, 1)
        oai_grid.attach(oai_m_box, 1, 2, 1, 1)
        oai_vbox.pack_start(oai_grid, False, False, 0)
        oai_frame.add(oai_vbox)
        tab_tr.pack_start(oai_frame, False, False, 0)

        # DeepL Frame
        dl_frame = Gtk.Frame(label=" 📘 DeepL API (Free 結尾為 :fx，自動辨識端點) ")
        dl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dl_box.set_margin_top(6)
        dl_box.set_margin_bottom(6)
        dl_box.set_margin_start(10)
        dl_box.set_margin_end(10)
        dl_box.pack_start(Gtk.Label(label="Auth Key：", xalign=0.0), False, False, 0)
        entry_dl_key = Gtk.Entry()
        entry_dl_key.set_visibility(False)
        entry_dl_key.set_hexpand(True)
        entry_dl_key.set_placeholder_text("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx")
        entry_dl_key.set_text(self.config.get("deepl_api_key", ""))
        dl_box.pack_start(entry_dl_key, True, True, 0)
        dl_frame.add(dl_box)
        tab_tr.pack_start(dl_frame, False, False, 0)

        # Live Translation Test Row
        tr_test_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        entry_tr_test = Gtk.Entry()
        entry_tr_test.set_hexpand(True)
        entry_tr_test.set_text("Agreed, their 2.0 era releases in general are just that good.")
        btn_tr_test = Gtk.Button(label="🔔 立即測試翻譯")
        tr_test_box.pack_start(entry_tr_test, True, True, 0)
        tr_test_box.pack_start(btn_tr_test, False, False, 0)
        tab_tr.pack_start(tr_test_box, False, False, 0)

        lbl_tr_test_out = Gtk.Label(xalign=0.0)
        lbl_tr_test_out.set_line_wrap(True)
        lbl_tr_test_out.set_markup("<small><span foreground='#94a3b8'>輸入金鑰後點「立即測試翻譯」即可驗證目前引擎與譯文流暢度。</span></small>")
        tab_tr.pack_start(lbl_tr_test_out, False, False, 0)

        tab_tr_scroll = Gtk.ScrolledWindow()
        tab_tr_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        tab_tr_scroll.add(tab_tr)
        notebook.append_page(tab_tr_scroll, Gtk.Label(label="🌐 翻譯引擎"))

        # ---------------- Tab 4: StarDict Dictionaries Manager (3 Independent Profiles + Root Dir) ----------------
        tab2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tab2.set_margin_top(10)
        tab2.set_margin_bottom(10)
        tab2.set_margin_start(12)
        tab2.set_margin_end(12)

        # Dict Root Directory Selector Bar
        dir_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dir_box.pack_start(Gtk.Label(label="📁 Dict 根目錄：", xalign=0.0), False, False, 0)
        entry_dict_dir = Gtk.Entry()
        entry_dict_dir.set_hexpand(True)
        entry_dict_dir.set_placeholder_text("/usr/share/stardict/dic（留空則掃描系統預設路徑）")
        entry_dict_dir.set_text(self.config.get("dict_root_dir", "/usr/share/stardict/dic"))
        btn_browse_dir = Gtk.Button(label="📂 瀏覽...")
        btn_rescan_dir = Gtk.Button(label="🔄 重新掃描")
        dir_box.pack_start(entry_dict_dir, True, True, 0)
        dir_box.pack_start(btn_browse_dir, False, False, 0)
        dir_box.pack_start(btn_rescan_dir, False, False, 0)
        tab2.pack_start(dir_box, False, False, 0)

        d_hdr = Gtk.Label(xalign=0.0)
        d_tab_lbl = Gtk.Label(label=f"📚 詞典管理 ({len(self.dict_mgr.all_dicts)} 套)")

        def update_dict_hdr():
            n_all = len(self.dict_mgr.all_dicts)
            n_tr = len(VIRTUAL_TRANSLATE_DICTS)
            d_hdr.set_markup(
                f"<b>已掃描到 {n_all} 套離線詞典 + {n_tr} 套線上翻譯引擎（三種情境獨立管理）</b>\n"
                f"<small>可自由混搭離線詞典與線上翻譯引擎並調整順位；離線詞典先即時顯示，線上翻譯並行回填</small>"
            )
            d_tab_lbl.set_text(f"📚 詞典管理 ({n_all}+{n_tr})")

        update_dict_hdr()
        tab2.pack_start(d_hdr, False, False, 0)

        grp_labels = {
            "online_tr": "線上翻譯",
            "en_zh": "英漢/漢英",
            "ja_zh": "日漢/漢日",
            "sci_en": "科技/英英",
        }
        dict_notebook = Gtk.Notebook()
        profile_refreshers = []

        def build_dict_profile_page(config_key, profile_desc, default_preset, allow_copy_win=False):
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            page.set_margin_top(8)
            page.set_margin_bottom(6)
            page.set_margin_start(8)
            page.set_margin_end(8)

            info_lbl = Gtk.Label(xalign=0.0)
            page.pack_start(info_lbl, False, False, 0)

            store = Gtk.ListStore(bool, str, str, str)
            rebuilding = [False]

            def populate_store_from_list(enabled_list):
                all_entries = list(VIRTUAL_TRANSLATE_DICTS) + list(self.dict_mgr.all_dicts)
                dict_by_name = {d["bookname"]: d for d in all_entries}
                store.clear()
                added = set()
                for name in enabled_list:
                    if name in dict_by_name and name not in added:
                        d = dict_by_name[name]
                        cnt_str = "線上" if d["group"] == "online_tr" else str(d["wordcount"])
                        store.append([True, d["bookname"], grp_labels.get(d["group"], d["group"]), cnt_str])
                        added.add(name)
                for d in all_entries:
                    if d["bookname"] not in added:
                        cnt_str = "線上" if d["group"] == "online_tr" else str(d["wordcount"])
                        store.append([False, d["bookname"], grp_labels.get(d["group"], d["group"]), cnt_str])
                        added.add(d["bookname"])

            def update_count_label():
                n_en = sum(1 for row in store if row[0])
                info_lbl.set_markup(
                    f"<small>{html.escape(profile_desc)} — 目前已啟用 <b>{n_en}</b> 套詞典</small>"
                )

            def refresh_from_mgr():
                rebuilding[0] = True
                try:
                    populate_store_from_list(list(self.config.get(config_key, default_preset)))
                    update_count_label()
                finally:
                    rebuilding[0] = False

            profile_refreshers.append(refresh_from_mgr)
            refresh_from_mgr()

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

        def on_rescan_dict_dir(*_):
            new_dir = entry_dict_dir.get_text().strip()
            self.config["dict_root_dir"] = new_dir
            self._save_config()
            self.dict_mgr.reload(new_dir)
            update_dict_hdr()
            for fn_ref in profile_refreshers:
                fn_ref()
            self._update_tooltip()
            if self.popup_win is not None:
                self.popup_win.sync_controls()

        def on_browse_dict_dir(*_):
            dlg = Gtk.FileChooserDialog(
                title="選擇 StarDict 詞典根目錄",
                parent=win,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            dlg.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
            )
            cur_d = os.path.expanduser(entry_dict_dir.get_text().strip() or "/usr/share/stardict/dic")
            if os.path.isdir(cur_d):
                dlg.set_current_folder(cur_d)
            if dlg.run() == Gtk.ResponseType.OK:
                chosen = dlg.get_filename()
                if chosen:
                    entry_dict_dir.set_text(chosen)
                    on_rescan_dict_dir()
            dlg.destroy()

        btn_browse_dir.connect("clicked", on_browse_dict_dir)
        btn_rescan_dir.connect("clicked", on_rescan_dict_dir)
        entry_dict_dir.connect("activate", on_rescan_dict_dir)

        notebook.append_page(tab2, d_tab_lbl)

        # ---------------- Tab 5: Vocabulary Flashcards (History + Dict Selection + HTML/EPUB Export) ----------------
        tab_vocab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tab_vocab.set_margin_top(10)
        tab_vocab.set_margin_bottom(10)
        tab_vocab.set_margin_start(12)
        tab_vocab.set_margin_end(12)

        v_top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chk_vocab_rec = Gtk.CheckButton(label="自動紀錄傳統詞典查詢命中的單字")
        chk_vocab_rec.set_active(self.config.get("vocab_record_enabled", True))
        v_top_box.pack_start(chk_vocab_rec, False, False, 0)

        combo_v_filter = Gtk.ComboBoxText()
        for f_id, f_lbl in (
            ("all", "全部紀錄"),
            ("7d", "最近 7 天"),
            ("30d", "最近 30 天"),
            ("freq2", "查詢 ≥ 2 次"),
        ):
            combo_v_filter.append(f_id, f_lbl)
        combo_v_filter.set_active_id("all")

        combo_v_sort = Gtk.ComboBoxText()
        for s_id, s_lbl in (
            ("recent", "排序：最近查詢"),
            ("freq", "排序：查詢次數"),
            ("alpha", "排序：字母 A→Z"),
            ("random", "🎲 排序：隨機洗牌"),
        ):
            combo_v_sort.append(s_id, s_lbl)
        combo_v_sort.set_active_id(self.config.get("vocab_sort_mode", "recent"))

        btn_v_refresh = Gtk.Button(label="🔄")
        btn_v_refresh.set_tooltip_text("重新整理單字紀錄（若選擇隨機排序會重新洗牌）")
        v_top_box.pack_end(btn_v_refresh, False, False, 0)
        v_top_box.pack_end(combo_v_sort, False, False, 0)
        v_top_box.pack_end(combo_v_filter, False, False, 0)
        tab_vocab.pack_start(v_top_box, False, False, 0)

        # Split pane: Left = recorded vocabulary words, Right = traditional StarDict dicts for card generation
        v_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        v_paned.set_vexpand(True)

        # Left pane: Recorded words TreeView
        v_left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        lbl_v_words = Gtk.Label(xalign=0.0)
        v_left.pack_start(lbl_v_words, False, False, 0)

        v_word_store = Gtk.ListStore(bool, str, int, str)
        v_word_tree = Gtk.TreeView(model=v_word_store)
        v_rend_chk = Gtk.CellRendererToggle()

        def on_v_word_toggled(_cell, path_str):
            v_word_store[path_str][0] = not v_word_store[path_str][0]
            update_vocab_counts()

        v_rend_chk.connect("toggled", on_v_word_toggled)
        v_word_tree.append_column(Gtk.TreeViewColumn("選", v_rend_chk, active=0))
        col_vw = Gtk.TreeViewColumn("單字", Gtk.CellRendererText(), text=1)
        col_vw.set_expand(True)
        v_word_tree.append_column(col_vw)
        v_word_tree.append_column(Gtk.TreeViewColumn("次數", Gtk.CellRendererText(), text=2))
        v_word_tree.append_column(Gtk.TreeViewColumn("最後查詢", Gtk.CellRendererText(), text=3))

        v_w_scroll = Gtk.ScrolledWindow()
        v_w_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        v_w_scroll.set_vexpand(True)
        v_w_scroll.add(v_word_tree)
        v_left.pack_start(v_w_scroll, True, True, 0)

        v_w_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        btn_vw_all = Gtk.Button(label="全選")
        btn_vw_none = Gtk.Button(label="取消")
        btn_vw_del = Gtk.Button(label="刪除勾選")
        btn_vw_clr = Gtk.Button(label="清空全部")
        for b_vw in (btn_vw_all, btn_vw_none, btn_vw_del, btn_vw_clr):
            v_w_btns.pack_start(b_vw, False, False, 0)
        v_left.pack_start(v_w_btns, False, False, 0)

        # Right pane: Traditional StarDict dictionaries selector for flashcards
        v_right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        v_right.set_margin_start(8)
        lbl_v_dicts = Gtk.Label(xalign=0.0)
        v_right.pack_start(lbl_v_dicts, False, False, 0)

        v_dict_store = Gtk.ListStore(bool, str)
        v_dict_tree = Gtk.TreeView(model=v_dict_store)
        vd_rend_chk = Gtk.CellRendererToggle()

        def save_vocab_dicts():
            chosen = [row[1] for row in v_dict_store if row[0]]
            self.config["vocab_card_dicts"] = chosen
            self._save_config()
            lbl_v_dicts.set_markup(f"<b>單字卡來源詞典</b> <small>(已選 {len(chosen)} 套)</small>")

        def on_v_dict_toggled(_cell, path_str):
            v_dict_store[path_str][0] = not v_dict_store[path_str][0]
            save_vocab_dicts()

        vd_rend_chk.connect("toggled", on_v_dict_toggled)
        v_dict_tree.append_column(Gtk.TreeViewColumn("用", vd_rend_chk, active=0))
        col_vd = Gtk.TreeViewColumn("傳統 Dict 詞典", Gtk.CellRendererText(), text=1)
        col_vd.set_expand(True)
        v_dict_tree.append_column(col_vd)

        v_d_scroll = Gtk.ScrolledWindow()
        v_d_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        v_d_scroll.set_vexpand(True)
        v_d_scroll.add(v_dict_tree)
        v_right.pack_start(v_d_scroll, True, True, 0)

        def refresh_vocab_dict_list():
            v_dict_store.clear()
            saved_vd = list(self.config.get("vocab_card_dicts", DEFAULT_PREFERRED_DICTS[:3]))
            saved_set = set(saved_vd)
            all_names = [d["bookname"] for d in self.dict_mgr.all_dicts]
            ordered = [n for n in saved_vd if n in all_names] + [n for n in all_names if n not in saved_set]
            for name in ordered:
                v_dict_store.append([name in saved_set, name])
            n_sel = sum(1 for row in v_dict_store if row[0])
            lbl_v_dicts.set_markup(f"<b>單字卡來源詞典</b> <small>(已選 {n_sel} 套)</small>")

        refresh_vocab_dict_list()
        profile_refreshers.append(refresh_vocab_dict_list)

        v_paned.pack1(v_left, True, False)
        v_paned.pack2(v_right, True, False)
        v_paned.set_position(330)
        tab_vocab.pack_start(v_paned, True, True, 0)

        # Bottom Export Bar (EPUB layout + HTML / EPUB buttons + Progress)
        v_exp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        v_exp_box.pack_start(Gtk.Label(label="EPUB 排版：", xalign=0.0), False, False, 0)
        combo_epub_layout = Gtk.ComboBoxText()
        combo_epub_layout.append("per_word", "一字一頁 (預設，適合翻頁背單字)")
        combo_epub_layout.append("az_chapters", "依字母 A–Z 分章")
        combo_epub_layout.set_active_id(self.config.get("vocab_epub_layout", "per_word"))
        v_exp_box.pack_start(combo_epub_layout, True, True, 0)

        btn_exp_html = Gtk.Button(label="📄 匯出 HTML 單字卡")
        btn_exp_epub = Gtk.Button(label="📚 匯出 EPUB 電子書")
        v_exp_box.pack_start(btn_exp_html, False, False, 0)
        v_exp_box.pack_start(btn_exp_epub, False, False, 0)
        tab_vocab.pack_start(v_exp_box, False, False, 0)

        v_prog_bar = Gtk.ProgressBar()
        v_prog_bar.set_show_text(True)
        v_prog_bar.set_text("就緒（勾選單字與詞典後即可匯出）")
        tab_vocab.pack_start(v_prog_bar, False, False, 0)

        v_tab_lbl = Gtk.Label(label="🗂 單字卡")

        def update_vocab_counts():
            total = len(v_word_store)
            sel = sum(1 for row in v_word_store if row[0])
            lbl_v_words.set_markup(f"<b>已紀錄單字</b> <small>(共 {total} 字，已勾選 {sel} 字)</small>")
            v_tab_lbl.set_text(f"🗂 單字卡 ({total})")

        def refresh_vocab_words(*_):
            f_mode = combo_v_filter.get_active_id() or "all"
            s_mode = combo_v_sort.get_active_id() or "recent"
            self.config["vocab_sort_mode"] = s_mode
            self._save_config()
            entries = self.vocab_mgr.get_entries(filter_mode=f_mode, sort_mode=s_mode)
            v_word_store.clear()
            for it in entries:
                dt_short = str(it.get("last_seen", ""))[:10]
                v_word_store.append([True, it["word"], int(it.get("count", 1)), dt_short])
            update_vocab_counts()

        refresh_vocab_words()

        def set_all_v_words(val):
            for row in v_word_store:
                row[0] = val
            update_vocab_counts()

        def delete_selected_v_words(*_):
            to_del = [row[1] for row in v_word_store if row[0]]
            if not to_del:
                return
            self.vocab_mgr.delete_words(to_del)
            refresh_vocab_words()

        def clear_all_v_words(*_):
            self.vocab_mgr.clear_all()
            refresh_vocab_words()

        def start_vocab_export(fmt):
            selected_rows = [(row[1], row[2], row[3]) for row in v_word_store if row[0]]
            if not selected_rows:
                v_prog_bar.set_text("⚠️ 請先勾選至少一個要匯出的單字")
                return
            dict_names = [row[1] for row in v_dict_store if row[0]]
            if not dict_names:
                v_prog_bar.set_text("⚠️ 請先在右側勾選至少一套傳統詞典")
                return

            ext = ".html" if fmt == "html" else ".epub"
            today_str = datetime.datetime.now().strftime("%Y%m%d")
            dlg = Gtk.FileChooserDialog(
                title=f"儲存 {fmt.upper()} 單字卡",
                parent=win,
                action=Gtk.FileChooserAction.SAVE,
            )
            dlg.set_do_overwrite_confirmation(True)
            dlg.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
            )
            dlg.set_current_folder(os.path.expanduser("~"))
            dlg.set_current_name(f"stardict-flashcards-{today_str}{ext}")
            resp = dlg.run()
            out_path = dlg.get_filename() if resp == Gtk.ResponseType.OK else None
            dlg.destroy()
            if not out_path:
                return
            if not out_path.lower().endswith(ext):
                out_path += ext

            sort_mode = combo_v_sort.get_active_id() or "recent"
            epub_layout = combo_epub_layout.get_active_id() or "per_word"
            if sort_mode == "random":
                random.shuffle(selected_rows)

            btn_exp_html.set_sensitive(False)
            btn_exp_epub.set_sensitive(False)
            v_prog_bar.set_fraction(0.0)
            v_prog_bar.set_text(f"⏳ 正在查詢詞典並製作 {fmt.upper()} ({len(selected_rows)} 字)...")

            def _exp_worker():
                cards = []
                total = len(selected_rows)
                for idx, (w_str, cnt_val, last_s) in enumerate(selected_rows, start=1):
                    hits = self.dict_mgr.query_sync(w_str, dict_names=dict_names, exact=True)
                    cards.append(
                        {
                            "word": w_str,
                            "count": cnt_val,
                            "last_seen": last_s,
                            "entries": hits,
                        }
                    )
                    frac = idx / float(max(1, total))
                    GLib.idle_add(
                        lambda f=frac, i=idx, t=total: (
                            v_prog_bar.set_fraction(f),
                            v_prog_bar.set_text(f"⏳ 查詢詞條進度：{i} / {t}"),
                            False,
                        )[-1]
                    )
                err_msg = None
                try:
                    book_title = f"StarDict 單字卡 ({today_str})"
                    if fmt == "html":
                        VocabHistoryManager.export_html(out_path, cards, title=book_title)
                    else:
                        VocabHistoryManager.export_epub(out_path, cards, layout=epub_layout, title=book_title)
                except Exception as exc:
                    err_msg = str(exc)

                def _exp_done():
                    btn_exp_html.set_sensitive(True)
                    btn_exp_epub.set_sensitive(True)
                    if err_msg:
                        v_prog_bar.set_fraction(0.0)
                        v_prog_bar.set_text(f"❌ 匯出失敗：{err_msg}")
                    else:
                        v_prog_bar.set_fraction(1.0)
                        v_prog_bar.set_text(f"✅ 已匯出 {len(cards)} 字至：{os.path.basename(out_path)}")
                    return False

                GLib.idle_add(_exp_done)

            threading.Thread(target=_exp_worker, daemon=True).start()

        btn_v_refresh.connect("clicked", refresh_vocab_words)
        combo_v_filter.connect("changed", refresh_vocab_words)
        combo_v_sort.connect("changed", refresh_vocab_words)
        btn_vw_all.connect("clicked", lambda *_: set_all_v_words(True))
        btn_vw_none.connect("clicked", lambda *_: set_all_v_words(False))
        btn_vw_del.connect("clicked", delete_selected_v_words)
        btn_vw_clr.connect("clicked", clear_all_v_words)
        btn_exp_html.connect("clicked", lambda *_: start_vocab_export("html"))
        btn_exp_epub.connect("clicked", lambda *_: start_vocab_export("epub"))

        notebook.append_page(tab_vocab, v_tab_lbl)

        outer.pack_start(notebook, True, True, 0)

        # Live apply bindings for Tab 1
        def rgba_to_hex(rgba):
            r = max(0, min(255, int(round(rgba.red * 255))))
            g = max(0, min(255, int(round(rgba.green * 255))))
            b = max(0, min(255, int(round(rgba.blue * 255))))
            return f"#{r:02x}{g:02x}{b:02x}"

        def apply_tab1(*_):
            old_hk_open = self.config.get("hotkey_open_window", "")
            old_hk_scan = self.config.get("hotkey_scan_selection", "")
            self.config["hotkey_open_window"] = entry_hk_open.get_text().strip()
            self.config["hotkey_scan_selection"] = entry_hk_scan.get_text().strip()
            self.config["scan_selection_enabled"] = chk_scan_en.get_active()
            self.config["scan_modifier"] = combo_mod.get_active_id() or "ctrl"
            self.config["calc_auto_copy"] = chk_auto_copy.get_active()
            self.config["google_translate_enabled"] = chk_gt_en.get_active()
            self.config["google_translate_fallback"] = chk_gt_fb.get_active()
            self.config["google_translate_target"] = combo_gt_tl.get_active_id() or "zh-TW"
            self.config["google_translate_reverse"] = combo_gt_rev.get_active_id() or "en"
            self.config["translate_engine"] = combo_tr_engine.get_active_id() or "google"
            self.config["translate_fallback_google"] = chk_tr_fb_google.get_active()
            self.config["gemini_api_key"] = entry_gem_key.get_text().strip()
            self.config["gemini_model"] = entry_gem_model.get_text().strip() or "gemini-3.1-flash-lite"
            self.config["openai_base_url"] = entry_oai_url.get_text().strip() or "https://api.groq.com/openai/v1"
            self.config["openai_api_key"] = entry_oai_key.get_text().strip()
            self.config["openai_model"] = entry_oai_model.get_text().strip() or "llama-3.3-70b-versatile"
            self.config["deepl_api_key"] = entry_dl_key.get_text().strip()
            chk_gt_fb.set_sensitive(chk_gt_en.get_active())
            cur_eng = self.config["translate_engine"]
            gem_frame.set_sensitive(cur_eng == "gemini")
            oai_frame.set_sensitive(cur_eng == "openai_compat")
            dl_frame.set_sensitive(cur_eng == "deepl")
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
            self.config["badge_border_color"] = rgba_to_hex(bd_col_btn.get_rgba())
            bg_op = max(0, min(100, int(round(scale_bg_alpha.get_value()))))
            self.config["badge_bg_opacity"] = bg_op
            lbl_bg_alpha.set_text(f"{bg_op}%")
            self.config["text_shadow"] = chk_shadow.get_active()
            self.config["vocab_record_enabled"] = chk_vocab_rec.get_active()
            self.config["vocab_epub_layout"] = combo_epub_layout.get_active_id() or "per_word"
            self._save_config()
            self._sync_scan_poll_timer()
            if (
                self.config["hotkey_open_window"] != old_hk_open
                or self.config["hotkey_scan_selection"] != old_hk_scan
            ):
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

        def apply_oai_preset(url_str, model_str):
            combo_tr_engine.set_active_id("openai_compat")
            entry_oai_url.set_text(url_str)
            entry_oai_model.set_text(model_str)
            apply_tab1()

        def on_toggle_show_keys(chk):
            vis = chk.get_active()
            entry_gem_key.set_visibility(vis)
            entry_oai_key.set_visibility(vis)
            entry_dl_key.set_visibility(vis)

        def on_fetch_gemini_models(*_):
            apply_tab1()
            key_s = entry_gem_key.get_text().strip()
            btn_fetch_gem.set_sensitive(False)
            lbl_tr_test_out.set_markup("<small>⏳ 正在向 Gemini API 查詢可用模型清單...</small>")

            def _w():
                models, err_m = GoogleTranslator.list_gemini_models_sync(key_s)

                def _done():
                    btn_fetch_gem.set_sensitive(True)
                    if err_m or not models:
                        lbl_tr_test_out.set_markup(
                            f"<small><span foreground='#f87171'>❌ {html.escape(str(err_m or '查無可用模型'))}</span></small>"
                        )
                        return False
                    cur_txt = entry_gem_model.get_text().strip()
                    combo_gem_model.remove_all()
                    for m_id in models:
                        combo_gem_model.append_text(m_id)
                    if cur_txt in models:
                        entry_gem_model.set_text(cur_txt)
                    else:
                        entry_gem_model.set_text(models[0])
                        apply_tab1()
                    lbl_tr_test_out.set_markup(
                        f"<small><span foreground='#34d399'>✔ 已載入 {len(models)} 個可用 Gemini 模型</span>（推薦：{html.escape(models[0])}）</small>"
                    )
                    return False

                GLib.idle_add(_done)

            threading.Thread(target=_w, daemon=True).start()

        def on_fetch_oai_models(*_):
            apply_tab1()
            url_s = entry_oai_url.get_text().strip()
            key_s = entry_oai_key.get_text().strip()
            btn_fetch_oai.set_sensitive(False)
            lbl_tr_test_out.set_markup("<small>⏳ 正在查詢 OpenAI 相容端點可用模型清單...</small>")

            def _w():
                models, err_m = GoogleTranslator.list_openai_models_sync(url_s, key_s)

                def _done():
                    btn_fetch_oai.set_sensitive(True)
                    if err_m or not models:
                        lbl_tr_test_out.set_markup(
                            f"<small><span foreground='#f87171'>❌ {html.escape(str(err_m or '查無可用模型'))}</span></small>"
                        )
                        return False
                    cur_txt = entry_oai_model.get_text().strip()
                    combo_oai_model.remove_all()
                    for m_id in models:
                        combo_oai_model.append_text(m_id)
                    if cur_txt in models:
                        entry_oai_model.set_text(cur_txt)
                    else:
                        entry_oai_model.set_text(models[0])
                        apply_tab1()
                    lbl_tr_test_out.set_markup(
                        f"<small><span foreground='#34d399'>✔ 已載入 {len(models)} 個可用模型</span>（下拉選單即可切換）</small>"
                    )
                    return False

                GLib.idle_add(_done)

            threading.Thread(target=_w, daemon=True).start()

        def on_test_translate_clicked(*_):
            apply_tab1()
            sample = entry_tr_test.get_text().strip() or "Hello world, how are you today?"
            lbl_tr_test_out.set_markup("<small>⏳ 翻譯測試中...</small>")
            btn_tr_test.set_sensitive(False)
            cfg_snap = dict(self.config)
            p_tl = cfg_snap.get("google_translate_target", "zh-TW")
            r_tl = cfg_snap.get("google_translate_reverse", "en")

            def _on_mig(new_m):
                def _upd():
                    entry_gem_model.set_text(new_m)
                    self.config["gemini_model"] = new_m
                    self._save_config()
                    return False
                GLib.idle_add(_upd)

            def _t_worker():
                tr_txt, src_l, tgt_l, eng_l, err_m = self.translator.translate_sync(
                    sample, primary_tl=p_tl, reverse_tl=r_tl, config=cfg_snap, on_model_migrated=_on_mig
                )

                def _done():
                    btn_tr_test.set_sensitive(True)
                    if err_m or not tr_txt:
                        lbl_tr_test_out.set_markup(
                            f"<small><span foreground='#f87171'>❌ [{html.escape(eng_l)}] {html.escape(str(err_m or '失敗'))}</span></small>"
                        )
                    else:
                        lbl_tr_test_out.set_markup(
                            f"<small><span foreground='#34d399'>✔ [{html.escape(eng_l)}]</span> {html.escape(tr_txt)}</small>"
                        )
                    return False

                GLib.idle_add(_done)

            threading.Thread(target=_t_worker, daemon=True).start()

        btn_apply_hk.connect("clicked", apply_tab1)
        combo_mod.connect("changed", apply_tab1)
        combo_gt_tl.connect("changed", apply_tab1)
        combo_gt_rev.connect("changed", apply_tab1)
        combo_tr_engine.connect("changed", apply_tab1)
        combo_out.connect("changed", apply_tab1)
        combo_dbus_style.connect("changed", apply_tab1)
        combo_bubble_pos.connect("changed", apply_tab1)
        combo_epub_layout.connect("changed", apply_tab1)
        col_btn.connect("color-set", lambda *_: (rb_col_cus.set_active(True), apply_tab1()))
        bg_col_btn.connect("color-set", apply_tab1)
        bd_col_btn.connect("color-set", apply_tab1)
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
        btn_oai_groq.connect("clicked", lambda *_: apply_oai_preset("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"))
        btn_oai_ollama.connect("clicked", lambda *_: apply_oai_preset("http://127.0.0.1:11434/v1", "qwen2.5:7b"))
        btn_oai_or.connect("clicked", lambda *_: apply_oai_preset("https://openrouter.ai/api/v1", "google/gemma-3-27b-it:free"))
        chk_show_keys.connect("toggled", on_toggle_show_keys)
        btn_fetch_gem.connect("clicked", on_fetch_gemini_models)
        btn_fetch_oai.connect("clicked", on_fetch_oai_models)
        btn_tr_test.connect("clicked", on_test_translate_clicked)
        prefs_debounce_id = [None]

        def schedule_apply_tab1(*_):
            if prefs_debounce_id[0] is not None:
                try:
                    GLib.source_remove(prefs_debounce_id[0])
                except Exception:
                    pass
            def _run_debounced():
                prefs_debounce_id[0] = None
                apply_tab1()
                return False
            prefs_debounce_id[0] = GLib.timeout_add(300, _run_debounced)

        for ent_tr in (entry_gem_key, entry_gem_model, entry_oai_url, entry_oai_key, entry_oai_model, entry_dl_key):
            ent_tr.connect("changed", schedule_apply_tab1)
        for w_c in (
            chk_scan_en,
            chk_auto_copy,
            chk_gt_en,
            chk_gt_fb,
            chk_tr_fb_google,
            rb_badge,
            rb_entry,
            rb_col_dyn,
            rb_col_cus,
            chk_shadow,
            chk_vocab_rec,
        ):
            w_c.connect("toggled", apply_tab1)
        # Initialize frame sensitivity to match active engine
        cur_eng0 = self.config.get("translate_engine", "google")
        gem_frame.set_sensitive(cur_eng0 == "gemini")
        oai_frame.set_sensitive(cur_eng0 == "openai_compat")
        dl_frame.set_sensitive(cur_eng0 == "deepl")
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
                "ShowLastResultAction",
                Gtk.STOCK_REVERT_TO_SAVED,
                "顯示上次查詢結果 (不自動關閉) (_Last Result)",
                None,
                "重新顯示最近一次的查詢或翻譯結果，且不會自動倒數關閉",
                widget.show_last_query_result,
            ),
            (
                "ToggleOutputModeAction",
                Gtk.STOCK_REFRESH,
                "切換氣泡框 / 無焦點視窗顯示 (_Toggle Output Mode)",
                None,
                "在氣泡框彈出模式與無焦點結果視窗之間切換",
                widget.toggle_result_output_mode,
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
        '<menuitem name="ShowLastResult" action="ShowLastResultAction"/>'
        '<menuitem name="ToggleOutputMode" action="ToggleOutputModeAction"/>'
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
