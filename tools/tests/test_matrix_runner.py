"""matrix_runner.py orchestration tests: mocked AdbClient / subprocess.

Covers the PRD scenarios:
  1. normal run - artifact layout contract per device + aggregate report
  2. single-device failure isolation (push failure) - others complete
  3. timeout hard-kill - device marked timeout, others complete
Plus validation rejection (>10, duplicate serials), aggregate report field
presence, and --dry-run zero device contact.
"""

import json
import sys
from pathlib import Path


import matrix_runner as mr
from common.adb import AdbError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "matrix"
TWO = FIXTURES / "matrix.two.json"
ELEVEN = FIXTURES / "matrix.eleven.json"


def _write_matrix(path: Path, devices, run_id="testrun") -> Path:
    path.write_text(
        json.dumps({"run_id": run_id, "devices": devices}), encoding="utf-8")
    return path


def _device(serial, apk="com.example.app", duration=1, profile=None):
    device = {"serial": serial, "apk": apk, "duration_min": duration}
    if profile is not None:
        device["profile"] = profile
    return device


class FakeAdb:
    """AdbClient double: records ops, simulates per-serial device behavior
    (unreachable / push failure) and deposits realistic artifact content."""

    behavior_by_serial: dict = {}
    instances: list = []

    def __init__(self, serial=None, timeout=30, retries=2, dry_run=False,
                 adb_path=None):
        self.serial = serial
        self.calls = []
        behavior = FakeAdb.behavior_by_serial.get(serial, {})
        self.unreachable = behavior.get("unreachable", False)
        self.fail_push = behavior.get("fail_push", False)
        FakeAdb.instances.append(self)

    def shell(self, cmd):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(("shell", cmd_str))
        if self.unreachable:
            raise AdbError("device offline")
        if cmd_str == "true":
            return 0, "", ""
        if cmd_str.startswith("getprop"):
            return 0, "arm64-v8a" + chr(10), ""
        if cmd_str.startswith("logcat"):
            return 0, "log line" + chr(10) + "ANR in com.example.app" + chr(10), ""
        return 0, "", ""

    def push(self, local, remote):
        self.calls.append(("push", local, remote))
        if self.fail_push:
            raise AdbError("push failed")
        return 0, "", ""

    def pull(self, remote, local):
        self.calls.append(("pull", remote, local))
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        if remote.endswith((".log", ".jsonl", ".snapshot")):
            target.write_text(_PULL_CONTENT.get(
                Path(remote).name, "stub"), encoding="utf-8")
        else:
            target.mkdir(exist_ok=True)
            (target / "stub.json").write_text("{}", encoding="utf-8")


_PULL_CONTENT = {
    "crash-dump.log": "1789497600000" + chr(10) + "crash:" + chr(10)
                      + "// CRASH: com.example.app (pid 1234)" + chr(10)
                      + "crash end" + chr(10),
    "audit.jsonl": (
        json.dumps({"ts": 1789497600000, "type": "rule_hit",
                    "activity": "com.example.app/.MainActivity",
                    "detail": "rule=gdpr;action=consent", "source": "device"})
        + chr(10)
        + json.dumps({"ts": 1789497601000, "type": "permission_prompt",
                      "activity": "com.example.app/.MainActivity",
                      "detail": "action=deny;permission=location", "source": "device"})
        + chr(10)),
    "fastbot_chaos.snapshot": '{"captured_at": "2026-09-16T00:00:00Z", '
                              '"states": []}' + chr(10),
}


_PERF_DATA = (
    json.dumps({"ts": 1789497600000, "serial": "s", "source": "pc",
                "cpu_percent": 4.0, "mem_pss_kb": 100000}) + chr(10)
    + json.dumps({"ts": 1789497610000, "serial": "s", "source": "pc",
                  "cpu_percent": 6.0, "mem_pss_kb": 120000}) + chr(10))

_STARTS_DATA = (
    json.dumps({"kind": "cold"}) + chr(10)
    + json.dumps({"kind": "warm"}) + chr(10)
    + json.dumps({"kind": "warm"}) + chr(10))


def _fake_run_with_timeout(argv, timeout_sec, log_path):
    """Stands in for _run_with_timeout: writes log, deposits perf artifacts
    when invoked for the perf_poller subprocess."""
    Path(log_path).write_text("fake subprocess log", encoding="utf-8")
    if any("perf_poller.py" in str(a) for a in argv):
        out_dir = Path(argv[argv.index("--out") + 1]) / "perf"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "data.json").write_text(_PERF_DATA, encoding="utf-8")
        (out_dir / "starts.json").write_text(_STARTS_DATA, encoding="utf-8")
    return 0


def _opts(tmp_path, monkeypatch, behaviors=None):
    """Patch AdbClient/launchers to fakes; build opts with tmp artifacts."""
    FakeAdb.behavior_by_serial = behaviors or {}
    FakeAdb.instances = []
    monkeypatch.setattr(mr, "AdbClient", FakeAdb)
    monkeypatch.setattr(mr, "_launch_fastbot", _fake_fastbot)
    monkeypatch.setattr(mr, "_run_with_timeout", _fake_run_with_timeout)
    for name in ("monkeyq.jar", "framework.jar", "fastbot-thirdpart.jar"):
        (tmp_path / name).write_bytes(b"jar")
    libs = tmp_path / "libs" / "arm64-v8a"
    libs.mkdir(parents=True, exist_ok=True)
    (libs / "libfastbot_native.so").write_bytes(b"so")
    return {
        "run_id": "run_x",
        "out_root": tmp_path,
        "throttle_ms": 100,
        "grace_sec": 5,
        "monkeyq": str(tmp_path / "monkeyq.jar"),
        "framework": str(tmp_path / "framework.jar"),
        "thirdpart": str(tmp_path / "fastbot-thirdpart.jar"),
        "libs_dir": str(tmp_path / "libs"),
        "max_concurrent": 4,
    }


def _fake_fastbot(adb_path, serial, command, timeout_sec, log_path):
    """Stands in for _launch_fastbot; per-serial outcome override."""
    Path(log_path).write_text("fastbot stdout" + chr(10), encoding="utf-8")
    override = FakeAdb.behavior_by_serial.get(serial, {}).get("fastbot")
    if override:
        return {"completed": 0, "crashed": 1, "timeout": None}[override]
    return 0


def _run_main(tmp_path, monkeypatch, devices, run_id="run_x", extra=(),
              behaviors=None):
    matrix_path = _write_matrix(tmp_path / "matrix.json", devices, run_id)
    _opts(tmp_path, monkeypatch, behaviors)
    rc = mr.main([
        "--matrix", str(matrix_path),
        "--out-root", str(tmp_path),
        "--monkeyq", str(tmp_path / "monkeyq.jar"),
        "--thirdpart", str(tmp_path / "fastbot-thirdpart.jar"),
        "--framework", str(tmp_path / "framework.jar"),
        "--libs-dir", str(tmp_path / "libs"),
        "--grace-sec", "5",
    ] + list(extra))
    return rc


def test_validate_matrix_rejections():
    assert mr.validate_matrix("nope") == ["matrix.json must be a JSON object"]
    assert mr.validate_matrix({}) == ["devices must be a list"]
    assert mr.validate_matrix({"devices": []}) == [
        "devices is empty; at least one device is required"]
    eleven = [{"serial": "d%d" % i, "apk": "com.a", "duration_min": 1}
              for i in range(11)]
    errors = mr.validate_matrix({"devices": eleven})
    assert len(errors) == 1 and "exceed the maximum of 10" in errors[0]
    errors = mr.validate_matrix({"devices": [
        {"serial": "a", "apk": "com.a", "duration_min": 0},
        {"serial": "", "apk": "com.a"},
        {"apk": "com.a", "duration_min": 1},
        {"serial": "b", "duration_min": 1},
        {"serial": "c", "apk": "com.a", "duration_min": 1,
         "profile": {"chaos": "yes"}},
        {"serial": "d", "apk": "com.a", "duration_min": 1,
         "profile": {"gpu": True}},
        {"serial": "x", "apk": "com.a", "duration_min": 1},
        {"serial": "x", "apk": "com.b", "duration_min": 1},
    ]})
    text = chr(10).join(errors)
    assert "duration_min must be a positive number" in text
    assert "devices[1].serial" in text
    assert "devices[2].serial" in text
    assert "devices[3].apk" in text
    assert "profile.chaos must be a boolean" in text
    assert "unknown key 'gpu'" in text
    assert "duplicate device serial: x" in text
    assert len(errors) == 7



def test_valid_matrix_passes():
    errors = mr.validate_matrix({"run_id": "r", "devices": [
        {"serial": "a", "apk": "com.a"},
        {"serial": "b", "apk": "com.b", "duration_min": 2.5,
         "profile": {"chaos": True, "perf": True, "privacy": True}},
    ]})
    assert errors == []


def test_validate_matrix_missing_run_id_ok_but_blank_rejected():
    assert mr.validate_matrix({"devices": [
        {"serial": "a", "apk": "com.a"}]}) == []
    assert mr.validate_matrix({"run_id": "  ", "devices": [
        {"serial": "a", "apk": "com.a"}]})


def test_scenario_normal_two_devices(tmp_path, monkeypatch):
    devices = [
        _device("devA", profile={"chaos": True, "perf": True, "privacy": True}),
        _device("devB", profile={"perf": True}),
    ]
    rc = _run_main(tmp_path, monkeypatch, devices)
    assert rc == 0
    base = tmp_path / "run_x" / "matrix"
    for serial, profile in (("devA", {"chaos": True, "perf": True, "privacy": True}),
                            ("devB", {"perf": True})):
        d = base / serial
        assert (d / "crash-dump.log").is_file(), serial
        assert (d / "fastbot_perf").is_dir(), serial
        assert (d / "fastbot_coverage").is_dir(), serial
        assert (d / "fastbot_privacy" / "audit.jsonl").is_file(), serial
        assert (d / "fastbot_chaos.snapshot").is_file(), serial
        assert (d / "max.config").is_file(), serial
        assert (d / "run.log").is_file(), serial
        assert (d / "logcat.txt").is_file(), serial
        assert (d / "device_run.json").is_file(), serial
        record = json.loads((d / "device_run.json").read_text(encoding="utf-8"))
        assert record["result"] == "completed"
        assert record["exit_code"] == 0
        assert record["missing"] == []
    # max.config content follows profile flags
    deva_config = (base / "devA" / "max.config").read_text(encoding="utf-8")
    for key in ("max.chaos.enable=true", "max.perf.frame=true",
                "max.privacy.enabled=true"):
        assert key in deva_config
    devb_config = (base / "devB" / "max.config").read_text(encoding="utf-8")
    assert "max.perf.frame=true" in devb_config
    assert "max.chaos" not in devb_config
    assert "max.privacy" not in devb_config
    # layout: perf dir only exists when profile.perf is on (both on here)
    assert (base / "devA" / "perf" / "data.json").is_file()
    assert (base / "devB" / "perf" / "data.json").is_file()
    # the aggregate report exists at the contract path
    assert (base / "report.html").is_file()
    # pull calls targeted the contract remote paths
    deva = [inst for inst in FakeAdb.instances if inst.serial == "devA"][0]
    pulled = [c[1] for c in deva.calls if c[0] == "pull"]
    for remote in ("/sdcard/crash-dump.log", "/sdcard/fastbot_perf",
                   "/sdcard/fastbot_coverage",
                   "/sdcard/fastbot_privacy/audit.jsonl",
                   "/sdcard/fastbot_chaos.snapshot"):
        assert remote in pulled
    pushed = [c[2] for c in deva.calls if c[0] == "push"]
    for remote in ("/sdcard/monkeyq.jar", "/sdcard/framework.jar",
                   "/sdcard/fastbot-thirdpart.jar", "/sdcard/max.config",
                   "/data/local/tmp/libfastbot_native.so"):
        assert remote in pushed


def test_scenario_single_device_failure_isolated(tmp_path, monkeypatch):
    devices = [_device("devA"), _device("devB"), _device("devC")]
    rc = _run_main(tmp_path, monkeypatch, devices,
                   behaviors={"devB": {"fail_push": True}})
    assert rc == 1
    base = tmp_path / "run_x" / "matrix" / "devB"
    record = json.loads((base / "device_run.json").read_text(encoding="utf-8"))
    assert record["result"] == "failed"
    assert "push failed" in record["error"]
    for serial in ("devA", "devC"):
        record = json.loads(
            (tmp_path / "run_x" / "matrix" / serial / "device_run.json")
            .read_text(encoding="utf-8"))
        assert record["result"] == "completed"
        assert record["exit_code"] == 0
    html = (tmp_path / "run_x" / "matrix" / "report.html").read_text(encoding="utf-8")
    assert "失败" in html
    assert "完成" in html
    # failed device still gets a report row
    assert "devB" in html


def test_scenario_timeout_hard_kill(tmp_path, monkeypatch):
    devices = [_device("fast1"), _device("slowdev")]
    rc = _run_main(tmp_path, monkeypatch, devices,
                   behaviors={"slowdev": {"fastbot": "timeout"}})
    assert rc == 1
    slow_record = json.loads(
        (tmp_path / "run_x" / "matrix" / "slowdev" / "device_run.json")
        .read_text(encoding="utf-8"))
    assert slow_record["result"] == "timeout"
    assert slow_record["exit_code"] is None
    fast_record = json.loads(
        (tmp_path / "run_x" / "matrix" / "fast1" / "device_run.json")
        .read_text(encoding="utf-8"))
    assert fast_record["result"] == "completed"
    html = (tmp_path / "run_x" / "matrix" / "report.html").read_text(encoding="utf-8")
    assert "超时" in html
    assert "duration+grace 超时强杀" in html


def test_reject_over_ten_devices_zero_contact(tmp_path, monkeypatch, capsys):
    class _Explode:
        def __init__(self, *a, **k):
            raise AssertionError("device contact attempted")
    monkeypatch.setattr(mr, "AdbClient", _Explode)
    rc = mr.main(["--matrix", str(ELEVEN), "--dry-run"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "exceed the maximum of 10" in err
    assert "未接触任何设备" in err


def test_duplicate_serial_rejected_via_main(tmp_path, capsys):
    matrix_path = _write_matrix(tmp_path / "dup.json", [
        _device("same"), _device("same"),
    ])
    rc = mr.main(["--matrix", str(matrix_path), "--dry-run"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "duplicate device serial: same" in err


def test_dry_run_zero_device_contact(tmp_path, monkeypatch, capsys):
    class _Explode:
        def __init__(self, *a, **k):
            raise AssertionError("device contact attempted")
    monkeypatch.setattr(mr, "AdbClient", _Explode)
    monkeypatch.setattr(mr, "_launch_fastbot",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("run attempted")))
    rc = mr.main(["--matrix", str(TWO), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    assert "无设备接触" in out
    assert "校验: OK" in out
    for token in ("emulator-5554", "emulator-5556", "CLASSPATH=",
                  "max.chaos.enable=true", "max.perf.frame=true",
                  "max.privacy.enabled=true", "/sdcard/max.config",
                  "/data/local/tmp/", "/sdcard/crash-dump.log",
                  "perf_poller"):
        assert token in out, token
    # perf-only device plan must not include chaos/privacy config keys
    assert out.count("max.chaos.enable=true") == 1
    assert out.count("max.privacy.enabled=true") == 1


def test_report_fields_presence(tmp_path, monkeypatch):
    devices = [
        _device("devA", profile={"chaos": True, "perf": True, "privacy": True}),
        _device("devB", profile={"perf": True}),
    ]
    _run_main(tmp_path, monkeypatch, devices)
    html = (tmp_path / "run_x" / "matrix" / "report.html").read_text(encoding="utf-8")
    for header in mr.TABLE_HEADERS:
        assert header in html, header
    # per-row values from the FakeAdb/fake-poller artifacts
    for value in ("CPU均值 5.0%", "内存均值 110000KB", "冷启动 1 / 温启动 2",
                  "2", "1", "—" ):
        assert value in html, value


def test_preflight_unreachable_isolated(tmp_path, monkeypatch):
    devices = [_device("dead1"), _device("alive1")]
    rc = _run_main(tmp_path, monkeypatch, devices,
                   behaviors={"dead1": {"unreachable": True}})
    assert rc == 1
    dead = json.loads(
        (tmp_path / "run_x" / "matrix" / "dead1" / "device_run.json")
        .read_text(encoding="utf-8"))
    assert dead["result"] == "failed"
    assert dead["error"].startswith("preflight")
    assert "采集跳过: 设备不可达" in dead["notes"]
    dead_calls = [inst for inst in FakeAdb.instances if inst.serial == "dead1"][0].calls
    assert not [c for c in dead_calls if c[0] in ("push", "pull")], dead_calls
    alive = json.loads(
        (tmp_path / "run_x" / "matrix" / "alive1" / "device_run.json")
        .read_text(encoding="utf-8"))
    assert alive["result"] == "completed"


def test_run_with_timeout_kills_real_subprocess(tmp_path):
    log = tmp_path / "t.log"
    rc = mr._run_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(30)"], 1.5, log)
    assert rc is None
    assert log.is_file()


def test_run_with_timeout_returns_rc(tmp_path):
    log = tmp_path / "t.log"
    rc = mr._run_with_timeout([sys.executable, "-c", "print(123)"], 30, log)
    assert rc == 0
    rc = mr._run_with_timeout([sys.executable, "-c", "raise SystemExit(3)"], 30, log)
    assert rc == 3


def test_outcome_from_rc():
    assert mr.outcome_from_rc(None) == "timeout"
    assert mr.outcome_from_rc(0) == "completed"
    assert mr.outcome_from_rc(1) == "crashed"


def test_build_max_config_off_by_default():
    config = mr.build_max_config({})
    assert "max.chaos" not in config
    assert "max.perf" not in config
    assert "max.privacy" not in config
    assert config.startswith("#")
    assert mr.build_max_config({"chaos": True}) == (
        "# generated by tools/matrix_runner.py (profile-driven)" + chr(10)
        + "max.chaos.enable=true" + chr(10))
    assert mr.build_max_config(
        {"chaos": True, "perf": True, "privacy": True}).count("=true") == 3


def test_build_fastbot_command():
    command = mr.build_fastbot_command("com.example.app", 1, 100)
    assert command.startswith(
        "CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:"
        "/sdcard/fastbot-thirdpart.jar exec app_process /system/bin "
        "com.android.commands.monkey.Monkey")
    assert "-p com.example.app" in command
    assert "--agent reuseq" in command
    assert "--running-minutes 1" in command
    assert "--throttle 100" in command
    assert command.endswith("-v -v")


def test_artifact_parsers():
    assert mr.count_crashes("missing-dir/nope.log") == 0
    assert mr.count_anrs("missing-dir/nope.txt") == 0
    assert mr.coverage_activity_count("missing-dir") is None
    privacy, malformed = mr.privacy_event_count("missing-dir/audit.jsonl")
    assert privacy is None and malformed == 0
    assert mr.perf_summary("missing-dir/perf") is None


def test_perf_summary_values_and_degradation(tmp_path):
    perf_dir = tmp_path / "perf"
    perf_dir.mkdir()
    (perf_dir / "data.json").write_text(_PERF_DATA, encoding="utf-8")
    (perf_dir / "starts.json").write_text(_STARTS_DATA, encoding="utf-8")
    assert mr.perf_summary(perf_dir) == "CPU均值 5.0%, 内存均值 110000KB, 冷启动 1 / 温启动 2"
    (perf_dir / "data.json").write_text(
        "garbage" + chr(10) + "[1]" + chr(10), encoding="utf-8")
    value = mr.perf_summary(perf_dir)
    assert value is not None
    assert "CPU均值" not in value
    assert "冷启动 1 / 温启动 2" in value
    (perf_dir / "data.json").write_text("", encoding="utf-8")
    (perf_dir / "starts.json").unlink()
    assert mr.perf_summary(perf_dir) == "-"


def test_summarize_missing_artifacts(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    summary = mr.summarize_device(empty)
    assert summary["crash"] == 0
    assert summary["anr"] == 0
    assert summary["coverage"] is None
    assert summary["perf"] is None
    assert summary["privacy"] is None
    html = mr.build_matrix_report(
        [{"serial": "s", "result": "failed", "error": "boom",
          "device_out": str(empty)}],
        run_id="r", max_concurrent=5)
    assert "—" in html
    assert "boom" in html
    assert "crash-dump.log 未采集" in html

def test_profile_config_passthrough_merged(tmp_path, monkeypatch):
    devices = [_device("devA", profile={
        "perf": True,
        "config": {"max.chaos.battery.pct": "30",
                   "max.privacy.rules": "/sdcard/max.privacy.rules"}})]
    rc = _run_main(tmp_path, monkeypatch, devices)
    assert rc == 0
    config = (tmp_path / "run_x" / "matrix" / "devA" / "max.config")        .read_text(encoding="utf-8")
    assert "max.perf.frame=true" in config
    assert "max.chaos.battery.pct=30" in config
    assert "max.privacy.rules=/sdcard/max.privacy.rules" in config
    # non max.* keys never reach the generated config (validated earlier)
    html = (tmp_path / "run_x" / "matrix" / "report.html")        .read_text(encoding="utf-8")
    assert "矩阵运行接管 /sdcard/max.config" in html


def test_profile_config_dry_run_shows_merge_and_note(tmp_path, monkeypatch, capsys):
    devices = [_device("devA", profile={
        "chaos": True,
        "config": {"max.chaos.battery.pct": "30"}})]
    matrix_path = _write_matrix(tmp_path / "m.json", devices)
    _opts(tmp_path, monkeypatch)
    rc = mr.main([
        "--matrix", str(matrix_path),
        "--out-root", str(tmp_path),
        "--monkeyq", str(tmp_path / "monkeyq.jar"),
        "--thirdpart", str(tmp_path / "fastbot-thirdpart.jar"),
        "--framework", str(tmp_path / "framework.jar"),
        "--libs-dir", str(tmp_path / "libs"),
        "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "max.chaos.battery.pct=30" in out
    assert "矩阵运行接管 /sdcard/max.config（生成式覆盖）" in out


def test_profile_config_invalid_entries_rejected():
    errors = mr.validate_matrix({"devices": [
        {"serial": "a", "apk": "com.a", "profile": {"config": "nope"}},
        {"serial": "b", "apk": "com.a", "profile": {"config": {"foo.bar": "x"}}},
        {"serial": "c", "apk": "com.a", "profile": {"config": {"max.k": 30}}},
    ]})
    text = chr(10).join(errors)
    assert "devices[0].profile.config must be a JSON object" in text
    assert "invalid key 'foo.bar'" in text
    assert "devices[2].profile.config.max.k must be a string, got 30" in text
    assert len(errors) == 3


def test_chaos_privacy_without_config_keys_warn(tmp_path, monkeypatch, capsys):
    warnings = []
    errors = mr.validate_matrix({"devices": [
        {"serial": "a", "apk": "com.a",
         "profile": {"chaos": True, "privacy": True}}]}, warnings)
    assert errors == [] and len(warnings) == 2
    assert warnings[0].endswith(
        "chaos=true 但未提供任何 max.chaos.<state>.pct — 注入将不会发生")
    assert warnings[1].endswith(
        "privacy=true 但未提供 max.privacy.rules — 规则引擎将不生效")
    # armed with the required keys -> silent
    warnings = []
    mr.validate_matrix({"devices": [
        {"serial": "a", "apk": "com.a", "profile": {
            "chaos": True, "privacy": True,
            "config": {"max.chaos.battery.pct": "30",
                       "max.privacy.rules": "/sdcard/r"}}}]}, warnings)
    assert warnings == []
    # warnings surface as WARNING lines via main (dry-run, zero contact)
    matrix_path = _write_matrix(tmp_path / "warn.json", [
        _device("devA", profile={"chaos": True})])
    class _Explode:
        def __init__(self, *a, **k):
            raise AssertionError("device contact attempted")
    monkeypatch.setattr(mr, "AdbClient", _Explode)
    rc = mr.main(["--matrix", str(matrix_path), "--dry-run"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "WARNING: devices[0]: chaos=true 但未提供任何" in err


def test_timeout_reset_battery_and_notes(tmp_path, monkeypatch):
    devices = [_device("fast1"), _device("slowdev")]
    rc = _run_main(tmp_path, monkeypatch, devices,
                   behaviors={"slowdev": {"fastbot": "timeout"}})
    assert rc == 1
    record = json.loads(
        (tmp_path / "run_x" / "matrix" / "slowdev" / "device_run.json")
        .read_text(encoding="utf-8"))
    assert record["result"] == "timeout"
    assert any("超时强杀可能使设备侧 chaos 状态残留" in n for n in record["notes"])
    slow = [inst for inst in FakeAdb.instances if inst.serial == "slowdev"][0]
    assert ("shell", "dumpsys battery reset") in slow.calls
