#!/usr/bin/env python3
"""
Dark-VPN — CLI session manager for OpenVPN on systemd-based Linux.
Profiles are discovered in the current working directory.
OpenVPN runs in the foreground under a dedicated transient systemd service.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

APP = "Dark-VPN"
VERSION = "1.0.0"
UNIT_PREFIX = "dark-vpn-"
START_TIMEOUT = 25

# ANSI palette (automatically disabled when stdout is not a terminal).
TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
RESET = "\033[0m" if TTY else ""
BOLD = "\033[1m" if TTY else ""
DIM = "\033[2m" if TTY else ""
CYAN = "\033[38;5;81m" if TTY else ""
PURPLE = "\033[38;5;177m" if TTY else ""
GREEN = "\033[38;5;84m" if TTY else ""
YELLOW = "\033[38;5;221m" if TTY else ""
RED = "\033[38;5;203m" if TTY else ""
WHITE = "\033[38;5;255m" if TTY else ""

def run(args: list[str], *, check: bool = False, capture: bool = True,
        timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, text=True, stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None, check=check, timeout=timeout
    )

def say(kind: str, message: str) -> None:
    styles = {"ok": GREEN, "info": CYAN, "warn": YELLOW, "error": RED}
    marks = {"ok": "✓", "info": "•", "warn": "!", "error": "✗"}
    print(f"  {styles[kind]}{marks[kind]}{RESET}  {message}")

def banner() -> None:
    art = r"""
██████   █████  ██████  ██   ██       ██    ██ ██████  ███    ██
██   ██ ██   ██ ██   ██ ██  ██        ██    ██ ██   ██ ████   ██
██   ██ ███████ ██████  █████   █████ ██    ██ ██████  ██ ██  ██
██   ██ ██   ██ ██   ██ ██  ██         ██  ██  ██      ██  ██ ██
██████  ██   ██ ██   ██ ██   ██         ████   ██      ██   ████"""
    art_lines = art.strip("\n").splitlines()
    width = max(len(line) for line in art_lines) + 4
    top = f"╭{'─' * width}╮"
    bottom = f"╰{'─' * width}╯"

    print(f"{CYAN}{top}{RESET}")
    for art_line in art_lines:
        padded = art_line.ljust(width - 4)
        print(f"{CYAN}│{RESET}  {PURPLE}{padded}{RESET}  {CYAN}│{RESET}")
    print(f"{CYAN}{bottom}{RESET}")
    print(f"  {BOLD}{CYAN}OPENVPN SESSION MANAGER{RESET}  {DIM}v{VERSION}{RESET}")
    print(f"  {DIM}Discover  •  Connect  •  Monitor  •  Disconnect{RESET}")
    print(f"{DIM}{'─' * 64}{RESET}")

def require_linux_systemd() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Dark-VPN currently requires Linux.")
    if not Path("/run/systemd/system").exists():
        raise RuntimeError("systemd is not running. This version uses systemd transient services.")
    for binary in ("systemctl", "systemd-run", "journalctl", "openvpn"):
        if shutil.which(binary) is None:
            raise RuntimeError(f"Required command not found: {binary}")

def ensure_root() -> None:
    if os.geteuid() == 0:
        return
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("Root privileges are required; install sudo or run as root.")
    script = str(Path(__file__).resolve())
    os.execvp(sudo, [sudo, sys.executable, script, *sys.argv[1:]])

def is_openvpn_profile(path: Path) -> bool:
    try:
        if not path.is_file() or not os.access(path, os.R_OK):
            return False
        if path.suffix.lower() not in {".ovpn", ".conf"}:
            return False
        sample = path.read_text(errors="replace")[:256_000]
        return bool(re.search(
            r"(?im)^\s*(client|remote\s+|dev\s+|proto\s+|tls-client\b|<connection>)",
            sample
        ))
    except OSError:
        return False

def discover_profiles(directory: Path) -> list[Path]:
    try:
        candidates = directory.iterdir()
    except OSError as exc:
        raise RuntimeError(f"Cannot scan {directory}: {exc}") from exc
    return sorted(
        (p.resolve() for p in candidates if is_openvpn_profile(p)),
        key=lambda p: p.name.casefold()
    )

def unit_for(profile: Path) -> str:
    # Include a path hash so same-named profiles in different folders don't collide.
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "-", profile.stem).strip("-.").lower() or "profile"
    digest = hashlib.sha256(str(profile).encode()).hexdigest()[:8]
    return f"{UNIT_PREFIX}{stem[:35]}-{digest}.service"

def unit_active(unit: str) -> bool:
    result = run(["systemctl", "is-active", "--quiet", unit])
    return result.returncode == 0

def unit_state(unit: str) -> str:
    result = run(["systemctl", "show", "--property=ActiveState", "--value", unit])
    return result.stdout.strip() if result.returncode == 0 else "not-found"

def start_profile(profile: Path) -> None:
    profile = profile.resolve()
    if not is_openvpn_profile(profile):
        raise RuntimeError(f"Not a readable OpenVPN profile: {profile}")
    unit = unit_for(profile)
    state = unit_state(unit)
    if state in {"active", "activating", "reloading"}:
        say("ok", f"Already running: {profile.name}")
        show_status(profile)
        return

    # Do not use --daemon: systemd owns and monitors the foreground process.
    command = [
        "systemd-run", f"--unit={unit}",
        f"--description=Dark-VPN session: {profile.name}",
        "--property=Type=exec",
        "--property=KillMode=control-group",
        "--property=Restart=no",
        "--collect",
        "--no-block",
        "openvpn", "--config", str(profile), "--verb", "3",
    ]
    result = run(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Could not start session: {detail or 'systemd-run failed'}")

    say("info", f"Starting {profile.name}...")
    deadline = time.monotonic() + START_TIMEOUT
    last_state = ""
    while time.monotonic() < deadline:
        last_state = unit_state(unit)
        if last_state == "active":
            say("ok", f"Session started: {profile.name}")
            say("info", f"Service: {unit}")
            show_status(profile)
            return
        if last_state in {"failed", "inactive", "not-found"}:
            break
        time.sleep(0.5)

    details = run(["journalctl", "-u", unit, "-n", "18", "--no-pager"])
    tail = details.stdout.strip()
    raise RuntimeError(
        f"OpenVPN did not reach active state (state: {last_state})."
        + (f"\nRecent service log:\n{tail}" if tail else "")
    )

def stop_profile(profile: Path) -> None:
    unit = unit_for(profile.resolve())
    state = unit_state(unit)
    if state in {"inactive", "not-found"}:
        say("info", f"No active session for {profile.name}.")
        return
    result = run(["systemctl", "stop", unit], timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "systemctl stop failed")
    say("ok", f"Disconnected: {profile.name}")

def show_status(profile: Path) -> None:
    unit = unit_for(profile.resolve())
    state = unit_state(unit)
    color = GREEN if state == "active" else YELLOW if state in {"activating", "deactivating"} else DIM
    print(f"  {'PROFILE':<14} {profile.name}")
    print(f"  {'STATE':<14} {color}{state.upper()}{RESET}")
    print(f"  {'SERVICE':<14} {unit}")
    if state == "active":
        result = run(["systemctl", "show", unit,
                      "--property=MainPID", "--property=ActiveEnterTimestamp",
                      "--property=ExecMainStartTimestamp"])
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key == "MainPID":
                print(f"  {'OPENVPN PID':<14} {value}")
            elif key == "ActiveEnterTimestamp" and value:
                print(f"  {'SINCE':<14} {value}")

def show_logs(profile: Path, follow: bool = False) -> None:
    unit = unit_for(profile.resolve())
    args = ["journalctl", "-u", unit, "-n", "60", "--no-pager"]
    if follow:
        args = ["journalctl", "-u", unit, "-f"]
    subprocess.run(args)

def list_profiles(profiles: list[Path]) -> None:
    if not profiles:
        say("warn", f"No OpenVPN profiles found in {Path.cwd()}")
        return
    print(f"\n  {BOLD}DISCOVERED PROFILES{RESET}")
    for index, profile in enumerate(profiles, 1):
        state = unit_state(unit_for(profile))
        color = GREEN if state == "active" else DIM
        print(f"  {PURPLE}{index:>2}{RESET}  {profile.name:<42} {color}{state}{RESET}")
    print()

def choose_profile(profiles: list[Path]) -> Optional[Path]:
    if not profiles:
        say("warn", f"No .ovpn or OpenVPN .conf profiles found in {Path.cwd()}")
        return None
    list_profiles(profiles)
    while True:
        choice = input(f"  {CYAN}Select profile number (0 to cancel) › {RESET}").strip()
        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1]
        say("warn", f"Enter a number from 1 to {len(profiles)}, or 0.")

def doctor() -> None:
    print(f"\n  {BOLD}SYSTEM DIAGNOSTICS{RESET}")
    for name, command in [
        ("systemd", ["systemctl", "is-system-running"]),
        ("OpenVPN", ["openvpn", "--version"]),
        ("systemd-run", ["systemd-run", "--version"]),
    ]:
        result = run(command)
        first = (result.stdout or result.stderr).splitlines()
        say("ok" if result.returncode == 0 else "error",
            f"{name}: {first[0] if first else 'available'}")
    print(f"  {'WORKING DIRECTORY':<18} {Path.cwd()}")
    print()

def interactive(profiles_dir: Path) -> None:
    while True:
        banner()
        profiles = discover_profiles(profiles_dir)
        print(f"  {DIM}Profiles directory: {profiles_dir}{RESET}")
        print(f"  {DIM}{len(profiles)} profile(s) discovered{RESET}\n")
        print(f"  {BOLD}MAIN MENU{RESET}")
        print(f"  {PURPLE}1{RESET}  Connect / select profile")
        print(f"  {PURPLE}2{RESET}  List profiles and session states")
        print(f"  {PURPLE}3{RESET}  Disconnect a profile")
        print(f"  {PURPLE}4{RESET}  View session logs")
        print(f"  {PURPLE}5{RESET}  System diagnostics")
        print(f"  {PURPLE}0{RESET}  Exit\n")
        choice = input(f"  {CYAN}Dark-VPN › {RESET}").strip()
        try:
            if choice == "1":
                profile = choose_profile(profiles)
                if profile:
                    start_profile(profile)
                    input("\n  Press Enter to return to menu...")
            elif choice == "2":
                list_profiles(profiles)
                input("  Press Enter to continue...")
            elif choice == "3":
                profile = choose_profile(profiles)
                if profile:
                    stop_profile(profile)
                    input("\n  Press Enter to continue...")
            elif choice == "4":
                profile = choose_profile(profiles)
                if profile:
                    show_logs(profile, follow=False)
                    input("\n  Press Enter to continue...")
            elif choice == "5":
                doctor()
                input("  Press Enter to continue...")
            elif choice == "0":
                print(f"\n  {GREEN}Dark-VPN closed. Managed VPN sessions remain running.{RESET}\n")
                return
            else:
                say("warn", "Unknown menu option.")
                time.sleep(1)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            say("error", str(exc))
            input("\n  Press Enter to continue...")
        except KeyboardInterrupt:
            print("\n")
            return

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dark-vpn",
        description="Dark-VPN — OpenVPN session manager for systemd Linux."
    )
    parser.add_argument("--profiles-dir", type=Path, default=Path(os.environ.get("DARK_VPN_PROFILES_DIR", Path.cwd())), help="Directory containing .ovpn/.conf profiles (default: current directory or DARK_VPN_PROFILES_DIR)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="Launch interactive session manager")
    sub.add_parser("list", help="Discover profiles and show their states")
    start = sub.add_parser("start", help="Start a profile")
    start.add_argument("profile", help="Profile filename or path")
    stop = sub.add_parser("stop", help="Stop a profile")
    stop.add_argument("profile", help="Profile filename or path")
    status = sub.add_parser("status", help="Show a profile's service status")
    status.add_argument("profile", help="Profile filename or path")
    logs = sub.add_parser("logs", help="Show a profile's logs")
    logs.add_argument("profile", help="Profile filename or path")
    logs.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("doctor", help="Check required system components")
    args = parser.parse_args()

    try:
        require_linux_systemd()
        if args.command is None or args.command == "menu":
            # The menu needs root only when an action is selected. Re-exec the
            # selected action through sudo in a later refinement; for now root
            # is requested at launch to keep service operations predictable.
            ensure_root()
            interactive(args.profiles_dir.expanduser().resolve())
            return 0

        ensure_root()
        if args.command == "doctor":
            doctor()
            return 0

        if args.command == "list":
            list_profiles(discover_profiles(args.profiles_dir.expanduser().resolve()))
            return 0

        candidate = Path(args.profile).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        profile = candidate.resolve()
        if args.command == "start":
            start_profile(profile)
        elif args.command == "stop":
            stop_profile(profile)
        elif args.command == "status":
            show_status(profile)
        elif args.command == "logs":
            show_logs(profile, args.follow)
        return 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        say("error", str(exc))
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
