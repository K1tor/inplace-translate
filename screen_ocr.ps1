param(
  [string]$ImagePath = "",
  [int]$Hwnd = 0,
  [int]$Left = 0, [int]$Top = 0, [int]$Width = 0, [int]$Height = 0,
  [int]$IntervalMs = 1200,
  [string]$Lang = "",
  [int]$MaxSeconds = 0
)
# 用 Windows 自带 OCR 引擎识别屏幕区域 / 指定窗口 / 图片文件, 输出协议:
#   LANG\t<语言>   初始化时
#   TEXT\t<文本>   文本有变化时
#   ERR\t<消息>    出错时
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class DpiOcr { [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int v); }'
try { [DpiOcr]::SetProcessDpiAwareness(2) | Out-Null } catch {}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public struct RECT { public int Left, Top, Right, Bottom; }
public class Win32Win {
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT rect);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT rect, int size);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
}
'@

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
  $netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}

function New-OcrEngine([string]$lang) {
  if ($lang) {
    try {
      $l = New-Object Windows.Globalization.Language($lang)
      $e = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($l)
      if ($e) { return $e }
    } catch {}
  }
  # 自动模式: 中文系统优先中文引擎, 否则用第一个可用语言
  $avail = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
  $zh = $avail | Where-Object { $_.LanguageTag -like 'zh*' } | Select-Object -First 1
  foreach ($l in @($zh) + $avail) {
    if ($l) {
      $e = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($l)
      if ($e) { return $e }
    }
  }
  return [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}

$engine = New-OcrEngine $Lang
if (-not $engine) {
  Write-Output "ERR`tno OCR language pack installed"
  [Console]::Out.Flush(); exit 1
}
Write-Output ("LANG`t" + $engine.RecognizerLanguage.LanguageTag)
[Console]::Out.Flush()

function Get-OcrText($engine, $stream) {
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $soft = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($soft)) ([Windows.Media.Ocr.OcrResult])
  return (($result.Lines | ForEach-Object { $_.Text }) -join "`n")
}

if ($ImagePath) {
  try {
    $bytes = [System.IO.File]::ReadAllBytes($ImagePath)
    $ms = New-Object System.IO.MemoryStream (,$bytes)
    $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
    $text = Get-OcrText $engine $ras
    Write-Output ("TEXT`t" + ($text -replace '\s+', ' ').Trim())
  } catch { Write-Output ("ERR`t" + $_.Exception.Message) }
  [Console]::Out.Flush(); exit 0
}

# ---- 指定窗口模式: 跟随窗口位置, PrintWindow 抓取(被遮挡也能抓), 最小化时跳过 ----
if ($Hwnd -ne 0) {
  $last = ""
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($true) {
    try {
      if (-not [Win32Win]::IsWindow([IntPtr]$Hwnd)) {
        Write-Output "ERR`twindow closed"
        [Console]::Out.Flush(); break
      }
      if ([Win32Win]::IsIconic([IntPtr]$Hwnd)) {
        Start-Sleep -Milliseconds $IntervalMs
        if ($MaxSeconds -gt 0 -and $sw.Elapsed.TotalSeconds -gt $MaxSeconds) { break }
        continue
      }
      $rect = New-Object RECT
      if ([Win32Win]::DwmGetWindowAttribute([IntPtr]$Hwnd, 9, [ref]$rect, 16) -ne 0) {
        [Win32Win]::GetWindowRect([IntPtr]$Hwnd, [ref]$rect) | Out-Null
      }
      $w = $rect.Right - $rect.Left
      $h = $rect.Bottom - $rect.Top
      if ($w -ge 30 -and $h -ge 20) {
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        try {
          $ok = $false
          $hdc = $g.GetHdc()
          try { $ok = [Win32Win]::PrintWindow([IntPtr]$Hwnd, $hdc, 2) } finally { $g.ReleaseHdc($hdc) }
          $c1 = $bmp.GetPixel(4, 4)
          $c2 = $bmp.GetPixel($w - 5, $h - 5)
          $c3 = $bmp.GetPixel([int]($w / 2), [int]($h / 2))
          if (-not $ok -or ($c1.Equals($c2) -and $c2.Equals($c3))) {
            # PrintWindow 失败(全黑)时退回屏幕抓取
            $g.CopyFromScreen($rect.Left, $rect.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))
          }
          $ms = New-Object System.IO.MemoryStream
          $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
          $ms.Position = 0
          $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
          $text = Get-OcrText $engine $ras
          $norm = ($text -replace '\s+', ' ').Trim()
          if ($norm.Length -ge 2 -and $norm -ne $last) {
            $last = $norm
            Write-Output ("TEXT`t" + $norm)
            [Console]::Out.Flush()
          }
          $ms.Dispose()
        } finally { $g.Dispose(); $bmp.Dispose() }
      }
    } catch {
      Write-Output ("ERR`t" + $_.Exception.Message)
      [Console]::Out.Flush()
    }
    if ($MaxSeconds -gt 0 -and $sw.Elapsed.TotalSeconds -gt $MaxSeconds) { break }
    Start-Sleep -Milliseconds $IntervalMs
  }
  exit 0
}

if ($Width -le 0 -or $Height -le 0) {
  Write-Output "ERR`tbad region"; [Console]::Out.Flush(); exit 1
}

$bmp = New-Object System.Drawing.Bitmap($Width, $Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$last = ""
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($true) {
  try {
    $g.CopyFromScreen($Left, $Top, 0, 0, (New-Object System.Drawing.Size($Width, $Height)))
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $ms.Position = 0
    $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
    $text = Get-OcrText $engine $ras
    $norm = ($text -replace '\s+', ' ').Trim()
    if ($norm.Length -ge 2 -and $norm -ne $last) {
      $last = $norm
      Write-Output ("TEXT`t" + $norm)
      [Console]::Out.Flush()
    }
    $ms.Dispose()
  } catch {
    Write-Output ("ERR`t" + $_.Exception.Message)
    [Console]::Out.Flush()
  }
  if ($MaxSeconds -gt 0 -and $sw.Elapsed.TotalSeconds -gt $MaxSeconds) { break }
  Start-Sleep -Milliseconds $IntervalMs
}
