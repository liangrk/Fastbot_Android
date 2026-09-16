"""frida hook collector PC-side logic tests (frida CLI + device fully mocked).

hook.js execution itself is NOT tested here (needs a rooted device; verified
on-device by the user team per the plan ADR follow-up). Covered: apis.json
well-formedness, hook.js manifest mirror, collect.py line filtering/parsing,
record normalization vs audit.schema.json, command building, dry-run, and the
mocked-run JSONL pipeline.
"""

import json
import subprocess
import sys
import types
from pathlib import Path

from jsonschema import validate

HOOK_DIR = Path(__file__).resolve().parents[1] / "privacy_hook"
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

import collect  # tools/privacy_hook/collect.py

TOOLS_DIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (TOOLS_DIR / "schemas" / "audit.schema.json").read_text(encoding="utf-8"))
MANIFEST_PATH = HOOK_DIR / "apis.json"
HOOK_JS_PATH = HOOK_DIR / "hook.js"

FIXTURE_STDOUT = "\n".join([
    "Frida banner noise line",
    "[fastbot-hook] manifest: 15 apis from embedded default (monitor all)",
    '@@AUDIT@@{"ts": 1789500000000, "type": "sensitive_api", "activity": '
    '"com.example.app/.MainActivity", "detail": '
    '"api=android.telephony.TelephonyManager.getDeviceId; permission=READ_PHONE_STATE; '
    'package=com.example.app; args=", "source": "frida"}',
    '@@AUDIT@@{"ts": 1789500001000, "type": "sensitive_api", "activity": "(unknown)", '
    '"detail": "api=android.content.ClipboardManager.getPrimaryClip; '
    'permission=READ_CLIPBOARD; package=com.example.app; args=", "source": "frida"}',
    '@@AUDIT@@{not json}',
    "",
])


def test_apis_json_wellformed():
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = payload["apis"]
    assert len(entries) >= 10
    seen = set()
    for entry in entries:
        for key in ("class", "method", "permission", "risk"):
            assert isinstance(entry.get(key), str) and entry[key].strip()
        assert entry["risk"] in ("high", "medium")
        seen.add((entry["class"], entry["method"], entry.get("uri", "")))
    assert len(seen) == len(entries)


def test_hook_js_mirrors_manifest_and_prefix():
    hook_text = HOOK_JS_PATH.read_text(encoding="utf-8")
    assert collect.AUDIT_PREFIX in hook_text  # @@AUDIT@@ kept in sync
    assert "FALLBACK_APIS" in hook_text
    assert "MANIFEST_DEVICE_FILE" in hook_text  # documented device-file constant
    assert "sensitive_api" in hook_text
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in payload["apis"]:
        assert '"%s"' % entry["class"] in hook_text
        assert '"%s"' % entry["method"] in hook_text
        assert '"%s"' % entry["permission"] in hook_text


def test_filter_audit_lines_mixed():
    records, malformed = collect.filter_audit_lines(FIXTURE_STDOUT)
    assert len(records) == 2
    assert len(malformed) == 1
    assert malformed[0][0] == 5  # the malformed @@AUDIT@@ line number
    assert records[0]["type"] == "sensitive_api"
    assert records[0]["source"] == "frida"
    assert "api=android.telephony.TelephonyManager.getDeviceId" in records[0]["detail"]


def test_filter_audit_lines_prefix_only_ignores_noise():
    records, malformed = collect.filter_audit_lines(
        "noise\n" + collect.AUDIT_PREFIX + " []\nmore noise")
    assert len(records) == 1  # parsed, rejected later by normalize
    assert malformed == []


def test_normalize_record_fills_nulls_schema():
    raw = {
        "ts": 1789500000000, "type": "sensitive_api",
        "activity": "com.example.app/.MainActivity",
        "detail": "api=...; args=x", "source": "frida",
    }
    record, reason = collect.normalize_record(raw)
    assert reason is None
    assert list(record.keys()) == [
        "ts", "type", "activity", "widget", "detail", "screenshot", "source"]
    assert record["widget"] is None and record["screenshot"] is None
    validate(instance=record, schema=SCHEMA)


def test_normalize_record_rejections():
    good = {
        "ts": 1, "type": "sensitive_api", "activity": "a", "detail": "d",
        "source": "frida",
    }
    assert collect.normalize_record("nope")[0] is None
    assert collect.normalize_record({})[1] == "ts must be an integer"
    bool_ts = dict(good); bool_ts["ts"] = True
    assert collect.normalize_record(bool_ts)[1] == "ts must be an integer"
    bad_type = dict(good); bad_type["type"] = "api_hit"
    assert "type must be one of" in collect.normalize_record(bad_type)[1]
    bad_src = dict(good); bad_src["source"] = "pc"
    assert "source must be one of" in collect.normalize_record(bad_src)[1]
    bad_act = dict(good); bad_act["activity"] = 5
    assert collect.normalize_record(bad_act)[1] == "activity must be a string"


def test_build_command_variants():
    spawn = collect.build_command("/f/frida", "/t/w.js", None, "com.example", False)
    assert spawn == ["/f/frida", "-U", "-f", "com.example", "-l", "/t/w.js", "-q"]
    with_serial = collect.build_command("/f/frida", "/t/w.js", "SER", "com.example", False)
    assert with_serial[:4] == ["/f/frida", "-D", "SER", "-f"]
    attach = collect.build_command("/f/frida", "/t/w.js", None, "com.example", True)
    assert attach[2:4] == ["-n", "com.example"]
    attach_pid = collect.build_command("/f/frida", "/t/w.js", "SER", "com.example", True, 4242)
    assert attach_pid[1:5] == ["-D", "SER", "-p", "4242"]


def test_resolve_pid_parses_frida_ps_output():
    output = (
        " PID  Name                       Identifier\n"
        "-----  -------------------------  ----------------------\n"
        " 31504  Settings                   com.android.settings\n"
        " 22752  ele.me                     me.ele\n"
        "  1234 自助机                       selfserve\n"
    )
    def fake_run(argv, **kwargs):
        class P:
            stdout = output
            returncode = 0
        return P()
    monkeypatch = __import__("unittest").mock
    orig = collect.subprocess.run
    collect.subprocess.run = fake_run
    try:
        assert collect.resolve_pid("/f/frida", "SER", "com.android.settings") == 31504
        assert collect.resolve_pid("/f/frida", "SER", "me.ele") == 22752
        assert collect.resolve_pid("/f/frida", None, "selfserve") == 1234
        assert collect.resolve_pid("/f/frida", "SER", "not.running") is None
    finally:
        collect.subprocess.run = orig


def test_resolve_pid_survives_ps_failure():
    def boom(argv, **kwargs):
        raise OSError("no frida-ps")
    orig = collect.subprocess.run
    collect.subprocess.run = boom
    try:
        assert collect.resolve_pid("/f/frida", None, "com.example") is None
    finally:
        collect.subprocess.run = orig


def test_wrapper_injects_manifest():
    entries = collect.load_manifest(str(MANIFEST_PATH))
    wrapper = collect.build_wrapper("/* hook.js body */", entries)
    assert wrapper.startswith("globalThis.APIS_OVERRIDE = ")
    assert "getSubscriberId" in wrapper
    assert wrapper.rstrip().endswith("/* hook.js body */")


def test_load_manifest_rejects_bad_shape(tmp_path):
    """Missing key -> 'missing non-empty'; bad risk value -> 'risk must be'."""
    missing = tmp_path / "missing.json"
    missing.write_text('{"apis": [{"class": "x"}]}', encoding="utf-8")
    try:
        collect.load_manifest(str(missing))
        raise AssertionError("expected ValueError")
    except ValueError as error:
        assert "missing non-empty" in str(error)
    bad_risk = tmp_path / "risk.json"
    bad_risk.write_text(
        '{"apis": [{"class": "c", "method": "m", "permission": "P", "risk": "highish"}]}',
        encoding="utf-8")
    try:
        collect.load_manifest(str(bad_risk))
        raise AssertionError("expected ValueError")
    except ValueError as error:
        assert "risk must be" in str(error)
    empty = tmp_path / "empty.json"
    empty.write_text('{"apis": []}', encoding="utf-8")
    assert collect.load_manifest(str(empty)) == []


class FakeAdb:
    """AdbClient stand-in: one device, package installed."""

    def __init__(self, serial=None, timeout=30, retries=2, dry_run=False, adb_path=None):
        self.serial = serial

    def devices(self):
        return ["SER123"]

    def _resolve_serial(self):
        self.serial = self.serial or "SER123"
        return self.serial

    def shell(self, cmd):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        if "pm path" in cmd_str:
            return 0, "package:/data/app/com.example/base.apk\n", ""
        return 1, "", "unexpected: %s" % cmd_str


class FakePopen:
    """Popen stand-in returning fixture frida stdout."""

    last_argv = None

    def __init__(self, argv, **kwargs):
        FakePopen.last_argv = list(argv)

    def communicate(self, timeout=None):
        self.returncode = 0
        return FIXTURE_STDOUT, ""

    def kill(self):
        pass


class TimeoutPopen(FakePopen):
    """Popen stand-in hitting the duration deadline on the first communicate."""

    def communicate(self, timeout=None):
        if not getattr(self, "_raised", False):
            self._raised = True
            raise subprocess.TimeoutExpired(cmd="frida", timeout=timeout)
        self.returncode = 0
        return FIXTURE_STDOUT, ""


def _patch_env(monkeypatch, popen_cls):
    monkeypatch.setattr(collect, "AdbClient", FakeAdb)
    monkeypatch.setattr(collect, "shutil", types.SimpleNamespace(
        which=lambda name: "/fake/frida", rmtree=lambda p, ignore_errors=False: None))
    monkeypatch.setattr(collect, "subprocess", types.SimpleNamespace(
        Popen=popen_cls, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(collect.tempfile, "mkdtemp", lambda prefix="": "/fake_tmp")
    monkeypatch.setattr("builtins.open", _guarded_open)


def _guarded_open(file, mode="r", **kwargs):
    if str(file).startswith("/fake_tmp"):
        import io
        return io.StringIO()
    return _real_open(file, mode, **kwargs)


_real_open = open


def test_main_mock_run_writes_schema_jsonl(tmp_path, monkeypatch):
    out_file = tmp_path / "audit.jsonl"
    _patch_env(monkeypatch, FakePopen)
    rc = collect.main([
        "--package", "com.example", "--duration-sec", "5",
        "--out", str(out_file),
    ])
    assert rc == 0
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        validate(instance=record, schema=SCHEMA)
        assert record["source"] == "frida"
        assert record["type"] == "sensitive_api"
        assert record["widget"] is None and record["screenshot"] is None
    assert FakePopen.last_argv[1:3] == ["-U", "-f"]
    assert any(arg.endswith("frida_wrapper.js") for arg in FakePopen.last_argv)


def test_main_timeout_still_writes(tmp_path, monkeypatch):
    out_file = tmp_path / "audit.jsonl"
    _patch_env(monkeypatch, TimeoutPopen)
    rc = collect.main([
        "--package", "com.example", "--duration-sec", "5", "--out", str(out_file)])
    assert rc == 0
    assert len(out_file.read_text(encoding="utf-8").splitlines()) == 2


def test_main_frida_missing_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(collect, "shutil", types.SimpleNamespace(which=lambda name: None))
    rc = collect.main(["--package", "com.example"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "pip install frida-tools" in captured.err


def test_main_device_absent_exit_1(monkeypatch, capsys):
    class NoDeviceAdb(FakeAdb):
        def devices(self):
            return []

    monkeypatch.setattr(collect, "AdbClient", NoDeviceAdb)
    monkeypatch.setattr(collect, "shutil", types.SimpleNamespace(
        which=lambda name: "/fake/frida"))
    rc = collect.main(["--package", "com.example"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no attached device" in captured.err


def test_main_package_missing_exit_1(monkeypatch, capsys):
    class NoPkgAdb(FakeAdb):
        def shell(self, cmd):
            return 1, "", "Failure"

    monkeypatch.setattr(collect, "AdbClient", NoPkgAdb)
    monkeypatch.setattr(collect, "shutil", types.SimpleNamespace(
        which=lambda name: "/fake/frida"))
    rc = collect.main(["--package", "com.missing"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not installed" in captured.err


def test_dry_run_zero_contact(capsys):
    rc = collect.main(["--dry-run", "--package", "com.example"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "frida -U -f com.example" in captured.out
    assert "[DRY-RUN]" in captured.out and "pm path com.example" in captured.out
    assert "no frida execution, no device contact" in captured.out
    assert "15 apis, 8 classes" in captured.out
