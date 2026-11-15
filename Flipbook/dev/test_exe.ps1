<#
  End-to-end test of a BUILT Flipbook.exe (run after build.bat, before releasing).

    powershell -ExecutionPolicy Bypass -File Flipbook\dev\test_exe.ps1 [-Exe <path>] [-ModelDir <dir>] [-SkipDictionary]

  -Exe       the exe to test (default: Flipbook\Flipbook.exe). Tip: build to a separate folder
             (..\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath _build\dist --workpath _build Flipbook.spec)
             and test a copy of it in a scratch folder, with $env:LOCALAPPDATA set to a scratch folder
             for the call (its own WebView profile), so the user's running Flipbook is never touched.
  -ModelDir  folder holding kokoro-v1.0.fp16.onnx + voices-v1.0.bin to test the AI voice
             without downloading 206 MB (e.g. a release folder's addons\voice). Skipped if absent.
  -GpuSrc    path to kokoro-v1.0.onnx (fp32, 325,532,387 bytes) to test the graphics-card path without
             the 326 MB download: the exe converts it, self-tests the GPU, and each mode (gpu/cpu/auto)
             makes one sentence (X-Voice-Device shows where). Needs -ModelDir too.
  Checks: version, Dictionary add-on download+build+lookups+remove (real 40 MB download),
  AI voice synthesis at 1x and 2x (X-Playback-Rate), stale-request refusal (409), voice remove,
  clean shutdown, no error.log. Add-ons are created next to the tested exe and removed again.
  The exe's window opens briefly; it is closed at the end.
#>
param(
  [string]$Exe = (Join-Path (Split-Path $PSScriptRoot -Parent) "Flipbook.exe"),
  [string]$ModelDir = "",
  [string]$GpuSrc = "",
  [switch]$SkipDictionary
)
$ErrorActionPreference = "Stop"
$dist = Split-Path (Resolve-Path $Exe) -Parent
if (Test-Path "$dist\addons") { Write-Warning "$dist\addons already exists - this test adds/removes add-ons there. Use a separate build folder."; exit 1 }
$before = @((Get-Process Flipbook -ErrorAction SilentlyContinue).Id)
Start-Process -FilePath $Exe
Start-Sleep 10
$ids = (Get-Process Flipbook | Where-Object { $before -notcontains $_.Id }).Id
$port = (Get-NetTCPConnection -State Listen -OwningProcess $ids | Select-Object -First 1).LocalPort
$b = "http://127.0.0.1:$port"; $h = @{ "X-Flipbook" = "1" }
"exe: $Exe"
"port $port  info: " + (Invoke-WebRequest -UseBasicParsing "$b/api/info").Content

if (-not $SkipDictionary) {
  Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h "$b/api/dict/install" | Out-Null
  $t0 = Get-Date
  do { Start-Sleep 3; $st = ((Invoke-WebRequest -UseBasicParsing "$b/api/dict").Content | ConvertFrom-Json) } while ($st.download.state -in @("downloading","verifying","building") -and ((Get-Date) - $t0).TotalSeconds -lt 300)
  "dictionary: state=$($st.download.state) installed=$($st.installed) in $([int]((Get-Date) - $t0).TotalSeconds)s error=$($st.download.error)"
  foreach ($w in "travelled","efiect","candidate") {
    $d = (Invoke-WebRequest -UseBasicParsing "$b/api/dict/lookup?w=$w").Content | ConvertFrom-Json
    "  $w -> word=$($d.word) lemma_of=$($d.lemma_of) sug=$($d.suggestions -join ',') etym=$((@($d.webster | Where-Object etym))[0].etym)"
  }
  Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h "$b/api/dict/remove" | Out-Null
  "  after remove: folder exists=" + (Test-Path "$dist\addons\dictionary")
}

if ($ModelDir -and (Test-Path "$ModelDir\kokoro-v1.0.fp16.onnx")) {
  New-Item -ItemType Directory -Force "$dist\addons\voice" | Out-Null
  Copy-Item "$ModelDir\kokoro-v1.0.fp16.onnx","$ModelDir\voices-v1.0.bin" "$dist\addons\voice\"
  $gen = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
  foreach ($sp in 1.0, 2.0) {
    $body = @{ text = "This had a great effect on the town."; voice = "af_heart"; speed = $sp; gen = $gen; prio = "now" } | ConvertTo-Json
    $r = Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h -ContentType "application/json" -Body $body "$b/api/tts"
    "voice @${sp}x: status $($r.StatusCode) $($r.Headers['Content-Type']) rate=$($r.Headers['X-Playback-Rate']) bytes=$($r.RawContentLength)  (expect rate 1.0 and 1.538)"
  }
  $old = @{ text = "old"; voice = "af_heart"; speed = 1; gen = $gen - 5; prio = "ahead" } | ConvertTo-Json
  try { Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h -ContentType "application/json" -Body $old "$b/api/tts" | Out-Null; "stale request: NOT refused (BUG)" } catch { "stale request refused: HTTP $($_.Exception.Response.StatusCode.value__) (expect 409)" }
  if ($GpuSrc -and (Test-Path $GpuSrc)) {
    # graphics card: the fp32 model is already there (skips the 326 MB download), so the exe converts it itself
    New-Item -ItemType Directory -Force "$dist\addons\voice\gpu" | Out-Null
    Copy-Item $GpuSrc "$dist\addons\voice\gpu\"
    Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h "$b/api/voice/gpu/install" | Out-Null
    $t0 = Get-Date
    do { Start-Sleep 2; $v = ((Invoke-WebRequest -UseBasicParsing "$b/api/voice").Content | ConvertFrom-Json) } while (($v.device.gpu_model.download.state -in @("downloading","verifying","converting") -or $v.device.testing) -and ((Get-Date) - $t0).TotalSeconds -lt 300)
    "gpu: card=$($v.device.gpu.name) ok=$($v.device.gpu.ok) model=$($v.device.gpu_model.installed) active=$($v.device.active) speedup=$($v.device.probe.speedup) mel=$($v.device.probe.mel_db)  (expect active gpu where the card can do it)"
    "     note: $($v.device.note)"
    foreach ($m in "gpu", "cpu", "auto") {
      Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h -ContentType "application/json" -Body (@{ mode = $m } | ConvertTo-Json) "$b/api/voice/device" | Out-Null
      $gen++
      $body = @{ text = "The graphics card reads this sentence."; voice = "bm_george"; speed = 1.0; gen = $gen; prio = "now" } | ConvertTo-Json
      $t1 = Get-Date
      $r = Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h -ContentType "application/json" -Body $body "$b/api/tts"
      "  mode $m -> made on $($r.Headers['X-Voice-Device']) in $([int]((Get-Date) - $t1).TotalMilliseconds) ms, bytes=$($r.RawContentLength)"
    }
  } else { "graphics-card test skipped (no -GpuSrc kokoro-v1.0.onnx)" }
  Invoke-WebRequest -UseBasicParsing -Method POST -Headers $h "$b/api/voice/remove" | Out-Null
  "voice removed: folder exists=" + (Test-Path "$dist\addons\voice")
} else { "AI voice test skipped (no -ModelDir with the model files)" }

(Get-Process -Id $ids | Where-Object MainWindowTitle).CloseMainWindow() | Out-Null
Start-Sleep 3
"left running: " + @(Get-Process -Id $ids -ErrorAction SilentlyContinue).Count + " (expect 0)"
$helpers = @(Get-CimInstance Win32_Process -Filter "Name = 'Flipbook.exe'" | Where-Object { $_.CommandLine -like "*--voice-worker*" -and $_.CommandLine -like "*$dist*" })
"graphics-card helpers left running: $($helpers.Count) (expect 0)"
if (Test-Path "$dist\addons") { Remove-Item -Recurse -Force "$dist\addons" }
if (Test-Path "$dist\flipbook-data\voice-device.json") { Remove-Item -Force "$dist\flipbook-data\voice-device.json" }
"error.log present: " + (Test-Path "$env:LOCALAPPDATA\Flipbook\error.log") + " (expect False)"
