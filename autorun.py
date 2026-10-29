"""Install/remove the weekly refresh (Windows Task Scheduler) and the
start-up shortcut (Startup folder). Both run under your user, no admin needed."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASK = "FlyerDeals Weekly Refresh"
SHORTCUT = "Flyer Deals.lnk"


def pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def ps(script):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def q(s):
    """Single-quote a value for PowerShell."""
    return "'" + str(s).replace("'", "''") + "'"


def apply(cfg):
    """Make Windows match the schedule settings. cfg values are already validated."""
    script = ROOT / "grocery.py"
    if cfg["weekly_refresh"]:
        ps(f"""
$a = New-ScheduledTaskAction -Execute {q(pythonw())} -Argument {q(f'"{script}" --auto')} -WorkingDirectory {q(ROOT)}
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {cfg['refresh_day']} -At {q(cfg['refresh_time'])}
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName {q(TASK)} -Action $a -Trigger $t -Settings $s -Description 'Refresh grocery flyer deals' -Force | Out-Null
""")
    else:
        ps(f"Unregister-ScheduledTask -TaskName {q(TASK)} -Confirm:$false -ErrorAction SilentlyContinue")

    lnk = f"(Join-Path ([Environment]::GetFolderPath('Startup')) {q(SHORTCUT)})"
    if cfg["run_on_startup"] or cfg["open_page_on_startup"]:
        ps(f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut({lnk})
$s.TargetPath = {q(pythonw())}
$s.Arguments = {q(f'"{script}" --auto startup')}
$s.WorkingDirectory = {q(ROOT)}
$s.Description = 'Flyer Deals start-up refresh'
$s.Save()
""")
    else:
        ps(f"Remove-Item {lnk} -ErrorAction SilentlyContinue")


def status():
    """What's actually installed, for the settings panel."""
    out = ps(f"""
$t = Get-ScheduledTask -TaskName {q(TASK)} -ErrorAction SilentlyContinue
$next = if ($t) {{ (Get-ScheduledTaskInfo -TaskName {q(TASK)}).NextRunTime }} else {{ $null }}
$lnk = Test-Path (Join-Path ([Environment]::GetFolderPath('Startup')) {q(SHORTCUT)})
@{{ weekly = [bool]$t; next_run = if ($next) {{ $next.ToString('s') }} else {{ $null }}; startup = $lnk }} | ConvertTo-Json -Compress
""")
    return json.loads(out or "{}")
