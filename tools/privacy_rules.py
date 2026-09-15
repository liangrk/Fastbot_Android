#!/usr/bin/env python3
"""Fastbot privacy rules: PC-side reference implementation of the device-side
rule engine (monkey/src/main/java/com/android/commands/monkey/events/
customize/PrivacyRuleEngine.java). Pure logic, no device required.

Semantics (both sides):
  - rules are evaluated strictly in file order; the FIRST rule whose page
    regex matches the activity name wins;
  - "page" (mandatory): regular expression, search semantics (not full
    match) against the top activity class name;
  - "widget" (optional): regular expression with search semantics against
    the GUI XML string produced by TreeBuilder.dumpDocumentStrWithOutTree;
    a rule without widget matches every rendering of its page (audit-only);
  - "action" (optional): "consent" or "deny"; when omitted it falls back to
    max.privacy.defaultAction;
  - "name" / "permission": optional free-form tags for reporting.

Usage:
    python tools/privacy_rules.py validate <rules.json> [--dry-run]

Exit codes: 0 = valid (warnings allowed), 1 = validation errors,
2 = usage/IO errors (missing file, unparsable JSON).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

ACTIONS = ("consent", "deny")
DEFAULT_ACTION = "consent"


def parse_rules(text: str) -> List[Dict[str, Any]]:
    """Parse rules JSON text into a list of raw rule objects.

    Raises ValueError when the root is not a JSON array or an entry is not
    a JSON object (mirrors the Java engine's JSONArray/getJSONObject).
    """
    root = json.loads(text)
    if not isinstance(root, list):
        raise ValueError("rules root must be a JSON array")
    rules: List[Dict[str, Any]] = []
    for position, entry in enumerate(root, 1):
        if not isinstance(entry, dict):
            raise ValueError("rule #%d is not a JSON object" % position)
        rules.append(entry)
    return rules


def compile_rules(rules: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Validate raw rules and precompile their regexes.

    Returns (compiled, errors, warnings); compiled contains only the rules
    that were valid, preserving file order.
    """
    compiled: List[Dict[str, Any]] = []
    errors: List[str] = []
    warnings: List[str] = []
    for position, rule in enumerate(rules, 1):
        where = _rule_label(rule, position)
        page = rule.get("page")
        if not isinstance(page, str) or not page.strip():
            errors.append("%s: missing page" % where)
            continue
        entry: Dict[str, Any] = {"index": position, "page": page}
        try:
            entry["page_re"] = re.compile(page)
        except re.error as error:
            errors.append("%s: invalid page regex: %s" % (where, error))
            continue
        widget = rule.get("widget")
        if widget is not None:
            if not isinstance(widget, str) or not widget.strip():
                errors.append("%s: widget must be a non-empty string or omitted" % where)
                continue
            try:
                entry["widget_re"] = re.compile(widget)
            except re.error as error:
                errors.append("%s: invalid widget regex: %s" % (where, error))
                continue
            entry["widget"] = widget
        action = rule.get("action")
        if action is not None:
            if action not in ACTIONS:
                errors.append(
                    "%s: action must be one of %s, got %r" % (where, list(ACTIONS), action))
                continue
            entry["action"] = action
        name = rule.get("name")
        if name is not None:
            if not isinstance(name, str):
                errors.append("%s: name must be a string" % where)
                continue
            entry["name"] = name
        permission = rule.get("permission")
        if permission is not None:
            if not isinstance(permission, str):
                errors.append("%s: permission must be a string" % where)
                continue
            entry["permission"] = permission
        compiled.append(entry)
    if not rules:
        warnings.append("rules file is empty (matching disabled)")
    return compiled, errors, warnings


def _rule_label(rule: Dict[str, Any], position: int) -> str:
    name = rule.get("name")
    if isinstance(name, str) and name.strip():
        return "%s(rule#%d)" % (name, position)
    return "rule#%d" % position


def rule_label(compiled: Dict[str, Any]) -> str:
    """Display name used in logs/reports (rule name or rule#N fallback)."""
    name = compiled.get("name")
    if name:
        return str(name)
    return "rule#%d" % compiled["index"]


def resolve_action(compiled: Dict[str, Any], default_action: str = DEFAULT_ACTION) -> str:
    """Action resolution with the defaultAction fallback."""
    action = compiled.get("action")
    return action if action else default_action


def match(
    activity: str,
    xml: str,
    compiled: List[Dict[str, Any]],
    default_action: str = DEFAULT_ACTION,
) -> Optional[Dict[str, Any]]:
    """First matching rule in file order, or None (device-parity semantics)."""
    del default_action  # resolution is a property of the hit, not the match
    for rule in compiled:
        if rule["page_re"].search(activity or "") is None:
            continue
        widget_re = rule.get("widget_re")
        if widget_re is None or widget_re.search(xml or "") is not None:
            return rule
    return None


def find_center(xml: str, widget_re) -> Optional[Tuple[int, int]]:
    """Click target: center of the first bounds attribute at or after the
    widget match. TreeBuilder writes bounds as the LAST attribute of every
    node, so for attribute-level matches this is the enclosing node's own
    bounds. Returns (x, y) or None (mirrors PrivacyRule.findCenterInXml).
    """
    if widget_re is None:
        return None
    m = widget_re.search(xml or "")
    if m is None:
        return None
    return _bounds_center_after(xml, m.start())


def _bounds_center_after(xml: str, start: int) -> Optional[Tuple[int, int]]:
    key = 'bounds="'
    idx = xml.find(key, start)
    while idx >= 0:
        value_start = idx + len(key)
        close = xml.find('"', value_start)
        if close > value_start:
            return _parse_bounds(xml[value_start:close])
        idx = xml.find(key, value_start)
    return None


def _parse_bounds(value: str) -> Optional[Tuple[int, int]]:
    """Parse "[l,t][r,b]" into its center point, or None."""
    if not value.startswith("["):
        return None
    try:
        comma1 = value.index(",")
        mid = value.index("]")
        open2 = value.index("[", mid + 1)
        comma2 = value.index(",", open2)
        end = value.index("]", open2)
        if not (comma1 >= 1 and mid > comma1 and open2 > mid and comma2 > open2 and end > comma2):
            return None
        left = int(value[1:comma1])
        top = int(value[comma1 + 1:mid])
        right = int(value[open2 + 1:comma2])
        bottom = int(value[comma2 + 1:end])
    except ValueError:
        return None
    return ((left + right) // 2, (top + bottom) // 2)


def validate_file(path) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Load and validate a rules file from disk. Raises OSError on IO errors."""
    text = _read_text(path)
    rules = parse_rules(text)
    return compile_rules(rules)


def _read_text(path) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="privacy_rules.py",
        description="Validate a Fastbot privacy rules JSON file (device parity).",
    )
    sub = parser.add_subparsers(dest="command")
    p_validate = sub.add_parser(
        "validate", help="validate a rules file; --dry-run prints the compiled rules")
    p_validate.add_argument("rules_file", help="path to the rules JSON file")
    p_validate.add_argument(
        "--dry-run", action="store_true",
        help="after validation, print the compiled rules instead of writing anything")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "validate":
        try:
            compiled, errors, warnings = validate_file(args.rules_file)
        except OSError as error:
            print("ERROR: cannot read rules file: %s" % error, file=sys.stderr)
            return 2
        except ValueError as error:
            print("ERROR: %s" % error, file=sys.stderr)
            return 1
        if errors:
            print("privacy rules: %d error(s)" % len(errors))
            for message in errors:
                print("  ERROR: %s" % message)
            return 1
        print("privacy rules OK: %d rule(s)" % len(compiled))
        for message in warnings:
            print("  WARNING: %s" % message)
        if args.dry_run:
            print("-- dry-run: compiled rules (file order) --")
            for rule in compiled:
                print("  [%d] %s page=%s widget=%s action=%s permission=%s" % (
                    rule["index"], rule_label(rule), rule["page"],
                    rule.get("widget", "-"), resolve_action(rule),
                    rule.get("permission", "-")))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
