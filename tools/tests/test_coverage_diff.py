"""coverage_diff.py unit + CLI tests (no device required)."""

import json
from pathlib import Path


import coverage_diff

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "coverage"
OLD = FIXTURE_DIR / "diff_old.json"
NEW = FIXTURE_DIR / "diff_new.json"
IDENT_A = FIXTURE_DIR / "ident_a.json"
IDENT_B = FIXTURE_DIR / "ident_b.json"
FALLBACK_OLD = FIXTURE_DIR / "fallback_old.json"
FALLBACK_NEW = FIXTURE_DIR / "fallback_new.json"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run_cli(tmp_path, old, new, extra_args=()):
    from coverage_diff import main
    out_dir = tmp_path / "out"
    rc = main([str(old), str(new), "--out", str(out_dir)] + list(extra_args))
    return rc, out_dir / "diff.json", out_dir / "diff.html"


def test_widget_key_priority():
    w = {"resource_id": "a:id/x", "text": "t", "content_desc": "c", "path": "p"}
    assert coverage_diff.widget_key(w) == ("resource_id", "a:id/x")
    assert coverage_diff.widget_key(
        {"resource_id": None, "text": "t", "content_desc": "c", "path": "p"}
    ) == ("text", "t")
    assert coverage_diff.widget_key(
        {"resource_id": None, "text": None, "content_desc": "c", "path": "p"}
    ) == ("content_desc", "c")
    assert coverage_diff.widget_key(
        {"resource_id": None, "text": None, "content_desc": None, "path": "p"}
    ) == ("path", "p")


def test_widget_key_empty_falls_to_path_empty_string():
    w = {"resource_id": None, "text": None, "content_desc": None, "path": ""}
    assert coverage_diff.widget_key(w) == ("path", "")
    assert coverage_diff.widget_key({}) == ("path", "")


def test_added_removed_changed_detected(tmp_path):
    rc, diff_path, html_path = _run_cli(tmp_path, OLD, NEW)
    assert rc == 0
    diff = _load(diff_path)
    by_name = {a["name"]: a for a in diff["activities"]}

    main_act = by_name["com.example.app.MainActivity"]
    assert main_act["status"] == "modified"
    # Settings (text-keyed) removed
    assert any(w.get("text") == "Settings" for w in main_act["removed_widgets"])
    # Search widget changed only in content_desc ("Search" -> "搜索"), same text key
    changed = main_act["changed_widgets"]
    assert len(changed) == 1
    assert changed[0]["key_field"] == "text"
    assert changed[0]["old"]["content_desc"] == "Search"
    assert changed[0]["new"]["content_desc"] == "搜索"
    # path-only GridView/ImageButton added
    assert any(w["path"].startswith("android.widget.GridView") for w in main_act["added_widgets"])

    assert by_name["com.example.app.LegacyActivity"]["status"] == "removed"
    assert by_name["com.example.app.NewActivity"]["status"] == "added"


def test_summary_counts(tmp_path):
    rc, diff_path, _ = _run_cli(tmp_path, OLD, NEW)
    assert rc == 0
    summary = _load(diff_path)["summary"]
    assert summary["added"] == 2
    assert summary["removed"] == 2
    assert summary["changed"] == 1
    assert summary["activities_added"] == 1
    assert summary["activities_removed"] == 1
    diff = _load(diff_path)
    assert diff["identical"] is False


def test_identical_snapshots_determinism_pass(tmp_path, capsys):
    rc, diff_path, _ = _run_cli(tmp_path, IDENT_A, IDENT_B, extra_args=["--determinism-check"])
    assert rc == 0
    assert _load(diff_path)["identical"] is True
    assert "PASSED" in capsys.readouterr().out


def test_determinism_check_fails_with_report(tmp_path, capsys):
    rc, diff_path, _ = _run_cli(tmp_path, OLD, NEW, extra_args=["--determinism-check"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "determinism check FAILED" in captured.out
    assert "com.example.app.MainActivity" in captured.out
    # visit_count/captured_at drift alone must not fail determinism
    assert diff_path.exists()


def test_nullable_fields_fall_back_to_path_key(tmp_path):
    rc, diff_path, _ = _run_cli(tmp_path, FALLBACK_OLD, FALLBACK_NEW)
    assert rc == 0
    activity = _load(diff_path)["activities"][0]
    # CheckBox widget (all nullable fields null) matched via path key on both sides
    # -> it is neither added nor removed
    assert activity["status"] == "modified"
    assert activity["added_widgets"] == [
        {"resource_id": None, "text": None, "content_desc": None,
         "path": "android.widget.FrameLayout/android.widget.ImageButton"}
    ]
    assert activity["removed_widgets"] == [
        {"resource_id": None, "text": None, "content_desc": "Share",
         "path": "android.widget.FrameLayout/android.widget.ImageButton"}
    ]
    assert activity["changed_widgets"] == []


def test_outputs_written(tmp_path):
    rc, diff_path, html_path = _run_cli(tmp_path, OLD, NEW)
    assert rc == 0
    diff = _load(diff_path)
    assert diff["summary"]["added"] == 2
    html = html_path.read_text(encoding="utf-8")
    assert "覆盖率差异报告" in html
    assert "生成时间" in html


def test_visit_count_drift_alone_is_not_a_difference(tmp_path):
    import copy
    old = _load(IDENT_A)
    new = copy.deepcopy(old)
    new["captured_at"] = "2027-01-01T00:00:00Z"
    new["activities"][0]["visit_count"] = 99
    old_path = tmp_path / "o.json"
    new_path = tmp_path / "n.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    new_path.write_text(json.dumps(new), encoding="utf-8")
    rc, diff_path, _ = _run_cli(tmp_path, old_path, new_path, extra_args=["--determinism-check"])
    assert rc == 0
    assert _load(diff_path)["identical"] is True
