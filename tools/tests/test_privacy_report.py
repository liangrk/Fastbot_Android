"""privacy_report.py unit + CLI tests (no device required)."""

import json
from pathlib import Path

from jsonschema import validate

import privacy_report as prepo

TOOLS_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "privacy"
SAMPLE = FIXTURE_DIR / "audit.sample.jsonl"
SCHEMA = TOOLS_DIR / "schemas" / "audit.schema.json"


def test_parse_audit_fixture():
    records, malformed = prepo.parse_audit(SAMPLE)
    assert len(records) == 6
    assert len(malformed) == 2
    assert malformed[0][0] == 7  # the not-json line
    assert "not a JSON object" not in str(malformed)
    assert malformed[1] == (8, "type must be one of ['permission_prompt', 'sensitive_api', 'rule_hit']")


def test_parse_audit_text_garbage_never_crashes():
    records, malformed = prepo.parse_audit_text("zzz" + chr(10) + chr(0) + chr(10) + "[]" + chr(10))
    assert records == []
    assert len(malformed) == 3


def test_check_record_rejections():
    assert prepo._check_record("nope") == "not a JSON object"
    assert prepo._check_record({"ts": 1.5}) is not None
    assert prepo._check_record({"ts": True}) is not None  # bool is not a valid ts
    assert prepo._check_record({"ts": 1}) is not None  # missing activity/detail/type/source
    assert prepo._check_record({
        "ts": 1, "type": "rule_hit", "activity": "a", "detail": "d", "source": "device",
    }) is None


def test_detail_value():
    detail = "rule=agree-gdpr;action=consent;permission=隐私政策"
    assert prepo.detail_value(detail, "action") == "consent"
    assert prepo.detail_value(detail, "rule") == "agree-gdpr"
    assert prepo.detail_value(detail, "permission") == "隐私政策"
    assert prepo.detail_value(detail, "missing") is None
    assert prepo.detail_value("no pairs here", "rule") is None


def test_aggregate_counts():
    records, _ = prepo.parse_audit(SAMPLE)
    stats = prepo.aggregate(records)
    assert stats["total"] == 6
    assert stats["by_type"] == prepo.Counter({"rule_hit": 4, "permission_prompt": 1, "sensitive_api": 1})
    assert stats["by_action"] == prepo.Counter({"consent": 3, "deny": 1, "-": 2})
    assert stats["by_permission"] == prepo.Counter({"隐私政策": 2, "AUTO_START": 1, "位置": 1, "(无)": 2})
    assert stats["first_ts"] == 1789497600000
    assert stats["last_ts"] == 1789497900000


def test_build_sections_and_render(tmp_path):
    records, malformed = prepo.parse_audit(SAMPLE)
    sections = prepo.build_sections(records, malformed, tmp_path)
    kinds = [s["type"] for s in sections]
    assert kinds == ["summary"] + ["html"] * 5
    html = prepo.render_report("t", sections)
    for label in ("概览", "按事件类型", "按权限聚合", "按页面聚合", "按处理动作", "事件时间线"):
        assert label in html
    assert 'lang="zh-CN"' in html


def test_timeline_links_screenshots_relpath(tmp_path):
    shots = tmp_path / "screenshots"
    shots.mkdir()
    real_shot = shots / "hit-1.png"
    real_shot.write_bytes(b"\x89PNG fake")
    records, malformed = prepo.parse_audit(SAMPLE)
    records[0] = dict(records[0])
    records[0]["screenshot"] = "screenshots/hit-1.png"
    html = prepo.render_report("t", prepo.build_sections(records, malformed, tmp_path))
    # existing file -> relative link; missing file -> plain text
    assert 'href="screenshots%shit-1.png"' % ("\\" if __import__("os").name == "nt" else "/") in html
    assert "screenshots/hit-1789497780000-2.png" in html


def test_timeline_shows_malformed_rows():
    records, malformed = prepo.parse_audit(SAMPLE)
    html = prepo.render_report("t", prepo.build_sections(records, malformed, None))
    assert "第 7 行解析失败" in html
    assert "第 8 行解析失败" in html


def test_fixture_lines_validate_against_audit_schema():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    records, malformed = prepo.parse_audit(SAMPLE)
    assert len(records) == 6 and len(malformed) == 2
    for record in records:
        validate(instance=record, schema=schema)


def test_cli_dry_run_prints_summary(capsys):
    rc = prepo.main([str(SAMPLE), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "事件总数: 6" in out
    assert "规则命中: 4" in out
    assert "解析失败行: 2" in out


def test_cli_writes_html(tmp_path, capsys):
    out_file = tmp_path / "report.html"
    rc = prepo.main([str(SAMPLE), "--out", str(out_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out_file.exists()
    html = out_file.read_text(encoding="utf-8")
    assert "按权限聚合" in html
    assert "事件时间线" in html
    assert "screenshots/hit-1789497780000-2.png" in html
    assert 'lang="zh-CN"' in html


def test_cli_missing_audit_file_returns_2():
    assert prepo.main(["missing_audit.jsonl", "--dry-run"]) == 2
