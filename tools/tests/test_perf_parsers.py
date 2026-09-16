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

# ---------------------------------------------------------------------------
# framestats raw CSV: perf_report.parse_framestats_csv / frame_time_stats
# ---------------------------------------------------------------------------

import perf_report as pr


def _derive_csv_from_gfxinfo(gfx_text, ts=1789497600000):
    """Simulate the device-side PerfFrameEvent CSV emission: PROFILEDATA
    header (first section) + every section's data rows, ts-prefixed."""
    marker = '---' + 'PROFILEDATA' + '---'
    pos = 0
    header = None
    rows = []
    while True:
        begin = gfx_text.find(marker, pos)
        if begin < 0:
            break
        end = gfx_text.find(marker, begin + len(marker))
        if end < 0:
            break
        section = [ln.strip() for ln in
                   gfx_text[begin + len(marker):end].splitlines() if ln.strip()]
        if header is None:
            header = section[0]
        rows.extend(section[1:])
        pos = end + len(marker)
    lines = ['ts_ms,' + header] + ['%d,%s' % (ts, r) for r in rows]
    return chr(10).join(lines) + chr(10), header, rows


REAL_FIXTURE = FIXTURES / 'gfxinfo_framestats_real.txt'


def test_framestats_real_fixture_accepted_and_dropped():
    """Real Android 13 capture (me.ele): 4 raw rows, 1 sanity-dropped
    (Flags=8 row has FrameCompleted=0 -> negative frame time)."""
    csv_text, header, raw_rows = _derive_csv_from_gfxinfo(REAL_FIXTURE.read_text(encoding='utf-8'))
    assert len(raw_rows) == 4
    assert 'IntendedVsync' in header and 'FrameCompleted' in header
    stats = pr.frame_time_stats(csv_text)
    assert stats['count'] == 3
    assert stats['dropped'] == 1
    assert stats['skipped_rows'] == 0
    assert 0 < stats['min'] <= stats['p50'] <= stats['p90'] <= stats['p99'] <= stats['max']
    assert stats['max'] < pr.MAX_PLAUSIBLE_FRAME_MS
    rows = pr.parse_framestats_csv(csv_text)
    assert len(rows) == 4
    assert rows[0]['frame_time_ms'] > 50.0  # 58.27ms first launch frame
    assert abs(rows[1]['frame_time_ms'] - 4.106586) < 0.001


def test_framestats_missing_profiledata_section():
    report = []
    rows = pr.parse_framestats_csv('Stats since: 123ns' + chr(10) + 'no section here', report=report)
    assert rows == []
    assert report  # reason recorded
    assert pr.frame_time_stats('no section at all') is None


def test_framestats_malformed_and_nonnumeric_rows_counted():
    header = 'ts_ms,Flags,IntendedVsync,Vsync,FrameCompleted'
    good = '1000,0,1000000,1000000,61000000'
    lines = [header,
             good,
             '1001,0,2000000',                # too few fields
             '1002,notanint,3000000,3000000,62000000',  # non-numeric Flags
             '1003,0,xyz,4000000,63000000',   # non-numeric IntendedVsync
             '',                              # blank: ignored, not counted
             '1004,0,5000000,5000000,4000000']  # negative frame time -> dropped by guard
    report = []
    rows = pr.parse_framestats_csv(chr(10).join(lines), report=report)
    assert [r['ts_ms'] for r in rows] == [1000, 1004]  # both parseable rows return; guard drops later
    assert len(report) == 3
    assert all(s.startswith('line ') for s in report)
    stats = pr.frame_time_stats(chr(10).join(lines))
    assert stats['count'] == 1
    assert stats['dropped'] == 1
    assert stats['skipped_rows'] == 3


def test_framestats_exact_frametime_integer_math():
    """ns values above 2^53: int subtraction must happen before float
    division (a float parse of each field would lose the low bits)."""
    iv = 2472277837274748
    fc = 2472277895542367
    header = 'ts_ms,Flags,IntendedVsync,FrameCompleted'
    row = '1789497600000,0,%d,%d' % (iv, fc)
    rows = pr.parse_framestats_csv(header + chr(10) + row)
    assert len(rows) == 1
    assert abs(rows[0]['frame_time_ms'] - (fc - iv) / 1000000.0) < 1e-9
    assert abs(rows[0]['frame_time_ms'] - 58.267619) < 1e-6


def test_framestats_empty_and_bad_header():
    assert pr.parse_framestats_csv('') == []
    assert pr.parse_framestats_csv('a,b,c' + chr(10) + '1,2,3') == []
    assert pr.frame_time_stats('') is None


def test_framestats_section_rendering():
    csv_text, _header, _rows = _derive_csv_from_gfxinfo(REAL_FIXTURE.read_text(encoding='utf-8'))
    stats = pr.frame_time_stats(csv_text)
    sections = pr.build_sections([], frame_stats=stats)
    html = pr.render_report('t', sections)
    assert '帧耗时分布 (framestats)' in html
    assert 'p99' in html and '<rect' in html
    # absent stats -> section silently omitted
    sections_plain = pr.build_sections([])
    html_plain = pr.render_report('t', sections_plain)
    assert '帧耗时分布' not in html_plain
