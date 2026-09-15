"""privacy_rules.py unit + CLI tests (no device required)."""

from pathlib import Path

import pytest

import privacy_rules as pr

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "privacy"
VALID = FIXTURE_DIR / "rules.valid.json"
INVALID = FIXTURE_DIR / "rules.invalid.json"

# TreeBuilder-shaped GUI XML: bounds is the LAST attribute of every node.
SAMPLE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
    '<node index="0" text="" resource-id="" class="android.widget.FrameLayout"'
    ' package="com.example.app" content-desc="" checkable="false" bounds="[0,0][1080,2280]">'
    '<node index="1" text="同意" resource-id="com.example.app:id/agree"'
    ' class="android.widget.Button" package="com.example.app" content-desc=""'
    ' checkable="false" bounds="[440,1702][640,1802]" />'
    '<node index="2" text="拒绝" resource-id="com.example.app:id/deny"'
    ' class="android.widget.Button" package="com.example.app" content-desc=""'
    ' checkable="false" bounds="[440,1902][640,2002]" />'
    "</node>"
)


def _load_valid():
    return pr.validate_file(VALID)


def test_valid_fixture_passes():
    compiled, errors, warnings = _load_valid()
    assert errors == []
    assert warnings == []
    assert len(compiled) == 4
    assert [r["index"] for r in compiled] == [1, 2, 3, 4]


def test_invalid_fixture_errors():
    compiled, errors, _ = pr.validate_file(INVALID)
    assert compiled == []
    text = "\n".join(errors)
    assert "missing page" in text
    assert "invalid page regex" in text
    assert "invalid widget regex" in text
    assert "action must be one of" in text
    assert "name must be a string" in text
    assert len(errors) == 5


def test_parse_rules_rejects_non_array_root():
    with pytest.raises(ValueError):
        pr.parse_rules('{"page": ".*"}')


def test_parse_rules_rejects_non_object_entry():
    with pytest.raises(ValueError):
        pr.parse_rules('[{"page": ".*"}, "nope"]')


def test_action_resolution_fallback():
    compiled, _, _ = _load_valid()
    by_label = {pr.rule_label(r): r for r in compiled}
    assert pr.resolve_action(by_label["agree-gdpr"]) == "consent"
    assert pr.resolve_action(by_label["rule#3"]) == "consent"  # default fallback (rule omits action)
    assert pr.resolve_action(by_label["rule#3"], default_action="deny") == "deny"
    assert pr.resolve_action(by_label["location-deny-anywhere"]) == "deny"


def test_file_order_priority_first_match_wins():
    compiled, _, _ = _load_valid()
    # rules 1 and 4 both match MainActivity + the agree widget; rule 1 is first.
    hit = pr.match("com.example.app/.MainActivity", SAMPLE_XML, compiled)
    assert pr.rule_label(hit) == "agree-gdpr"
    assert pr.resolve_action(hit) == "consent"


def test_widget_and_page_search_semantics():
    compiled, _, _ = _load_valid()
    # page regex is a search (not full match): suffix matches.
    assert pr.match("com.example.app/.MainActivity", "", compiled) is None  # widget miss, rule2 page miss
    # rule without widget matches regardless of xml
    splash_only = [r for r in compiled if r["page"] == ".*SplashActivity"]
    assert pr.match("com.example.app/.SplashActivity", "", splash_only) is not None


def test_match_returns_none_when_nothing_matches():
    compiled, _, _ = _load_valid()
    assert pr.match("com.other.app/.Whatever", SAMPLE_XML, compiled) is None


def test_find_center_of_matched_widget():
    compiled, _, _ = _load_valid()
    agree = [r for r in compiled if pr.rule_label(r) == "agree-gdpr"][0]
    center = pr.find_center(SAMPLE_XML, agree["widget_re"])
    assert center == (540, 1752)
    # full-xml click targeting uses the whole tree: identical result
    hit_xml = SAMPLE_XML
    assert pr.find_center(hit_xml, agree["widget_re"]) == (540, 1752)


def test_find_center_bounds_targeting_regex():
    re_c = pr.re.compile(pr.re.escape('bounds="[440,1902][640,2002]"'))
    assert pr.find_center(SAMPLE_XML, re_c) == (540, 1952)


def test_find_center_returns_none_without_widget():
    compiled, _, _ = _load_valid()
    splash = [r for r in compiled if r["page"] == ".*SplashActivity"][0]
    assert pr.find_center(SAMPLE_XML, splash.get("widget_re")) is None
    assert pr.find_center(SAMPLE_XML, None) is None


def test_find_center_no_bounds_attr():
    re_c = pr.re.compile('text="orphan"')
    assert pr.find_center('<node text="orphan" other="1" />', re_c) is None


def test_cli_validate_valid(tmp_path, capsys):
    rc = pr.main(["validate", str(VALID)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "privacy rules OK: 4 rule(s)" in out


def test_cli_validate_dry_run_lists_compiled_rules(capsys):
    rc = pr.main(["validate", str(VALID), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "-- dry-run: compiled rules (file order) --" in out
    assert "agree-gdpr" in out
    assert 'page=.*MainActivity' in out


def test_cli_validate_invalid_returns_1(capsys):
    rc = pr.main(["validate", str(INVALID)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "5 error(s)" in out


def test_cli_validate_missing_file_returns_2():
    rc = pr.main(["validate", "no_such_rules_file.json"])
    assert rc == 2
