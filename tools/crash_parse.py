"""Pure parsing/clustering functions for Fastbot crash-dump.log + logcat.

    crash:  (or anr:)
    // CRASH: <process> (pid <pid>) (dump time: <yyyy-MM-dd HH:mm:ss>
    // Version: <versionCode>
    // Long Msg: <stackTrace with newlines rewritten to "\n// ">
    // \tat ... frame lines
    crash end  (or anr end)

Truncated tails (missing "crash end"/"anr end") are kept as records.
logcat input: FATAL EXCEPTION blocks (AndroidRuntime) + "ANR in" lines
(ActivityManager); timestamp-free brief format yields time "".

Determinism contract: no IO, no clock; every output collection is sorted,
so reruns are byte-identical (PYTHONHASHSEED-immune).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

TIME_RE = re.compile(r"^\d{14}$")
HEADER_RE = re.compile(r"^// (?:CRASH|ANR): (\S+) \(pid (\d+)\)")
VERSION_RE = re.compile(r"^// Version: (\d+)")
LONG_MSG_RE = re.compile(r"^// Long Msg: (.*)$")
FRAME_RE = re.compile(r"^\s*(?://\s*)?at\s+\S+\(")
LOGCAT_LINE_RE = re.compile(r"^([VEDIW])/[\w.]+\([^)]*\):\s?(.*)$")
LOGCAT_TS_RE = re.compile(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*")
ANR_IN_RE = re.compile(r"ANR in (\S+)")
ACTIVITY_RE = re.compile(r"(?:^|\s)Activity:\s+(\S+)")

LINE_NO_RE = re.compile(r":\d+\)")
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{4,}")
AT_PREFIX_RE = re.compile(r"^\s*(?://\s*)?at\s+")

GLUE_PREFIXES = (
    "java.lang.reflect.",
    "dalvik.system.",
    "android.os.Looper.",
    "android.os.Handler.",
    "android.os.AsyncTask",
    "android.app.ActivityThread.",
    "com.android.internal.os.",
)

MARKERS = ("crash:", "anr:")


def split_records(text: str) -> List[str]:
    """Cut a crash-dump.log text into record texts.

    A record starts at a 14-digit timestamp line followed by a crash:/anr:
    marker line.  The final record may be truncated (no crash end).
    """
    lines = text.splitlines()
    starts = [
        i
        for i in range(len(lines))
        if TIME_RE.match(lines[i])
        and i + 1 < len(lines)
        and lines[i + 1].strip() in MARKERS
    ]
    records = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        records.append("\n".join(lines[start:end]))
    return records


def exception_type(exception_line: str) -> str:
    """Type token of an exception line (before the first ':')."""
    return exception_line.split(":", 1)[0].strip()


def extract_activities(line: str) -> List[str]:
    """Activity tokens from one line: ANR in <proc>/<act> or Activity: <pkg>."""
    found: List[str] = []
    m = ANR_IN_RE.search(line)
    if m and "/" in m.group(1):
        found.append(m.group(1))
    a = ACTIVITY_RE.search(line)
    if a:
        found.append(a.group(1))
    return found


def normalize_frame(frame: str) -> str:
    """Normalize one 'at ...' frame; '' when the frame must be skipped
    (non-frame lines and glue-prefix frames)."""
    text = frame.strip()
    if not FRAME_RE.match(text):
        return ""
    body = AT_PREFIX_RE.sub("", text)
    if body.startswith(GLUE_PREFIXES):
        return ""
    body = LINE_NO_RE.sub(")", body)
    body = ADDR_RE.sub("0xADDR", body)
    return "at " + body


def normalize_stack(frames: List[str], max_frames: int = 8) -> List[str]:
    """Keep+normalize at-frames, drop glue, cap at max_frames."""
    out: List[str] = []
    for frame in frames:
        norm = normalize_frame(frame)
        if not norm:
            continue
        out.append(norm)
        if len(out) >= max_frames:
            break
    return out


def signature_of(exception_type: str, norm_frames: List[str]) -> str:
    """sha1(type + '\n' + frames)[:16]; the volatile message is excluded."""
    payload = exception_type + "\n" + "\n".join(norm_frames)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def parse_record(rec_text: str) -> Optional[Dict[str, Any]]:
    """Parse one crash-dump record into a plain dict; None without a time."""
    lines = rec_text.splitlines()
    time_s = ""
    kind = "crash"
    for i, ln in enumerate(lines):
        if TIME_RE.match(ln.strip()):
            time_s = ln.strip()
            if i + 1 < len(lines) and lines[i + 1].strip() == "anr:":
                kind = "anr"
            break
    if not time_s:
        return None
    process = None
    pid = None
    version_code = None
    exception_line = ""
    frames: List[str] = []
    activities: List[str] = []
    for ln in lines:
        m = HEADER_RE.match(ln)
        if m and process is None:
            process = m.group(1)
            pid = int(m.group(2))
            continue
        vm = VERSION_RE.match(ln)
        if vm and version_code is None:
            version_code = int(vm.group(1))
            continue
        lm = LONG_MSG_RE.match(ln)
        if lm and not exception_line:
            exception_line = lm.group(1).strip()
            continue
        if FRAME_RE.match(ln):
            frames.append(ln.strip())
            continue
        activities.extend(extract_activities(ln))
    if not exception_line and kind == "anr":
        m = ANR_IN_RE.search(rec_text)
        token = m.group(1) if m else (process or "unknown")
        exception_line = "ANR in " + token
    return {
        "kind": kind,
        "time": time_s,
        "process": process,
        "pid": pid,
        "version_code": version_code,
        "exception_line": exception_line,
        "frames": frames,
        "activities": activities,
        "source": "crash_dump",
    }


def _tag_blocks(lines: List[str]) -> List[List[tuple]]:
    """Group logcat lines into maximal same-tag (raw, msg) runs."""
    blocks: List[List[tuple]] = []
    current: List[tuple] = []
    current_tag = None
    for raw in lines:
        ts = LOGCAT_TS_RE.match(raw)
        payload = raw[ts.end():].lstrip() if ts else raw
        m = LOGCAT_LINE_RE.match(payload)
        if not m:
            if current:
                blocks.append(current)
            current = []
            current_tag = None
            continue
        tag = m.group(1)
        if tag != current_tag:
            if current:
                blocks.append(current)
            current = []
        current_tag = tag
        current.append((raw, m.group(2)))
    if current:
        blocks.append(current)
    return blocks


def _logcat_time(raw: str) -> str:
    """'MM-DD HH:MM:SS' prefix -> '0000MMDDHHMMSS' ('' when absent)."""
    m = LOGCAT_TS_RE.match(raw)
    if not m:
        return ""
    return "0000" + m.group(1) + m.group(2) + m.group(3) + m.group(4) + m.group(5)


PROCESS_PID_RE = re.compile(r"^Process: (\S+), PID: (\d+)")


def _parse_fatal_block(block: List[tuple]) -> Dict[str, Any]:
    time_s = ""
    process = None
    pid = None
    exception_line = ""
    frames: List[str] = []
    for raw, msg in block:
        if "FATAL EXCEPTION" in msg:
            time_s = _logcat_time(raw)
            continue
        pm = PROCESS_PID_RE.match(msg)
        if pm and process is None:
            process = pm.group(1)
            pid = int(pm.group(2))
            continue
        if not exception_line and msg.strip() and not FRAME_RE.match(msg):
            exception_line = msg.strip()
            continue
        if FRAME_RE.match(msg):
            frames.append(msg.strip())
    return {
        "kind": "crash",
        "time": time_s,
        "process": process,
        "pid": pid,
        "version_code": None,
        "exception_line": exception_line or "unknown",
        "frames": frames,
        "activities": [],
        "source": "logcat",
    }


def parse_logcat(text: str) -> List[Dict[str, Any]]:
    """FATAL EXCEPTION blocks + ANR-in lines from logcat text."""
    records: List[Dict[str, Any]] = []
    for block in _tag_blocks(text.splitlines()):
        msgs = [msg for _raw, msg in block]
        has_fatal = any("FATAL EXCEPTION" in msg for msg in msgs)
        if has_fatal:
            records.append(_parse_fatal_block(block))
            continue
        for raw, msg in block:
            m = ANR_IN_RE.search(msg)
            if not m:
                continue
            token = m.group(1)
            records.append({
                "kind": "anr",
                "time": _logcat_time(raw),
                "process": token.split("/")[0],
                "pid": None,
                "version_code": None,
                "exception_line": "ANR in " + token,
                "frames": [],
                "activities": extract_activities(msg),
                "source": "logcat",
            })
    return records


def parse_dump_records(text: str) -> List[Dict[str, Any]]:
    """split_records + parse_record over a crash-dump.log text."""
    parsed = (parse_record(rec) for rec in split_records(text))
    return [rec for rec in parsed if rec is not None]


def stack_text(record: Dict[str, Any]) -> str:
    """exception_line + raw frames, the record's evidence text."""
    parts = [record["exception_line"]] + list(record["frames"])
    return "\n".join(p for p in parts if p)


def group_by_signature(records, max_frames: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    """sig -> records; the single grouping source for cluster()/root_cause."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        etype = exception_type(rec["exception_line"])
        norm = normalize_stack(rec["frames"], max_frames)
        sig = signature_of(etype, norm)
        groups.setdefault(sig, []).append(rec)
    return groups


def cluster(records: List[Dict[str, Any]], max_frames: int = 8) -> List[Dict[str, Any]]:
    """Deterministic cluster rows: order (-count, signature)."""
    groups = group_by_signature(records, max_frames)
    clusters: List[Dict[str, Any]] = []
    for sig in sorted(groups):
        recs = groups[sig]
        rep = min(recs, key=lambda r: (r["time"], stack_text(r)))
        clusters.append({
            "signature": sig,
            "kind": "anr" if all(r["kind"] == "anr" for r in recs) else "crash",
            "count": len(recs),
            "first_seen": min(r["time"] for r in recs),
            "last_seen": max(r["time"] for r in recs),
            "exception_line": rep["exception_line"],
            "top_frames": normalize_stack(rep["frames"], max_frames),
            "processes": sorted({r["process"] for r in recs if r["process"]}),
            "version_codes": sorted({r["version_code"] for r in recs
                                     if r["version_code"] is not None}),
            "activities": sorted({tok for r in recs
                                  for tok in (r.get("activities") or [])}),
            "representative_stack": stack_text(rep),
        })
    clusters.sort(key=lambda c: (-c["count"], c["signature"]))
    return clusters


def diff_clusters(baseline, current) -> Dict[str, List[Dict[str, Any]]]:
    """new/gone/worse/better between two cluster-row lists (by signature)."""
    base = {c["signature"]: c for c in baseline}
    cur = {c["signature"]: c for c in current}
    worse = []
    better = []
    for sig in sorted(set(base) & set(cur)):
        b = base[sig]["count"]
        c = cur[sig]["count"]
        row = {
            "signature": sig,
            "exception_line": cur[sig]["exception_line"],
            "baseline_count": b,
            "current_count": c,
        }
        if c > b:
            worse.append(row)
        elif c < b:
            better.append(row)
    return {
        "new": [cur[s] for s in sorted(set(cur) - set(base))],
        "gone": [base[s] for s in sorted(set(base) - set(cur))],
        "worse": worse,
        "better": better,
    }
