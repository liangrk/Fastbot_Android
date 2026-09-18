#!/usr/bin/env python3
"""fastbot_run.py — Fastbot 单设备跑/等/采集原语 (Phase-2 auto-agent Component 2).

Runs Fastbot on one device via adb (the README CLASSPATH app_process run
command), waits to completion (timeout + grace), then collects the
crash/coverage/logcat artifacts. Core primitive of the auto-agent closed
loop (启动 → 等待 → 采集).

Steps (order IS the contract):
  1. UNCONDITIONALLY wipe /sdcard/crash-dump.log (H1: the file is
     append-across-runs — Monkey.java:1756 opens FileWriter in append
     mode — so without this pre-launch wipe every crash report built
     from this run is polluted by prior runs).
  2. optional `adb push <max-config> /sdcard/max.config`
  3. optional wipe of /sdcard/max.xpath.actions (--clean-actions)
  4. launch: CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:
     /sdcard/fastbot-thirdpart.jar exec app_process /system/bin
     com.android.commands.monkey.Monkey -p PKG --agent reuseq
     --running-minutes M --throttle T -v -v, stdout streamed to
     <out>/fastbot.log
  5. pull /sdcard/crash-dump.log + /sdcard/fastbot_coverage/<pkg>.json +
     `logcat -d` -> <out>/logcat.txt; --collect-all adds fastbot_perf/,
     fastbot_privacy/audit.jsonl, fastbot_chaos.snapshot

Timeout semantics (honest): the local wait is minutes*60 + grace_sec on
the adb shell subprocess. On expiry only the LOCAL adb client process is
killed; the device-side Monkey is NOT forcibly stopped — it self-exits
when --running-minutes elapses. Until that device-side self-exit the
device keeps testing; do not relaunch on the same device before it ends.

Artifacts land in --out (default tools/out/fastbot_run).

Exit codes: 0 completed (时限模式含 rc=注入事件数) / 1 fastbot failed or timeout / 2 usage.
--dry-run prints the launch + collect plan with ZERO device contact
(the dry-run branch runs before any adb call, including step 1).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from common.adb import AdbClient, AdbError

DEFAULT_MINUTES = 30
DEFAULT_THROTTLE_MS = 100
DEFAULT_GRACE_SEC = 30
ADB_TIMEOUT_SEC = 30

CLASSPATH_JARS = ("monkeyq.jar", "framework.jar", "fastbot-thirdpart.jar")
CRASH_DUMP_PATH = "/sdcard/crash-dump.log"
XPATH_ACTIONS_PATH = "/sdcard/max.xpath.actions"
DEVICE_MAX_CONFIG = "/sdcard/max.config"
COVERAGE_DIR = "/sdcard/fastbot_coverage"
PERF_DIR = "/sdcard/fastbot_perf"
PRIVACY_AUDIT = "/sdcard/fastbot_privacy/audit.jsonl"
CHAOS_SNAPSHOT = "/sdcard/fastbot_chaos.snapshot"
CRASH_DUMP_RM = "rm -f " + CRASH_DUMP_PATH
XPATH_ACTIONS_RM = "rm -f " + XPATH_ACTIONS_PATH

TOOLS_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = TOOLS_DIR / "out" / "fastbot_run"


@dataclass
class RunResult:
    """One fastbot_run invocation: status in {completed, failed, timeout}."""

    status: str
    rc: Optional[int]
    collected: List[Path] = field(default_factory=list)


def build_run_command(package: str, minutes: int, throttle: int) -> List[str]:
    """The README run command as an argv list (pure; no device contact)."""
    classpath = ":".join("/sdcard/" + name for name in CLASSPATH_JARS)
    return [
        "CLASSPATH=%s" % classpath,
        "exec", "app_process", "/system/bin",
        "com.android.commands.monkey.Monkey",
        "-p", package,
        "--agent", "reuseq",
        "--running-minutes", str(int(minutes)),
        "--throttle", str(int(throttle)),
        "-v", "-v",
    ]


def artifact_pulls(package: str, collect_all: bool) -> List[Tuple[str, str]]:
    """(remote path, out-dir-relative local path) pairs, in pull order."""
    pulls = [(CRASH_DUMP_PATH, "crash-dump.log")]
    if package:
        pulls.append((
            "%s/%s.json" % (COVERAGE_DIR, package),
            "fastbot_coverage/%s.json" % package))
    if collect_all:
        pulls.append((PERF_DIR, "fastbot_perf"))
        pulls.append((PRIVACY_AUDIT, "fastbot_privacy/audit.jsonl"))
        pulls.append((CHAOS_SNAPSHOT, "fastbot_chaos.snapshot"))
    return pulls


def collect_artifacts(adb: AdbClient, package: str, out_dir,
                      collect_all: bool = False) -> List[Path]:
    """Best-effort step-5 collection. Pull failures warn and skip —
    never fatal (a crash run must still yield its other artifacts)."""
    collected: List[Path] = []
    out = Path(out_dir)
    for remote, rel in artifact_pulls(package, collect_all):
        local = out / rel
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            adb.pull(remote, str(local))
            collected.append(local)
        except (AdbError, OSError) as error:
            print("WARNING: pull 失败 %s: %.160s" % (remote, error),
                  file=sys.stderr)
    try:
        rc, text, _err = adb.shell("logcat -d")
        if rc == 0 and text.strip():
            logcat = out / "logcat.txt"
            logcat.write_text(text, encoding="utf-8", errors="replace")
            collected.append(logcat)
        else:
            print("WARNING: logcat -d rc=%s 或输出为空, logcat.txt 未采集" % rc,
                  file=sys.stderr)
    except (AdbError, OSError) as error:
        print("WARNING: logcat 采集失败: %.160s" % error, file=sys.stderr)
    return collected


def _run_with_timeout(argv: Sequence[str], timeout_sec: float,
                      log_path) -> Optional[int]:
    """Run argv locally, streaming stdout+stderr into log_path, bounded by
    timeout_sec. On expiry subprocess.run kills the LOCAL adb client
    process only and raises TimeoutExpired (-> returns None); the
    device-side Monkey self-exits at --running-minutes."""
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(
                list(argv), stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout_sec)
            return proc.returncode
        except subprocess.TimeoutExpired:
            return None


def _launch_argv(adb: AdbClient, command: Sequence[str]) -> List[str]:
    argv = [adb.adb_path]
    if adb.serial:
        argv += ["-s", adb.serial]
    return argv + ["shell"] + list(command)


_DONE_MARK = "Events injected:"
_CRASH_MARKS = ("// App appears", "RemoteException while injecting",
                "Monkey aborted", "FATAL EXCEPTION")


def _completed_time_based(log_path: Path) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if _DONE_MARK not in text:
        return False
    return not any(mark in text for mark in _CRASH_MARKS)


def run_fastbot(opts, adb: Optional[AdbClient] = None) -> RunResult:
    """Steps 1-5 in order. Steps 1-3+5 go through the injectable AdbClient;
    step 4 is a local subprocess bounded by minutes*60 + grace_sec.
    Collection (step 5) runs regardless of the launch outcome."""
    out_dir = Path(opts.out)
    adb = adb or AdbClient(serial=opts.serial, timeout=ADB_TIMEOUT_SEC,
                           retries=1)
    command = build_run_command(opts.package, opts.minutes, opts.throttle)
    print("fastbot_run: %s serial=%s minutes=%d throttle=%d -> %s"
          % (opts.package, adb.serial or "(auto)", opts.minutes,
             opts.throttle, out_dir))
    adb.shell(CRASH_DUMP_RM)
    print("① 清理 %s (H1: 跨 run 追加文件)" % CRASH_DUMP_PATH)
    if opts.max_config:
        adb.push(str(opts.max_config), DEVICE_MAX_CONFIG)
        print("② 推送 %s -> %s" % (opts.max_config, DEVICE_MAX_CONFIG))
    if opts.clean_actions:
        adb.shell(XPATH_ACTIONS_RM)
        print("③ 清理 %s" % XPATH_ACTIONS_PATH)
    timeout_sec = opts.minutes * 60 + opts.grace_sec
    print("④ 启动 Fastbot, 本地等待至多 %d s ..." % timeout_sec)
    rc = _run_with_timeout(_launch_argv(adb, command), timeout_sec,
                           out_dir / "fastbot.log")
    if rc is None:
        status = "timeout"
        print("本地等待超时: 已杀本地 adb 客户端; "
              "设备侧 Monkey 将在 --running-minutes 到点后自行退出")
    elif rc == 0:
        status = "completed"
        print("Fastbot 正常退出 rc=0")
    elif rc > 0 and _completed_time_based(out_dir / "fastbot.log"):
        # Monkey.run: --running-minutes 到点后 runMonkeyCycles 返回注入事件数,
        # 该值 < mCount-1 时进程以 rc=事件数 退出 —— 属正常完成而非失败
        status = "completed"
        print("Fastbot 时限模式正常结束 rc=%s (=注入事件数)" % rc)
    else:
        status = "failed"
        print("Fastbot 非零退出 rc=%s (详见 %s)" % (rc, out_dir / "fastbot.log"))
    collected = collect_artifacts(adb, opts.package, out_dir,
                                  opts.collect_all)
    print("⑤ 采集 %d 个产物" % len(collected))
    for path in collected:
        print("    %s" % path)
    return RunResult(status=status, rc=rc, collected=collected)


def print_dry_run(opts) -> None:
    out_dir = Path(opts.out)
    timeout_sec = opts.minutes * 60 + opts.grace_sec
    print("fastbot_run dry-run 执行计划 (零设备接触)")
    print("serial: %s" % (opts.serial or "(缺省: 自动检测单设备)"))
    print("package: %s  minutes: %d  throttle: %d  grace: %ds (本地等待 %d s)"
          % (opts.package, opts.minutes, opts.throttle, opts.grace_sec,
             timeout_sec))
    print("out: %s" % out_dir)
    print("① %s   (H1: 跨 run 追加文件, 必须无条件先清)" % CRASH_DUMP_RM)
    if opts.max_config:
        print("② adb push %s %s" % (opts.max_config, DEVICE_MAX_CONFIG))
    if opts.clean_actions:
        print("③ %s   (--clean-actions)" % XPATH_ACTIONS_RM)
    print("④ 执行命令:")
    print("    " + " ".join(build_run_command(
        opts.package, opts.minutes, opts.throttle)))
    print("    输出流 -> %s (超时仅杀本地 adb 客户端)"
          % (out_dir / "fastbot.log"))
    print("⑤ 采集:")
    for remote, rel in artifact_pulls(opts.package, opts.collect_all):
        print("    - %s -> %s" % (remote, out_dir / rel))
    print("    - logcat -d -> %s" % (out_dir / "logcat.txt"))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fastbot_run.py",
        description="Fastbot 单设备跑/等/采集原语 (auto-agent Component 2).",
    )
    parser.add_argument("--package", required=True, help="被测包名 (-p)")
    parser.add_argument("--serial", default=None,
                        help="设备 serial (缺省自动检测单设备)")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES,
                        help="--running-minutes (default 30)")
    parser.add_argument("--throttle", type=int, default=DEFAULT_THROTTLE_MS,
                        help="monkey --throttle ms (default 100)")
    parser.add_argument("--max-config", default=None,
                        help="推送为 /sdcard/max.config 的本地文件")
    parser.add_argument("--clean-actions", action="store_true",
                        help="启动前删除 /sdcard/max.xpath.actions")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="产物目录 (default tools/out/fastbot_run)")
    parser.add_argument("--grace-sec", type=int, default=DEFAULT_GRACE_SEC,
                        help="超时宽限秒 (default 30)")
    parser.add_argument("--collect-all", action="store_true",
                        help="附加采集 fastbot_perf/ fastbot_privacy/audit.jsonl"
                             " fastbot_chaos.snapshot")
    parser.add_argument("--dry-run", action="store_true",
                        help="打印执行计划, 零设备接触")
    return parser


_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_-]+)+$")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not _PACKAGE_RE.match(args.package):
        print("ERROR: --package looks invalid (expect com.example.app): %r"
              % args.package, file=sys.stderr)
        return 2
    if args.minutes <= 0:
        print("ERROR: --minutes must be a positive integer, got %d"
              % args.minutes, file=sys.stderr)
        return 2
    if args.throttle < 0 or args.grace_sec < 0:
        print("ERROR: --throttle 与 --grace-sec 必须 >= 0", file=sys.stderr)
        return 2
    if args.dry_run:
        print_dry_run(args)
        return 0
    if args.max_config and not Path(args.max_config).is_file():
        print("ERROR: --max-config 文件不存在: %s" % args.max_config,
              file=sys.stderr)
        return 2
    Path(args.out).mkdir(parents=True, exist_ok=True)
    try:
        result = run_fastbot(args)
    except AdbError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    if result.status == "completed":
        print("RESULT: completed")
        return 0
    print("RESULT: %s (rc=%s)" % (result.status, result.rc))
    return 1


if __name__ == "__main__":
    sys.exit(main())
