# Starts LawKitty, waits for its window, and saves a picture of the window's client area.
#   pwsh shot.ps1 -Exe <LawKitty.exe> -Out <file.png> [-AppArgs '--open','itaa-1997/8-1'] [-Wait 1500]
# The window is put at 1440 by 900 (client area) so it lines up with the web reference.
param(
    [Parameter(Mandatory)] [string]$Exe,
    [Parameter(Mandatory)] [string]$Out,
    [string[]]$AppArgs = @(),
    [int]$Wait = 1500,
    [int]$Width = 1440,
    [int]$Height = 900
)
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class Win {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
}
'@
[void][Win]::SetProcessDPIAware()
$p = Start-Process -FilePath $Exe -ArgumentList $AppArgs -PassThru
$sw = [Diagnostics.Stopwatch]::StartNew()
# The first window a GL program opens can be a throwaway one: wait for one with a real client area.
$c = New-Object Win+RECT
$h = [IntPtr]::Zero
while ($sw.ElapsedMilliseconds -lt 15000) {
    $p.Refresh()
    $h = $p.MainWindowHandle
    if ($h -ne 0 -and [Win]::IsWindowVisible($h) -and [Win]::GetClientRect($h, [ref]$c) -and $c.R -gt 100) { break }
    Start-Sleep -Milliseconds 10
}
$windowMs = $sw.ElapsedMilliseconds
if ($h -eq 0 -or $c.R -le 100) { Write-Error 'no window'; if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }; exit 1 }
# Size the window so its client area is exactly Width x Height, at the top left of the screen.
$w = New-Object Win+RECT
[void][Win]::GetClientRect($h, [ref]$c); [void][Win]::GetWindowRect($h, [ref]$w)
$dx = ($w.R - $w.L) - $c.R; $dy = ($w.B - $w.T) - $c.B
[void][Win]::SetWindowPos($h, [IntPtr](-1), 0, 0, $Width + $dx, $Height + $dy, 0x0040)
[void][Win]::SetForegroundWindow($h)
Start-Sleep -Milliseconds $Wait
[void][Win]::GetClientRect($h, [ref]$c)
$o = New-Object Win+POINT
[void][Win]::ClientToScreen($h, [ref]$o)
# Keep the pointer off the picture.
[void][Win]::SetCursorPos($o.X + $c.R + 40, $o.Y + $c.B + 40)
Start-Sleep -Milliseconds 250
$bmp = New-Object System.Drawing.Bitmap $c.R, $c.B
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($o.X, $o.Y, 0, 0, $bmp.Size)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Stop-Process -Id $p.Id -Force
"window after $windowMs ms; saved $($c.R)x$($c.B) to $Out"
