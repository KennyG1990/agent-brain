"""
Scheduling the scan, on whichever OS you're on.

Only the SCAN is scheduled — it is free, local and deterministic. The graph step
costs money, so it stays manual by design. A hidden scheduled job that quietly spends
money is exactly the failure this project was born from.

Windows : schtasks
macOS   : launchd (~/Library/LaunchAgents)
Linux   : systemd --user timer, falling back to crontab

Every backend reports the exact command it ran, so nothing is installed invisibly.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TASK = "AgentBrainScan"


def _python() -> str:
    return sys.executable or "python"


def _cmd(vault: Path) -> list[str]:
    root = Path(__file__).resolve().parents[1]
    return [_python(), "-m", "agentbrain.cli", "scan", "--vault", str(vault)], str(root)


def _run(cmd: list[str], stdin: str | None = None) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout or p.stderr or "").strip()


def backend() -> str:
    if sys.platform == "win32":
        return "Windows Task Scheduler"
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl"):
        return "systemd --user"
    if shutil.which("crontab"):
        return "crontab"
    return "none"


# --------------------------------------------------------------------------- #
def install(vault: Path, hour: int = 3) -> tuple[bool, str]:
    argv, root = _cmd(vault)
    if sys.platform == "win32":
        inner = " ".join(f'"{a}"' if " " in a else a for a in argv)
        ok, out = _run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/TN",
                TASK,
                "/SC",
                "DAILY",
                "/ST",
                f"{hour:02d}:00",
                "/TR",
                f"cmd /c set PYTHONPATH={root}&& {inner}",
            ]
        )
        return ok, (
            f"Daily at {hour:02d}:00 via Task Scheduler ({TASK}).\n"
            f"Remove with: schtasks /Delete /TN {TASK} /F"
            if ok
            else out
        )

    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"com.agentbrain.{TASK}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"    <string>{a}</string>\n" for a in argv)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f"  <key>Label</key><string>com.agentbrain.{TASK}</string>\n"
            f"  <key>ProgramArguments</key><array>\n{args}  </array>\n"
            f"  <key>EnvironmentVariables</key><dict>"
            f"<key>PYTHONPATH</key><string>{root}</string></dict>\n"
            f"  <key>StartCalendarInterval</key><dict>"
            f"<key>Hour</key><integer>{hour}</integer>"
            f"<key>Minute</key><integer>0</integer></dict>\n"
            "</dict></plist>\n",
            encoding="utf-8",
        )
        _run(["launchctl", "unload", str(plist)])
        ok, out = _run(["launchctl", "load", str(plist)])
        return ok, (f"Daily at {hour:02d}:00 via launchd.\n{plist}" if ok else out)

    if shutil.which("systemctl"):
        d = Path.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{TASK}.service").write_text(
            f"[Unit]\nDescription=Agent Brain scan\n\n[Service]\nType=oneshot\n"
            f"Environment=PYTHONPATH={root}\n"
            f"ExecStart={' '.join(argv)}\n",
            encoding="utf-8",
        )
        (d / f"{TASK}.timer").write_text(
            f"[Unit]\nDescription=Agent Brain daily scan\n\n[Timer]\n"
            f"OnCalendar=*-*-* {hour:02d}:00:00\nPersistent=true\n\n"
            f"[Install]\nWantedBy=timers.target\n",
            encoding="utf-8",
        )
        _run(["systemctl", "--user", "daemon-reload"])
        ok, out = _run(["systemctl", "--user", "enable", "--now", f"{TASK}.timer"])
        return ok, (
            f"Daily at {hour:02d}:00 via systemd --user.\n"
            f"Remove with: systemctl --user disable --now {TASK}.timer"
            if ok
            else out
        )

    if shutil.which("crontab"):
        ok, cur = _run(["crontab", "-l"])
        lines = [l for l in (cur.splitlines() if ok else []) if TASK not in l]
        lines.append(f"0 {hour} * * * PYTHONPATH={root} {' '.join(argv)}  # {TASK}")
        ok, out = _run(["crontab", "-"], stdin="\n".join(lines) + "\n")
        return ok, (f"Daily at {hour:02d}:00 via crontab." if ok else out)

    return False, (
        "No scheduler found on this system. Run the scan manually, or wire "
        f"this command into whatever you use:\n  PYTHONPATH={root} "
        f"{' '.join(argv)}"
    )


def uninstall() -> tuple[bool, str]:
    if sys.platform == "win32":
        return _run(["schtasks", "/Delete", "/TN", TASK, "/F"])
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"com.agentbrain.{TASK}.plist"
        _run(["launchctl", "unload", str(plist)])
        if plist.exists():
            plist.unlink()
        return True, "launchd job removed"
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", f"{TASK}.timer"])
        for f in (Path.home() / ".config" / "systemd" / "user").glob(f"{TASK}.*"):
            f.unlink()
        return True, "systemd timer removed"
    if shutil.which("crontab"):
        ok, cur = _run(["crontab", "-l"])
        lines = [l for l in (cur.splitlines() if ok else []) if TASK not in l]
        return _run(["crontab", "-"], stdin="\n".join(lines) + "\n")
    return False, "no scheduler"


def status() -> tuple[bool, str]:
    if sys.platform == "win32":
        ok, out = _run(["schtasks", "/Query", "/TN", TASK])
        return ok, out if ok else "not scheduled"
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "LaunchAgents" / f"com.agentbrain.{TASK}.plist"
        return p.exists(), (str(p) if p.exists() else "not scheduled")
    if shutil.which("systemctl"):
        ok, out = _run(["systemctl", "--user", "list-timers", f"{TASK}.timer", "--no-pager"])
        return (ok and TASK in out), (out if ok else "not scheduled")
    if shutil.which("crontab"):
        ok, cur = _run(["crontab", "-l"])
        hit = ok and TASK in cur
        return hit, ("scheduled via crontab" if hit else "not scheduled")
    return False, "no scheduler available"


def main() -> int:
    import argparse

    from . import config

    ap = argparse.ArgumentParser(description="Schedule the (free, local) Agent Brain scan")
    ap.add_argument("action", choices=["install", "uninstall", "status"])
    ap.add_argument("--hour", type=int, default=3)
    ap.add_argument("--vault", type=Path, default=None)
    a = ap.parse_args()
    print(f"Scheduler backend: {backend()}")
    if a.action == "install":
        ok, msg = install(a.vault or config.vault_path(), a.hour)
    elif a.action == "uninstall":
        ok, msg = uninstall()
    else:
        ok, msg = status()
    print(("OK: " if ok else "FAILED: ") + str(msg))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
