# InPlace Translate —— 原地翻译

把文件里的文本直接翻译成目标语言，并**覆盖写回原文件**。

- 免安装依赖：纯 Python 标准库（Python 3.8+）
- 默认引擎：微软必应翻译，**国内直连、无需密钥、无需代理**
- 备选引擎：Google（需代理）、DeepL（免费密钥）、OpenAI 兼容接口
- 支持格式：`.txt` `.md` `.srt` `.vtt` `.lrc` `.json`，其他文本文件也可强制处理
- 保留原文件的编码（UTF-8 / GBK / Big5 自动识别）与换行风格（CRLF / LF）

## 快速开始

```bash
# 翻译单个文件并覆盖（目标语言必须指定）
python translator.py 小说.txt -t zh-CN

# 只试翻预览，不写文件（先看看效果）
python translator.py 小说.txt -t zh-CN --dry-run

# 覆盖前留一份 .bak 备份
python translator.py 小说.txt -t zh-CN --backup

# 译文写到新文件，原文件不动
python translator.py novel.txt -t zh-CN --out novel.zh.txt
```

也可以直接把文件**拖到 `translate.bat` 图标上**，按提示输入目标语言即可。

## 悬浮窗实时翻译

```bash
python floating.py       # 或双击 悬浮窗翻译.bat（无控制台窗口）
```

全自动检测要翻译的内容，无需手动输入：

- **窗口监控**：勾选"窗口"并从列表中选择任意程序——浏览器、**游戏**、播放器都可以（支持搜索；无标题的窗口按进程名显示）。每秒自动识别该窗口内的文字（Windows 自带 OCR），只翻译有变化的部分。窗口移动、切换都能跟随；被遮挡的窗口也能抓取（PrintWindow）
- **声音翻译**（两个声音源，可同时开）：
  - ☑ **声音**：识别**麦克风**，翻译你说的话
  - ☑ **直播**：识别**系统正在播放的声音**（直播/游戏语音/视频配音），通过 WASAPI 环回直接抓取播放音频流，无需任何 Windows 设置
- **剪贴板监控**：复制任意文本（Ctrl+C）自动翻译
- 输入框仅作备用：打字/粘贴也会实时翻译（0.6 秒防抖）

窗口操作：拖标题栏移动位置；点 `▾` 收起成小条；`Esc`（焦点不在输入框时）或 `✕` 关闭。底部可切换目标语言（切换后立即重翻当前内容）；点"复制"一键复制译文。

命令行参数：

```bash
python floating.py --window Edge                 # 按标题/进程名关键字直接选窗口
python floating.py --region 100,600,640,120      # 改为监控固定屏幕区域(左,上,宽,高)
python floating.py --voice                       # 启动时同时开启麦克风识别
python floating.py --live                        # 启动时同时开启系统声音识别
```

> 提示：
> - OCR 识别语言默认自动（中文系统优先中文引擎），`config.json` 的 `ocr_lang` 可指定；语音识别语言 `voice_lang` 可指定（默认中文普通话）
> - **独占全屏**运行的游戏建议改为"无边框窗口"模式，抓取更稳定；游戏窗口正常出现在选择列表里（按进程名查找）
> - 整窗 OCR 对小字号界面有识别噪声；语音识别质量取决于发音清晰度，低置信度结果已自动过滤
> - 监控子进程随主窗口关闭自动退出，不会残留

## 常用参数

| 参数 | 说明 |
|---|---|
| `-t, --target` | 目标语言：`zh-CN` `zh-TW` `en` `ja` `ko` `ru` `fr` `de` `es` 等（必填） |
| `-s, --source` | 源语言，默认 `auto` 自动检测 |
| `-e, --engine` | 引擎：`bing`（默认）/ `google` / `deepl` / `openai` |
| `--dry-run` | 只试翻并预览，不写文件 |
| `--backup` | 覆盖前把原文件复制为 `原名.bak` |
| `--out FILE` | 译文写入新文件（仅单文件时可用） |
| `-r` | 目录模式递归子目录 |
| `--ext` | 目录模式处理的扩展名，默认 `txt,md,srt,vtt,json,lrc` |
| `--skip-keys` | JSON 模式跳过的键名，逗号分隔 |
| `--delay` | 每次请求间隔秒数（默认 0.3，被限流时调大） |
| `--force` | 强制处理代码/配置类文件（高风险，会把代码也翻译掉） |

## 目录批量翻译

```bash
# 翻译 subtitles 目录下所有字幕（不递归）
python translator.py subtitles -t zh-CN

# 递归整个项目目录，只处理 txt 和 srt，每份留备份
python translator.py . -r -t zh-CN --ext txt,srt --backup
```

## 各格式的处理方式

- **纯文本**（`.txt` 等）：整篇按段落/句子切块翻译后拼回，换行原样保留。
- **Markdown**（`.md`）：代码围栏（``` 块）原样保留不翻译，其余正文翻译。
- **SRT / VTT 字幕**：只翻译字幕文本行，序号、时间轴原样保留。
- **LRC 歌词**：`[00:12.00]` 时间标签保留，只翻译后面的歌词。
- **JSON**：只翻译字符串**值**（键名、数字、URL、纯符号跳过）；注意缩进会重排为 2 空格。

## 引擎与代理

默认 `bing` 引擎直连使用，无需任何配置。切换引擎编辑 `config.json`：

```json
{
  "engine": "google",
  "proxy": "http://127.0.0.1:7890",
  "deepl_api_key": "你的免费密钥(以 :fx 结尾)",
  "openai_base": "https://api.openai.com/v1",
  "openai_key": "sk-...",
  "openai_model": "gpt-4o-mini"
}
```

- `google` 引擎在国内需要代理（`config.json` 的 `proxy` 或环境变量 `HTTPS_PROXY`）；代理失败会自动尝试直连。
- `deepl` 免费 API 密钥在 [deepl.com/pro-api](https://www.deepl.com/pro-api) 注册获取。
- `openai` 引擎兼容任何 OpenAI 格式接口（中转站也可以，改 `openai_base` 即可）。

密钥也可以用环境变量：`DEEPL_API_KEY`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。

## 注意事项

1. **覆盖不可恢复**：默认直接覆盖原文件。重要文件请先加 `--backup`，或先 `--dry-run` 预览。
2. 翻译质量取决于引擎；整篇机器翻译后语句可能不通顺，正式发布前请人工校对。
3. `.py` `.js` 等代码文件默认拒绝处理（会把代码翻译坏），确要翻译整篇请加 `--force`。
4. Bing 免费接口请求频繁时可能被限流（返回 401/验证码），可加大 `--delay` 或稍后再试；工具遇到 token 过期会自动重取一次。
5. JSON 文件会被重新格式化（2 空格缩进）；如需保留原始排版请勿用本工具处理 JSON。
