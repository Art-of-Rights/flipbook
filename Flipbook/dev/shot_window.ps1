<#
  Window and icon check for a built Flipbook.exe (v1.8.4): saves the exe's 16 and 32 px icons as
  Windows extracts them, then starts the exe with its own profile (LOCALAPPDATA -> <Out>\lad),
  captures the whole window with PrintWindow (window.png: the Home screen) and the title bar
  enlarged 3x (titlebar-x3.png), and closes only that process.

    powershell -ExecutionPolicy Bypass -File Flipbook\dev\shot_window.ps1 -Exe <scratch>\Flipbook.exe -Out <scratch>\shots
#>
param([string]$Exe, [string]$Out)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public class W32 {
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("shell32.dll", CharSet=CharSet.Unicode)] public static extern uint ExtractIconEx(string f, int i, IntPtr[] large, IntPtr[] small, uint n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@
New-Item -ItemType Directory -Force $Out | Out-Null
$lg = New-Object IntPtr[] 1; $sm = New-Object IntPtr[] 1
[void][W32]::ExtractIconEx($Exe, 0, $lg, $sm, 1)
[System.Drawing.Icon]::FromHandle($lg[0]).ToBitmap().Save("$Out\exe-icon-32.png")
[System.Drawing.Icon]::FromHandle($sm[0]).ToBitmap().Save("$Out\exe-icon-16.png")
$before = @((Get-Process Flipbook -ErrorAction SilentlyContinue).Id)
$real = $env:LOCALAPPDATA; $env:LOCALAPPDATA = Join-Path $Out "lad"
Start-Process -FilePath $Exe
$env:LOCALAPPDATA = $real
Start-Sleep 12
$p = Get-Process Flipbook | Where-Object { $before -notcontains $_.Id -and $_.MainWindowHandle -ne 0 } | Select-Object -First 1
$r = New-Object W32+RECT; [void][W32]::GetWindowRect($p.MainWindowHandle, [ref]$r)
$w = $r.R - $r.L; $h = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp); $dc = $g.GetHdc()
[void][W32]::PrintWindow($p.MainWindowHandle, $dc, 2); $g.ReleaseHdc($dc); $g.Dispose()
$bmp.Save("$Out\window.png")
$crop = $bmp.Clone((New-Object System.Drawing.Rectangle 0, 0, ([Math]::Min(420, $w)), ([Math]::Min(70, $h))), $bmp.PixelFormat)
$big = New-Object System.Drawing.Bitmap ($crop.Width * 3), ($crop.Height * 3)
$g2 = [System.Drawing.Graphics]::FromImage($big); $g2.InterpolationMode = "NearestNeighbor"; $g2.PixelOffsetMode = "Half"
$g2.DrawImage($crop, 0, 0, $big.Width, $big.Height); $g2.Dispose()
$big.Save("$Out\titlebar-x3.png")
"window: $w x $h  title='$($p.MainWindowTitle)'"
$ids = @((Get-Process Flipbook | Where-Object { $before -notcontains $_.Id }).Id)
(Get-Process -Id $ids | Where-Object MainWindowTitle).CloseMainWindow() | Out-Null
Start-Sleep 4; Get-Process -Id $ids -ErrorAction SilentlyContinue | Stop-Process -Force
"left: " + @(Get-Process -Id $ids -ErrorAction SilentlyContinue).Count
