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
 * M1 chaos-injection channel: vpn. Forces always-on-VPN lockdown behavior via
 * global settings. The probe uses cmd connectivity, which many ROMs do not
 * expose, so this channel is EXPECTED to self-disable on those ROMs - that is
 * the designed degradation path, not a defect.
 */
public class ChaosVpnEvent extends AbstractChaosEvent {

    public ChaosVpnEvent() {
        super("vpn", "vpn");
    }

    @Override
    protected boolean probeChannel() {
        return shellOk("cmd connectivity");
    }

    @Override
    protected String snapshotState() {
        String app = shellOut("settings get global always_on_vpn_app").trim();
        String lock = shellOut("settings get global always_on_vpn_lockdown").trim();
        if (app.length() == 0 && lock.length() == 0) {
            return "";
        }
        return "always_on_vpn_app=" + app + ";always_on_vpn_lockdown=" + lock;
    }

    @Override
    protected boolean injectState() {
        return shellOk("settings put global always_on_vpn_lockdown 1");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        boolean ok = putOrDeleteSetting("global", "always_on_vpn_lockdown",
                snapshotValue(snapshot, "always_on_vpn_lockdown"));
        return ok && putOrDeleteSetting("global", "always_on_vpn_app",
                snapshotValue(snapshot, "always_on_vpn_app"));
    }
}
