"""push_config.py: max.xpath.actions validation + push wiring tests."""

from pathlib import Path

import pytest

import common.adb
import push_config as pc

TOOLS_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent"
VALID = FIXTURES / "xpath.actions.valid.json"
INVALID = FIXTURES / "xpath.actions.invalid.json"


class FakeAdb:
    def __init__(self, *a, **k):
        self.pushed = []
        self.serial = "emu-5554"

    def push(self, local, remote):
        self.pushed.append((local, remote))
        return (0, "", "")


def test_valid_fixture_passes():
    errors, warnings = pc.validate_actions(pc.load_config(VALID))
    assert errors == []
    assert warnings == []


def test_valid_fixture_main_dry_run(capsys):
    rc = pc.main([str(VALID), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RESULT: VALID" in out
    assert "--dry-run: validation only" in out


def test_invalid_fixture_errors_and_rc1(capsys):
    rc = pc.main([str(INVALID), "--dry-run"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "unknown action type 'TAP'" in out
    assert "CLICK requires a non-empty xpath locator" in out
    assert "xpath must be a string" in out
    assert "throttle must be an int >= 0" in out
    assert "missing or empty 'activity'" in out
    assert "'actions' must be a non-empty array" in out
    assert "RESULT: INVALID" in out


def test_prob_and_times_validation():
    case = {"prob": 1.5, "times": 0, "activity": "com.a.A", "actions": [
        {"action": "CLICK", "xpath": "//*[@text='x']"}]}
    errors, _w = pc.validate_actions([case])
    assert any("prob must be a number in [0, 1]" in e for e in errors)
    assert any("times must be an int >= 1" in e for e in errors)


def test_actionList_alias_tolerated_with_warning():
    data = {"actionList": [{"activity": "com.a.A", "actions": [
        {"action": "BACK"}]}]}
    errors, warnings = pc.validate_actions(data)
    assert errors == []
    assert any("actionList" in w for w in warnings)


def test_back_without_xpath_warns():
    errors, warnings = pc.validate_actions([
        {"activity": "com.a.A", "actions": [{"action": "BACK"}]}])
    assert errors == []
    assert any("BACK without xpath" in w for w in warnings)


def test_regex_activity_warns():
    errors, warnings = pc.validate_actions([
        {"activity": "Regexp[Config].*Activity", "actions": [
            {"action": "BACK"}]}])
    assert errors == []
    assert any("exact string equality" in w for w in warnings)


def test_empty_config_warns():
    errors, warnings = pc.validate_actions([])
    assert errors == []
    assert any("config is empty" in w for w in warnings)


def test_root_not_list_errors():
    errors, warnings = pc.validate_actions({"activity": "x"})
    assert len(errors) == 1
    assert "root must be a JSON array" in errors[0]
    assert warnings == []


def test_push_wiring(monkeypatch, capsys):
    client = FakeAdb()
    monkeypatch.setattr(pc, "AdbClient", lambda **kw: client)
    rc = pc.main([str(VALID), "--package", "com.example.app"])
    assert rc == 0
    assert client.pushed == [(str(VALID), "/sdcard/max.xpath.actions")]
    out = capsys.readouterr().out
    assert "pushed" in out
    assert "CLASSPATH=/sdcard/monkeyq.jar" in out
    assert "-p com.example.app --agent reuseq" in out


def test_push_failure_is_reported(monkeypatch, capsys):
    class Boom(FakeAdb):
        def push(self, local, remote):
            raise common.adb.AdbError("device offline")

    monkeypatch.setattr(pc, "AdbClient", lambda **kw: Boom())
    with pytest.raises(common.adb.AdbError):
        pc.main([str(VALID)])
    out = capsys.readouterr().out
    assert "RESULT: VALID" in out
