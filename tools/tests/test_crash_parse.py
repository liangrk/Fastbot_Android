"""crash_parse.py unit tests (no device, no IO besides fixtures)."""

import hashlib
from pathlib import Path

import crash_parse

FIX = Path(__file__).resolve().parent / "fixtures" / "crash"
SAMPLE = FIX / "crash-dump.sample.log"
VARIANTS = FIX / "crash-dump.variants.log"
LOGCAT = FIX / "logcat.sample.txt"

NPE_SIG = "1db1f4dad7d1fe6c"


def _dump_text():
    return SAMPLE.read_text(encoding="utf-8")


def _records():
    return crash_parse.parse_dump_records(_dump_text())


def test_normalize_frame_line_numbers():
    assert crash_parse.normalize_frame(
        "// \tat a.b.C.d(X.java:42)") == "at a.b.C.d(X.java)"


def test_normalize_frame_addresses():
    assert crash_parse.normalize_frame(
        "at a.b.C.d(0x7f8a1b2c)") == "at a.b.C.d(0xADDR)"
    assert crash_parse.normalize_frame(
        "at a.b.C.d(X.java:0xDEADBEEF)") == "at a.b.C.d(X.java:0xADDR)"


def test_normalize_frame_native_method_untouched():
    assert crash_parse.normalize_frame(
        "at a.b.C.d(Native method)") == "at a.b.C.d(Native method)"


def test_normalize_frame_skips_non_frame():
    assert crash_parse.normalize_frame("Caused by: java.io.IOException: x") == ""


def test_normalize_frame_glue_prefixes():
    for prefix in crash_parse.GLUE_PREFIXES:
        frame = "at %sFoo.bar(Foo.java:1)" % prefix
        assert crash_parse.normalize_frame(frame) == ""


def test_normalize_stack_cap():
    frames = ["at a.B%d.c(X.java:1)" % i for i in range(12)]
    assert len(crash_parse.normalize_stack(frames, 8)) == 8
    assert len(crash_parse.normalize_stack(frames, 3)) == 3


def test_signature_of_vector():
    sig = crash_parse.signature_of(
        "java.lang.Throwable", ["at a.B.c(X)"])
    assert sig == hashlib.sha1(b"java.lang.Throwable\nat a.B.c(X)").hexdigest()[:16]
    assert len(sig) == 16


def test_exception_type():
    assert crash_parse.exception_type(
        "java.lang.NullPointerException: msg") == "java.lang.NullPointerException"
    assert crash_parse.exception_type("ANR in com.example.app") == "ANR in com.example.app"


def test_extract_activities():
    line = "ANR in com.example.app/com.example.app.SettingsActivity"
    assert crash_parse.extract_activities(line) == ["com.example.app/com.example.app.SettingsActivity"]
    assert crash_parse.extract_activities("ANR in com.example.app") == []
    assert crash_parse.extract_activities("Activity: com.example.app/.Main") == ["com.example.app/.Main"]
    assert crash_parse.extract_activities("nothing here") == []


def test_split_records_counts():
    recs = crash_parse.split_records(_dump_text())
    assert len(recs) == 8
    assert "crash end" not in recs[-1]
    assert "crash end" in recs[0]


def test_split_records_no_marker():
    assert crash_parse.split_records("no records here\n") == []


def test_parse_record_crash():
    rec = crash_parse.parse_record(crash_parse.split_records(_dump_text())[0])
    assert rec is not None
    assert rec["kind"] == "crash"
    assert rec["time"] == "20260915100001"
    assert rec["process"] == "com.example.app"
    assert rec["pid"] == 4241
    assert rec["version_code"] == 7
    assert rec["exception_line"].startswith("java.lang.NullPointerException")
    assert len(rec["frames"]) == 5
    assert rec["source"] == "crash_dump"


def test_parse_record_anr():
    rec = crash_parse.parse_record(crash_parse.split_records(_dump_text())[6])
    assert rec["kind"] == "anr"
    assert rec["exception_line"] == "ANR in com.example.app"
    assert rec["activities"] == []
    assert rec["version_code"] == 7


def test_parse_logcat_records():
    text = LOGCAT.read_text(encoding="utf-8")
    recs = crash_parse.parse_logcat(text)
    assert len(recs) == 2
    fatal, anr = recs
    assert fatal["kind"] == "crash" and fatal["source"] == "logcat"
    assert fatal["process"] == "com.example.app"
    assert fatal["pid"] == 4250
    assert fatal["exception_line"] == "java.lang.ArithmeticException: divide by zero"
    kept = crash_parse.normalize_stack(fatal["frames"], 8)
    assert kept == [
        "at com.example.app.calc.Calculator.divide(Calculator.java)",
        "at com.example.app.calc.Calculator.access$100(Calculator.java)",
        "at com.example.app.ui.MenuActivity.onOption(MenuActivity.java)",
    ]
    assert anr["kind"] == "anr"
    assert anr["exception_line"] == "ANR in com.example.app/com.example.app.SettingsActivity"
    assert anr["activities"] == ["com.example.app/com.example.app.SettingsActivity"]


def test_parse_logcat_timestamped_line():
    line = "09-15 10:00:40.100 E/tag( 123): FATAL EXCEPTION: main"
    recs = crash_parse.parse_logcat(line)
    assert len(recs) == 1
    assert recs[0]["time"] == "00000915100040"


def test_logcat_time_helper():
    assert crash_parse._logcat_time("09-15 10:00:40.100 E/tag( 1): x") == "00000915100040"
    assert crash_parse._logcat_time("E/tag( 1): x") == ""


def test_cluster_fields():
    clusters = crash_parse.cluster(_records(), 8)
    assert len(clusters) == 5
    npe = clusters[0]
    assert npe["count"] == 3
    assert npe["kind"] == "crash"
    assert npe["signature"] == NPE_SIG
    assert npe["first_seen"] == "20260915100001"
    assert min(r["time"] for r in _records() if r["kind"] == "crash") == "20260915100001"
    assert npe["last_seen"] == "20260915100003"
    assert npe["exception_line"].startswith("java.lang.NullPointerException")
    assert npe["top_frames"] == [
        "at com.example.app.Foo.bar(Foo.java)",
        "at com.example.app.Foo.onCreate(Foo.java)",
        "at com.example.app.MainActivity.onCreate(MainActivity.java)",
    ]
    assert npe["processes"] == ["com.example.app"]
    assert npe["version_codes"] == [7]
    assert npe["activities"] == []
    assert "Foo.bar(Foo.java:42)" in npe["representative_stack"]


def test_cluster_order_and_anr_kind():
    clusters = crash_parse.cluster(_records(), 8)
    assert [c["count"] for c in clusters] == [3, 2, 1, 1, 1]
    anr_rows = [c for c in clusters if c["kind"] == "anr"]
    assert len(anr_rows) == 1
    dump_anr = [c for c in anr_rows if c["exception_line"] == "ANR in com.example.app"]
    assert len(dump_anr) == 1 and dump_anr[0]["count"] == 1


def test_representative_is_earliest():
    clusters = crash_parse.cluster(_records(), 8)
    npe = clusters[0]
    assert "Foo.bar(Foo.java:42)" in npe["representative_stack"]


def test_variants_same_signatures():
    sig_s = [c["signature"] for c in crash_parse.cluster(_records(), 8)]
    recs_v = crash_parse.parse_dump_records(VARIANTS.read_text(encoding="utf-8"))
    sig_v = [c["signature"] for c in crash_parse.cluster(recs_v, 8)]
    assert sig_s == sig_v


def test_diff_clusters_categories():
    base = [
        {"signature": "aaaa", "count": 2, "exception_line": "X: kept worse"},
        {"signature": "bbbb", "count": 2, "exception_line": "Y: kept better"},
        {"signature": "cccc", "count": 1, "exception_line": "Z: gone"},
    ]
    cur = [
        {"signature": "aaaa", "count": 5, "exception_line": "X: kept worse"},
        {"signature": "bbbb", "count": 1, "exception_line": "Y: kept better"},
        {"signature": "dddd", "count": 1, "exception_line": "W: new"},
    ]
    diff = crash_parse.diff_clusters(base, cur)
    assert [r["signature"] for r in diff["new"]] == ["dddd"]
    assert [r["signature"] for r in diff["gone"]] == ["cccc"]
    assert diff["worse"] == [{"signature": "aaaa", "exception_line": "X: kept worse",
                              "baseline_count": 2, "current_count": 5}]
    assert [r["signature"] for r in diff["better"]] == ["bbbb"]


def test_diff_clusters_equal_counts():
    rows = [{"signature": "aaaa", "count": 2, "exception_line": "X"}]
    diff = crash_parse.diff_clusters(rows, [dict(rows[0])])
    assert diff == {"new": [], "gone": [], "worse": [], "better": []}
