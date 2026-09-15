#!/usr/bin/env python3
"""M5 device-matrix: parallel multi-device Fastbot orchestration + aggregate report.

Reads a matrix.json, validates it, then runs Fastbot on each device in
parallel (bounded concurrency, default 5) with per-device failure isolation:

  1. preflight  : reachability probe (adb -s <serial> shell true, the
                  executable equivalent of `adb -s <serial> get-state`)
  2. push       : monkeyq.jar / framework.jar / fastbot-thirdpart.jar -> /sdcard/,
                  generated max.config (profile-driven) -> /sdcard/max.config,
                  libs/<abi>/*.so -> /data/local/tmp/ (abi via getprop)
  3. run        : CLASSPATH=... exec app_process ... Monkey, hard-killed at
                  duration_min + grace; perf_poller.py launched alongside
                  when profile.perf
  4. collect    : pull crash-dump.log / fastbot_perf / fastbot_coverage /
                  fastbot_privacy/audit.jsonl / fastbot_chaos.snapshot +
                  a `logcat -d` dump (ANR counting source)

Layout: tools/out/<run_id>/matrix/<serial>/... and the aggregate Chinese
report at tools/out/<run_id>/matrix/report.html (tools/common/report.py).

Exit codes: 0 = all devices completed; 1 = matrix ran but at least one
device failed or timed out; 2 = usage/validation error (unreadable matrix,
>10 devices, schema violations) with zero device contact.
--dry-run prints the per-device plan and the validation result without any
device contact.

matrix.json schema (see validate_matrix):
    {"run_id": "m1", "devices": [
        {"serial": "...", "apk": "<package name passed to -p>",
         "duration_min": 60,
         "profile": {"chaos": true, "perf": true, "privacy": true}}]}
profile keys: chaos/perf/privacy (booleans); omitted profile = all off.
profile.config (optional): {"max.<key>": "<string value>"} entries merged
verbatim into the generated /sdcard/max.config (e.g. max.chaos.battery.pct,
max.privacy.rules); non "max.*" keys are rejected.
duration_min: optional (default 60); must be a positive number when present.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common.adb import AdbClient, AdbError, locate_adb
from common.report import render_report, write_report
from privacy_report import parse_audit_text

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
PERF_POLLER_SCRIPT = TOOLS_DIR / "perf_poller.py"
DEFAULT_OUT_ROOT = REPO_ROOT / "tools" / "out"

MAX_DEVICES = 10
DEFAULT_MAX_CONCURRENT = 5
DEFAULT_DURATION_MIN = 60
DEFAULT_THROTTLE_MS = 100
DEFAULT_GRACE_SEC = 60
POLLER_EXTRA_GRACE_SEC = 120
PROFILE_KEYS = ("chaos", "perf", "privacy")
PROFILE_CONFIG_KEYS = {
    "chaos": "max.chaos.enable",
    "perf": "max.perf.frame",
    "privacy": "max.privacy.enabled",
}
CLASSPATH_JARS = ("monkeyq.jar", "framework.jar", "fastbot-thirdpart.jar")

# profile.config key whitelist: ^max\.[A-Za-z0-9._]+$
CONFIG_KEY_RE = re.compile(r"^max\.[A-Za-z0-9._]+$")
# a chaos per-state arm ratio: ^max\.chaos\.[A-Za-z0-9._]+\.pct$
CHAOS_PCT_KEY_RE = re.compile(r"^max\.chaos\.[A-Za-z0-9._]+\.pct$")
MAX_CONFIG_TAKEOVER_NOTE = "矩阵运行接管 /sdcard/max.config（生成式覆盖）"
TIMEOUT_CHAOS_RESET_NOTE = (
    "超时强杀可能使设备侧 chaos 状态残留（Monkey finally 未运行）；"
    "已尽力重置 battery，其余通道请对照 fastbot_chaos.snapshot 手工恢复")
DEVICE_ARTIFACTS = (
    ("/sdcard/crash-dump.log", "crash-dump.log"),
    ("/sdcard/fastbot_perf", "fastbot_perf"),
    ("/sdcard/fastbot_coverage", "fastbot_coverage"),
    ("/sdcard/fastbot_privacy/audit.jsonl", "fastbot_privacy/audit.jsonl"),
    ("/sdcard/fastbot_chaos.snapshot", "fastbot_chaos.snapshot"),
)
REPORT_TITLE = "Fastbot 设备矩阵测试报告"
RESULT_LABELS = {"completed": "完成", "failed": "失败", "timeout": "超时"}
CRASH_MARKER = "// CRASH:"
ANR_MARKER = "ANR in"

TABLE_HEADERS = [
    "设备 serial", "结果", "crash 数", "ANR 数",
    "Activity 覆盖", "性能摘要", "隐私事件数", "备注",
]
METHODOLOGY_NOTE = (
    "crash 数 = crash-dump.log 中 '" + CRASH_MARKER + "' 出现次数；"
    "ANR 数 = logcat 转储中 '" + ANR_MARKER + "' 出现次数（logcat 未采集时计 0）；"
    "Activity 覆盖 = fastbot_coverage JSON 的 activities 数组长度（矩阵 profile 不含"
    " coverage 开关，需设备侧另行开启 max.coverage.exportWidgetLevel）；"
    "性能摘要 = perf/data.json 的 CPU/内存均值 + starts.json 冷/温启动计数；"
    "隐私事件数 = fastbot_privacy/audit.jsonl 事件行数；"
    + MAX_CONFIG_TAKEOVER_NOTE + "。"
    "产物缺失时显示 '—' 并在备注注明。"
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_matrix(data: Any,
                    warnings: Optional[List[str]] = None) -> List[str]:
    """Schema-style validation of the parsed matrix.json.

    Returns a list of error strings; empty list means valid.
    Checks: JSON object shape, devices list non-empty and <= MAX_DEVICES,
    per-device serial/apk presence, positive duration_min, profile keys
    limited to chaos/perf/privacy plus an optional "config" dict of
    "max.*" -> string entries, duplicate serials.
    Advisory problems (chaos/privacy armed without their required config
    keys) are appended to `warnings` (when a list is given) instead of
    rejecting the matrix.
    """
    errors: List[str] = []

    def _warn(message: str) -> None:
        if warnings is not None:
            warnings.append(message)
    if not isinstance(data, dict):
        return ["matrix.json must be a JSON object"]
    run_id = data.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        errors.append("run_id must be a non-empty string when present")
    devices = data.get("devices")
    if not isinstance(devices, list):
        errors.append("devices must be a list")
        return errors
    if not devices:
        errors.append("devices is empty; at least one device is required")
        return errors
    if len(devices) > MAX_DEVICES:
        errors.append(
            "%d devices exceed the maximum of %d" % (len(devices), MAX_DEVICES))
        return errors
    serials: List[str] = []
    for index, device in enumerate(devices):
        prefix = "devices[%d]" % index
        if not isinstance(device, dict):
            errors.append("%s must be a JSON object" % prefix)
            continue
        serial = device.get("serial")
        if not isinstance(serial, str) or not serial.strip():
            errors.append("%s.serial must be a non-empty string" % prefix)
        else:
            serials.append(serial)
        apk = device.get("apk")
        if not isinstance(apk, str) or not apk.strip():
            errors.append("%s.apk must be a non-empty string" % prefix)
        duration = device.get("duration_min", DEFAULT_DURATION_MIN)
        if not _is_number(duration) or duration <= 0:
            errors.append("%s.duration_min must be a positive number" % prefix)
        profile = device.get("profile")
        if profile is None:
            profile = {}
        if not isinstance(profile, dict):
            errors.append("%s.profile must be a JSON object" % prefix)
            continue
        for key, value in profile.items():
            if key == "config":
                continue
            if key not in PROFILE_KEYS:
                errors.append(
                    "%s.profile has unknown key %r (known: %s + config)"
                    % (prefix, key, ", ".join(PROFILE_KEYS)))
            elif value is not True and value is not False:
                errors.append(
                    "%s.profile.%s must be a boolean, got %r" % (prefix, key, value))
        config = profile.get("config")
        config_ok = isinstance(config, dict)
        if config is not None and not config_ok:
            errors.append("%s.profile.config must be a JSON object" % prefix)
        elif config_ok:
            for ckey in sorted(config):
                cval = config[ckey]
                if not CONFIG_KEY_RE.match(ckey):
                    errors.append(
                        "%s.profile.config has invalid key %r"
                        r" (must match ^max\.[A-Za-z0-9._]+$)" % (prefix, ckey))
                if not isinstance(cval, str):
                    errors.append(
                        "%s.profile.config.%s must be a string, got %r"
                        % (prefix, ckey, cval))
        if profile.get("chaos") is True and (
                not config_ok
                or not any(CHAOS_PCT_KEY_RE.match(k) for k in config)):
            _warn("%s: chaos=true 但未提供任何 max.chaos.<state>.pct — 注入将不会发生" % prefix)
        if profile.get("privacy") is True and (
                not config_ok or "max.privacy.rules" not in config):
            _warn("%s: privacy=true 但未提供 max.privacy.rules — 规则引擎将不生效" % prefix)
    duplicates = sorted({s for s in serials if serials.count(s) > 1})
    for serial in duplicates:
        errors.append("duplicate device serial: %s" % serial)
    return errors


def _profile_of(device: Dict[str, Any]) -> Dict[str, bool]:
    """Normalize a device entry into {chaos, perf, privacy} booleans."""
    profile = device.get("profile") or {}
    return {key: bool(profile.get(key, False)) for key in PROFILE_KEYS}


def build_max_config(profile: Dict[str, bool],
                     config: Optional[Dict[str, str]] = None) -> str:
    """max.config text: one key per enabled profile flag, then the
    profile.config entries merged verbatim (sorted by key for determinism).

    Disabled flags are omitted entirely so the device-side defaults (all
    extensions off) stay in effect - zero-regression principle P2.
    """
    lines = ["# generated by tools/matrix_runner.py (profile-driven)"]
    for key in PROFILE_KEYS:
        if profile.get(key):
            lines.append("%s=true" % PROFILE_CONFIG_KEYS[key])
    for ckey in sorted(config or {}):
        lines.append("%s=%s" % (ckey, config[ckey]))
    return chr(10).join(lines) + chr(10)


def build_fastbot_command(package: str, duration_min: float, throttle_ms: int) -> str:
    """The CLASSPATH exec app_process Fastbot command (README run pattern)."""
    classpath = ":".join("/sdcard/" + name for name in CLASSPATH_JARS)
    return (
        "CLASSPATH=%s exec app_process /system/bin com.android.commands.monkey.Monkey "
        "-p %s --agent reuseq --running-minutes %d --throttle %d -v -v"
        % (classpath, package, int(duration_min), int(throttle_ms))
    )


def outcome_from_rc(rc: Optional[int]) -> str:
    """Map a run exit code to completed | crashed | timeout."""
    if rc is None:
        return "timeout"
    return "completed" if rc == 0 else "crashed"


def build_device_plan(device: Dict[str, Any], opts: Dict[str, Any]) -> Dict[str, Any]:
    """Per-device execution plan (also rendered by --dry-run)."""
    serial = device["serial"]
    profile = _profile_of(device)
    profile_config = (device.get("profile") or {}).get("config") or {}
    duration_min = device.get("duration_min", DEFAULT_DURATION_MIN)
    device_out = opts["out_root"] / opts["run_id"] / "matrix" / serial
    pushes = [
        (str(opts["monkeyq"]), "/sdcard/monkeyq.jar"),
        (str(opts["framework"]), "/sdcard/framework.jar"),
        (str(opts["thirdpart"]), "/sdcard/fastbot-thirdpart.jar"),
    ]
    pulls = [
        (remote, "matrix/%s/%s" % (serial, local_rel))
        for remote, local_rel in DEVICE_ARTIFACTS
    ]
    return {
        "serial": serial,
        "package": device["apk"],
        "duration_min": duration_min,
        "profile": profile,
        "max_config": build_max_config(profile, profile_config),
        "max_config_local": str(device_out / "max.config"),
        "command": build_fastbot_command(
            device["apk"], duration_min, opts["throttle_ms"]),
        "pushes": pushes,
        "libs_dir": str(opts["libs_dir"]),
        "pulls": pulls,
        "perf_poller": profile["perf"],
        "timeout_sec": duration_min * 60 + opts["grace_sec"],
        "device_out": str(device_out),
    }


def _poller_argv(plan: Dict[str, Any], device_out: str) -> List[str]:
    """Command line for the alongside perf_poller.py subprocess."""
    return [
        sys.executable, str(PERF_POLLER_SCRIPT),
        "--serial", plan["serial"],
        "--package", plan["package"],
        "--interval", "10",
        "--duration", str(int(plan["duration_min"] * 60)),
        "--out", device_out,
    ]


def print_plans(plans: Sequence[Dict[str, Any]], opts: Dict[str, Any]) -> None:
    """Render the per-device plans (--dry-run output; no device contact)."""
    print("matrix_runner dry-run 执行计划 (无设备接触)")
    print("run_id: %s" % opts["run_id"])
    print("设备数: %d  并发上限: %d" % (len(plans), opts["max_concurrent"]))
    for plan in plans:
        profile = plan["profile"]
        flags = " ".join(
            "%s=%s" % (key, "on" if profile[key] else "off")
            for key in PROFILE_KEYS)
        print("--- 设备 %s ---" % plan["serial"])
        print("  包名: %s  时长: %s min  (超时强杀: + %ds grace)"
              % (plan["package"], plan["duration_min"], opts["grace_sec"]))
        print("  profile: %s" % flags)
        print("  生成 max.config -> /sdcard/max.config:")
        for line in plan["max_config"].strip().splitlines():
            print("      %s" % line)
        print("  注: " + MAX_CONFIG_TAKEOVER_NOTE)
        print("  推送:")
        for local, remote in plan["pushes"]:
            print("    - %s -> %s" % (local, remote))
        print("    - libs/<abi>/*.so -> /data/local/tmp/"
              " (ABI 经 getprop ro.product.cpu.abi 检测, 来自 %s)" % plan["libs_dir"])
        print("  执行命令:")
        print("    %s" % plan["command"])
        if plan["perf_poller"]:
            print("  perf_poller: %s" % " ".join(_poller_argv(plan, plan["device_out"])))
        else:
            print("  perf_poller: (off, 不启动)")
        print("  采集 (pull):")
        for remote, local_rel in plan["pulls"]:
            print("    - %s -> %s" % (remote, local_rel))
        print("    - logcat -d -> matrix/%s/logcat.txt" % plan["serial"])


def count_crashes(path) -> int:
    """Number of crash markers in crash-dump.log (missing/garbage -> 0)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return text.count(CRASH_MARKER)


def count_anrs(path) -> int:
    """Number of 'ANR in' occurrences in the collected logcat dump
    (missing/garbage -> 0)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return text.count(ANR_MARKER)


def coverage_activity_count(coverage_dir) -> Optional[int]:
    """activities count from the first parseable fastbot_coverage/*.json
    (None when the artifact is missing/unparsable)."""
    directory = Path(coverage_dir)
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.glob("*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        activities = data.get("activities") if isinstance(data, dict) else None
        if isinstance(activities, list):
            return len(activities)
    return None


def privacy_event_count(audit_path) -> Tuple[Optional[int], int]:
    """(event count, malformed line count) from audit.jsonl via the
    privacy_report parser; (None, 0) when the artifact is missing."""
    try:
        text = Path(audit_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, 0
    records, malformed = parse_audit_text(text)
    return len(records), len(malformed)


def _avg(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def perf_summary(perf_dir) -> Optional[str]:
    """Performance summary string from perf/data.json + starts.json
    (avg CPU/memory, cold/warm start counts). None when data.json missing
    or entirely unparsable; '-' when present but without usable numbers."""
    data_path = Path(perf_dir) / "data.json"
    if not data_path.is_file():
        return None
    samples: List[Dict[str, Any]] = []
    try:
        for line in data_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                samples.append(record)
    except OSError:
        return None
    cpu = _avg([s["cpu_percent"] for s in samples
                if isinstance(s.get("cpu_percent"), (int, float))])
    mem = _avg([s["mem_pss_kb"] for s in samples
                if isinstance(s.get("mem_pss_kb"), (int, float))])
    cold = warm = None
    starts_path = Path(perf_dir) / "starts.json"
    if starts_path.is_file():
        cold = warm = 0
        try:
            for line in starts_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("kind") == "cold":
                    cold += 1
                elif event.get("kind") == "warm":
                    warm += 1
        except OSError:
            pass
    parts: List[str] = []
    if cpu is not None:
        parts.append("CPU均值 %.1f%%" % cpu)
    if mem is not None:
        parts.append("内存均值 %dKB" % int(mem))
    if cold is not None:
        parts.append("冷启动 %d / 温启动 %d" % (cold, warm))
    return ", ".join(parts) if parts else "-"


def summarize_device(device_out) -> Dict[str, Any]:
    """Parse a per-device artifact directory into report cells + notes."""
    out = Path(device_out)
    crash = count_crashes(out / "crash-dump.log")
    logcat_path = out / "logcat.txt"
    anr = count_anrs(logcat_path)
    coverage = coverage_activity_count(out / "fastbot_coverage")
    perf = perf_summary(out / "perf")
    privacy, malformed = privacy_event_count(
        out / "fastbot_privacy" / "audit.jsonl")
    notes: List[str] = []
    if not (out / "crash-dump.log").is_file():
        notes.append("crash-dump.log 未采集")
    if not logcat_path.is_file():
        notes.append("logcat 未采集 (ANR 计 0)")
    if coverage is None:
        notes.append("coverage 产物缺失")
    if perf is None:
        notes.append("perf 产物缺失")
    if privacy is None:
        notes.append("audit.jsonl 未采集")
    elif malformed:
        notes.append("audit.jsonl 含 %d 条坏行" % malformed)
    return {
        "crash": crash, "anr": anr, "coverage": coverage,
        "perf": perf, "privacy": privacy, "notes": notes,
    }


def _table(title: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    from html import escape
    lines = ["<h2>%s</h2>" % escape(title), "<table>", "<thead><tr>"]
    for header in headers:
        lines.append("<th>%s</th>" % escape(header))
    lines.append("</tr></thead><tbody>")
    for row in rows:
        lines.append("<tr>%s</tr>" % "".join(
            "<td>%s</td>" % escape(str(cell)) for cell in row))
    lines.append("</tbody></table>")
    return chr(10).join(lines)


def _cell(value: Any) -> Any:
    """Report cell: '—' placeholder for missing values."""
    return "—" if value is None else value


def build_matrix_report(
    records: Sequence[Dict[str, Any]],
    run_id: str,
    max_concurrent: int,
) -> str:
    """Aggregate Chinese HTML report, one row per device (via common/report.py)."""
    rows: List[List[Any]] = []
    for record in records:
        summary = summarize_device(record["device_out"])
        notes = list(summary["notes"]) + list(record.get("notes") or [])
        if record.get("error"):
            notes.append(record["error"])
        rows.append([
            record["serial"],
            RESULT_LABELS.get(record["result"], record["result"]),
            summary["crash"],
            summary["anr"],
            _cell(summary["coverage"]),
            _cell(summary["perf"]),
            _cell(summary["privacy"]),
            "; ".join(notes) if notes else "-",
        ])
    completed = sum(1 for r in records if r["result"] == "completed")
    failed = sum(1 for r in records if r["result"] == "failed")
    timed_out = sum(1 for r in records if r["result"] == "timeout")
    sections = [
        {"type": "summary", "rows": [
            ["运行 ID", run_id],
            ["设备数", len(records)],
            ["完成 / 失败 / 超时", "%d / %d / %d" % (completed, failed, timed_out)],
            ["并发上限", max_concurrent],
        ]},
        {"type": "html", "body": _table("设备结果汇总", TABLE_HEADERS, rows)},
        {"type": "html", "body": "<h2>统计口径说明</h2><p>%s</p>" % METHODOLOGY_NOTE},
    ]
    return render_report(REPORT_TITLE, sections)


def _preflight(client: AdbClient) -> None:
    """Reachability probe: the executable equivalent of
    `adb -s <serial> get-state`; raises AdbError when unreachable."""
    rc, _out, _err = client.shell("true")
    if rc != 0:
        raise AdbError("preflight probe failed rc=%s" % rc)


def _push_artifacts(client: AdbClient, plan: Dict[str, Any],
                    device_out: Path, record: Dict[str, Any]) -> None:
    """Push jars, the generated max.config, and the ABI-matched native libs."""
    for local, remote in plan["pushes"]:
        local_path = Path(local)
        if not local_path.is_file():
            raise AdbError("artifact not found: %s" % local_path)
        client.push(str(local_path), remote)
    (device_out / "max.config").write_text(
        plan["max_config"], encoding="utf-8")
    client.push(plan["max_config_local"], "/sdcard/max.config")
    rc, abi_out, _err = client.shell("getprop ro.product.cpu.abi")
    abi = (abi_out or "").strip().split()
    if rc != 0 or not abi:
        raise AdbError("cannot determine device ABI (getprop ro.product.cpu.abi)")
    libs_dir = Path(plan["libs_dir"]) / abi[0]
    if not libs_dir.is_dir():
        raise AdbError("native libs dir not found for abi %s: %s" % (abi[0], libs_dir))
    for so_path in sorted(libs_dir.glob("*.so")):
        client.push(str(so_path), "/data/local/tmp/" + so_path.name)


def _collect(client: AdbClient, device_out: Path, record: Dict[str, Any]) -> None:
    """Best-effort collection: logcat dump + the five device artifacts.
    Pull failures are warned and recorded as missing - never fatal."""
    try:
        rc, text, _err = client.shell("logcat -d")
        if rc == 0 and text.strip():
            (device_out / "logcat.txt").write_text(
                text, encoding="utf-8", errors="replace")
        else:
            record["missing"].append("logcat")
    except (AdbError, OSError):
        record["missing"].append("logcat")
    for remote, local_rel in DEVICE_ARTIFACTS:
        target = device_out / local_rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            client.pull(remote, str(target))
        except (AdbError, OSError) as error:
            record["missing"].append(local_rel)
            record["notes"].append(
                ("pull 失败 %s: %.120s" % (local_rel, error)).rstrip())


def _run_with_timeout(argv: Sequence[str], timeout_sec: float, log_path) -> Optional[int]:
    """Run argv as a subprocess writing stdout+stderr to log_path; hard-kill
    at timeout. Returns the exit code, or None when killed on timeout."""
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(
            list(argv), stdout=log, stderr=subprocess.STDOUT)
        try:
            return proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return None


def _launch_fastbot(adb_path: str, serial: str, command: str,
                    timeout_sec: float, log_path) -> Optional[int]:
    """Run the Fastbot command on the device; bounded by duration+grace."""
    argv = [adb_path, "-s", serial, "shell", command]
    return _run_with_timeout(argv, timeout_sec, log_path)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        print("WARNING: cannot write %s: %s" % (path, error), file=sys.stderr)


def _reset_chaos_after_timeout(client: AdbClient,
                               record: Dict[str, Any]) -> None:
    """Best-effort device-side chaos reset after a timeout hard-kill
    (the Monkey finally block never ran, so device state may persist)."""
    try:
        client.shell("dumpsys battery reset")
    except (AdbError, OSError) as error:
        record["notes"].append("battery reset 失败: %.120s" % error)
    record["notes"].append(TIMEOUT_CHAOS_RESET_NOTE)


def run_device(plan: Dict[str, Any], opts: Dict[str, Any]) -> Dict[str, Any]:
    """Full per-device pipeline. Never raises: any exception inside is
    recorded on this device's record only (failure isolation)."""
    record: Dict[str, Any] = {
        "serial": plan["serial"],
        "package": plan["package"],
        "duration_min": plan["duration_min"],
        "profile": plan["profile"],
        "result": "completed",
        "exit_code": None,
        "error": None,
        "missing": [],
        "notes": [],
        "device_out": plan["device_out"],
    }
    device_out = Path(plan["device_out"])
    device_out.mkdir(parents=True, exist_ok=True)
    try:
        client = AdbClient(serial=plan["serial"], timeout=30, retries=1)
        try:
            _preflight(client)
        except AdbError as error:
            record["result"] = "failed"
            record["error"] = "preflight: %s" % error
            record["notes"].append("采集跳过: 设备不可达")
            return record
        _push_artifacts(client, plan, device_out, record)
        poller_thread = None
        poller_rc: Dict[str, Optional[int]] = {}
        if plan["profile"]["perf"]:
            poller_argv = _poller_argv(plan, str(device_out))
            poller_thread = threading.Thread(
                target=lambda: poller_rc.update(
                    {"rc": _run_with_timeout(
                        poller_argv, plan["timeout_sec"] + POLLER_EXTRA_GRACE_SEC,
                        device_out / "perf_poller.log")}),
                name="poller-%s" % plan["serial"])
            poller_thread.start()
        rc = _launch_fastbot(
            locate_adb(), plan["serial"], plan["command"],
            plan["timeout_sec"], device_out / "run.log")
        outcome = outcome_from_rc(rc)
        if outcome == "timeout":
            record["result"] = "timeout"
            record["notes"].append("duration+grace 超时强杀")
            _reset_chaos_after_timeout(client, record)
        elif outcome == "crashed":
            record["result"] = "failed"
            record["exit_code"] = rc
            record["notes"].append("Fastbot 非零退出 rc=%s" % rc)
        else:
            record["exit_code"] = rc
        if poller_thread is not None:
            poller_thread.join(plan["timeout_sec"] + POLLER_EXTRA_GRACE_SEC + 30)
            if poller_rc.get("rc") is None:
                record["notes"].append("perf_poller 超时被杀")
        _collect(client, device_out, record)
    except Exception as error:  # failure isolation: one device never affects others
        record["result"] = "failed"
        record["error"] = "%s: %s" % (type(error).__name__, error)
    finally:
        _write_json(device_out / "device_run.json", record)
    return record


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="matrix_runner.py",
        description="Fastbot multi-device matrix orchestration (M5).",
    )
    parser.add_argument("--matrix", required=True, help="path to matrix.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="print per-device plans + validation, no device contact")
    parser.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT,
                        help="max devices run in parallel (default 5)")
    parser.add_argument("--throttle-ms", type=int, default=DEFAULT_THROTTLE_MS,
                        help="monkey --throttle ms (default 100)")
    parser.add_argument("--grace-sec", type=int, default=60,
                        help="extra seconds before hard kill (default 60)")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT),
                        help="output root (default tools/out)")
    parser.add_argument("--monkeyq", default=str(REPO_ROOT / "monkeyq.jar"),
                        help="local monkeyq.jar path")
    parser.add_argument("--thirdpart", default=str(REPO_ROOT / "fastbot-thirdpart.jar"),
                        help="local fastbot-thirdpart.jar path")
    parser.add_argument("--framework", default=str(REPO_ROOT / "framework.jar"),
                        help="local framework.jar path")
    parser.add_argument("--libs-dir", default=str(REPO_ROOT / "libs"),
                        help="native libs root containing <abi>/*.so")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        data = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print("ERROR: cannot read matrix %s: %s" % (args.matrix, error),
              file=sys.stderr)
        return 2
    warnings: List[str] = []
    errors = validate_matrix(data, warnings)
    for item in warnings:
        print("WARNING: " + item, file=sys.stderr)
    gate = "validation" if args.dry_run else "execution"
    if errors:
        for item in errors:
            print("ERROR: " + item, file=sys.stderr)
        print("RESULT: INVALID matrix (%d error(s)); 未接触任何设备" % len(errors),
              file=sys.stderr)
        return 2
    opts = {
        "run_id": data.get("run_id") or ("run_" + time.strftime("%Y%m%d_%H%M%S")),
        "out_root": Path(args.out_root),
        "throttle_ms": args.throttle_ms,
        "grace_sec": args.grace_sec,
        "monkeyq": args.monkeyq,
        "thirdpart": args.thirdpart,
        "framework": args.framework,
        "libs_dir": args.libs_dir,
        "max_concurrent": max(1, args.max_concurrent),
    }
    plans = [build_device_plan(device, opts) for device in data["devices"]]
    if args.dry_run:
        print_plans(plans, opts)
        print("校验: OK (%d 台设备; 无设备接触)" % len(plans))
        return 0
    run_dir = opts["out_root"] / opts["run_id"] / "matrix"
    run_dir.mkdir(parents=True, exist_ok=True)
    workers = min(opts["max_concurrent"], len(plans))
    records: List[Dict[str, Any]] = []
    by_serial: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_device, plan, opts): plan["serial"]
                   for plan in plans}
        for future in as_completed(futures):
            record = future.result()
            by_serial[record["serial"]] = record
            print("[%s] %s" % (record["serial"], record["result"]), flush=True)
    for plan in plans:
        records.append(by_serial[plan["serial"]])
    html = build_matrix_report(records, run_id=opts["run_id"],
                               max_concurrent=opts["max_concurrent"])
    report_path = run_dir / "report.html"
    write_report(report_path, html)
    counts = {"completed": 0, "failed": 0, "timeout": 0}
    for record in records:
        counts[record["result"]] += 1
    print("matrix: %d 完成 / %d 失败 / %d 超时" % (
        counts["completed"], counts["failed"], counts["timeout"]))
    print("matrix report written: %s" % report_path)
    return 0 if counts["failed"] + counts["timeout"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
