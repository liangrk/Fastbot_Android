"""weaknet_device.py tests: rule strings, uid/sni parsing, FakeAdb lifecycle, dry-run, loopback."""

import asyncio
from pathlib import Path

import pytest

import weaknet as wn
import weaknet_device as wd
from common.adb import AdbError

NL = chr(10)
CRLF = chr(13) + chr(10)
DROP_SPEC = ("-p udp --dport 443 -m owner --uid-owner 10123 "
             "-m comment --comment FASTBOT_WEAKNET -j DROP")
REDIRECT_SPEC_80 = ("-p tcp --dport 80 -m owner --uid-owner 10123 "
                    "-m comment --comment FASTBOT_WEAKNET -j REDIRECT "
                    "--to-ports 4242")


def _client_hello(host, with_sni=True):
    """Minimal SNI-carrying TLS ClientHello record (bytes built without escapes)."""
    host_b = host.encode("ascii")
    entry = bytes([0]) + len(host_b).to_bytes(2, "big") + host_b
    data = len(entry).to_bytes(2, "big") + entry
    exts = (10).to_bytes(2, "big") + (2).to_bytes(2, "big") + bytes([0, 23])
    if with_sni:
        exts = ((0).to_bytes(2, "big") + len(data).to_bytes(2, "big") + data
                + exts)
    body = (bytes([3, 3]) + bytes(32) + bytes([0])
            + (2).to_bytes(2, "big") + bytes([0x13, 1])
            + bytes([1, 0])
            + len(exts).to_bytes(2, "big") + exts)
    hs = bytes([1]) + len(body).to_bytes(3, "big") + body
    return bytes([0x16, 3, 1]) + len(hs).to_bytes(2, "big") + hs


class FakeAdb:
    """AdbClient double: scripted shell/_execute with a call log."""

    def __init__(self, serial=None, fail_root=False, uid_text=None,
                 filter_rules=None, nat_rules=None, list_holds=None):
        self.serial = serial
        self.fail_root = fail_root
        self.filter_rules = list(filter_rules or [])
        self.nat_rules = list(nat_rules or [])
        self.uid_text = uid_text if uid_text is not None else (
            "Packages:" + NL + "  userId=10123" + NL)
        self.list_holds = list_holds
        self.list_count = 0
        self.reverse_ports = [4242]
        self.calls = []

    def shell(self, cmd):
        self.calls.append(cmd)
        if cmd == "su -c id":
            if self.fail_root:
                return 1, "", ""
            return 0, "uid=0(root) gid=0(root)", ""
        if cmd == "id -u":
            return (0, "2000", "") if self.fail_root else (0, "0", "")
        if cmd.startswith("dumpsys package"):
            return 0, self.uid_text, ""
        if " -C " in cmd:
            table, spec = self._parse(cmd)
            if spec in self._store(table):
                return 0, "", ""
            raise AdbError("absent: " + spec)
        if " -A " in cmd:
            table, spec = self._parse(cmd)
            self._store(table).append(spec)
            return 0, "", ""
        if " -D " in cmd:
            table, spec = self._parse(cmd)
            store = self._store(table)
            if spec not in store:
                raise AdbError("absent: " + spec)
            store.remove(spec)
            return 0, "", ""
        if " -S OUTPUT" in cmd:
            store = self.nat_rules if "-t nat" in cmd else self.filter_rules
            return 0, "".join("-A OUTPUT " + r + NL for r in store), ""
        return 0, "", ""

    def _store(self, table):
        return self.nat_rules if table == "nat" else self.filter_rules

    @staticmethod
    def _parse(cmd):
        inner = cmd.split("su -c '", 1)[1].rstrip("'")
        table = "nat" if "-t nat" in inner else "filter"
        for op in ("-C OUTPUT ", "-A OUTPUT ", "-D OUTPUT "):
            if op in inner:
                return table, inner.split(op, 1)[1]
        raise AssertionError(cmd)

    def _execute(self, args):
        self.calls.append(" ".join(args))
        joined = " ".join(args)
        if joined.startswith("reverse --list"):
            self.list_count += 1
            if self.list_holds is not None and self.list_count > self.list_holds:
                return 0, "", ""
            lines = ["s1 tcp:%d tcp:%d" % (p, p) for p in self.reverse_ports]
            return 0, "".join(line + NL for line in lines), ""
        if joined.startswith("reverse --remove"):
            port = int(args[-1].split(":")[1])
            if port in self.reverse_ports:
                self.reverse_ports.remove(port)
            return 0, "", ""
        if joined.startswith("reverse tcp:"):
            port = int(args[1].split(":")[1])
            if port not in self.reverse_ports:
                self.reverse_ports.append(port)
            return 0, "", ""
        return 0, "", ""


def test_rule_strings_exact():
    assert wd.build_redirect_rule(10123, 4242, 80) == (
        "iptables -w 5 -t nat -A OUTPUT -p tcp --dport 80 "
        "-m owner --uid-owner 10123 -m comment --comment FASTBOT_WEAKNET "
        "-j REDIRECT --to-ports 4242")
    assert wd.build_redirect_rule(10123, 4242, 443) == (
        "iptables -w 5 -t nat -A OUTPUT -p tcp --dport 443 "
        "-m owner --uid-owner 10123 -m comment --comment FASTBOT_WEAKNET "
        "-j REDIRECT --to-ports 4242")
    assert wd.build_drop_rule(10123) == (
        "iptables -w 5 -A OUTPUT -p udp --dport 443 "
        "-m owner --uid-owner 10123 -m comment --comment FASTBOT_WEAKNET "
        "-j DROP")


def test_parse_package_uid():
    text = ("Packages:" + NL + "  Package [com.example] (1234):" + NL
            + "    userId=10123" + NL + "    flags=[ DEBUGGABLE ]" + NL)
    assert wd.parse_package_uid(text) == 10123
    assert wd.parse_package_uid("nothing here") is None


def test_parse_ports_valid_and_invalid():
    assert wd.parse_ports("80,443") == [80, 443]
    assert wd.parse_ports(" 80 , 443 ") == [80, 443]
    with pytest.raises(ValueError):
        wd.parse_ports("80,99999")
    with pytest.raises(ValueError):
        wd.parse_ports("http")
    with pytest.raises(ValueError):
        wd.parse_ports(",,")


def test_parse_sni_variants_and_m3_ownership():
    hello = _client_hello("srv.example.test")
    assert wd.parse_sni(hello) == "srv.example.test"
    assert wd.parse_sni(_client_hello("x.test", with_sni=False)) is None
    assert wd.parse_sni(b"GET / HTTP/1.1" + b"\r\n\r\n") is None
    assert wd.parse_sni(hello[:-1]) is None  # truncated record
    wd_src = Path(wd.__file__).read_text(encoding="utf-8")
    wn_src = Path(wn.__file__).read_text(encoding="utf-8")
    assert wd_src.count("def parse_sni") == 1
    assert "def parse_sni" not in wn_src


def test_probe_root_fails_before_any_add():
    adb = FakeAdb(fail_root=True)
    with pytest.raises(wd.RootUnavailableError):
        wd.probe_root(adb)
    assert adb.calls == ["su -c id", "id -u"]
    assert not any(" -A " in c for c in adb.calls)


def test_clean_then_apply_idempotent():
    adb = FakeAdb(filter_rules=[DROP_SPEC], nat_rules=[REDIRECT_SPEC_80])
    removed = wd.clean_rules(adb)
    assert removed == ["-A OUTPUT " + DROP_SPEC,
                       "-A OUTPUT " + REDIRECT_SPEC_80]
    assert adb.filter_rules == [] and adb.nat_rules == []
    applied = wd.apply_rules(adb, 10123, 4242, [80, 443], False)
    assert len(applied) == 3
    assert adb.nat_rules == [REDIRECT_SPEC_80,
                             "-p tcp --dport 443 -m owner --uid-owner 10123 "
                             "-m comment --comment FASTBOT_WEAKNET "
                             "-j REDIRECT --to-ports 4242"]
    assert adb.filter_rules == [DROP_SPEC]
    wd.clean_rules(adb)
    assert wd.apply_rules(adb, 10123, 4242, [80, 443], False) == applied


def test_apply_verify_miss_cleans_and_raises():
    class MissAdb(FakeAdb):
        def shell(self, cmd):
            if " -C " in cmd and "-p udp --dport 443" in cmd:
                self.calls.append(cmd)
                raise AdbError("simulated verify miss")
            return FakeAdb.shell(self, cmd)

    adb = MissAdb()
    with pytest.raises(wd.ApplyError):
        wd.apply_rules(adb, 10123, 4242, [80, 443], False)
    assert adb.filter_rules == [] and adb.nat_rules == []
    adds = [c for c in adb.calls if " -A " in c]
    deletes = [c for c in adb.calls if " -D " in c]
    assert len(adds) == 3 and len(deletes) == 3


def test_verify_clean_semantics():
    assert wd.verify_clean(FakeAdb()) is True
    assert wd.verify_clean(FakeAdb(filter_rules=[DROP_SPEC])) is False

    class HitAdb(FakeAdb):
        def shell(self, cmd):
            if " -C " in cmd:
                self.calls.append(cmd)
                return 0, "", ""  # -C hit despite empty listing
            return FakeAdb.shell(self, cmd)

    adb = HitAdb()
    stale = "-A OUTPUT " + DROP_SPEC
    assert wd.verify_clean(adb, [stale]) is False


def _patch_adb(monkeypatch, **pre):
    holder = {}

    def factory(**kw):
        adb = FakeAdb(**{**pre, **kw})
        holder["adb"] = adb
        return adb
    monkeypatch.setattr(wd, "AdbClient", factory)
    return holder


def _no_contact(**_kw):
    raise AssertionError("device contact attempted during --dry-run")


def test_device_on_dry_run_zero_contact_placeholder_uid(monkeypatch, capsys):
    monkeypatch.setattr(wd, "AdbClient", _no_contact)
    rc = wn.main(["device-on", "--package", "com.example", "--dry-run",
                  "--serial", "s1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no adb contact" in out
    assert out.count("--uid-owner <UID>") == 3
    assert out.count("--to-ports <PROXY_PORT>") == 3  # 2 rules + proxy-bind line
    assert "FASTBOT_WEAKNET" in out
    assert "dumpsys package com.example" in out


def test_device_on_dry_run_with_uid_embeds_10123(monkeypatch, capsys):
    monkeypatch.setattr(wd, "AdbClient", _no_contact)
    rc = wn.main(["device-on", "--package", "com.example", "--dry-run",
                  "--uid", "10123"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--uid-owner 10123" in out
    assert "<UID>" not in out
    assert out.count("iptables -w 5 -t nat -A OUTPUT") == 2
    assert out.count("iptables -w 5 -A OUTPUT -p udp") == 1
    assert "adb reverse tcp:<PROXY_PORT> tcp:<PROXY_PORT>" in out


def test_device_off_status_dry_runs(monkeypatch, capsys):
    monkeypatch.setattr(wd, "AdbClient", _no_contact)
    assert wn.main(["device-off", "--dry-run"]) == 0
    out1 = capsys.readouterr().out
    assert "iptables -w 5 -S OUTPUT" in out1
    assert "reverse --list" in out1
    assert wn.main(["device-status", "--dry-run"]) == 0
    out2 = capsys.readouterr().out
    assert "su -c id" in out2
    assert "reverse --list" in out2


def test_device_on_no_root_exits_1_zero_rules(monkeypatch, capsys):
    holder = _patch_adb(monkeypatch, fail_root=True)
    rc = wn.main(["device-on", "--package", "com.example", "--serial", "s1",
                  "--uid", "10123"])
    assert rc == 1
    adb = holder["adb"]
    out = capsys.readouterr()
    assert "no root" in out.err
    assert not any(" -A " in c for c in adb.calls)
    assert "su -c id" in adb.calls and "id -u" in adb.calls


def _ctrl_c_after(n):
    real = asyncio.sleep
    state = {"n": 0}

    async def fake_sleep(t):
        if state["n"] >= n:
            raise KeyboardInterrupt
        state["n"] += 1
        return await real(0)
    return fake_sleep


def test_device_on_happy_path_ctrl_c_teardown(monkeypatch, capsys):
    holder = _patch_adb(monkeypatch)
    monkeypatch.setattr(wn, "STATS_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(asyncio, "sleep", _ctrl_c_after(3))
    rc = wn.main(["device-on", "--package", "com.example", "--serial", "s1",
                  "--uid", "10123", "--profile", "wifi"])
    assert rc == 130
    adb = holder["adb"]
    assert len([c for c in adb.calls if " -A " in c]) == 3
    assert len([c for c in adb.calls if " -D " in c]) == 3
    assert adb.filter_rules == [] and adb.nat_rules == []
    assert adb.calls[-1].startswith("reverse --remove tcp:")
    out = capsys.readouterr().out
    assert "shaping uid=10123" in out


def test_device_on_resolves_uid_via_dumpsys(monkeypatch, capsys):
    holder = _patch_adb(monkeypatch)
    monkeypatch.setattr(wn, "STATS_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(asyncio, "sleep", _ctrl_c_after(3))
    rc = wn.main(["device-on", "--package", "com.example", "--serial", "s1",
                  "--profile", "wifi"])
    assert rc == 130
    adb = holder["adb"]
    assert any(c == "dumpsys package com.example" for c in adb.calls)
    adds = [c for c in adb.calls if " -A " in c]
    assert len(adds) == 3 and "--uid-owner 10123" in adds[0]


def test_reverse_lost_readd_once_then_teardown(monkeypatch, capsys):
    holder = _patch_adb(monkeypatch, list_holds=1)
    monkeypatch.setattr(wn, "STATS_INTERVAL_SEC", 0.01)
    rc = wn.main(["device-on", "--package", "com.example", "--serial", "s1",
                  "--uid", "10123", "--profile", "wifi"])
    assert rc == 1
    adb = holder["adb"]
    out = capsys.readouterr().out
    assert "re-adding once" in out
    adds = [c for c in adb.calls if " -A " in c]
    assert len(adds) == 3
    readds = [c for c in adb.calls
              if c.startswith("reverse tcp:") and "--remove" not in c]
    assert len(readds) >= 2  # initial + one re-add
    assert adb.calls[-1].startswith("reverse --remove tcp:")
    assert adb.filter_rules == [] and adb.nat_rules == []

# --- LOOPBACK ---


def _wifi_profile():
    return wn.resolve_profile("wifi", None, None, None, None)


def test_transparent_tls_split_client_hello_roundtrip(monkeypatch):
    hello = _client_hello("127.0.0.1")
    state = {}

    async def upstream(reader, writer):
        try:
            state["hello"] = await reader.readexactly(len(hello))
            writer.write(b"TLS-OK")
            await writer.drain()
            await reader.read()  # hold open until the client disconnects
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def scenario(monkeypatch):
        up_srv = await asyncio.start_server(upstream, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]
        monkeypatch.setattr(wd, "TLS_UPSTREAM_PORT", up_port)
        proxy = wn.WeakNetProxy("127.0.0.1", 0, _wifi_profile(), transparent=True)
        await proxy.start()
        try:
            c_reader, c_writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port())
            c_writer.write(hello[:8])  # M4: hello split across two sends
            await c_writer.drain()
            await asyncio.sleep(0.05)
            c_writer.write(hello[8:])
            await c_writer.drain()
            marker = await asyncio.wait_for(c_reader.readexactly(6), timeout=10)
            assert marker == b"TLS-OK"
            c_writer.close()
        finally:
            await proxy.close()
            up_srv.close()
            await up_srv.wait_closed()
        assert state["hello"] == hello  # byte-exact through the tunnel
        assert proxy.counters.connections == 1
        assert proxy.counters.errors == 0

    asyncio.run(scenario(monkeypatch))


def test_transparent_no_sni_tls_counts_error():
    async def scenario():
        proxy = wn.WeakNetProxy("127.0.0.1", 0, _wifi_profile(), transparent=True)
        await proxy.start()
        try:
            c_reader, c_writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port())
            c_writer.write(_client_hello("h.test", with_sni=False))
            await c_writer.drain()
            try:
                data = await asyncio.wait_for(c_reader.read(64), timeout=5)
            except (ConnectionResetError, OSError):
                data = b""  # Windows RST = same failure signal
            assert data == b""
            c_writer.close()
        finally:
            await proxy.close()
        assert proxy.counters.errors == 1

    asyncio.run(scenario())


def test_transparent_http_origin_path():
    async def scenario():
        async def peek(reader, writer):
            try:
                line = await reader.readline()
                writer.write(b"SEEN:" + line)
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()

        peek_srv = await asyncio.start_server(peek, "127.0.0.1", 0)
        peek_port = peek_srv.sockets[0].getsockname()[1]
        proxy = wn.WeakNetProxy("127.0.0.1", 0, _wifi_profile(), transparent=True)
        await proxy.start()
        try:
            c_reader, c_writer = await asyncio.open_connection(
                "127.0.0.1", proxy.bound_port())
            request = ("GET /x.js HTTP/1.1" + CRLF
                       + "Host: 127.0.0.1:%d" % peek_port + CRLF
                       + "Connection: close" + CRLF + CRLF)
            c_writer.write(request.encode("ascii"))
            await c_writer.drain()
            response = await asyncio.wait_for(c_reader.read(-1), timeout=10)
            assert response.startswith(b"SEEN:GET /x.js HTTP/1.1")
            c_writer.close()
        finally:
            await proxy.close()
            peek_srv.close()
            await peek_srv.wait_closed()
        assert proxy.counters.errors == 0

    asyncio.run(scenario())


def test_transparent_unknown_and_cap_errors():
    async def scenario():
        proxy = wn.WeakNetProxy("127.0.0.1", 0, _wifi_profile(), transparent=True)
        await proxy.start()
        try:
            c1_r, c1_w = await asyncio.open_connection("127.0.0.1", proxy.bound_port())
            c1_w.write(bytes([0x16, 3, 1]) + (65535).to_bytes(2, "big"))
            await c1_w.drain()
            c1_w.close()  # cap-exceeded ClientHello header
            await asyncio.sleep(0.2)
            c2_r, c2_w = await asyncio.open_connection("127.0.0.1", proxy.bound_port())
            c2_w.write(bytes([9] * 8))
            await c2_w.drain()
            c2_w.close()  # non-TLS junk, never a header end
            await asyncio.sleep(0.2)
        finally:
            await proxy.close()
        assert proxy.counters.connections == 2
        assert proxy.counters.errors == 2
    asyncio.run(scenario())


def test_device_on_invalid_package_rc2(capsys):
    rc = wn.main(["device-on", "--package", "com.evil;reboot", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "--package" in captured.err
