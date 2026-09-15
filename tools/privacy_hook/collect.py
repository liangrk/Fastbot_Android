#!/usr/bin/env python3
"""M4 privacy extension (plan item M4.5, optional, rooted devices only): frida collector.

Runs the frida CLI (invoked as an external binary - this module never imports frida)
with tools/privacy_hook/hook.js wrapped with the apis.json manifest injected as
``globalThis.APIS_OVERRIDE``, monitors the target app for --duration-sec seconds and
appends audit records to --out as JSONL conforming to tools/schemas/audit.schema.json:

    {"ts": <int>, "type": "sensitive_api", "activity": "<str>", "widget": null,
     "detail": "api=...; permission=...; package=...; args=...", "screenshot": null,
     "source": "frida"}

Mechanism
    - writes a temporary wrapper: ``globalThis.APIS_OVERRIDE = <apis.json>;`` + hook.js
    - argv: frida [-D <serial> | -U] (-f <package> | -n <package>) -l <wrapper> -q
    - filters frida stdout lines with the @@AUDIT@@ prefix, parses the JSON payload,
      validates loosely (required fields + types), fills missing optional fields
      (widget/screenshot) with null per schema, appends to --out.

Exit codes
    0  success (monitoring window finished, records appended if any)
    1  runtime error: frida CLI missing (hint: pip install frida-tools), no device,
       package not installed, or frida exited nonzero with zero audit output
    2  validation error: unreadable manifest / hook.js, non-positive duration

Usage
    python tools/privacy_hook/collect.py --dry-run --package com.example
    python tools/privacy_hook/collect.py --package com.example --serial SER --duration-sec 120 --out audit.jsonl
    python tools/privacy_hook/collect.py --package com.example --attach --duration-sec 60
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# tools/privacy_hook/ is one level below tools/, so tools/ is parents[1];
# bootstrap it so `from common.adb import ...` works when run as a script.
TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from common.adb import AdbClient, AdbError

HERE = Path(__file__).resolve().parent
HOOK_JS = Path(__file__).resolve().parent / "hook.js"
DEFAULT_MANIFEST = HERE / "apis.json"

AUDIT_PREFIX = "@@AUDIT@@"
TYPES = ("permission_prompt", "sensitive_api", "rule_hit")
SOURCES = ("device", "frida")

HINT_FRIDA = "frida CLI not found - install it with: pip install frida-tools"
HINT_DEVICE = (
    "no attached device found - connect a rooted device with frida-server running "
    "(see https://frida.re/docs/frida-server/), or pass --serial"
)
HINT_SERVER = (
    "frida exited nonzero with zero audit output. If the device is rooted, "
    "ensure frida-server is running on the device "
    "(adb shell su -c '/data/local/tmp/frida-server &')"
)


def load_manifest(path: str) -> List[Dict[str, Any]]:
    """Load + shape-check an apis.json manifest. Returns the entries list.

    Raises ValueError on unreadable/invalid manifests (exit 2 territory).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        raise ValueError("cannot read manifest %s: %s" % (path, error))
    if not isinstance(payload, dict) or not isinstance(payload.get("apis"), list):
        raise ValueError("manifest %s must be a JSON object with an 'apis' array" % path)
    entries = payload["apis"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("manifest entry #%d is not a JSON object" % index)
        for key in ("class", "method", "permission", "risk"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "manifest entry #%d (%s) missing non-empty '%s'"
                    % (index, entry.get("class", "?"), key)
                )
        if entry["risk"] not in ("high", "medium"):
            raise ValueError(
                "manifest entry #%d (%s.%s): risk must be 'high' or 'medium', got %r"
                % (index, entry["class"], entry["method"], entry["risk"])
            )
    return entries


def prelude_js(entries: List[Dict[str, Any]]) -> str:
    """JS prelude injecting the manifest as a Frida global (see hook.js header)."""
    return "globalThis.APIS_OVERRIDE = %s;\n" % json.dumps(
        {"apis": entries}, ensure_ascii=False
    )


def build_wrapper(hook_source: str, entries: List[Dict[str, Any]]) -> str:
    """Full -l script: manifest prelude + hook.js source."""
    return prelude_js(entries) + hook_source


def build_command(
    frida_exe: str,
    wrapper_path: str,
    serial: Optional[str],
    package: str,
    attach: bool,
) -> List[str]:
    """Build the frida argv: [frida] [-D serial | -U] [-f pkg | -n pkg] -l wrapper -q."""
    argv: List[str] = [frida_exe]
    argv += ["-D", serial] if serial else ["-U"]
    argv += ["-n", package] if attach else ["-f", package]
    argv += ["-l", wrapper_path, "-q"]
    return argv


def filter_audit_lines(text: str) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """Filter @@AUDIT@@-prefixed lines from frida stdout and parse their JSON.

    Returns (records, malformed) where malformed is a list of (lineno, reason);
    malformed lines are skipped and counted, never crash.
    """
    records: List[Dict[str, Any]] = []
    malformed: List[Tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line.startswith(AUDIT_PREFIX):
            continue
        body = line[len(AUDIT_PREFIX):]
        try:
            records.append(json.loads(body))
        except ValueError as error:
            malformed.append((lineno, "invalid JSON: %s" % error))
    return records, malformed


def normalize_record(obj: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Loose schema validation + optional-field fill for one audit object.

    Checks the schema-required fields (ts integer, type/source enums,
    activity/detail strings) and returns a record holding all 7 schema keys in
    schema order, with widget/screenshot filled with null when missing.
    Returns (record, None) on success or (None, reason).
    """
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    ts = obj.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int):
        return None, "ts must be an integer"
    if obj.get("type") not in TYPES:
        return None, "type must be one of %s (got %r)" % (list(TYPES), obj.get("type"))
    activity = obj.get("activity")
    if not isinstance(activity, str):
        return None, "activity must be a string"
    if not isinstance(obj.get("detail"), str):
        return None, "detail must be a string"
    if obj.get("source") not in SOURCES:
        return None, "source must be one of %s" % list(SOURCES)
    widget = obj.get("widget")
    screenshot = obj.get("screenshot")
    record = {
        "ts": ts,
        "type": obj["type"],
        "activity": activity,
        "widget": widget if isinstance(widget, str) else None,
        "detail": obj["detail"],
        "screenshot": screenshot if isinstance(screenshot, str) else None,
        "source": obj["source"],
    }
    return record, None


def append_jsonl(path: str, records: Sequence[Dict[str, Any]]) -> None:
    """Append normalized records to --out as JSONL (utf-8, append mode)."""
    if not records:
        return
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ": ")))
            handle.write("\n")


def run_frida_capture(argv: Sequence[str], duration_sec: float) -> Tuple[int, str, str]:
    """Run frida via subprocess for --duration-sec, kill at the deadline.

    The deadline is the expected end of the monitoring window, not an error.
    Returns (returncode, stdout, stderr).
    """
    print("  frida running for %ss (deadline kill = expected end of window)..." % duration_sec)
    proc = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, err = proc.communicate(timeout=duration_sec)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        returncode = proc.returncode
        print("  duration reached - frida terminated (expected)")
    return returncode, out or "", err or ""


def check_device_and_package(client: AdbClient, package: str) -> Tuple[Optional[str], Optional[str]]:
    """Contact the device: presence + package installed. Returns (serial, error)."""
    try:
        serials = client.devices()
        if not serials:
            return None, HINT_DEVICE
        serial = client._resolve_serial()
        rc, out, _ = client.shell(["pm", "path", package])
    except AdbError as error:
        return None, "adb error during preflight: %s" % error
    if rc != 0 or not out.strip():
        return None, "package %s not installed on device (pm path rc=%s)" % (package, rc)
    return serial, None


def run_collection(args: argparse.Namespace) -> int:
    """Real run: frida preflight, monitoring window, parse + append JSONL."""
    frida_exe = shutil.which("frida")
    if frida_exe is None:
        print("ERROR: %s" % HINT_FRIDA, file=sys.stderr)
        return 1
    if args.duration_sec <= 0:
        print("ERROR: --duration-sec must be > 0", file=sys.stderr)
        return 2
    try:
        entries = load_manifest(args.manifest)
    except ValueError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2
    try:
        hook_source = HOOK_JS.read_text(encoding="utf-8")
    except OSError:
        print("ERROR: cannot read %s" % HOOK_JS, file=sys.stderr)
        return 2
    client = AdbClient(serial=args.serial)
    serial, preflight_error = check_device_and_package(client, args.package)
    if preflight_error is not None:
        print("ERROR: %s" % preflight_error, file=sys.stderr)
        return 1
    print(
        "collect: package=%s serial=%s mode=%s duration=%ss out=%s"
        % (args.package, serial, "attach" if args.attach else "spawn", args.duration_sec, args.out)
    )
    print("  manifest: %d apis from %s" % (len(entries), args.manifest))
    wrapper_text = build_wrapper(hook_source, entries)
    tempdir = tempfile.mkdtemp(prefix="fastbot_hook_")
    wrapper_path = os.path.join(tempdir, "frida_wrapper.js")
    with open(wrapper_path, "w", encoding="utf-8") as handle:
        handle.write(wrapper_text)
    try:
        argv = build_command(frida_exe, wrapper_path, args.serial, args.package, args.attach)
        print("  frida command: %s" % " ".join(argv))
        returncode, out, _err = run_frida_capture(argv, args.duration_sec)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
    records, malformed = filter_audit_lines(out)
    written = 0
    skipped = len(malformed)
    for lineno, reason in malformed:
        print("WARNING: stdout line %d skipped (%s)" % (lineno, reason), file=sys.stderr)
    for raw in records:
        record, reason = normalize_record(raw)
        if record is None:
            print("WARNING: audit record skipped: %s" % reason, file=sys.stderr)
            skipped += 1
        else:
            append_jsonl(args.out, [record])
            written += 1
    print(
        "done: %d audit line(s), %d written, %d skipped -> %s (frida returncode=%s)"
        % (written + skipped, written, skipped, args.out, returncode)
    )
    if returncode != 0 and written == 0:
        print("ERROR: %s" % HINT_SERVER, file=sys.stderr)
        return 1
    return 0


def run_dry_run(args: argparse.Namespace) -> int:
    """--dry-run: print the command plan, zero frida/device contact, exit 0."""
    try:
        entries = load_manifest(args.manifest)
    except ValueError as error:
        print("collect.py dry-run: manifest error: %s" % error, file=sys.stderr)
        return 2
    try:
        hook_source = HOOK_JS.read_text(encoding="utf-8")
    except OSError:
        print("collect.py dry-run: cannot read %s" % HOOK_JS, file=sys.stderr)
        return 2
    print(
        "collect.py dry-run: package=%s duration=%ss mode=%s out=%s"
        % (args.package, args.duration_sec, "attach" if args.attach else "spawn", args.out)
    )
    print(
        "  frida command: %s"
        % " ".join(build_command("frida", "<tempdir>/frida_wrapper.js", args.serial, args.package, args.attach))
    )
    classes = sorted(set(entry["class"] for entry in entries))
    print("  wrapper: APIS_OVERRIDE=%d apis injected + hook.js (%d lines)" % (len(entries), hook_source.count(chr(10))))
    print("  manifest: %d apis, %d classes from %s" % (len(entries), len(classes), args.manifest))
    print("  package check: adb [-s <serial>] shell pm path <package> (dry: [DRY-RUN] line only)")
    print("  out: append JSONL to %s" % args.out)
    client = AdbClient(serial=args.serial, dry_run=True)
    client.shell(["pm", "path", args.package])
    print("  dry-run: no frida execution, no device contact")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collect.py",
        description="Frida sensitive-API audit collector (M4.5; frida invoked as external CLI, never imported).",
    )
    parser.add_argument(
        "--serial", default=None,
        help="device serial (frida -D <serial>; default frida -U picks the USB device)",
    )
    parser.add_argument("--package", required=True, help="target app package name")
    parser.add_argument(
        "--duration-sec", type=float, default=60.0,
        help="monitoring window in seconds; frida is killed at the deadline (expected end)",
    )
    parser.add_argument("--out", default="audit.jsonl", help="append audit JSONL here (default: audit.jsonl)")
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST),
        help="apis.json path (default: bundled tools/privacy_hook/apis.json)",
    )
    parser.add_argument(
        "--attach", action="store_true",
        help="attach to a running process (-n <package>) instead of spawning (-f)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the command plan and exit 0 without running frida (device check stays dry)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.dry_run:
        return run_dry_run(args)
    return run_collection(args)


if __name__ == "__main__":
    raise SystemExit(main())
