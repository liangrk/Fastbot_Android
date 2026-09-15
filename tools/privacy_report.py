#!/usr/bin/env python3
"""M4 privacy-compliance: audit.jsonl -> Chinese HTML report.

Parses /sdcard/fastbot_privacy/audit.jsonl (one audit.schema.json object
per line), tolerating malformed lines (skipped + counted + warned, never
crashing), and renders a Chinese HTML report (via tools/common/report.py)
with:
  - summary: totals, malformed line count, time range;
  - by event type / by permission / by page (activity) aggregations;
  - a timeline table with relative links to screenshot files.

Usage:
    python tools/privacy_report.py <audit.jsonl> [--out FILE] [--dry-run]

--dry-run prints the parse summary only and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.report import render_report, write_report

TYPES = ("permission_prompt", "sensitive_api", "rule_hit")
SOURCES = ("device", "frida")
REQUIRED_STR = ("activity", "detail")
REQUIRED_ENUMS = {"type": TYPES, "source": SOURCES}

REPORT_TITLE = "Fastbot 隐私合规审计报告"


def parse_audit_text(text: str) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """Parse JSONL audit content. Returns (records, malformed) where malformed
    is a list of (lineno, reason). Malformed lines are skipped, counted and
    warned on stderr at CLI level - never crash.
    """
    records: List[Dict[str, Any]] = []
    malformed: List[Tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            malformed.append((lineno, "invalid JSON: %s" % error))
            continue
        problem = _check_record(record)
        if problem:
            malformed.append((lineno, problem))
            continue
        records.append(record)
    return records, malformed

def _check_record(record: Any) -> Optional[str]:
    """Schema-required-field check (lenient; full validation via jsonschema)."""
    if not isinstance(record, dict):
        return "not a JSON object"
    if isinstance(record.get("ts"), bool) or not isinstance(record.get("ts"), int):
        return "ts must be an integer"
    for field in REQUIRED_STR:
        if not isinstance(record.get(field), str):
            return "%s must be a string" % field
    for field, allowed in REQUIRED_ENUMS.items():
        if record.get(field) not in allowed:
            return "%s must be one of %s" % (field, list(allowed))
    return None


def parse_audit(path) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """Read + parse an audit JSONL file from disk."""
    with open(path, encoding="utf-8") as handle:
        return parse_audit_text(handle.read())


def detail_value(detail: str, key: str) -> Optional[str]:
    """Extract a key=value pair from the detail string (rule/action/permission
    pairs written by the device-side PrivacyAuditor)."""
    for part in detail.split(";"):
        k, sep, v = part.partition("=")
        if sep and k == key:
            return v
    return None


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate audit records: totals, by type, by page, by action, by
    permission, and the record time range."""
    return {
        "total": len(records),
        "by_type": Counter(r["type"] for r in records),
        "by_page": Counter(r["activity"] for r in records),
        "by_action": Counter(detail_value(r["detail"], "action") or "-" for r in records),
        "by_permission": Counter(detail_value(r["detail"], "permission") or "(无)" for r in records),
        "first_ts": min((r["ts"] for r in records), default=None),
        "last_ts": max((r["ts"] for r in records) , default=None),
    }


def _ts_str(ts: Optional[int]) -> str:
    if ts is None:
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(ts / 1000.0).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _counter_rows(counter: Counter) -> List[List[Any]]:
    return [[key, count] for key, count in counter.most_common()]


def _screenshot_link(record: Dict[str, Any], out_dir: Optional[Path]) -> Optional[str]:
    """Relative link target for a screenshot path (None = plain text)."""
    screenshot = record.get("screenshot")
    if not screenshot or out_dir is None:
        return None
    candidate = Path(screenshot)
    if not candidate.is_absolute():
        candidate = out_dir / candidate
    if candidate.exists():
        return os.path.relpath(str(candidate), start=str(out_dir))
    return None



def build_sections(
    records: List[Dict[str, Any]],
    malformed: Optional[List[Tuple[int, str]]] = None,
    out_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Build report.py sections for the privacy audit report."""
    malformed = list(malformed or [])
    stats = aggregate(records)
    sections: List[Dict[str, Any]] = [
        {"type": "summary", "rows": [
            ["事件总数", stats["total"]],
            ["规则命中 (rule_hit)", stats["by_type"].get("rule_hit", 0)],
            ["权限弹窗 (permission_prompt)", stats["by_type"].get("permission_prompt", 0)],
            ["敏感 API (sensitive_api)", stats["by_type"].get("sensitive_api", 0)],
            ["解析失败行", len(malformed)],
            ["首条事件", _ts_str(stats["first_ts"])],
            ["末条事件", _ts_str(stats["last_ts"])],
        ]},
        {"type": "html", "body": _table(
            "按事件类型", ["类型", "次数"], _counter_rows(stats["by_type"]))},
        {"type": "html", "body": _table(
            "按权限聚合", ["权限", "次数"], _counter_rows(stats["by_permission"]))},
        {"type": "html", "body": _table(
            "按页面聚合", ["Activity", "次数"], _counter_rows(stats["by_page"]))},
        {"type": "html", "body": _table(
            "按处理动作", ["动作", "次数"], _counter_rows(stats["by_action"]))},
        {"type": "html", "body": _timeline_table(records, malformed, out_dir)},
    ]
    return sections


def _table(title: str, headers: List[str], rows: List[List[Any]]) -> str:
    lines = ["<h2>%s</h2>" % escape(title), "<table>", "<thead><tr>"]
    for header in headers:
        lines.append("<th>%s</th>" % escape(header))
    lines.append("</tr></thead><tbody>")
    for row in rows:
        lines.append("<tr>%s</tr>" % "".join(
            "<td>%s</td>" % escape(str(cell)) for cell in row))
    lines.append("</tbody></table>")
    return "\n".join(lines)


def _timeline_table(
    records: List[Dict[str, Any]],
    malformed: List[Tuple[int, str]],
    out_dir: Optional[Path],
) -> str:
    lines = [
        "<h2>事件时间线</h2>",
        "<table>",
        "<thead><tr><th>时间</th><th>类型</th><th>Activity</th><th>动作</th>"
        "<th>规则</th><th>权限</th><th>控件</th><th>截图</th></tr></thead>",
        "<tbody>",
    ]
    for record in records:
        link = _screenshot_link(record, out_dir)
        if link:
            screenshot_html = '<a href="%s">%s</a>' % (
                escape(link, quote=True), escape(str(record.get("screenshot"))))
        else:
            screenshot_html = escape(str(record.get("screenshot") or "-"))
        lines.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                escape(_ts_str(record["ts"])),
                escape(str(record["type"])),
                escape(str(record["activity"])),
                escape(str(detail_value(record["detail"], "action") or "-")),
                escape(str(detail_value(record["detail"], "rule") or "-")),
                escape(str(detail_value(record["detail"], "permission") or "(无)")),
                escape(str(record.get("widget") or "-")),
                screenshot_html,
            ))
    for lineno, reason in malformed:
        lines.append(
            '<tr><td colspan="8">第 %d 行解析失败：%s</td></tr>' % (lineno, escape(reason)))
    lines.append("</tbody></table>")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="privacy_report.py",
        description="Render a Chinese HTML report from a Fastbot privacy audit JSONL file.",
    )
    parser.add_argument("audit_file", help="path to audit.jsonl")
    parser.add_argument("--out", default="tools/out/privacy_report.html",
                        help="output HTML path (default: tools/out/privacy_report.html)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the parse summary and exit without writing anything")
    args = parser.parse_args(argv)
    try:
        records, malformed = parse_audit(args.audit_file)
    except OSError as error:
        print("ERROR: cannot read audit file: %s" % error, file=sys.stderr)
        return 2
    for lineno, reason in malformed:
        print("WARNING: 第 %d 行解析失败，已跳过：%s" % (lineno, reason), file=sys.stderr)
    stats = aggregate(records)
    if args.dry_run:
        print("privacy_report dry-run: %s" % args.audit_file)
        print("  事件总数: %d" % stats["total"])
        print("  规则命中: %d" % stats["by_type"].get("rule_hit", 0))
        print("  权限弹窗: %d" % stats["by_type"].get("permission_prompt", 0))
        print("  敏感API: %d" % stats["by_type"].get("sensitive_api", 0))
        print("  解析失败行: %d" % len(malformed))
        return 0
    out_path = Path(args.out)
    html = render_report(REPORT_TITLE, build_sections(records, malformed, out_path.parent))
    write_report(out_path, html)
    print("privacy report written: %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
