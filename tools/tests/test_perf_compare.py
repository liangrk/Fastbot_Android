"""perf_compare.py: 四指标门禁 — rc 契约 / N-A / 基线=0 / jank 边界 / dry-run / --out."""

import json
from pathlib import Path

import perf_compare as pc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "perf_compare"
BASE = FIXTURES / "base"
EXP = FIXTURES / "exp"
EXP_BREACH = FIXTURES / "exp_breach"
EXP_MULTI = FIXTURES / "exp_multi"
EXP_NA = FIXTURES / "exp_na"

COLD_DEFAULT = (1000.0, 1100.0, 1200.0, 1300.0, 1400.0, 1500.0)


def frame_row(i, janky):
    ft_ns = 20_000_000 if i in janky else 8_000_000
    iv = 1_000_000_000 + i * 10_000_000
    return "%d,%d,%d,%d" % (i * 10, 0, iv, iv + ft_ns)


def write_frames(path, n, janky):
    lines = ["ts_ms,Flags,IntendedVsync,FrameCompleted"]
    lines += [frame_row(i, janky) for i in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def synth_dir(tmp_path, name, cpu=(10.0, 20.0), pss=(100000.0, 110000.0),
              cold=COLD_DEFAULT, n_frames=40, janky=frozenset((9, 19, 29, 39))):
    """Synthesize a perf run dir; cpu/pss entries may be None (key omitted)."""
    perf = tmp_path / name / "perf"
    perf.mkdir(parents=True)
    samples = []
    for i, (c, v) in enumerate(zip(cpu, pss)):
        sample = {"ts": 1000 + i, "serial": "t", "source": "test"}
        if c is not None:
            sample["cpu_percent"] = c
        if v is not None:
            sample["mem_pss_kb"] = v
        samples.append(json.dumps(sample))
    (perf / "data.json").write_text("\n".join(samples) + "\n", encoding="utf-8")
    if cold is not None:
        starts = [json.dumps({"time": "10:00:%02d.000" % (i * 10), "pid": 100,
                              "activity": "com.example/.Main",
                              "duration_ms": d, "kind": "cold"})
                  for i, d in enumerate(cold)]
        (perf / "starts.json").write_text("\n".join(starts) + "\n",
                                          encoding="utf-8")
    if n_frames:
        write_frames(perf / "run_frames.csv", n_frames, janky)
    return tmp_path / name


def test_all_pass_rc0(capsys):
    rc = pc.main([str(BASE), str(EXP)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RESULT: PASS" in out
    assert "MISMATCH" not in out
    assert "基线 15.00 / 实验 15.50 / 变化 +3.33%" in out
    assert "基线 105000.00 / 实验 107500.00 / 变化 +2.38%" in out
    assert "基线 1400.00 / 实验 1500.00 / 变化 +7.14%" in out
    assert "变化 +0.00pp" in out
    assert "先基线后实验" in out
    assert "16.67ms" in out


def test_single_metric_breach_rc1(capsys):
    rc = pc.main([str(BASE), str(EXP_BREACH)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: FAIL" in out
    assert "MISMATCH cpu: baseline=15.00 experiment=17.00" in out
    assert "MISMATCH pss" not in out
    assert "判定 MISMATCH" in out
    assert out.count("MISMATCH cpu") == 1


def test_multi_metric_breach_rc1_both_lines(capsys):
    rc = pc.main([str(BASE), str(EXP_MULTI)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: FAIL" in out
    assert "MISMATCH cpu: baseline=15.00 experiment=17.00" in out
    assert "MISMATCH pss: baseline=105000.00 experiment=121800.00" in out
    assert "MISMATCH cold_p90" not in out
    assert "MISMATCH jank_rate" not in out
    assert out.count("MISMATCH cpu") == 1
    assert out.count("MISMATCH pss") == 1


def test_missing_data_na_rc0(capsys):
    rc = pc.main([str(BASE), str(EXP_NA)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RESULT: PASS" in out
    assert out.count("N/A (门禁豁免)") == 4


def test_baseline_zero_cpu_both_branches(tmp_path, capsys):
    base = synth_dir(tmp_path, "base", cpu=(0.0, 0.0))
    exp_pos = synth_dir(tmp_path, "exp_pos", cpu=(12.0, 12.0))
    exp_zero = synth_dir(tmp_path, "exp_zero", cpu=(0.0, 0.0))
    rc = pc.main([str(base), str(exp_pos)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "N/A (基线=0; 判定值 100.00%: 实验>0 记 100%, 否则 0%)" in out
    assert "MISMATCH cpu" in out
    rc = pc.main([str(base), str(exp_zero)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "N/A (基线=0; 判定值 0.00%: 实验>0 记 100%, 否则 0%)" in out
    assert "RESULT: PASS" in out


def test_baseline_zero_jank_both_branches(tmp_path, capsys):
    base = synth_dir(tmp_path, "base", n_frames=40, janky=frozenset())
    exp_pos = synth_dir(tmp_path, "exp_pos", n_frames=40,
                        janky=frozenset((9, 19, 29, 39)))
    exp_zero = synth_dir(tmp_path, "exp_zero", n_frames=40, janky=frozenset())
    rc = pc.main([str(base), str(exp_pos)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "变化 N/A (基线=0; 判定值 100.00pp" in out
    assert "MISMATCH jank_rate" in out
    rc = pc.main([str(base), str(exp_zero)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "变化 N/A (基线=0; 判定值 0.00pp" in out
    assert "RESULT: PASS" in out


def test_jank_boundary_passes_at_05(tmp_path, capsys):
    base = synth_dir(tmp_path / "a", "base", n_frames=200,
                     janky=frozenset(range(20)))
    exp = synth_dir(tmp_path / "a", "exp", n_frames=200,
                    janky=frozenset(range(21)))
    rc = pc.main([str(base), str(exp)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "变化 +0.50pp" in out
    assert "RESULT: PASS" in out


def test_jank_boundary_fails_at_06(tmp_path, capsys):
    base = synth_dir(tmp_path / "b", "base", n_frames=500,
                     janky=frozenset(range(50)))
    exp = synth_dir(tmp_path / "b", "exp", n_frames=500,
                    janky=frozenset(range(53)))
    rc = pc.main([str(base), str(exp)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "变化 +0.60pp" in out
    assert "MISMATCH jank_rate" in out


def test_cold_p90_and_jank_unit_values():
    starts = [json.loads(line)
              for line in (BASE / "perf" / "starts.json")
              .read_text(encoding="utf-8").splitlines() if line]
    assert pc.cold_start_p90(starts) == 1400.0
    shuffled = list(reversed(starts))
    assert pc.cold_start_p90(shuffled) == 1400.0
    warm_only = [dict(s, kind="warm") for s in starts]
    assert pc.cold_start_p90(warm_only) is None
    assert pc.cold_start_p90([]) is None
    frames_text = (BASE / "perf" / "run_frames.csv").read_text(encoding="utf-8")
    rows = pc.parse_framestats_csv(frames_text, report=[])
    assert pc.jank_rate_from_rows(rows) == 10.0
    assert pc.jank_rate_from_rows([]) is None


def test_dry_run_rc0_no_writes(tmp_path, capsys):
    out_dir = tmp_path / "gate_out"
    rc = pc.main([str(BASE), str(EXP), "--dry-run", "--out", str(out_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out
    for name in ("cpu", "pss", "cold_p90", "jank_rate"):
        assert name in out
    assert "10.00" in out and "15.00" in out and "0.50" in out
    assert not out_dir.exists()


def test_out_json_contents(tmp_path, capsys):
    out_dir = tmp_path / "gate_out"
    rc = pc.main([str(BASE), str(EXP), "--out", str(out_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    result_file = out_dir / "perf_gate_result.json"
    assert result_file.is_file()
    assert "perf gate result written" in out
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["result"] == "PASS"
    assert set(payload["metrics"]) == {"cpu", "pss", "cold_p90", "jank_rate"}
    assert payload["thresholds"] == {"cpu": 10.0, "pss": 10.0,
                                     "cold_p90": 15.0, "jank_rate": 0.5}
    cpu = payload["metrics"]["cpu"]
    assert cpu["baseline"] == 15.0 and cpu["experiment"] == 15.5
    assert cpu["regression"] == 10.0 / 3.0
    assert cpu["mismatch"] is False and cpu["na"] is False
    jank = payload["metrics"]["jank_rate"]
    assert jank["baseline"] == 10.0 and jank["absolute"] is True
    assert payload["metrics"]["cold_p90"]["baseline"] == 1400.0
    assert "先基线后实验" in payload["baseline_definition"]
    assert "16.67" in payload["jank_definition_note"]


def test_missing_inputs_rc2(tmp_path, capsys):
    missing = tmp_path / "does_not_exist"
    assert pc.main([str(missing), str(EXP)]) == 2
    assert pc.main([str(BASE), str(missing)]) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pc.main([str(empty), str(EXP)]) == 2
    err = capsys.readouterr().err
    assert "ERROR" in err
