"""chaos_restore.py unit tests: snapshot parsing, per-channel restore
command mapping (mirrors of the Java restoreState bodies), and the
matrix_runner timeout restore path with a mocked AdbClient."""

import json

import chaos_restore as cr
import matrix_runner as mr


def _snapshot_json(states):
    return json.dumps({"captured_at": "2026-09-16T00:00:00Z",
                       "states": states})


def _item(state, value):
    return {"state": state, "value": value, "channel": "shell"}


# ---------------------------------------------------------------------------
# parse_snapshot / parse_snapshot_explained
# ---------------------------------------------------------------------------

def test_parse_snapshot_valid_all_eight_states():
    text = _snapshot_json([
        _item("battery", "level=60;ac=false;usb=true"),
        _item("power_save", "low_power=0"),
        _item("bluetooth", "bluetooth_on=1"),
        _item("location", "location_mode=3"),
        _item("mobile_data", "mobile_data=1"),
        _item("vpn", "always_on_vpn_lockdown=0;always_on_vpn_app=null"),
        _item("dnd", "zen_mode=0"),
        _item("system_config", "night_mode=no;font_scale=1.0"),
    ])
    states = cr.parse_snapshot(text)
    assert states == [
        ("battery", "level=60;ac=false;usb=true"),
        ("power_save", "low_power=0"),
        ("bluetooth", "bluetooth_on=1"),
        ("location", "location_mode=3"),
        ("mobile_data", "mobile_data=1"),
        ("vpn", "always_on_vpn_lockdown=0;always_on_vpn_app=null"),
        ("dnd", "zen_mode=0"),
        ("system_config", "night_mode=no;font_scale=1.0"),
    ]
    assert [s for s, _v in cr.parse_snapshot(text)] == [
        "battery", "power_save", "bluetooth", "location",
        "mobile_data", "vpn", "dnd", "system_config"]


def test_parse_snapshot_empty_and_bad_json():
    for text in ("", None, "   ", "not json {", "[1, 2]"):
        states, problems = cr.parse_snapshot_explained(text)
        assert states == []
        assert len(problems) == 1


def test_parse_snapshot_missing_states_array():
    states, problems = cr.parse_snapshot_explained('{"captured_at": "t"}')
    assert states == []
    assert problems == ["快照缺少 states 数组"]


def test_parse_snapshot_partial_items_with_reasons():
    text = _snapshot_json([
        "not-an-object",
        _item("warp_drive", "engaged=1"),
        {"state": "dnd", "value": 7, "channel": "shell"},
        {"state": "dnd", "value": "zen_mode=0", "channel": "adb"},
        _item("battery", "level=80;ac=true;usb=false"),
        {"state": "dnd", "value": "zen_mode=0"},
    ])
    states, problems = cr.parse_snapshot_explained(text)
    # battery + the channel-omitted dnd item are accepted
    assert states == [("battery", "level=80;ac=true;usb=false"),
                      ("dnd", "zen_mode=0")]
    assert len(problems) == 4
    assert any("states[0]" in p and "不是对象" in p for p in problems)
    assert any("states[1]" in p and "warp_drive" in p for p in problems)
    assert any("states[2]" in p for p in problems)
    assert any("states[3]" in p for p in problems)
    # channel omitted entirely is tolerated
    assert states == cr.parse_snapshot(text)


# ---------------------------------------------------------------------------
# restore_commands / restore_decision: all 8 channels, exact Java mirrors
# ---------------------------------------------------------------------------

def test_restore_battery_ignores_payload():
    assert cr.restore_commands("battery", "level=15;ac=false;usb=false") == [
        "dumpsys battery reset"]
    assert cr.restore_commands("battery", "") == ["dumpsys battery reset"]
    assert cr.restore_decision("battery", "").skip_reason is None


def test_restore_bluetooth_truthy_branch():
    assert cr.restore_commands("bluetooth", "bluetooth_on=1") == [
        "svc bluetooth enable"]
    assert cr.restore_commands("bluetooth", "bluetooth_on=true") == [
        "svc bluetooth enable"]
    assert cr.restore_commands("bluetooth", "bluetooth_on=0") == [
        "svc bluetooth disable"]
    assert cr.restore_commands("bluetooth", "bluetooth_on=false") == [
        "svc bluetooth disable"]


def test_restore_bluetooth_unparseable_skips():
    decision = cr.restore_decision("bluetooth", "total garbage")
    assert decision.commands == []
    assert decision.skip_reason == "值不可解析"


def test_restore_power_save_put_or_delete():
    assert cr.restore_commands("power_save", "low_power=0") == [
        "settings put global low_power 0"]
    for absent in ("low_power=null", ""):
        assert cr.restore_commands("power_save", absent) == [
            "settings delete global low_power"]


def test_restore_location_secure_namespace():
    assert cr.restore_commands("location", "location_mode=3") == [
        "settings put secure location_mode 3"]
    assert cr.restore_commands("location", "location_mode=null") == [
        "settings delete secure location_mode"]


def test_restore_mobile_data_truthy_branch():
    assert cr.restore_commands("mobile_data", "mobile_data=1") == [
        "svc data enable"]
    assert cr.restore_commands("mobile_data", "mobile_data=0") == [
        "svc data disable"]
    assert cr.restore_decision("mobile_data", "??").skip_reason == "值不可解析"


def test_restore_vpn_lockdown_then_app_with_deletes():
    assert cr.restore_commands(
        "vpn", "always_on_vpn_lockdown=1;always_on_vpn_app=com.vpn.app") == [
            "settings put global always_on_vpn_lockdown 1",
            "settings put global always_on_vpn_app com.vpn.app"]
    # null app value -> delete (putOrDeleteSetting mirror), lockdown first
    assert cr.restore_commands(
        "vpn", "always_on_vpn_lockdown=0;always_on_vpn_app=null") == [
            "settings put global always_on_vpn_lockdown 0",
            "settings delete global always_on_vpn_app"]
    # both keys missing -> cannot restore: skip with reason
    decision = cr.restore_decision("vpn", "unparseable")
    assert decision.commands == [] and decision.skip_reason == "值不可解析"


def test_restore_dnd_off_then_zen_mode():
    assert cr.restore_commands("dnd", "zen_mode=2") == [
        "cmd notification set_dnd off",
        "settings put global zen_mode 2"]
    assert cr.restore_commands("dnd", "zen_mode=null") == [
        "cmd notification set_dnd off",
        "settings delete global zen_mode"]


def test_restore_system_config_night_and_font_scale():
    assert cr.restore_commands("system_config", "night_mode=yes;font_scale=1.1") == [
        "cmd uimode night yes",
        "settings put system font_scale 1.1"]
    # Java fallbacks: night != "yes" -> no; missing font_scale -> 1.0
    assert cr.restore_commands("system_config", "") == [
        "cmd uimode night no",
        "settings put system font_scale 1.0"]
    assert cr.restore_commands("system_config", "night_mode=no;font_scale=null") == [
        "cmd uimode night no",
        "settings put system font_scale 1.0"]


def test_restore_unknown_state_skips():
    decision = cr.restore_decision("airplane", "mode=1")
    assert decision.commands == []
    assert decision.skip_reason == "未知状态: airplane"


def test_truthy_mirrors_java():
    assert cr.truthy("1") and cr.truthy("true") and cr.truthy("True")
    assert not cr.truthy("0") and not cr.truthy("yes") and not cr.truthy(None)


# ---------------------------------------------------------------------------
# matrix_runner._reset_chaos_after_timeout with a mocked AdbClient
# ---------------------------------------------------------------------------

class FakeShellClient:
    """Records shell calls; serves the snapshot for `cat`, per-command rc map."""

    def __init__(self, snapshot=None, snapshot_rc=0, failing=()):
        self.calls = []
        self.snapshot = snapshot
        self.snapshot_rc = snapshot_rc
        self.failing = set(failing)

    def shell(self, cmd):
        self.calls.append(cmd)
        if cmd.startswith("cat "):
            if self.snapshot is None:
                return self.snapshot_rc, "", "No such file or directory"
            return 0, self.snapshot, ""
        if cmd in self.failing:
            return 1, "", "cmd failed"
        return 0, "", ""


FULL_SNAPSHOT = _snapshot_json([
    _item("battery", "level=15;ac=false;usb=false"),
    _item("bluetooth", "bluetooth_on=1"),
    _item("power_save", "low_power=1"),
    _item("dnd", "zen_mode=1"),
])


def _record():
    return {"notes": []}


def test_timeout_full_restore_per_channel():
    client = FakeShellClient(snapshot=FULL_SNAPSHOT)
    record = _record()
    mr._reset_chaos_after_timeout(client, record)
    assert client.calls == [
        "cat /sdcard/fastbot_chaos.snapshot",
        "dumpsys battery reset",
        "svc bluetooth enable",
        "settings put global low_power 1",
        "cmd notification set_dnd off",
        "settings put global zen_mode 1",
    ]
    note = record["notes"][0]
    assert note.startswith("chaos 恢复: ")
    assert "battery ok" in note and "bluetooth ok" in note
    assert "power_save ok" in note and "dnd ok" in note
    assert "跳过" not in note


def test_timeout_command_failure_and_detail_note():
    client = FakeShellClient(snapshot=FULL_SNAPSHOT,
                             failing=["cmd notification set_dnd off"])
    record = _record()
    mr._reset_chaos_after_timeout(client, record)
    note = record["notes"][0]
    assert "dnd 失败(原始值: zen_mode=1)" in note
    assert "battery ok" in note
    # command sequence stops at the failed command for that state only
    assert "cmd notification set_dnd off" in client.calls
    assert "settings put global zen_mode 1" not in client.calls
    assert any("chaos 恢复失败详情" in n and "rc=1" in n
               for n in record["notes"][1:])


def test_timeout_skips_unparseable_value():
    text = _snapshot_json([
        _item("battery", "level=15;ac=false;usb=false"),
        _item("vpn", "garbage-payload"),
    ])
    client = FakeShellClient(snapshot=text)
    record = _record()
    mr._reset_chaos_after_timeout(client, record)
    assert "settings put global always_on_vpn_lockdown 1" not in client.calls
    assert "dumpsys battery reset" in client.calls
    assert "跳过: vpn(值不可解析)" in record["notes"][0]


def test_timeout_missing_snapshot_falls_back_to_battery_reset():
    client = FakeShellClient(snapshot=None, snapshot_rc=1)
    record = _record()
    mr._reset_chaos_after_timeout(client, record)
    assert client.calls == ["cat /sdcard/fastbot_chaos.snapshot",
                            "dumpsys battery reset"]
    joined = chr(10).join(record["notes"])
    assert "chaos 快照不可用: 快照为空或不可读" in joined
    assert "超时强杀可能使设备侧 chaos 状态残留" in joined


def test_timeout_bad_json_snapshot_falls_back():
    client = FakeShellClient(snapshot="{broken json")
    record = _record()
    mr._reset_chaos_after_timeout(client, record)
    assert client.calls[-1] == "dumpsys battery reset"
    joined = chr(10).join(record["notes"])
    assert "快照 JSON 解析失败" in joined
    assert "超时强杀可能使设备侧 chaos 状态残留" in joined


def test_timeout_shell_exception_falls_back():
    class ExplodingClient:
        def __init__(self):
            self.calls = []

        def shell(self, cmd):
            from common.adb import AdbError
            self.calls.append(cmd)
            raise AdbError("device offline")
    client = ExplodingClient()
    record = _record()
    mr._reset_chaos_after_timeout(client, record)
    # best-effort: the battery reset is still attempted and its failure noted
    assert client.calls == ["cat /sdcard/fastbot_chaos.snapshot",
                            "dumpsys battery reset"]
    joined = chr(10).join(record["notes"])
    assert "chaos 快照读取失败" in joined
    assert "battery reset 失败" in joined
    assert "超时强杀可能使设备侧 chaos 状态残留" in joined
