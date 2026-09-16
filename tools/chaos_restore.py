#!/usr/bin/env python3
"""Chaos snapshot parsing + restore-command derivation (PC-side mirror).

This module is the Python mirror of the device-side Java restore semantics
in events/base/chaos/AbstractChaosEvent.java (putOrDeleteSetting,
snapshotValue, truthy) and the eight Chaos*Event.restoreState()
implementations: same snapshot payloads, same shell commands.

Per-state restore mapping (verbatim from the Java restoreState bodies):
  battery      -> dumpsys battery reset
  bluetooth    -> truthy(bluetooth_on) ? svc bluetooth enable : svc bluetooth disable
  power_save   -> putOrDelete global low_power (null/empty/"null" -> delete)
  location     -> putOrDelete secure location_mode
  mobile_data  -> truthy(mobile_data) ? svc data enable : svc data disable
  vpn          -> lockdown first, then app (both global always_on_vpn_*)
  dnd          -> cmd notification set_dnd off + putOrDelete global zen_mode
  system_config-> cmd uimode night yes|no + settings put system font_scale

Known-state payloads whose keys cannot be parsed are SKIPPED with a reason
instead of guessed (Java would fall back to disable/1.0); the fallbacks that
are safe by construction (settings delete, font_scale 1.0, night no) are kept
faithful to the Java. Radio channels (bluetooth, mobile_data) never guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

SNAPSHOT_PATH = "/sdcard/fastbot_chaos.snapshot"
SCHEMA_STATES = ("battery", "power_save", "bluetooth", "location",
                 "mobile_data", "vpn", "dnd", "system_config")

# payload keys per state, and how the Java restore decides
_FAITHFUL_FALLBACK_STATES = ("power_save", "location", "vpn", "dnd",
                             "system_config")
# payload keys consumed by each channel's restore (from the Java bodies)
_PAYLOAD_KEYS = {
    "battery": (),  # payload unused: reset clears every override
    "bluetooth": ("bluetooth_on",),
    "power_save": ("low_power",),
    "location": ("location_mode",),
    "mobile_data": ("mobile_data",),
    "vpn": ("always_on_vpn_lockdown", "always_on_vpn_app"),
    "dnd": ("zen_mode",),
    "system_config": ("night_mode", "font_scale"),
}


@dataclass
class RestoreDecision:
    """Per-state restore outcome: exact commands, or a skip reason.

    commands is empty exactly when skip_reason is set.
    """
    state: str
    commands: List[str] = field(default_factory=list)
    skip_reason: Optional[str] = None


def truthy(value: Optional[str]) -> bool:
    """Mirror AbstractChaosEvent.truthy: only "1"/"true" (case-insensitive)."""
    if value is None:
        return False
    token = value.strip()
    return token == "1" or token.lower() == "true"


def parse_pairs(payload: str) -> dict:
    """Parse a snapshot payload ("k=v;k2=v2", newline or semicolon separated)
    the same way AbstractChaosEvent.snapshotValue does."""
    pairs: dict = {}
    if not payload:
        return pairs
    for line in str(payload).splitlines():
        for segment in line.split(";"):
            eq = segment.find("=")
            if eq > 0:
                key = segment[:eq].strip()
                pairs[key] = segment[eq + 1:].strip()
    return pairs


def parse_snapshot_explained(text: Optional[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Parse /sdcard/fastbot_chaos.snapshot content against
    tools/schemas/chaos_snapshot.schema.json (tolerant).

    Returns (states, problems): states is [(state, payload), ...] for every
    well-formed item; problems lists each skipped item as
    "states[N] ..." (or one whole-file reason with no states[N] prefix).
    """
    problems: List[str] = []
    if text is None or not text.strip():
        return [], ["快照为空或不可读"]
    try:
        data = json.loads(text)
    except ValueError as error:
        return [], ["快照 JSON 解析失败: %.120s" % error]
    if not isinstance(data, dict):
        return [], ["快照顶层不是 JSON 对象"]
    states_raw = data.get("states")
    if not isinstance(states_raw, list):
        return [], ["快照缺少 states 数组"]
    states: List[Tuple[str, str]] = []
    for index, item in enumerate(states_raw):
        if not isinstance(item, dict):
            problems.append("states[%d] 不是对象" % index)
            continue
        state = item.get("state")
        value = item.get("value")
        channel = item.get("channel")
        if channel is not None and channel != "shell":
            problems.append("states[%d] 非法 channel: %s" % (index, channel))
            continue
        if not isinstance(state, str) or state not in SCHEMA_STATES:
            problems.append("states[%d] 未知状态: %s" % (index, state))
            continue
        if not isinstance(value, str):
            problems.append("states[%d] value 缺失或非字符串" % index)
            continue
        states.append((state, value))
    return states, problems


def parse_snapshot(text: Optional[str]) -> List[Tuple[str, str]]:
    """Spec signature: well-formed [(state, payload), ...], empty on failure
    (see parse_snapshot_explained for the per-item reasons)."""
    states, _problems = parse_snapshot_explained(text)
    return states


def _put_or_delete(ns: str, key: str, value: Optional[str]) -> List[str]:
    """Mirror AbstractChaosEvent.putOrDeleteSetting: null/empty/"null"
    restores the default by deleting the settings key."""
    if value is None or value == "" or value == "null":
        return ["settings delete %s %s" % (ns, key)]
    return ["settings put %s %s %s" % (ns, key, value)]


def restore_decision(state: str, value: str) -> RestoreDecision:
    """Derive the exact adb shell commands the Java restoreState would run
    for one snapshot item (state + payload value). Unknown states and
    payloads whose required keys cannot be parsed yield a skip reason.
    """
    if state not in SCHEMA_STATES:
        return RestoreDecision(state, skip_reason="未知状态: %s" % state)
    pairs = parse_pairs(value)
    if state == "battery":
        # ChaosBatteryEvent.restoreState ignores the payload entirely
        return RestoreDecision(state, ["dumpsys battery reset"])
    if state == "bluetooth":
        # missing key = cannot determine the original radio state: skip,
        # never guess (Java would fall back to disable)
        raw = pairs.get("bluetooth_on")
        if raw is None:
            return RestoreDecision(state, skip_reason="值不可解析")
        if truthy(raw):
            return RestoreDecision(state, ["svc bluetooth enable"])
        return RestoreDecision(state, ["svc bluetooth disable"])
    if state == "mobile_data":
        raw = pairs.get("mobile_data")
        if raw is None:
            return RestoreDecision(state, skip_reason="值不可解析")
        if truthy(raw):
            return RestoreDecision(state, ["svc data enable"])
        return RestoreDecision(state, ["svc data disable"])
    if state == "power_save":
        return RestoreDecision(state, _put_or_delete(
            "global", "low_power", pairs.get("low_power")))
    if state == "location":
        return RestoreDecision(state, _put_or_delete(
            "secure", "location_mode", pairs.get("location_mode")))
    if state == "vpn":
        lockdown = pairs.get("always_on_vpn_lockdown")
        app = pairs.get("always_on_vpn_app")
        if lockdown is None and app is None:
            return RestoreDecision(state, skip_reason="值不可解析")
        # Java order: lockdown first, then app
        commands = _put_or_delete("global", "always_on_vpn_lockdown", lockdown)
        commands += _put_or_delete("global", "always_on_vpn_app", app)
        return RestoreDecision(state, commands)
    if state == "dnd":
        # Java: set_dnd off first, then putOrDelete the zen_mode setting
        commands = ["cmd notification set_dnd off"]
        commands += _put_or_delete("global", "zen_mode", pairs.get("zen_mode"))
        return RestoreDecision(state, commands)
    if state == "system_config":
        night = pairs.get("night_mode")
        yes = "yes" == night
        commands = ["cmd uimode night %s" % ("yes" if yes else "no")]
        font = pairs.get("font_scale")
        if font is None or font == "" or font == "null":
            font = "1.0"
        commands.append("settings put system font_scale %s" % font)
        return RestoreDecision(state, commands)
    # unreachable: SCHEMA_STATES membership checked above
    return RestoreDecision(state, skip_reason="未知状态: %s" % state)


def restore_commands(state: str, value: str) -> List[str]:
    """Spec signature: command list only (see restore_decision)."""
    return restore_decision(state, value).commands

