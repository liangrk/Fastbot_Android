#!/usr/bin/env python3
"""PC-side weak-network shaping proxy for Fastbot runs (no root required).

Honest limitation (read before trusting numbers):
    Shaping only affects device traffic that honors the Android system HTTP
    proxy, i.e. HTTP and HTTPS carried as CONNECT tunneling or absolute-URI
    requests. Apps using raw native sockets, QUIC/HTTP3, or HTTP libraries
    configured to bypass the system proxy are NOT shaped. Watch the printed
    connection counters to confirm traffic actually flows through here.

How it works:
    1. An asyncio proxy is bound on the PC (default 127.0.0.1:8123).
    2. The device global http_proxy is pointed at the PC interface the device
       can reach:  adb shell settings put global http_proxy <pc-ip>:<port>
    3. Each proxied connection is shaped: latency+jitter delay before the
       upstream connect, a loss roll (drop = close immediately so the app
       sees a network failure), and per-connection bandwidth throttling via
       a token bucket applied per chunk.
    4. Restore guarantee: the device proxy is cleared
       (settings put global http_proxy :0) in a finally block on normal
       exit, error, or Ctrl+C. The `stop` subcommand is the manual recovery
       path if a previous run died hard.

Usage:
    python tools/weaknet.py start --pc-ip 192.168.1.10 [--profile 3g]
        [--serial SER] [--port 8123] [--duration-sec 0] [--dry-run]
    python tools/weaknet.py stop [--serial SER] [--dry-run]
    python tools/weaknet.py profiles

Exit codes: 0 = success, 1 = runtime/adb failure, 2 = usage/validation error.
Stdlib only (asyncio/socket/argparse/random/time).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import socket
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple, Union

from common.adb import AdbClient, AdbError

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8123
DEFAULT_PROFILE = "3g"
STATS_INTERVAL_SEC = 10.0
HEADER_TIMEOUT_SEC = 15.0
CHUNK_SIZE = 65536
CLEAR_PROXY_CMD = "settings put global http_proxy :0"
GET_PROXY_CMD = "settings get global http_proxy"
SET_PROXY_PREFIX = "settings put global http_proxy "
CLEARED_PROXY_VALUES = (":0", "", "null")
HEADER_END = b"\r\n\r\n"


# --------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------- #

class Profile:
    """Immutable-ish shaping knobs for one run."""

    def __init__(self, name: str, latency_ms: int, jitter_ms: int,
                 loss_pct: float, bandwidth_kbps: float) -> None:
        self.name = name
        self.latency_ms = int(latency_ms)
        self.jitter_ms = int(jitter_ms)
        self.loss_pct = float(loss_pct)
        self.bandwidth_kbps = float(bandwidth_kbps)

    def describe(self) -> str:
        return ("profile=%s latency_ms=%d jitter_ms=%d loss_pct=%g bandwidth_kbps=%g"
                % (self.name, self.latency_ms, self.jitter_ms,
                   self.loss_pct, self.bandwidth_kbps))


PRESETS: Dict[str, Profile] = {
    "edge": Profile("edge", 400, 100, 2.0, 50.0),
    "3g": Profile("3g", 200, 50, 1.5, 750.0),
    "lossy": Profile("lossy", 150, 50, 10.0, 0.0),
    "wifi": Profile("wifi", 0, 0, 0.0, 0.0),
}


def resolve_profile(name: str, latency_ms: Optional[int] = None,
                    jitter_ms: Optional[int] = None,
                    loss_pct: Optional[float] = None,
                    bandwidth_kbps: Optional[float] = None) -> Profile:
    """Resolve --profile plus optional explicit knob overrides.

    custom requires at least one explicit knob (otherwise nothing would be
    shaped, which is surely a mistake). Explicit knobs override preset
    values. Raises ValueError on unknown/invalid input (exit code 2).
    """
    knobs = (("latency", latency_ms), ("jitter", jitter_ms),
             ("loss", loss_pct), ("bandwidth", bandwidth_kbps))
    if latency_ms is not None and latency_ms < 0:
        raise ValueError("--latency-ms must be >= 0, got %r" % latency_ms)
    if jitter_ms is not None and jitter_ms < 0:
        raise ValueError("--jitter-ms must be >= 0, got %r" % jitter_ms)
    if loss_pct is not None and not 0.0 <= loss_pct <= 100.0:
        raise ValueError("--loss-pct must be in [0, 100], got %r" % loss_pct)
    if bandwidth_kbps is not None and bandwidth_kbps < 0:
        raise ValueError("--bandwidth-kbps must be >= 0, got %r" % bandwidth_kbps)
    if name == "custom":
        if all(value is None for _label, value in knobs):
            raise ValueError(
                "--profile custom requires at least one explicit knob "
                "(--latency-ms/--jitter-ms/--loss-pct/--bandwidth-kbps)")
        return Profile("custom",
                       latency_ms if latency_ms is not None else 0,
                       jitter_ms if jitter_ms is not None else 0,
                       loss_pct if loss_pct is not None else 0.0,
                       bandwidth_kbps if bandwidth_kbps is not None else 0.0)
    if name not in PRESETS:
        raise ValueError("unknown profile %r (known: %s, custom)"
                         % (name, ", ".join(sorted(PRESETS))))
    base = PRESETS[name]
    return Profile(base.name,
                   latency_ms if latency_ms is not None else base.latency_ms,
                   jitter_ms if jitter_ms is not None else base.jitter_ms,
                   loss_pct if loss_pct is not None else base.loss_pct,
                   bandwidth_kbps if bandwidth_kbps is not None else base.bandwidth_kbps)


# --------------------------------------------------------------------- #
# Shaping decisions (pure functions, clock/rand injected for tests)
# --------------------------------------------------------------------- #

def compute_delay_ms(latency_ms: int, jitter_ms: int, rng: random.Random) -> int:
    """Base latency plus uniform 0..jitter milliseconds."""
    extra = rng.randint(0, int(jitter_ms)) if jitter_ms > 0 else 0
    return int(latency_ms) + extra


def should_drop_connection(loss_pct: float, rng: random.Random) -> bool:
    """True when this connection must be dropped (close = app sees failure)."""
    if loss_pct <= 0:
        return False
    return rng.random() * 100.0 < float(loss_pct)


class TokenBucket:
    """Per-connection byte pacing token bucket.

    rate_kbps <= 0 disables throttling (every delay is 0). Capacity is one
    second worth of bytes; the bucket may go negative down to -capacity.
    The clock is injected so tests can advance time deterministically.
    """

    def __init__(self, rate_kbps: float, clock=time.monotonic) -> None:
        self.rate_bps = float(rate_kbps) * 1024.0 / 8.0
        self.capacity = self.rate_bps
        self.clock = clock
        self.tokens = self.capacity
        self.last = clock()

    def _refill(self, now: float) -> None:
        if self.rate_bps <= 0:
            return
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_bps)
        self.last = now

    def delay_for(self, n_bytes: int, now: Optional[float] = None) -> float:
        """Seconds to wait before releasing n_bytes (0 when allowed now)."""
        if self.rate_bps <= 0 or n_bytes <= 0:
            return 0.0
        if now is None:
            now = self.clock()
        self._refill(now)
        deficit = n_bytes - self.tokens
        if deficit <= 0:
            return 0.0
        return deficit / self.rate_bps

    def consume(self, n_bytes: int, now: Optional[float] = None) -> None:
        """Deduct n_bytes of tokens after the caller slept delay_for()."""
        if self.rate_bps <= 0 or n_bytes <= 0:
            return
        if now is None:
            now = self.clock()
        self._refill(now)
        self.tokens = max(-self.capacity, self.tokens - n_bytes)


# --------------------------------------------------------------------- #
# HTTP request-line / header parsing (pure, byte fixtures in tests)
# --------------------------------------------------------------------- #

def _split_authority(authority: str, default_port: int) -> Tuple[Optional[str], int]:
    """host[:port] -> (host, port); (None, default) when empty."""
    authority = authority.strip()
    if not authority:
        return None, default_port
    host, sep, port_text = authority.rpartition(":")
    if not sep:
        return authority, default_port
    try:
        port = int(port_text)
    except ValueError:
        return None, default_port
    if not 0 < port < 65536:
        return None, default_port
    return host, port


def parse_request_line(line: Union[bytes, bytearray, str]) -> Optional[Dict[str, object]]:
    """Parse the HTTP request first line.

    Returns one of:
      {"kind": "connect",  "host", "port", "version", "rewritten": None}
      {"kind": "absolute", "method", "host", "port", "path", "version",
       "rewritten": "METHOD <origin-form-path> HTTP/1.x"}
      {"kind": "origin",   "method", "path", "version", "rewritten": <line>}
      None for anything unparsable/unsupported (caller answers 400).
    Target host for origin-form requests comes from the Host header via
    resolve_target().
    """
    text = (line.decode("latin-1") if isinstance(line, (bytes, bytearray))
            else str(line)).strip()
    parts = text.split()
    if len(parts) != 3:
        return None
    method, target, version = parts
    if not version.upper().startswith("HTTP/1."):
        return None
    if method.upper() == "CONNECT":
        host, sep, port_text = target.rpartition(":")
        if not sep or not host:
            return None
        try:
            port = int(port_text)
        except ValueError:
            return None
        if not 0 < port < 65536:
            return None
        return {"kind": "connect", "host": host, "port": port,
                "version": version, "rewritten": None}
    if target.lower().startswith("http://"):
        rest = target[len("http://"):]
        slash = rest.find("/")
        if slash <= 0:
            return None
        host, port = _split_authority(rest[:slash], 80)
        if host is None:
            return None
        path = rest[slash:]
        rewritten = "%s %s %s" % (method, path, version)
        return {"kind": "absolute", "method": method.upper(), "host": host,
                "port": port, "path": path, "version": version,
                "rewritten": rewritten}
    if target.startswith("/"):
        return {"kind": "origin", "method": method.upper(), "path": target,
                "version": version, "rewritten": text}
    return None


def parse_headers(header_lines: Sequence[str]) -> Dict[str, str]:
    """Parse header lines (no CRLF, no request line) into a dict."""
    headers: Dict[str, str] = {}
    for line in header_lines:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


def resolve_target(parsed: Dict[str, object],
                   headers: Dict[str, str]) -> Tuple[Optional[str], int]:
    """Upstream (host, port) for absolute/origin requests."""
    if parsed["kind"] == "absolute":
        return parsed["host"], parsed["port"]  # type: ignore[return-value]
    host, port = _split_authority(headers.get("host", ""), 80)
    if not host:
        return None, port
    return host, port


# --------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------- #

class Counters:
    """Totals across all proxied connections."""

    def __init__(self) -> None:
        self.connections = 0
        self.dropped = 0
        self.errors = 0
        self.bytes_up = 0
        self.bytes_down = 0

    def total_bytes(self) -> int:
        return self.bytes_up + self.bytes_down

    def snapshot_line(self) -> str:
        return ("[weaknet] stats: conn=%d dropped=%d errors=%d bytes_up=%d "
                "bytes_down=%d bytes=%d"
                % (self.connections, self.dropped, self.errors,
                   self.bytes_up, self.bytes_down, self.total_bytes()))


# --------------------------------------------------------------------- #
# Proxy core
# --------------------------------------------------------------------- #

async def _respond_simple(writer: asyncio.StreamWriter, code: int,
                          reason: str) -> None:
    body = ("%d %s" % (code, reason)).encode("latin-1")
    head = ("HTTP/1.1 %d %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
            % (code, reason, len(body))).encode("latin-1")
    try:
        writer.write(head + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _bridge(client_reader: asyncio.StreamReader,
                  client_writer: asyncio.StreamWriter,
                  upstream_reader: asyncio.StreamReader,
                  upstream_writer: asyncio.StreamWriter,
                  profile: Profile, counters: Counters
                  ) -> Tuple[int, int]:
    """Full-duplex pipe with token-bucket throttling on every chunk.

    Returns (bytes_up, bytes_down). The bucket is per connection.
    """
    bucket = TokenBucket(profile.bandwidth_kbps)
    up_box = [0]
    down_box = [0]

    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
                   box: List[int], attr: str) -> None:
        try:
            while True:
                chunk = await src.read(CHUNK_SIZE)
                if not chunk:
                    break
                wait = bucket.delay_for(len(chunk))
                if wait > 0:
                    await asyncio.sleep(wait)
                bucket.consume(len(chunk))
                dst.write(chunk)
                await dst.drain()
                box[0] += len(chunk)
                setattr(counters, attr, getattr(counters, attr) + len(chunk))
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass

    task_up = asyncio.ensure_future(
        pump(client_reader, upstream_writer, up_box, "bytes_up"))
    task_down = asyncio.ensure_future(
        pump(upstream_reader, client_writer, down_box, "bytes_down"))
    try:
        done, pending = await asyncio.wait({task_up, task_down},
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        # First direction finished (EOF or error): close BOTH ends so EOF
        # propagates and the peer never hangs (half-close-safe for this tool).
        try:
            client_writer.close()
        except (ConnectionError, OSError):
            pass
        try:
            upstream_writer.close()
        except (ConnectionError, OSError):
            pass
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for task in (task_up, task_down):
            if not task.done():
                task.cancel()
    return up_box[0], down_box[0]


async def _open_upstream(host: str, port: int) -> Tuple[
        asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


async def _pipe_connect(client_reader: asyncio.StreamReader,
                        client_writer: asyncio.StreamWriter,
                        parsed: Dict[str, object], profile: Profile,
                        counters: Counters) -> Tuple[int, int]:
    try:
        upstream_reader, upstream_writer = await _open_upstream(
            str(parsed["host"]), int(parsed["port"]))  # type: ignore[arg-type]
    except (ConnectionError, OSError):
        counters.errors += 1
        await _respond_simple(client_writer, 502, "Bad Gateway")
        return 0, 0
    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await client_writer.drain()
    return await _bridge(client_reader, client_writer,
                         upstream_reader, upstream_writer, profile, counters)


async def _pipe_http(client_reader: asyncio.StreamReader,
                     client_writer: asyncio.StreamWriter,
                     host: str, port: int, request_bytes: bytes,
                     profile: Profile, counters: Counters) -> Tuple[int, int]:
    try:
        upstream_reader, upstream_writer = await _open_upstream(host, port)
    except (ConnectionError, OSError):
        counters.errors += 1
        await _respond_simple(client_writer, 502, "Bad Gateway")
        return 0, 0
    upstream_writer.write(request_bytes)
    await upstream_writer.drain()
    return await _bridge(client_reader, client_writer,
                         upstream_reader, upstream_writer, profile, counters)


class WeakNetProxy:
    """Shaping HTTP/CONNECT proxy bound on the PC (in-process testable)."""

    def __init__(self, bind_host: str, port: int, profile: Profile,
                 rng: Optional[random.Random] = None,
                 stats_interval: float = STATS_INTERVAL_SEC) -> None:
        self.bind_host = bind_host
        self.port = port
        self.profile = profile
        self.rng = rng if rng is not None else random.Random()
        self.stats_interval = stats_interval
        self.counters = Counters()
        self._server: Optional[asyncio.AbstractServer] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._handler_tasks = set()
        self._conn_seq = 0

    async def start(self) -> None:
        async def tracked(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            self._handler_tasks.add(task)
            try:
                await self._handle_client(reader, writer)
            finally:
                self._handler_tasks.discard(task)

        self._server = await asyncio.start_server(
            tracked, self.bind_host, self.port)
        self._stats_task = asyncio.ensure_future(self._stats_loop())

    async def close(self) -> None:
        if self._stats_task is not None:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stats_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._handler_tasks:
            for task in self._handler_tasks:
                task.cancel()
            await asyncio.gather(*self._handler_tasks,
                                 return_exceptions=True)
        print(self.counters.snapshot_line(), flush=True)

    def bound_port(self) -> int:
        sockets = self._server.sockets if self._server is not None else []
        if sockets:
            return sockets[0].getsockname()[1]
        return self.port

    async def _stats_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.stats_interval)
                print(self.counters.snapshot_line(), flush=True)
        except asyncio.CancelledError:
            pass

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self._conn_seq += 1
        conn_id = self._conn_seq
        self.counters.connections += 1
        peer = "?"
        peername = writer.get_extra_info("peername")
        if peername:
            peer = "%s:%s" % (peername[0], peername[1])
        up = down = 0
        dropped = False
        try:
            delay_ms = compute_delay_ms(
                self.profile.latency_ms, self.profile.jitter_ms, self.rng)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)
            if should_drop_connection(self.profile.loss_pct, self.rng):
                dropped = True
                self.counters.dropped += 1
                return  # finally closes: the app sees a network failure
            head = await asyncio.wait_for(
                reader.readuntil(HEADER_END), timeout=HEADER_TIMEOUT_SEC)
            head_lines = head.decode("latin-1").split("\r\n")[:-1]
            if not head_lines:
                return
            parsed = parse_request_line(head_lines[0])
            if parsed is None:
                self.counters.errors += 1
                await _respond_simple(writer, 400, "Bad Request")
                return
            headers = parse_headers(head_lines[1:])
            if parsed["kind"] == "connect":
                up, down = await _pipe_connect(
                    reader, writer, parsed, self.profile, self.counters)
                return
            host, port = resolve_target(parsed, headers)
            if not host:
                self.counters.errors += 1
                await _respond_simple(writer, 400, "Bad Request (missing Host)")
                return
            forward_lines = [str(parsed["rewritten"])] + head_lines[1:]
            if "host" not in headers:
                host_header = str(host)
                if port != 80:
                    host_header = "%s:%d" % (host_header, port)
                forward_lines.append("Host: " + host_header)
            request_bytes = ("\r\n".join(forward_lines) + "\r\n\r\n").encode("latin-1")
            up, down = await _pipe_http(
                reader, writer, str(host), port, request_bytes,
                self.profile, self.counters)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, ConnectionError, OSError):
            self.counters.errors += 1
        finally:
            self.counters.bytes_up += up
            self.counters.bytes_down += down
            print("[weaknet] conn #%d %s %s up=%d down=%d"
                  % (conn_id, peer, "DROPPED" if dropped else "closed",
                     up, down), flush=True)
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass


# --------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------- #

def detect_pc_ip() -> Optional[str]:
    """Best-effort PC interface IP via a UDP connect (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # canonical probe target; nothing is sent
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def format_adb(serial: Optional[str], shell_cmd: str) -> str:
    prefix = "adb"
    if serial:
        prefix += " -s " + serial
    return prefix + " shell " + shell_cmd


def clear_device_proxy(adb: AdbClient) -> None:
    """Restore guarantee: clear + verify. Called from finally blocks."""
    try:
        adb.shell(CLEAR_PROXY_CMD)
        _rc, out, _err = adb.shell(GET_PROXY_CMD)
        value = out.strip()
        if value in CLEARED_PROXY_VALUES:
            print("[weaknet] device proxy cleared (http_proxy=%r)" % value,
                  flush=True)
        else:
            print("WARNING: device http_proxy=%r after clear; manual recovery:"
                  " python tools/weaknet.py stop" % value, file=sys.stderr)
    except AdbError as error:
        print("ERROR: could not clear device proxy: %s (manual: adb shell %s)"
              % (error, CLEAR_PROXY_CMD), file=sys.stderr)


def print_start_plan(args: argparse.Namespace, profile: Profile) -> None:
    endpoint = "%s:%d" % (args.pc_ip if args.pc_ip else "<auto-pc-ip>",
                          args.port)
    print("weaknet start dry-run plan (no adb contact, no sockets bound)")
    print("  serial: %s" % (args.serial if args.serial
                            else "(auto-detect at start)"))
    print("  profile: %s" % profile.describe())
    print("  proxy bind: %s:%d" % (args.bind_host, args.port))
    print("  device http_proxy target: %s" % endpoint)
    if not args.pc_ip:
        print("  note: --pc-ip omitted; at start the PC IP is auto-detected "
              "via a UDP connect trick (best-effort, override with --pc-ip)")
    print("  duration_sec: %g%s" % (
        args.duration_sec,
        " (run until Ctrl+C)" if args.duration_sec == 0 else ""))
    print("  limitation: only traffic honoring the Android system http_proxy "
          "is shaped (HTTP/HTTPS CONNECT or absolute-URI); native sockets "
          "and QUIC bypass it")
    print("  planned adb commands:")
    print("    [set]   %s" % format_adb(args.serial, SET_PROXY_PREFIX + endpoint))
    print("    [clear] %s" % format_adb(args.serial, CLEAR_PROXY_CMD))
    print("    [get]   %s" % format_adb(args.serial, GET_PROXY_CMD))
    print("  restore guarantee: clear runs in a finally block (Ctrl+C "
          "included); manual recovery: python tools/weaknet.py stop%s"
          % (" --serial " + args.serial if args.serial else ""))


async def _amain_start(args: argparse.Namespace, profile: Profile,
                       adb: AdbClient, pc_ip: str,
                       flags: Dict[str, bool]) -> int:
    proxy = WeakNetProxy(args.bind_host, args.port, profile)
    await proxy.start()
    bound = proxy.bound_port()
    try:
        print("[weaknet] proxy listening on %s:%d" % (args.bind_host, bound),
              flush=True)
        adb.shell(SET_PROXY_PREFIX + "%s:%d" % (pc_ip, bound))
        flags["set"] = True
        print("[weaknet] device http_proxy set to %s:%d" % (pc_ip, bound),
              flush=True)
        print("[weaknet] shaping... press Ctrl+C to stop (restore is "
              "guaranteed)", flush=True)
        print(proxy.counters.snapshot_line(), flush=True)
        if args.duration_sec > 0:
            await asyncio.sleep(args.duration_sec)
        else:
            await asyncio.Event().wait()  # until cancelled (Ctrl+C)
    finally:
        await proxy.close()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    if not 0 <= args.port <= 65535:
        print("ERROR: --port must be in [0, 65535], got %r" % args.port,
              file=sys.stderr)
        return 2
    try:
        profile = resolve_profile(args.profile, args.latency_ms,
                                  args.jitter_ms, args.loss_pct,
                                  args.bandwidth_kbps)
    except ValueError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2
    if args.dry_run:
        print_start_plan(args, profile)
        return 0
    pc_ip = args.pc_ip
    if not pc_ip:
        pc_ip = detect_pc_ip()
        if not pc_ip:
            print("ERROR: could not auto-detect the PC IP; pass --pc-ip",
                  file=sys.stderr)
            return 2
        print("[weaknet] pc-ip auto-detected: %s (override with --pc-ip)"
              % pc_ip, flush=True)
    print("[weaknet] profile: %s" % profile.describe(), flush=True)
    adb = AdbClient(serial=args.serial)
    flags = {"set": False}
    try:
        try:
            return asyncio.run(
                _amain_start(args, profile, adb, pc_ip, flags))
        except KeyboardInterrupt:
            print("[weaknet] interrupted", flush=True)
            return 130
        finally:
            if flags["set"]:
                clear_device_proxy(adb)
    except AdbError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1


def cmd_stop(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("weaknet stop dry-run plan (no adb contact)")
        print("  planned adb commands:")
        print("    [clear] %s" % format_adb(args.serial, CLEAR_PROXY_CMD))
        print("    [get]   %s" % format_adb(args.serial, GET_PROXY_CMD))
        return 0
    adb = AdbClient(serial=args.serial)
    try:
        adb.shell(CLEAR_PROXY_CMD)
        _rc, out, _err = adb.shell(GET_PROXY_CMD)
    except AdbError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    value = out.strip()
    if value in CLEARED_PROXY_VALUES:
        print("[weaknet] device http_proxy cleared (value=%r)" % value)
        return 0
    print("WARNING: device http_proxy still %r after clear" % value,
          file=sys.stderr)
    return 1


def cmd_profiles(_args: argparse.Namespace) -> int:
    print("weaknet shaping profiles (knobs: latency/jitter/loss/bandwidth)")
    print("  %-8s %11s %11s %9s %15s"
          % ("name", "latency_ms", "jitter_ms", "loss_pct", "bandwidth_kbps"))
    for name in sorted(PRESETS):
        profile = PRESETS[name]
        print("  %-8s %11d %11d %9g %15g"
              % (name, profile.latency_ms, profile.jitter_ms,
                 profile.loss_pct, profile.bandwidth_kbps))
    print("  %-8s %11s %11s %9s %15s"
          % ("custom", "user", "user", "user", "user"))
    print("  notes:")
    print("    - custom requires at least one explicit knob "
          "(--latency-ms/--jitter-ms/--loss-pct/--bandwidth-kbps)")
    print("    - explicit knobs override preset values")
    print("    - loss drop = immediate close, so the app sees a network "
          "failure")
    print("    - bandwidth is a per-connection token bucket; 0 = unlimited")
    print("    - only proxy-honoring traffic (HTTP/HTTPS via the system "
          "http_proxy) is shaped; native sockets are NOT shaped")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weaknet.py",
        description="PC-side weak-network shaping proxy for Fastbot "
                    "(Android system http_proxy, no root).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("--serial", default=None,
                                help="device serial (auto-detects single device)")
        sub_parser.add_argument("--dry-run", action="store_true",
                                help="print the plan and exit (no adb calls, "
                                     "no sockets bound)")

    p_start = sub.add_parser(
        "start", help="bind the shaping proxy and point the device at it")
    add_common(p_start)
    p_start.add_argument("--profile", default=DEFAULT_PROFILE,
                         help="edge|3g|lossy|wifi|custom (default %s)"
                              % DEFAULT_PROFILE)
    p_start.add_argument("--latency-ms", type=int, default=None,
                         help="delay before first byte of each connection (ms)")
    p_start.add_argument("--jitter-ms", type=int, default=None,
                         help="extra random 0..jitter per connection (ms)")
    p_start.add_argument("--loss-pct", type=float, default=None,
                         help="connection drop probability 0..100")
    p_start.add_argument("--bandwidth-kbps", type=float, default=None,
                         help="per-connection throttle; 0 = unlimited")
    p_start.add_argument("--pc-ip", default=None,
                         help="PC IP reachable from the device "
                              "(auto-detects when omitted)")
    p_start.add_argument("--bind-host", default=DEFAULT_BIND_HOST,
                         help="proxy bind interface (default %s)"
                              % DEFAULT_BIND_HOST)
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT,
                         help="proxy port (default %d)" % DEFAULT_PORT)
    p_start.add_argument("--duration-sec", type=float, default=0.0,
                         help="run duration; 0 = until Ctrl+C")

    p_stop = sub.add_parser(
        "stop", help="clear the device http_proxy (manual recovery)")
    add_common(p_stop)

    p_profiles = sub.add_parser("profiles", help="print the preset table")
    add_common(p_profiles)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    return cmd_profiles(args)


if __name__ == "__main__":
    sys.exit(main())
