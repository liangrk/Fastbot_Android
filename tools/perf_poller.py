#!/usr/bin/env python3
"""M2 perf-metrics: PC-side performance poller (cpu/mem/net/battery + starts).

Per tick (default 10s) this collects on the target package:
  - dumpsys cpuinfo --checkin
  - dumpsys meminfo <pkg> --checkin
  - dumpsys netstats --checkin
  - dumpsys battery --checkin
via tools/common/adb.py AdbClient, each parsed into one JSONL line conforming
to tools/schemas/perf_sample.schema.json (ts, serial, source=pc required;
numeric fields omitted when the parser cannot extract them - a missing field
is never guessed).

Cold/warm start detection (Architect F4):
  PRIMARY signal is the logcat Displayed line. Each tick reads
  `logcat -d -s ActivityTaskManager:I` and extracts "Displayed" lines for the
  package; every Displayed line carries the pid of the process that rendered
  the activity, so the LOG ITSELF records process identity changes and the
  classification is not limited by the poll window. A new Displayed line with
  a different pid than the previous one = COLD start (the process went through
  disappearance/reappearance); same pid = WARM start. The first Displayed
  line in the buffer window has no in-log predecessor, so it is classified
  "unknown" (best effort; shown as such in the report).

  CORROBORATION ONLY: `pidof <pkg>` is sampled per tick to note the current
  pid in the report, but pid disappearance/reappearance is NOT used as the
  cold/warm decision signal: a 10s polling window misses restart cycles
  shorter than the window (and cannot distinguish LMK kills from crashes).
  If a whole restart fits between two ticks, the poller may miss it entirely;
  the report methodology note says so.

Parsers (parse_cpuinfo / parse_meminfo / parse_netstats / parse_battery /
parse_displayed_lines) are importable pure functions for pytest - no adb
contact happens at import or parse time.

Usage:
    python tools/perf_poller.py --package com.example [--serial SER]
        [--interval 10] [--duration 60] [--out DIR] [--dry-run]

Output layout (default out root tools/out/<run_id>):
    <out>/perf/data.json     JSONL, perf_sample.schema.json lines
    <out>/perf/starts.json   start-detection events (reported too)
    <out>/perf/report.html   Chinese HTML report (via common/report.py)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common.adb import AdbClient, AdbError
from common.report import write_report
from perf_report import render_perf_report

DEFAULT_OUT_ROOT = "tools/out"
DEFAULT_INTERVAL = 10.0
DEFAULT_DURATION = 60.0

PER_TICK_COMMANDS = (
    ("cpuinfo", "dumpsys cpuinfo --checkin"),
    ("meminfo", "dumpsys meminfo {package} --checkin"),
    ("netstats", "dumpsys netstats --checkin"),
    ("battery", "dumpsys battery --checkin"),
    ("displayed", "logcat -d -s ActivityTaskManager:I"),
    ("pidof", "pidof {package}"),
)

START_DETECTION_NOTE = (
    "冷/温启动判定以 logcat Displayed 行为主信号（日志内含 pid，不受轮询窗口限制）；"
    "pidof 仅作佐证：10s 轮询会漏检短于窗口的重启周期，且无法区分 LMK 与崩溃。"
    "若整个重启周期落在两个采样点之间，本轮可能完全漏检，报告按已观测数据如实标注。"
)


def parse_cpuinfo(text: str, package: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Parse `dumpsys cpuinfo --checkin` rows for the package.

    Checkin row layout (ProcessCpuTracker.checkin): uid,percent,name,...
    Returns {"cpu_percent": float} or None (no row / garbage).
    """
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        fields = line.strip().split(",")
        if len(fields) < 3:
            continue
        if package is not None and fields[2].strip() != package:
            continue
        try:
            percent = float(fields[1])
        except ValueError:
            continue
        if percent < 0 or math.isnan(percent) or math.isinf(percent):
            continue
        return {"cpu_percent": percent}
    return None


def parse_meminfo(text: str) -> Optional[Dict[str, Any]]:
    """Parse `dumpsys meminfo <pkg> --checkin` for the app row.

    The app checkin row looks like 9,3,a,<numbers...> (version 9 format,
    "a" marks the application row); the trailing numeric column is the
    total PSS in KB. Returns {"mem_pss_kb": int} or None.
    """
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        fields = line.strip().split(",")
        if len(fields) < 4 or fields[0] != "9" or fields[2] != "a":
            continue
        try:
            numbers = [int(v) for v in fields[3:] if v.strip() != ""]
        except ValueError:
            continue
        if len(numbers) < 8:
            continue
        return {"mem_pss_row_len": len(numbers), "mem_pss_kb": numbers[-1]}
    return None


def parse_netstats(text: str) -> Optional[Dict[str, Any]]:

    """Parse `dumpsys netstats --checkin` history buckets.

    The checkin dump contains "D," continuation lines (NetworkStatsHistory
    buckets):
        D,bucketStart[,activeTime,]rxBytes,rxPackets,txBytes,txPackets[,ops]
    Field positions shift between Android versions (activeTime present or
    absent), so both the 8-field and 7-field layouts are accepted. The
    returned rx/tx are the SUM over all reported buckets - a stable
    device-wide cumulative baseline suitable for rate derivation in the
    report; it is not a per-app counter.
    Returns {"net_rx_bytes": int, "net_tx_bytes": int} or None.
    """

    if not isinstance(text, str):
        return None
    rx_total = tx_total = 0
    seen = 0
    for line in text.splitlines():
        fields = line.strip().split(",")
        if fields[0] != "D" or len(fields) not in (7, 8):
            continue
        try:
            if len(fields) == 8:
                rx, tx = int(fields[3]), int(fields[5])
            else:
                rx, tx = int(fields[2]), int(fields[4])
        except ValueError:
            continue
        if rx < 0 or tx < 0:
            continue
        rx_total += rx
        tx_total += tx
        seen += 1
    if seen == 0:
        return None
    return {"net_rx_bytes": rx_total, "net_tx_bytes": tx_total}


def parse_battery(text: str) -> Optional[Dict[str, Any]]:
    """Parse `dumpsys battery --checkin` (single line).

    Layout (BatteryService checkin): 9,status,health,present,plugged,level,...
    so the level is field index 5. Returns {"battery_pct": int(0..100)} or
    None (absent/garbage/out-of-range values are rejected, never clamped).
    """
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        fields = line.strip().split(",")
        if fields[0] != "9" or len(fields) < 6:
            continue
        try:
            level = int(fields[5])
        except ValueError:
            continue
        if 0 <= level <= 100:
            return {"battery_pct": level}
    return None


def _parse_displayed_duration(token: str) -> Optional[int]:
    """Parse a Displayed trailing duration like +350ms or +1s234ms to ms."""
    body = token[1:] if token.startswith("+") else token
    seconds = 0
    millis = 0
    digits = ""
    for ch in body:
        if ch.isdigit():
            digits += ch
        elif ch == "s":
            seconds = int(digits) if digits else 0
            digits = ""
        elif ch == "m":
            millis = int(digits) if digits else 0
            digits = ""
            break
        else:
            break
    if digits:
        millis = int(digits)
    return seconds * 1000 + millis


def parse_displayed_lines(text: str, package: str) -> List[Dict[str, Any]]:
    """Extract the package Displayed lines from logcat output (PRIMARY
    cold/warm signal, see module docstring).

    A logcat line looks like:
      09-16 10:23:45.612  1234  2345 I ActivityTaskManager: Displayed
      com.pkg/.MainActivity +1s234ms
    Returned records (log order): {"id", "time", "pid", "activity",
    "duration_ms"}; id = time+target and dedups across repeated `logcat -d`
    reads of the same buffer.
    """
    records: List[Dict[str, Any]] = []
    if not isinstance(text, str):
        return records
    for line in text.splitlines():
        if " Displayed " not in line or package not in line:
            continue
        tokens = line.split()
        if len(tokens) < 8 or tokens[5] != "ActivityTaskManager:":
            continue
        if " Displayed " not in line or tokens[6] != "Displayed":
            continue
        pid_token = tokens[2]
        if not pid_token.isdigit():
            continue
        target = tokens[7]
        if not target.startswith(package + ":"):
            if not target.startswith(package + "/"):
                continue
        time_str = tokens[0] + " " + tokens[1]
        records.append({
            "id": time_str + " " + target,
            "time": time_str,
            "pid": int(pid_token),
            "activity": target,
            "duration_ms": _parse_displayed_duration(tokens[8])
            if len(tokens) > 8 else None,
        })
    return records


class StartTracker:
    """Incremental cold/warm classifier over Displayed records.

    Classification rule (Displayed = PRIMARY signal, see module docstring):
    a new Displayed record whose pid differs from the previous processed
    record is a cold start; same pid is warm; the first record in the buffer
    window has no in-log predecessor and is classified "unknown".
    Deduplicates by record id across repeated logcat -d reads.
    """

    def __init__(self) -> None:
        self._seen: set = set()
        self._last_pid: Optional[int] = None
        self._any = False

    def feed(self, records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for record in records:
            if record.get("id") in self._seen:
                continue
            self._seen.add(record.get("id"))
            if not self._any:
                kind = "unknown"
            elif record.get("pid") != self._last_pid:
                kind = "cold"
            else:
                kind = "warm"
            self._any = True
            self._last_pid = record.get("pid")
            events.append({
                "time": record.get("time"),
                "pid": record.get("pid"),
                "activity": record.get("activity"),
                "duration_ms": record.get("duration_ms"),
                "kind": kind,
            })
        return events


def build_plan(package: str, interval: float, duration: float) -> Dict[str, Any]:
    """Return the sampling plan (also rendered by --dry-run)."""
    ticks = max(1, int(math.ceil(float(duration) / float(interval)))) if interval > 0 else 1
    return {
        "package": package,
        "interval_sec": float(interval),
        "duration_sec": float(duration),
        "ticks": ticks,
        "per_tick_commands": [
            {"collector": name, "command": cmd.replace("{package}", package)}
            for name, cmd in PER_TICK_COMMANDS
        ],
        "start_detection": {
            "primary": "logcat -d -s ActivityTaskManager:I -> Displayed lines (per-line pid)",
            "corroboration": "pidof <pkg> per tick (window-limited, misses sub-window restarts)",
            "note": START_DETECTION_NOTE,
        },
    }


def build_sample_line(
    ts_ms: int,
    serial: str,
    cpu: Optional[Dict[str, Any]],
    mem: Optional[Dict[str, Any]],
    net: Optional[Dict[str, Any]],
    battery: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble one perf_sample.schema.json line; absent metrics are omitted
    (schema required keys are ts/serial/source only)."""
    sample: Dict[str, Any] = {"ts": int(ts_ms), "serial": str(serial), "source": "pc"}
    if cpu:
        sample["cpu_percent"] = cpu["cpu_percent"]
    if mem:
        sample["mem_pss_kb"] = mem["mem_pss_kb"]
    if net:
        sample["net_rx_bytes"] = net["net_rx_bytes"]
        sample["net_tx_bytes"] = net["net_tx_bytes"]
    if battery:
        sample["battery_pct"] = battery["battery_pct"]
    return sample


def collect_tick(adb: AdbClient, package: str, tracker: StartTracker) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """One poll tick: run the per-tick commands, parse, classify starts.

    Returns (sample_line_or_None, start_events). When a dumpsys/logcat
    command raises AdbError the whole tick is skipped (returns (None, []));
    pidof failure (rc=1 when the process is absent) degrades to "no
    corroboration" - pidof is corroboration only, never fatal. No exception
    escapes collect_tick.
    """
    try:
        cpu_text = adb.shell("dumpsys cpuinfo --checkin")[1]
        mem_text = adb.shell("dumpsys meminfo %s --checkin" % package)[1]
        net_text = adb.shell("dumpsys netstats --checkin")[1]
        battery_text = adb.shell("dumpsys battery --checkin")[1]
        logcat_text = adb.shell("logcat -d -s ActivityTaskManager:I")[1]
    except AdbError as error:
        print("WARNING: tick failed, skipped: %s" % error, file=sys.stderr)
        return None, []
    pidof_pid = None
    try:
        pidof_text = adb.shell("pidof %s" % package)[1]
        pidof_pid = pidof_text.strip().split()[0] if pidof_text.strip() else None
    except AdbError:
        pidof_pid = None  # process absent (rc=1): corroboration only
    events = tracker.feed(parse_displayed_lines(logcat_text, package))
    for event in events:
        pidof_note = ""
        if pidof_pid is not None and event["pid"] is not None:
            pidof_note = " (佐证 pidof=%s)" % pidof_pid
        print("START: %s %s pid=%s %s%s" % (
            event["kind"], event["time"], event["pid"],
            event["activity"], pidof_note))
    serial = adb.serial if adb.serial else "unknown"
    sample = build_sample_line(
        int(time.time() * 1000),
        serial,
        parse_cpuinfo(cpu_text, package),
        parse_meminfo(mem_text),
        parse_netstats(net_text),
        parse_battery(battery_text),
    )
    return sample, events


def print_plan(plan: Dict[str, Any], out_root: Path) -> None:
    """Render the sampling plan (--dry-run output, no device contact)."""
    print("perf_poller dry-run 采样计划 (无设备接触)")
    print("  目标包名: %s" % plan["package"])
    print("  采样间隔: %gs  持续: %gs  预计 tick 数: %d" % (
        plan["interval_sec"], plan["duration_sec"], plan["ticks"]))
    print("  每 tick 采集命令:")
    for item in plan["per_tick_commands"]:
        print("    - [%s] %s" % (item["collector"], item["command"]))
    print("  冷/温启动检测计划:")
    print("    - 主信号: %s" % plan["start_detection"]["primary"])
    print("    - 佐证:   %s" % plan["start_detection"]["corroboration"])
    print("  方法论: %s" % plan["start_detection"]["note"])
    print("  输出布局: %s" % (out_root / "perf"))
    print("    - data.json   (perf_sample.schema.json JSONL)")
    print("    - starts.json (启动事件)")
    print("    - report.html (中文性能报告)")


def _write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_poller.py",
        description="Fastbot PC-side performance poller (cpu/mem/net/battery + cold/warm starts).",
    )
    parser.add_argument("--serial", default=None, help="device serial (auto-detects single device)")
    parser.add_argument("--package", required=True, help="target package name")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="poll interval seconds (default 10)")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION,
                        help="total collection duration seconds (default 60)")
    parser.add_argument("--out", default=None,
                        help="run output root dir (default tools/out/<run_id>; writes <out>/perf/*)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the sampling plan and exit (no device contact)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    plan = build_plan(args.package, args.interval, args.duration)
    run_id = time.strftime("%Y%m%d_%H%M%S")
    out_root = Path(args.out) if args.out else Path(DEFAULT_OUT_ROOT) / run_id
    if args.dry_run:
        print_plan(plan, out_root)
        return 0
    client = AdbClient(serial=args.serial, timeout=30, retries=2)
    if client.serial is None:
        serials = client.devices()
        if len(serials) == 1:
            client.serial = serials[0]
    tracker = StartTracker()
    samples: List[Dict[str, Any]] = []
    starts: List[Dict[str, Any]] = []
    started = time.monotonic()
    ticks_done = 0
    while True:
        sample, events = collect_tick(client, args.package, tracker)
        if sample is not None:
            samples.append(sample)
            ticks_done += 1
        starts.extend(events)
        elapsed = time.monotonic() - started
        if elapsed >= args.duration:
            break
        time.sleep(min(args.interval, args.duration - elapsed))
    perf_dir = out_root / "perf"
    _write_jsonl(perf_dir / "data.json", samples)
    _write_jsonl(perf_dir / "starts.json", starts)
    html = render_perf_report(samples, starts, meta={
        "package": args.package,
        "serial": client.serial or "unknown",
        "run_id": run_id,
        "note": plan["start_detection"]["note"],
    })
    report_path = perf_dir / "report.html"
    write_report(report_path, html)
    print("perf data written: %s" % (perf_dir / "data.json"))
    print("perf report written: %s" % report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
