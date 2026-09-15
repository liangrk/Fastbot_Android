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
 * M1 chaos-injection channel: bluetooth. Toggles BT off via svc bluetooth so
 * apps with BT features hit their device-unavailable code paths.
 */
public class ChaosBluetoothEvent extends AbstractChaosEvent {

    public ChaosBluetoothEvent() {
        super("bluetooth", "bluetooth");
    }

    @Override
    protected boolean probeChannel() {
        return shellOk("settings get global bluetooth_on");
    }

    @Override
    protected String snapshotState() {
        String v = shellOut("settings get global bluetooth_on").trim();
        if (v.length() == 0) {
            return "";
        }
        return "bluetooth_on=" + v;
    }

    @Override
    protected boolean injectState() {
        return shellOk("svc bluetooth disable");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        String v = snapshotValue(snapshot, "bluetooth_on");
        boolean wasOn = truthy(v);
        return shellOk(wasOn ? "svc bluetooth enable" : "svc bluetooth disable");
    }
}
