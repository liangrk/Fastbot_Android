#!/usr/bin/env python3
"""M6 llm-protocol: AC6 baseline vs experiment coverage comparison.

Compares two coverage snapshots (tools/schemas/coverage.schema.json shape)
produced by the M3 device-side exporter:

  python tools/coverage_compare.py baseline.json experiment.json
      [--threshold 15.0] [--fail-under-threshold]
      [--baseline-config max.config] [--experiment-config max.config]
      [--dry-run]

Metrics:
  - Activity 覆盖提升 % = (experiment.visited - baseline.visited)
                         / baseline.visited * 100
    baseline=0 guard: reported as N/A; treated as 100% when the experiment
    visited > 0, else 0% (so --fail-under-threshold fails on an empty run).
  - per-activity added/lost lists
  - widget-level delta: reused from tools/coverage_diff.py diff_snapshots()

AC6 Rev.2 config-consistency clause: with --baseline-config /
--experiment-config the max.chaos.* / max.privacy.* / max.perf.* keys of the
two runs' max.config files are diffed (last-wins Java-Properties semantics);
any mismatch is reported LOUDLY (WARNING lines listing the differing keys).

Baseline definition (printed in the footer):
  同一二进制 + 无 max.xpath.actions + 其余 max.* 键完全一致

Exit codes: 0 = compared (improvement >= threshold or no gate),
1 = improvement under threshold with --fail-under-threshold,
2 = usage/IO errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from chaos_validate import parse_config
from coverage_diff import diff_snapshots, load_snapshot

BASELINE_DEFINITION = (
    "基线定义: 同一二进制 + 无 max.xpath.actions + 其余 max.* 键完全一致"
)
CONFIG_KEY_PREFIXES = ("max.chaos.", "max.privacy.", "max.perf.")


def config_key_map(text: str) -> Dict[str, str]:
    """Last-wins key->value map from max.config text (Java Properties)."""
    result: Dict[str, str] = {}
    for _lineno, key, value in parse_config(text):
        if key is not None:
            result[key] = value
    return result


def compare_config_files(base_text: str, exp_text: str) -> Tuple[List[str], List[str]]:
    """Diff the chaos/privacy/perf keys of two max.config texts.

    Returns (mismatches, notes).  Mismatch lines name both sides' values
    ("" means the key exists on one side only); every key under
    max.chaos./max.privacy./max.perf. participates.
    """
    base_map = {k: v for k, v in config_key_map(base_text).items()
                if k.startswith(CONFIG_KEY_PREFIXES)}
    exp_map = {k: v for k, v in config_key_map(exp_text).items()
               if k.startswith(CONFIG_KEY_PREFIXES)}
    mismatches: List[str] = []
    for key in sorted(set(base_map) | set(exp_map)):
        base_value = base_map.get(key, "<absent>")
        exp_value = exp_map.get(key, "<absent>")
        if base_value != exp_value:
            mismatches.append("%s: baseline=%s experiment=%s"
                              % (key, base_value, exp_value))
    notes = [
        "checked keys: %d (baseline) / %d (experiment)"
        % (len(base_map), len(exp_map)),
    ]
    return mismatches, notes


def activity_name_sets(baseline: Dict[str, Any], experiment: Dict[str, Any]) -> Tuple[set, set]:
    """Visited-activity name sets of the two snapshots."""
    base = {a.get("name", "") for a in baseline.get("activities", [])}
    exp = {a.get("name", "") for a in experiment.get("activities", [])}
    return base, exp


def compare_snapshots(baseline: Dict[str, Any],
                      experiment: Dict[str, Any]) -> Dict[str, Any]:
    """Activity-level improvement + added/lost lists + widget-level delta."""
    base_set, exp_set = activity_name_sets(baseline, experiment)
    base_n = len(base_set)
    exp_n = len(exp_set)
    if base_n == 0:
        improvement = None  # N/A
        effective = 100.0 if exp_n > 0 else 0.0
    else:
        improvement = (exp_n - base_n) / base_n * 100.0
        effective = improvement
    widget_diff = diff_snapshots(baseline, experiment)
    return {
        "baseline_visited": base_n,
        "experiment_visited": exp_n,
        "improvement_pct": improvement,
        "improvement_effective_pct": effective,
        "added_activities": sorted(exp_set - base_set),
        "lost_activities": sorted(base_set - exp_set),
        "widget_summary": widget_diff["summary"],
        "widget_diff_identical": widget_diff["identical"],
    }


def print_result(result: Dict[str, Any], mismatches: List[str],
                 config_notes: List[str], threshold: float,
                 baseline_path: str, experiment_path: str,
                 check_ran: bool = True) -> None:
    """Human-readable output for the comparison result."""
    print("== AC6 覆盖对比 ==")
    print("  基线快照: %s" % baseline_path)
    print("  实验快照: %s" % experiment_path)
    print("  Activity 已访: 基线 %d -> 实验 %d"
          % (result["baseline_visited"], result["experiment_visited"]))
    improvement = result["improvement_pct"]
    if improvement is None:
        print("  覆盖提升: N/A (基线 visited=0; 判定值 %s%%: 实验>0 记 100%%, 否则 0%%)"
              % result["improvement_effective_pct"])
    else:
        print("  覆盖提升: %.2f%%" % improvement)
    print("  阈值: %.2f%%" % threshold)
    print("  新增 Activity (%d): %s" % (len(result["added_activities"]),
          ", ".join(result["added_activities"]) or "-"))
    print("  丢失 Activity (%d): %s" % (len(result["lost_activities"]),
          ", ".join(result["lost_activities"]) or "-"))
    ws = result["widget_summary"]
    print("  控件级 delta (coverage_diff): + %d / - %d / ~ %d (activities + %d / - %d)"
          % (ws["added"], ws["removed"], ws["changed"],
             ws["activities_added"], ws["activities_removed"]))
    for note in config_notes:
        print("  config: %s" % note)
    if not check_ran:
        print("  config: 未提供 --baseline-config/--experiment-config, "
              "一致性检查跳过 (AC6 要求对比运行配置一致)")
    elif mismatches:
        print("  !!! AC6 配置一致性检查未通过 - 对比运行配置不一致 !!!")
        for m in mismatches:
            print("  !!! MISMATCH %s" % m)
    else:
        print("  config: AC6 一致性检查通过 (chaos/privacy/perf 键完全一致)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coverage_compare.py",
        description="AC6 baseline vs experiment coverage comparison (M6).",
    )
    parser.add_argument("baseline", help="baseline coverage JSON")
    parser.add_argument("experiment", help="experiment coverage JSON")
    parser.add_argument("--threshold", type=float, default=15.0,
                        help="improvement threshold %% (default 15.0)")
    parser.add_argument("--fail-under-threshold", action="store_true",
                        help="exit 1 when improvement < threshold")
    parser.add_argument("--baseline-config", default=None,
                        help="max.config of the baseline run (consistency check)")
    parser.add_argument("--experiment-config", default=None,
                        help="max.config of the experiment run (consistency check)")
    parser.add_argument("--out", default=None,
                        help="write diff.json/diff.html here (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the comparison plan and exit (no files)")
    args = parser.parse_args(argv)

    if args.dry_run:
        print("coverage_compare dry-run 对比计划 (无文件写入)")
        print("  baseline: %s" % args.baseline)
        print("  experiment: %s" % args.experiment)
        print("  threshold: %.2f%%" % args.threshold)
        print("  fail-under-threshold: %s" % args.fail_under_threshold)
        print("  config 一致性: %s" % (
            "将检查 (--baseline-config + --experiment-config)"
            if args.baseline_config and args.experiment_config
            else "未提供 config 对, 跳过 (WARNING)"))
        return 0
    if args.baseline_config and not args.experiment_config:
        print("WARNING: 仅提供 --baseline-config; 无法校验配置一致性 "
              "(AC6 Rev.2 要求对比运行配置一致)")
    if args.experiment_config and not args.baseline_config:
        print("WARNING: 仅提供 --experiment-config; 无法校验配置一致性 "
              "(AC6 Rev.2 要求对比运行配置一致)")
    try:
        baseline = load_snapshot(args.baseline)
        experiment = load_snapshot(args.experiment)
    except SystemExit as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2
    mismatches: List[str] = []
    config_notes: List[str] = []
    if args.baseline_config and args.experiment_config:
        try:
            base_text = Path(args.baseline_config).read_text(encoding="utf-8")
            exp_text = Path(args.experiment_config).read_text(encoding="utf-8")
        except OSError as error:
            print("ERROR: cannot read config pair: %s" % error, file=sys.stderr)
            return 2
        mismatches, config_notes = compare_config_files(base_text, exp_text)
    result = compare_snapshots(baseline, experiment)
    check_ran = bool(args.baseline_config and args.experiment_config)
    print_result(result, mismatches, config_notes, args.threshold,
                 args.baseline, args.experiment, check_ran)
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(result)
        payload["config_mismatches"] = mismatches
        (out_dir / "comparison.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print("comparison written: %s" % (out_dir / "comparison.json"))
    print(BASELINE_DEFINITION)
    if args.fail_under_threshold:
        if result["improvement_effective_pct"] < args.threshold:
            print("RESULT: FAIL (improvement %s%% < threshold %s%%)"
                  % (result["improvement_effective_pct"], args.threshold))
            return 1
        print("RESULT: PASS (improvement %s%% >= threshold %s%%)"
              % (result["improvement_effective_pct"], args.threshold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
