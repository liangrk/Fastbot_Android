# Fastbot 外部 Agent 交互协议 (M6 llm-protocol)

外部 LLM Agent(Claude Code / Codex / pi / omp 均可驱动)通过本协议参与 Fastbot
稳定性测试的**半自动闭环**: 机器负责采集与判定, Agent 负责分析与生成专家配置。
本仓库内不含任何 LLM 代码; 所有交互均通过下述 CLI 完成。

## 0. 闭环总览

```
导出 (gui_export.py) → 分析 (Agent) → 生成 (max.xpath.actions)
  → 校验+推送 (push_config.py) → 复跑 (Fastbot) → 对比 (coverage_compare.py)
```

| 步骤 | 工具 | 输入 | 输出 |
| --- | --- | --- | --- |
| 导出 | tools/gui_export.py | 设备 + 包名 | summary.json / gui.xml / screenshot.png |
| 分析 | Agent(人机协同) | 导出包 | 未访 Activity 清单 + 到达路径假设 |
| 生成 | Agent | 分析结论 | max.xpath.actions(JSON 数组) |
| 校验+推送 | tools/push_config.py | 生成的 JSON | 校验结果 + /sdcard/max.xpath.actions |
| 复跑 | Fastbot(README 命令) | 新配置 | 新的 coverage JSON |
| 对比 | tools/coverage_compare.py | 基线/实验 coverage | 覆盖提升 % + 一致性结论 |

## 1. 硬约束(违反即闭环无效)

1. **基线定义**: 同一二进制 + 无 `max.xpath.actions` + 其余 max.* 键完全一致。
2. **对比运行配置一致性**: chaos/privacy/perf 相关 max.* 键在基线与实验运行中
   必须完全一致(AC6 Rev.2 条款), 由 coverage_compare.py 的
   `--baseline-config/--experiment-config` 校验; 不一致会大字警告。
3. **设备规模 ≤ 10 台**: 单机闭环或小规模矩阵; 超过 10 台的编排属于 M5
   matrix_runner 的领域(且 matrix_runner 拒绝 >10 台)。
4. **配置文件名不可改**: max.xpath.actions 等 max.* 文件名固定, 推送目标恒为
   /sdcard/max.xpath.actions。
5. **同版本对比**: 基线与实验必须同 App 版本(coverage JSON 的 version 字段),
   同二进制; 跨版本请用 coverage_diff.py 并解读为 UI 变更而非提升。
6. **等同时长**: 基线与实验 --running-minutes 相同。
7. **coverage 导出开启**: 基线与实验的 max.config 均需
   `max.coverage.exportWidgetLevel = true`, 否则不会生成
   /sdcard/fastbot_coverage/<pkg>.json, 闭环没有对比物。

## 2. 步骤 0: 前提

按 README 推送运行时三件套与 so:

```
adb push monkeyq.jar fastbot-thirdpart.jar framework.jar /sdcard/
adb push libs/arm64-v8a/* /data/local/tmp/
adb push max.config /sdcard/          # 至少含 max.coverage.exportWidgetLevel = true
```

## 3. 步骤 1: 导出 (gui_export.py)

命令(设备自动探测时 --serial 可省):

```
python tools/gui_export.py --package com.example.app --serial <serial> --out tools/out/loop1/agent_export
```

产出 `<out>/{summary.json, gui.xml, screenshot.png}`。summary.json 字段:

| 字段 | 说明 |
| --- | --- |
| package | 目标包名 |
| activity | 导出时刻顶部 Activity(解析 dumpsys; 失败为 unknown) |
| ts | ISO8601 UTC 时间戳 |
| visited_activities | 已访清单, 来自 /sdcard/fastbot_coverage/<pkg>.json |
| unvisited_activities | 未访清单 = 声明全集 − 已访(集合差) |
| visited_count / unvisited_count | 计数 |
| total_declared | 声明 Activity 全集大小(**未访分母**, 来自 `dumpsys package` "Activities:" 段) |
| gui_xml_source | fastbot_step_xml(优先, max.saveGUITreeToXmlEveryStep 落盘的最新 step-*.xml)或 uiautomator_dump(兜底) |
| screenshot | screenshot.png(成功时) |
| notes | 降级说明(如 coverage 缺失 → visited=[] + note) |

无设备冒烟: `python tools/gui_export.py --package com.example.app --dry-run`
(打印导出计划, 零设备接触)。

## 4. 步骤 2: 分析 (Agent)

Agent 读 summary.json + gui.xml + screenshot.png, 输出:

- 未访 Activity 清单(unvisited_activities);
- 从当前 activity 出发的到达路径假设(结合 gui.xml 可见控件);
- 供步骤 3 生成 case 的 activity/activity 与 locator 建议。

示例提示语(供操作者粘贴给 Agent):

```
以下是 Fastbot 导出的 GUI 状态包(summary.json/gui.xml/screenshot.png)。
未访 Activity: <粘贴 unvisited_activities>。
请分析哪些值得覆盖、给出每个目标 Activity 的到达路径假设,
并按 Fastbot max.xpath.actions 的真实格式生成 case。
activity 字段必须是完整 Activity 名(原生层做精确等值匹配, 不是正则)。
```

## 5. 步骤 3: 生成 max.xpath.actions (Agent)

**格式以仓库真实文件为准**(test/max.xpath.actions 与 native/events/Preference.cpp
loadActions), 不要发明字段:

```
[
  {
    "prob": 1,
    "activity": "com.ss.android.xxx.SplashActivity",
    "times": 100,
    "actions": [
      { "xpath": "//*[@resource-id='com.xxx.go:id/bbb']", "action": "CLICK", "throttle": 3000 }
    ]
  }
]
```

字段速查:
- prob: 0..1 发生概率(缺省 1); times: 重复次数(缺省 1, 需 ≥1);
- activity: **完整 Activity 名**(精确等值匹配; 相对名/正则永不匹配, 校验器会警告);
- actions[]: action ∈ CLICK/LONG_CLICK/BACK/SCROLL_TOP_DOWN/SCROLL_BOTTOM_UP/
  SCROLL_LEFT_RIGHT/SCROLL_RIGHT_LEFT(完整集合见 tools/push_config.py 的
  KNOWN_ACTION_TYPES, 即 native Base.cpp actName[]);
- CLICK/LONG_CLICK/SCROLL_* 必须带非空 xpath 定位(原生 patchActionBounds
  匹配失败即跳过); BACK 等无目标动作不需要 xpath;
- throttle: ms(缺省 1000); text/clearText/wait/useAdbInput: CLICK 输入相关。

## 6. 步骤 4: 校验 + 推送 (push_config.py)

```
python tools/push_config.py agent_out/max.xpath.actions --serial <serial> --package com.example.app
python tools/push_config.py agent_out/max.xpath.actions --dry-run   # 只校验, 零设备接触
```

非法配置 exit 1 并逐条列出错误; 合法则推送 /sdcard/max.xpath.actions 并打印
复跑命令。非法示例输出:

```
ERROR: case#1 action#1: unknown action type 'TAP' (known: CRASH, FUZZ, ...)
ERROR: case#2: missing or empty 'activity' (native matches the FULL name exactly)
RESULT: INVALID (5 error(s), 1 warning(s))
```

## 7. 步骤 5: 复跑 (Fastbot)

```
adb -s <serial> shell CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:/sdcard/fastbot-thirdpart.jar exec app_process /system/bin com.android.commands.monkey.Monkey -p com.example.app --agent reuseq --running-minutes 30 --throttle 100 -v -v
```

复跑结束后 coverage JSON 已刷新(30min 任务每 10min 节点导出 + finally 兜底导出)。

## 8. 步骤 6: 对比 (coverage_compare.py)

```
python tools/coverage_compare.py baseline.json experiment.json \
    --threshold 15 --fail-under-threshold \
    --baseline-config run1/max.config --experiment-config run2/max.config
```

输出: Activity 覆盖提升 %((实验已访 − 基线已访)/基线已访 × 100, 基线=0 时记
N/A: 实验>0 记 100% 否则 0%)、新增/丢失 Activity 清单、控件级 delta(复用
coverage_diff), 并做 AC6 配置一致性检查(chaos/privacy/perf 键, 不一致则
大字警告)。基线定义会打印在输出脚注:

```
基线定义: 同一二进制 + 无 max.xpath.actions + 其余 max.* 键完全一致
```

## 9. 每步示例: 一轮完整闭环

```
# (1) 基线运行(无 max.xpath.actions; 建议先 adb shell rm /sdcard/max.xpath.actions)
adb shell rm -f /sdcard/max.xpath.actions
adb shell CLASSPATH=... exec app_process ... --running-minutes 30 ...
adb pull /sdcard/fastbot_coverage/com.example.app.json tools/out/loop1/baseline.json
# (2) 导出 -> Agent 分析/生成 -> 校验+推送
python tools/gui_export.py --package com.example.app --out tools/out/loop1/agent_export
# (Agent 分析 gui.xml/screenshot.png, 生成 agent_out/max.xpath.actions)
python tools/push_config.py agent_out/max.actions.json --dry-run && \
python tools/push_config.py agent_out/max.actions.json --serial <serial> --package com.example.app
# (3) 复跑 + 导出实验 coverage
adb shell CLASSPATH=... exec app_process ... --running-minutes 30 ...
adb pull /sdcard/fastbot_coverage/com.example.app.json tools/out/loop1/experiment.json
```

## 10. 矩阵约束 (≤10 台)

多台并行复跑请用 matrix_runner(≤10 台, 超出 exit 2): 示例 matrix.json 见
tools/tests/fixtures/matrix/matrix.two.json; 单设备失败隔离, 聚合报告
report.html 含每台 crash/覆盖/性能/隐私摘要。闭环对比时所有设备使用同一
max.config 与同一二进制。
