"""perf_poller parser unit tests on realistic fixtures (no device required).

Includes a small python mirror of the device-side PerfFrameEvent parsing
rules (same markers, same delta-fps rule) so the emitted line can be checked
against perf_frame.schema.json offline - the Java parser itself is verified
by the full javac build.
"""

import json
from pathlib import Path

from jsonschema import validate

import perf_poller as pp

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = TOOLS_DIR.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "perf"
SAMPLE_SCHEMA = json.loads(
    (REPO_DIR / "tools" / "schemas" / "perf_sample.schema.json").read_text(encoding="utf-8"))
FRAME_SCHEMA = json.loads(
    (REPO_DIR / "tools" / "schemas" / "perf_frame.schema.json").read_text(encoding="utf-8"))


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# device-side mirror: same rules as PerfFrameEvent (Java) for schema checks
# ---------------------------------------------------------------------------

def java_mirror_frame_line(gfx_text, prev_total, prev_ts, ts):
    """Mirror PerfFrameEvent.parse+fps rules. Returns a perf_frame dict or
    None (first window / unparseable), like the Java emitter."""
    def first_long_after(text, marker):
        idx = text.find(marker)
        if idx < 0:
            return None
        i = idx + len(marker)
        while i < len(text):
            if text[i] == chr(10):
                return None
            if text[i].isdigit():
                break
            i += 1
        if i >= len(text):
            return None
        start = i
        while i < len(text) and text[i].isdigit():
            i += 1
        return int(text[start:i])

    total = first_long_after(gfx_text, "Total frames rendered:")
    janky = first_long_after(gfx_text, "Janky frames:")
    p90 = first_long_after(gfx_text, "90th percentile:")
    if total is None or janky is None or p90 is None:
        return None
    if prev_total < 0:
        return None  # first window: no rate, Java skips the sample
    frames = total - prev_total
    if frames < 0:
        frames = total  # restart re-baseline
    fps = round(frames * 1000.0 / (ts - prev_ts), 2)
    return {"ts": ts, "fps": fps, "janky_frames": janky, "p90_ms": float(p90)}


def test_mirror_first_window_skipped():
    line = java_mirror_frame_line(_fixture("gfxinfo_framestats.txt"), -1, 0, 1734412805456)
    assert line is None


def test_mirror_frame_line_valid():
    line = java_mirror_frame_line(_fixture("gfxinfo_framestats.txt"), 45, 1734412800456, 1734412805456)
    assert line == {"ts": 1734412805456, "fps": 12.0, "janky_frames": 12, "p90_ms": 12.0}
    validate(instance=line, schema=FRAME_SCHEMA)


def test_mirror_restart_rebaselines():
    line = java_mirror_frame_line(_fixture("gfxinfo_framestats.txt"), 500, 1734412800456, 1734412805456)
    assert line["fps"] == 21.0  # 105 frames from the new process over 5s


def test_mirror_no_crash_on_empty_garbage():
    assert java_mirror_frame_line("", -1, 0, 1) is None
    assert java_mirror_frame_line("total nonsense", 10, 0, 1000) is None


# ---------------------------------------------------------------------------
# PC-side parser tests: normal / empty / garbage -> no crash
# ---------------------------------------------------------------------------

def test_parse_cpuinfo_normal():
    result = pp.parse_cpuinfo(_fixture("cpuinfo_checkin.txt"), "com.example")
    assert result == {"cpu_percent": 4.6}


def test_parse_cpuinfo_package_missing():
    assert pp.parse_cpuinfo(_fixture("cpuinfo_checkin.txt"), "com.absent") is None


def test_parse_cpuinfo_empty_and_garbage():
    assert pp.parse_cpuinfo("", "com.example") is None
    assert pp.parse_cpuinfo("garbage without commas", "com.example") is None
    assert pp.parse_cpuinfo("a,b,c", None) is None  # percent not numeric
    assert pp.parse_cpuinfo("10047,-5.0,com.example", "com.example") is None  # negative rejected


def test_parse_meminfo_normal():
    result = pp.parse_meminfo(_fixture("meminfo_checkin.txt"))
    assert result == {"mem_pss_row_len": 24, "mem_pss_kb": 24927}


def test_parse_meminfo_empty_and_garbage():
    assert pp.parse_meminfo("") is None
    assert pp.parse_meminfo("9,3,a") is None
    assert pp.parse_meminfo("9,3,a,x,y,z") is None
    assert pp.parse_meminfo("8,3,a,1,2,3,4,5,6,7,8") is None  # wrong version tag


def test_parse_netstats_normal():
    result = pp.parse_netstats(_fixture("netstats_checkin.txt"))
    assert result == {"net_rx_bytes": 59231, "net_tx_bytes": 15522}


def test_parse_netstats_seven_field_variant():
    seven = "D,100,1,2,3,4,5" + chr(10) + "D,200,6,7,8,9,10"
    result = pp.parse_netstats(seven)
    assert result == {"net_rx_bytes": 7, "net_tx_bytes": 11}


def test_parse_netstats_empty_and_garbage():
    assert pp.parse_netstats("") is None
    assert pp.parse_netstats("Xt I Q (0) 0 0") is None
    assert pp.parse_netstats("D,1,2,3") is None  # too short
    assert pp.parse_netstats("D,1,x,y,z,w,v,u") is None  # non-numeric


def test_parse_battery_normal():
    assert pp.parse_battery(_fixture("battery_checkin.txt")) == {"battery_pct": 86}


def test_parse_battery_rejects_out_of_range():
    assert pp.parse_battery("9,2,2,1,2,250,4352,4400,0,0") is None
    assert pp.parse_battery("9,2,2,1,2,-1,4352") is None


def test_parse_battery_empty_and_garbage():
    assert pp.parse_battery("") is None
    assert pp.parse_battery("9") is None
    assert pp.parse_battery("9,a,b,c,d,e") is None


# ---------------------------------------------------------------------------
# Displayed-line parsing + cold/warm classification (PRIMARY signal tests)
# ---------------------------------------------------------------------------

def test_displayed_mixed_fixture_records():
    records = pp.parse_displayed_lines(_fixture("logcat_displayed_mixed.txt"), "com.example")
    assert [r["pid"] for r in records] == [24561, 24561, 31572]
    assert [r["duration_ms"] for r in records] == [1234, 287, 2105]
    assert records[0]["activity"] == "com.example/.MainActivity"
    assert records[2]["activity"] == "com.example/.MainActivity"


def test_displayed_empty_and_garbage():
    assert pp.parse_displayed_lines("", "com.example") == []
    assert pp.parse_displayed_lines("no logcat here", "com.example") == []
    assert pp.parse_displayed_lines(None, "com.example") == []


def test_duration_parsing():
    assert pp._parse_displayed_duration("+1s234ms") == 1234
    assert pp._parse_displayed_duration("+287ms") == 287
    assert pp._parse_displayed_duration("+2s105ms") == 2105
    assert pp._parse_displayed_duration("+0") == 0


def test_cold_warm_mixed_sequence():
    records = pp.parse_displayed_lines(_fixture("logcat_displayed_mixed.txt"), "com.example")
    tracker = pp.StartTracker()
    events = tracker.feed(records)
    assert [e["kind"] for e in events] == ["unknown", "warm", "cold"]


def test_cold_warm_cold_fixture():
    records = pp.parse_displayed_lines(_fixture("logcat_displayed_cold.txt"), "com.example")
    tracker = pp.StartTracker()
    events = tracker.feed(records)
    assert [e["kind"] for e in events] == ["unknown", "cold"]
    assert events[1]["pid"] == 31572
    assert events[1]["duration_ms"] == 2105


def test_cold_warm_warm_fixture():
    records = pp.parse_displayed_lines(_fixture("logcat_displayed_warm.txt"), "com.example")
    tracker = pp.StartTracker()
    events = tracker.feed(records)
    assert [e["kind"] for e in events] == ["unknown", "warm", "warm"]


def test_tracker_dedup_across_repeated_reads():
    records = pp.parse_displayed_lines(_fixture("logcat_displayed_mixed.txt"), "com.example")
    tracker = pp.StartTracker()
    first = tracker.feed(records)
    second = tracker.feed(records)  # same buffer re-read: no new events
    assert len(first) == 3
    assert second == []


def test_incremental_feed_appends_only_new():
    records = pp.parse_displayed_lines(_fixture("logcat_displayed_mixed.txt"), "com.example")
    tracker = pp.StartTracker()
    tracker.feed(records[:2])
    more = tracker.feed(records)  # third record is new
    assert len(more) == 1
    assert more[0]["kind"] == "cold"


def test_perf_sample_lines_from_fixtures_conform_schema():
    sample = pp.build_sample_line(
        1734412805456, "emulator-5554",
        pp.parse_cpuinfo(_fixture("cpuinfo_checkin.txt"), "com.example"),
        pp.parse_meminfo(_fixture("meminfo_checkin.txt")),
        pp.parse_netstats(_fixture("netstats_checkin.txt")),
        pp.parse_battery(_fixture("battery_checkin.txt")),
    )
    validate(instance=sample, schema=SAMPLE_SCHEMA)
    assert sample["cpu_percent"] == 4.6
    assert sample["mem_pss_kb"] == 24927
    assert sample["net_rx_bytes"] == 59231
    assert sample["net_tx_bytes"] == 15522
    assert sample["battery_pct"] == 86


def test_perf_sample_line_minimal_conforms_schema():
    minimal = pp.build_sample_line(1, "s", None, None, None, None)
    assert minimal == {"ts": 1, "serial": "s", "source": "pc"}
    validate(instance=minimal, schema=SAMPLE_SCHEMA)
