#!/usr/bin/env python3
"""M3 coverage-diff: compare two widget-level coverage snapshots.

Usage:
    python tools/coverage_diff.py old.json new.json [--out DIR] [--determinism-check]

Widget identity quadruple priority: resource-id > text > content-desc > path
(per activity). Empty or null fields fall through to the next priority.
--determinism-check exits 1 when the two snapshots' coverage content
(activities and their widget sets) differs; captured_at and visit_count drift
are ignored by design (they are expected run-to-run noise).
"""

import argparse
import json
import sys
from html import escape
from pathlib import Path

from common.report import render_report, write_report

KEY_PRIORITY = ("resource_id", "text", "content_desc", "path")


def load_snapshot(path):
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        raise SystemExit("cannot read coverage snapshot %s: %s" % (path, error))


def widget_key(widget):
    """Return (field, value) with priority resource_id > text > content_desc > path."""
    for field in KEY_PRIORITY:
        value = widget.get(field)
        if value:
            return (field, value)
    return ("path", "")


def widget_label(widget):
    parts = []
    for field in KEY_PRIORITY:
        parts.append("%s=%r" % (field, widget.get(field)))
    return " ".join(parts)


def diff_snapshots(old, new):
    old_activities = {a.get("name", ""): a for a in old.get("activities", [])}
    new_activities = {a.get("name", ""): a for a in new.get("activities", [])}

    result_activities = []
    for name in sorted(set(old_activities) | set(new_activities)):
        in_old = name in old_activities
        in_new = name in new_activities
        old_widgets = {widget_key(w): w for w in old_activities.get(name, {}).get("widgets", [])}
        new_widgets = {widget_key(w): w for w in new_activities.get(name, {}).get("widgets", [])}

        added = [new_widgets[k] for k in sorted(new_widgets.keys() - old_widgets.keys())]
        removed = [old_widgets[k] for k in sorted(old_widgets.keys() - new_widgets.keys())]

        changed = []
        for key in sorted(old_widgets.keys() & new_widgets.keys()):
            old_w = old_widgets[key]
            new_w = new_widgets[key]
            if any(old_w.get(f) != new_w.get(f) for f in KEY_PRIORITY):
                changed.append({
                    "key_field": key[0],
                    "key_value": key[1],
                    "old": old_w,
                    "new": new_w,
                })
        status = "unchanged"
        if not in_old:
            status = "added" if in_new else "unchanged"
        elif not in_new:
            status = "removed"
        elif added or removed or changed:
            status = "modified"
        result_activities.append({
            "name": name,
            "status": status,
            "added_widgets": added,
            "removed_widgets": removed,
            "changed_widgets": changed,
        })

    total = {
        "added": sum(len(a["added_widgets"]) for a in result_activities),
        "removed": sum(len(a["removed_widgets"]) for a in result_activities),
        "changed": sum(len(a["changed_widgets"]) for a in result_activities),
        "activities_added": sum(1 for a in result_activities if a["status"] == "added"),
        "activities_removed": sum(1 for a in result_activities if a["status"] == "removed"),
    }
    return {
        "identical": all(
            not a["added_widgets"] and not a["removed_widgets"] and not a["changed_widgets"]
            for a in result_activities
        ),
        "summary": total,
        "activities": result_activities,
    }


def build_report(old, new, diff, old_path, new_path):
    sections = [{
        "type": "summary",
        "rows": [
            ["旧快照", str(old_path)],
            ["新快照", str(new_path)],
            ["包名", "%s -> %s" % (old.get("package", ""), new.get("package", ""))],
            ["应用版本", "%s -> %s" % (old.get("version", ""), new.get("version", ""))],
            ["新增 Activity", diff["summary"]["activities_added"]],
            ["移除 Activity", diff["summary"]["activities_removed"]],
            ["新增控件", diff["summary"]["added"]],
            ["移除控件", diff["summary"]["removed"]],
            ["变更控件", diff["summary"]["changed"]],
        ],
    }]
    for activity in diff["activities"]:
        if activity["status"] == "unchanged":
            continue
        rows_html = []
        for title, items in (
                ("新增控件", activity["added_widgets"]),
                ("移除控件", activity["removed_widgets"]),
                ("变更控件", activity["changed_widgets"]),
        ):
            if not items:
                continue
            cells = "".join("<li><code>%s</code></li>" % escape(widget_label(w)) for w in items)
            if title == "变更控件":
                cells = "".join(
                    "<li><code>%s</code>（%s -> %s）</li>"
                    % (escape(w["key_field"] + "=" + w["key_value"]),
                       escape(widget_label(w["old"])),
                       escape(widget_label(w["new"])))
                    for w in items
                )
            rows_html.append("<h3>%s（%d）</h3><ul>%s</ul>" % (escape(title), len(items), cells))
        body = (
            "<p>状态：<b>%s</b></p>" % escape(activity["status"])
            + ("".join(rows_html) if rows_html else "<p>无控件级差异</p>")
        )
        sections.append({"type": "html", "body": body})
    return render_report("覆盖率差异报告", sections)


def _describe_content_differences(diff):
    lines = ["determinism check FAILED: coverage content differs"]
    for activity in diff["activities"]:
        if activity["status"] == "unchanged":
            continue
        lines.append("activity %s [%s]" % (activity["name"], activity["status"]))
        for w in activity["added_widgets"]:
            lines.append("  + %s" % widget_label(w))
        for w in activity["removed_widgets"]:
            lines.append("  - %s" % widget_label(w))
        for w in activity["changed_widgets"]:
            lines.append(
                "  * %s: %s -> %s"
                % (w["key_field"] + "=" + w["key_value"],
                   widget_label(w["old"]), widget_label(w["new"]))
            )
    return chr(10).join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="coverage_diff",
        description="Compare two widget-level coverage snapshots (M3).",
    )
    parser.add_argument("old", help="old coverage snapshot JSON")
    parser.add_argument("new", help="new coverage snapshot JSON")
    parser.add_argument("--out", default=None, help="output directory (default: alongside the new snapshot)")
    parser.add_argument("--determinism-check", action="store_true",
                        help="exit 1 when the two snapshots' coverage content differs")
    args = parser.parse_args(argv)

    old = load_snapshot(args.old)
    new = load_snapshot(args.new)
    diff = diff_snapshots(old, new)

    out_dir = Path(args.out) if args.out else Path(args.new).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    diff_path = out_dir / "diff.json"
    diff_path.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = out_dir / "diff.html"
    write_report(html_path, build_report(old, new, diff, args.old, args.new))

    summary = diff["summary"]
    print("coverage diff: +%d added, -%d removed, ~%d changed widgets; "
          "+%d / -%d activities" % (
              summary["added"], summary["removed"], summary["changed"],
              summary["activities_added"], summary["activities_removed"]))
    print("diff written: %s" % diff_path)
    print("report written: %s" % html_path)

    if args.determinism_check:
        if diff["identical"]:
            print("determinism check PASSED: coverage content is identical")
            return 0
        print(_describe_content_differences(diff))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
