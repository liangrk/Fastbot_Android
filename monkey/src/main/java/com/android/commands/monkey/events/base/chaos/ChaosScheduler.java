/*
 * Copyright (c) 2026 Bytedance Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
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

import com.android.commands.monkey.events.MonkeyEvent;
import com.android.commands.monkey.utils.Logger;

import java.util.List;
import java.util.Random;

/**
 * M1 chaos-injection scheduler. Owns the 8 channel instances (created in the
 * static initializer, so the chaos classes only load when chaos is enabled)
 * and the per-decision-cycle probability roll.
 *
 * Entry points (all safe to call when chaos is off):
 * <ul>
 *   <li>{@link #rollOnce(Random)} - from MonkeySourceApeNative.generateEvents(),
 *       once per decision cycle. Returns the events to enqueue via addEvent;
 *       never injects anything itself.</li>
 *   <li>{@link #snapshotAll()} - from Monkey.run, after setActivityController.
 *       Pre-probes every enabled channel once and captures its snapshot; a
 *       failing channel is disabled for the run (degrade, never crash).</li>
 * </ul>
 */
public final class ChaosScheduler {

    private ChaosScheduler() {
    }

    static {
        // construction registers each channel in AbstractChaosEvent's registry
        new ChaosBatteryEvent();
        new ChaosPowerSaveEvent();
        new ChaosBluetoothEvent();
        new ChaosLocationEvent();
        new ChaosMobileDataEvent();
        new ChaosVpnEvent();
        new ChaosDoNotDisturbEvent();
        new ChaosSystemConfigEvent();
    }

    /**
     * Per-decision-cycle probability roll for every enabled, alive, not-yet-active
     * channel. Enforcement of max.chaos.maxConcurrent counts active states plus
     * the picks of this roll. Callers enqueue the result via addEvent so the
     * events flow getNextEvent and then the P0 guard in Monkey.
     *
     * @param random the source's java.util.Random (seed-reproducible scheduling)
     * @return events to enqueue (possibly empty; never null)
     */
    public static List<MonkeyEvent> rollOnce(Random random) {
        return AbstractChaosEvent.scheduleOnce(random);
    }

    /**
     * Startup pre-probe + snapshot of every enabled channel. Called from
     * Monkey.run after setActivityController when max.chaos.enable is true.
     */
    public static void snapshotAll() {
        AbstractChaosEvent.snapshotAll();
    }

    /**
     * Restore every perturbed channel to its pre-task snapshot. Called from the
     * finally block in Monkey.run (throw path included); swallows Throwable so
     * a restore failure never masks the original exception.
     */
    public static void restoreAll() {
        try {
            AbstractChaosEvent.restoreAll();
        } catch (Throwable t) {
            Logger.warningPrintln("[chaos] restoreAll failed: " + t);
        }
    }
}
