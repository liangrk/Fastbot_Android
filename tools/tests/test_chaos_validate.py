"""chaos_validate.py unit + CLI tests (no device required)."""

from pathlib import Path

import pytest

import chaos_validate as cv

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "chaos"
VALID = FIXTURE_DIR / "max.config.valid"
INVALID = FIXTURE_DIR / "max.config.invalid"
OFF = FIXTURE_DIR / "max.config.off"


def _pairs(text):
    return cv.parse_config(text)


def test_parse_basic():
    pairs = _pairs("max.chaos.enable = true\n\n# comment\nmax.chaos.dnd.pct = 0.1\n")
    assert pairs == [(1, "max.chaos.enable", "true"), (4, "max.chaos.dnd.pct", "0.1")]


def test_parse_no_spaces_and_inline_junk():
    pairs = _pairs("max.chaos.enable=true\nmax.chaos.dnd.pct=0.1\n")
    assert pairs == [
        (1, "max.chaos.enable", "true"),
        (2, "max.chaos.dnd.pct", "0.1"),
    ]


def test_parse_malformed_line_flagged():
    pairs = _pairs("max.chaos.enable = true\nnot_a_pair\n")
    assert pairs[1] == (2, None, "not_a_pair")
    errors, _ = cv.validate_config(pairs)
    assert any("unparsable" in e for e in errors)


def test_valid_fixture_passes():
    pairs = cv.parse_config(VALID.read_text(encoding="utf-8"))
    errors, warnings = cv.validate_config(pairs)
    assert errors == []
    assert warnings == []


def test_invalid_fixture_errors():
    pairs = cv.parse_config(INVALID.read_text(encoding="utf-8"))
    errors, warnings = cv.validate_config(pairs)
    text = "\n".join(errors)
    assert "enable" in text
    assert "battery" in text
    assert "vpn" in text
    assert "maxConcurrent" in text
    assert "timeoutSec" in text
    assert any("mobiledata" in e for e in errors)
    assert any("sysconfig2" in w for w in warnings)
    assert any("wooo" in w for w in warnings)


def test_chaos_off_with_pct_warns():
    pairs = cv.parse_config(OFF.read_text(encoding="utf-8"))
    errors, warnings = cv.validate_config(pairs)
    assert errors == []
    assert any("chaos will not run" in w for w in warnings)
    plan = cv.build_plan(pairs)
    assert plan["enabled"] is False
    assert plan["states"] == []


def test_plan_defaults_and_schema_mapping():
    plan = cv.build_plan(cv.parse_config("max.chaos.enable = true\n"))
    assert plan == {
        "enabled": True,
        "states": [],
        "max_concurrent": 1,
        "timeout_sec": 5,
    }


def test_plan_states_and_values():
    plan = cv.build_plan(cv.parse_config(VALID.read_text(encoding="utf-8")))
    assert plan["enabled"] is True
    assert plan["max_concurrent"] == 2
    assert plan["timeout_sec"] == 8
    by_state = {s["state"]: s for s in plan["states"]}
    assert by_state["battery"]["pct"] == pytest.approx(0.3)
    assert by_state["powersave"]["schema_state"] == "power_save"
    assert by_state["dnd"]["schema_state"] == "dnd"
    assert "mobiledata" not in by_state  # pct absent -> not scheduled


def test_plan_ignores_unparsable_pct():
    plan = cv.build_plan(cv.parse_config("max.chaos.dnd.pct = abc\n"))
    assert plan["states"] == []


def test_cli_dry_run_valid(tmp_path, capsys):
    rc = cv.main([str(VALID), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RESULT: VALID" in out
    assert "chaos plan" in out
    assert "battery" in out
    assert "power_save" in out
    assert "no device touched" in out


def test_cli_dry_run_invalid_exit_code(tmp_path, capsys):
    rc = cv.main([str(INVALID), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: INVALID" in out
    assert "ERROR:" in out


def test_cli_missing_file_exit_2(capsys):
    rc = cv.main([str(FIXTURE_DIR / "does_not_exist.config")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found" in err
