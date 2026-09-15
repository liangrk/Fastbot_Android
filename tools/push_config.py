#!/usr/bin/env python3
"""M6 llm-protocol: validate + push the max.xpath.actions expert config.

The file format is NOT invented: it mirrors the repo's real config
(test/max.xpath.actions) and the native parser (native/events/Preference.cpp
loadActions + native/Base.cpp actName[]):

  root   : JSON array of case objects  (a {"actionList": [...]} wrapper is
           tolerated with a warning; the native parser reads a bare array)
  case   : {"prob"?: float 0..1 (default 1),
            "activity": string, the FULL activity name (native matches by
                        EXACT string equality, Preference.cpp:128 - not a
                        regex, not a relative name),
            "times"?: int >= 1 (default 1),
            "actions": non-empty array of action objects}
  action : {"action": one of the actName[] types (CLICK, LONG_CLICK, BACK,
                        SCROLL_TOP_DOWN, SCROLL_BOTTOM_UP, SCROLL_LEFT_RIGHT,
                        SCROLL_RIGHT_LEFT, ...),
            "xpath"?: string locator (REQUIRED for CLICK/LONG_CLICK/SCROLL_*,
                        native patchActionBounds no-ops without a match),
            "throttle"?: int >= 0 ms (native default 1000),
            "text"?: string, "clearText"?: bool, "wait"?: int >= 0,
            "useAdbInput"?: bool}

Exit codes: 0 = valid and pushed (or --dry-run validation only),
1 = validation errors (each listed), 2 = usage/IO errors.

Usage:
    python tools/push_config.py <actions.json> [--serial SER]
        [--package PKG] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from common.adb import AdbClient

TARGET_REMOTE = "/sdcard/max.xpath.actions"

# native/Base.cpp actName[] - the exact set stringToActionType() accepts
KNOWN_ACTION_TYPES: Tuple[str, ...] = (
    "CRASH", "FUZZ", "START", "RESTART", "CLEAN_RESTART", "NOP",
    "ACTIVATE", "BACK", "FEED", "CLICK", "LONG_CLICK",
    "SCROLL_TOP_DOWN", "SCROLL_BOTTOM_UP", "SCROLL_LEFT_RIGHT",
    "SCROLL_RIGHT_LEFT", "SCROLL_BOTTOM_UP_N", "SHELL_EVENT", "HOVER",
)
TARGET_ACTIONS: Tuple[str, ...] = (
    "CLICK", "LONG_CLICK", "SCROLL_TOP_DOWN", "SCROLL_BOTTOM_UP",
    "SCROLL_LEFT_RIGHT", "SCROLL_RIGHT_LEFT", "HOVER",
)
RERUN_HINT_FMT = (
    "复跑命令 (README 模式):\n"
    "adb -s {serial} shell CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:"
    "/sdcard/fastbot-thirdpart.jar exec app_process /system/bin "
    "com.android.commands.monkey.Monkey -p {package} --agent reuseq "
    "--running-minutes <minutes> --throttle <throttle_ms> -v -v"
)


def _validate_action(action: Any, where: str, errors: List[str],
                     warnings: List[str]) -> None:
    """Validate one action object, appending to errors/warnings."""
    if not isinstance(action, dict):
        errors.append("%s: action is not a JSON object" % where)
        return
    for key in action:
        if key not in ("action", "xpath", "throttle", "text", "clearText",
                       "wait", "useAdbInput", "command"):
            warnings.append("%s: unknown action key %r (ignored natively)"
                            % (where, key))
    action_type = action.get("action")
    if not isinstance(action_type, str) or not action_type.strip():
        errors.append("%s: missing or non-string 'action' type" % where)
        action_type = ""
    elif action_type not in KNOWN_ACTION_TYPES:
        errors.append("%s: unknown action type %r (known: %s)"
                      % (where, action_type, ", ".join(KNOWN_ACTION_TYPES)))
        action_type = ""
    xpath = action.get("xpath")
    if xpath is not None and not isinstance(xpath, str):
        errors.append("%s: xpath must be a string" % where)
        xpath = None
    needs_target = action_type in TARGET_ACTIONS
    if needs_target and not (xpath and xpath.strip()):
        errors.append("%s: %s requires a non-empty xpath locator"
                      % (where, action_type or "?"))
    elif not needs_target and action_type and not (xpath and xpath.strip()):
        warnings.append("%s: %s without xpath (legal, no widget target)"
                        % (where, action_type))
    if isinstance(xpath, str) and xpath.strip() and "(" in xpath and "child::" not in xpath and "[" not in xpath:
        warnings.append("%s: xpath looks like a plain predicate-less "
                        "function call, verify syntax" % where)
    for key in ("throttle", "wait"):
        raw = action.get(key)
        if raw is not None:
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                errors.append("%s: %s must be an int >= 0" % (where, key))
    for key in ("text", "command"):
        if key in action and not isinstance(action[key], str):
            errors.append("%s: %s must be a string" % (where, key))
    for key in ("clearText", "useAdbInput"):
        if key in action and not isinstance(action[key], bool):
            errors.append("%s: %s must be a bool" % where)


def _validate_case(case: Any, index: int, errors: List[str],
                   warnings: List[str]) -> None:
    """Validate one case object (array element) of max.xpath.actions."""
    where = "case#%d" % index
    if not isinstance(case, dict):
        errors.append("%s: case is not a JSON object" % where)
        return
    # unknown keys: warn (native getJsonValue ignores extras; typo guard)
    for key in case:
        if key not in ("prob", "activity", "times", "actions"):
            warnings.append("%s: unknown case key %r (typo?)" % (where, key))
    activity = case.get("activity")
    if not isinstance(activity, str) or not activity.strip():
        errors.append("%s: missing or empty 'activity' "
                      "(native matches the FULL name exactly)" % where)
    else:
        if "." not in activity:
            warnings.append("%s: activity %r has no dot - relative names "
                            "never match native exact equality" % (where, activity))
        if any(ch in activity for ch in "()[]{}*+?|^$"):
            warnings.append("%s: activity %r looks like a regex - native "
                            "matches by exact string equality, not regex"
                            % (where, activity))
    prob = case.get("prob")
    if prob is not None:
        if not isinstance(prob, (int, float)) or isinstance(prob, bool) or not (0.0 <= float(prob) <= 1.0):
            errors.append("%s: prob must be a number in [0, 1]" % where)
    times = case.get("times")
    if times is not None:
        if not isinstance(times, int) or isinstance(times, bool) or times < 1:
            errors.append("%s: times must be an int >= 1" % where)
    actions = case.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("%s: 'actions' must be a non-empty array" % where)
        return
    for pos, action in enumerate(actions, 1):
        _validate_action(action, "%s action#%d" % (where, pos), errors, warnings)


def validate_actions(data: Any) -> Tuple[List[str], List[str]]:
    """Validate the parsed max.xpath.actions root.  Returns (errors, warnings).

    Accepts a bare JSON array (the real format, test/max.xpath.actions and
    Preference.cpp loadActions) or {"actionList": [...]} with a warning.
    """
    warnings: List[str] = []
    if isinstance(data, dict) and "actionList" in data:
        warnings.append("root is an object with 'actionList'; the native "
                        "parser reads a bare JSON array - re-export as array")
        data = data["actionList"]
    if not isinstance(data, list):
        return (["root must be a JSON array of case objects (got %s)"
                 % type(data).__name__], warnings)
    errors: List[str] = []
    if not data:
        warnings.append("config is empty (no custom events)")
        return (errors, warnings)
    for index, case in enumerate(data, 1):
        _validate_case(case, index, errors, warnings)
    return (errors, warnings)


def load_config(path: str) -> Any:
    """Load + JSON-parse the config file.  Raises OSError/ValueError."""
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="push_config.py",
        description="Validate (+ push) the Fastbot max.xpath.actions config (M6).",
    )
    parser.add_argument("config", help="path to the max.xpath.actions JSON")
    parser.add_argument("--serial", default=None,
                        help="device serial (auto-detects single device)")
    parser.add_argument("--package", default=None,
                        help="package name for the rerun hint (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate only; no device contact")
    args = parser.parse_args(argv)

    try:
        data = load_config(args.config)
    except OSError as error:
        print("ERROR: cannot read config: %s" % error, file=sys.stderr)
        return 2
    except ValueError as error:
        print("ERROR: unparsable JSON: %s" % error, file=sys.stderr)
        return 2
    errors, warnings = validate_actions(data)
    for message in warnings:
        print("WARNING: %s" % message)
    for message in errors:
        print("ERROR: %s" % message)
    if errors:
        print("RESULT: INVALID (%d error(s), %d warning(s))"
              % (len(errors), len(warnings)))
        return 1
    print("RESULT: VALID (%d case(s), %d warning(s))"
          % (len(data) if isinstance(data, list) else 0, len(warnings)))
    if args.dry_run:
        print("--dry-run: validation only, nothing pushed")
        return 0
    client = AdbClient(serial=args.serial, timeout=30, retries=2)
    push = client.push(args.config, TARGET_REMOTE)
    print("pushed %s -> %s (rc=%d)" % (args.config, TARGET_REMOTE, push[0]))
    serial = args.serial or client.serial or "<serial>"
    print(RERUN_HINT_FMT.format(serial=serial, package=args.package or "<package>"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
