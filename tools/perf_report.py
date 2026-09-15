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
from typing import Any, Dict, List, Optional, Sequence

from common.report import render_report, write_report

REPORT_TITLE = "Fastbot 性能采样报告"

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
) -> List[Dict[str, Any]]:
    """report.py sections: summary table + inline SVG series + start events."""
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
    if meta.get("note"):
        sections.append({"type": "html", "body":
                         "<h2>方法论说明</h2><p>%s</p>" % escape(meta["note"])})
    return sections


def render_perf_report(
    samples: Sequence[Dict[str, Any]],
    starts: Optional[Sequence[Dict[str, Any]]] = None,
    meta: Optional[Dict[str, Any]] = None,
    malformed: Optional[Sequence[str]] = None,
) -> str:
    """Full HTML document for the given samples/starts."""
    return render_report(REPORT_TITLE, build_sections(samples, starts, meta, malformed))


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
    if args.dry_run:
        print("perf_report dry-run: %s" % args.data_file)
        print("  样本数: %d" % len(samples))
        print("  解析失败行: %d" % len(malformed))
        print("  启动事件: %d" % len(starts))
        return 0
    out_path = Path(args.out)
    html = render_perf_report(samples, starts, meta={"package": "-", "serial": "-"},
                              malformed=malformed)
    write_report(out_path, html)
    print("perf report written: %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
