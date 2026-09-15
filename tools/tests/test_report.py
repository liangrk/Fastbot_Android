"""render_report / write_report tests (no device required)."""

import pytest

from common.report import render_report, write_report


def test_title_and_charset_present():
    out = render_report("性能测试报告", [])
    assert "性能测试报告" in out
    assert '<meta charset="utf-8">' in out
    assert out.lstrip().lower().startswith("<!doctype html>")


def test_summary_renders_table_rows():
    rows = [["包名", "com.example.app"], ["平均FPS", "58.5"]]
    out = render_report("报告", [{"type": "summary", "rows": rows}])
    assert "<table>" in out and "</table>" in out
    assert "<th>项目</th>" in out and "<th>值</th>" in out
    assert "<td>包名</td>" in out and "<td>com.example.app</td>" in out


def test_series_renders_inline_svg():
    points = [(0, 60), (1, 58.2), (2, 30), (3, 55)]
    out = render_report("t", [{"type": "series", "name": "FPS 曲线", "points": points}])
    assert "<svg" in out and "</svg>" in out
    assert "<polyline" in out
    assert "FPS 曲线" in out


def test_series_empty_points_still_renders_svg():
    out = render_report("t", [{"type": "series", "name": "空曲线", "points": []}])
    assert "<svg" in out and "</svg>" in out


def test_html_section_passthrough():
    body = "<p>自定义<b>内容</b></p>"
    out = render_report("t", [{"type": "html", "body": body}])
    assert body in out


def test_unknown_section_type_raises():
    with pytest.raises(ValueError):
        render_report("t", [{"type": "nope"}])


def test_summary_escapes_html_in_values():
    out = render_report("t", [{"type": "summary", "rows": [["x", "<script>"]]}])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_write_report_roundtrip(tmp_path):
    target = tmp_path / "nested" / "report.html"
    content = render_report("落盘报告", [{"type": "summary", "rows": [["k", "v"]]}])
    result = write_report(target, content)
    assert result == target
    assert target.read_text(encoding="utf-8") == content
