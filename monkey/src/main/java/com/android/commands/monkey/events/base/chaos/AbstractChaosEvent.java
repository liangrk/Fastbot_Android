/*
 * Copyright (c) 2026 Bytedance Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.android.commands.monkey.events.base.chaos;

import android.app.IActivityManager;
import android.view.IWindowManager;

import com.android.commands.monkey.events.MonkeyEvent;
import com.android.commands.monkey.utils.Config;
import com.android.commands.monkey.utils.Logger;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStream;
import java.io.StringReader;
import java.io.Writer;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.TimeZone;
import java.util.concurrent.TimeUnit;

/**
 * M1 chaos-injection: abstract base for shell-driven device-state chaos events
 * (8 channels: battery, powersave, bluetooth, location, mobiledata, vpn, dnd,
 * sysconfig). Extends MonkeyEvent with the same injectEvent(iwm, iam, verbose)
 * signature the existing mutation events use.
 *
 * Hard constraints (ralplan M1, consensus-reviewed):
 * <ul>
 *   <li>BOUNDED shell waits only: runBounded() caps wall time via
 *       Process.waitFor(long, TimeUnit); a hung channel degrades to INJECT_FAIL
 *       plus drop, never stalls the decision thread (the bare waitFor() in
 *       MutationWifiEvent is the documented anti-pattern).</li>
 *   <li>Channels degrade independently: any probe/snapshot/inject/restore
 *       failure disables that channel for the rest of the run with a warning;
 *       the failure is never fatal to the fuzzing run.</li>
 *   <li>Scheduling rolls live in MonkeySourceApeNative.generateEvents() and
 *       enqueue through addEvent, so chaos events flow getNextEvent and then
 *       through the P0 guard in Monkey (catch Exception | LinkageError).
 *       startMutation is never touched; injectEvent here is only ever called
 *       by the Monkey main loop.</li>
 *   <li>Decision-thread confined. The static registry is synchronized anyway
 *       (cheap); snapshotAll/restoreAll run from Monkey.run on the same thread
 *       as the decision loop.</li>
 * </ul>
 */
public abstract class AbstractChaosEvent extends MonkeyEvent {

    private static final long FALLBACK_TIMEOUT_SEC = 5L;
    /** max bytes of stdout captured per command (probes/snapshots are tiny) */
    private static final int MAX_CAPTURE = 16384;
    /** poll interval while the child produces no output */
    private static final long POLL_MS = 25L;
    /** persisted evidence file, compatible with tools/schemas/chaos_snapshot.schema.json */
    static final String SNAPSHOT_PATH = "/sdcard/fastbot_chaos.snapshot";

    /** config state key: battery, powersave, bluetooth, ... */
    private final String mStateName;
    /** chaos_snapshot.schema.json state enum value (power_save, mobile_data, ...) */
    private final String mSchemaState;
    /** true while this channel's disruption is applied (until end-of-run restore) */
    protected volatile boolean mActive = false;

    protected AbstractChaosEvent(String stateName, String schemaState) {
        super(EVENT_TYPE_COMMON);
        mStateName = stateName;
        mSchemaState = schemaState;
        registerInstance(this);
    }

    // ==================== template methods ====================

    /**
     * Harmless query form of the channel command; run once per enabled channel
     * at snapshot time (pre-probe). A failure permanently disables the channel.
     *
     * @return true when the channel tool answers on this ROM
     */
    protected abstract boolean probeChannel();

    /**
     * Capture the current device state as a compact self-describing payload
     * of key=value pairs (line or semicolon separated). Doubles as the
     * restore recipe.
     *
     * @return the payload, or an empty string when the state cannot be read
     */
    protected abstract String snapshotState();

    /** Apply the disruption with bounded shell waits. @return true on success */
    protected abstract boolean injectState();

    /** Revert to the given snapshot payload. @return true on success */
    protected abstract boolean restoreState(String snapshot);

    // ==================== injectEvent orchestration ====================

    /**
     * Monkey main-loop contract: snapshot (fallback), inject, and track the
     * channel as active until end-of-run restore. Every failure path degrades:
     * INJECT_FAIL + drop counter + channel disable - never an exception out of
     * this method (the P0 guard in Monkey drops it as usual as well).
     */
    @Override
    public int injectEvent(IWindowManager iwm, IActivityManager iam, int verbose) {
        if (mActive) {
            // idempotent for duplicate enqueues
            return INJECT_SUCCESS;
        }
        if (!isChannelUsable(mStateName)) {
            noteDrop("channel unavailable: " + mStateName);
            return INJECT_FAIL;
        }
        try {
            String snap = getSnapshot(mStateName);
            if (snap == null || snap.length() == 0) {
                snap = snapshotState();
                if (snap == null || snap.length() == 0) {
                    disableChannel(mStateName, "snapshot failed");
                    noteDrop("snapshot failed: " + mStateName);
                    return INJECT_FAIL;
                }
                putSnapshot(mStateName, snap);
                persistSnapshotFileLocked();
            }
            if (!injectState()) {
                disableChannel(mStateName, "inject failed");
                noteDrop("inject failed: " + mStateName);
                return INJECT_FAIL;
            }
            mActive = true;
            markActive(mStateName);
            Logger.println("[chaos] injected " + mStateName);
            return INJECT_SUCCESS;
        } catch (Throwable t) {
            disableChannel(mStateName, "inject exception: " + t);
            noteDrop("inject exception: " + mStateName);
            return INJECT_FAIL;
        }
    }

    // ==================== bounded shell execution ====================

    /**
     * Timeout-bounded shell execution path (the bounded analog of the unbounded
     * AndroidDevice.executeCommandAndWaitFor, which is Runtime.exec(cmd) with a
     * bare waitFor() and no timeout - the MutationWifiEvent anti-pattern).
     *
     * Runs via "sh -c <cmd> 2>/dev/null", drains stdout through available()-gated
     * reads (in.read() on an empty pipe blocks until the child exits, so reads
     * are only issued when bytes are pending), and bounds total wall time with
     * Process.waitFor(long, TimeUnit). No extra threads.
     *
     * @return child exit code, or -1 on timeout/exception/interrupt
     */
    protected static int runBounded(String cmd, long timeoutSec, StringBuilder out) {
        Process proc = null;
        try {
            proc = Runtime.getRuntime().exec(new String[]{"sh", "-c", cmd + " 2>/dev/null"});
            final long deadline = System.nanoTime() + timeoutSec * 1000000000L;
            InputStream in = proc.getInputStream();
            byte[] buf = new byte[2048];
            boolean exited = false;
            while (true) {
                if (System.nanoTime() > deadline) {
                    proc.destroy();
                    Logger.warningPrintln("[chaos] timeout after " + timeoutSec + "s: " + cmd);
                    return -1;
                }
                int avail;
                try {
                    avail = in.available();
                } catch (IOException io) {
                    // pipe closed because the child already exited
                    exited = true;
                    break;
                }
                if (avail > 0) {
                    try {
                        int n = in.read(buf, 0, Math.min(avail, buf.length));
                        if (n < 0) {
                            exited = true;
                            break;
                        }
                        if (out != null && out.length() < MAX_CAPTURE) {
                            out.append(new String(buf, 0, n));
                        }
                    } catch (IOException io) {
                        exited = true;
                        break;
                    }
                } else {
                    try {
                        if (proc.waitFor(POLL_MS, TimeUnit.MILLISECONDS)) {
                            exited = true;
                            break;
                        }
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        proc.destroy();
                        return -1;
                    }
                }
            }
            if (!exited) {
                return -1;
            }
            // drain whatever is still buffered after child exit
            try {
                while (true) {
                    int rest = in.available();
                    if (rest <= 0) {
                        break;
                    }
                    int n = in.read(buf, 0, Math.min(rest, buf.length));
                    if (n < 0) {
                        break;
                    }
                    if (out != null && out.length() < MAX_CAPTURE) {
                        out.append(new String(buf, 0, n));
                    }
                }
            } catch (IOException io) {
                // ignore: output capture is best-effort
            }
            try {
                in.close();
            } catch (IOException io) {
                // ignore
            }
            try {
                return proc.exitValue();
            } catch (IllegalStateException ise) {
                // exitValue throws IllegalThreadStateException on a live process;
                // unreachable after a successful bounded waitFor, kept for safety
                return -1;
            }
        } catch (IOException e) {
            Logger.warningPrintln("[chaos] exec failed: " + cmd + " : " + e);
            if (proc != null) {
                proc.destroy();
            }
            return -1;
        }
    }

    /** @return stdout of the command when it exits 0, else an empty string */
    protected static String shellOut(String cmd) {
        StringBuilder sb = new StringBuilder();
        int code = runBounded(cmd, shellTimeoutSec(), sb);
        return code == 0 ? sb.toString() : "";
    }

    /** @return true when the command exits 0 within the timeout budget */
    protected static boolean shellOk(String cmd) {
        return runBounded(cmd, shellTimeoutSec(), null) == 0;
    }

    private static long shellTimeoutSec() {
        long t = (long) Config.chaosTimeoutSec;
        return t > 0 ? t : FALLBACK_TIMEOUT_SEC;
    }

    // ==================== small parse helpers ====================

    /** Split text into trimmed lines. */
    protected static List<String> lines(String text) {
        List<String> res = new ArrayList<String>();
        if (text == null || text.length() == 0) {
            return res;
        }
        BufferedReader r = new BufferedReader(new StringReader(text));
        try {
            String line;
            while ((line = r.readLine()) != null) {
                res.add(line.trim());
            }
        } catch (IOException ignored) {
            // StringReader never throws IOException
        } finally {
            try {
                r.close();
            } catch (IOException ignored) {
                // ignore
            }
        }
        return res;
    }

    /** @return value of a key=value pair in the snapshot payload, or null.
     *  Pairs may be separated by line breaks or by semicolons. */
    protected static String snapshotValue(String snapshot, String key) {
        if (snapshot == null) {
            return null;
        }
        for (String line : lines(snapshot)) {
            for (String seg : line.split(";")) {
                int eq = seg.indexOf('=');
                if (eq > 0 && key.equals(seg.substring(0, eq).trim())) {
                    return seg.substring(eq + 1).trim();
                }
            }
        }
        return null;
    }

    protected static int parseIntSafe(String s) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Throwable t) {
            return -1;
        }
    }

    protected static boolean truthy(String v) {
        if (v == null) {
            return false;
        }
        String t = v.trim();
        return "1".equals(t) || "true".equalsIgnoreCase(t);
    }

    /**
     * Shared restore for settings-backed channels: put the snapshot value back,
     * or delete the key when the device had no value for it.
     */
    protected static boolean putOrDeleteSetting(String ns, String key, String value) {
        if (value == null || value.length() == 0 || "null".equals(value)) {
            return shellOk("settings delete " + ns + " " + key);
        }
        return shellOk("settings put " + ns + " " + key + " " + value);
    }

    // ==================== static per-state registry ====================

    private static final Map<String, AbstractChaosEvent> sInstances = new LinkedHashMap<String, AbstractChaosEvent>();
    private static final Set<String> sAlive = new HashSet<String>();
    private static final Set<String> sDisabled = new HashSet<String>();
    private static final Set<String> sActive = new LinkedHashSet<String>();
    private static final Map<String, String> sSnapshots = new LinkedHashMap<String, String>();
    private static int sDroppedChaosEvents = 0;

    static synchronized void registerInstance(AbstractChaosEvent ev) {
        sInstances.put(ev.mStateName, ev);
    }

    /** @return all registered channel names, in registration order */
    static synchronized Set<String> channelNames() {
        return new LinkedHashSet<String>(sInstances.keySet());
    }

    /** @return true when the channel passed its pre-probe and is not disabled */
    static synchronized boolean isChannelUsable(String state) {
        return sAlive.contains(state) && !sDisabled.contains(state);
    }

    static synchronized void disableChannel(String state, String reason) {
        if (sDisabled.add(state)) {
            Logger.warningPrintln("[chaos] channel disabled for the rest of this run: "
                    + state + " (" + reason + ")");
        }
    }

    static synchronized void markActive(String state) {
        sActive.add(state);
    }

    /** @return number of currently perturbed states (maxConcurrent accounting) */
    static synchronized int activeStateCount() {
        return sActive.size();
    }

    static synchronized void putSnapshot(String state, String snapshot) {
        sSnapshots.put(state, snapshot);
    }

    static synchronized String getSnapshot(String state) {
        return sSnapshots.get(state);
    }

    static synchronized void noteDrop(String reason) {
        sDroppedChaosEvents++;
        Logger.warningPrintln("[chaos] event dropped (total " + sDroppedChaosEvents + "): " + reason);
    }

    /** @return number of chaos events dropped due to INJECT_FAIL (chaos counter) */
    static synchronized int droppedChaosEvents() {
        return sDroppedChaosEvents;
    }

    /** @return the Config probability for a state (max.chaos.<state>.pct) */
    protected static double pctFor(String state) {
        if ("battery".equals(state)) {
            return Config.chaosBatteryPct;
        }
        if ("powersave".equals(state)) {
            return Config.chaosPowerSavePct;
        }
        if ("bluetooth".equals(state)) {
            return Config.chaosBluetoothPct;
        }
        if ("location".equals(state)) {
            return Config.chaosLocationPct;
        }
        if ("mobiledata".equals(state)) {
            return Config.chaosMobileDataPct;
        }
        if ("vpn".equals(state)) {
            return Config.chaosVpnPct;
        }
        if ("dnd".equals(state)) {
            return Config.chaosDndPct;
        }
        if ("sysconfig".equals(state)) {
            return Config.chaosSysconfigPct;
        }
        return 0.0;
    }

    // ==================== orchestration (snapshot / schedule / restore) ====================

    /**
     * Startup pre-probe + snapshot of every enabled channel. Called once from
     * Monkey.run (after setActivityController) when chaos is enabled. A channel
     * that fails here is disabled for the whole run (degrade, never crash).
     */
    static synchronized void snapshotAll() {
        if (!Config.chaosEnable) {
            return;
        }
        for (AbstractChaosEvent ev : sInstances.values()) {
            String state = ev.mStateName;
            if (pctFor(state) <= 0.0) {
                continue;
            }
            if (sAlive.contains(state) || sDisabled.contains(state)) {
                continue;
            }
            String snap;
            try {
                if (!ev.probeChannel()) {
                    disableChannel(state, "probe failed (command unavailable on this ROM)");
                    continue;
                }
                snap = ev.snapshotState();
                if (snap == null || snap.length() == 0) {
                    disableChannel(state, "snapshot empty");
                    continue;
                }
            } catch (Throwable t) {
                disableChannel(state, "snapshot exception: " + t);
                continue;
            }
            sAlive.add(state);
            sSnapshots.put(state, snap);
            Logger.println("[chaos] channel ready: " + state + " snapshot=[" + snap + "]");
        }
        persistSnapshotFileLocked();
    }

    /**
     * Per-decision-cycle probability roll. Only called from
     * MonkeySourceApeNative.generateEvents(); the returned events MUST be
     * enqueued via addEvent (never injected directly from generateEvents).
     * Enforces max.chaos.maxConcurrent by counting already-active states plus
     * the picks of this roll.
     */
    static synchronized List<MonkeyEvent> scheduleOnce(Random random) {
        List<MonkeyEvent> picked = new ArrayList<MonkeyEvent>();
        if (!Config.chaosEnable || sAlive.isEmpty()) {
            return picked;
        }
        if (sActive.size() >= Config.chaosMaxConcurrent) {
            return picked;
        }
        for (String state : sInstances.keySet()) {
            AbstractChaosEvent ev = sInstances.get(state);
            if (ev.mActive || !sAlive.contains(state) || sDisabled.contains(state)) {
                continue;
            }
            double pct = pctFor(state);
            if (pct <= 0.0) {
                continue;
            }
            if (random.nextDouble() < pct) {
                picked.add(ev);
                if (sActive.size() + picked.size() >= Config.chaosMaxConcurrent) {
                    break;
                }
            }
        }
        return picked;
    }

    /**
     * Restore every perturbed state to its snapshot. Called from the finally
     * block in Monkey.run so it also executes on the throw path. Never throws;
     * per-channel failures are logged, not propagated.
     */
    static synchronized void restoreAll() {
        if (sActive.isEmpty()) {
            return;
        }
        for (String state : new LinkedHashSet<String>(sActive)) {
            AbstractChaosEvent ev = sInstances.get(state);
            if (ev == null) {
                continue;
            }
            String snap = sSnapshots.get(state);
            try {
                if (ev.restoreState(snap)) {
                    Logger.println("[chaos] restored " + state);
                } else {
                    Logger.warningPrintln("[chaos] restore failed for " + state
                            + " (snapshot=[" + snap + "])");
                }
            } catch (Throwable t) {
                Logger.warningPrintln("[chaos] restore exception for " + state + ": " + t);
            }
            ev.mActive = false;
        }
        sActive.clear();
    }

    /**
     * Persist current snapshots to /sdcard/fastbot_chaos.snapshot as evidence,
     * compatible with tools/schemas/chaos_snapshot.schema.json (written by the
     * org.json dependency already used by action/Action.java). Best-effort.
     */
    private static void persistSnapshotFileLocked() {
        if (sSnapshots.isEmpty()) {
            return;
        }
        Writer w = null;
        try {
            JSONObject root = new JSONObject();
            root.put("captured_at", isoNowUtc());
            JSONArray states = new JSONArray();
            for (Map.Entry<String, String> e : sSnapshots.entrySet()) {
                JSONObject o = new JSONObject();
                AbstractChaosEvent ev = sInstances.get(e.getKey());
                o.put("state", ev != null ? ev.mSchemaState : e.getKey());
                o.put("value", e.getValue());
                o.put("channel", "shell");
                states.put(o);
            }
            root.put("states", states);
            w = new FileWriter(new java.io.File(SNAPSHOT_PATH));
            w.write(root.toString());
            Logger.println("[chaos] snapshot persisted to " + SNAPSHOT_PATH);
        } catch (Throwable t) {
            Logger.warningPrintln("[chaos] snapshot persist failed (ignored): " + t);
        } finally {
            if (w != null) {
                try {
                    w.close();
                } catch (IOException ignored) {
                    // ignore
                }
            }
        }
    }

    private static String isoNowUtc() {
        SimpleDateFormat fmt = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US);
        fmt.setTimeZone(TimeZone.getTimeZone("UTC"));
        return fmt.format(new Date());
    }
}
