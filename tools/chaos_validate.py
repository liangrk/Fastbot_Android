"""Parse and validate Fastbot max.config chaos keys (no device required).

Usage:
    python tools/chaos_validate.py path/to/max.config --dry-run
    python tools/chaos_validate.py path/to/max.config

Exit codes: 0 = valid (warnings allowed), 1 = validation errors,
2 = usage/IO errors (missing file, unparsable).

The device-side run also writes /sdcard/fastbot_chaos.snapshot (JSON matching
tools/schemas/chaos_snapshot.schema.json); this validator works purely on the
max.config text.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Config state keys (max.chaos.<state>.pct) - must match utils/Config.java
KNOWN_STATES = (
    "battery", "powersave", "bluetooth", "location",
    "mobiledata", "vpn", "dnd", "sysconfig",
)

# chaos_snapshot.schema.json enum values per config state
SCHEMA_STATE = {
    "battery": "battery",
    "powersave": "power_save",
    "bluetooth": "bluetooth",
    "location": "location",
    "mobiledata": "mobile_data",
    "vpn": "vpn",
    "dnd": "dnd",
    "sysconfig": "system_config",
}

DEFAULTS = {
    "enable": False,
    "pct": 0.0,
    "max_concurrent": 1,
    "timeout_sec": 5,
}


def parse_config(text: str) -> List[Tuple[int, Optional[str], str]]:
    """Parse Java-Properties-style lines from max.config content.

    Returns a list of (lineno, key, value) triples in file order; malformed
    lines (no '=' separator) yield key=None with the raw line as value.
    Full-line comments and blank lines are skipped.
    """
    pairs = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            pairs.append((lineno, None, line))
            continue
        key, _, value = line.partition("=")
        pairs.append((lineno, key.strip(), value.strip()))
    return pairs


def validate_config(pairs) -> Tuple[List[str], List[str]]:
    """Validate chaos keys. Returns (errors, warnings).

    Rules (ralplan M1):
    - max.chaos.enable must be true/false
    - max.chaos.<state>.pct must be a float in [0, 1]; unknown state warns
    - max.chaos.maxConcurrent must be an int >= 1
    - max.chaos.timeoutSec must be an int >= 1
    - any other max.chaos.* key warns (typo protection)
    - chaos disabled but pct > 0 set warns (keys would have no effect)
    """
    errors: List[str] = []
    warnings: List[str] = []
    seen_pct = False
    for lineno, key, value in pairs:
        if key is None:
            errors.append("line %d: unparsable line (missing '='): %s" % (lineno, value))
            continue
        if key == "max.chaos.enable":
            if value.lower() not in ("true", "false"):
                errors.append("line %d: max.chaos.enable must be true/false, got %r" % (lineno, value))
            continue
        if key.startswith("max.chaos."):
            key_body = key[len("max.chaos."):]
            if key_body.endswith(".pct"):
                state = key_body[: -len(".pct")]
                if state not in KNOWN_STATES:
                    warnings.append(
                        "line %d: unknown chaos state %r (known: %s)"
                        % (lineno, state, ", ".join(KNOWN_STATES))
                    )
                    continue
                try:
                    pct = float(value)
                except ValueError:
                    errors.append(
                        "line %d: max.chaos.%s.pct must be a number, got %r"
                        % (lineno, state, value)
                    )
                    continue
                if not (0.0 <= pct <= 1.0):
                    errors.append(
                        "line %d: max.chaos.%s.pct must be in [0, 1], got %r"
                        % (lineno, state, value)
                    )
                if pct > 0:
                    seen_pct = True
                continue
            if key_body == "maxConcurrent":
                try:
                    mc = int(value)
                except ValueError:
                    errors.append(
                        "line %d: max.chaos.maxConcurrent must be an integer, got %r"
                        % (lineno, value)
                    )
                    continue
                if mc < 1:
                    errors.append(
                        "line %d: max.chaos.maxConcurrent must be >= 1, got %r"
                        % (lineno, value)
                    )
                continue
            if key_body == "timeoutSec":
                try:
                    ts = int(value)
                except ValueError:
                    errors.append(
                        "line %d: max.chaos.timeoutSec must be an integer, got %r"
                        % (lineno, value)
                    )
                    continue
                if ts < 1:
                    errors.append(
                        "line %d: max.chaos.timeoutSec must be >= 1, got %r"
                        % (lineno, value)
                    )
                continue
            warnings.append("line %d: unknown chaos key %r" % (lineno, key))
    if seen_pct and not _chaos_enabled(pairs):
        warnings.append(
            "max.chaos.<state>.pct > 0 present but max.chaos.enable is not true;"
            " chaos will not run"
        )
    return errors, warnings


def _chaos_enabled(pairs) -> bool:
    """Last-wins scan for max.chaos.enable (Java Properties semantics)."""
    enabled = DEFAULTS["enable"]
    for _lineno, key, value in pairs:
        if key == "max.chaos.enable":
            enabled = value.lower() == "true"
    return enabled


def _last_value(pairs, key):
    """Last-wins value for key, or None."""
    result = None
    for _lineno, k, value in pairs:
        if k == key:
            result = value
    return result


def _int_value(pairs, key, default):
    """Last-wins integer value for key, or default when absent/unparsable."""
    raw = _last_value(pairs, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def build_plan(pairs) -> dict:
    """Effective chaos plan (last-wins semantics), independent of validation."""
    enabled = _chaos_enabled(pairs)
    states = []
    max_concurrent = _int_value(pairs, "max.chaos.maxConcurrent", DEFAULTS["max_concurrent"])
    timeout_sec = _int_value(pairs, "max.chaos.timeoutSec", DEFAULTS["timeout_sec"])
    if not enabled:
        return {
            "enabled": False,
            "states": [],
            "max_concurrent": max_concurrent,
            "timeout_sec": timeout_sec,
        }
    for state in KNOWN_STATES:
        raw = _last_value(pairs, "max.chaos.%s.pct" % state)
        if raw is None:
            continue
        try:
            pct = float(raw)
        except ValueError:
            pct = DEFAULTS["pct"]
        if pct <= 0:
            continue
        states.append(
            {
                "state": state,
                "schema_state": SCHEMA_STATE[state],
                "pct": pct,
            }
        )
    max_concurrent = _int_value(pairs, "max.chaos.maxConcurrent", DEFAULTS["max_concurrent"])
    timeout_sec = _int_value(pairs, "max.chaos.timeoutSec", DEFAULTS["timeout_sec"])
    return {
        "enabled": enabled,
        "states": states,
        "max_concurrent": max_concurrent,
        "timeout_sec": timeout_sec,
    }


def format_plan(plan: dict) -> str:
    """Render the effective chaos plan as human-readable text."""
    lines = []
    if not plan["enabled"]:
        lines.append("chaos DISABLED (max.chaos.enable is not true)")
    lines.append("max_concurrent: %d" % plan["max_concurrent"])
    lines.append("timeout_sec:    %d" % plan["timeout_sec"])
    if plan["states"]:
        lines.append("states:")
        for st in plan["states"]:
            lines.append(
                "  - %-11s pct=%-5s (schema: %s)"
                % (st["state"], st["pct"], st["schema_state"])
            )
    else:
        lines.append("states: none enabled (all max.chaos.<state>.pct are 0/absent)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Fastbot max.config chaos keys (PC side, no device)."
    )
    parser.add_argument("config", help="path to the max.config file to check")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the effective chaos plan without touching a device",
    )
    args = parser.parse_args(argv)

    path = Path(args.config)
    if not path.is_file():
        print("ERROR: config file not found: %s" % path, file=sys.stderr)
        return 2
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print("ERROR: cannot read %s: %s" % (path, exc), file=sys.stderr)
        return 2
    pairs = parse_config(text)
    errors, warnings = validate_config(pairs)
    for w in warnings:
        print("WARNING: " + w)
    for e in errors:
        print("ERROR: " + e)
    if args.dry_run:
        print("--- chaos plan (dry-run, no device touched) ---")
        print(format_plan(build_plan(pairs)))
    if errors:
        print("RESULT: INVALID (%d error(s), %d warning(s))" % (len(errors), len(warnings)))
        return 1
    print("RESULT: VALID (%d warning(s))" % len(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
