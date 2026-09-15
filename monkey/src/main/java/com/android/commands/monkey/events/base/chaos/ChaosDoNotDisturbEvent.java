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
 * M1 chaos-injection channel: dnd (Do Not Disturb). Enables zen priority mode
 * via cmd notification set_dnd so apps with notification/alarms handling get
 * exercised under a muted notification environment. Snapshot value is put
 * back at run end.
 */
public class ChaosDoNotDisturbEvent extends AbstractChaosEvent {

    public ChaosDoNotDisturbEvent() {
        super("dnd", "dnd");
    }

    @Override
    protected boolean probeChannel() {
        return shellOk("cmd notification");
    }

    @Override
    protected String snapshotState() {
        String v = shellOut("settings get global zen_mode").trim();
        if (v.length() == 0) {
            return "";
        }
        return "zen_mode=" + v;
    }

    @Override
    protected boolean injectState() {
        return shellOk("cmd notification set_dnd priority");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        boolean ok = shellOk("cmd notification set_dnd off");
        String v = snapshotValue(snapshot, "zen_mode");
        return ok && putOrDeleteSetting("global", "zen_mode", v);
    }
}
