"""Doc-contract tests: agent_protocol.md §11 (v2 closed loop) must stay in sync
with the actual CLI surfaces — primitives, stop conditions, gate semantics.
The protocol doc IS the PromptPack consumed by external agents, so any drift
between doc and CLI breaks AC-AA1; these tests pin the load-bearing strings.
"""

from pathlib import Path

PROTO = Path(__file__).resolve().parents[1] / "agent_protocol.md"


def _text() -> str:
    return PROTO.read_text(encoding="utf-8")


def test_v2_section_present():
    text = _text()
    assert "闭环驱动 (v2" in text
    assert "八步循环" in text
    assert "停止条件" in text


def test_v1_semiauto_intact():
    text = _text()
    assert "半自动闭环" in text
    assert "基线定义" in text
    assert "不含任何 LLM 代码" in text


def test_all_primitives_documented():
    text = _text()
    for name in (
        "fastbot_run.py",
        "crash_report.py",
        "gui_export.py",
        "push_config.py",
        "coverage_compare.py",
        "perf_compare.py",
        "weaknet.py",
    ):
        assert name in text, "missing primitive: %s" % name


def test_stop_conditions_three():
    text = _text()
    for marker in ("轮数上限", "覆盖目标达成", "崩溃门禁触发"):
        assert marker in text, "missing stop condition: %s" % marker


def test_gate_semantics_pinned():
    text = _text()
    assert "--threshold 15" in text
    assert "--fail-under-threshold" in text
    assert "--fail-new-crash" in text
    assert "--clean-actions" in text


def test_root_cause_consumption_documented():
    text = _text()
    assert "root_cause.jsonl" in text


def test_zero_contact_rehearsal_sequence():
    text = _text()
    assert "零接触演练" in text
    assert text.count("--dry-run") >= 6
    assert "fastbot_run.py --package com.example.app --dry-run" in text


def test_claude_code_sample_present():
    text = _text()
    assert "Claude Code" in text
    assert "闭环驱动 Agent" in text
