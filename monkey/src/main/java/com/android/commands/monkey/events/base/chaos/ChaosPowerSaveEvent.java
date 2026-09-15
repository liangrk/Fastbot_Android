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

/**
 * M1 chaos-injection channel: powersave. Forces the device into low-power mode
 * (settings put global low_power 1) so apps under test hit their
 * power-restriction code paths (job skipping, network throttling, ...).
 */
public class ChaosPowerSaveEvent extends AbstractChaosEvent {

    public ChaosPowerSaveEvent() {
        super("powersave", "power_save");
    }

    @Override
    protected boolean probeChannel() {
        return shellOk("settings get global low_power");
    }

    @Override
    protected String snapshotState() {
        String v = shellOut("settings get global low_power").trim();
        if (v.length() == 0) {
            return "";
        }
        return "low_power=" + v;
    }

    @Override
    protected boolean injectState() {
        return shellOk("settings put global low_power 1");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        return putOrDeleteSetting("global", "low_power", snapshotValue(snapshot, "low_power"));
    }
}
