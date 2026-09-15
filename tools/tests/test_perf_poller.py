"""perf_poller.py wiring tests: plan, dry-run, tick collection, report."""

import json
import time
from pathlib import Path

from jsonschema import validate

import perf_poller as pp
import perf_report as pr

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = TOOLS_DIR.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "perf"
SAMPLE_SCHEMA = json.loads(
    (REPO_DIR / "tools" / "schemas" / "perf_sample.schema.json").read_text(encoding="utf-8"))


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeAdb:
    """AdbClient stand-in returning fixture text per command."""

    def __init__(self):
        self.serial = "emulator-5554"

    def shell(self, cmd):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        if "cpuinfo" in cmd_str:
            return 0, _fixture("cpuinfo_checkin.txt"), ""
        if "meminfo" in cmd_str:
            return 0, _fixture("meminfo_checkin.txt"), ""
        if "netstats" in cmd_str:
            return 0, _fixture("netstats_checkin.txt"), ""
        if "battery" in cmd_str:
            return 0, _fixture("battery_checkin.txt"), ""
        if "logcat" in cmd_str:
            return 0, _fixture("logcat_displayed_mixed.txt"), ""
        if "pidof" in cmd_str:
            return 0, "31572" + chr(10), ""
        return 1, "", "unexpected"


def test_plan_shape():
    plan = pp.build_plan("com.example", 10.0, 60.0)
    assert plan["package"] == "com.example"
    assert plan["interval_sec"] == 10.0
    assert plan["duration_sec"] == 60.0
    assert plan["ticks"] == 6
    collectors = [c["collector"] for c in plan["per_tick_commands"]
                  if c["collector"] != "displayed"]
    assert collectors == ["cpuinfo", "meminfo", "netstats", "battery", "pidof"]
    mem_cmd = [c for c in plan["per_tick_commands"] if c["collector"] == "meminfo"][0]
    assert mem_cmd["command"] == "dumpsys meminfo com.example --checkin"
    assert "Displayed" in plan["start_detection"]["primary"]
    assert "pidof" in plan["start_detection"]["corroboration"]


def test_dry_run_prints_plan_and_needs_no_device(capsys):
    rc = pp.main(["--package", "com.example", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "无设备接触" in captured.out
    assert "dumpsys cpuinfo --checkin" in captured.out
    assert "dumpsys meminfo com.example --checkin" in captured.out
    assert "dumpsys netstats --checkin" in captured.out
    assert "dumpsys battery --checkin" in captured.out
    assert "Displayed" in captured.out
    assert "pidof" in captured.out
    assert "佐证" in captured.out
    assert "主信号" in captured.out


def test_collect_tick_fake_adb():
    tracker = pp.StartTracker()
    sample, events = pp.collect_tick(FakeAdb(), "com.example", tracker)
    assert sample["serial"] == "emulator-5554"
    assert sample["source"] == "pc"
    assert sample["cpu_percent"] == 4.6
    assert sample["mem_pss_kb"] == 24927
    assert sample["net_rx_bytes"] == 59231
    assert sample["net_tx_bytes"] == 15522
    assert sample["battery_pct"] == 86
    assert [e["kind"] for e in events] == ["unknown", "warm", "cold"]
    validate(instance=sample, schema=SAMPLE_SCHEMA)


def test_collect_tick_degrades_on_adb_error():
    from common.adb import AdbError

    class BrokenAdb:
        serial = None

        def shell(self, cmd):
            raise AdbError("boom")

    sample, events = pp.collect_tick(BrokenAdb(), "com.example", pp.StartTracker())
    assert sample is None
    assert events == []


def test_report_render_with_starts(tmp_path):
    tracker = pp.StartTracker()
    sample1, events = pp.collect_tick(FakeAdb(), "com.example", tracker)
    sample2 = dict(sample1)
    sample2["ts"] = sample1["ts"] + 10000
    starts = list(events)
    html = pr.render_perf_report([sample1, sample2], starts, meta={
        "package": "com.example",
        "serial": "emulator-5554",
        "note": "轮询方法论测试",
    })
    assert "Fastbot" in html
    assert "com.example" in html
    assert "轮询方法论测试" in html
    assert "<svg" in html
    # start events render into the report table
    assert "cold" in html
    starts_path = tmp_path / "starts.json"
    starts_path.write_text(
        chr(10).join(json.dumps(e, ensure_ascii=False) for e in starts),
        encoding="utf-8")
    loaded = [json.loads(line) for line in starts_path.read_text(encoding="utf-8").splitlines()]
    assert len(loaded) == 3


def test_poller_main_writes_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pp, "AdbClient", lambda **kw: FakeAdb())
    monkeypatch.setattr(pp.time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "strftime", lambda *a, **k: "20260916_120000")
    fake = {"t": 1000.0}

    def fake_monotonic():
        fake["t"] += 5.0
        return fake["t"]

    monkeypatch.setattr(pp.time, "monotonic", fake_monotonic)
    rc = pp.main(["--package", "com.example", "--duration", "10", "--interval", "10",
                  "--out", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    perf_dir = tmp_path / "perf"
    parsed = [json.loads(line)
              for line in (perf_dir / "data.json").read_text(encoding="utf-8").splitlines()]
    assert len(parsed) == 2
    for item in parsed:
        validate(instance=item, schema=SAMPLE_SCHEMA)
    assert (perf_dir / "starts.json").exists()
    assert (perf_dir / "report.html").exists()
    assert "perf data written" in captured.out
    assert "perf report written" in captured.out


