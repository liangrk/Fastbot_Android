"""Minimal Windows-safe ADB wrapper for the Fastbot extension tooling.

The adb binary is located via the ADB environment variable, then the
bundled android-cli path, then PATH, then bare "adb".
subprocess is always invoked with an argument list (never shell=True).

CLI example:
    python tools/common/adb.py --dry-run shell "echo hi"
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional, Sequence, Tuple, Union

Command = Union[str, Sequence[str]]
CommandResult = Tuple[int, str, str]

BACKOFF_STEP_SECONDS = 1

BUNDLED_ADB = r"E:\developer\android-cli\platform-tools\adb.exe"


class AdbError(RuntimeError):
    """Raised when an adb operation fails after all retries."""


def locate_adb() -> str:
    """adb path: ADB env -> bundled android-cli path (when present) -> PATH."""
    env_value = os.environ.get("ADB")
    if env_value:
        return env_value
    if os.path.exists(BUNDLED_ADB):
        return BUNDLED_ADB
    return shutil.which("adb") or "adb"


class AdbClient:
    """Subprocess wrapper around adb with timeout, retry and dry-run support."""

    def __init__(
        self,
        serial: Optional[str] = None,
        timeout: float = 30,
        retries: int = 2,
        dry_run: bool = False,
        adb_path: Optional[str] = None,
    ) -> None:
        self.serial = serial
        self.timeout = timeout
        self.retries = retries
        self.dry_run = dry_run
        self.adb_path = adb_path or locate_adb()

    # ------------------------------------------------------------------ #
    # Public operations
    # ------------------------------------------------------------------ #

    def shell(self, cmd: Command) -> CommandResult:
        """Run a shell command on the device. Returns (rc, stdout, stderr)."""
        args = ["shell"]
        if isinstance(cmd, str):
            args.append(cmd)
        else:
            args.extend(cmd)
        return self._execute(args)

    def push(self, local: str, remote: str) -> CommandResult:
        """Push a local file to the device. Returns (rc, stdout, stderr)."""
        return self._execute(["push", local, remote])

    def pull(self, remote: str, local: str) -> CommandResult:
        """Pull a device file to this machine. Returns (rc, stdout, stderr)."""
        return self._execute(["pull", remote, local])

    def devices(self) -> List[str]:
        """Return serials of attached devices currently in the 'device' state."""
        _, stdout, _ = self._execute(["devices"], use_serial=False)
        serials: List[str] = []
        for line in stdout.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _resolve_serial(self) -> str:
        """Return the target serial, auto-detecting a single attached device."""
        if self.serial is not None:
            return self.serial
        serials = self.devices()
        if not serials:
            raise AdbError("no devices attached; cannot auto-detect serial")
        if len(serials) > 1:
            raise AdbError(
                "multiple devices attached (%s); pass --serial explicitly"
                % ", ".join(serials)
            )
        self.serial = serials[0]
        return self.serial

    def _dry_run(self, args: Sequence[str], use_serial: bool) -> CommandResult:
        argv: List[str] = [self.adb_path]
        if use_serial and self.serial:
            argv += ["-s", self.serial]
        argv += list(args)
        print("[DRY-RUN] " + " ".join(argv))
        return (0, "", "")

    def _execute(self, args: Sequence[str], use_serial: bool = True) -> CommandResult:
        """Run adb with retry + linear backoff; raise AdbError when exhausted."""
        if self.dry_run:
            return self._dry_run(args, use_serial)

        argv: List[str] = [self.adb_path]
        if use_serial:
            argv += ["-s", self._resolve_serial()]
        argv += list(args)

        last_error: Optional[AdbError] = None
        for attempt in range(self.retries + 1):
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                )
                if proc.returncode == 0:
                    return (proc.returncode, proc.stdout, proc.stderr)
                last_error = AdbError(
                    "adb failed (exit=%s): %s | stderr: %s"
                    % (proc.returncode, " ".join(argv), proc.stderr.strip())
                )
            except subprocess.TimeoutExpired:
                last_error = AdbError(
                    "adb timed out after %ss: %s" % (self.timeout, " ".join(argv))
                )
            if attempt < self.retries:
                time.sleep(BACKOFF_STEP_SECONDS * (attempt + 1))
        if last_error is None:
            raise AdbError("adb failed without an error object: %s" % " ".join(argv))
        raise last_error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adb.py",
        description="Fastbot ADB helper (supports --dry-run command planning).",
    )
    parser.add_argument(
        "--serial", default=None, help="device serial (auto-detects when omitted)"
    )
    parser.add_argument(
        "--timeout", type=float, default=30, help="per-command timeout in seconds"
    )
    parser.add_argument(
        "--retries", type=int, default=2, help="retry count on transient failures"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned adb command instead of executing it",
    )
    sub = parser.add_subparsers(dest="op", required=True)

    p_shell = sub.add_parser("shell", help="run a device shell command")
    p_shell.add_argument("cmd", nargs="+", help="shell command, e.g. 'echo hi'")

    p_push = sub.add_parser("push", help="push a local file to the device")
    p_push.add_argument("local")
    p_push.add_argument("remote")

    p_pull = sub.add_parser("pull", help="pull a device file to this machine")
    p_pull.add_argument("remote")
    p_pull.add_argument("local")

    sub.add_parser("devices", help="list attached device serials")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    client = AdbClient(
        serial=args.serial,
        timeout=args.timeout,
        retries=args.retries,
        dry_run=args.dry_run,
    )
    if args.op == "shell":
        returncode, stdout, _ = client.shell(args.cmd)
    elif args.op == "push":
        returncode, stdout, _ = client.push(args.local, args.remote)
    elif args.op == "pull":
        returncode, stdout, _ = client.pull(args.remote, args.local)
    else:  # devices
        for serial in client.devices():
            print(serial)
        return 0
    sys.stdout.write(stdout)
    if stdout and not stdout.endswith("\n"):
        sys.stdout.write("\n")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
