#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
InPlace Translate —— 把文件文本翻译成目标语言并原位覆盖。

引擎:
  bing    微软必应翻译(免费, 国内直连, 默认)
  google  Google 翻译(免费, 国内需代理)
  deepl   DeepL API(需免费密钥, 以 :fx 结尾)
  openai  OpenAI 兼容接口(需密钥)

格式:
  纯文本  .txt .md(保留代码围栏) 等, 整篇分段翻译
  字幕    .srt .vtt, 只翻译字幕文本, 保留时间轴
  歌词    .lrc, 保留时间标签
  JSON    只翻译字符串值, 键与结构不变(缩进会重排为 2 空格)
"""

import argparse
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) InPlaceTranslate/1.0"
BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

CODE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
             ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".sh", ".bat",
             ".ps1", ".sql", ".html", ".htm", ".xml", ".yaml", ".yml", ".toml",
             ".ini", ".cfg", ".css", ".lua", ".jsonc"}
DEFAULT_EXTS = {".txt", ".md", ".srt", ".vtt", ".json", ".lrc"}

GOOGLE_ALIASES = {"zh": "zh-CN", "zh-cn": "zh-CN", "zh-hans": "zh-CN",
                  "zh-tw": "zh-TW", "zh-hant": "zh-TW", "zt": "zh-TW",
                  "in": "id", "iw": "he"}
BING_ALIASES = {"zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-hans": "zh-Hans",
                "zh-tw": "zh-Hant", "zh-hant": "zh-Hant", "zt": "zh-Hant",
                "in": "id", "iw": "he"}
DEEPL_ALIASES = {"zh": "ZH", "zh-cn": "ZH", "zh-hans": "ZH",
                 "zh-tw": "ZH", "zh-hant": "ZH",
                 "en": "EN-US", "en-us": "EN-US", "en-gb": "EN-GB", "pt": "PT-BR"}

LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def eprint(*args):
    print(*args, file=sys.stderr)


# ---------------------------------------------------------------- 配置与代理

def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.path.join(script_dir(), "config.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as exc:
            eprint(f"[警告] 读取 config.json 失败: {exc}")
    return {}


def resolve_proxy(args_proxy, cfg):
    return (args_proxy or cfg.get("proxy")
            or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()


# ---------------------------------------------------------------- HTTP

class Http:
    """带重试、代理回退(代理失败自动尝试直连)、延迟限速的请求器"""

    def __init__(self, proxies, delay=0.3, timeout=20):
        self.proxies = proxies          # 候选代理列表, "" 表示直连
        self.delay = delay
        self.timeout = timeout
        self.insecure = False
        self.requests = 0
        self.last_url = ""

    def _opener(self, proxy):
        handlers = []
        if self.insecure:
            handlers.append(urllib.request.HTTPSHandler(
                context=ssl._create_unverified_context()))
        if proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def request(self, url, data=None, headers=None, timeout=None):
        last = None
        for proxy in self.proxies:
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, data=data, headers=headers or {})
                    with self._opener(proxy).open(req, timeout=timeout or self.timeout) as resp:
                        body = resp.read()
                        self.last_url = resp.geturl()
                    self.requests += 1
                    if self.delay:
                        time.sleep(self.delay)
                    return body
                except urllib.error.HTTPError as exc:
                    last = exc
                    if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                        time.sleep(min(1.5 * 2 ** attempt, 6))
                        continue
                    raise
                except ssl.SSLCertVerificationError as exc:
                    last = exc
                    if not self.insecure:
                        self.insecure = True
                        eprint("[警告] 证书校验失败, 本次运行将以不校验证书的方式重试")
                        continue
                    raise
                except (urllib.error.URLError, TimeoutError, socket.timeout,
                        ConnectionError, OSError) as exc:
                    last = exc
                    if attempt < 2:
                        time.sleep(min(1.0 * 2 ** attempt, 4))
                        continue
            # 当前候选彻底失败, 换下一个(通常是直连)
        raise last


# ---------------------------------------------------------------- 翻译引擎

class BingEngine:
    """微软必应翻译: 从 bing.com/translator 页面取 token, POST ttranslatev3"""
    name = "bing"

    def __init__(self, http, source, target):
        self.http = http
        self.source = source if source and source != "auto" else "auto-detect"
        self.target = BING_ALIASES.get(target.lower(), target)
        self._cfg = None

    def _fetch_config(self):
        body = self.http.request("https://www.bing.com/translator",
                                 headers={"User-Agent": BING_UA})
        page = body.decode("utf-8", "replace")

        def need(pattern, what):
            m = re.search(pattern, page)
            if not m:
                raise RuntimeError(f"无法从 Bing 页面提取 {what}")
            return m.group(1)

        ig = need(r'IG:"([^"]+)"', "IG")
        iid = need(r'data-iid="([^"]+)"', "IID")
        m = re.search(r'params_AbusePreventionHelper\s?=\s?\[([^\]]+)\]', page)
        if not m:
            raise RuntimeError("无法从 Bing 页面提取 token")
        parts = m.group(1).split(",")
        key = parts[0].strip()
        token = parts[1].strip().strip('"')
        sub = ""
        m = re.match(r"https?://(\w+)\.bing\.com", self.http.last_url or "")
        if m:
            sub = m.group(1) + "."
        self._cfg = {"sub": sub, "ig": ig, "iid": iid, "token": token, "key": key}

    def _post(self, text):
        c = self._cfg
        url = (f"https://{c['sub']}bing.com/ttranslatev3?isVertical=1"
               f"&IG={urllib.parse.quote(c['ig'])}&IID={urllib.parse.quote(c['iid'])}")
        data = urllib.parse.urlencode({
            "fromLang": self.source,
            "text": text,
            "to": self.target,
            "token": c["token"],
            "key": c["key"],
            "tryFetchingGenderDebiasedTranslations": "true",
        }).encode("utf-8")
        try:
            raw = self.http.request(url, data=data, headers={
                "User-Agent": BING_UA,
                "Referer": f"https://{c['sub']}bing.com/translator",
            })
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise _BingRefresh()
            raise
        s = raw.decode("utf-8", "replace")
        obj = json.loads(s)
        if isinstance(obj, dict):
            if obj.get("statusCode") == 401:
                raise _BingRefresh()
            raise RuntimeError(f"bing 返回异常: {s[:150]}")
        try:
            return obj[0]["translations"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"bing 返回格式异常: {s[:150]}")

    def call(self, text):
        if self._cfg is None:
            self._fetch_config()
        try:
            return self._post(text)
        except _BingRefresh:
            self._fetch_config()
            return self._post(text)


class _BingRefresh(Exception):
    """Bing token 过期, 需要重新获取会话"""


class GoogleEngine:
    name = "google"
    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, http, source, target):
        self.http = http
        self.source = source or "auto"
        self.target = GOOGLE_ALIASES.get(target.lower(), target)

    def call(self, text):
        params = [("client", "gtx"), ("sl", self.source),
                  ("tl", self.target), ("dt", "t"), ("q", text)]
        qs = urllib.parse.urlencode(params)
        headers = {"User-Agent": UA}
        if len(qs) > 3800:  # URL 太长时改用 POST
            raw = self.http.request(
                self.URL, data=qs.encode("utf-8"),
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
        else:
            raw = self.http.request(self.URL + "?" + qs, headers=headers)
        data = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            raise RuntimeError(f"google 返回格式异常: {str(data)[:120]}")
        return "".join(seg[0] for seg in data[0]
                       if isinstance(seg, list) and seg and isinstance(seg[0], str))


class DeeplEngine:
    name = "deepl"

    def __init__(self, http, source, target, key):
        if not key:
            raise SystemExit("使用 deepl 引擎需要密钥: config.json 的 deepl_api_key "
                             "或环境变量 DEEPL_API_KEY (免费密钥以 :fx 结尾)")
        host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
        self.http = http
        self.url = host + "/v2/translate"
        self.key = key
        self.source = source if source and source != "auto" else None
        self.target = DEEPL_ALIASES.get(target.lower(), target.upper())

    def call_many(self, texts):
        body = {"text": texts, "target_lang": self.target}
        if self.source:
            body["source_lang"] = DEEPL_ALIASES.get(self.source.lower(), self.source.upper())
        raw = self.http.request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "DeepL-Auth-Key " + self.key,
                     "Content-Type": "application/json"})
        out = [t["text"] for t in json.loads(raw.decode("utf-8"))["translations"]]
        if len(out) != len(texts):
            raise RuntimeError("deepl 返回数量不匹配")
        return out

    def call(self, text):
        return self.call_many([text])[0]


class OpenAIEngine:
    name = "openai"

    def __init__(self, http, source, target, base, key, model):
        if not key:
            raise SystemExit("使用 openai 引擎需要密钥: 环境变量 OPENAI_API_KEY "
                             "或 config.json 的 openai_key")
        self.http = http
        self.url = (base or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self.model = model or "gpt-4o-mini"
        self._key = key
        src = f" from {source}" if source and source != "auto" else ""
        self.system = (f"You are a professional translator. Translate the user's text{src} "
                       f"into {target}. Reply with the translation ONLY, preserving original "
                       f"line breaks, punctuation style and placeholders. Never add notes.")

    def call(self, text):
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": self.system},
                             {"role": "user", "content": text}]}
        raw = self.http.request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._key,
                     "Content-Type": "application/json"},
            timeout=120)
        return json.loads(raw.decode("utf-8"))["choices"][0]["message"]["content"].strip()


def args_verbose_hint():
    return os.environ.get("IPT_DEBUG") == "1"


# ---------------------------------------------------------------- 文本切分

def split_chunks(text, max_len):
    """把文本切成不超过 max_len 的块, 所有块拼接后与原文完全一致"""
    if len(text) <= max_len:
        return [text]
    pieces = []
    for line in text.splitlines(keepends=True):
        if len(line) <= max_len:
            pieces.append(line)
            continue
        for part in re.split(r"(?<=[。！？!?；;.])", line):
            while len(part) > max_len:
                pieces.append(part[:max_len])
                part = part[max_len:]
            if part:
                pieces.append(part)
    chunks, buf = [], ""
    for piece in pieces:
        if buf and len(buf) + len(piece) > max_len:
            chunks.append(buf)
            buf = ""
        buf += piece
    if buf:
        chunks.append(buf)
    return chunks


def pack_by_length(texts, max_chars):
    """把多条文本按下标分组, 每组合并后总长不超过 max_chars"""
    groups, cur, size = [], [], 0
    for i, t in enumerate(texts):
        n = min(len(t), max_chars) + 1
        if cur and size + n > max_chars:
            groups.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        groups.append(cur)
    return groups


# ---------------------------------------------------------------- 各格式的可翻译单元

def srt_units(text):
    units = []
    for line in text.split("\n"):
        s = line.strip()
        ok = (bool(s) and "-->" not in s
              and not re.fullmatch(r"\d+", s)
              and not s.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")))
        units.append((line, ok))
    return units


def lrc_units(text):
    units = []
    for line in text.split("\n"):
        m = re.match(r"^((?:\[[^\]]*\]\s*)+)(.*)$", line)
        units.append((line, bool(m and m.group(2).strip())))
    return units


def md_units(text):
    units, buf = [], []
    state = {"fence": False}

    def flush():
        if buf:
            units.append(("".join(buf), True))
            buf.clear()

    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            flush()
            state["fence"] = not state["fence"]
            units.append((line, False))
        elif state["fence"]:
            units.append((line, False))
        else:
            buf.append(line)
    flush()
    return units


def build_units(text, mode, ext):
    """返回 [(片段, 是否翻译)], 片段拼接后与原文一致(json 模式除外)"""
    if mode == "srt":
        return srt_units(text)
    if mode == "lrc":
        return lrc_units(text)
    if mode == "plain":
        if ext == ".md":
            return md_units(text)
        return [(text, True)]
    raise ValueError(mode)


def expand_units(units, chunk):
    """把过长的可翻译单元切成小块"""
    final = []
    for seg, ok in units:
        if ok and seg.strip():
            for piece in split_chunks(seg, chunk):
                final.append((piece, True))
        else:
            final.append((seg, False))
    return final


def json_should_skip(s):
    if not s.strip():
        return True
    if not LETTER_RE.search(s):          # 没有字母(数字/符号)
        return True
    if re.match(r"^\s*(?:https?:|/|\\|data:|mailto:|#)", s):
        return True                       # 链接、路径、锚点
    if len(s.strip()) == 1:
        return True
    return False


# ---------------------------------------------------------------- 翻译调度

def translate_texts(engine, texts, joinable, ctx, label=""):
    """翻译 texts, 返回等长译文列表"""
    results = [None] * len(texts)
    if not texts:
        return results
    total = len(texts)
    state = {"done": 0}

    def progress():
        state["done"] += 1
        if ctx.verbose or state["done"] % 5 == 0 or state["done"] == total:
            print(f"\r  {label} {state['done']}/{total}", end="", flush=True)

    def do_single(i):
        t = texts[i]
        if engine.name == "bing" and len(t) > ctx.chunk:
            # bing 单条有长度限制, 超长时切块翻译再拼回
            results[i] = "".join(engine.call(p) for p in split_chunks(t, ctx.chunk))
        else:
            results[i] = engine.call(t)
        progress()

    def do_group(idxs):
        joined = "\n".join(texts[i] for i in idxs)
        try:
            parts = engine.call(joined).split("\n")
        except Exception:
            return False
        if len(parts) != len(idxs):
            return False
        for i, p in zip(idxs, parts):
            results[i] = p
            progress()
        return True

    if engine.name == "deepl":
        for start in range(0, len(texts), 50):
            idxs = list(range(start, min(start + 50, len(texts))))
            outs = engine.call_many([texts[i] for i in idxs])
            for i, o in zip(idxs, outs):
                results[i] = o
                progress()
    elif joinable and len(texts) > 1:
        for idxs in pack_by_length(texts, max(64, ctx.chunk - 64)):
            if len(idxs) == 1 or not do_group(idxs):
                for i in idxs:
                    do_single(i)
    else:
        for i in range(len(texts)):
            do_single(i)
    print()
    return results


# ---------------------------------------------------------------- 文件处理

def decode_bytes(raw):
    bom = raw.startswith(b"\xef\xbb\xbf")
    data = raw[3:] if bom else raw
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            text = data.decode(enc)
            return text, ("utf-8-sig" if (enc == "utf-8" and bom) else enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", raw, 0, 1, "cannot detect encoding")


def encode_text(text, enc):
    try:
        return text.encode(enc)
    except UnicodeEncodeError:
        fallback = "utf-8-sig" if enc == "utf-8" else "utf-8"
        eprint(f"[警告] 译文无法用原编码 {enc} 保存, 已改用 {fallback}")
        return text.encode(fallback)


def unit_affix(seg, mode):
    """拆出片段中不参与翻译的部分: (要拼回的前缀, 要拼回的后缀, 要发送的正文)"""
    head = ""
    core = seg
    if mode == "lrc":
        m = re.match(r"^((?:\[[^\]]*\]\s*)+)", seg)
        if m:
            head = m.group(1)
            core = seg[len(head):]
    if not core.strip():
        return seg, "", ""
    ws_head = core[:len(core) - len(core.lstrip())]
    tail = core[len(core.rstrip()):]
    return head + ws_head, tail, core.strip()


def process_text(text, mode, ext, ctx):
    units = expand_units(build_units(text, mode, ext), ctx.chunk)
    todo_idx, texts, affix = [], [], {}
    for i, (seg, ok) in enumerate(units):
        if ok and LETTER_RE.search(seg):
            head, tail, core = unit_affix(seg, mode)
            affix[i] = (head, tail)
            todo_idx.append(i)
            texts.append(core)
    outs = translate_texts(ctx.engine_obj, texts, joinable=(mode in ("srt", "lrc")),
                           ctx=ctx, label=ext.lstrip(".").upper() or "TXT")
    trans = {i: affix[i][0] + t + affix[i][1] for i, t in zip(todo_idx, outs)}
    if mode in ("srt", "lrc"):
        new_text = "\n".join(trans[i] if i in trans else seg
                             for i, (seg, _ok) in enumerate(units))
    else:
        new_text = "".join(trans[i] if i in trans else seg
                           for i, (seg, _ok) in enumerate(units))
    samples = list(zip(texts, outs))[:5]
    return new_text, samples


def process_json(text, ctx):
    obj = json.loads(text)
    pairs = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str):
                    pairs.append((o, k))
                else:
                    walk(v)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                if isinstance(v, str):
                    pairs.append((o, i))
                else:
                    walk(v)

    walk(obj)
    skip_keys = {k.strip() for k in ctx.skip_keys.split(",") if k.strip()}
    todo, seen = [], {}
    for container, key in pairs:
        val = container[key]
        if key in skip_keys or json_should_skip(val):
            continue
        if val not in seen:
            seen[val] = None
            todo.append(val)
    outs = translate_texts(ctx.engine_obj, todo, joinable=True, ctx=ctx, label="JSON")
    for src, dst in zip(todo, outs):
        seen[src] = dst
    for container, key in pairs:
        val = container[key]
        if val in seen and seen[val] is not None:
            container[key] = seen[val]
    trailing = "\n" if text.endswith("\n") else ""
    new_text = json.dumps(obj, ensure_ascii=False, indent=2) + trailing
    samples = list(zip(todo, outs))[:5]
    return new_text, samples


def process_file(path, ctx):
    ext = os.path.splitext(path)[1].lower()
    label = os.path.basename(path)
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        print(f"[跳过] {label}: 疑似二进制文件")
        return "skip"
    try:
        text, enc = decode_bytes(raw)
    except Exception:
        print(f"[跳过] {label}: 无法识别的编码")
        return "skip"
    if ext in CODE_EXTS and not ctx.force:
        print(f"[跳过] {label}: 代码/配置类文件, 如确要翻译请加 --force")
        return "skip"

    if ext in (".srt", ".vtt"):
        mode = "srt"
    elif ext == ".json":
        mode = "json"
    elif ext == ".lrc":
        mode = "lrc"
    else:
        mode = "plain"

    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    if ctx.verbose:
        print(f"[处理] {path} (编码 {enc}, 模式 {mode}, {len(text)} 字符)")

    try:
        if mode == "json":
            new_text, samples = process_json(text, ctx)
        else:
            new_text, samples = process_text(text, mode, ext, ctx)
    except SystemExit:
        raise
    except Exception as exc:
        eprint(f"[失败] {path}: {exc}")
        return "fail"

    if crlf:
        new_text = new_text.replace("\n", "\r\n")

    if new_text == text:
        print(f"[无变化] {path}")
        return "same"

    for old, new in samples:
        old = re.sub(r"\s+", " ", old.strip())[:80]
        new = re.sub(r"\s+", " ", new.strip())[:80]
        print(f"  原文: {old}\n  译文: {new}")

    if ctx.dry_run:
        print(f"[预览] {path} (dry-run, 未写入)")
        return "dry"

    if ctx.out:
        target_path = ctx.out
        with open(target_path, "wb") as f:
            f.write(encode_text(new_text, enc))
        print(f"[完成] {path} -> 已写入 {target_path}")
    else:
        if ctx.backup:
            shutil.copy2(path, path + ".bak")
            print(f"  已备份到 {path}.bak")
        with open(path, "wb") as f:
            f.write(encode_text(new_text, enc))
        print(f"[完成] {path} -> 已覆盖")
    return "ok"


# ---------------------------------------------------------------- 主入口

def collect_files(paths, exts, recursive):
    files = []
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            if recursive:
                for root, _dirs, names in os.walk(p):
                    for n in sorted(names):
                        if os.path.splitext(n)[1].lower() in exts:
                            files.append(os.path.join(root, n))
            else:
                for n in sorted(os.listdir(p)):
                    fp = os.path.join(p, n)
                    if os.path.isfile(fp) and os.path.splitext(n)[1].lower() in exts:
                        files.append(fp)
        else:
            raise SystemExit(f"[错误] 路径不存在: {p}")
    return list(dict.fromkeys(files))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        prog="translator.py",
        description="原地翻译: 把文件文本替换成目标语言并覆盖原文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python translator.py novel.txt -t zh-CN              翻译单个文件并覆盖
  python translator.py . -r -t en --backup             目录递归翻译, 保留 .bak 备份
  python translator.py movie.srt -t zh-CN --dry-run    只试翻预览, 不写文件
  python translator.py strings.json -t en -e deepl     指定引擎翻译 JSON
  python translator.py a.txt -t ja --out a.ja.txt      译文写到新文件
""")
    ap.add_argument("paths", nargs="+", help="文件或目录(可多个)")
    ap.add_argument("-t", "--target",
                    help="目标语言, 如 zh-CN / zh-TW / en / ja / ko / ru / fr / de / es")
    ap.add_argument("-s", "--source", default="auto", help="源语言, 默认 auto 自动检测")
    ap.add_argument("-e", "--engine", choices=["bing", "google", "deepl", "openai"],
                    help="翻译引擎, 默认取 config.json 或 bing")
    ap.add_argument("--proxy", help="代理地址, 如 http://127.0.0.1:7890 (google 引擎需要)")
    ap.add_argument("--out", help="把译文写入新文件而不是覆盖(仅单个文件时可用)")
    ap.add_argument("--backup", action="store_true", help="覆盖前保留 .bak 备份")
    ap.add_argument("--dry-run", action="store_true", help="只试翻并预览, 不写文件")
    ap.add_argument("-r", "--recursive", action="store_true", help="目录模式下递归子目录")
    ap.add_argument("--ext", help="目录模式要处理的扩展名, 逗号分隔, 默认 txt,md,srt,vtt,json,lrc")
    ap.add_argument("--skip-keys", default="", help="JSON 模式跳过的键名, 逗号分隔")
    ap.add_argument("--force", action="store_true", help="强制处理代码/配置类文件(高风险)")
    ap.add_argument("--model", help="openai 引擎模型名")
    ap.add_argument("--chunk", type=int, help="每次请求的最大字符数, 默认 1000")
    ap.add_argument("--delay", type=float, help="每次请求的间隔秒数, 默认 0.3")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示详细信息")
    args = ap.parse_args()

    cfg = load_config()
    target = args.target or cfg.get("target")
    if not target:
        ap.error("请用 -t 指定目标语言, 例如: -t zh-CN")

    engine_name = args.engine or cfg.get("engine") or "bing"
    proxy = resolve_proxy(args.proxy, cfg)
    proxies = [proxy, ""] if proxy else [""]
    delay = args.delay if args.delay is not None else float(cfg.get("delay", 0.3))
    chunk = args.chunk if args.chunk is not None else int(cfg.get("chunk", 1000))

    http = Http(proxies, delay=delay)
    if engine_name == "bing":
        engine = BingEngine(http, args.source, target)
    elif engine_name == "google":
        engine = GoogleEngine(http, args.source, target)
    elif engine_name == "deepl":
        engine = DeeplEngine(http, args.source, target,
                             os.environ.get("DEEPL_API_KEY") or cfg.get("deepl_api_key") or "")
    else:
        engine = OpenAIEngine(http, args.source, target,
                              os.environ.get("OPENAI_BASE_URL") or cfg.get("openai_base"),
                              os.environ.get("OPENAI_API_KEY") or cfg.get("openai_key") or "",
                              args.model or os.environ.get("OPENAI_MODEL") or cfg.get("openai_model"))
    args.engine_obj = engine
    args.chunk = chunk

    if args.ext:
        exts = {"." + e.strip().lstrip(".").lower() for e in args.ext.split(",") if e.strip()}
    else:
        exts = set(DEFAULT_EXTS)

    files = collect_files(args.paths, exts, args.recursive)
    if not files:
        raise SystemExit("[错误] 没有找到可处理的文件 (目录模式可用 --ext 调整扩展名)")
    if args.out and len(files) != 1:
        raise SystemExit("[错误] --out 只支持单个文件")

    print(f"引擎 {engine_name} | 目标 {target} | 源 {args.source} | "
          f"代理 {proxy or '无'} | 文件数 {len(files)}")

    stats = {"ok": 0, "same": 0, "skip": 0, "fail": 0, "dry": 0}
    try:
        for fp in files:
            try:
                r = process_file(fp, args)
            except SystemExit:
                raise
            except Exception as exc:
                eprint(f"[失败] {fp}: {exc}")
                r = "fail"
            stats[r] = stats.get(r, 0) + 1
    except KeyboardInterrupt:
        eprint("\n[中断] 用户取消")
        sys.exit(130)

    print(f"\n汇总: 覆盖 {stats['ok']} / 无变化 {stats['same']} / "
          f"预览 {stats['dry']} / 跳过 {stats['skip']} / 失败 {stats['fail']} "
          f"(请求 {http.requests} 次)")
    sys.exit(1 if stats["fail"] else 0)


if __name__ == "__main__":
    main()
