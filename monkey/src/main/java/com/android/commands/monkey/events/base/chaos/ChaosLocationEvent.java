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
 * M1 chaos-injection channel: location. Flips the secure location_mode to 0
 * (off) so apps exercising location features hit their permission/availability
 * fallback paths. Snapshot value is restored at run end.
 */
public class ChaosLocationEvent extends AbstractChaosEvent {

    public ChaosLocationEvent() {
        super("location", "location");
    }

    @Override
    protected boolean probeChannel() {
        return shellOk("settings get secure location_mode");
    }

    @Override
    protected String snapshotState() {
        String v = shellOut("settings get secure location_mode").trim();
        if (v.length() == 0) {
            return "";
        }
        return "location_mode=" + v;
    }

    @Override
    protected boolean injectState() {
        return shellOk("settings put secure location_mode 0");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        String v = snapshotValue(snapshot, "location_mode");
        return putOrDeleteSetting("secure", "location_mode", v);
    }
}
