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
 * M1 chaos-injection channel: sysconfig. Safe subset only: dark mode (cmd
 * uimode night) plus font scale, per the plan's "safe subset" restriction for
 * system configuration perturbation.
 */
public class ChaosSystemConfigEvent extends AbstractChaosEvent {

    public ChaosSystemConfigEvent() {
        super("sysconfig", "system_config");
    }

    @Override
    protected boolean probeChannel() {
        return shellOk("cmd uimode night");
    }

    @Override
    protected String snapshotState() {
        String night = shellOut("cmd uimode night").trim();
        boolean isNight = night.contains("yes");
        String font = shellOut("settings get system font_scale").trim();
        if (night.length() == 0 && font.length() == 0) {
            return "";
        }
        return "night_mode=" + (isNight ? "yes" : "no") + ";font_scale=" + font;
    }

    @Override
    protected boolean injectState() {
        boolean ok = shellOk("cmd uimode night yes");
        return ok && shellOk("settings put system font_scale 1.3");
    }

    @Override
    protected boolean restoreState(String snapshot) {
        String night = snapshotValue(snapshot, "night_mode");
        boolean ok = shellOk("cmd uimode night " + ("yes".equals(night) ? "yes" : "no"));
        String font = snapshotValue(snapshot, "font_scale");
        if (font == null || font.length() == 0 || "null".equals(font)) {
            font = "1.0";
        }
        return ok && shellOk("settings put system font_scale " + font);
    }
}
