/*
 * Copyright (c) 2026 Bytedance Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except already in compliance with the License.
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
 * M1 chaos-injection channel: mobiledata. Disables mobile data via svc data so
 * network-dependent apps hit offline/degraded-network code paths.
 */
public class ChaosMobileDataEvent extends AbstractChaosEvent {

    public ChaosMobileDataEvent() {
        super("mobiledata", "mobile_data");
    }

    @Override
    protected boolean probeChannel() {
        return shellOk("settings get global mobile_data");
    }

    @Override
    protected String snapshotState() {
        String v = shellOut("settings get global mobile_data").trim();
        if (v.length() == 0) {
            return "";
        }
        return "mobile_data=" + v;
    }

    @Override
    protected boolean injectState() {
        return shellOk("svc data disable");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        String v = snapshotValue(snapshot, "mobile_data");
        boolean wasOn = truthy(v);
        return shellOk(wasOn ? "svc data enable" : "svc data disable");
    }
}
