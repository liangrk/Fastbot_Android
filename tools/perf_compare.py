#!/usr/bin/env python3
"""AC-PG perf-gate: 四指标性能回归门禁 (语义仿 coverage_compare).

比较两次 Fastbot run 的 perf 产物目录 (perf_poller.py 输出布局):

    python tools/perf_compare.py <baseline_dir> <experiment_dir>
        [--max-cpu-regression 10.0] [--max-pss-regression 10.0]
        [--max-cold-p90-regression 15.0] [--max-jank-regression 0.5]
        [--out DIR] [--dry-run]

每个目录期望: perf/data.json (perf_sample JSONL, 必需), perf/starts.json
(可选), perf/*_frames.csv (可选, 取首个按名排序匹配)。缺失目录或
perf/data.json → exit 2; 可选输入缺失/不可读 → 对应指标 N/A。

指标 (基线侧; 实验侧对称):
  cpu       metric_stats(samples, "cpu_percent")["avg"]   相对 % 回归
  pss       metric_stats(samples, "mem_pss_kb")["avg"]    相对 % 回归
  cold_p90  nearest-rank p90 of duration_ms over kind=="cold"
            (与 perf_report 同一最近秩公式)                相对 % 回归
  jank_rate 100 * count(frame_time_ms > 16.67) / 幸存行数, 行来自
            parse_framestats_csv + sanity guard (0 < ft <= 5000ms)
                                                        绝对 pp 差

N/A 规则: 任一侧取不到值 (0 数值样本 / 0 冷启动事件 / 0 幸存帧行) →
该指标行 N/A, 排除在门禁之外, 永不导致失败。
基线=0 规则 (AC-PG2, 四指标统一): 展示 N/A; 判定值 = 实验>0 ? 100.0 : 0.0
(jank 同理: 判定 pp 差 = 实验>0 ? 100.0 : 0.0)。

jank 口径注记 (M1): jank_rate 是本期新增定义 (60Hz 帧预算, framestats
边车口径, frame_time_ms > 16.67ms 行占比), 与一期 perf_report 的 gfxinfo
summary janky_frames (vsync-deadline 语义) 口径不同、不可直接互比; 120Hz
设备 (如验收机 PHK110) 上 16.67ms ≈ 错过 2 个 vsync — 仍为有效 jank 信号
但偏保守。perf_frame schema 的 Flags 列留作将来对齐 gfxinfo 口径的扩展位。

--out DIR 写入 perf_gate_result.json。这是本次比较的派生产物 (机器可读
留档), 刻意不冻结 schema — 不在 spec 的两个冻结 schema 之列。

基线定义 (页脚打印): 同一设备 + 同一二进制 + 先基线后实验。

Exit codes: 0 = 全部门禁通过 / 1 = 任一 MISMATCH / 2 = usage-IO 错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from perf_report import (
    MAX_PLAUSIBLE_FRAME_MS,
    load_samples,
    metric_stats,
    parse_framestats_csv,
)

FRAME_BUDGET_MS = 16.67
BASELINE_DEFINITION = "基线定义: 同一设备 + 同一二进制 + 先基线后实验"
JANK_NOTE = (
    "jank 口径注记 (M1): jank_rate = framestats 边车 frame_time_ms > 16.67ms 行占比, "
    "本期新增定义 (60Hz 帧预算口径), 与一期 perf_report gfxinfo summary janky_frames "
    "(vsync-deadline 语义) 口径不同、不可直接互比; 120Hz 设备上 16.67ms ≈ 错过 2 个 vsync, "
    "仍为有效 jank 信号但偏保守; perf_frame schema 的 Flags 列留作对齐 gfxinfo 口径的扩展位。"
)
RESULT_FILE_NAME = "perf_gate_result.json"

METRIC_ORDER = ("cpu", "pss", "cold_p90", "jank_rate")
RELATIVE_METRICS = frozenset(("cpu", "pss", "cold_p90"))
METRIC_LABELS = {
    "cpu": "cpu (cpu_percent 均值)",
    "pss": "pss (mem_pss_kb 均值)",
    "cold_p90": "cold_p90 (冷启动 p90, ms)",
    "jank_rate": "jank_rate (帧预算超限占比, %)",
}


def load_perf_dir(dir_path: str) -> Dict[str, Any]:
    """Load <dir>/perf/data.json + optional starts.json + first *_frames.csv.

    Raises NotADirectoryError/FileNotFoundError (→ exit 2) for missing dir or
    missing perf/data.json. Optional inputs unreadable → WARNING + N/A.
    """
    root = Path(dir_path)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    data_path = root / "perf" / "data.json"
    if not data_path.is_file():
        raise FileNotFoundError(str(data_path))
    samples, malformed = load_samples(data_path)
    if malformed:
        print("WARNING: %s: %d malformed JSONL line(s) skipped"
              % (data_path, malformed), file=sys.stderr)
    starts: List[Dict[str, Any]] = []
    starts_path = root / "perf" / "starts.json"
    if starts_path.is_file():
        try:
            for raw in starts_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line:
                    starts.append(json.loads(line))
        except (OSError, ValueError) as error:
            print("WARNING: cannot read %s: %s" % (starts_path, error),
                  file=sys.stderr)
            starts = []
    frames_text: Optional[str] = None
    frames_path: Optional[Path] = None
    try:
        candidates = sorted(root.glob("perf/*_frames.csv"))
        if candidates:
            frames_path = candidates[0]
            frames_text = candidates[0].read_text(
                encoding="utf-8", errors="replace")
    except OSError as error:
        print("WARNING: cannot read frames csv: %s" % error, file=sys.stderr)
        frames_text = None
    return {
        "dir": str(root),
        "samples": samples,
        "malformed": malformed,
        "starts": starts,
        "frames_text": frames_text,
        "frames_path": frames_path,
    }


def cold_start_p90(starts: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Nearest-rank p90 of duration_ms over kind=="cold" events.

    Same nearest-rank formula as perf_report.frame_time_stats._pct (a closure
    there, so the one-line formula is replicated to stay identical)."""
    values = sorted(float(s["duration_ms"]) for s in starts
                    if s.get("kind") == "cold"
                    and isinstance(s.get("duration_ms"), (int, float)))
    if not values:
        return None
    position = min(len(values) - 1, int(round(90 / 100.0 * (len(values) - 1))))
    return values[position]


def jank_rate_from_rows(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Percentage of surviving rows (sanity guard: 0 < ft <= 5000ms) whose
    frame_time_ms exceeds the 16.67ms (60Hz) frame budget. None = no rows."""
    surviving = [row for row in rows
                 if 0 < row["frame_time_ms"] <= MAX_PLAUSIBLE_FRAME_MS]
    if not surviving:
        return None
    janky = sum(1 for row in surviving if row["frame_time_ms"] > FRAME_BUDGET_MS)
    return 100.0 * janky / len(surviving)


def evaluate_metric(name: str, base_val: Optional[float],
                    exp_val: Optional[float], threshold: float,
                    absolute: bool = False) -> Dict[str, Any]:
    """One metric row: N/A handling, baseline=0 rule, mismatch decision."""
    row: Dict[str, Any] = {
        "metric": name,
        "baseline": base_val,
        "experiment": exp_val,
        "regression": None,
        "effective": None,
        "threshold": threshold,
        "absolute": absolute,
        "na": base_val is None or exp_val is None,
        "baseline_zero": False,
        "mismatch": False,
    }
    if row["na"]:
        return row
    if base_val == 0.0:
        # AC-PG2 uniform baseline=0 rule: display N/A, gate on 100/0.
        row["baseline_zero"] = True
        row["effective"] = 100.0 if exp_val > 0 else 0.0
    else:
        if absolute:
            row["regression"] = exp_val - base_val
        else:
            row["regression"] = (exp_val - base_val) / base_val * 100.0
        row["effective"] = row["regression"]
    row["mismatch"] = row["effective"] > threshold
    return row


def evaluate_all(base: Dict[str, Any], exp: Dict[str, Any],
                 thresholds: Dict[str, float]) -> List[Dict[str, Any]]:
    """Evaluate the four metrics in fixed order from loaded perf dirs."""
    base_cpu = metric_stats(base["samples"], "cpu_percent")
    exp_cpu = metric_stats(exp["samples"], "cpu_percent")
    base_pss = metric_stats(base["samples"], "mem_pss_kb")
    exp_pss = metric_stats(exp["samples"], "mem_pss_kb")
    base_frames = parse_framestats_csv(base["frames_text"] or "", report=[])
    exp_frames = parse_framestats_csv(exp["frames_text"] or "", report=[])
    rows = [
        evaluate_metric(
            "cpu",
            base_cpu["avg"] if base_cpu else None,
            exp_cpu["avg"] if exp_cpu else None,
            thresholds["cpu"]),
        evaluate_metric(
            "pss",
            base_pss["avg"] if base_pss else None,
            exp_pss["avg"] if exp_pss else None,
            thresholds["pss"]),
        evaluate_metric(
            "cold_p90",
            cold_start_p90(base["starts"]),
            cold_start_p90(exp["starts"]),
            thresholds["cold_p90"]),
        evaluate_metric(
            "jank_rate",
            jank_rate_from_rows(base_frames),
            jank_rate_from_rows(exp_frames),
            thresholds["jank_rate"], absolute=True),
    ]
    return rows


def _fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return "%.2f" % value


def _fmt_change(row: Dict[str, Any]) -> str:
    if row["na"]:
        return "N/A"
    if row["baseline_zero"]:
        return ("N/A (基线=0; 判定值 %.2f%s: 实验>0 记 100%%, 否则 0%%)"
                % (row["effective"], "pp" if row["absolute"] else "%"))
    unit = "pp" if row["absolute"] else "%"
    return "%+0.2f%s" % (row["regression"], unit)


def _fmt_judgment(row: Dict[str, Any]) -> str:
    if row["na"]:
        return "N/A (门禁豁免)"
    return "MISMATCH" if row["mismatch"] else "PASS"


def _fmt_mismatch(row: Dict[str, Any]) -> str:
    unit = "pp" if row["absolute"] else "%"
    return ("MISMATCH %s: baseline=%s experiment=%s 实际回归 %+0.2f%s 阈值 %+0.2f%s"
            % (row["metric"], _fmt_num(row["baseline"]),
               _fmt_num(row["experiment"]), row["effective"], unit,
               row["threshold"], unit))


def print_result(rows: List[Dict[str, Any]],
                 baseline_dir: str, experiment_dir: str) -> None:
    """Human-readable output mirroring coverage_compare style."""
    print("== AC-PG 性能回归门禁 ==")
    print("  基线目录: %s" % baseline_dir)
    print("  实验目录: %s" % experiment_dir)
    for name in METRIC_ORDER:
        row = rows[METRIC_ORDER.index(name)]
        print("  %s: 基线 %s / 实验 %s / 变化 %s / 阈值 %+0.2f%s / 判定 %s"
              % (METRIC_LABELS[name], _fmt_num(row["baseline"]),
                 _fmt_num(row["experiment"]), _fmt_change(row),
                 row["threshold"], "pp" if row["absolute"] else "%",
                 _fmt_judgment(row)))
    mismatches = [row for row in rows if row["mismatch"]]
    for row in mismatches:
        print("  %s" % _fmt_mismatch(row))
    print("RESULT: %s" % ("FAIL" if mismatches else "PASS"))


def build_payload(rows: List[Dict[str, Any]], thresholds: Dict[str, float],
                  baseline_dir: str, experiment_dir: str) -> Dict[str, Any]:
    """Machine-readable derived artifact (deliberately not a frozen schema)."""
    return {
        "baseline_dir": baseline_dir,
        "experiment_dir": experiment_dir,
        "thresholds": {name: thresholds[name] for name in METRIC_ORDER},
        "metrics": {row["metric"]: {
            "baseline": row["baseline"],
            "experiment": row["experiment"],
            "regression": row["regression"],
            "effective": row["effective"],
            "threshold": row["threshold"],
            "absolute": row["absolute"],
            "na": row["na"],
            "baseline_zero": row["baseline_zero"],
            "mismatch": row["mismatch"],
        } for row in rows},
        "result": "FAIL" if any(row["mismatch"] for row in rows) else "PASS",
        "baseline_definition": BASELINE_DEFINITION,
        "jank_definition_note": JANK_NOTE,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="perf_compare.py",
        description="AC-PG 四指标性能回归门禁 (cpu/pss/cold_p90/jank_rate).",
    )
    parser.add_argument("baseline", help="基线 perf 目录 (含 perf/data.json)")
    parser.add_argument("experiment", help="实验 perf 目录 (含 perf/data.json)")
    parser.add_argument("--max-cpu-regression", type=float, default=10.0,
                        help="cpu 均值相对回归阈值 %% (default 10.0)")
    parser.add_argument("--max-pss-regression", type=float, default=10.0,
                        help="pss 均值相对回归阈值 %% (default 10.0)")
    parser.add_argument("--max-cold-p90-regression", type=float, default=15.0,
                        help="cold_p90 相对回归阈值 %% (default 15.0)")
    parser.add_argument("--max-jank-regression", type=float, default=0.5,
                        help="jank_rate 绝对 pp 差阈值 (default 0.5)")
    parser.add_argument("--out", default=None,
                        help="write perf_gate_result.json here (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the judgment plan and exit (no writes)")
    args = parser.parse_args(argv)

    thresholds = {
        "cpu": args.max_cpu_regression,
        "pss": args.max_pss_regression,
        "cold_p90": args.max_cold_p90_regression,
        "jank_rate": args.max_jank_regression,
    }
    if args.dry_run:
        print("perf_compare dry-run 判定计划 (零设备接触, 无文件写入)")
        print("  基线目录: %s" % args.baseline)
        print("  实验目录: %s" % args.experiment)
        print("  期望输入 (每目录): perf/data.json; 可选 perf/starts.json, "
              "perf/*_frames.csv (首个按名排序)")
        for name in METRIC_ORDER:
            unit = "pp" if name == "jank_rate" else "%"
            print("  指标 %-9s 阈值 %+0.2f%s" % (name, thresholds[name], unit))
        print("  N/A 规则: 任一侧缺数据 → N/A 行, 门禁豁免")
        print("  基线=0 规则: 判定值 = 实验>0 ? 100.0 : 0.0")
        return 0

    try:
        base = load_perf_dir(args.baseline)
        exp = load_perf_dir(args.experiment)
    except (NotADirectoryError, FileNotFoundError, OSError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2

    rows = evaluate_all(base, exp, thresholds)
    print_result(rows, str(base["dir"]), str(exp["dir"]))
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = build_payload(rows, thresholds, str(base["dir"]),
                                str(exp["dir"]))
        out_path = out_dir / RESULT_FILE_NAME
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
        print("perf gate result written: %s" % out_path)
    print(BASELINE_DEFINITION)
    print(JANK_NOTE)
    if any(row["mismatch"] for row in rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
