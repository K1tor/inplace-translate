#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
悬浮窗实时翻译 —— InPlace Translate 的悬浮窗模式。

全自动检测要翻译的内容, 无需手动输入:
- 窗口监控: 勾选"窗口"并选择一个程序, 每秒用 Windows 自带 OCR 识别该窗口内的文字,
  有变化的部分就实时翻译(游戏/视频/图片等无法选中文字的场景也适用)
- 剪贴板监控: 复制任意文本自动翻译
- 声音翻译: 勾选"声音", 用 Windows 自带语音引擎识别默认录音设备并实时翻译
  (想翻译程序里播放的声音: 把系统默认录音设备设为"立体声混音")
- 输入框仅作备用: 打字/粘贴也会实时翻译(0.6 秒防抖)
- 默认使用 Bing 免费引擎, 与 translator.py 共用 config.json
- 标题栏可拖动; ▾ 收起成小条; Esc 或 ✕ 关闭
"""

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

from translator import (Http, BingEngine, GoogleEngine, DeeplEngine, OpenAIEngine,
                        load_config, resolve_proxy, split_chunks, LETTER_RE)

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

LANGS = [("简体中文", "zh-Hans"), ("繁體中文", "zh-Hant"), ("English", "en"),
         ("日本語", "ja"), ("한국어", "ko"), ("Français", "fr"), ("Deutsch", "de"),
         ("Español", "es"), ("Русский", "ru"), ("Português", "pt"), ("Italiano", "it"),
         ("Tiếng Việt", "vi"), ("ไทย", "th"), ("العربية", "ar")]

TARGET_ALIASES = {"zh": "zh-Hans", "zh-CN": "zh-Hans", "zh-TW": "zh-Hant",
                  "zh-HANS": "zh-Hans", "zh-HANT": "zh-Hant"}

PLACEHOLDER = "复制文字 / 选窗口 / 开声音, 自动翻译…"

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 9, "bold")

C_BG = "#23262e"
C_HEAD = "#2f3442"
C_SRC_BG = "#2a2e38"
C_DST_BG = "#20242c"
C_FG = "#e8eaf0"
C_DIM = "#aeb6c6"
C_RES = "#cfe0ff"
C_ERR = "#ff9c9c"
C_SEL = "#3a5f8a"


def build_engine(name, http, source, target, cfg):
    if name == "bing":
        return BingEngine(http, source, target)
    if name == "google":
        return GoogleEngine(http, source, target)
    if name == "deepl":
        return DeeplEngine(http, source, target,
                           os.environ.get("DEEPL_API_KEY") or cfg.get("deepl_api_key") or "")
    if name == "openai":
        return OpenAIEngine(http, source, target,
                            os.environ.get("OPENAI_BASE_URL") or cfg.get("openai_base"),
                            os.environ.get("OPENAI_API_KEY") or cfg.get("openai_key") or "",
                            os.environ.get("OPENAI_MODEL") or cfg.get("openai_model"))
    raise ValueError(name)


def bind_job(proc):
    """把子进程绑定到 Job 对象: 本进程退出时 OCR 子进程一并结束, 避免孤儿进程"""
    if os.name != "nt":
        return
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_ulonglong),
                    ("PerJobUserTimeLimit", ctypes.c_ulonglong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return
        k32.AssignProcessToJobObject(job, int(proc._handle))
        bind_job._handle = job                   # 持有句柄, 本进程结束时自动回收
    except Exception:
        pass


def process_name(pid):
    """进程名, 失败返回空"""
    if os.name != "nt" or not pid:
        return ""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def list_windows():
    """可见的顶层窗口, 按窗口 Z 序返回 [(hwnd, 标题, 进程名)]"""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi
    me = ctypes.windll.kernel32.GetCurrentProcessId()
    results = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cloaked = ctypes.c_int(0)
            dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
            if cloaked.value:
                return True
            ex = user32.GetWindowLongW(hwnd, -20)                     # GWL_EXSTYLE
            if ex & 0x80:                                             # WS_EX_TOOLWINDOW
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(max(length, 1) + 1)
            if length > 0:
                user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me:
                return True
            results.append((hwnd, buf.value.strip(), process_name(pid.value)))
        except Exception:
            pass
        return True

    cb_ref = proto(cb)
    user32.EnumWindows(cb_ref, 0)
    return [w for w in results if w[1] or w[2]]


class FloatingApp:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.chunk = int(cfg.get("chunk", 1000))
        self.engine_name = cfg.get("engine") or "bing"

        proxy = resolve_proxy(None, cfg)
        self.http = Http([proxy, ""] if proxy else [""], delay=float(cfg.get("delay", 0.3)))
        self.engine = None
        self.cache = {}
        self.last_sent = None
        self.req_t0 = 0.0
        self.last_clip = ""
        self.last_out = ""
        self.pending_after = None
        self.collapsed = False
        self.ocr_proc = None
        self.audio_procs = {"mic": None, "system": None}
        self.ocr_q = queue.Queue()
        self._last_ocr_compact = ""
        self.monitor_title = ""

        self.req_q = queue.Queue()
        self.res_q = queue.Queue()

        self.build_ui()

        target = (cfg.get("target") or "zh-Hans")
        target = TARGET_ALIASES.get(target.upper(), target)
        labels = dict((code, label) for label, code in LANGS)
        self.combo.set(labels.get(target, "简体中文"))
        self.set_language(target)

        threading.Thread(target=self.worker_loop, daemon=True).start()
        self.root.after(120, self.poll_results)
        self.root.after(900, self.poll_clipboard)
        self.root.after(300, self.poll_ocr)

    # ------------------------------------------------------------ 界面

    def build_ui(self):
        r = self.root
        r.title("悬浮翻译")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=C_BG)
        r.option_add("*TCombobox*Listbox*Font", FONT_SMALL)

        # 标题栏(可拖动)
        head = tk.Frame(r, bg=C_HEAD, padx=6, pady=3)
        head.pack(fill="x")
        title = tk.Label(head, text="⠿ 悬浮翻译", bg=C_HEAD, fg="#dfe4ee",
                         font=FONT_TITLE, cursor="fleur")
        title.pack(side="left")
        btn_opts = dict(bg=C_HEAD, fg=C_DIM, font=FONT_SMALL, cursor="hand2", padx=5)
        self.btn_fold = tk.Label(head, text="▾", **btn_opts)
        btn_close = tk.Label(head, text="✕", **btn_opts)
        self.btn_fold.pack(side="right")
        btn_close.pack(side="right")
        self.btn_fold.bind("<Button-1>", lambda e: self.toggle_collapse())
        btn_close.bind("<Button-1>", lambda e: self.on_close())
        for w in (head, title):
            w.bind("<Button-1>", self.start_move)
            w.bind("<B1-Motion>", self.do_move)

        # 中部: 原文 / 译文
        self.body = tk.Frame(r, bg=C_BG)
        self.src = tk.Text(self.body, height=4, width=38, wrap="word", font=FONT,
                           bg=C_SRC_BG, fg=C_DIM, insertbackground=C_FG,
                           relief="flat", padx=8, pady=6, selectbackground=C_SEL,
                           spacing1=1, spacing3=1)
        self.src.pack(fill="x", padx=4, pady=(4, 0))
        self.src.bind("<KeyRelease>", self.on_src_key)
        self.src.bind("<FocusIn>", self.clear_placeholder)
        self.src.bind("<FocusOut>", self.maybe_placeholder)

        self.dst_frame = tk.Frame(self.body, bg=C_BG)
        self.dst_frame.pack(fill="x", padx=4, pady=(2, 0))
        self.dst = tk.Text(self.dst_frame, height=7, width=38, wrap="word", font=FONT,
                           bg=C_DST_BG, fg=C_RES, relief="flat", padx=8, pady=6,
                           selectbackground=C_SEL, state="disabled",
                           spacing1=1, spacing3=1)
        scroll = ttk.Scrollbar(self.dst_frame, command=self.dst.yview)
        self.dst.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.dst.pack(side="left", fill="both", expand=True)

        # 底部两行: 上行 = 语言与检测开关, 下行 = 状态与复制
        self.footer = tk.Frame(r, bg=C_HEAD, padx=6, pady=4)
        self.footer.pack(side="bottom", fill="x")
        self.body.pack(fill="x")
        row1 = tk.Frame(self.footer, bg=C_HEAD)
        row1.pack(fill="x")
        row2 = tk.Frame(self.footer, bg=C_HEAD)
        row2.pack(fill="x")
        self.lang_var = tk.StringVar()
        self.combo = ttk.Combobox(row1, textvariable=self.lang_var,
                                  values=[l for l, _ in LANGS], state="readonly",
                                  width=9, font=FONT_SMALL)
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", self.on_lang_change)
        self.clip_on = tk.BooleanVar(value=True)
        tk.Checkbutton(row1, text="剪贴板", variable=self.clip_on,
                       bg=C_HEAD, fg=C_DIM, selectcolor=C_SRC_BG,
                       activebackground=C_HEAD, activeforeground=C_FG,
                       font=FONT_SMALL, relief="flat", highlightthickness=0
                       ).pack(side="left", padx=(8, 0))
        self.ocr_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row1, text="窗口", variable=self.ocr_var,
                       command=self.toggle_window,
                       bg=C_HEAD, fg=C_DIM, selectcolor=C_SRC_BG,
                       activebackground=C_HEAD, activeforeground=C_FG,
                       font=FONT_SMALL, relief="flat", highlightthickness=0
                       ).pack(side="left", padx=(6, 0))
        self.voice_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row1, text="声音", variable=self.voice_var,
                       command=self.toggle_voice,
                       bg=C_HEAD, fg=C_DIM, selectcolor=C_SRC_BG,
                       activebackground=C_HEAD, activeforeground=C_FG,
                       font=FONT_SMALL, relief="flat", highlightthickness=0
                       ).pack(side="left", padx=(6, 0))
        self.live_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row1, text="直播", variable=self.live_var,
                       command=self.toggle_live,
                       bg=C_HEAD, fg=C_DIM, selectcolor=C_SRC_BG,
                       activebackground=C_HEAD, activeforeground=C_FG,
                       font=FONT_SMALL, relief="flat", highlightthickness=0
                       ).pack(side="left", padx=(6, 0))
        self.btn_copy = tk.Label(row2, text="复制", **btn_opts)
        self.btn_copy.pack(side="right")
        self.btn_copy.bind("<Button-1>", self.copy_result)
        self.status = tk.Label(row2, text="就绪", bg=C_HEAD, fg=C_DIM,
                               font=FONT_SMALL, anchor="w")
        self.status.pack(side="right", fill="x", expand=True)

        self.maybe_placeholder()

        def _esc(event):
            if isinstance(event.widget, tk.Text):
                return
            if getattr(self, "_overlay", None) is not None:
                return
            self.on_close()
        r.bind_all("<Escape>", _esc)

        # 按实际内容宽度贴屏幕右上角, 避免被屏幕边缘截断
        r.update_idletasks()
        w = r.winfo_reqwidth()
        r.geometry(f"+{max(8, r.winfo_screenwidth() - w - 24)}+90")

    # 占位符 -----------------------------------------------------------

    def src_text(self):
        return self.src.get("1.0", "end").strip()

    def clear_placeholder(self, _event=None):
        if self.src.get("1.0", "end").strip() == PLACEHOLDER:
            self.src.delete("1.0", "end")
        self.src.configure(fg=C_FG)

    def maybe_placeholder(self, _event=None):
        if not self.src_text():
            self.src.delete("1.0", "end")
            self.src.insert("1.0", PLACEHOLDER)
            self.src.configure(fg="#6b7280")

    def _set_src(self, text):
        self.src.delete("1.0", "end")
        if text:
            self.src.insert("1.0", text)
            self.src.configure(fg=C_FG)
        self.schedule_translate()

    def _set_out(self, text):
        self.dst.configure(state="normal")
        self.dst.delete("1.0", "end")
        if text:
            self.dst.insert("1.0", text)
        self.dst.configure(state="disabled")
        self.dst.yview_moveto(0)

    # 交互 -------------------------------------------------------------

    def start_move(self, event):
        self._dx, self._dy = event.x, event.y

    def do_move(self, event):
        self.root.geometry(f"+{event.x_root - self._dx}+{event.y_root - self._dy}")

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            self.footer.pack_forget()
            self.btn_fold.configure(text="▴")
        else:
            self.footer.pack(side="bottom", fill="x")
            self.body.pack(fill="x")
            self.btn_fold.configure(text="▾")

    def copy_result(self, _event=None):
        if self.last_out:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.last_out)
            self.set_status("已复制译文")
            self.last_clip = self.last_out  # 避免剪贴板监控把译文再翻一遍

    def on_src_key(self, _event=None):
        self.schedule_translate()

    def schedule_translate(self, delay=600):
        if self.pending_after:
            self.root.after_cancel(self.pending_after)
        self.pending_after = self.root.after(delay, self.translate_now)

    def on_close(self):
        self.stop_monitor()
        self.stop_audio("mic")
        self.stop_audio("system")
        self.root.destroy()

    # 翻译 -------------------------------------------------------------

    def set_language(self, code):
        old = self.engine
        self.engine = build_engine(self.engine_name, self.http, "auto", code, self.cfg)
        if old is not None and old.name == self.engine.name == "bing" and old._cfg:
            self.engine._cfg = old._cfg
        self.cache.clear()
        self.last_sent = None
        self.schedule_translate(delay=80)

    def on_lang_change(self, _event=None):
        code = dict(LANGS)[self.lang_var.get()]
        self.set_language(code)

    def translate_now(self):
        self.pending_after = None
        text = self.src_text()
        if not text or text == PLACEHOLDER:
            self._set_out("")
            self.last_out = ""
            self.set_status("就绪")
            return
        if len(text) < 2 or not LETTER_RE.search(text):
            self.set_status("内容太少, 跳过")
            return
        if text == self.last_sent:
            return
        if text in self.cache:
            self.req_t0 = time.time()
            self.show_result(text, self.cache[text])
            return
        self.job_seq = getattr(self, "job_seq", 0) + 1
        self.last_sent = text
        self.req_t0 = time.time()
        self.req_q.put((self.job_seq, text))
        self.set_status("翻译中…")

    def show_result(self, text, out):
        self.cache[text] = out
        if len(self.cache) > 300:
            self.cache.clear()
        self._set_out(out)
        self.last_out = out
        self.set_status(f"完成 · {time.time() - self.req_t0:.1f}s")

    def set_status(self, text, error=False):
        self.status.configure(text=text, fg=C_ERR if error else C_DIM)

    # 后台线程与轮询 ---------------------------------------------------

    def worker_loop(self):
        while True:
            item = self.req_q.get()
            if item is None:
                return
            _seq, text = item
            try:
                if self.engine.name == "bing" and len(text) > self.chunk:
                    out = "".join(self.engine.call(p) for p in split_chunks(text, self.chunk))
                else:
                    out = self.engine.call(text)
                self.res_q.put((text, out, None))
            except Exception as exc:
                self.res_q.put((text, None, str(exc)))

    def poll_results(self):
        while True:
            try:
                text, out, err = self.res_q.get_nowait()
            except queue.Empty:
                break
            if text != self.src_text():
                continue  # 结果已过期(输入变了)
            if err:
                self._set_out(f"[翻译失败] {err}")
                self.last_out = ""
                self.set_status("失败", error=True)
            else:
                self.show_result(text, out)
        self.root.after(120, self.poll_results)

    def poll_clipboard(self):
        if self.clip_on.get():
            try:
                text = self.root.clipboard_get()
            except tk.TclError:
                text = ""
            if text and text != self.last_clip and text != self.last_out and len(text) <= 5000:
                self.last_clip = text
                self._set_src(text)
        self.root.after(900, self.poll_clipboard)

    # 窗口选择与 OCR 监控 ------------------------------------------------

    def toggle_window(self):
        if self.ocr_var.get():
            self.choose_window()
        else:
            self.stop_monitor()

    def choose_window(self):
        wins = list_windows()
        if not wins:
            self.set_status("没有找到可选择的窗口", error=True)
            self.ocr_var.set(False)
            return
        dlg = tk.Toplevel(self.root)
        self._overlay = dlg
        dlg.title("选择要翻译的窗口")
        dlg.attributes("-topmost", True)
        dlg.geometry("560x430")
        dlg.configure(bg=C_BG)
        bo = dict(bg=C_BG, fg=C_DIM, font=FONT_SMALL, cursor="hand2", padx=8)
        bar = tk.Frame(dlg, bg=C_BG)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(bar, text="搜索:", bg=C_BG, fg=C_DIM, font=FONT_SMALL).pack(side="left")
        kw = tk.StringVar()
        entry = tk.Entry(bar, textvariable=kw, bg=C_SRC_BG, fg=C_FG,
                         insertbackground=C_FG, relief="flat", font=FONT_SMALL)
        entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        listbox = tk.Listbox(dlg, bg=C_SRC_BG, fg=C_FG, selectbackground=C_SEL,
                             relief="flat", font=FONT_SMALL, activestyle="none")
        sb = ttk.Scrollbar(dlg, command=listbox.yview)
        listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=4)
        listbox.pack(fill="both", expand=True, padx=(8, 0), pady=4)

        shown = []

        def refill(_a=None, _b=None, _c=None):
            nonlocal shown
            key = kw.get().strip().lower()
            listbox.delete(0, "end")
            shown = [(h, t, x) for (h, t, x) in wins
                     if not key or key in t.lower() or key in x.lower()]
            for _h, t, x in shown[:200]:
                listbox.insert("end", f"{x or '?'}  —  {t or '(无标题)'}"[:90])

        kw.trace_add("write", refill)
        refill()

        def confirm(_e=None):
            sel = listbox.curselection()
            if not sel:
                return
            hwnd, title, exe = shown[sel[0]]
            dlg.destroy()
            self._overlay = None
            self.monitor_title = (exe + " " + title)[:34]
            self.start_monitor(hwnd=hwnd)

        def cancel(_e=None):
            dlg.destroy()
            self._overlay = None
            self.ocr_var.set(False)

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(btns, text="双击列表项或点\"选择\"", bg=C_BG, fg="#6b7280",
                 font=FONT_SMALL).pack(side="left")
        ok = tk.Label(btns, text="选择", **bo)
        ok.pack(side="right")
        ok.bind("<Button-1>", confirm)
        ca = tk.Label(btns, text="取消", **bo)
        ca.pack(side="right", padx=(0, 6))
        ca.bind("<Button-1>", cancel)
        listbox.bind("<Double-Button-1>", confirm)
        dlg.bind("<Escape>", cancel)
        entry.focus_set()

    def start_monitor(self, hwnd=None, region=None, lang=None):
        self.stop_monitor()
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_ocr.ps1")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
        if hwnd:
            cmd += ["-Hwnd", str(hwnd)]
        else:
            left, top, width, height = region or (0, 0, 0, 0)
            if width < 30 or height < 20:
                self.set_status("区域太小", error=True)
                return
            cmd += ["-Left", str(left), "-Top", str(top),
                    "-Width", str(width), "-Height", str(height)]
        lang = lang or self.cfg.get("ocr_lang") or ""
        if lang:
            cmd += ["-Lang", lang]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.ocr_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace",
                                         creationflags=flags)
        bind_job(self.ocr_proc)
        threading.Thread(target=self.ocr_reader, daemon=True).start()
        self._last_ocr_compact = ""
        if hwnd:
            self.set_status(f"监控窗口 {self.monitor_title}…")
        else:
            self.set_status(f"屏幕监控中 {region[2]}×{region[3]}…")

    def stop_monitor(self):
        if self.ocr_proc:
            try:
                self.ocr_proc.kill()
            except Exception:
                pass
            self.ocr_proc = None
            self.set_status("已停止窗口监控")

    def ocr_reader(self):
        proc = self.ocr_proc
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line.startswith("TEXT\t"):
                    self.ocr_q.put(("text", line[5:].strip()))
                elif line.startswith("ERR\t"):
                    self.ocr_q.put(("err", line[4:]))
                elif line.startswith("LANG\t"):
                    self.ocr_q.put(("lang", line[5:]))
        except Exception:
            pass

    def _feed_ocr_text(self, raw):
        """整窗 OCR 噪声大: 与上次结果对比, 只把真正变化的部分送去翻译"""
        compact = re.sub(r"\s+", "", raw)
        prev = self._last_ocr_compact
        self._last_ocr_compact = compact
        if not compact or len(compact) < 2:
            return
        if not prev:
            self._set_src(raw.strip())
            return
        n = min(len(prev), len(compact))
        pre = 0
        while pre < n and prev[pre] == compact[pre]:
            pre += 1
        suf = 0
        while (suf < n - pre
               and prev[len(prev) - 1 - suf] == compact[len(compact) - 1 - suf]):
            suf += 1
        changed = compact[pre:len(compact) - suf]
        if len(changed) < 2 or not LETTER_RE.search(changed):
            return
        if len(changed) < 0.4 * max(len(prev), len(compact), 1):
            self._set_src(changed)          # 小变化: 新字幕/弹幕等增量内容
        else:
            self._set_src(raw.strip())      # 大变化: 整个窗口文本重翻

    def poll_ocr(self):
        try:
            while True:
                kind, payload = self.ocr_q.get_nowait()
                if kind == "text":
                    self._feed_ocr_text(payload)
                elif kind == "voice":
                    if payload and payload != self.src_text():
                        self._set_src(payload)
                elif kind == "lang":
                    self.set_status(f"窗口监控中 · 识别 {payload}")
                elif kind == "vlang":
                    name = "麦克风" if payload[0] == "mic" else "系统声音"
                    self.set_status(f"{name}语音识别 {payload[1]} 中…")
                elif kind == "vsrc":
                    name = "麦克风" if payload[0] == "mic" else "系统声音"
                    self.set_status(f"{name}已连接 ({payload[1]})")
                elif kind == "err":
                    self.set_status("窗口识别: " + payload, error=True)
                    self.stop_monitor()
                    self.ocr_var.set(False)
                elif kind == "verr":
                    name = "麦克风" if payload[0] == "mic" else "系统声音"
                    self.set_status(f"{name}语音识别: " + payload[1], error=True)
                    self.stop_audio(payload[0])
                    (self.voice_var if payload[0] == "mic" else self.live_var).set(False)
        except queue.Empty:
            pass
        self.root.after(300, self.poll_ocr)

    # 声音翻译: 麦克风(mic) / 系统正在播放的声音(system, 直播·游戏·视频) ------

    def toggle_voice(self):
        self._toggle_audio("mic", self.voice_var)

    def toggle_live(self):
        self._toggle_audio("system", self.live_var)

    def _toggle_audio(self, source, var):
        if var.get():
            self.start_audio(source)
        else:
            self.stop_audio(source)

    def start_audio(self, source):
        self.stop_audio(source)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speech_recog.ps1")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
               "-Source", source]
        vl = self.cfg.get("voice_lang") or ""
        if vl:
            cmd += ["-Lang", vl]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=flags)
        bind_job(proc)
        label = "语音识别(麦克风)" if source == "mic" else "语音识别(系统声音)"
        threading.Thread(target=self.audio_reader, args=(proc, source, label),
                         daemon=True).start()
        self.audio_procs[source] = proc
        self.set_status(label + "启动中…")

    def stop_audio(self, source):
        proc = self.audio_procs.get(source)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
            self.audio_procs[source] = None
            self.set_status("已停止语音识别")

    def audio_reader(self, proc, source, label):
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line.startswith("TEXT\t"):
                    self.ocr_q.put(("voice", line[5:].strip()))
                elif line.startswith("ERR\t"):
                    self.ocr_q.put(("verr", (source, line[4:])))
                elif line.startswith("SRC\t"):
                    self.ocr_q.put(("vsrc", (source, line[4:])))
                elif line.startswith("LANG\t"):
                    self.ocr_q.put(("vlang", (source, line[5:])))
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(prog="floating.py", description="悬浮窗实时翻译")
    ap.add_argument("--window", help="按标题/进程名关键字选择窗口并监控, 如 --window Edge")
    ap.add_argument("--region", help="改为监控屏幕区域 L,T,W,H (跳过窗口选择)")
    ap.add_argument("--lang", help="OCR 识别语言, 如 zh-Hans-CN / en-US")
    ap.add_argument("--voice", action="store_true", help="启动时同时开启麦克风语音识别")
    ap.add_argument("--live", action="store_true", help="启动时同时开启系统声音识别(直播/游戏/视频)")
    args = ap.parse_args()

    cfg = load_config()
    root = tk.Tk()
    app = FloatingApp(root, cfg)
    if args.window:
        kw = args.window.lower()
        match = next(((h, t, x) for h, t, x in list_windows()
                      if kw in t.lower() or kw in x.lower()), None)
        if match:
            app.monitor_title = (match[2] + " " + match[1])[:34]
            app.ocr_var.set(True)
            app.start_monitor(hwnd=match[0], lang=args.lang)
        else:
            print(f"window not found: {args.window}", file=sys.stderr)
    elif args.region:
        try:
            left, top, width, height = (int(v) for v in args.region.split(","))
            app.ocr_var.set(True)
            app.start_monitor(region=(left, top, width, height), lang=args.lang)
        except ValueError:
            print(f"bad --region: {args.region}", file=sys.stderr)
    if args.voice:
        app.voice_var.set(True)
        app.start_audio("mic")
    if args.live:
        app.live_var.set(True)
        app.start_audio("system")
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror("悬浮翻译启动失败", traceback.format_exc()[-900:])
        except Exception:
            pass
        sys.exit(1)
