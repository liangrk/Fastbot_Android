#!/usr/bin/env python3
"""M2 perf-metrics: perf data.json -> Chinese HTML report.

Renders the PC-side perf samples (perf_sample.schema.json JSONL) into a
standalone offline HTML report (tools/common/report.py) with:
  - 摘要表: per-metric avg/min/max (CPU%, 内存 PSS, 网络速率, 电量);
  - 内联 SVG time-series: CPU%, 内存 KB, 网络收发速率;
  - 冷/温启动统计与事件表 (Displayed 主信号, pidof 佐证);
  - 方法论说明 (轮询窗口漏检说明, 见 perf_poller 模块文档).

Usage:
    python tools/perf_report.py <data.json> [--out report.html]
        [--starts starts.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common.report import render_report, write_report

REPORT_TITLE = "Fastbot 性能采样报告"

# framestats raw CSV sanity guard: plausible frame time range in ms
MAX_PLAUSIBLE_FRAME_MS = 5000.0
FRAMESTATS_REQUIRED_COLUMNS = ("ts_ms", "Flags", "IntendedVsync", "FrameCompleted")

METRICS = [
    ("cpu_percent", "CPU 占用率 (%)"),
    ("mem_pss_kb", "内存 PSS (KB)"),
    ("net_rx_bytes", "累计接收字节 (B)"),
    ("net_tx_bytes", "累计发送字节 (B)"),
    ("battery_pct", "电量 (%)"),
]


def load_samples(path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Load perf data.json (JSONL). Malformed lines are skipped+counted,
    never crash - mirrors privacy_report semantics."""
    samples: List[Dict[str, Any]] = []
    malformed: List[str] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            malformed.append("line %d: %s" % (lineno, error))
            continue
        if not isinstance(record, dict):
            malformed.append("line %d: not a JSON object" % lineno)
            continue
        samples.append(record)
    return samples, malformed


def metric_stats(samples: Sequence[Dict[str, Any]], key: str) -> Optional[Dict[str, float]]:
    """avg/min/max over the samples where the metric is present."""
    values = [float(s[key]) for s in samples if isinstance(s.get(key), (int, float))]
    if not values:
        return None
    return {
        "avg": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "n": len(values),
    }


def net_rates(samples: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-interval network rates (KB/s) derived from consecutive cumulative
    byte counters. First sample (no predecessor) yields no rate."""
    rates: List[Dict[str, Any]] = []
    prev: Optional[Dict[str, Any]] = None
    for sample in samples:
        if (prev is not None
                and isinstance(sample.get("net_rx_bytes"), (int, float))
                and isinstance(sample.get("net_tx_bytes"), (int, float))
                and isinstance(prev.get("net_rx_bytes"), (int, float))
                and isinstance(prev.get("net_tx_bytes"), (int, float))
                and sample["ts"] > prev["ts"]):
            dt = (sample["ts"] - prev["ts"]) / 1000.0
            rates.append({
                "ts": sample["ts"],
                "rx_kb_per_s": max(0.0, (sample["net_rx_bytes"] - prev["net_rx_bytes"]) / dt / 1024.0),
                "tx_kb_per_s": max(0.0, (sample["net_tx_bytes"] - prev["net_tx_bytes"]) / dt / 1024.0),
            })
        prev = sample
    return rates


def parse_framestats_csv(text: str, report: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Parse a PerfFrameEvent <runid>_frames.csv artifact into per-frame rows.

    CSV layout (device-side PerfFrameEvent): header line of column names
    (ts_ms + the gfxinfo PROFILEDATA header, verbatim per ROM), one raw
    PROFILEDATA row per line with the sample window timestamp prefixed.
    Columns are located BY NAME so both the legacy 12-column and the newer
    wide header layouts parse. Each row becomes
    {"ts_ms", "flags", "frame_time_ms"} where
    frame_time_ms = (FrameCompleted - IntendedVsync) / 1e6, computed in
    integer arithmetic before the float division (ns values exceed 2^53).

    Tolerant: blank lines are ignored; rows with missing/non-numeric
    ts_ms/Flags/IntendedVsync/FrameCompleted fields are skipped and counted
    via the optional report out-list. Never raises on bad input.
    """
    skipped: List[str] = report if report is not None else []
    rows: List[Dict[str, Any]] = []
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        skipped.append("frames CSV 为空")
        return rows
    header = [column.strip() for column in lines[0].split(",")]
    if not all(name in header for name in FRAMESTATS_REQUIRED_COLUMNS):
        skipped.append("frames CSV 表头缺少必要列 (ts_ms/Flags/IntendedVsync/FrameCompleted)")
        return rows
    index = {name: header.index(name) for name in FRAMESTATS_REQUIRED_COLUMNS}
    for lineno, line in enumerate(lines[1:], 2):
        fields = [field.strip() for field in line.split(",")]
        if any(index[name] >= len(fields) for name in FRAMESTATS_REQUIRED_COLUMNS):
            skipped.append("line %d: 字段数不足" % lineno)
            continue
        try:
            ts_ms = int(fields[index["ts_ms"]])
            flags = int(fields[index["Flags"]])
            intended_vsync = int(fields[index["IntendedVsync"]])
            frame_completed = int(fields[index["FrameCompleted"]])
        except ValueError:
            skipped.append("line %d: 非数值字段" % lineno)
            continue
        rows.append({
            "ts_ms": ts_ms,
            "flags": flags,
            "frame_time_ms": (frame_completed - intended_vsync) / 1000000.0,
        })
    return rows


def frame_time_stats(text: str) -> Optional[Dict[str, Any]]:
    """Frame-time distribution over a frames CSV: count/p50/p90/p99/max
    after a vsync sanity guard (drop frame_time_ms <= 0 or > 5000ms), plus
    skipped-row and dropped-row counts and histogram buckets.
    Returns None when no frame row survives parsing+guard."""
    skipped: List[str] = []
    all_rows = parse_framestats_csv(text, report=skipped)
    if not all_rows and skipped:
        return None
    frames = [row for row in all_rows
              if 0 < row["frame_time_ms"] <= MAX_PLAUSIBLE_FRAME_MS]
    if not frames:
        return None
    values = sorted(row["frame_time_ms"] for row in frames)
    count = len(values)

    def _pct(p: float) -> float:
        position = min(count - 1, int(round(p / 100.0 * (count - 1))))
        return values[position]

    bucket_count = 20
    low, high = values[0], values[-1]
    width = (high - low) / bucket_count if high > low else 1.0
    buckets = [(low + i * width, 0) for i in range(bucket_count)]
    for value in values:
        slot = min(bucket_count - 1, int((value - low) / width))
        buckets[slot] = (buckets[slot][0], buckets[slot][1] + 1)
    return {
        "count": count,
        "p50": _pct(50), "p90": _pct(90), "p99": _pct(99), "max": values[-1],
        "dropped": len(all_rows) - count,
        "skipped_rows": len(skipped) - (1 if (not all_rows and skipped) else 0),
        "buckets": buckets,
        "bucket_width_ms": width,
        "min": low,
    }

def _histogram_svg(buckets: Sequence[Tuple[float, int]]) -> str:
    """Inline bar-chart SVG for the frame-time buckets, styled after
    tools/common/report.py series rendering."""
    max_count = max((count for _start, count in buckets), default=0)
    if max_count <= 0:
        return ""
    width, height, pad = 640, 300, 40
    inner_w = width - 2 * pad
    bar_w = inner_w / len(buckets)
    baseline = height - pad
    max_h = height - 2 * pad
    bars = []
    for i, (start_ms, count) in enumerate(buckets):
        bar_h = count / max_count * max_h
        x = pad + i * bar_w
        y = baseline - bar_h
        bars.append(
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="#2563eb" />'
            % (x, y, bar_w, bar_h))
    x_axis = ('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" />'
              % (pad, baseline, width - pad, baseline))
    y_axis = ('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" />'
              % (pad, pad, pad, baseline))
    return ('<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
            'xmlns="http://www.w3.org/2000/svg">%s%s%s</svg>'
            % (width, height, width, height, x_axis, y_axis, "".join(bars)))

def _table(title: str, headers: List[str], rows: List[List[Any]]) -> str:
    from html import escape
    lines = ["<h2>%s</h2>" % escape(title), "<table>", "<thead><tr>"]
    for header in headers:
        lines.append("<th>%s</th>" % escape(header))
    lines.append("</tr></thead><tbody>")
    for row in rows:
        lines.append("<tr>%s</tr>" % "".join(
            "<td>%s</td>" % escape(str(cell)) for cell in row))
    lines.append("</tbody></table>")
    return "\n".join(lines)


def build_sections(
    samples: Sequence[Dict[str, Any]],
    starts: Optional[Sequence[Dict[str, Any]]] = None,
    meta: Optional[Dict[str, Any]] = None,
    malformed: Optional[Sequence[str]] = None,
    frame_stats: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """report.py sections: summary table + inline SVG series + start events
    (+ frame-time distribution when a frames CSV is available)."""
    from html import escape
    meta = meta or {}
    starts = list(starts or [])
    sections: List[Dict[str, Any]] = []

    cold = sum(1 for s in starts if s.get("kind") == "cold")
    warm = sum(1 for s in starts if s.get("kind") == "warm")
    unknown = sum(1 for s in starts if s.get("kind") == "unknown")

    summary_rows: List[List[Any]] = [
        ["目标包名", meta.get("package", "-")],
        ["设备序列号", meta.get("serial", "-")],
        ["样本数", len(samples)],
    ]
    if malformed:
        summary_rows.append(["解析失败行", len(malformed)])
    summary_rows += [
        ["冷启动 (Displayed 主信号)", cold],
        ["温启动", warm],
        ["未知 (窗口内首条 Displayed)", unknown],
    ]
    sections.append({"type": "summary", "rows": summary_rows})

    def _fmt(value: Optional[Dict[str, float]], unit: str) -> str:
        if value is None:
            return "-"
        return "avg %.2f / min %.2f / max %.2f %s (n=%d)" % (
            value["avg"], value["min"], value["max"], unit, value["n"])

    stats_rows: List[List[Any]] = []
    for key, label in METRICS:
        stats_rows.append([label, _fmt(metric_stats(samples, key), "")])
    sections.append({"type": "html", "body": _table(
        "指标摘要 (avg/min/max)", ["指标", "统计"], stats_rows)})

    def _points(key: str) -> List[Any]:
        return [(s["ts"], s[key]) for s in samples
                if isinstance(s.get(key), (int, float)) and "ts" in s]

    for key, label in METRICS:
        if key in ("net_rx_bytes", "net_tx_bytes"):
            continue
        sections.append({"type": "series", "name": "%s 时间序列" % label,
                         "points": _points(key)})

    rates = net_rates(samples)
    sections.append({"type": "series", "name": "网络接收速率 (KB/s)",
                     "points": [(r["ts"], r["rx_kb_per_s"]) for r in rates]})
    sections.append({"type": "series", "name": "网络发送速率 (KB/s)",
                     "points": [(r["ts"], r["tx_kb_per_s"]) for r in rates]})

    if starts:
        sections.append({"type": "html", "body": _table(
            "启动事件 (冷/温启动)",
            ["时间", "类型", "pid", "Activity", "耗时(ms)"],
            [[s.get("time", "-"), s.get("kind", "-"), s.get("pid", "-"),
              s.get("activity", "-"), s.get("duration_ms", "-")] for s in starts])})
    if frame_stats is not None:
        stats_rows = [
            ["帧样本数", frame_stats["count"]],
            ["p50 / p90 / p99 / max (ms)", "%.2f / %.2f / %.2f / %.2f" % (
                frame_stats["p50"], frame_stats["p90"],
                frame_stats["p99"], frame_stats["max"])],
            ["剔除行 (帧耗时 <=0 或 >5000ms)", frame_stats["dropped"]],
            ["解析跳过行", frame_stats["skipped_rows"]],
        ]
        body = _table("帧耗时分布 (framestats)", ["指标", "值"], stats_rows)
        body += _histogram_svg(frame_stats["buckets"])
        body += "<p>帧耗时 = FrameCompleted - IntendedVsync (ms)；直方图 %d 桶 × %.2fms，横轴 %.2fms 起。</p>" % (
            len(frame_stats["buckets"]), frame_stats["bucket_width_ms"],
            frame_stats["min"])
        sections.append({"type": "html", "body": body})
    if meta.get("note"):
        sections.append({"type": "html", "body":
                         "<h2>方法论说明</h2><p>%s</p>" % escape(meta["note"])})
    return sections


def render_perf_report(
    samples: Sequence[Dict[str, Any]],
    starts: Optional[Sequence[Dict[str, Any]]] = None,
    meta: Optional[Dict[str, Any]] = None,
    malformed: Optional[Sequence[str]] = None,
    frame_stats: Optional[Dict[str, Any]] = None,
) -> str:
    """Full HTML document for the given samples/starts (+ frames CSV stats)."""
    return render_report(REPORT_TITLE, build_sections(
        samples, starts, meta, malformed, frame_stats))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="perf_report.py",
        description="Render a Chinese HTML perf report from perf data.json.",
    )
    parser.add_argument("data_file", help="path to perf data.json (JSONL)")
    parser.add_argument("--starts", default=None, help="optional starts.json")
    parser.add_argument("--out", default="tools/out/perf_report.html",
                        help="output HTML path (default tools/out/perf_report.html)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print summary only, write nothing")
    args = parser.parse_args(argv)
    samples, malformed = load_samples(args.data_file)
    starts: List[Dict[str, Any]] = []
    if args.starts:
        for raw in Path(args.starts).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line:
                starts.append(json.loads(line))
    frame_stats = None
    if not args.dry_run:
        try:
            candidates = sorted(Path(args.data_file).parent.glob("*_frames.csv"))
            if candidates:
                frame_stats = frame_time_stats(
                    candidates[0].read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            print("WARNING: cannot read frames csv: %s" % error, file=sys.stderr)
    if args.dry_run:
        print("perf_report dry-run: %s" % args.data_file)
        print("  样本数: %d" % len(samples))
        print("  解析失败行: %d" % len(malformed))
        print("  启动事件: %d" % len(starts))
        return 0
    out_path = Path(args.out)
    html = render_perf_report(samples, starts, meta={"package": "-", "serial": "-"},
                              malformed=malformed, frame_stats=frame_stats)
    write_report(out_path, html)
    print("perf report written: %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
