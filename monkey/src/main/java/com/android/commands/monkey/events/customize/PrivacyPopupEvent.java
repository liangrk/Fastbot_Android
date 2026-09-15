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

package com.android.commands.monkey.events.customize;

import android.graphics.PointF;
import android.os.SystemClock;
import android.view.MotionEvent;

import com.android.commands.monkey.events.CustomEvent;
import com.android.commands.monkey.events.MonkeyEvent;
import com.android.commands.monkey.events.base.MonkeyTouchEvent;
import com.android.commands.monkey.events.base.MonkeyWaitEvent;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.List;

/**
 * M4 privacy-compliance consent/deny click. A one-shot tap event for the
 * widget that triggered a privacy rule hit. Modeled after the customize
 * ClickEvent: generateMonkeyEvents() expands into the standard
 * touch-down/wait/touch-up sequence, and the caller enqueues every
 * MonkeyEvent via addEvent so the click flows getNextEvent -> the P0 guard
 * (catch Exception | LinkageError) in Monkey - it is never injected
 * directly from the decision path.
 */
public class PrivacyPopupEvent extends AbstractCustomEvent {

    private static final long serialVersionUID = 1L;

    private final float x;
    private final float y;
    private final long waitTime;
    private final String ruleName;
    private final String action;

    public PrivacyPopupEvent(float x, float y, long waitTime, String ruleName, String action) {
        this.x = x;
        this.y = y;
        this.waitTime = waitTime;
        this.ruleName = ruleName;
        this.action = action;
    }

    public PrivacyPopupEvent(PointF point, long waitTime, String ruleName, String action) {
        this(point.x, point.y, waitTime, ruleName, action);
    }

    public PointF getPoint() {
        return new PointF(this.x, this.y);
    }

    public long getWaitTime() {
        return this.waitTime;
    }

    public String getRuleName() {
        return this.ruleName;
    }

    public String getAction() {
        return this.action;
    }

    @Override
    public List<MonkeyEvent> generateMonkeyEvents() {
        long downAt = SystemClock.uptimeMillis();
        MonkeyEvent down = new MonkeyTouchEvent(MotionEvent.ACTION_DOWN)
                .setDownTime(downAt).addPointer(0, x, y).setIntermediateNote(false);
        MonkeyEvent wait = new MonkeyWaitEvent(waitTime);

        MonkeyEvent up = new MonkeyTouchEvent(MotionEvent.ACTION_UP)
                .setDownTime(downAt).addPointer(0, x, y).setIntermediateNote(false);
        if (waitTime == 0) {
            return Arrays.asList(down, up);
        }
        return Arrays.asList(down, wait, up);
    }

    @Override
    public JSONObject toJSONObject() throws JSONException {
        JSONObject jEvent = new JSONObject();
        jEvent.put("type", "privacyPopup");
        jEvent.put("waitTime", String.valueOf(waitTime));
        jEvent.put("x", String.valueOf(x));
        jEvent.put("y", String.valueOf(y));
        if (ruleName != null) {
            jEvent.put("rule", ruleName);
        }
        if (action != null) {
            jEvent.put("action", action);
        }
        return jEvent;
    }
}
