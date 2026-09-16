"""weaknet.py tests: profiles, parsing, shaping math, adb wiring, live loopback."""

import asyncio
import random
import time

import pytest

import weaknet as wn

# --------------------------------------------------------------------- #
# profiles command
# --------------------------------------------------------------------- #

def test_profiles_command(capsys):
    rc = wn.main(["profiles"])
    out = capsys.readouterr().out
    assert rc == 0
    for name in ("edge", "3g", "lossy", "wifi", "custom"):
        assert name in out
    assert "400" in out and "200" in out and "150" in out
    assert "http_proxy" in out


# --------------------------------------------------------------------- #
# profile resolution
# --------------------------------------------------------------------- #

def test_profile_presets():
    for name, knobs in (("edge", (400, 100, 2.0, 50.0)),
                        ("3g", (200, 50, 1.5, 750.0)),
                        ("lossy", (150, 50, 10.0, 0.0)),
                        ("wifi", (0, 0, 0.0, 0.0))):
        profile = wn.resolve_profile(name, None, None, None, None)
        got = (profile.latency_ms, profile.jitter_ms,
               profile.loss_pct, profile.bandwidth_kbps)
        assert got == knobs
        assert profile.name == name


def test_profile_custom_requires_knob():
    with pytest.raises(ValueError):
        wn.resolve_profile("custom", None, None, None, None)


def test_profile_custom_partial_and_preset_override():
    profile = wn.resolve_profile("custom", 300, None, 5.0, None)
    assert (profile.latency_ms, profile.jitter_ms,
            profile.loss_pct, profile.bandwidth_kbps) == (300, 0, 5.0, 0.0)
    overridden = wn.resolve_profile("3g", 500, None, None, None)
    assert overridden.latency_ms == 500
    assert overridden.loss_pct == 1.5


def test_profile_invalid_inputs():
    with pytest.raises(ValueError):
        wn.resolve_profile("lte", None, None, None, None)
    with pytest.raises(ValueError):
        wn.resolve_profile("custom", -5, None, None, None)
    with pytest.raises(ValueError):
        wn.resolve_profile("3g", None, None, 150.0, None)
    with pytest.raises(ValueError):
        wn.resolve_profile("3g", None, None, None, -1.0)


# --------------------------------------------------------------------- #
# request-line parser (byte fixtures)
# --------------------------------------------------------------------- #

def test_parse_connect_request_line():
    parsed = wn.parse_request_line(b"CONNECT api.example.com:443 HTTP/1.1\r\n")
    assert parsed is not None
    assert parsed["kind"] == "connect"
    assert parsed["host"] == "api.example.com"
    assert parsed["port"] == 443
    assert parsed["rewritten"] is None


def test_parse_absolute_get_rewrites_to_origin_form():
    parsed = wn.parse_request_line(b"GET http://cdn.example.com/a/b.js HTTP/1.1\r\n")
    assert parsed is not None
    assert parsed["kind"] == "absolute"
    assert parsed["host"] == "cdn.example.com"
    assert parsed["port"] == 80
    assert parsed["rewritten"] == "GET /a/b.js HTTP/1.1"


def test_parse_origin_form_resolves_target_from_host_header():
    parsed = wn.parse_request_line(b"GET /path/q?a=b HTTP/1.1\r\n")
    assert parsed is not None
    assert parsed["kind"] == "origin"
    headers = wn.parse_headers(["Host: reg.example.com:8080",
                                "User-Agent: fastbot-test"])
    assert parsed["rewritten"] == "GET /path/q?a=b HTTP/1.1"
    assert wn.resolve_target(parsed, headers) == ("reg.example.com", 8080)


def test_parse_garbage_rejected():
    assert wn.parse_request_line(b"NONSENSE\r\n") is None
    assert wn.parse_request_line(b"GET ftp://x/y HTTP/1.1\r\n") is None
    assert wn.parse_request_line(b"CONNECT noport HTTP/1.1\r\n") is None
    assert wn.parse_request_line(b"GET /x HTTP/2.0\r\n") is None
    assert wn.parse_request_line(b"\r\n") is None
    assert wn.parse_request_line(b"") is None


def test_parse_headers_and_missing_host():
    headers = wn.parse_headers(["Host: h.example.com",
                                "X-Forwarded-For: 10.0.0.9"])
    assert headers["host"] == "h.example.com"
    parsed = wn.parse_request_line(b"GET /x HTTP/1.1\r\n")
    assert wn.resolve_target(parsed, {}) == (None, 80)


# --------------------------------------------------------------------- #
# shaping decisions
# --------------------------------------------------------------------- #

def test_latency_math_injected_rng():
    class FixedRng:
        def randint(self, low, high):
            return 37

    assert wn.compute_delay_ms(200, 50, FixedRng()) == 237
    assert wn.compute_delay_ms(200, 0, FixedRng()) == 200
    assert wn.compute_delay_ms(0, 0, FixedRng()) == 0


def test_token_bucket_math_injected_clock():
    cell = {"now": 0.0}

    def clock():
        return cell["now"]

    bucket = wn.TokenBucket(8, clock=clock)  # 8 kbps = 1024 bytes/s
    assert bucket.delay_for(512) == 0.0            # within 1s burst
    assert bucket.delay_for(2048) == pytest.approx(1.0)  # (2048-1024)/1024
    bucket.consume(2048)
    assert bucket.tokens == pytest.approx(-1024.0)
    cell["now"] = 1.0                              # refill 1024 -> tokens 0
    assert bucket.delay_for(512) == pytest.approx(0.5)
    unlimited = wn.TokenBucket(0, clock=clock)
    assert unlimited.delay_for(10 ** 9) == 0.0


def test_loss_with_seeded_random():
    assert wn.should_drop_connection(0.0, random.Random(42)) is False
    assert wn.should_drop_connection(100.0, random.Random(1)) is True
    rng = random.Random(1234)
    outcomes = [wn.should_drop_connection(50.0, rng) for _ in range(1000)]
    assert 400 < sum(outcomes) < 600  # deterministic given the seed


# --------------------------------------------------------------------- #
# AdbClient wiring and --dry-run zero contact
# --------------------------------------------------------------------- #

class FakeAdb:
    """AdbClient stand-in recording every shell call."""

    def __init__(self, serial=None, **_kw):
        self.serial = serial
        self.calls = []

    def shell(self, cmd):
        self.calls.append(cmd)
        if "settings get" in cmd:
            return 0, ":0" + chr(10), ""
        return 0, "", ""


def test_start_dry_run_zero_contact(monkeypatch, capsys):
    def forbidden(**_kw):
        raise AssertionError("AdbClient must not be constructed in dry-run")

    monkeypatch.setattr(wn, "AdbClient", forbidden)
    rc = wn.main(["start", "--dry-run", "--profile", "3g",
                  "--serial", "emulator-5554", "--pc-ip", "192.168.1.10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no adb contact, no sockets bound" in out
    assert "settings put global http_proxy 192.168.1.10:8123" in out
    assert "settings put global http_proxy :0" in out
    assert "settings get global http_proxy" in out
    assert "auto-detect" not in out  # pc-ip given: no auto-detect note


def test_stop_wiring_and_idempotent_clear(monkeypatch, capsys):
    constructed = []
    holder = {}

    def factory(**kw):
        client = FakeAdb(**kw)
        constructed.append(kw)
        holder["adb"] = client
        return client

    monkeypatch.setattr(wn, "AdbClient", factory)
    rc = wn.main(["stop", "--serial", "emu-1"])
    assert rc == 0
    assert constructed == [{"serial": "emu-1"}]
    cmds = holder["adb"].calls
    assert any("settings put global http_proxy :0" in c for c in cmds)
    assert any("settings get global http_proxy" in c for c in cmds)
    # idempotent: clearing twice stays clean
    rc2 = wn.main(["stop", "--serial", "emu-1"])
    assert rc2 == 0


def test_stop_warns_when_not_cleared(monkeypatch, capsys):
    class StubbornAdb(FakeAdb):
        def shell(self, cmd):
            self.calls.append(cmd)
            if "settings get" in cmd:
                return 0, "192.168.1.5:8123", ""
            return 0, "", ""

    monkeypatch.setattr(wn, "AdbClient",
                        lambda **kw: StubbornAdb(**kw))
    rc = wn.main(["stop", "--serial", "emu-1"])
    assert rc == 1


def test_start_restore_on_crash(monkeypatch):
    """Simulated exception after the set still emits the clear command."""
    calls = []

    class CrashAdb:
        def __init__(self, serial=None, **_kw):
            pass

        def shell(self, cmd):
            calls.append(cmd)
            return 0, ":0", ""

    monkeypatch.setattr(wn, "AdbClient", CrashAdb)

    async def boom(args, profile, adb, pc_ip, flags):
        adb.shell("settings put global http_proxy 10.0.0.1:8123")
        flags["set"] = True
        raise RuntimeError("simulated crash mid-run")

    monkeypatch.setattr(wn, "_amain_start", boom)
    with pytest.raises(RuntimeError):
        wn.main(["start", "--pc-ip", "10.0.0.1",
                 "--serial", "emu-1", "--profile", "wifi"])
    sets = [c for c in calls if "10.0.0.1:8123" in c]
    clears = [c for c in calls if c == wn.CLEAR_PROXY_CMD]
    assert sets and clears
    assert calls.index(sets[0]) < calls.index(clears[0])


def test_start_restore_on_keyboard_interrupt(monkeypatch):
    calls = []

    class CrashAdb:
        def __init__(self, serial=None, **_kw):
            pass

        def shell(self, cmd):
            calls.append(cmd)
            return 0, ":0", ""

    monkeypatch.setattr(wn, "AdbClient", CrashAdb)

    async def interrupt_me(args, profile, adb, pc_ip, flags):
        adb.shell("settings put global http_proxy 10.0.0.1:8123")
        flags["set"] = True
        raise KeyboardInterrupt

    monkeypatch.setattr(wn, "_amain_start", interrupt_me)
    rc = wn.main(["start", "--pc-ip", "10.0.0.1",
                  "--serial", "emu-1", "--profile", "wifi"])
    assert rc == 130
    clears = [c for c in calls if c == wn.CLEAR_PROXY_CMD]
    assert clears  # Ctrl+C still clears


def test_start_full_loop_fake_adb(monkeypatch, capsys):
    calls = []

    class LoopAdb:
        def __init__(self, serial=None, **_kw):
            self.serial = serial

        def shell(self, cmd):
            calls.append(cmd)
            return 0, ":0", ""

    monkeypatch.setattr(wn, "AdbClient", LoopAdb)
    rc = wn.main(["start", "--pc-ip", "10.0.0.1", "--serial", "emu-1",
                  "--port", "0", "--duration-sec", "0.3",
                  "--profile", "wifi"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "proxy listening" in out
    assert "device http_proxy set to 10.0.0.1:" in out
    assert calls, "set must have run"
    assert calls[0].startswith("settings put global http_proxy 10.0.0.1:")
    assert calls[-2:] == [wn.CLEAR_PROXY_CMD, wn.GET_PROXY_CMD]


def test_start_auto_detect_note_in_dry_run(monkeypatch, capsys):
    def forbidden(**_kw):
        raise AssertionError("no adb contact expected")

    monkeypatch.setattr(wn, "AdbClient", forbidden)
    rc = wn.main(["start", "--profile", "edge", "--duration-sec", "2",
                  "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "auto-detect" in out
    assert "<auto-pc-ip>" in out


# --------------------------------------------------------------------- #
# live loopback integration (localhost only)
# --------------------------------------------------------------------- #

async def _echo_server(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _peek_server(reader, writer):
    """Reply SEEN:<first request line> then close (proves the rewrite)."""
    try:
        line = await reader.readline()
        writer.write(b"SEEN:" + line)
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _open_via_proxy(proxy, target_port):
    reader, writer = await asyncio.open_connection("127.0.0.1",
                                                   proxy.bound_port())
    request = "CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % target_port
    writer.write(request.encode("ascii"))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    assert b"200 Connection Established" in head
    return reader, writer


def test_live_loopback_wifi_roundtrip_byte_exact():
    """Named for acceptance: wifi preset passthrough roundtrip is exact."""

    async def scenario():
        echo = await asyncio.start_server(_echo_server, "127.0.0.1", 0)
        echo_port = echo.sockets[0].getsockname()[1]
        profile = wn.resolve_profile("wifi", None, None, None, None)
        proxy = wn.WeakNetProxy("127.0.0.1", 0, profile)
        await proxy.start()
        try:
            reader, writer = await _open_via_proxy(proxy, echo_port)
            payload = bytes(range(256)) * 8  # 2048 bytes
            writer.write(payload)
            await writer.drain()
            received = b""
            while len(received) < len(payload):
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
                if not chunk:
                    break
                received += chunk
            assert received == payload  # byte-exact roundtrip
            writer.close()
        finally:
            await proxy.close()
            echo.close()
            await echo.wait_closed()
        assert proxy.counters.connections == 1
        assert proxy.counters.dropped == 0
        assert proxy.counters.total_bytes() >= len(payload)

    asyncio.run(scenario())


def test_live_loopback_latency_lower_bound():
    """One-sided timing assertion (>= lower bound) to stay flake-free."""

    async def scenario():
        echo = await asyncio.start_server(_echo_server, "127.0.0.1", 0)
        echo_port = echo.sockets[0].getsockname()[1]
        profile = wn.Profile("slow", latency_ms=300, jitter_ms=0,
                             loss_pct=0.0, bandwidth_kbps=0.0)
        proxy = wn.WeakNetProxy("127.0.0.1", 0, profile)
        await proxy.start()
        try:
            started = time.monotonic()
            await _open_via_proxy(proxy, echo_port)
            elapsed = time.monotonic() - started
            assert elapsed >= 0.25  # one-sided: at least ~latency
        finally:
            await proxy.close()
            echo.close()
            await echo.wait_closed()

    asyncio.run(scenario())


def test_live_loopback_loss_100_closes_connection():
    """loss=100 must close immediately: the app sees a network failure."""

    async def scenario():
        profile = wn.Profile("allloss", 0, 0, 100.0, 0.0)
        proxy = wn.WeakNetProxy("127.0.0.1", 0, profile)
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port())
            writer.write(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n")
            await writer.drain()
            try:
                data = await asyncio.wait_for(reader.read(1024), timeout=5)
            except ConnectionResetError:
                data = b""  # Windows RST on abrupt close = same failure signal
            assert data == b""  # EOF without any reply
            writer.close()
        finally:
            await proxy.close()
        assert proxy.counters.dropped == 1
        assert proxy.counters.connections == 1

    asyncio.run(scenario())


def test_live_loopback_absolute_get_rewrites_origin_form():
    """Absolute-URI GET reaches the origin server rewritten to origin-form."""

    async def scenario():
        peek = await asyncio.start_server(_peek_server, "127.0.0.1", 0)
        peek_port = peek.sockets[0].getsockname()[1]
        profile = wn.resolve_profile("wifi", None, None, None, None)
        proxy = wn.WeakNetProxy("127.0.0.1", 0, profile)
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port())
            request = ("GET http://127.0.0.1:%d/x.js HTTP/1.1\r\n"
                       "Host: ignored.example.com\r\n"
                       "Connection: close\r\n\r\n" % peek_port)
            writer.write(request.encode("ascii"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(-1), timeout=10)
            assert response.startswith(b"SEEN:GET /x.js HTTP/1.1")
            writer.close()
        finally:
            await proxy.close()
            peek.close()
            await peek.wait_closed()

    asyncio.run(scenario())
