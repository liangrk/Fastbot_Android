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

package com.android.commands.monkey.events.base;

import com.android.commands.monkey.utils.Logger;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.concurrent.TimeUnit;

/**
 * M2 perf-metrics: device-side, collection-only frame sampler.
 *
 * Modeled on MonkeyGetAppFrameRateEvent (dumpsys gfxinfo parsing, SD-card
 * output) but never enqueued as a MonkeyEvent: it is invoked directly from
 * MonkeySourceApeNative.maybeSamplePerf() behind an elapsed-time gate, the
 * same way the coverage exporter is. Hard constraints:
 * <ul>
 *   <li>collection-only and cheap: no threads; the dumpsys child is bounded
 *       via available()-gated reads + Process.waitFor(long,TimeUnit) polling
 *       + destroy() on deadline (the AbstractChaosEvent.runBounded technique
 *       - a bare waitFor() would stall the decision thread).</li>
 *   <li>failures never escape: exec/parse/write problems are warned and the
 *       sample is skipped; the fuzzing loop is never affected.</li>
 * </ul>
 *
 * Output: one JSON line per sample appended to
 * /sdcard/fastbot_perf/&lt;runid&gt;.jsonl, conforming to
 * tools/schemas/perf_frame.schema.json (ts, optional activity, fps,
 * janky_frames, p90_ms - no other keys).
 *
 * fps semantics: dumpsys gfxinfo reports cumulative "Total frames rendered"
 * since process start, so a rate needs two samples. The first interval after
 * Fastbot start (or after an app restart) is a warm-up and produces no line;
 * a counter decrease (app restart detected) re-baselines to zero instead of
 * emitting a negative fps.
 */
public final class PerfFrameEvent {

    private static final String TAG = "[perf]";
    /** evidence dir on the device sdcard */
    private static final String PERF_DIR = "/sdcard/fastbot_perf";
    /** max bytes of stdout captured per gfxinfo dump */
    private static final int MAX_CAPTURE = 65536;
    /** poll interval while the gfxinfo child produces no output */
    private static final long POLL_MS = 25L;
    /** wall-time budget of one dumpsys gfxinfo invocation */
    private static final long SAMPLE_TIMEOUT_SEC = 2L;

    /** run id: a timestamp captured once per Fastbot process start */
    private static volatile String sRunId;
    /** previous cumulative frame counter (for the fps delta window) */
    private static long sPrevTotalFrames = -1L;
    /** timestamp (wall clock) of the previous accepted sample */
    private static long sPrevTsMs = 0L;
    /** warned-once flag for a missing target package */
    private static boolean sWarnedNoPkg = false;

    private PerfFrameEvent() {
        // static-only class; not a MonkeyEvent, never enqueued
    }

    /**
     * Take one frame sample (bounded dumpsys + parse + append). Called from
     * the decision thread behind the elapsed-time gate in
     * MonkeySourceApeNative.maybeSamplePerf(). Never throws.
     */
    public static void sampleOnce(String pkg, String activity) {
        try {
            doSample(pkg, activity);
        } catch (Throwable t) {
            Logger.warningPrintln(TAG + " sample failed, skipped: " + t);
        }
    }

    private static void doSample(String pkg, String activity) throws IOException, JSONException {
        if (pkg == null || pkg.length() == 0) {
            if (!sWarnedNoPkg) {
                Logger.warningPrintln(TAG + " no target package, perf frame sampling disabled");
                sWarnedNoPkg = true;
                sPrevTotalFrames = -1L;
            }
            return;
        }
        StringBuilder out = new StringBuilder();
        String cmd = "dumpsys gfxinfo " + pkg + " framestats";
        int code = runBounded(cmd, SAMPLE_TIMEOUT_SEC, out);
        long ts = System.currentTimeMillis();
        if (code != 0) {
            Logger.warningPrintln(TAG + " dumpsys failed (exit " + code + "), sample skipped: " + cmd);
            sPrevTotalFrames = -1L;
            sPrevTsMs = 0L;
            return;
        }
        Long total = firstLongAfter(out.toString(), "Total frames rendered:");
        Long janky = firstLongAfter(out.toString(), "Janky frames:");
        Long p90 = firstLongAfter(out.toString(), "90th percentile:");
        if (total == null || janky == null || p90 == null) {
            Logger.warningPrintln(TAG + " gfxinfo stats not parseable, sample skipped");
            sPrevTotalFrames = -1L;
            sPrevTsMs = 0L;
            return;
        }
        Double fps = computeFps(total.longValue(), ts);
        if (fps == null) {
            // first accepted window: no rate computable yet (see class doc)
            return;
        }
        appendLine(ts, activity, fps.doubleValue(), janky.longValue(), p90.longValue());
    }

    /**
     * Frame rate over the sample window from cumulative counters. Returns
     * null for the first window (no baseline), and re-baselines when the
     * counter decreased (app restart): fps is then computed from the new
     * process count over the same window, never negative.
     */
    private static Double computeFps(long totalFrames, long tsMs) {
        if (sPrevTotalFrames < 0L) {
            sPrevTotalFrames = totalFrames;
            sPrevTsMs = tsMs;
            return null;
        }
        long dtMs = tsMs - sPrevTsMs;
        if (dtMs <= 0L) {
            // wall clock went backwards; keep baseline, skip this window
            return null;
        }
        long frames;
        if (totalFrames < sPrevTotalFrames) {
            // app restarted: count the new process frames over the window
            frames = totalFrames;
        } else {
            frames = totalFrames - sPrevTotalFrames;
        }
        sPrevTotalFrames = totalFrames;
        sPrevTsMs = tsMs;
        return (frames * 1000.0) / dtMs;
    }

    /**
     * Parse the first long integer that follows the given marker string in
     * the dumpsys text, or null when the marker is absent/numberless.
     */
    static Long firstLongAfter(String text, String marker) {
        int idx = text.indexOf(marker);
        if (idx < 0) {
            return null;
        }
        int i = idx + marker.length();
        while (i < text.length() && !Character.isDigit(text.charAt(i))) {
            if (text.charAt(i) == 10) {
                // end-of-line reached before any digit: not the value line
                return null;
            }
            i++;
        }
        if (i >= text.length()) {
            return null;
        }
        int start = i;
        while (i < text.length() && Character.isDigit(text.charAt(i)) ) {
            i++;
        }
        try {
            return Long.parseLong(text.substring(start, i));
        } catch (NumberFormatException nfe) {
            return null;
        }
    }

    /**
     * Append one JSON evidence line to /sdcard/fastbot_perf/&lt;runid&gt;.jsonl.
     * The run id is captured once per Fastbot process start (lazily). Write
     * failures are warned and dropped, never propagated.
     */
    private static void appendLine(long ts, String activity, double fps,
                                   long janky, double p90) throws JSONException {
        JSONObject json = new JSONObject();
        json.put("ts", ts);
        if (activity != null && activity.length() > 0) {
            json.put("activity", activity);
        }
        json.put("fps", Math.round(fps * 100.0) / 100.0);
        json.put("janky_frames", (int) Math.min(janky, 2147483647L));
        json.put("p90_ms", p90);
        File dir = new File(PERF_DIR);
        if (!dir.exists() && !dir.mkdirs() && !dir.exists()) {
            Logger.warningPrintln(TAG + " cannot create " + PERF_DIR + ", sample dropped");
            return;
        }
        FileWriter writer = null;
        try {
            writer = new FileWriter(new File(dir, runId() + ".jsonl"), true);
            writer.write(json.toString());
            writer.write("\n");
        } catch (IOException e) {
            Logger.warningPrintln(TAG + " write failed, sample dropped: " + e);
        } finally {
            if (writer != null) {
                try {
                    writer.close();
                } catch (IOException e) {
                    Logger.warningPrintln(TAG + " close failed: " + e);
                }
            }
        }
    }

    /** Run id: timestamp captured once per Fastbot process, lazily. */
    private static String runId() {
        if (sRunId == null) {
            sRunId = new SimpleDateFormat("yyyyMMdd_HHmmss").format(new Date());
        }
        return sRunId;
    }

    // ==================== bounded shell execution ====================
    // Mirrors AbstractChaosEvent.runBounded (M1) - same technique, kept
    // self-contained so the chaos package is untouched by M2.

    /**
     * Timeout-bounded shell execution: available()-gated stdout reads (a read
     * on an empty pipe blocks until the child exits, so reads are only issued
     * when bytes are pending) + Process.waitFor(long,TimeUnit) polling +
     * nanoTime deadline + destroy() on timeout. No threads, no bare waitFor().
     *
     * @return child exit code, or -1 on timeout/exception/interrupt
     */
    private static int runBounded(String cmd, long timeoutSec, StringBuilder out) {
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
                    Logger.warningPrintln(TAG + " timeout after " + timeoutSec + "s: " + cmd);
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
            } catch (IllegalThreadStateException ise) {
                // unreachable after a successful bounded waitFor; kept for safety
                return -1;
            }
        } catch (IOException e) {
            Logger.warningPrintln(TAG + " exec failed: " + cmd + " : " + e);
            if (proc != null) {
                proc.destroy();
            }
            return -1;
        }
    }
}
