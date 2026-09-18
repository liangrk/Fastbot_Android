#!/usr/bin/env python3
"""Device-side weak-network shaping for Fastbot (rooted device, UID scoped).

Subcommands (registered into weaknet.py via register_subparsers):
  device-on      UID-scoped iptables REDIRECT of the target app's TCP traffic
                 (default 80,443) to a PC-side shaping proxy over `adb reverse`,
                 plus a UDP/443 DROP pushing QUIC apps back to TCP
                 (--allow-quic restores it). Non-target apps: untouched.
  device-off     manual recovery: delete every FASTBOT_WEAKNET rule, verify
                 zero leftovers, then remove reverse mappings.
  device-status  root state, tagged rules, reverse mappings.

Transparent mode (WeakNetProxy(transparent=True)): bounded peek loop - read
the 5-byte TLS record header, derive record length, accumulate until the
ClientHello is complete or the ~16KB cap (ClientHello often spans TCP
segments). HTTP/CONNECT takes the phase-1 proxy path; TLS with an SNI
extension connects straight to host:443 - E2E TLS, no CA install, no MITM.
No-SNI TLS, cap-exceeded and unknown protocols are counted as errors and
closed. Needs root; probe_root runs BEFORE any rule is added, so a no-root
device exits 1 with zero partial state. No --duration-sec by design:
caller-driven timeout, recovery is device-off.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Dict, List, Optional, Tuple

from common.adb import AdbClient, AdbError

import weaknet as wn

TAG = "FASTBOT_WEAKNET"
IPT = "iptables -w 5"
UID_PLACEHOLDER, PORT_PLACEHOLDER = "<UID>", "<PROXY_PORT>"
NL, CRLF = chr(10), chr(13) + chr(10)
TLS_HANDSHAKE_BYTE = 0x16
TLS_UPSTREAM_PORT = 443
TLS_PEEK_CAP = 5 + 16384
TLS_CONNECT_TIMEOUT_SEC = 10.0
MAX_CLEAN_SWEEPS = 5


class RootUnavailableError(RuntimeError):
    """Raised before any rule is added when the device has no root."""


class ApplyError(RuntimeError):
    """Raised when applied rules fail verification (after cleanup)."""


def _su(cmd: str) -> str:
    return "su -c '%s'" % cmd


def _try_shell(adb: AdbClient, cmd: str) -> Tuple[bool, str]:
    """shell() as (ok, stdout); AdbClient raises on nonzero rc."""
    try:
        rc, out, _err = adb.shell(cmd)
        return rc == 0, out
    except AdbError:
        return False, ""


def build_redirect_rule(uid: int, port: int, dport: int) -> str:
    return ("%s -t nat -A OUTPUT -p tcp --dport %s -m owner --uid-owner %s "
            "-m comment --comment %s -j REDIRECT --to-ports %s"
            % (IPT, dport, uid, TAG, port))


def build_drop_rule(uid: int, dport: int = 443) -> str:
    return ("%s -A OUTPUT -p udp --dport %s -m owner --uid-owner %s "
            "-m comment --comment %s -j DROP" % (IPT, dport, uid, TAG))


def parse_package_uid(dumpsys_text: str) -> Optional[int]:
    for line in dumpsys_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("userId="):
            continue
        fields = stripped[len("userId="):].split()
        if fields:
            try:
                return int(fields[0])
            except ValueError:
                continue
    return None


def parse_ports(spec: str) -> List[int]:
    ports: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            port = int(part)
        except ValueError:
            raise ValueError("bad port in --ports %r" % spec)
        if not 0 < port < 65536:
            raise ValueError("port out of range in --ports %r" % spec)
        ports.append(port)
    if not ports:
        raise ValueError("--ports %r resolves to nothing" % spec)
    return ports


def probe_root(adb: AdbClient) -> None:
    ok, out = _try_shell(adb, "su -c id")
    if ok and "uid=0" in out:
        return
    ok, out = _try_shell(adb, "id -u")
    if ok and out.strip() == "0":
        return
    raise RootUnavailableError(
        "device has no root (su -c id and id -u both failed); enable "
        "adb root or install su - NO rules were applied")


def list_tagged_rules(adb: AdbClient) -> Tuple[List[str], List[str]]:
    _ok, fout = _try_shell(adb, _su("%s -S OUTPUT" % IPT))
    _ok, nout = _try_shell(adb, _su("%s -t nat -S OUTPUT" % IPT))
    return ([line.strip() for line in fout.splitlines() if TAG in line],
            [line.strip() for line in nout.splitlines() if TAG in line])


def clean_rules(adb: AdbClient) -> List[str]:
    """Delete every tagged rule (-A rewritten to -D) until listing is empty.
    Idempotent; safe on a device with zero tagged rules."""
    removed: List[str] = []
    for _sweep in range(MAX_CLEAN_SWEEPS):
        rules, nat_rules = list_tagged_rules(adb)
        stale = ([("filter", line) for line in rules]
                 + [("nat", line) for line in nat_rules])
        if not stale:
            return removed
        removable = [pair for pair in stale
                     if "'" not in pair[1] and '"' not in pair[1]]
        if stale and not removable:
            raise ApplyError(
                "tagged rule line contains a quote char; device listing is "
                "untrusted - manual iptables cleanup required")
        for table, line in removable:
            flag = "-t nat " if table == "nat" else ""
            adb.shell(_su(IPT + " " + flag + line.replace("-A ", "-D ", 1)))
            removed.append(line)
    raise ApplyError("tagged rules still present after %d clean sweeps"
                     % MAX_CLEAN_SWEEPS)


def apply_rules(adb: AdbClient, uid: int, port: int, ports: List[int],
                allow_quic: bool) -> List[str]:
    rules = [build_redirect_rule(uid, port, dport) for dport in ports]
    if not allow_quic:
        rules.append(build_drop_rule(uid))
    for rule in rules:
        ok, _out = _try_shell(adb, _su(rule))
        if not ok:
            clean_rules(adb)
            raise ApplyError("iptables refused: %s" % rule)
    for rule in rules:
        ok, _out = _try_shell(adb, _su(rule.replace("-A ", "-C ", 1)))
        if not ok:
            clean_rules(adb)
            raise ApplyError("verification miss after apply: %s" % rule)
    return rules


def verify_clean(adb: AdbClient,
                 rules: Optional[List[str]] = None) -> bool:
    """True when -S shows zero tagged rules AND each formerly applied rule
    answers -C with a miss."""
    leftover, nat_leftover = list_tagged_rules(adb)
    if leftover or nat_leftover:
        return False
    for line in rules or []:
        if _try_shell(adb, _su(line.replace("-A ", "-C ", 1)))[0]:
            return False
    return True


def parse_sni(head: bytes) -> Optional[str]:
    """Extract the host_name from a complete TLS ClientHello record."""
    if len(head) < 6 or head[0] != TLS_HANDSHAKE_BYTE:
        return None
    if len(head) < 5 + int.from_bytes(head[3:5], "big"):
        return None
    pos = 5
    if head[pos] != 0x01:  # not a ClientHello handshake type
        return None
    pos += 4            # handshake header (type + 3-byte length)
    pos += 2 + 32       # client version + random
    if pos >= len(head):
        return None
    pos += 1 + head[pos]                                 # session id
    if pos + 2 > len(head):
        return None
    pos += 2 + int.from_bytes(head[pos:pos + 2], "big")  # cipher suites
    if pos >= len(head):
        return None
    pos += 1 + head[pos]                                 # compression
    if pos + 2 > len(head):
        return None
    extensions_end = pos + 2 + int.from_bytes(head[pos:pos + 2], "big")
    pos += 2
    end = min(extensions_end, len(head))
    while pos + 4 <= end:
        ext_type = int.from_bytes(head[pos:pos + 2], "big")
        ext_len = int.from_bytes(head[pos + 2:pos + 4], "big")
        body = head[pos + 4:pos + 4 + ext_len]
        pos += 4 + ext_len
        if ext_type != 0x0000 or len(body) < 5:
            continue
        if body[2] == 0:  # host_name entry
            name_len = int.from_bytes(body[3:5], "big")
            if len(body) >= 5 + name_len:
                return body[5:5 + name_len].decode("latin-1")
    return None


async def _read_n(reader: asyncio.StreamReader, n: int) -> Optional[bytes]:
    """Exactly n bytes, or None on EOF mid-read."""
    buf = bytearray()
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


async def handle_transparent(proxy: wn.WeakNetProxy,
                             reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> Tuple[int, int]:
    """Bounded-peek dispatcher: TLS-with-SNI, HTTP, or counted close.

    Every read is timeout-guarded: an idle or port-scan connection must not
    pin a task forever (P4 code-review finding #3).
    """
    peek_timeout = wn.HEADER_TIMEOUT_SEC
    try:
        head = await asyncio.wait_for(_read_n(reader, 5), timeout=peek_timeout)
    except asyncio.TimeoutError:
        proxy.counters.errors += 1
        return 0, 0
    if head is None:
        proxy.counters.errors += 1
        return 0, 0
    if head[0] == TLS_HANDSHAKE_BYTE:
        total = 5 + int.from_bytes(head[3:5], "big")
        try:
            rest = (None if total > TLS_PEEK_CAP else
                    await asyncio.wait_for(_read_n(reader, total - 5),
                                           timeout=peek_timeout))
        except asyncio.TimeoutError:
            proxy.counters.errors += 1
            return 0, 0
        if rest is None:
            proxy.counters.errors += 1
            return 0, 0
        host = parse_sni(head + rest)
        if host:
            return await _pipe_tls(proxy, reader, writer, head + rest, host)
        proxy.counters.errors += 1
        return 0, 0
    buf = bytearray(head)
    while wn.HEADER_END not in buf and len(buf) <= TLS_PEEK_CAP:
        try:
            chunk = await asyncio.wait_for(reader.read(4096),
                                           timeout=peek_timeout)
        except asyncio.TimeoutError:
            proxy.counters.errors += 1
            return 0, 0
        if not chunk:
            break
        buf.extend(chunk)
    if wn.HEADER_END not in buf:
        proxy.counters.errors += 1
        return 0, 0
    return await _finish_http(proxy, reader, writer, bytes(buf))


async def _pipe_tls(proxy: wn.WeakNetProxy, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter, client_hello: bytes,
                    host: str) -> Tuple[int, int]:
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, TLS_UPSTREAM_PORT),
            timeout=TLS_CONNECT_TIMEOUT_SEC)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        proxy.counters.errors += 1
        return 0, 0
    upstream_writer.write(client_hello)
    await upstream_writer.drain()
    return await wn._bridge(reader, writer, upstream_reader, upstream_writer,
                            proxy.profile, proxy.counters)


async def _finish_http(proxy: wn.WeakNetProxy, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter,
                       head: bytes) -> Tuple[int, int]:
    """HTTP branch of transparent mode; mirrors weaknet._handle_client."""
    lines = head.decode("latin-1").split(CRLF)[:-1]
    if not lines:
        proxy.counters.errors += 1
        return 0, 0
    parsed = wn.parse_request_line(lines[0])
    if parsed is None:
        proxy.counters.errors += 1
        await wn._respond_simple(writer, 400, "Bad Request")
        return 0, 0
    headers = wn.parse_headers(lines[1:])
    if parsed["kind"] == "connect":
        return await wn._pipe_connect(reader, writer, parsed,
                                      proxy.profile, proxy.counters)
    host, port = wn.resolve_target(parsed, headers)
    if not host:
        proxy.counters.errors += 1
        await wn._respond_simple(writer, 400, "Bad Request (missing Host)")
        return 0, 0
    forward = [str(parsed["rewritten"])] + lines[1:]
    if "host" not in headers:
        host_header = str(host) if port == 80 else "%s:%d" % (host, port)
        forward.append("Host: " + host_header)
    request_bytes = (CRLF.join(forward) + CRLF + CRLF).encode("latin-1")
    return await wn._pipe_http(reader, writer, str(host), port,
                               request_bytes, proxy.profile, proxy.counters)


def _reverse(adb: AdbClient, args: List[str]) -> Tuple[bool, str]:
    # AdbClient exposes no reverse wrapper; use the raw host-command path.
    try:
        rc, out, _err = adb._execute(args)
        return rc == 0, out
    except AdbError:
        return False, ""


async def _amain_device_on(args: argparse.Namespace, adb: AdbClient,
                           profile: wn.Profile, ports: List[int],
                           state: Dict[str, object]) -> None:
    try:
        probe_root(adb)
        uid = args.uid
        if uid is None:
            _ok, out = _try_shell(adb, "dumpsys package %s" % args.package)
            uid = parse_package_uid(out)
            if uid is None:
                raise AdbError("no uid found for package %s" % args.package)
        print("[weaknet] uid=%d resolved for %s" % (uid, args.package), flush=True)
        clean_rules(adb)
        proxy = wn.WeakNetProxy(args.bind_host, args.port, profile,
                                transparent=True)
        await proxy.start()
        state["proxy"] = proxy
        q = proxy.bound_port()
        state["reverse_port"] = q
        _reverse(adb, ["reverse", "tcp:%d" % q, "tcp:%d" % q])
        apply_rules(adb, uid, q, ports, args.allow_quic)
        print("[weaknet] shaping uid=%d ports=%s via tcp:%d; Ctrl+C to stop "
              "(teardown is guaranteed; after SIGKILL run device-off)"
              % (uid, ports, q), flush=True)
        re_added = False
        while True:
            await asyncio.sleep(wn.STATS_INTERVAL_SEC)
            _ok, listing = _reverse(adb, ["reverse", "--list"])
            if "tcp:%d" % q in listing:
                continue
            if re_added:
                raise AdbError("reverse tunnel tcp:%d lost again; giving up" % q)
            print("[weaknet] reverse tcp:%d missing; re-adding once" % q, flush=True)
            if not _reverse(adb, ["reverse", "tcp:%d" % q, "tcp:%d" % q])[0]:
                raise AdbError("reverse re-add failed; giving up")
            re_added = True
    finally:
        await _teardown(adb, state)


async def _teardown(adb: AdbClient, state: Dict[str, object]) -> None:
    """Teardown order is a contract: RULES, REVERSE, PROXY."""
    try:
        clean_rules(adb)
        if not verify_clean(adb):
            print("WARNING: %s rules remain; manual recovery: python "
                  "tools/weaknet.py device-off" % TAG, file=sys.stderr)
    except (AdbError, ApplyError) as error:
        print("WARNING: rule cleanup failed: %s (manual recovery: python "
              "tools/weaknet.py device-off)" % error, file=sys.stderr)
    q = state.get("reverse_port")
    if q:
        _reverse(adb, ["reverse", "--remove", "tcp:%d" % int(q)])
    proxy = state.get("proxy")
    if proxy is not None:
        await proxy.close()


def print_device_on_plan(args, profile, ports) -> None:
    uid = str(args.uid) if args.uid is not None else UID_PLACEHOLDER
    pport = str(args.port) if args.port else PORT_PLACEHOLDER
    lines = [
        "weaknet device-on dry-run plan (no adb contact, no sockets bound)",
        "  serial: %s" % (args.serial if args.serial else "(auto-detect at start)"),
        "  package: %s" % args.package,
        "  uid: %s (resolved at start via: adb shell dumpsys package %s)" % (uid, args.package),
        "  profile: %s" % profile.describe(),
        "  ports: %s allow_quic: %s" % (ports, args.allow_quic),
        "  proxy bind: %s:%s (REDIRECT --to-ports %s)" % (args.bind_host, pport, pport),
        "  planned iptables rules (each via adb shell su -c '<rule>'):",
    ]
    lines += ["    %s" % build_redirect_rule(uid, pport, dport) for dport in ports]
    if not args.allow_quic:
        lines.append("    %s" % build_drop_rule(uid))
    lines += [
        "  planned tunnel: adb reverse tcp:%s tcp:%s" % (pport, pport),
        "  idempotency: existing %s rules deleted before applying" % TAG,
        "  teardown: device-off / Ctrl+C (rc 130); no duration auto-teardown",
    ]
    print(NL.join(lines))


_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_-]+)+$")


def cmd_device_on(args: argparse.Namespace) -> int:
    if not _PACKAGE_RE.match(args.package):
        print("ERROR: --package looks invalid (expect com.example.app): %r"
              % args.package, file=sys.stderr)
        return 2
    try:
        profile = wn.resolve_profile(args.profile, args.latency_ms, args.jitter_ms,
                                     args.loss_pct, args.bandwidth_kbps)
        ports = parse_ports(args.ports)
    except ValueError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2
    if args.dry_run:
        print_device_on_plan(args, profile, ports)
        return 0
    adb = AdbClient(serial=args.serial)
    state: Dict[str, object] = {"proxy": None, "reverse_port": None}
    try:
        asyncio.run(_amain_device_on(args, adb, profile, ports, state))
        return 0
    except KeyboardInterrupt:
        print("[weaknet] interrupted", flush=True)
        return 130
    except (RootUnavailableError, ApplyError, AdbError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1


def cmd_device_off(args: argparse.Namespace) -> int:
    if args.dry_run:
        print(NL.join([
            "weaknet device-off dry-run plan (no adb contact)",
            "  serial: %s" % (args.serial if args.serial else "(auto-detect at start)"),
            "  planned adb commands:",
            "    %s" % wn.format_adb(args.serial, "%s -S OUTPUT" % IPT),
            "    %s" % wn.format_adb(args.serial, "%s -t nat -S OUTPUT" % IPT),
            "    then: delete each %s rule (-A to -D) and verify zero" % TAG,
            "    adb reverse --list, then --remove every listed tcp:N",
        ]))
        return 0
    adb = AdbClient(serial=args.serial)
    try:
        probe_root(adb)
        removed = clean_rules(adb)
        clean = verify_clean(adb)
        _ok, listing = _reverse(adb, ["reverse", "--list"])
        for token in sorted({part for line in listing.splitlines()
                             for part in line.split()
                             if part.startswith("tcp:")}):
            _reverse(adb, ["reverse", "--remove", token])
            print("[weaknet] reverse %s removed" % token)
    except (RootUnavailableError, ApplyError, AdbError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    print("[weaknet] device-off: %d tagged rules removed" % len(removed))
    if not clean:
        print("WARNING: %s rules remain after device-off" % TAG, file=sys.stderr)
        return 1
    print("[weaknet] no %s rules remain" % TAG)
    return 0


def cmd_device_status(args: argparse.Namespace) -> int:
    if args.dry_run:
        print(NL.join([
            "weaknet device-status dry-run plan (no adb contact)",
            "  serial: %s" % (args.serial if args.serial else "(auto-detect at start)"),
            "  planned adb commands:",
            "    %s (root probe)" % wn.format_adb(args.serial, "su -c id"),
            "    %s" % wn.format_adb(args.serial, "%s -S OUTPUT" % IPT),
            "    %s" % wn.format_adb(args.serial, "%s -t nat -S OUTPUT" % IPT),
            "    %s (reverse mappings)" % wn.format_adb(args.serial, "reverse --list"),
        ]))
        return 0
    adb = AdbClient(serial=args.serial)
    try:
        try:
            probe_root(adb)
            rooted = True
        except RootUnavailableError:
            rooted = False
        rules, nat_rules = list_tagged_rules(adb)
        _ok, listing = _reverse(adb, ["reverse", "--list"])
    except AdbError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    print(NL.join(
        ["weaknet device-status",
         "  root: %s" % ("yes" if rooted else "NO (rules unmanageable)"),
         "  tagged filter rules: %d" % len(rules)]
        + ["    %s" % line for line in rules]
        + ["  tagged nat rules: %d" % len(nat_rules)]
        + ["    %s" % line for line in nat_rules]
        + ["  reverse mappings:"]
        + ["    %s" % line.strip() for line in listing.splitlines() if line.strip()]))
    return 0


def register_subparsers(sub) -> None:
    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--serial", default=None,
                        help="device serial (auto-detects single device)")
        sp.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit (no adb calls)")

    p_on = sub.add_parser("device-on", help="UID-scoped device shaping (rooted device)")
    p_on.add_argument("--package", required=True,
                      help="target app package (uid resolved via dumpsys)")
    add_common(p_on)
    p_on.add_argument("--profile", default=wn.DEFAULT_PROFILE,
                      help="edge|3g|lossy|wifi|custom (default %s)" % wn.DEFAULT_PROFILE)
    p_on.add_argument("--latency-ms", type=int, default=None)
    p_on.add_argument("--jitter-ms", type=int, default=None)
    p_on.add_argument("--loss-pct", type=float, default=None)
    p_on.add_argument("--bandwidth-kbps", type=float, default=None)
    p_on.add_argument("--ports", default="80,443",
                      help="TCP ports redirected to the proxy (comma list)")
    p_on.add_argument("--allow-quic", action="store_true",
                      help="omit the UDP/443 DROP rule (default: drop QUIC)")
    p_on.add_argument("--bind-host", default=wn.DEFAULT_BIND_HOST,
                      help="proxy bind interface (default %s)" % wn.DEFAULT_BIND_HOST)
    p_on.add_argument("--port", type=int, default=0,
                      help="proxy port; 0 = pick a free port")
    p_on.add_argument("--uid", type=int, default=None,
                      help="explicit app uid (skips dumpsys resolution)")
    p_off = sub.add_parser("device-off",
                           help="remove all %s rules + reverse mappings" % TAG)
    add_common(p_off)
    p_status = sub.add_parser("device-status",
                              help="show root state, tagged rules, reverses")
    add_common(p_status)


def run_cmd(args: argparse.Namespace) -> int:
    if args.cmd == "device-on":
        return cmd_device_on(args)
    if args.cmd == "device-off":
        return cmd_device_off(args)
    return cmd_device_status(args)


if __name__ == "__main__":
    sys.exit(wn.main())
