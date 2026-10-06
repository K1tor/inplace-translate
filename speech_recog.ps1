param(
  [string]$Lang = "",
  [string]$Source = "mic",
  [int]$MaxSeconds = 0
)
# 用 Windows 自带 SAPI 语音识别引擎识别声音并输出, 协议:
#   LANG\t<语言>   初始化时
#   SRC\t<来源>    音频来源
#   TEXT\t<文本>   每识别出一句话
#   ERR\t<消息>    出错时
# -Source mic    默认录音设备(麦克风; 若把系统默认录音设备设为立体声混音则为系统声音)
# -Source system WASAPI 环回捕获: 直接抓取系统正在播放的声音(直播/游戏/视频), 无需任何设置
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Add-Type -AssemblyName System.Speech

$cs = @"
using System;
using System.Runtime.InteropServices;
using System.Threading;

namespace LiveCap {
  public class LoopbackStream : System.IO.Stream {
    private readonly System.Collections.Generic.Queue<byte[]> chunks = new System.Collections.Generic.Queue<byte[]>();
    private byte[] current = null;
    private int currentPos = 0;
    private long totalRead = 0;
    private readonly AutoResetEvent dataEvent = new AutoResetEvent(false);
    private readonly object gate = new object();
    private bool stopped = false;

    public void Push(byte[] data) {
      if (data == null || data.Length == 0) return;
      lock (gate) { chunks.Enqueue(data); }
      dataEvent.Set();
    }

    public void StopFeed() { stopped = true; dataEvent.Set(); }

    public override int Read(byte[] buffer, int offset, int count) {
      while (true) {
        lock (gate) {
          if (current == null && chunks.Count > 0) { current = chunks.Dequeue(); currentPos = 0; }
          if (current != null) {
            int n = Math.Min(count, current.Length - currentPos);
            Array.Copy(current, currentPos, buffer, offset, n);
            currentPos += n;
            totalRead += n;
            if (currentPos >= current.Length) { current = null; currentPos = 0; }
            return n;
          }
          if (stopped) return 0;
        }
        dataEvent.WaitOne(200);
        if (stopped) { lock (gate) { if (chunks.Count == 0) return 0; } }
      }
    }

    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return false; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return totalRead; } }
    public override long Position { get { return totalRead; } set { } }
    public override void Flush() { }
    public override long Seek(long o, System.IO.SeekOrigin s) { return totalRead; }
    public override void SetLength(long v) { }
    public override void Write(byte[] b, int o, int c) { }
  }

  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
  class MMDeviceEnumeratorComObject { }

  [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
  }

  [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDevice {
    int Activate(ref Guid iid, int clsContext, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
  }

  [Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioClient {
    int Initialize(int shareMode, int streamFlags, long bufferDuration, long periodicity, IntPtr format, IntPtr audioSessionGuid);
    int GetBufferSize(out uint bufferFrameCount);
    int GetStreamLatency(out long latency);
    int GetCurrentPadding(out int currentPadding);
    int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closestMatch);
    int GetMixFormat(out IntPtr format);
    int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
    int Start();
    int Stop();
    int Reset();
    int SetEventHandle(IntPtr eventHandle);
    int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
  }

  [Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioCaptureClient {
    int GetBuffer(out IntPtr data, out uint frames, out uint flags, out long devicePosition, out long qpcPosition);
    int ReleaseBuffer(uint framesWritten);
    int GetNextPacketSize(out uint frames);
  }

  public class LoopbackCapture {
    [DllImport("ole32.dll")]
    private static extern int CoInitializeEx(IntPtr pv, uint coInit);

    public static LoopbackStream Start(out string mixInfo) {
      LoopbackStream stream = new LoopbackStream();
      string err = null;
      string info = "";
      ManualResetEvent ready = new ManualResetEvent(false);
      Thread t = new Thread(delegate() {
        IAudioCaptureClient capture = null;
        double ratio = 1.0;
        int channels = 2;
        int rate = 48000;
        try {
          CoInitializeEx(IntPtr.Zero, 0);
          IMMDeviceEnumerator enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
          IMMDevice device;
          Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 0, out device));
          Guid iid = new Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2");
          object obj;
          Marshal.ThrowExceptionForHR(device.Activate(ref iid, 1, IntPtr.Zero, out obj));
          IAudioClient client = (IAudioClient)obj;
          IntPtr mixFormat;
          Marshal.ThrowExceptionForHR(client.GetMixFormat(out mixFormat));
          channels = Marshal.ReadInt16(mixFormat, 2);
          rate = Marshal.ReadInt32(mixFormat, 4);
          short bits = Marshal.ReadInt16(mixFormat, 14);
          if (bits != 32 || channels < 1) throw new Exception("unsupported mix format " + bits + "bit x" + channels);
          Marshal.ThrowExceptionForHR(client.Initialize(0, 0x00020000, 5000000, 0, mixFormat, IntPtr.Zero));
          Guid capIid = new Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317");
          object capObj;
          Marshal.ThrowExceptionForHR(client.GetService(ref capIid, out capObj));
          capture = (IAudioCaptureClient)capObj;
          Marshal.ThrowExceptionForHR(client.Start());
          ratio = (double)rate / 16000.0;
          info = rate + "Hz x" + channels;
        } catch (Exception ex) {
          err = ex.Message;
        }
        ready.Set();
        if (err != null) return;
        try {
          while (true) {
            uint frames;
            capture.GetNextPacketSize(out frames);
            while (frames > 0) {
              IntPtr dataPtr;
              uint flags;
              long devPos, qpcPos;
              Marshal.ThrowExceptionForHR(capture.GetBuffer(out dataPtr, out frames, out flags, out devPos, out qpcPos));
              int floatCount = (int)frames * (int)channels;
              float[] floats = new float[floatCount];
              Marshal.Copy(dataPtr, floats, 0, floatCount);
              int outFrames = (int)(frames * 16000.0 / rate);
              byte[] pcm = new byte[outFrames * 2];
              for (int i = 0; i < outFrames; i++) {
                int srcIdx = (int)(i * ratio) * (int)channels;
                double sum = 0;
                for (int c = 0; c < channels; c++) sum += floats[srcIdx + c];
                double v = sum / channels;
                if (v > 1) v = 1;
                if (v < -1) v = -1;
                short s = (short)(v * 32767);
                pcm[i * 2] = (byte)(s & 0xFF);
                pcm[i * 2 + 1] = (byte)((s >> 8) & 0xFF);
              }
              stream.Push(pcm);
              capture.ReleaseBuffer(frames);
              capture.GetNextPacketSize(out frames);
            }
            Thread.Sleep(8);
          }
        } catch { }
        stream.StopFeed();
      });
      t.IsBackground = true;
      t.Start();
      ready.WaitOne(8000);
      if (err != null) throw new Exception("环回捕获初始化失败: " + err);
      mixInfo = info;
      return stream;
    }
  }
}
"@
Add-Type -TypeDefinition $cs -Language CSharp

$all = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
$rec = $null
if ($Lang) {
  $rec = $all | Where-Object { $_.Culture.Name -like "$Lang*" } | Select-Object -First 1
}
if (-not $rec) {
  $rec = $all | Where-Object { $_.Culture.Name -like 'zh*' } | Select-Object -First 1
}
if (-not $rec) { $rec = $all | Select-Object -First 1 }
if (-not $rec) {
  Write-Output "ERR`tno speech recognizer installed"
  [Console]::Out.Flush(); exit 1
}
Write-Output ("LANG`t" + $rec.Culture.Name)

$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($rec)
if ($Source -eq "system") {
  try {
    $info = ""
    $stream = [LiveCap.LoopbackCapture]::Start([ref]$info)
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
      16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
      [System.Speech.AudioFormat.AudioChannel]::Mono)
    $engine.SetInputToAudioStream($stream, $fmt)
    Write-Output ("SRC`tsystem " + $info)
  } catch {
    Write-Output ("ERR`t" + $_.Exception.Message)
    [Console]::Out.Flush(); exit 1
  }
} else {
  $engine.SetInputToDefaultAudioDevice()
  Write-Output "SRC`tmic"
}
[Console]::Out.Flush()

$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$engine.EndSilenceTimeout = [TimeSpan]::FromMilliseconds(700)
$engine.EndSilenceTimeoutAmbiguous = [TimeSpan]::FromMilliseconds(900)

$events = New-Object System.Collections.ArrayList
$sync = [System.Collections.ArrayList]::Synchronized($events)
Register-ObjectEvent -InputObject $engine -EventName SpeechRecognized -Action {
  $r = $EventArgs.Result
  $t = $r.Text.Trim()
  if ($t.Length -ge 2 -and $r.Confidence -ge 0.55) {
    $sync.Add("TEXT`t" + ($t -replace '\s+', ' ')) | Out-Null
  }
} | Out-Null

$engine.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($true) {
  while ($sync.Count -gt 0) {
    $item = $sync[0]
    $sync.RemoveAt(0)
    Write-Output $item
    [Console]::Out.Flush()
  }
  if ($MaxSeconds -gt 0 -and $sw.Elapsed.TotalSeconds -gt $MaxSeconds) { break }
  Start-Sleep -Milliseconds 200
}
try { $engine.RecognizeAsyncCancel() } catch {}
