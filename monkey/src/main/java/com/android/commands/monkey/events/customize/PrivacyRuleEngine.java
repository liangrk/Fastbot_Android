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

import com.android.commands.monkey.utils.Config;
import com.android.commands.monkey.utils.Logger;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * M4 privacy-compliance rule engine. Parses the JSON rules file referenced
 * by max.privacy.rules and matches it against (activityName, guiXml) pairs.
 *
 * Rule semantics (mirrored by tools/privacy_rules.py, the PC-side reference
 * implementation):
 * <ul>
 *   <li>Rules are evaluated strictly in file order; the first rule whose
 *       page regex matches the activity name wins.</li>
 *   <li>page: mandatory Java regular expression, matched with find()
 *       semantics (search, not full match) against the top activity class
 *       name.</li>
 *   <li>widget: optional Java regular expression, matched with find()
 *       semantics against the XML string produced by
 *       TreeBuilder.dumpDocumentStrWithOutTree. A rule without widget
 *       matches every rendering of its page (audit-only hit).</li>
 *   <li>action: optional "consent" or "deny"; when a rule omits it the
 *       engine falls back to max.privacy.defaultAction.</li>
 *   <li>name / permission: optional free-form strings, recorded in the
 *       audit trail for reporting.</li>
 * </ul>
 *
 * All patterns are precompiled at load time so the per-decision-cycle
 * match cost is one pass of cheap regex searches. The engine only loads
 * when max.privacy.enabled=true; an unconfigured run never touches this
 * class (zero overhead when off). The engine never injects anything
 * itself; the caller enqueues clicks via addEvent.
 */
public final class PrivacyRuleEngine {

    /** JSON rules file format: [{"page","widget","action","name","permission"}] */
    public static final String[] VALID_ACTIONS = {"consent", "deny"};

    private static final Object sLock = new Object();
    private static volatile PrivacyRuleEngine sEngine;

    private final List<PrivacyRule> mRules;
    private final String mDefaultAction;

    private PrivacyRuleEngine(List<PrivacyRule> rules, String defaultAction) {
        this.mRules = Collections.unmodifiableList(rules);
        this.mDefaultAction = defaultAction;
    }

    /** Number of loaded rules (0 = matching disabled). */
    public int size() {
        return mRules.size();
    }

    /** The configured fallback action for rules that omit "action". */
    public String getDefaultAction() {
        return mDefaultAction;
    }

    /**
     * Parse and compile a rules file body. Throws JSONException or
     * IllegalArgumentException on any structural problem, so the caller
     * decides how to degrade.
     */
    public static PrivacyRuleEngine load(String jsonText) throws JSONException {
        String defaultAction = Config.privacyDefaultAction;
        List<PrivacyRule> rules = new ArrayList<PrivacyRule>();
        JSONArray array = new JSONArray(jsonText);
        for (int i = 0; i < array.length(); i++) {
            rules.add(PrivacyRule.fromJSON(i + 1, array.getJSONObject(i), defaultAction));
        }
        return new PrivacyRuleEngine(rules, defaultAction);
    }

    /**
     * Load the rules file referenced by max.privacy.rules. FileInputStream
     * (not java.nio) keeps the runtime requirement at the minSdk 22 baseline.
     */
    public static PrivacyRuleEngine loadFromFile(String path)
            throws IOException, JSONException {
        InputStream in = new FileInputStream(new File(path));
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            int read;
            while ((read = in.read(buffer)) > 0) {
                out.write(buffer, 0, read);
            }
            return load(new String(out.toByteArray(), "UTF-8"));
        } finally {
            in.close();
        }
    }

    /**
     * Lazily create the process-wide engine once (first enabled decision
     * cycle). Never throws: a missing/unparsable rules file degrades to an
     * empty engine and is cached, so a broken file neither retries every
     * cycle nor interrupts exploration.
     */
    private static PrivacyRuleEngine createEngine() {
        String path = Config.privacyRulesPath;
        if (path == null || path.trim().length() == 0) {
            Logger.warningPrintln("[privacy] max.privacy.rules is empty, matching disabled");
            return new PrivacyRuleEngine(new ArrayList<PrivacyRule>(), Config.privacyDefaultAction);
        }
        try {
            PrivacyRuleEngine engine = loadFromFile(path);
            Logger.println("[privacy] loaded " + engine.size() + " rule(s) from " + path);
            return engine;
        } catch (Throwable t) {
            Logger.warningPrintln("[privacy] rules load failed, matching disabled: " + t);
            return new PrivacyRuleEngine(new ArrayList<PrivacyRule>(), Config.privacyDefaultAction);
        }
    }

    /**
     * First matching rule in file order, or null. Entry point used once per
     * decision cycle; engines are compiled at first use and reused.
     */
    public static PrivacyRule match(String activityName, String xml) {
        PrivacyRuleEngine engine = sEngine;
        if (engine == null) {
            synchronized (sLock) {
                if (sEngine == null) {
                    sEngine = createEngine();
                }
                engine = sEngine;
            }
        }
        return engine.matchRules(activityName, xml);
    }

    /** Instance-level file-order match; package-private for tests. */
    PrivacyRule matchRules(String activityName, String xml) {
        for (PrivacyRule rule : mRules) {
            if (!rule.matchesActivity(activityName)) {
                continue;
            }
            if (rule.widgetPattern == null || rule.matchesXml(xml)) {
                return rule;
            }
        }
        return null;
    }

    /** One compiled privacy rule. */
    public static final class PrivacyRule {
        /** 1-based position in the rules file. */
        public final int index;
        /** Raw page regex source. */
        public final String page;
        /** Raw widget regex source, or null for page-only (audit-only) rules. */
        public final String widget;
        /** Raw action, or null when the rule defers to the default action. */
        public final String action;
        /** Optional free-form permission tag for reporting, or null. */
        public final String permission;
        final Pattern pagePattern;
        final Pattern widgetPattern;
        private final String mName;

        private PrivacyRule(int index, String name, String page, String widget,
                String action, String permission, Pattern pagePattern, Pattern widgetPattern) {
            this.index = index;
            this.mName = name;
            this.page = page;
            this.widget = widget;
            this.action = action;
            this.permission = permission;
            this.pagePattern = pagePattern;
            this.widgetPattern = widgetPattern;
        }

        static PrivacyRule fromJSON(int index, JSONObject obj, String defaultAction)
                throws JSONException {
            if (obj == null) {
                throw new IllegalArgumentException("rule #" + index + " is not an object");
            }
            String page = obj.optString("page", "");
            if (page == null || page.trim().length() == 0) {
                throw new IllegalArgumentException("rule #" + index + ": missing page");
            }
            String widget = obj.has("widget") && !obj.isNull("widget")
                    ? obj.getString("widget") : null;
            String action = obj.has("action") && !obj.isNull("action")
                    ? obj.getString("action") : null;
            if (action != null && !isValidAction(action)) {
                throw new IllegalArgumentException(
                        "rule #" + index + ": action must be one of " + Arrays.toString(VALID_ACTIONS));
            }
            String name = obj.has("name") && !obj.isNull("name")
                    ? obj.getString("name") : null;
            String permission = obj.has("permission") && !obj.isNull("permission")
                    ? obj.getString("permission") : null;
            try {
                Pattern pagePattern = Pattern.compile(page);
                Pattern widgetPattern = widget == null ? null : Pattern.compile(widget);
                return new PrivacyRule(index, name, page, widget, action, permission,
                        pagePattern, widgetPattern);
            } catch (IllegalArgumentException e) {
                // Pattern.compile signals bad regex with IllegalArgumentException
                throw new IllegalArgumentException(
                        "rule #" + index + ": invalid regex - " + e.getMessage());
            }
        }

        public static boolean isValidAction(String action) {
            for (String candidate : VALID_ACTIONS) {
                if (candidate.equals(action)) {
                    return true;
                }
            }
            return false;
        }

        /** Human-readable identity used in logs and audit records. */
        public String displayName() {
            return mName != null && mName.length() > 0 ? mName : ("rule#" + index);
        }

        /** Resolved action with max.privacy.defaultAction fallback. */
        public String resolveAction(String defaultAction) {
            return action != null ? action : defaultAction;
        }

        public boolean matchesActivity(String activityName) {
            return activityName != null
                    && pagePattern.matcher(activityName).find();
        }

        public boolean matchesXml(String xml) {
            return xml != null && widgetPattern != null
                    && widgetPattern.matcher(xml).find();
        }

        /** Log/audit friendly one-line description. */
        public String describe() {
            StringBuilder sb = new StringBuilder(displayName());
            sb.append("(page=").append(page);
            if (widget != null) {
                sb.append(", widget=").append(widget);
            }
            if (action != null) {
                sb.append(", action=").append(action);
            }
            if (permission != null) {
                sb.append(", permission=").append(permission);
            }
            return sb.append(")").toString();
        }

        /**
         * Click target for the matched widget: center of the first
         * bounds attribute at or after the widget match. TreeBuilder writes
         * bounds as the LAST attribute of every node, so for attribute-level
         * matches this is the enclosing node's own bounds; for page-only
         * rules (no widget) there is nothing to click and this returns null.
         * Returns {x, y} or null.
         */
        public int[] findCenterInXml(String xml) {
            if (widgetPattern == null || xml == null) {
                return null;
            }
            Matcher m = widgetPattern.matcher(xml);
            if (!m.find()) {
                return null;
            }
            return findBoundsCenterAfter(xml, m.start());
        }

        /** Center of the first bounds="..." attribute at or after from. */
        static int[] findBoundsCenterAfter(String xml, int from) {
            int idx = xml.indexOf("bounds=", from);
            while (idx >= 0) {
                int valueStart = idx + "bounds=".length();
                if (valueStart < xml.length() && xml.charAt(valueStart) == '"') {
                    int valueEnd = xml.indexOf('"', valueStart + 1);
                    if (valueEnd > valueStart) {
                        return parseBoundsCenter(xml.substring(valueStart + 1, valueEnd));
                    }
                }
                idx = xml.indexOf("bounds=", valueStart);
            }
            return null;
        }

        /** Parses "[l,t][r,b]" into its center point, or null. */
        static int[] parseBoundsCenter(String value) {
            if (value == null || value.length() == 0 || value.charAt(0) != '[') {
                return null;
            }
            try {
                int comma1 = value.indexOf(',');
                int mid = value.indexOf(']');
                int open2 = value.indexOf('[', mid + 1);
                int comma2 = value.indexOf(',', open2);
                int end = value.indexOf(']', open2);
                if (comma1 < 1 || mid < comma1 || open2 < mid
                        || comma2 < open2 || end < comma2) {
                    return null;
                }
                int left = Integer.parseInt(value.substring(1, comma1));
                int top = Integer.parseInt(value.substring(comma1 + 1, mid));
                int right = Integer.parseInt(value.substring(open2 + 1, comma2));
                int bottom = Integer.parseInt(value.substring(comma2 + 1, end));
                return new int[]{(left + right) / 2, (top + bottom) / 2};
            } catch (NumberFormatException e) {
                return null;
            }
        }
    }
}
