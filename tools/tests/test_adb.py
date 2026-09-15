"""AdbClient tests using dry-run mode and monkeypatched subprocess (no device)."""

import subprocess
import time

import pytest

from common.adb import AdbClient, AdbError


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_run(monkeypatch, outcomes):
    calls = []
    queue = list(outcomes)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        outcome = queue.pop(0) if queue else outcomes[-1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _install_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)
    return delays


# ---------------------------------------------------------------------- #
# dry-run mode
# ---------------------------------------------------------------------- #


def test_dry_run_shell_prints_plan_and_returns_zero(capsys):
    client = AdbClient(serial="emu-1", dry_run=True)
    rc, out, err = client.shell("echo hi")
    assert (rc, out, err) == (0, "", "")
    plan = capsys.readouterr().out
    assert plan.startswith("[DRY-RUN] ")
    assert "adb" in plan
    assert "shell" in plan
    assert "echo hi" in plan


def test_dry_run_push_pull_devices(capsys):
    client = AdbClient(serial="emu-1", dry_run=True)
    assert client.push("local.apk", "/data/local/tmp/app.apk") == (0, "", "")
    assert client.pull("/sdcard/f.txt", "f.txt") == (0, "", "")
    assert client.devices() == []
    plan = capsys.readouterr().out
    assert plan.count("[DRY-RUN]") == 3


# ---------------------------------------------------------------------- #
# real mode (subprocess monkeypatched)
# ---------------------------------------------------------------------- #


def test_shell_real_mode_returns_output(monkeypatch):
    client = AdbClient(serial="emu-1")
    calls = _install_run(monkeypatch, [_FakeCompleted(0, "hi\n", "")])
    rc, out, err = client.shell("echo hi")
    assert (rc, out, err) == (0, "hi\n", "")
    assert calls[0] == [client.adb_path, "-s", "emu-1", "shell", "echo hi"]


def test_retry_then_succeed(monkeypatch):
    _install_sleep(monkeypatch)
    client = AdbClient(serial="emu-1", retries=2)
    calls = _install_run(
        monkeypatch,
        [_FakeCompleted(1, "", "transient"), _FakeCompleted(0, "ok", "")],
    )
    rc, out, err = client.shell("echo hi")
    assert (rc, out) == (0, "ok")
    assert len(calls) == 2


def test_adb_error_after_retries_on_nonzero_exit(monkeypatch):
    delays = _install_sleep(monkeypatch)
    client = AdbClient(serial="emu-1", retries=2)
    calls = _install_run(monkeypatch, [_FakeCompleted(1, "", "device offline")])
    with pytest.raises(AdbError):
        client.shell("echo hi")
    assert len(calls) == 3      # retries=2 -> 3 attempts total
    assert delays == [1, 2]     # linear backoff, 1s step


def test_adb_error_after_retries_on_timeout(monkeypatch):
    _install_sleep(monkeypatch)
    client = AdbClient(serial="emu-1", retries=1)
    calls = _install_run(
        monkeypatch, [subprocess.TimeoutExpired(cmd="adb shell", timeout=1)]
    )
    with pytest.raises(AdbError):
        client.shell("echo hi")
    assert len(calls) == 2


def test_auto_detect_single_device(monkeypatch):
    client = AdbClient()  # serial=None -> auto-detect
    listing = "List of devices attached\nemu-5554\tdevice\n\n"
    calls = _install_run(
        monkeypatch,
        [_FakeCompleted(0, listing, ""), _FakeCompleted(0, "hello\n", "")],
    )
    rc, out, err = client.shell("echo hi")
    assert out == "hello\n"
    assert calls[0][-1] == "devices"
    assert calls[1][1:3] == ["-s", "emu-5554"]


def test_auto_detect_no_device_raises(monkeypatch):
    client = AdbClient()
    listing = "List of devices attached\n"
    _install_run(monkeypatch, [_FakeCompleted(0, listing, "")])
    with pytest.raises(AdbError):
        client.shell("echo hi")


def test_auto_detect_multiple_devices_raises(monkeypatch):
    client = AdbClient()
    listing = "List of devices attached\nemu-5554\tdevice\nemu-5556\tdevice\n"
    _install_run(monkeypatch, [_FakeCompleted(0, listing, "")])
    with pytest.raises(AdbError):
        client.shell("echo hi")


def test_devices_filters_non_device_states(monkeypatch):
    client = AdbClient()
    listing = (
        "List of devices attached\n"
        "emu-5554\tdevice\nemu-5556\tunauthorized\nemu-5558\toffline\n"
    )
    _install_run(monkeypatch, [_FakeCompleted(0, listing, "")])
    assert client.devices() == ["emu-5554"]


def test_shell_accepts_command_list(monkeypatch):
    client = AdbClient(serial="emu-1")
    calls = _install_run(monkeypatch, [_FakeCompleted(0, "", "")])
    client.shell(["ls", "/sdcard"])
    assert calls[0][-3:] == ["shell", "ls", "/sdcard"]


def test_serial_cached_after_auto_detect(monkeypatch):
    listing = "List of devices attached\nemu-5554\tdevice\n"
    calls = _install_run(
        monkeypatch,
        [
            _FakeCompleted(0, listing, ""),
            _FakeCompleted(0, "a\n", ""),
            _FakeCompleted(0, "b\n", ""),
        ],
    )
    client = AdbClient()
    client.shell("echo a")
    client.shell("echo b")
    assert len(calls) == 3  # devices + 2 shell calls, no repeated detection
