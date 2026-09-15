# Fastbot_Android 六项自动化测试能力扩展 — 设计文档

> 需求规格见 `.omc/specs/deep-interview-fastbot-six-extensions.md`（13 轮深度访谈，歧义度 1.0%）。
> 构建顺序：chaos-injection → perf-metrics → coverage-diff → privacy-compliance → device-matrix → llm-exploration。
> 前置依赖：P0 Android 17 隐藏 API 兼容修复（commit 4c42a5d）已完成。

## 0. 总体架构

```
┌─ PC 侧（新增，tools/，Python 3.9+）─────────────────────────┐
│ matrix_runner.py ── 并行编排 ≤10 台设备，失败隔离，聚合报告      │
│ perf_poller.py ── adb 轮询 CPU/内存/流量/电量/启动检测（10s）    │
│ coverage_diff.py ── 控件级覆盖 JSON diff + 确定性自检           │
│ privacy_report.py ── 审计 JSONL → 中文 HTML                    │
│ gui_export.py / push_config.py ── 外部 Agent 闭环端点           │
│ common/ ── adb 封装、报告模板（中文 HTML）、config schema        │
└──────────────────────────────────────────────────────────┘
        │ adb push/pull/shell                          │ 外部 Agent
        ▼                                              ▼（Claude Code 等）
┌─ 设备侧（Fastbot 本体扩展）──────────────────────────────────┐
│ chaos: events/base/chaos/*（8 个新 Mutation Event，shell 注入） │
│ perf: 帧率/jank Event 扩展 → /sdcard/fastbot_perf/*.jsonl      │
│ coverage: native desc/reuse 序列化扩展 → fastbot_coverage.json │
│ privacy: 弹窗自动处理 + 审计（复用 customize/config 模式）       │
│ llm: 复用现有截图/XML 落盘开关供 gui_export 拉取                │
└──────────────────────────────────────────────────────────┘
```

原则：设备侧只做"采集与执行"（性能开销敏感），PC 侧做"编排/分析/报告"；所有降级路径复用 P0 引入的 `INJECT_FAIL` 事件丢弃语义；所有配置沿用 `max.*` 命名进 `max.config` 或独立配置文件（push 到 `/sdcard/`）。

## 1. chaos-injection（设备侧 Java）

**模块**：`monkey/src/main/java/com/android/commands/monkey/events/base/chaos/`
新增 8 个事件类，统一继承抽象基类 `AbstractChaosEvent`（新增，封装快照/恢复/降级骨架）：

| 事件类 | 状态 | shell 通道 |
|--------|------|-----------|
| ChaosBatteryEvent | 低电量/拔充电 | `dumpsys battery set level/unplug` |
| ChaosPowerSaveEvent | 省电模式 | `settings put global low_power_switcher` / `cmd power set-fixed-performance-mode` |
| ChaosBluetoothEvent | 蓝牙开关 | `svc bluetooth enable/disable` |
| ChaosLocationEvent | GPS/定位模式 | `settings put secure location_mode` |
| ChaosMobileDataEvent | 移动数据 | `svc data enable/disable` |
| ChaosVpnEvent | VPN 建立/断开 | 预置 profile + `cmd connectivity`（不可用则降级跳过） |
| ChaosDoNotDisturbEvent | 勿扰模式 | `cmd notification set_dnd on/off` |
| ChaosSystemConfigEvent | 深色模式/字体大小/语言（单次任务内只做安全子集：深色/字体） | `cmd uimode night yes/no`、`settings put system font_scale` |

**配置**（`max.config`，名称固定）：
```
max.chaos.enable = true
max.chaos.battery.pct = 0.02      # 每动作周期注入概率
max.chaos.bluetooth.pct = 0.01
...（每状态一个键，未配置 = 不启用该状态）
max.chaos.maxConcurrent = 1       # 同一时刻至多一个活跃突变态
```

**快照/恢复**：任务启动时逐通道读取当前状态，写入 `/sdcard/fastbot_chaos.snapshot`（JSON：`{state: value}`）；任务结束（`Monkey.run` 的 finally 清理区，紧邻现有 `MutationAirplaneEvent.resetStatusAndExecute` 调用点）反向逐项恢复；恢复失败仅告警不阻断退出。

**降级**：通道命令执行失败（NonZero exit/IOException）→ `INJECT_FAIL` 丢弃该事件（计入 `mDroppedMutationEvents` 新计数器），不终止任务；遵循 P0 主循环守卫。

**测试**：pytest 无法覆盖（设备侧 Java），按仓库惯例真机验证；PC 侧 `tools/chaos_validate.py --dry-run` 校验配置文件合法性（可单测）。

## 2. perf-metrics（混合通道）

**设备侧**（Java）：扩展 `MonkeyGetAppFrameRateEvent` 模式，新增 `PerfFrameEvent`：
- 每 N 秒采集帧率/jank（`dumpsys gfxinfo <pkg> framestats` 解析），追加写 `/sdcard/fastbot_perf/<runid>.jsonl`（行格式 `{"ts":ms,"fps":x,"janky":n,"p90ms":x}`）
- 由 `max.config` 开关：`max.perf.frame = true`，`max.perf.frameIntervalSec = 5`

**PC 侧**（`tools/perf_poller.py`）：
- 10s 周期 adb 轮询：`dumpsys cpuinfo --checkin`、`dumpsys meminfo <pkg> --checkin`、`dumpsys netstats`、`dumpsys battery` → 时间序列
- 启动检测：轮询 `pidof <pkg>`，进程消失后重现 = 冷启动（计时目标 Activity 首帧用 `logcat -s ActivityTaskManager:I` 的 Displayed 行校准）；无消失的重启 = 温启动
- 产物：`tools/out/<run_id>/perf/data.json`（序列）+ `report.html`（中文，含曲线图，纯内联 SVG 无外部依赖）

**开销预算**：PC 轮询经 adb 不占用设备主循环；设备侧帧采集间隔 ≥5s，`throttle` 影响目标 <5%（AC2）。

## 3. coverage-diff（Native 序列化扩展 + PC diff）

**设备侧**（C++，复用模型复用通道）：
- `native/desc/reuse/ReuseState.cpp` 序列化处扩展：导出每个已探索 State 的 Activity 名 + 控件列表（`RichWidget` 已含 resource-id/text/content-desc），新增 `native/storage/CoverageExporter`
- 落盘 `/sdcard/fastbot_coverage/<pkg>.json`：`{"version":"<versionName>","activities":[{name, widgets:[WidgetKey], visit_count}]}`
- 时机：与 `.fbm` 相同（每 10 分钟 + 任务结束覆写）
- 反混淆：`max.mapping` 已有解析逻辑（专家系统），归一化在序列化前应用
- 开关：`max.coverage.exportWidgetLevel = true`（默认 false，保证存量行为不变）

**WidgetKey 身份键**（AC3）：`resource-id` > `text` > `content-desc` > 控件树路径（`class:parent_index` 链），序列化时四元组全存，匹配时按优先级降级。

**PC 侧**（`tools/coverage_diff.py`）：
- 输入两份 JSON → 输出 `diff.html`/`diff.json`：新增控件/消失控件/变更控件（按 Activity 分组）
- `--determinism-check`：同版本两份快照 diff 必须为空，非空则报出差异项并 exit 1（AC3 确定性校验）

**风险**：C++ 序列化改动需验证对 `.fbm` 向后兼容——采用**独立文件**（fastbot_coverage）而非扩展 .fbm 格式，零兼容风险。

## 4. privacy-compliance（设备侧 + PC 报告 + 可选 frida 外挂）

**弹窗自动处理**（Java）：新增 `events/customize/PrivacyPopupEvent`（配置驱动，复用 `AbstractCustomEvent` 模式）：
- `max.privacy.rules`（JSON，push /sdcard/）：`[{"page":".*privacy.*","widget":"com.xxx:id/agree","action":"consent"},{"page":".*","widget":".*permission.*allow","action":"consent"}]`
- `max.config`：`max.privacy.defaultAction = consent`（consent/deny），`max.privacy.enabled = true`
- 匹配循环挂在 TreeBuilder 输出后：命中规则→注入点击→记录审计→继续探索（不中断）

**敏感行为审计**（Java）：权限弹窗触发/规则命中时记录 `AuditRecord`（时刻、Activity、widget、ActionType、截图路径——复用 `ImageWriterQueue` 截图，需 `max.takeScreenshot = true`），追加写 `/sdcard/fastbot_privacy/audit.jsonl`。

**PC 报告**（`tools/privacy_report.py`）：audit.jsonl → `privacy_report.html`（中文，按权限/页面聚合统计 + 截图索引）。

**frida 外挂**（可选，仅 root 机，`tools/privacy_hook/`）：
- `hook.js`：frida 注入被测 App，监控敏感 API（`TelephonyManager.getDeviceId`、`ClipboardManager`、定位、通讯录读取等，清单可配 `tools/privacy_hook/apis.json`）
- 命中 → 以相同 AuditRecord 格式追加（经 `adb forward` 到设备文件或经 stdout 由 PC 收集），与进程内审计同报告通道

## 5. device-matrix（PC 编排器）

**`tools/matrix_runner.py`**：
- 输入 `matrix.json`：`{"run_id":"...","devices":[{"serial":"","apk":"","duration_min":60,"profile":{"chaos":true,"perf":true,"privacy":true}}]}`（≤10 台）
- 流程：逐设备 push 产物与配置 → 并行 `adb -s <serial>` 拉起 Fastbot + perf_poller → 监控进程存活 → 到时/崩溃后收集产物（crash-dump、oom-traces、perf jsonl、coverage json、audit jsonl、截图）
- 失败隔离：单设备任务失败仅标记该设备结果，不影响其他（每设备独立子进程 + 超时强杀）
- 产物布局：`tools/out/<run_id>/matrix/<serial>/{crash-dump.log, perf/, coverage/, privacy/}`
- 聚合：`report.html` 中文矩阵总览（每台设备一行：crash 数/ANR 数/Activity 覆盖率/性能摘要/隐私事件数）

## 6. llm-exploration（外部 Agent 闭环）

**`tools/gui_export.py`**：连接正在运行的设备，拉取当前 GUI 状态导出包：
- GUI XML（复用 TreeBuilder 格式，经 `max.saveGUITreeToXmlEveryStep` 或实时 `uiautomator dump` 兜底）
- 截图（`max.takeScreenshot` 开关，默认开）
- 状态摘要 JSON：Activity、包名、已执行动作数、当前 .fbm 的已访/未访 Activity 统计
- 输出：`tools/out/<run_id>/agent_export/<ts>/`（XML + PNG + summary.json）

**`tools/push_config.py`**：校验 Agent 生成的 `max.xpath.actions`（schema：事件序列、activity 匹配、widget 定位字段完整性）→ push `/sdcard/` → 提示复跑；`--dry-run` 只校验。

**`tools/agent_protocol.md`**：面向外部 Agent 的交互协议文档（导出→分析→生成→校验→推送→复跑循环，含字段说明与示例）。

**基线对比**（AC6）：`tools/coverage_compare.py baseline.json experiment.json`，输出 Activity 覆盖率提升百分比；提升阈值可配（默认 15%）。

## 7. 横切关注点

- **目录规范**：`tools/{common/, out/<run_id>/, *.py}`；`tools/requirements.txt` 仅基础依赖（不引入重框架）
- **测试策略**：pytest 覆盖所有设备无关逻辑（diff 算法、schema 校验、报告生成、配置解析——adb 层 mock）；每个入口命令支持 `--dry-run`；Fastbot 本体（Java/C++）无单测基建，真机验收由用户团队执行
- **报告**：共享 `tools/common/report.py` 模板（内联 SVG，无 CDN 依赖，中文正文/英文字段）
- **向后兼容**：所有新 `max.*` 配置键缺省关闭；不修改现有 .fbm 格式；存量命令行为零变化
- **风险与缓解**：① frida 依赖 root 机环境（隔离为可选增强）；② `cmd connectivity` VPN 通道在部分 ROM 不可用（降级跳过并告警）；③ C++ 导出异常不得影响主循环（Exporter 独立线程 + 异常吞没 + 计数器暴露）；④ adb 并发 10 台在低配 PC 可能吃满 USB 带宽（matrix_runner 内置并发信号量，可配）

## 8. 实施切分（进入规划阶段的输入）

每组件独立可交付，按构建顺序 6 个里程碑：M1 chaos → M2 perf → M3 coverage → M4 privacy → M5 matrix → M6 llm-protocol。M5 依赖 M2/M4 产物格式冻结；M6 依赖 M3 的覆盖率统计。每里程碑交付即按对应 AC 验收。
