"""coverage_compare.py: improvement calc, threshold gate, config consistency."""

import json
from pathlib import Path

import coverage_compare as cc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent"
BASE = FIXTURES / "coverage_baseline.json"
ABOVE = FIXTURES / "coverage_experiment_above.json"
BELOW = FIXTURES / "coverage_experiment_below.json"
ZERO = FIXTURES / "coverage_experiment_zero.json"
CFG_BASE = FIXTURES / "max.config.base"
CFG_SAME = FIXTURES / "max.config.exp.same"
CFG_MISMATCH = FIXTURES / "max.config.exp.mismatch"


def test_improvement_above_threshold_rc0(capsys):
    rc = cc.main([str(BASE), str(ABOVE), "--fail-under-threshold"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "100.00%" in out
    assert "RESULT: PASS" in out
    assert "新增 Activity (2)" in out
    assert "丢失 Activity (0)" in out


def test_improvement_below_threshold_rc0_without_gate(capsys):
    rc = cc.main([str(BASE), str(BELOW)])
    rc2 = cc.main([str(BASE), str(BELOW), "--fail-under-threshold"])
    out = capsys.readouterr().out
    assert rc == 0
    assert rc2 == 1
    assert "RESULT: FAIL" in out
    assert "0.00%" in out


def test_baseline_zero_guard(capsys):
    rc = cc.main([str(ZERO), str(BASE), "--fail-under-threshold"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "N/A" in out
    rc2 = cc.main([str(ZERO), str(ZERO), "--fail-under-threshold"])
    out2 = capsys.readouterr().out
    assert rc2 == 1
    assert "N/A" in out2
    assert "判定值 0.0%" in out2


def test_widget_delta_delegates_to_coverage_diff(capsys):
    rc = cc.main([str(BASE), str(ABOVE)])
    out = capsys.readouterr().out
    assert "控件级 delta" in out
    result = cc.compare_snapshots(
        json.loads(BASE.read_text(encoding="utf-8")),
        json.loads(ABOVE.read_text(encoding="utf-8")))
    assert result["widget_summary"]["activities_added"] == 2
    assert result["added_activities"] == [
        "com.example.app.C", "com.example.app.D"]
    assert result["lost_activities"] == []


def test_config_consistency_same_passes(capsys):
    rc = cc.main([str(BASE), str(ABOVE),
                  "--baseline-config", str(CFG_BASE),
                  "--experiment-config", str(CFG_SAME)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "一致性检查通过" in out


def test_config_consistency_mismatch_warns_loudly(capsys):
    rc = cc.main([str(BASE), str(ABOVE),
                  "--baseline-config", str(CFG_BASE),
                  "--experiment-config", str(CFG_MISMATCH)])
    out = capsys.readouterr().out
    assert rc == 0  # warn, not fail
    assert "配置一致性检查未通过" in out
    assert "MISMATCH max.perf.frame: baseline=true experiment=false" in out


def test_config_pair_half_provided_warns(capsys):
    rc = cc.main([str(BASE), str(ABOVE), "--baseline-config", str(CFG_BASE)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "仅提供 --baseline-config" in out
