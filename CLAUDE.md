# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fastbot_Android is ByteDance's open-source model-based GUI stability testing tool for Android. It is an enhanced Monkey: a Java client executes actions on the device while a C++ native layer uses reinforcement learning (sarsa n-step) over an Activity Transition Graph to pick the highest-reward next action. A knowledge graph of this codebase is in `graphify-out/` (query via `graphify query "<question>"`).

## Build Commands

Requires JDK 8, Gradle 7.6.2 (via sdkman), NDK 25.2.9519653, CMake 3.18.1, Android SDK (compileSdk 32, minSdk 22).

```shell
# Build Java layer (produces monkey/build/libs/monkey.jar)
./gradlew clean makeJar

# Convert to dex jar (the runnable Fastbot artifact; path varies by SDK location)
~/Library/Android/sdk/build-tools/28.0.3/dx --dex --output=monkeyq.jar monkey/build/libs/monkey.jar

# Build native layer (all 4 ABIs; requires $NDK_ROOT set; outputs .so into libs/<ABI>/)
sh ./build_native.sh
```

There are no unit tests. Testing happens on-device: push artifacts (`monkeyq.jar`, `fastbot-thirdpart.jar`, `framework.jar` to `/sdcard/`, `libs/*` to `/data/local/tmp/`) and run via `adb shell CLASSPATH=... exec app_process /system/bin com.android.commands.monkey.Monkey -p <package> --agent reuseq --running-minutes N --throttle N -v -v`. See `handbook-cn.md` for the full manual and `README.md` for the run command.

## Architecture

Two layers that communicate over JNI, exchanging XML (device → native) and JSON (native → device):

```
Java (monkey/)                          C++ (native/)
Monkey.runMonkeyCycles (main loop)      fastbot_native.cpp (JNI impl,
  └─ MonkeySourceApeNative               core decision in b0bhkadf)
      └─ AiClient ──JNI──►  Model.getOperateOpt(xml, activity, deviceId)
  ◄──JSON Operate──────    └─ Agent (AbstractAgent → ModelReusableAgent,
  └─ execute action             sarsa n-step, model reuse .fbm)
TreeBuilder: AccessibilityNodeInfo → XML     └─ Graph (Activity Transition Graph:
                                              State nodes, Action edges)
```

Key data structures: `Action` (typed model action) → `Operate` (device-executable wrapper, JSON-serialized by `DeviceOperateWrapper`) → Java executes it.

### Java layer (`monkey/src/main/java/`)

- `com.android.commands.monkey.Monkey` — entry point; `processOptions` (CLI args), `runMonkeyCycles` (main loop)
- `source/` — event sources; `MonkeySourceApeNative` is the Fastbot source that drives decisions
- `bytedance/fastbot/AiClient` — JNI method declarations talking to the native layer
- `tree/TreeBuilder` — dumps GUI tree as XML
- `action/`, `fastbot/client/` — Action classes; `ActionType`, `Operate` (JSON protocol types)
- `events/base/` — native Monkey events; `events/base/mutation/`, `events/customize/` — Fastbot config-driven custom events (expert system)
- `framework/` — Android system abstraction and OS-version compatibility
- `provider/` — `SchemaProvider`, `ShellProvider` (Schema Event / shell command support)
- `utils/` — config reading (`max.*` files from `/sdcard/`), logging, activity filtering

### Native layer (`native/`)

- `project/jni/fastbot_native.cpp` — JNI entry (`b0bhkadf` is the decision core); `dumpCoverage` exports widget-level coverage
- `model/` — `Model` (per-device agent orchestration, `getOperate`/`getOperateOpt`), `Graph` (state transition graph)
- `agent/` — `AbstractAgent` (decision base class) → `ModelReusableAgent` (sarsa n-step, .fbm model reuse)
- `desc/` — `Action`, `Element`, `Node`, `ActionFilter`, `DeviceOperateWrapper`; `desc/reuse/` — serialization for model reuse (`ReuseState`, `ActivityNameAction`, `RichWidget`)
- `storage/` — `CoverageExporter` (widget-quadruple accumulator + JSON emitter; decision-thread only — the Graph is lock-free, never read it from other threads)
- `thirdpart/` — tinyxml2, flatbuffers, json (vendored)
- Built via `monkey/build.gradle` `externalNativeBuild` (gradle) or standalone via `build_native.sh` (cmake)

## PC-Side Tooling (`tools/`)

Python 3.9+, minimal deps (`pip install -r tools/requirements.txt`: pytest + jsonschema). Run the PC-side suite: `python -m pytest tools/tests/ -q`. Every device-contacting CLI supports `--dry-run`. Product formats are frozen in `tools/schemas/` (perf_frame / perf_sample / coverage / audit / chaos_snapshot) — changing a schema is a contract change; update fixtures and consumers together.

- `matrix_runner.py` — ≤10-device orchestrator (failure-isolated subprocesses, generated max.config per device, aggregate Chinese HTML report)
- `perf_poller.py` / `perf_report.py` — 10s adb polling (cpuinfo/meminfo/netstats/battery `--checkin`); cold/warm start detection uses logcat `Displayed` lines as primary signal, `pidof` only as corroboration
- `coverage_diff.py` — widget-level coverage diff; identity priority resource-id > text > content-desc > path; `--determinism-check` requires empty diff for same-version runs
- `privacy_rules.py` / `privacy_report.py` — rule-file validation (same semantics as the device-side engine) and audit.jsonl → Chinese HTML report
- `chaos_validate.py` — max.config chaos-key validator
- `gui_export.py` / `push_config.py` / `coverage_compare.py` — external-agent closed loop (protocol: `tools/agent_protocol.md`; acceptance manual covering AC1–AC6: `tools/ACCEPTANCE.md`)
- `weaknet.py` — PC-side weak-network shaping proxy (latency/jitter/loss/bandwidth) via system http_proxy; apps ignoring the proxy are NOT shaped

## Default-Off Config Keys (device side)

All keys below default OFF/0 — an unconfigured run behaves exactly like stock Fastbot:

- `max.coverage.exportWidgetLevel` — widget-level coverage JSON to `/sdcard/fastbot_coverage/` (periodic gate in `MonkeySourceApeNative.generateEvents` + final export in the `Monkey.run` finally block)
- `max.chaos.enable`, `max.chaos.<state>.pct`, `max.chaos.maxConcurrent`, `max.chaos.timeoutSec` — 8 system-state chaos channels (`events/base/chaos/`); events enqueue via `addEvent` and flow the main-loop guard; snapshot/restore in `Monkey.run` finally
- `max.privacy.enabled`, `max.privacy.rules`, `max.privacy.defaultAction`, `max.privacy.screenshot` — privacy-popup rule engine + audit JSONL (`events/customize/Privacy*`), hooked between the XML dump and the native decision
- `max.perf.frame`, `max.perf.frameIntervalSec` — gfxinfo frame sampling to `/sdcard/fastbot_perf/` (`events/base/PerfFrameEvent.java`)

## Critical Build Quirks

- Java code compiles against the vendored `monkey/libs/framework.jar` (hidden Android APIs), prepended to the bootclasspath in both `build.gradle` files. Do not remove this; it means the code uses non-SDK interfaces not available in the public SDK.
- `fastbot-thirdpart.jar`, `framework.jar` (root), and `monkeyq.jar` in the repo root are prebuilt runtime artifacts, not source.
- `test/` contains the expert-system config files (`max.config`, `max.strings`, `max.xpath.actions`, `max.widget.black`, `max.tree.pruning`, `max.fuzzing.strings`, ...) that are pushed to `/sdcard/`; filenames are fixed and cannot be renamed. `data/fuzzing/` holds media fixtures pushed to the device for fuzzing.

## Extension Points

Documented in `fastbot_code_analysis.md` (bilingual):

- New decision algorithm: subclass `Model` (C++), add JNI bridge methods, add a Java event source subclassing `MonkeySourceApeNative`/`MonkeyEvent`, wire into `Monkey.java`
- New agent: subclass `AbstractAgent`, register in `AgentFactory`
- New custom event: subclass `AbstractCustomEvent` in `events/customize/` (config-driven via `max.xpath.actions`)
- New CLI option: `Monkey.processOptions` + handling in `run`/`runMonkeyCycle`
