"""crash_report.py CLI tests (fixtures only, no device)."""

import json
from pathlib import Path

import jsonschema

import crash_report

FIX = Path(__file__).resolve().parent / "fixtures" / "crash"
DUMP = FIX / "crash-dump.sample.log"
VARIANTS = FIX / "crash-dump.variants.log"
LOGCAT = FIX / "logcat.sample.txt"
PREV = FIX / "prev_clusters.jsonl"
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"

NPE_SIG = "1db1f4dad7d1fe6c"


def _run(tmp_path, argv):
    out = tmp_path / "out"
    rc = crash_report.main([str(x) for x in argv] + ["--out", str(out)])
    return rc, out


def _rows(path):
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def test_dry_run_before_validation(tmp_path, capsys):
    rc, out = _run(tmp_path, ["E:/no/such/dump.log", "--dry-run"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "dry-run" in text
    assert "E:/no/such/dump.log" in text
    assert not out.exists()


def test_missing_input_rc2(tmp_path, capsys):
    rc, out = _run(tmp_path, ["E:/no/such/dump.log"])
    assert rc == 2


def test_missing_logcat_rc2(tmp_path):
    rc, out = _run(tmp_path, [DUMP, "--logcat", "E:/no/such/logcat.txt"])
    assert rc == 2


def test_bad_baseline_rc2(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    rc, out = _run(tmp_path, [DUMP, "--baseline", bad])
    assert rc == 2


def test_e2e_outputs_and_schemas(tmp_path):
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT])
    assert rc == 0
    clusters_path = out / "clusters.jsonl"
    root_path = out / "root_cause.jsonl"
    html_path = out / "crash_report.html"
    assert clusters_path.exists() and root_path.exists() and html_path.exists()
    clusters = _rows(clusters_path)
    assert [c["count"] for c in clusters] == [3, 2, 1, 1, 1, 1, 1]
    cluster_schema = json.loads((SCHEMAS / "crash_cluster.schema.json").read_text(encoding="utf-8"))
    pack_schema = json.loads((SCHEMAS / "root_cause_pack.schema.json").read_text(encoding="utf-8"))
    for row in clusters:
        jsonschema.validate(row, cluster_schema)
    for row in _rows(root_path):
        jsonschema.validate(row, pack_schema)
    html = html_path.read_text(encoding="utf-8")
    assert crash_report.TOP_HEADER in html
    assert "TopN" in html


def test_rerun_byte_identical(tmp_path):
    rc1, out1 = _run(tmp_path, [DUMP, "--logcat", LOGCAT])
    rc2, out2 = _run(tmp_path, [DUMP, "--logcat", LOGCAT, "--out", str(tmp_path / "out2")])
    assert rc1 == 0 and rc2 == 0
    assert (out1 / "clusters.jsonl").read_bytes() == (out2 / "clusters.jsonl").read_bytes()
    assert (out1 / "root_cause.jsonl").read_bytes() == (out2 / "root_cause.jsonl").read_bytes()


def test_determinism_check_rc0(tmp_path):
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT, "--determinism-check"])
    assert rc == 0


def test_determinism_check_rc1(tmp_path, monkeypatch, capsys):
    original = crash_report._render_payloads
    calls = {"n": 0}

    def fake(dump_text, logcat_text, max_frames):
        calls["n"] += 1
        result = original(dump_text, logcat_text, max_frames)
        if calls["n"] == 2:
            lines = result[2].splitlines()
            lines[0] = lines[0].replace('"count": 3', '"count": 9', 1)
            result = (result[0], result[1], "\n".join(lines) + "\n", result[3])
        return result

    monkeypatch.setattr(crash_report, "_render_payloads", fake)
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT, "--determinism-check"])
    assert rc == 1
    assert "determinism check FAILED" in capsys.readouterr().out


def test_top_n(tmp_path, capsys):
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT, "--top", "2"])
    assert rc == 0
    text = capsys.readouterr().out
    top_lines = [l for l in text.splitlines() if "  Top " in l]
    assert len(top_lines) == 2
    html = (out / "crash_report.html").read_text(encoding="utf-8")
    assert html.count("<h3>#") == 2


def test_frames_flag_changes_signatures(tmp_path):
    rc8, out8 = _run(tmp_path, [DUMP, "--frames", "8"])
    assert rc8 == 0
    sigs8 = [r["signature"] for r in _rows(out8 / "clusters.jsonl")]
    rc1, out1 = _run(tmp_path, [DUMP, "--frames", "1", "--out", str(tmp_path / "out2")])
    assert rc1 == 0
    rows1 = _rows(out1 / "clusters.jsonl")
    sigs1 = [r["signature"] for r in rows1]
    assert sigs8 != sigs1
    assert all(len(r["top_frames"]) == 1 for r in rows1 if r["top_frames"])


def test_fail_new_crash_rc1(tmp_path, capsys):
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT,
                              "--baseline", PREV, "--fail-new-crash"])
    assert rc == 1
    text = capsys.readouterr().out
    assert "RESULT: FAIL" in text
    new_lines = [l for l in text.splitlines() if "NEW SIGNATURE" in l]
    assert len(new_lines) == 3
    assert "diff vs baseline: new=3 gone=1 worse=1 better=1" in text


def test_fail_new_crash_rc0_when_no_new(tmp_path):
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT, "--baseline", PREV])
    assert rc == 0
    rc2, out2 = _run(tmp_path, [DUMP, "--logcat", LOGCAT, "--out", str(tmp_path / "o2"),
                                "--baseline", str(tmp_path / "out" / "clusters.jsonl"),
                                "--fail-new-crash"])
    assert rc2 == 0


def test_root_cause_last_scene_is_latest(tmp_path):
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT])
    assert rc == 0
    packs = _rows(out / "root_cause.jsonl")
    npe = [r for r in packs if r["signature"] == NPE_SIG][0]
    scene = npe["last_scene"]
    assert scene["seen"] == "20260915100003"
    assert scene["process"] == "com.example.app"
    assert scene["version_code"] == 7
    assert "Foo.bar(Foo.java:424242)" in scene["stack"]
    assert scene["seen"] == npe["last_seen"]
    assert npe["generated_by"] == "crash_report"
    assert "Foo.bar(Foo.java:42)" in npe["representative_stack"]


def test_variants_normalization_gate(tmp_path, capsys):
    rc, out = _run(tmp_path, [DUMP, "--logcat", LOGCAT])
    base = out / "clusters.jsonl"
    rc2, out2 = _run(tmp_path, [VARIANTS, "--logcat", LOGCAT,
                                "--baseline", str(base), "--fail-new-crash",
                                "--out", str(tmp_path / "o2")])
    assert rc2 == 0
    text = capsys.readouterr().out
    assert "new=0 gone=0" in text


def test_fail_new_crash_without_baseline_rc2(capsys):
    rc = crash_report.main(["whatever.log", "--fail-new-crash"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "--baseline" in captured.err
