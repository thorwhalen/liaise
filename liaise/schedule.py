"""Scheduling `liaise run --once` (A.7): a launchd agent on macOS, a systemd
user timer on Linux.

launchd and systemd both hand a job a nearly-empty environment, so the
installed job needs an environment **snapshot** taken from the installing
shell — `PATH` (so it can find `gh` and the dispatch command, e.g. `claude`),
plus the ntfy topic variable and anything else a partner's dispatch command
needs. Re-run install after moving any of those.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from liaise.config import DFLT_NTFY_TOPIC_ENV

DFLT_LAUNCHD_LABEL = "com.liaise.run"
DFLT_SYSTEMD_UNIT = "liaise-run"
DFLT_INTERVAL_MINUTES = 2

DFLT_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
DFLT_LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs"
DFLT_SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"


def job_environment(
    *, extra_env_vars: Sequence[str] = (), ntfy_topic_env: str = DFLT_NTFY_TOPIC_ENV
) -> dict[str, str]:
    """The environment to pin into the installed job.

    `PATH` carries every directory a dispatch command is likely to need `gh`
    or `claude` from, plus the standard system locations. `HOME` is included
    because both schedulers otherwise omit it. The ntfy topic variable and any
    `extra_env_vars` are copied over from *this* shell if set — silently
    omitted otherwise, since a job that can't notify still runs.
    """
    path_parts = [
        str(Path(sys.executable).parent),
        *([str(Path(shutil.which("gh")).parent)] if shutil.which("gh") else []),
        *([str(Path(shutil.which("claude")).parent)] if shutil.which("claude") else []),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    env = {
        "PATH": ":".join(dict.fromkeys(path_parts)),
        "HOME": str(Path.home()),
    }
    for var in (ntfy_topic_env, *extra_env_vars):
        value = os.environ.get(var)
        if value:
            env[var] = value
    return env


def _liaise_command(*, root: Optional[str] = None) -> list[str]:
    args = [sys.executable, "-m", "liaise", "run", "--once"]
    if root:
        args += ["--root", root]
    return args


def _plist_xml(
    *,
    label: str,
    program_args: list[str],
    env: dict[str, str],
    interval_seconds: int,
    log_path: Path,
) -> str:
    esc = lambda s: (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    args_xml = "\n".join(f"        <string>{esc(a)}</string>" for a in program_args)
    env_xml = "\n".join(
        f"        <key>{esc(k)}</key>\n        <string>{esc(v)}</string>"
        for k, v in env.items()
    )
    # AbandonProcessGroup: the tick starts processor runs detached and exits at once;
    # without it launchd kills those runs along with the job's process group (§3.5).
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{esc(label)}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>AbandonProcessGroup</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{esc(log_path)}</string>
    <key>StandardErrorPath</key>
    <string>{esc(log_path)}</string>
</dict>
</plist>
"""


def _systemd_unit_text(*, program_args: list[str], env: dict[str, str]) -> str:
    exec_start = " ".join(program_args)
    env_lines = "\n".join(f'Environment="{k}={v}"' for k, v in env.items())
    # KillMode=process: like launchd's AbandonProcessGroup, so detached runs outlive the tick.
    return f"""[Unit]
Description=liaise run --once

[Service]
Type=oneshot
KillMode=process
{env_lines}
ExecStart={exec_start}
"""


def _systemd_timer_text(*, unit: str, interval_minutes: int) -> str:
    return f"""[Unit]
Description=Run {unit}.service on a schedule

[Timer]
OnBootSec={interval_minutes}min
OnUnitActiveSec={interval_minutes}min
Persistent=true

[Install]
WantedBy=timers.target
"""


def install_schedule(
    *,
    root: Optional[str] = None,
    interval_minutes: int = DFLT_INTERVAL_MINUTES,
    system: Optional[str] = None,
    launchd_dir: Optional[Path] = None,
    launchd_log_dir: Optional[Path] = None,
    systemd_dir: Optional[Path] = None,
    label: str = DFLT_LAUNCHD_LABEL,
    unit: str = DFLT_SYSTEMD_UNIT,
    load: bool = True,
    ntfy_topic_env: str = DFLT_NTFY_TOPIC_ENV,
    extra_env_vars: Sequence[str] = (),
) -> str:
    """Install (or replace) the scheduled job. Returns the path(s) written.

    `system` overrides `platform.system()` (a seam for tests); `launchd_dir`
    / `launchd_log_dir` / `systemd_dir` override the real locations under
    `$HOME` (also for tests); `load=False` writes the files without calling
    `launchctl`/`systemctl` — the actual scheduler state is left untouched.
    `ntfy_topic_env` should be this installation's actual configured
    `notify.ntfy_topic_env` (M-4) — a custom variable name that never reaches
    here means every scheduled-run notification silently disappears.
    """
    system = system or platform.system()
    program_args = _liaise_command(root=root)
    env = job_environment(extra_env_vars=extra_env_vars, ntfy_topic_env=ntfy_topic_env)

    if system == "Darwin":
        return _install_launchd(
            program_args,
            env,
            interval_minutes,
            launchd_dir,
            launchd_log_dir,
            label,
            load,
        )
    if system == "Linux":
        return _install_systemd(
            program_args, env, interval_minutes, systemd_dir, unit, load
        )
    raise NotImplementedError(
        f"no scheduler support for {system!r} — liaise supports macOS (launchd) "
        f"and Linux (systemd user timers)."
    )


def _install_launchd(
    program_args: list[str],
    env: dict[str, str],
    interval_minutes: int,
    launchd_dir: Optional[Path],
    log_dir: Optional[Path],
    label: str,
    load: bool,
) -> str:
    launchd_dir = Path(launchd_dir) if launchd_dir else DFLT_LAUNCHD_DIR
    log_dir = Path(log_dir) if log_dir else DFLT_LAUNCHD_LOG_DIR
    log_path = log_dir / f"{label}.log"

    plist = _plist_xml(
        label=label,
        program_args=program_args,
        env=env,
        interval_seconds=interval_minutes * 60,
        log_path=log_path,
    )
    plist_path = launchd_dir / f"{label}.plist"
    launchd_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)

    if load:
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True
        )
        loaded = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True,
            text=True,
        )
        if loaded.returncode != 0:
            raise RuntimeError(
                f"wrote {plist_path} but launchctl bootstrap failed: "
                f"{(loaded.stderr or loaded.stdout).strip()}"
            )
    return str(plist_path)


def _install_systemd(
    program_args: list[str],
    env: dict[str, str],
    interval_minutes: int,
    systemd_dir: Optional[Path],
    unit: str,
    load: bool,
) -> str:
    systemd_dir = Path(systemd_dir) if systemd_dir else DFLT_SYSTEMD_DIR
    systemd_dir.mkdir(parents=True, exist_ok=True)

    service_path = systemd_dir / f"{unit}.service"
    timer_path = systemd_dir / f"{unit}.timer"
    service_path.write_text(_systemd_unit_text(program_args=program_args, env=env))
    timer_path.write_text(
        _systemd_timer_text(unit=unit, interval_minutes=interval_minutes)
    )

    if load:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        enabled = subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{unit}.timer"],
            capture_output=True,
            text=True,
        )
        if enabled.returncode != 0:
            raise RuntimeError(
                f"wrote {service_path} and {timer_path} but systemctl enable failed: "
                f"{(enabled.stderr or enabled.stdout).strip()}"
            )
    return f"{service_path}, {timer_path}"


def uninstall_schedule(
    *,
    system: Optional[str] = None,
    launchd_dir: Optional[Path] = None,
    systemd_dir: Optional[Path] = None,
    label: str = DFLT_LAUNCHD_LABEL,
    unit: str = DFLT_SYSTEMD_UNIT,
    load: bool = True,
) -> str:
    """Remove the scheduled job. Idempotent — a no-op if nothing was installed."""
    system = system or platform.system()
    if system == "Darwin":
        launchd_dir = Path(launchd_dir) if launchd_dir else DFLT_LAUNCHD_DIR
        plist_path = launchd_dir / f"{label}.plist"
        if load:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
            )
        plist_path.unlink(missing_ok=True)
        return f"removed {plist_path}"
    if system == "Linux":
        systemd_dir = Path(systemd_dir) if systemd_dir else DFLT_SYSTEMD_DIR
        service_path = systemd_dir / f"{unit}.service"
        timer_path = systemd_dir / f"{unit}.timer"
        if load:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{unit}.timer"],
                capture_output=True,
            )
        service_path.unlink(missing_ok=True)
        timer_path.unlink(missing_ok=True)
        return f"removed {service_path}, {timer_path}"
    raise NotImplementedError(f"no scheduler support for {system!r}")


def schedule_status(
    *,
    system: Optional[str] = None,
    launchd_dir: Optional[Path] = None,
    systemd_dir: Optional[Path] = None,
    label: str = DFLT_LAUNCHD_LABEL,
    unit: str = DFLT_SYSTEMD_UNIT,
) -> str:
    """Whether the scheduled job is installed, and where."""
    system = system or platform.system()
    if system == "Darwin":
        path = (
            Path(launchd_dir) if launchd_dir else DFLT_LAUNCHD_DIR
        ) / f"{label}.plist"
    elif system == "Linux":
        path = (
            Path(systemd_dir) if systemd_dir else DFLT_SYSTEMD_DIR
        ) / f"{unit}.timer"
    else:
        raise NotImplementedError(f"no scheduler support for {system!r}")
    return (
        f"installed: {path}" if path.exists() else f"not installed (expected at {path})"
    )
