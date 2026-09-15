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
 * M1 chaos-injection channel: battery. Unplugs the (virtual) charger and fakes
 * a critically low level so the target app's low-battery handling is exercised.
 * Uses only the standard dumpsys battery override surface (no root required).
 */
public class ChaosBatteryEvent extends AbstractChaosEvent {

    public ChaosBatteryEvent() {
        super("battery", "battery");
    }

    @Override
    protected boolean probeChannel() {
        // harmless read-only query form of the channel command
        return shellOk("dumpsys battery");
    }

    @Override
    protected String snapshotState() {
        String out = shellOut("dumpsys battery");
        int level = -1;
        String ac = "?";
        String usb = "?";
        for (String line : lines(out)) {
            if (line.startsWith("level:")) {
                level = parseIntSafe(line.substring(6));
            } else if (line.startsWith("AC powered:")) {
                ac = line.substring(11).trim();
            } else if (line.startsWith("USB powered:")) {
                usb = line.substring(12).trim();
            }
        }
        if (level < 0) {
            return "";
        }
        return "level=" + level + ";ac=" + ac + ";usb=" + usb;
    }

    @Override
    protected boolean injectState() {
        boolean ok = shellOk("dumpsys battery unplug");
        return ok && shellOk("dumpsys battery set level 15");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        // dumpsys battery reset clears every override (level, unplug, status)
        return shellOk("dumpsys battery reset");
    }
}
