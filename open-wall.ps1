<#
    open-wall.ps1 - open the Grafana dashboard windows on Windows and place them.

    This is the Windows counterpart of clickthis.command, and it does LESS on purpose:
    it opens windows and positions them. There is no watchdog, no reload, no scroll -
    that half of the project is macOS-only (it drives Chrome through AppleScript,
    which does not exist on Windows).

        .\open-wall.ps1                 open and place the windows
        .\open-wall.ps1 -ShowScreens    print your monitor coordinates, change nothing
        .\open-wall.ps1 -DryRun         say what it would do, change nothing

    FIRST RUN: run -ShowScreens, then paste the coordinates you want into the
    $WallWindows block below. The defaults assume one 1920x1080 screen split in half.
#>

param(
    [switch]$ShowScreens,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- configuration

$DashboardUrl = 'https://grafana.client8.me/d/281e8816-ccb0-4335-922b-6b248491fd28/core-metrics-arms-aliyun?orgId=1&from=now-6h&to=now'

# One entry per window, opened and placed in this order. X/Y may be negative or
# larger than one screen - they are virtual-desktop coordinates, so a second monitor
# to the right of a 1920-wide primary starts at X=1920. Run -ShowScreens to find out.
# Add or remove entries freely; 'Match' is a fragment of the expected window title,
# used to pick the right new window when Chrome opens more than one.
$WallWindows = @(
    @{ Name = 'top';    Url = $DashboardUrl; X = 0;   Y = 0; W = 960; H = 1080; Match = 'Core Metrics' }
    @{ Name = 'bottom'; Url = $DashboardUrl; X = 960; Y = 0; W = 960; H = 1080; Match = 'Core Metrics' }
    # Add the other windows you want on the wall, for example:
    # @{ Name = 'online'; Url = 'https://grafana.client8.me/d/fe70d4bd-4729-471f-9ede-e981ad277963/online-number'; X = 1920; Y = 0; W = 960; H = 1080; Match = 'Online Number' }
)

$WindowWaitSeconds = 25

# ---------------------------------------------------------------- win32

if (-not ('Wall.Win32' -as [type])) {
    Add-Type -Namespace 'Wall' -Name 'Win32' -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Auto)]
public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr childAfter, string cls, string win);

[DllImport("user32.dll", CharSet = CharSet.Auto)]
public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder text, int count);

[DllImport("user32.dll")]
public static extern bool IsWindowVisible(IntPtr h);

[DllImport("user32.dll")]
public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int ht, uint flags);

[DllImport("user32.dll")]
public static extern bool ShowWindow(IntPtr h, int cmd);
'@
}

$SW_RESTORE = 9
$SWP_NOZORDER = 0x4
$SWP_NOACTIVATE = 0x10

function Get-ChromeWindows {
    # FindWindowEx with childAfter walks every top-level window of Chrome's class.
    # Deliberately avoids EnumWindows, which needs a callback delegate.
    $out = @()
    $h = [IntPtr]::Zero
    while (($h = [Wall.Win32]::FindWindowEx([IntPtr]::Zero, $h, 'Chrome_WidgetWin_1', $null)) -ne [IntPtr]::Zero) {
        if (-not [Wall.Win32]::IsWindowVisible($h)) { continue }
        $sb = New-Object System.Text.StringBuilder 512
        [void][Wall.Win32]::GetWindowText($h, $sb, $sb.Capacity)
        $title = $sb.ToString()
        if ([string]::IsNullOrWhiteSpace($title)) { continue }
        $out += [pscustomobject]@{ Handle = $h; Title = $title }
    }
    return $out
}

function Find-Chrome {
    # Any of these roots can be unset (32-bit hosts have no ProgramFiles(x86)),
    # and Join-Path on a null root throws while ErrorActionPreference is 'Stop'.
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LocalAppData) |
        Where-Object { $_ }
    foreach ($r in $roots) {
        $p = Join-Path $r 'Google\Chrome\Application\chrome.exe'
        if (Test-Path $p) { return $p }
    }
    $cmd = Get-Command 'chrome.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# ---------------------------------------------------------------- -ShowScreens

if ($ShowScreens) {
    Add-Type -AssemblyName System.Windows.Forms
    Write-Host ''
    Write-Host 'Your monitors, in virtual-desktop coordinates:' -ForegroundColor Cyan
    foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
        $b = $s.Bounds
        $tag = if ($s.Primary) { ' (primary)' } else { '' }
        Write-Host ("   {0,-14} X={1,-6} Y={2,-6} {3}x{4}{5}" -f $s.DeviceName, $b.X, $b.Y, $b.Width, $b.Height, $tag)
    }
    Write-Host ''
    Write-Host 'Use those X/Y/width/height in the $WallWindows block of this script.'
    Write-Host 'A window filling the second monitor above would be X=<its X>, Y=<its Y>.'
    Write-Host ''
    return
}

# ---------------------------------------------------------------- open + place

Write-Host ''
Write-Host 'Grafana dashboard wall - opening windows' -ForegroundColor Cyan

$chrome = Find-Chrome
if (-not $chrome) {
    Write-Host '   FAIL  Could not find chrome.exe.' -ForegroundColor Red
    Write-Host '         Install Google Chrome, or set its full path in Find-Chrome.'
    exit 1
}
Write-Host "   chrome: $chrome"

$claimed = @()
foreach ($w in (Get-ChromeWindows)) { $claimed += $w.Handle }
Write-Host ("   chrome windows already open: {0}" -f $claimed.Count)

$placed = 0
foreach ($spec in $WallWindows) {
    Write-Host ''
    Write-Host ("-> {0}: {1},{2} {3}x{4}" -f $spec.Name, $spec.X, $spec.Y, $spec.W, $spec.H)

    if ($DryRun) {
        Write-Host ("   dry-run: would open {0}" -f $spec.Url)
        continue
    }

    Start-Process -FilePath $chrome -ArgumentList @(
        '--new-window',
        ("--window-position={0},{1}" -f $spec.X, $spec.Y),
        ("--window-size={0},{1}" -f $spec.W, $spec.H),
        $spec.Url
    ) | Out-Null

    # Wait for a window we have not already claimed. Chrome reuses one process for
    # every window, so identify the new one by handle, not by process.
    $deadline = (Get-Date).AddSeconds($WindowWaitSeconds)
    $target = $null
    do {
        Start-Sleep -Milliseconds 400
        $fresh = @(Get-ChromeWindows | Where-Object { $claimed -notcontains $_.Handle })
        if ($fresh.Count -gt 0) {
            $match = @($fresh | Where-Object { $spec.Match -and $_.Title -like "*$($spec.Match)*" })
            if ($match.Count -gt 0) { $target = $match[0] }
            elseif ((Get-Date) -gt $deadline.AddSeconds(-8)) { $target = $fresh[0] }
        }
    } while (-not $target -and (Get-Date) -lt $deadline)

    if (-not $target) {
        Write-Host '   FAIL  no new Chrome window appeared' -ForegroundColor Red
        continue
    }

    $claimed += $target.Handle
    [void][Wall.Win32]::ShowWindow($target.Handle, $SW_RESTORE)   # a maximized window ignores SetWindowPos
    Start-Sleep -Milliseconds 150
    $moved = [Wall.Win32]::SetWindowPos($target.Handle, [IntPtr]::Zero,
        [int]$spec.X, [int]$spec.Y, [int]$spec.W, [int]$spec.H,
        ($SWP_NOZORDER -bor $SWP_NOACTIVATE))

    if ($moved) {
        $placed++
        Write-Host ("   ok    placed - {0}" -f $target.Title) -ForegroundColor Green
    } else {
        Write-Host '   FAIL  SetWindowPos refused to move the window' -ForegroundColor Red
    }
}

Write-Host ''
if ($DryRun) {
    Write-Host ("dry run: {0} window(s) configured, nothing changed." -f $WallWindows.Count)
} else {
    Write-Host ("Done: {0} of {1} window(s) placed." -f $placed, $WallWindows.Count)
    if ($placed -lt $WallWindows.Count) {
        Write-Host 'If a window landed on the wrong screen, run -ShowScreens and check the X/Y'
        Write-Host 'values in the $WallWindows block against your real monitor coordinates.'
    }
}
Write-Host ''
