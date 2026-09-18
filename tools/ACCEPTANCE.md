# Fastbot 六项扩展验收手册 (AC1-AC6)

按 ralplan-fastbot-six-extensions 计划 §Acceptance Criteria 编写, 供用户团队
真机执行。每个 AC 给出 目标 / 前置 / 步骤(命令级) / 通过标准。所有 AC 的对比
型验收都要遵守统一约束: **同一二进制、同一 App 版本、等同时长、≤10 台、对比
运行配置一致**。

## AC1 chaos 注入与恢复

**目标**: 4 组状态按 max.chaos.<state>.pct 概率注入 30min 任务; 任务结束恢复
任务前快照; crash-dump 正常落盘。

**前置**:
- 已按 README 推送 monkeyq.jar / fastbot-thirdpart.jar / framework.jar 至
  /sdcard/ 与 libs/<abi>/*.so 至 /data/local/tmp/;
- 构造 max.config(样例 tools/tests/fixtures/chaos/max.config.valid):

```
max.chaos.enable = true
max.chaos.battery.pct = 0.3
max.chaos.powersave.pct = 0.2
max.chaos.dnd.pct = 0.1
max.chaos.maxConcurrent = 2
max.chaos.timeoutSec = 8
```

**步骤**:
1. `adb push max.config /sdcard/`
2. 30min 运行(README 模式):

```
adb -s <serial> shell CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:/sdcard/fastbot-thirdpart.jar exec app_process /system/bin com.android.commands.monkey.Monkey -p <pkg> --agent reuseq --running-minutes 30 --throttle 100 -v -v
```

**通过标准**:
1. /sdcard/fastbot_chaos.snapshot 存在且 JSON 合法(chaos_snapshot.schema.json);
2. 运行后设备状态恢复到快照值(手动核对 settings/服务的状态值, 或对比快照
   JSON 内的 captured 状态); 恢复发生在任务结束恢复任务前(源码 finally 块);
3. /sdcard/crash-dump.log 正常落盘(若有崩溃则追加, 无崩溃文件可缺省);
4. logcat 无 INJECT_FAIL 以外的异常退出; INJECT_FAIL 计数仅来自已禁用通道。

## AC2 perf 开销测量

**目标**: JSON+HTML 报告(CPU/内存序列粒度 ≤10s、帧率/jank、冷/温启动);
**测量协议**: 同设备/同 App/同时长, perf 开 vs 关, 主循环节流开销
= |events/min(开) − events/min(关)| / events/min(关) × 100% < 5%。

**前置**:
- 设备装好被测 App; 准备两份 max.config: A=关闭(不含 max.perf.frame 或
  设 false), B=开启(`max.perf.frame = true`、`max.perf.frameIntervalSec = 5`);
  除 perf 两键外 A 与 B 其余内容完全一致;
- 两次运行同一二进制、同一时长(建议 30min)、同一 throttle。

**步骤**:
1. 关闭组: push max.config A, 运行:

```
adb -s <serial> shell CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:/sdcard/fastbot-thirdpart.jar exec app_process /system/bin com.android.commands.monkey.Monkey -p <pkg> --agent reuseq --running-minutes 30 --throttle 100 -v -v 2>&1 | tee run_perf_off.log
```

2. 统计关闭组 events/min: Fastbot 每个决策周期打印一条
   "Top activity name is:<activity>"; 用事件行计数作为 events 代理:

```
grep -c "Top activity name is" run_perf_off.log
```

3. 同时在关闭组并行启动 PC 轮询器(证据留存, 可选但建议):

```
python tools/perf_poller.py --serial <serial> --package <pkg> --interval 10 --duration 1800 --out tools/out/ac2/off
```

4. 开启组: push max.config B, 重复步骤 1-3(输出 run_perf_on.log 与
   tools/out/ac2/on); 结束后拉取:

```
adb pull /sdcard/fastbot_perf/ tools/out/ac2/on/
python tools/perf_report.py tools/out/ac2/on/fastbot_perf/<runid>.jsonl --starts tools/out/ac2/on/perf/starts.json --out tools/out/ac2/report_on.html
```

5. 计算节流开销比并出报告:

```
OFF = grep -c "Top activity name is" run_perf_off.log / 30
ON  = grep -c "Top activity name is" run_perf_on.log / 30
开销比 = |ON - OFF| / OFF * 100%
```

**通过标准**:
1. 开销比 < 5%;
2. tools/out/ac2/on/perf/report.html 存在, 含 CPU/内存序列(粒度 ≤10s)、
   帧率/jank、冷/温启动表; data.json 每行符合 perf_sample.schema.json;
3. fastbot_perf/<runid>.jsonl 每行符合 perf_frame.schema.json;
4. 冷/温启动判定以 logcat Displayed 行为主信号(pidof 仅佐证, 方法论注记见报告)。

## AC3 coverage 确定性与跨版本差异

**目标**: 同版本两次运行 diff 为空(--determinism-check exit 0); 跨版本输出
新增/消失/变更控件清单。

**前置**: max.config 含 `max.coverage.exportWidgetLevel = true`; 同 App 两个
版本(或同版本跑两次)各自产出 coverage JSON。

**步骤**:
1. 确定性: 同版本两次运行, 分别拉取:

```
adb pull /sdcard/fastbot_coverage/<pkg>.json run1.json
adb pull /sdcard/fastbot_coverage/<pkg>.json run2.json
python tools/coverage_diff.py run1.json run2.json --determinism-check
```

2. 跨版本: 对比两个版本的 coverage JSON:

```
python tools/coverage_diff.py v1.json v2.json
```

**通过标准**:
1. 确定性检查 exit 0, "determinism check PASSED";
2. 跨版本生成 diff.json/diff.html, 新增/移除/变更控件清单完整(控件身份
   四元组优先级 resource-id > text > content-desc > path)。

## AC4 privacy 弹窗处理与审计

**目标**: 隐私弹窗 100% 自动处理; 审计含 时刻+页面+控件+截图; 规则可配置。

**前置**:
- 编写规则文件(格式见 tools/privacy_rules.py 文档字符串与
  tools/tests/fixtures/privacy/rules.valid.json):

```
[
  {"page": "com.example.app.*.PrivacyDialogActivity",
   "widget": "bounds=\"[2,2][1080,120]\" resource-id=\"com.example.app:id/agree\"",
   "action": "consent", "name": "隐私弹窗", "permission": "privacy"},
  {"page": "com.example.app.*.PermissionActivity", "action": "deny",
   "name": "权限弹窗", "permission": "READ_PHONE_STATE"}
]
```

- 先用 PC 校验器预检(与设备侧语义对齐):

```
python tools/privacy_rules.py validate rules.json
```

- max.config 追加:

```
max.privacy.enabled = true
max.privacy.rules = /sdcard/rules.json
max.privacy.defaultAction = consent
max.privacy.screenshot = true
```

**步骤**:
1. `adb push rules.json /sdcard/rules.json`
2. 运行 30min 任务(README 命令, --agent reuseq);
3. 拉取审计与截图:

```
adb pull /sdcard/fastbot_privacy/ tools/out/ac4/
python tools/privacy_report.py tools/out/ac4/audit.jsonl --out tools/out/ac4/report.html
```

**通过标准**:
1. 每次规则命中的弹窗均被点击(审计 action=click), 无人工干预;
   页面级命中而无 bounds 时为 audit-only(记录但不盲点);
2. audit.jsonl 每行符合 audit.schema.json(时刻+页面+控件+action+detail),
   audit-only 命中也逐条落盘;
3. max.privacy.screenshot=true 时命中项配 hit-*.png 截图且 report.html
   链接可点; 截图失败不阻塞审计(先审计后截图, 降级不抛错);
4. 规则文件顺序首个命中生效; 空规则=引擎关闭。

### AC4-supplement frida 敏感 API 外挂(可选, 仅 root 机)

**目标**: 被测 App 进程内的敏感 API 调用(定位/剪贴板/相机/麦克风/通讯录等)
以同一 audit 格式落盘, 与弹窗审计同报告渲染。

**前置**:
- 设备已 root 且装有匹配版本 frida-server(如
  `/data/local/tmp/frida-server`), 启动:
  `adb shell su -c '/data/local/tmp/frida-server -D &'`;
  PC 需 `pip install frida-tools`(`frida --version` 可执行);
- `frida-ps -U` 能列出进程(连通性检查);
- 语义/API 清单: tools/privacy_hook/apis.json(15 APIs / 8 classes,
  可经 collect.py --manifest 替换)。

**步骤**:

```
# spawn 模式(拉起被测 App, 适合冷启动期 SDK 采集):
python tools/privacy_hook/collect.py --serial <serial> \
  --package <被测包名> --duration-sec 60 --out tools/out/ac4/audit_frida.jsonl
# attach 模式(App 已在运行; 自动经 frida-ps -ai 解析包名->PID):
python tools/privacy_hook/collect.py --serial <serial> --attach \
  --package <被测包名> --duration-sec 60 --out tools/out/ac4/audit_frida.jsonl

python tools/privacy_report.py tools/out/ac4/audit_frida.jsonl --out tools/out/ac4/frida.html
```

**通过标准**:
1. 注入期被测 App 不崩溃(collect.py frida returncode=0);
2. 命中的 API 逐行落盘且每行通过 audit.schema.json
   (type=sensitive_api, source=frida);
3. 命中记录出现在 privacy_report.html 聚合中。

**已知边界**(2026-09-16 真机验证结论, OPPO PHK110 / Android 13 / frida 17.9):
- 注入/hook 安装/发射/捕获全链路已在真机打通; 但自然命中有赖目标 App 行为:
  Android 10+ 已对普通应用封禁 getDeviceId/getSubscriberId 等遗留标识接口
  (清单仍保留, 供系统级/特权场景), 验收时优先观察 location/clipboard/camera
  等仍可用的 API;
- 部分加固 App(如系统相机)有反 frida 检测, spawn 即失败——属预期,
  换非加固目标即可;
- attach 模式要求目标进程已存活, 否则报 "not running" 后退出。

## AC5 多设备矩阵

**目标**: ≥3 台并行 1h; 单设备失败隔离; 聚合报告含每台 crash/覆盖/性能摘要。

**前置**:
- ≥3 台设备 adb 可见; 准备 matrix.json(参考
  tools/tests/fixtures/matrix/matrix.two.json):

```
{
  "run_id": "ac5",
  "devices": [
    {"serial": "<serial1>", "apk": "com.example.app",
     "duration_min": 60, "profile": {}},
    {"serial": "<serial2>", "apk": "com.example.app",
     "duration_min": 60, "profile": {}},
    {"serial": "<serial3>", "apk": "com.example.app",
     "duration_min": 60, "profile": {"perf": true}}
  ]
}
```

- apk 字段即被测包名, 原样作为 Monkey 的 `-p` 参数(不是本机 APK 文件
  路径); duration_min 必须写在每台设备对象内(顶层 duration/package 键
  会被 validate_matrix 忽略)。

**步骤**:
1. 计划预检(零设备接触): `python tools/matrix_runner.py --matrix matrix.json --dry-run`
2. 执行: `python tools/matrix_runner.py --matrix matrix.json --max-concurrent 3`
3. 结束后查看 `tools/out/ac5/report.html`。

**通过标准**:
1. 3 台并行完成 1h; 任一台 preflight 失败/超时/崩溃不影响其余设备
   (失败隔离, 聚合报告该台标记 失败/超时 + 备注);
2. report.html 含每台: 设备/结果/crash 计数/ANR/Activity 覆盖/性能摘要/
   隐私事件数/备注;
3. 超时硬杀在 duration+grace(默认 60s)内发生, 不悬挂。

## AC6 llm 闭环与覆盖提升

**目标**: 导出→分析→生成→校验→推送→复跑闭环跑通; 同等时长下 Activity 覆盖
提升 ≥15%(阈值可配); 对比运行配置一致性(chaos/privacy/perf max.* 键)。

**前置**:
- 阅读并遵循 tools/agent_protocol.md(闭环步骤、字段速查、约束);
- 两份 max.config(基线/实验)除 max.xpath.actions 有无外完全一致, 且均含
  `max.coverage.exportWidgetLevel = true`;
- 基线定义(硬约束): **同一二进制 + 无 max.xpath.actions + 其余 max.* 键
  完全一致**。

**步骤**:
1. 基线运行: 确认无 /sdcard/max.xpath.actions
   (`adb shell rm -f /sdcard/max.xpath.actions`), 30min 运行, 拉取
   /sdcard/fastbot_coverage/<pkg>.json 存为 baseline.json; 同步留存该次
   max.config 为 baseline.max.config;
2. 闭环生成: gui_export 导出 GUI 状态包 → Agent 分析未访 Activity → 生成
   max.xpath.actions → push_config --dry-run 校验 → push_config 推送
   /sdcard/max.xpath.actions(逐条命令见 agent_protocol.md §3-§7);
3. 实验运行: 同命令 30min 复跑, 拉取 coverage 存为 experiment.json; 留存
   experiment.max.config;
4. 对比与判定:

```
python tools/coverage_compare.py baseline.json experiment.json \
    --threshold 15 --fail-under-threshold \
    --baseline-config baseline.max.config --experiment-config experiment.max.config
```

**通过标准**:
1. 闭环全链路可跑通(每个工具 exit 0/1 语义正确, 无工具崩溃);
2. coverage_compare exit 0 且提升 ≥15%(阈值 --threshold 可配); 基线=0 时按
   N/A 规则判定(实验>0 记 100%, 否则 0%);
3. 配置一致性: 未提供 config 对 → 输出明确提示"跳过"; 仅提供一半 →
   WARNING; 提供成对且 chaos/privacy/perf 键不一致 → 大字 WARNING 并逐条
   列出 MISMATCH(报告义务, 不改变 exit code);
4. 输出脚注含基线定义原文。

## 附: 验收证据留存建议

- 每个 AC 留存: 命令 + exit code + 关键输出行 + 产物文件
  (coverage JSON / audit.jsonl / perf JSONL / chaos snapshot / report.html);
- 对比型验收(AC2/AC3/AC6)额外留存两组运行的 max.config 副本, 供
  coverage_compare 的配置一致性检查复核。

## AC-CR 崩溃聚合报告 (二期, PC 侧即验)

**目标**: crash-dump.log + logcat FATAL/ANR → 确定性堆栈签名聚类; 跨 run 新增签名
门禁; 每簇根因包(供 auto-agent)。

**步骤**:

```
python tools/crash_report.py <run>/crash-dump.log --logcat <run>/logcat.txt --out tools/out/cr
python tools/crash_report.py <run>/crash-dump.log --determinism-check --out tools/out/cr
python tools/crash_report.py <cur>/crash-dump.log --baseline tools/out/prev/clusters.jsonl --fail-new-crash --out tools/out/cr
```

**通过标准**:
1. clusters.jsonl / root_cause.jsonl / crash_report.html 三产物齐, JSONL 行通过
   crash_cluster / root_cause_pack schema;
2. `--determinism-check` exit 0; 同输入重跑 clusters.jsonl 逐字节一致;
3. `--fail-new-crash`: 有新增签名 exit 1 且逐行 NEW SIGNATURE, 无新增 exit 0;
4. 消息文本不入签名哈希(易变路径/地址不分裂根因), 仅异常类型+归一化帧入哈希。

## AC-AA LLM 自动闭环 (AC-AA1 PC 即验; AA2/AA3 需真机)

**AC-AA1(零接触)**: agent_protocol.md §11.4 演练序列全部 rc0;
`pytest tools/tests/test_agent_protocol.py tools/tests/test_fastbot_run.py -q` 全绿。

**AC-AA2/AA3(真机)**: 按 agent_protocol.md §11.5 粘贴提示语给 Claude Code。

**2026-09-18 实测记录**(三星 A53 容器 / Android 15 / x86_64 / KernelSU root,
目标包 com.android.settings, 每轮 2min, 指定机 192.168.128.220:41917 离线改用):
3 轮零人工完成; push_config 全程 0 format 错误; 三轮均无 crash-dump.log
(新增崩溃门禁零新签名, 通过); round2→round3 +11 widgets / +1 activity
(agent 依 gui_export 修正 actions 后覆盖回升)。**AC-AA2 未达标**:
baseline(6 acts/77 widgets)→round3(2 acts/16 widgets) 未达 +15% —— 目标 App
为 Android 15 SPA 设置, 全部页面渲染于单一 SpaActivity, Activity 级覆盖
天花板低, 且 baseline 首轮漫游(含 Launcher)不可复现。结论: 闭环机制验收
通过, 覆盖提升指标需真实被测 App + 更长轮次复验。

**本轮验收驱动的两处修复**:
1. `MonkeySourceApeNative.createUiAutomationCompat`: UiAutomation 构造器
   形态随 AOSP 演化(≤12 具体 2 参 / 13/14 flags 3 参 / 15+ 接口型 2 参),
   改为 `getDeclaredConstructors()` 按首参 Looper + 可接 UiAutomationConnection
   扫描匹配, Android 9/15 实测均启动成功;
2. `fastbot_run.py`: `--running-minutes` 模式 Monkey 正常跑满时限也以
   rc=注入事件数 退出(Monkey.run `crashedAtCycle < mCount-1` 分支), 不再
   误判为 failed —— 以 fastbot.log 含 "Events injected:" 且无崩溃标记判定。

## AC1-AC5 真机冒烟 (2026-09-18, 三星 A53 容器 / Android 15 / x86_64)

注: 各 AC 全程协议为 30min, 本轮为 3min 合并冒烟; 30min 全程与 AC2 开销比
(<5%) 留给用户团队复验。

- **AC1 chaos**: 合并 max.config(coverage+chaos+perf+privacy) 3min run,
  `fastbot_chaos.snapshot` 符合 chaos_snapshot.schema.json, log 见
  "[chaos] channel ready / snapshot persisted / restored battery"(finally 恢复)。
- **AC2 perf**: `fastbot_perf/<ts>.jsonl` 34 行全部符合 perf_frame.schema.json,
  perf_report.html 生成(CPU/内存序列+帧率+jank+冷/温启动)。
- **AC3 coverage**: 三轮 coverage JSON 均被 coverage_compare/coverage_diff
  正常消费(见 AC-AA 记录)。
- **AC4 privacy**: 2 条规则(中文 text xpath)命中 170 次全部符合
  audit.schema.json, privacy_report.html 生成。
- **AC5 matrix**: 3 异构设备(a53x Android 15 / 云机 Android 9 x86_64 /
  rk3588 Android 10 arm64)并行编排, 失败隔离与聚合 HTML 报告验证通过
  (1 completed + 1 timeout + 1 failed, 无串扰)。失败均为容器环境:
  rk3588 系统 server 拒绝注册 UiAutomation(RemoteException),
  云机 Android 9 收尾导出超过 grace。**本轮验收驱动修复**:
  `matrix_runner.py` serial 含 `:` 时 Windows 目录名非法, 已净化为
  `_serial_dir()`(非 [A-Za-z0-9._-] 替换为 `_`)。

**通过标准**:
1. 零人工干预完成 3 轮(人在起点/终点), 每轮产物齐(fastbot.log / crash-dump.log /
   coverage / agent_export / max.xpath.actions);
2. 终轮 `coverage_compare --threshold 15 --fail-under-threshold` exit 0;
3. 每轮 `push_config --dry-run` 零 format 错误;
4. 停止条件(轮数上限 / 覆盖目标 / 新增崩溃门禁)任一满足即停。

## AC-DW 设备侧弱网 (需 root 机)

**前置**: rooted 设备(su 可用); 被测 App 已安装。

**步骤**:

```
python tools/weaknet.py device-on --package <pkg> --profile edge --serial <serial>
# 在目标 App 内发起 HTTP(S) 请求, 同时对照:
python tools/weaknet.py device-status --serial <serial>
python tools/weaknet.py device-off --serial <serial>
```

**通过标准**:
1. 目标 App HTTP(S) 延迟 ≈ preset(edge ≈ 400ms ±30%); 非 UID 流量(其他 App /
   `adb shell` 端 curl, uid 2000)不受影响;
2. 缺省(无 `--allow-quic`)UDP/443 被 DROP(App 回落 TCP); 加 `--allow-quic` 放行;
3. 清理: device-off 与 Ctrl+C 中断后
   `adb shell su -c 'iptables -S OUTPUT | grep FASTBOT_WEAKNET'` 为空;
   二次 device-on 规则集与首次相同(幂等);
4. 无 root: exit 1 + 指引, 设备上零 FASTBOT_WEAKNET 规则残留;
5. 备注: device-off 移除设备上**全部** `tcp:` reverse 映射(共享设备先
   `adb reverse --list` 核对); device-on 进程被 SIGKILL 时规则不自动清除,
   手动运行 device-off 恢复。

## AC-PG 性能回归门禁 (真机两跑 + PC 判定)

**步骤**: 按 AC2 协议同设备先基线后实验各跑一次(`fastbot_run --collect-all` 或
perf_poller 均可, 两跑的采集方式必须一致), 得两份 perf 目录后:

```
python tools/perf_compare.py tools/out/pg/base tools/out/pg/exp \
    --max-cpu-regression 10 --max-pss-regression 10 --max-cold-p90-regression 15 --max-jank-regression 0.5
```

**通过标准**:
1. exit 语义仿 coverage_compare: 全达标 0 / 超阈值 1(MISMATCH 逐条) / 用法或 IO 2;
2. 四指标: CPU 均值 / PSS 均值 / 冷启动 p90(相对 %) + jank 率(绝对 pp); 缺数据 N/A
   不参与判定; 基线=0 按 N/A 规则(实验>0 记 100% 否则 0%);
3. 报告脚注含基线定义(同一设备+同一二进制+先基线后实验)与 jank 口径注记
   (framestats 16.67ms 预算, 与 perf_report 的 janky_frames 不可互比);
4. `pytest tools/tests/test_perf_compare.py -q` 全绿(all-pass/breach/multi/NA 四路径)。
