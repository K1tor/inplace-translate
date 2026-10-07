#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
悬浮窗声音翻译 —— InPlace Translate 的悬浮窗模式。

实时声音翻译, 全程无需手动输入:
- 声音: 识别麦克风, 翻译你说的话
- 直播: WASAPI 环回捕获系统正在播放的声音(直播/游戏/视频语音), 实时翻译
- 翻译引擎默认接入大模型(GLM-4-Flash, 免费/国内直连), 无密钥自动回退 Bing
- 输入框仅作备用: 打字/粘贴也会实时翻译(0.6 秒防抖)
- 与 translator.py 共用 config.json (openai_key / voice_lang)
- 标题栏可拖动; ▾ 收起成小条; Esc 或 ✕ 关闭
"""

import argparse
import os
import queue
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

PLACEHOLDER = "打开声音/直播, 说话自动翻译…"

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_ICON = ("Segoe Fluent Icons", 10)

# Win11 深色风格
C_BG = "#202020"
C_HEAD = "#202020"
C_SRC_BG = "#2b2b2b"
C_DST_BG = "#272727"
C_BORDER = "#3f3f3f"
C_ACCENT = "#4CC2FF"
C_BTN = "#2d2d2d"
C_BTN_HOVER = "#373737"
C_ACCENT_BTN = "#0078D4"
C_ACCENT_BTN_HOVER = "#1a86d9"
C_CLOSE_HOVER = "#C42B1C"
C_FG = "#ffffff"
C_DIM = "#9d9d9d"
C_RES = "#ffffff"
C_ERR = "#ff99a4"
C_SEL = "#0078D4"


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
    """把子进程绑定到 Job 对象: 本进程退出时识别子进程一并结束, 避免孤儿进程"""
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


def _dwm_round(widget):
    """Win11 窗口圆角 (DWMWA_WINDOW_CORNER_PREFERENCE=2)"""
    if os.name != "nt":
        return
    try:
        val = ctypes.c_int(2)
        user32 = ctypes.windll.user32
        for hwnd in (widget.winfo_id(), user32.GetParent(widget.winfo_id())):
            if hwnd:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(val), 4)
    except Exception:
        pass


class FloatingApp:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.chunk = int(cfg.get("chunk", 1000))
        engine_name = cfg.get("engine") or "bing"
        if engine_name == "openai" and not (os.environ.get("OPENAI_API_KEY")
                                            or cfg.get("openai_key")):
            engine_name = "bing"
            self.llm_missing = True
        else:
            self.llm_missing = False
        self.engine_name = engine_name

        proxy = resolve_proxy(None, cfg)
        # 大模型接口无需请求间隔, 语音句子逐条到达, 去掉延迟让翻译更快
        self.http = Http([proxy, ""] if proxy else [""],
                         delay=0.0 if engine_name == "openai" else float(cfg.get("delay", 0.3)))
        self.engine = None
        self.cache = {}
        self.last_sent = None
        self.req_t0 = 0.0
        self.last_out = ""
        self.pending_after = None
        self.collapsed = False
        self.audio_procs = {"mic": None, "system": None}
        self.audio_q = queue.Queue()

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
        self.root.after(150, self.poll_audio)

        if self.llm_missing:
            self.set_status("未配置大模型密钥, 已回退 Bing")

    # ------------------------------------------------------------ 界面

    def build_ui(self):
        r = self.root
        r.title("悬浮翻译")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        try:
            r.configure(bg=C_BG, highlightthickness=2, highlightbackground=C_BORDER)
        except Exception:
            r.configure(bg=C_BG)

        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=C_SRC_BG, background=C_BTN,
                        foreground=C_FG, arrowcolor=C_FG, bordercolor=C_BORDER,
                        lightcolor=C_BORDER, darkcolor=C_SRC_BG, insertcolor=C_FG)
        style.map("TCombobox",
                  fieldbackground=[("readonly", C_SRC_BG), ("focus", C_SRC_BG)],
                  foreground=[("readonly", C_FG)],
                  selectbackground=[("readonly", C_SRC_BG)],
                  selectforeground=[("readonly", C_FG)])
        style.configure("TScrollbar", background=C_BTN, troughcolor=C_BG,
                        bordercolor=C_BG, arrowcolor=C_DIM)
        style.map("TScrollbar", background=[("active", C_BTN_HOVER)])
        r.option_add("*TCombobox*Listbox*Background", C_SRC_BG)
        r.option_add("*TCombobox*Listbox*Foreground", C_FG)
        r.option_add("*TCombobox*Listbox*selectBackground", C_SEL)
        r.option_add("*TCombobox*Listbox*selectForeground", C_FG)
        r.option_add("*TCombobox*Listbox*Font", FONT_SMALL)

        # Win11 风格标题栏: Fluent 图标 + 悬停变色(关闭=红), 整栏可拖动
        head = tk.Frame(r, bg=C_HEAD, padx=8, pady=2)
        head.pack(fill="x")
        title = tk.Label(head, text="\uE774  声音翻译", bg=C_HEAD, fg=C_FG,
                         font=FONT_TITLE, cursor="fleur", anchor="w")
        title.pack(side="left", fill="x", expand=True)
        btn_close = tk.Label(head, text="\uE8BB", bg=C_HEAD, fg=C_FG,
                             font=FONT_ICON, cursor="hand2", padx=10, pady=1)
        self.btn_fold = tk.Label(head, text="\uE70D", bg=C_HEAD, fg=C_FG,
                                 font=FONT_ICON, cursor="hand2", padx=10, pady=1)
        btn_close.pack(side="right")
        self.btn_fold.pack(side="right")
        self.btn_fold.bind("<Enter>", lambda e: self.btn_fold.configure(bg=C_BTN_HOVER))
        self.btn_fold.bind("<Leave>", lambda e: self.btn_fold.configure(bg=C_HEAD))
        btn_close.bind("<Enter>", lambda e: btn_close.configure(bg=C_CLOSE_HOVER))
        btn_close.bind("<Leave>", lambda e: btn_close.configure(bg=C_HEAD))
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
                           highlightthickness=1, highlightbackground=C_BORDER,
                           highlightcolor=C_ACCENT, spacing1=1, spacing3=1)
        self.src.pack(fill="x", padx=4, pady=(4, 0))
        self.src.bind("<KeyRelease>", self.on_src_key)
        self.src.bind("<FocusIn>", self.clear_placeholder)
        self.src.bind("<FocusOut>", self.maybe_placeholder)

        self.dst_frame = tk.Frame(self.body, bg=C_BG)
        self.dst_frame.pack(fill="x", padx=4, pady=(2, 0))
        self.dst = tk.Text(self.dst_frame, height=7, width=38, wrap="word", font=FONT,
                           bg=C_DST_BG, fg=C_RES, relief="flat", padx=8, pady=6,
                           selectbackground=C_SEL, state="disabled",
                           highlightthickness=1, highlightbackground=C_BORDER,
                           spacing1=1, spacing3=1)
        scroll = ttk.Scrollbar(self.dst_frame, command=self.dst.yview)
        self.dst.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.dst.pack(side="left", fill="both", expand=True)

        # 底部两行: 上行 = 语言与声音开关, 下行 = 状态与复制
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
                                  width=11, font=FONT_SMALL)
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", self.on_lang_change)
        self.voice_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row1, text="声音", variable=self.voice_var,
                       command=self.toggle_voice,
                       bg=C_HEAD, fg=C_DIM, selectcolor=C_ACCENT,
                       activebackground=C_HEAD, activeforeground=C_FG,
                       font=FONT_SMALL, relief="flat", highlightthickness=0
                       ).pack(side="left", padx=(10, 0))
        self.live_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row1, text="直播", variable=self.live_var,
                       command=self.toggle_live,
                       bg=C_HEAD, fg=C_DIM, selectcolor=C_ACCENT,
                       activebackground=C_HEAD, activeforeground=C_FG,
                       font=FONT_SMALL, relief="flat", highlightthickness=0
                       ).pack(side="left", padx=(8, 0))
        self.btn_copy = tk.Label(row2, text="复制", bg=C_ACCENT_BTN, fg=C_FG,
                                 font=FONT_SMALL, cursor="hand2", padx=10, pady=2)
        self.btn_copy.pack(side="right")
        self.btn_copy.bind("<Button-1>", self.copy_result)
        self.btn_copy.bind("<Enter>", lambda e: self.btn_copy.configure(bg=C_ACCENT_BTN_HOVER))
        self.btn_copy.bind("<Leave>", lambda e: self.btn_copy.configure(bg=C_ACCENT_BTN))
        self.status = tk.Label(row2, text="就绪", bg=C_HEAD, fg=C_DIM,
                               font=FONT_SMALL, anchor="w")
        self.status.pack(side="right", fill="x", expand=True)

        self.maybe_placeholder()

        def _esc(event):
            if isinstance(event.widget, tk.Text):
                return
            self.on_close()
        r.bind_all("<Escape>", _esc)

        # 按实际内容宽度贴屏幕右上角, 避免被屏幕边缘截断
        r.update_idletasks()
        w = r.winfo_reqwidth()
        r.geometry(f"+{max(8, r.winfo_screenwidth() - w - 24)}+90")
        _dwm_round(r)
        # Tk 9 + overrideredirect 下 DWM 圆角属性有时不生效, 补一帧延时再试
        r.after(50, _dwm_round, r)

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
            self.src.configure(fg="#6b6b6b")

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
            self.btn_fold.configure(text="\uE70E")
        else:
            self.footer.pack(side="bottom", fill="x")
            self.body.pack(fill="x")
            self.btn_fold.configure(text="\uE70D")

    def copy_result(self, _event=None):
        if self.last_out:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.last_out)
            self.set_status("已复制译文")

    def on_src_key(self, _event=None):
        self.schedule_translate()

    def schedule_translate(self, delay=600):
        if self.pending_after:
            self.root.after_cancel(self.pending_after)
        self.pending_after = self.root.after(delay, self.translate_now)

    def on_close(self):
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
        engine_tag = "大模型" if self.engine_name == "openai" else "Bing"
        self.set_status(f"完成 · {time.time() - self.req_t0:.1f}s · {engine_tag}")

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
                    self.audio_q.put(("voice", line[5:].strip()))
                elif line.startswith("ERR\t"):
                    self.audio_q.put(("verr", (source, line[4:])))
                elif line.startswith("SRC\t"):
                    self.audio_q.put(("vsrc", (source, line[4:])))
                elif line.startswith("LANG\t"):
                    self.audio_q.put(("vlang", (source, line[5:])))
        except Exception:
            pass

    def poll_audio(self):
        try:
            while True:
                kind, payload = self.audio_q.get_nowait()
                if kind == "voice":
                    if payload and payload != self.src_text():
                        self._set_src(payload)
                elif kind == "vlang":
                    name = "麦克风" if payload[0] == "mic" else "系统声音"
                    self.set_status(f"{name}语音识别 {payload[1]} 中…")
                elif kind == "vsrc":
                    name = "麦克风" if payload[0] == "mic" else "系统声音"
                    self.set_status(f"{name}已连接 ({payload[1]})")
                elif kind == "verr":
                    name = "麦克风" if payload[0] == "mic" else "系统声音"
                    self.set_status(f"{name}语音识别: " + payload[1], error=True)
                    self.stop_audio(payload[0])
                    (self.voice_var if payload[0] == "mic" else self.live_var).set(False)
        except queue.Empty:
            pass
        self.root.after(150, self.poll_audio)


def main():
    ap = argparse.ArgumentParser(prog="floating.py", description="悬浮窗声音翻译")
    ap.add_argument("--voice", action="store_true", help="启动时同时开启麦克风识别")
    ap.add_argument("--live", action="store_true", help="启动时同时开启系统声音识别")
    args = ap.parse_args()

    cfg = load_config()
    root = tk.Tk()
    app = FloatingApp(root, cfg)
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
