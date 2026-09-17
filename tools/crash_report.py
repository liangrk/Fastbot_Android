#!/usr/bin/env python3
"""crash_report: cluster Fastbot crash-dump.log (+ optional logcat) and emit
a deterministic clusters.jsonl / root_cause.jsonl / crash_report.html.

Usage:
    python tools/crash_report.py <crash-dump.log> [--logcat LOGCAT]
        [--baseline CLUSTERS_JSONL] [--fail-new-crash] [--determinism-check]
        [--top N] [--frames N] [--out DIR] [--dry-run]

Exit codes: 0 = OK (gates off or passed); 1 = --fail-new-crash found new
signatures or --determinism-check mismatch; 2 = usage/IO errors.

Deviations from plan (explicit):
- --logcat is OPTIONAL (plan AC-CR1 lists logcat as an input; fastbot_run
  always collects logcat, so dump-only reruns stay workable).
- plan section 2.6 words last_scene as "matches the earliest record";
  implemented as the LATEST record of the cluster (last-scene semantics
  for the agent root-cause pack).
"""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import crash_parse
from common.report import render_report, write_report

DEFAULT_OUT = "tools/out/crash_report"
GENERATED_BY = "crash_report"
HTML_TITLE = "Fastbot crash report"
TOP_HEADER = "TopN clusters"
STACK_LIMIT = 60


class UsageError(Exception):
    """Invalid input file or baseline; maps to exit code 2."""


def _read_text(path: str, label: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise UsageError("cannot read %s %s: %s" % (label, path, error))


def load_baseline(path: str) -> List[Dict[str, Any]]:
    """Parse a clusters.jsonl baseline; blank lines skipped."""
    rows: List[Dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise UsageError("cannot read baseline %s: %s" % (path, error))
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as error:
            raise UsageError("baseline %s line %d is not JSON: %s"
                             % (path, line_no, error))
    return rows


def render_root_cause_jsonl(clusters, records, max_frames: int = 8) -> str:
    """Root-cause pack: one JSONL row per cluster with last_scene."""
    groups = crash_parse.group_by_signature(records, max_frames)
    lines = []
    for c in clusters:
        recs = groups[c["signature"]]
        last_rec = max(recs, key=lambda r: (r["time"], crash_parse.stack_text(r)))
        scene = {
            "seen": last_rec["time"],
            "process": last_rec["process"] or "",
            "stack": crash_parse.stack_text(last_rec),
        }
        if last_rec["version_code"] is not None:
            scene["version_code"] = last_rec["version_code"]
        row = {
            "signature": c["signature"],
            "kind": c["kind"],
            "count": c["count"],
            "first_seen": c["first_seen"],
            "exception_line": c["exception_line"],
            "top_frames": c["top_frames"],
            "activities": c["activities"],
            "representative_stack": c["representative_stack"],
            "last_seen": c["last_seen"],
            "last_scene": scene,
            "generated_by": GENERATED_BY,
        }
        lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return "".join(lines)


def render_clusters_jsonl(clusters) -> str:
    """Deterministic clusters.jsonl: sorted keys, one row per line."""
    return "".join(
        json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n"
        for c in clusters
    )


def _render_payloads(dump_text, logcat_text, max_frames):
    """(records, clusters, clusters_jsonl, root_cause_jsonl) from inputs."""
    records = crash_parse.parse_dump_records(dump_text)
    if logcat_text is not None:
        records = records + crash_parse.parse_logcat(logcat_text)
    clusters = crash_parse.cluster(records, max_frames)
    clusters_s = render_clusters_jsonl(clusters)
    root_s = render_root_cause_jsonl(clusters, records, max_frames)
    return records, clusters, clusters_s, root_s


def _first_diff(x: str, y: str) -> Optional[str]:
    """Label of the first differing line between two JSONL strings."""
    xs = x.splitlines()
    ys = y.splitlines()
    for i in range(max(len(xs), len(ys))):
        if i >= len(xs) or i >= len(ys) or xs[i] != ys[i]:
            return "line %d" % (i + 1)
    return None


def _first_diff_payloads(cl_a, root_a, cl_b, root_b) -> Optional[str]:
    """First difference across the two JSONL payloads."""
    for name, x, y in (
        ("clusters.jsonl", cl_a, cl_b),
        ("root_cause.jsonl", root_a, root_b),
    ):
        label = _first_diff(x, y)
        if label:
            return "%s %s" % (name, label)
    return None


def build_html(dump_path, logcat_path, records, clusters, top, max_frames):
    """Standalone Chinese HTML report with TopN table + representative stacks."""
    n_crash = sum(1 for r in records if r["kind"] == "crash")
    n_anr = sum(1 for r in records if r["kind"] == "anr")
    logcat_label = logcat_path if logcat_path else "-"
    sections = [{
        "type": "summary",
        "rows": [
            ["输入 dump", dump_path],
            ["输入 logcat", logcat_label],
            ["crash 记录", n_crash],
            ["ANR 记录", n_anr],
            ["聚类簇数", len(clusters)],
            ["签名帧数上限 (--frames)", max_frames],
            ["TopN (--top)", top],
        ],
    }]
    if clusters:
        rows = []
        for rank, c in enumerate(clusters[:top], 1):
            rows.append(
                "<tr><td>%d</td><td>%s</td><td>%d</td><td><code>%s</code></td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (rank, escape(c["kind"]), c["count"], escape(c["signature"]),
                   escape(c["first_seen"]), escape(c["last_seen"]),
                   escape(c["exception_line"]),
                   escape(", ".join(c["activities"]) or "-"))
            )
        table = (
            "<table><thead><tr><th>排名</th><th>类型</th><th>次数</th>"
            "<th>签名</th><th>首次</th><th>末次</th><th>异常行</th>"
            "<th>Activity</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>"
        )
        sections.append({"type": "html", "body": "<h2>%s</h2>%s" % (TOP_HEADER, table)})
        for rank, c in enumerate(clusters[:top], 1):
            meta = ("count=%d kind=%s first=%s last=%s processes=%s" % (
                c["count"], c["kind"], c["first_seen"], c["last_seen"],
                ", ".join(c["processes"]) or "-"))
            stack = c["representative_stack"]
            if len(stack) > STACK_LIMIT:
                stack = stack[:STACK_LIMIT] + "...(truncated)"
            sections.append({"type": "html", "body":
                "<h3>#%d <code>%s</code></h3><p>%s</p><pre>%s</pre>"
                % (rank, escape(c["signature"]), escape(meta), escape(stack))})
    else:
        sections.append({"type": "html", "body": "<p>无崩溃记录</p>"})
    return render_report(HTML_TITLE, sections)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crash_report.py",
        description="Cluster Fastbot crash dumps into deterministic clusters.",
    )
    parser.add_argument("dump", help="crash-dump.log path")
    parser.add_argument("--logcat", default=None, help="optional logcat capture")
    parser.add_argument("--baseline", default=None, help="previous clusters.jsonl")
    parser.add_argument("--fail-new-crash", action="store_true",
                        help="exit 1 when the diff shows new signatures")
    parser.add_argument("--determinism-check", action="store_true",
                        help="double-render in-process and byte-compare")
    parser.add_argument("--top", type=int, default=10, help="TopN clusters")
    parser.add_argument("--frames", type=int, default=8,
                        help="normalized frames per signature")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, write nothing")
    return parser


def _print_plan(args) -> None:
    print("crash_report dry-run 计划 (零接触, 不读输入不写输出)")
    print("  dump: %s" % args.dump)
    print("  logcat: %s" % (args.logcat or "-"))
    print("  baseline: %s" % (args.baseline or "-"))
    print("  frames: %d  top: %d" % (args.frames, args.top))
    print("  out: %s" % args.out)
    print("  fail-new-crash: %s  determinism-check: %s"
          % (args.fail_new_crash, args.determinism_check))
    print("  说明: --dry-run 允许输入路径不存在 (零接触演练, AC-AA1)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.dry_run:
        _print_plan(args)
        return 0
    if args.fail_new_crash and not args.baseline:
        print("ERROR: --fail-new-crash requires --baseline", file=sys.stderr)
        return 2
    try:
        dump_text = _read_text(args.dump, "crash-dump.log")
        logcat_text = None
        if args.logcat:
            logcat_text = _read_text(args.logcat, "logcat")
        baseline = load_baseline(args.baseline) if args.baseline else None
    except UsageError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2
    records, clusters, clusters_s, root_s = _render_payloads(
        dump_text, logcat_text, args.frames)
    if args.determinism_check:
        (_r2, _c2, cl2, root2) = _render_payloads(dump_text, logcat_text,
                                                  args.frames)
        marker = _first_diff_payloads(clusters_s, root_s, cl2, root2)
        if marker:
            print("determinism check FAILED: %s differs" % marker)
            return 1
        print("determinism check PASSED")
    return _emit(args, records, clusters, clusters_s, root_s, baseline)


def _emit(args, records, clusters, clusters_s, root_s, baseline) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_text(out_dir / "clusters.jsonl", clusters_s)
    _write_text(out_dir / "root_cause.jsonl", root_s)
    html = build_html(args.dump, args.logcat, records, clusters,
                      args.top, args.frames)
    write_report(out_dir / "crash_report.html", html)
    print("== Fastbot 崩溃聚类报告 ==")
    print("  dump: %s" % args.dump)
    print("  logcat: %s" % (args.logcat or "-"))
    print("  records: crash %d, anr %d"
          % (sum(1 for r in records if r["kind"] == "crash"),
             sum(1 for r in records if r["kind"] == "anr"))
          )
    print("  clusters: %d (frames=%d)" % (len(clusters), args.frames))
    for rank, c in enumerate(clusters[:args.top], 1):
        print("  Top %d: count=%d kind=%s sig=%s %s"
              % (rank, c["count"], c["kind"], c["signature"],
                 c["exception_line"]))
    print("  clusters.jsonl / root_cause.jsonl / crash_report.html -> %s"
          % out_dir)
    if baseline is not None:
        diff = crash_parse.diff_clusters(baseline, clusters)
        print("  diff vs baseline: new=%d gone=%d worse=%d better=%d"
              % (len(diff["new"]), len(diff["gone"]),
                 len(diff["worse"]), len(diff["better"])))
        for row in diff["new"]:
            print("  NEW SIGNATURE %s %s"
                  % (row["signature"], row["exception_line"]))
        for row in diff["gone"]:
            print("  GONE SIGNATURE %s %s"
                  % (row["signature"], row["exception_line"]))
        for row in diff["worse"]:
            print("  WORSE %s %s (%d -> %d)"
                  % (row["signature"], row["exception_line"],
                     row["baseline_count"], row["current_count"]))
        for row in diff["better"]:
            print("  BETTER %s %s (%d -> %d)"
                  % (row["signature"], row["exception_line"],
                     row["baseline_count"], row["current_count"]))
        if args.fail_new_crash and diff["new"]:
            print("RESULT: FAIL (--fail-new-crash, %d new signature(s))"
                  % len(diff["new"]))
            return 1
        if args.fail_new_crash:
            print("RESULT: PASS (no new signatures)")
    return 0


def _write_text(path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target


if __name__ == "__main__":
    sys.exit(main())
