"""fastbot_run.py tests: FakeAdb + launch-recorder pattern (mirrors
test_matrix_runner.py). Pins the H1 ordering contract: the unconditional
`rm -f /sdcard/crash-dump.log` must precede the launch command."""

import sys
from pathlib import Path

import pytest

import fastbot_run as fr
from common.adb import AdbError

PKG = "com.example.app"


class FakeAdb:
    """AdbClient double recording every op into self.calls as tuples:
    ("shell", cmd) / ("push", local, remote) / ("pull", remote, local)."""

    def __init__(self, serial=None, timeout=30, retries=2, dry_run=False,
                 adb_path=None):
        self.serial = serial or "fake-serial"
        self.timeout = timeout
        self.retries = retries
        self.adb_path = "adb"
        self.calls = []
        self.fail_pull_remote = None

    def shell(self, cmd):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(("shell", cmd_str))
        if cmd_str.startswith("logcat"):
            return 0, "log line" + chr(10), ""
        return 0, "", ""

    def push(self, local, remote):
        self.calls.append(("push", local, remote))
        return 0, "", ""

    def pull(self, remote, local):
        self.calls.append(("pull", remote, local))
        if self.fail_pull_remote and remote == self.fail_pull_remote:
            raise AdbError("pull failed: " + remote)
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        if remote.endswith((".log", ".jsonl", ".snapshot")):
            target.write_text("canned " + Path(remote).name,
                              encoding="utf-8")
        elif remote.endswith(".json"):
            target.write_text("{}", encoding="utf-8")
        else:
            target.mkdir(exist_ok=True)


def _setup(monkeypatch, launch_rc=0, fail_pull_remote=None):
    fake = FakeAdb()
    fake.fail_pull_remote = fail_pull_remote
    monkeypatch.setattr(fr, "AdbClient", lambda **kw: fake)
    def fake_launch(argv, timeout_sec, log_path):
        fake.calls.append(("launch", " ".join(argv)))
        Path(log_path).write_text("fastbot stdout" + chr(10),
                                  encoding="utf-8")
        return launch_rc
    monkeypatch.setattr(fr, "_run_with_timeout", fake_launch)
    return fake


def _run_main(tmp_path, extra=()):
    return fr.main([
        "--package", PKG,
        "--minutes", "1",
        "--out", str(tmp_path),
    ] + list(extra))


def test_build_run_command_exact_argv():
    assert fr.build_run_command("com.example.app", 30, 100) == [
        "CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:"
        "/sdcard/fastbot-thirdpart.jar",
        "exec", "app_process", "/system/bin",
        "com.android.commands.monkey.Monkey",
        "-p", "com.example.app", "--agent", "reuseq",
        "--running-minutes", "30", "--throttle", "100", "-v", "-v",
    ]


def test_h1_rm_precedes_launch(tmp_path, monkeypatch):
    fake = _setup(monkeypatch)
    rc = _run_main(tmp_path)
    assert rc == 0
    seq = fake.calls
    assert seq[0] == ("shell", "rm -f /sdcard/crash-dump.log")
    launch_indices = [i for i, c in enumerate(seq) if c[0] == "launch"]
    assert len(launch_indices) == 1
    assert 0 < launch_indices[0]
    launch = seq[launch_indices[0]][1]
    assert ("CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:"
            "/sdcard/fastbot-thirdpart.jar exec app_process /system/bin "
            "com.android.commands.monkey.Monkey") in launch
    assert "-p com.example.app" in launch
    assert "--agent reuseq" in launch
    assert "--running-minutes 1" in launch
    assert "--throttle 100" in launch
    assert launch.endswith("-v -v")
    # step 5 collection still lands on the fake (no collect-all)
    pulled = [c[1] for c in seq if c[0] == "pull"]
    assert pulled == ["/sdcard/crash-dump.log",
                      "/sdcard/fastbot_coverage/com.example.app.json"]
    assert (tmp_path / "crash-dump.log").is_file()
    assert (tmp_path / "fastbot_coverage" / (PKG + ".json")).is_file()
    assert (tmp_path / "logcat.txt").is_file()


def test_optional_steps_between_rm_and_launch(tmp_path, monkeypatch):
    fake = _setup(monkeypatch)
    config = tmp_path / "my.config"
    config.write_text("max.perf.frame=true", encoding="utf-8")
    rc = _run_main(tmp_path, ["--max-config", str(config),
                              "--clean-actions"])
    assert rc == 0
    seq = fake.calls
    assert [c[0] for c in seq] == [
        "shell", "push", "shell", "launch", "pull", "pull", "shell"]
    assert seq[0] == ("shell", "rm -f /sdcard/crash-dump.log")
    assert seq[1] == ("push", str(config), "/sdcard/max.config")
    assert seq[2] == ("shell", "rm -f /sdcard/max.xpath.actions")
    assert seq[3][0] == "launch"


def test_completed_collects_all_artifacts(tmp_path, monkeypatch):
    _setup(monkeypatch)
    rc = _run_main(tmp_path, ["--collect-all"])
    assert rc == 0
    assert (tmp_path / "crash-dump.log").is_file()
    assert (tmp_path / "fastbot_coverage" / (PKG + ".json")).is_file()
    assert (tmp_path / "fastbot_perf").is_dir()
    assert (tmp_path / "fastbot_privacy" / "audit.jsonl").is_file()
    assert (tmp_path / "fastbot_chaos.snapshot").is_file()
    assert (tmp_path / "logcat.txt").is_file()
    assert (tmp_path / "fastbot.log").is_file()


def test_failed_rc_maps_to_rc1(tmp_path, monkeypatch):
    _setup(monkeypatch, launch_rc=5)
    rc = _run_main(tmp_path)
    assert rc == 1
    # collection still runs on failure (crash dump matters most then)
    assert (tmp_path / "crash-dump.log").is_file()


def test_timeout_maps_to_rc1(tmp_path, monkeypatch):
    _setup(monkeypatch, launch_rc=None)
    rc = _run_main(tmp_path)
    assert rc == 1
    assert (tmp_path / "fastbot.log").is_file()
    assert (tmp_path / "crash-dump.log").is_file()


def test_pull_failure_is_best_effort(tmp_path, monkeypatch):
    _setup(monkeypatch, launch_rc=0, fail_pull_remote=(
        "/sdcard/fastbot_coverage/%s.json" % PKG))
    rc = _run_main(tmp_path)
    assert rc == 0
    assert not (tmp_path / "fastbot_coverage" / (PKG + ".json")).exists()
    assert (tmp_path / "crash-dump.log").is_file()
    assert (tmp_path / "logcat.txt").is_file()


def test_run_with_timeout_kills_real_subprocess(tmp_path):
    log = tmp_path / "t.log"
    rc = fr._run_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(30)"], 1.0, log)
    assert rc is None
    assert log.is_file()


def test_run_with_timeout_rc_passthrough(tmp_path):
    log = tmp_path / "t.log"
    assert fr._run_with_timeout([sys.executable, "-c", "print(1)"], 30,
                                log) == 0
    assert fr._run_with_timeout(
        [sys.executable, "-c", "raise SystemExit(3)"], 30, log) == 3


def test_dry_run_zero_contact(tmp_path, monkeypatch, capsys):
    class _Explode:
        def __init__(self, *a, **k):
            raise AssertionError("device contact attempted")

    def _explode_run(*a, **k):
        raise AssertionError("run attempted")
    monkeypatch.setattr(fr, "AdbClient", _Explode)
    monkeypatch.setattr(fr, "_run_with_timeout", _explode_run)
    rc = fr.main([
        "--package", PKG,
        "--out", str(tmp_path),
        "--dry-run",
        "--max-config", str(tmp_path / "missing.config"),
        "--clean-actions",
        "--collect-all",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    for token in ("rm -f /sdcard/crash-dump.log",
                  "adb push", str(tmp_path / "missing.config"),
                  "rm -f /sdcard/max.xpath.actions",
                  "CLASSPATH=", "com.android.commands.monkey.Monkey",
                  "--agent reuseq", "--running-minutes 30",
                  "logcat -d",
                  "/sdcard/fastbot_coverage/com.example.app.json",
                  "/sdcard/fastbot_perf",
                  "/sdcard/fastbot_privacy/audit.jsonl",
                  "/sdcard/fastbot_chaos.snapshot"):
        assert token in out, token


def test_missing_package_is_usage_rc2():
    with pytest.raises(SystemExit) as exc:
        fr.main([])
    assert exc.value.code == 2


def test_invalid_minutes_rc2(capsys):
    assert fr.main(["--package", PKG, "--minutes", "0"]) == 2
    assert fr.main(["--package", PKG, "--minutes", "-3"]) == 2


def test_missing_max_config_rc2_zero_contact(tmp_path, monkeypatch):
    fake = _setup(monkeypatch)
    rc = fr.main(["--package", PKG, "--out", str(tmp_path),
                  "--max-config", str(tmp_path / "nope.config")])
    assert rc == 2
    assert fake.calls == []


def test_invalid_package_rc2(capsys):
    for bad in ("com.evil;reboot", "../escape", "not a package"):
        rc = fr.main(["--package", bad, "--dry-run"])
        captured = capsys.readouterr()
        assert rc == 2, bad
        assert "--package" in captured.err


def test_valid_package_dry_run_still_rc0():
    assert fr.main(["--package", "com.example.app", "--dry-run"]) == 0
