#!/usr/bin/env python3
"""M6 llm-protocol: export a GUI state package for an external agent.

The external agent (Claude Code / Codex / pi / omp) drives the closed loop via
the CLI only; no LLM code lives here.  One invocation collects on-device:

  1. current top activity   - parse of `dumpsys activity activities`
  2. declared activities    - parse of `dumpsys package <pkg>` "Activities:"
                              section (the unvisited denominator, Critic
                              MINOR-3); defensive parsing of bracketed /
                              suffixed / "Activity #N:" line shapes
  3. visited activities     - pull of /sdcard/fastbot_coverage/<pkg>.json
                              (written by the M3 coverage exporter; a missing
                              file degrades to visited=[] plus a note)
  4. GUI XML                - PREFERRED: freshest /sdcard/fastbot-<pkg>*/
                              step-*.xml written by max.saveGUITreeToXmlEveryStep
                              (fallback: `uiautomator dump /sdcard/fastbot_gui.xml`)
  5. screenshot             - `screencap -p /sdcard/fastbot_gui.png`

Output (summary.json + gui.xml + screenshot.png) feeds the analysis step of
tools/agent_protocol.md.

Set semantics (visited / unvisited): names are compared after normalization
(relatively-named components such as `.MainActivity` or `MainActivity` are
expanded with the package prefix; bracketed dumpsys tokens are stripped).
Output lists keep the ORIGINAL names as found (coverage names for visited,
dumpsys names for unvisited) and are sorted.

Usage:
    python tools/gui_export.py --package com.example [--serial SER]
        [--out DIR] [--dry-run]

--dry-run prints the export plan with zero device contact.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common.adb import AdbClient, AdbError

_HEADER_RE = re.compile(r"^(\s*)Activities:\s*$")
_COMPONENT_RE = re.compile(r"^\[?\s*([A-Za-z][A-Za-z0-9_]*(?:[.][A-Za-z0-9_$]+)+)")

DEFAULT_OUT_ROOT = "tools/out"
COVERAGE_REMOTE_FMT = "/sdcard/fastbot_coverage/%s.json"
GUI_REMOTE_XML = "/sdcard/fastbot_gui.xml"
GUI_REMOTE_PNG = "/sdcard/fastbot_gui.png"
STEP_XML_GLOB_FMT = "/sdcard/fastbot-%s*/step-*.xml"


def normalize_activity(name: Optional[str], package: str) -> str:
    """Expand a component token to a full activity class name.

    Handles "pkg/.Act", "pkg/Act", ".Act", "Act", "[pkg.Act]" (dumpsys
    brackets) and strips surrounding whitespace.  Already-full names pass
    through unchanged (kept as-is; only comparison uses the expansion).
    """
    name = (name or "").strip().strip("[]").strip()
    if "/" in name:
        name = name.partition("/")[2]
    if name.startswith("."):
        return package + name
    if "." not in name:
        return package + "." + name
    return name





def _extract_component(line: str) -> Optional[str]:
    """Pull a component token out of one dumpsys section line, or None.

    Accepted shapes: com.foo.Bar, [com.foo.Bar], com.foo.Bar (filter null),
    Activity #0: com.foo.Bar.  Anything else yields None.
    """
    text = line.strip()
    if text.startswith("Activity ") or text.startswith("Activity#"):
        text = text.partition(":")[2].strip() if ":" in text else ""
        if not text:
            return None
    if text.startswith("["):
        text = text.strip("[]").strip()
    match = _COMPONENT_RE.match(text)
    return match.group(1) if match else None


def parse_declared_activities(text: str, package: str) -> List[str]:
    """Parse the "Activities:" sections of a `dumpsys package <pkg>` dump.

    Defensive: accepts bracketed entries, "(filter ...)" suffixes and
    "Activity #N: name" lines; section ends at the first non-blank line whose
    indentation is <= the header's.  Returns sorted ORIGINAL component names
    (deduped by normalized form) belonging to the package.
    """
    declared: Dict[str, str] = {}
    header_indent: Optional[int] = None
    for raw in text.splitlines():
        match = _HEADER_RE.match(raw)
        if header_indent is None:
            if match is None:
                continue
            header_indent = len(match.group(1))
            continue
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= header_indent:
            header_indent = len(match.group(1)) if match else None
            continue
        candidate = _extract_component(raw)
        if candidate is None:
            continue
        normalized = normalize_activity(candidate, package)
        if normalized == package or normalized.startswith(package + "."):
            declared.setdefault(normalized, candidate)
    return sorted(declared.values())


def parse_current_activity(text: str, package: str) -> Optional[str]:
    """Extract the resumed/focused activity from `dumpsys activity` output.

    Checks topResumedActivity, mResumedActivity and mCurrentFocus lines (in
    that priority); returns a full component name or None.
    """
    for marker in ("topResumedActivity", "mResumedActivity", "mCurrentFocus"):
        for raw in text.splitlines():
            if marker in raw and "u0 " in raw:
                match = re.search(r"u0\s+(\S+)", raw)
                if match:
                    return normalize_activity(
                        match.group(1).rstrip("}"), package)
    return None


def load_visited_activities(path: Path) -> Tuple[List[str], Optional[str]]:
    """Read visited activity names from a pulled coverage JSON.

    Returns (names, note); note is set (and names empty) when the file is
    missing or unreadable, mirroring the degrade-not-fail contract.
    """
    if not path.is_file():
        return [], "coverage JSON missing on device: %s (visited=[])" % path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = [a.get("name", "") for a in data.get("activities", [])]
        return [n for n in names if n], None
    except (OSError, ValueError) as error:
        return [], "coverage JSON unreadable: %s (%s)" % (path, error)


def build_summary(package: str, activity: Optional[str], visited: List[str],
                  declared: List[str], notes: List[str],
                  ts: Optional[str] = None) -> Dict[str, Any]:
    """Compute the visited/unvisited summary (set difference on normalized
    names; output keeps original names, sorted)."""
    package = package or ""
    visited_map = {normalize_activity(n, package): n.strip("[]") for n in visited}
    declared_map = {normalize_activity(n, package): n.strip("[]") for n in declared}
    unvisited = [declared_map[k] for k in sorted(declared_map)
                 if k not in visited_map]
    visited_out = [visited_map[k] for k in sorted(visited_map)]
    if not declared_map:
        notes.append("declared activities parse produced 0 entries "
                     "(dumpsys format variance); unvisited denominator empty")
    return {
        "package": package,
        "activity": activity or "unknown",
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "visited_activities": visited_out,
        "unvisited_activities": unvisited,
        "visited_count": len(visited_out),
        "unvisited_count": len(unvisited),
        "total_declared": len(declared_map),
        "notes": list(notes),
    }


def _pull_step_xml(client: AdbClient, package: str, out_dir: Path,
                   notes: List[str]) -> Optional[str]:
    """Pull the freshest Fastbot step XML (max.saveGUITreeToXmlEveryStep
    output) into out_dir/gui.xml.  Returns the source label or None."""
    glob = STEP_XML_GLOB_FMT % package
    try:
        rc, stdout, _ = client.shell("ls -t %s 2>/dev/null | head -1" % glob)
    except AdbError:
        return None
    remote = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
    if rc != 0 or not remote.endswith(".xml"):
        return None
    local = out_dir / "gui.xml"
    try:
        client.pull(remote, str(local))
    except AdbError as error:
        notes.append("step XML pull failed (%s); falling back" % error)
        return None
    if local.is_file():
        return "fastbot_step_xml: %s" % remote
    return None


def _dump_uiautomator(client: AdbClient, out_dir: Path,
                      notes: List[str]) -> Optional[str]:
    """Fallback GUI XML: uiautomator dump + pull.  Returns label or None."""
    local = out_dir / "gui.xml"
    try:
        client.shell("uiautomator dump %s" % GUI_REMOTE_XML)
        client.pull(GUI_REMOTE_XML, str(local))
    except AdbError as error:
        notes.append("uiautomator dump failed: %s" % error)
        return None
    if local.is_file():
        return "uiautomator_dump"
    return None


def export(client: AdbClient, package: str, out_dir: Path) -> Dict[str, Any]:
    """Run the on-device export and write summary.json / gui.xml /
    screenshot.png under out_dir.  Every step degrades with a note instead of
    raising; the summary is always written when out_dir is creatable."""
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: List[str] = []
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    activity = None
    try:
        _, dump_text, _ = client.shell("dumpsys activity activities")
        activity = parse_current_activity(dump_text, package)
    except AdbError as error:
        notes.append("dumpsys activity failed: %s" % error)
    if activity is None:
        notes.append("current activity unresolved (marked unknown)")

    declared: List[str] = []
    try:
        _, pkg_text, _ = client.shell("dumpsys package %s" % package)
        declared = parse_declared_activities(pkg_text, package)
    except AdbError as error:
        notes.append("dumpsys package failed: %s" % error)

    coverage_local = out_dir / "coverage.json"
    try:
        client.pull(COVERAGE_REMOTE_FMT % package, str(coverage_local))
    except AdbError:
        pass
    visited, note = load_visited_activities(coverage_local)
    if note:
        notes.append(note)

    gui_source = _pull_step_xml(client, package, out_dir, notes) \
        or _dump_uiautomator(client, out_dir, notes)
    if gui_source is None:
        notes.append("no GUI XML available (both sources failed)")

    screenshot_name = None
    try:
        client.shell("screencap -p %s" % GUI_REMOTE_PNG)
        client.pull(GUI_REMOTE_PNG, str(out_dir / "screenshot.png"))
        if (out_dir / "screenshot.png").is_file():
            screenshot_name = "screenshot.png"
    except AdbError as error:
        notes.append("screencap/pull failed: %s" % error)

    summary = build_summary(package, activity, visited, declared, notes,
                            ts=timestamp)
    summary["gui_xml_source"] = gui_source or "none"
    summary["screenshot"] = screenshot_name
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def print_plan(package: str, serial: Optional[str], out_dir: Path) -> None:
    """--dry-run output: the export plan, zero device contact."""
    print("gui_export dry-run 导出计划 (无设备接触)")
    print("  包名: %s" % package)
    print("  serial: %s" % (serial or "<自动探测>"))
    print("  步骤:")
    print("    1. dumpsys activity activities -> 解析当前 Activity")
    print("    2. dumpsys package <pkg> -> 解析声明 Activity 全集 (未访分母)")
    print("    3. pull %s -> 已访集合 (缺失 -> visited=[] + note)"
         % (COVERAGE_REMOTE_FMT % package))
    print("    4. GUI XML 优先链: /sdcard/fastbot-<pkg>*/step-*.xml 最新一份"
          " (max.saveGUITreeToXmlEveryStep 落盘)")
    print("       兜底: uiautomator dump %s + pull" % GUI_REMOTE_XML)
    print("    5. screencap -p %s + pull -> screenshot.png" % GUI_REMOTE_PNG)
    print("  输出: %s/{summary.json, gui.xml, screenshot.png}" % out_dir)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gui_export.py",
        description="Export a GUI state package for the external agent (M6).",
    )
    parser.add_argument("--serial", default=None,
                        help="device serial (auto-detects single device)")
    parser.add_argument("--package", required=True, help="target package name")
    parser.add_argument("--out", default=None,
                        help="output dir (default tools/out/<ts>/agent_export/<ts>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the export plan and exit (no device contact)")
    args = parser.parse_args(argv)

    export_ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = (Path(args.out) if args.out
               else Path(DEFAULT_OUT_ROOT) / export_ts / "agent_export" / export_ts)
    if args.dry_run:
        print_plan(args.package, args.serial, out_dir)
        return 0
    client = AdbClient(serial=args.serial, timeout=30, retries=2)
    summary = export(client, args.package, out_dir)
    print("package: %s" % summary["package"])
    print("activity: %s" % summary["activity"])
    print("visited: %d  unvisited: %d  declared: %d"
          % (summary["visited_count"], summary["unvisited_count"],
             summary["total_declared"]))
    for note in summary["notes"]:
        print("  note: %s" % note)
    print("summary written: %s" % (out_dir / "summary.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
