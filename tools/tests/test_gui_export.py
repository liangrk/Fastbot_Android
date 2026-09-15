"""gui_export.py: dumpsys parsing, set-difference summary, export wiring."""

import json
from pathlib import Path

import common.adb
import gui_export as ge

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent"
DUMPSYS = FIXTURES / "dumpsys_package.txt"

ACTIVITY_DUMP = (
    "  topResumedActivity=ActivityRecord{9abc123 u0 com.example.app/"
    ".DeepLinkActivity t123}" + chr(10) +
    "  mResumedActivity=ActivityRecord{9abc123 u0 com.example.app/"
    ".DeepLinkActivity t123}" + chr(10)
)


class FakeAdb:
    """Scriptable AdbClient stand-in; touchless guard for dry-run."""

    def __init__(self, coverage_exists=True, step_xml=True):
        self.serial = "emu-5554"
        self.coverage_exists = coverage_exists
        self.step_xml = step_xml
        self.calls = []

    def shell(self, cmd):
        self.calls.append(str(cmd))
        text = str(cmd)
        if "dumpsys activity" in text:
            return 0, ACTIVITY_DUMP, ""
        if "dumpsys package" in text:
            return 0, DUMPSYS.read_text(encoding="utf-8"), ""
        if text.startswith("ls -t "):
            if self.step_xml:
                return 0, "/sdcard/fastbot-com.example.app-running-minutes-30/step-3-s-a-1.xml" + chr(10), ""
            return 1, "", "no such file"
        if text.startswith("uiautomator dump"):
            return 0, "UI hierchacy dumped to: /sdcard/fastbot_gui.xml" + chr(10), ""
        if text.startswith("screencap"):
            return 0, "", ""
        return 0, "", ""

    def pull(self, remote, local):
        self.calls.append("pull " + str(remote))
        path = Path(local)
        path.parent.mkdir(parents=True, exist_ok=True)
        if "fastbot_coverage" in str(remote):
            if not self.coverage_exists:
                raise common.adb.AdbError("does not exist")
            path.write_text(json.dumps({
                "version": "1.0", "package": "com.example.app",
                "activities": [
                    {"name": "com.example.app.MainActivity"},
                    {"name": "com.example.app/.DeepLinkActivity"},
                ]}), encoding="utf-8")
            return 0, "", ""
        path.write_bytes(b"<xml/>" if str(remote).endswith(".xml") else b"png")
        return 0, "", ""


def test_dry_run_zero_contact(capsys, monkeypatch):
    def boom(**kw):
        raise AssertionError("device contact in dry-run")

    monkeypatch.setattr(ge, "AdbClient", boom)
    rc = ge.main(["--package", "com.example.app", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "dumpsys package" in out
    assert "uiautomator dump" in out


def test_dumpsys_parser_on_fixture():
    text = DUMPSYS.read_text(encoding="utf-8")
    declared = ge.parse_declared_activities(text, "com.example.app")
    assert declared == [
        "com.example.app.DeepLinkActivity",
        "com.example.app.LoginActivity",
        "com.example.app.MainActivity",
        "com.example.app.SplashActivity",
        "com.example.app.settings.Outer$InnerActivity",
        "com.example.app.settings.SettingsActivity",
    ]


def test_parser_stops_at_section_end():
    text = (
        "  Activities:" + chr(10) +
        "    com.example.app.A" + chr(10) +
        "  Receivers:" + chr(10) +
        "    com.example.app.R" + chr(10)
    )
    assert ge.parse_declared_activities(text, "com.example.app") == [
        "com.example.app.A"]


def test_normalization_cases():
    norm = ge.normalize_activity
    assert norm("com.p/.A", "com.p") == "com.p.A"
    assert norm("com.p/A", "com.p") == "com.p.A"
    assert norm(".A", "com.p") == "com.p.A"
    assert norm("A", "com.p") == "com.p.A"
    assert norm("[com.p.A]", "com.p") == "com.p.A"
    assert norm("com.p.sub.A", "com.p") == "com.p.sub.A"


def test_current_activity_priority():
    text = ("mCurrentFocus=Window{beef u0 com.example.app/com.example.app.MainActivity}" + chr(10) +
            "topResumedActivity=ActivityRecord{1 u0 com.example.app/.SplashActivity t9}")
    current = ge.parse_current_activity(text, "com.example.app")
    assert current == "com.example.app.SplashActivity"


def test_export_writes_summary_and_files(tmp_path):
    client = FakeAdb()
    summary = ge.export(client, "com.example.app", tmp_path)
    assert (tmp_path / "summary.json").is_file()
    assert (tmp_path / "gui.xml").is_file()
    assert (tmp_path / "screenshot.png").is_file()
    assert summary["package"] == "com.example.app"
    assert summary["activity"] == "com.example.app.DeepLinkActivity"
    assert summary["total_declared"] == 6
    assert summary["visited_count"] == 2
    assert summary["unvisited_count"] == 4
    assert summary["gui_xml_source"].startswith("fastbot_step_xml")
    unvisited = summary["unvisited_activities"]
    assert "com.example.app.settings.SettingsActivity" in unvisited
    assert "com.other.pkg.UnrelatedActivity" not in str(unvisited)
    loaded = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert loaded["unvisited_count"] == 4
    assert loaded["notes"] == []


def test_export_step_xml_fallback_chain(tmp_path):
    client = FakeAdb(step_xml=False)
    summary = ge.export(client, "com.example.app", tmp_path)
    assert summary["gui_xml_source"] == "uiautomator_dump"


def test_export_missing_coverage_degrades_with_note(tmp_path):
    client = FakeAdb(coverage_exists=False)
    summary = ge.export(client, "com.example.app", tmp_path)
    assert summary["visited_activities"] == []
    assert summary["visited_count"] == 0
    assert any("coverage JSON missing" in n for n in summary["notes"])
    assert summary["unvisited_count"] == 6
    assert summary["total_declared"] == 6


def test_unvisited_uses_declared_denominator(tmp_path):
    client = FakeAdb()
    summary = ge.export(client, "com.example.app", tmp_path)
    # declared=6, visited=2 -> unvisited=4 (denominator is the FULL
    # declared set, Critic MINOR-3), NOT declared-minus-nothing.
    assert summary["total_declared"] == 6
    assert summary["unvisited_count"] == summary["total_declared"] - summary["visited_count"]
