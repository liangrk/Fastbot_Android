/*
 * Copyright (c) 2026 Bytedance Inc.
 *
 * Licensed under the Apache License, Version 2.0 only
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

import com.android.commands.monkey.utils.Logger;
import com.android.commands.monkey.utils.Config;

import org.json.JSONObject;

import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.io.Writer;

/**
 * M4 privacy-compliance audit trail. Appends one JSON line per privacy rule
 * hit to /sdcard/fastbot_privacy/audit.jsonl, conforming to
 * tools/schemas/audit.schema.json (ts, type=rule_hit, activity, widget,
 * detail, screenshot, source=device). Because the frozen schema forbids
 * additional properties, rule/action/permission go into the required
 * "detail" field as key=value pairs:
 *     rule=<displayName>;action=<resolved>;permission=<tag>
 * (permission pair omitted when the rule has none).
 *
 * Auditing must NEVER interrupt exploration: every write is wrapped in
 * try-catch(Throwable) and only logged via Logger.warningPrintln.
 */
public final class PrivacyAuditor {

    /** Root directory for audit trail + hit screenshots. */
    public static final String OUTPUT_DIR = "/sdcard/fastbot_privacy";
    /** JSONL audit file (one audit.schema.json object per line). */
    public static final String AUDIT_FILE = OUTPUT_DIR + "/audit.jsonl";

    private PrivacyAuditor() {
    }

    /**
     * Append one rule-hit audit record. Fully guarded: any Throwable is
     * downgraded to a warning, never propagated to the decision loop.
     */
    public static void audit(PrivacyRuleEngine.PrivacyRule rule, String activityName,
            String screenshotPath) {
        try {
            JSONObject record = new JSONObject();
            record.put("ts", System.currentTimeMillis());
            record.put("type", "rule_hit");
            record.put("activity", activityName == null ? "" : activityName);
            if (rule.widget != null) {
                record.put("widget", rule.widget);
            }
            StringBuilder detail = new StringBuilder();
            detail.append("rule=").append(rule.displayName());
            detail.append(";action=").append(rule.resolveAction(Config.privacyDefaultAction));
            if (rule.permission != null) {
                detail.append(";permission=").append(rule.permission);
            }
            record.put("detail", detail.toString());
            if (screenshotPath != null) {
                record.put("screenshot", screenshotPath);
            }
            record.put("source", "device");
            appendLine(record.toString());
        } catch (Throwable t) {
            Logger.warningPrintln("[privacy] audit write failed (ignored): " + t);
        }
    }

    /**
     * Append one pre-serialized JSON line. Guarded here as well so callers
     * cannot be broken by IO failures (disk full, permission denied, ...).
     */
    static void appendLine(String line) {
        Writer writer = null;
        try {
            File dir = new File(OUTPUT_DIR);
            if (!dir.exists()) {
                dir.mkdirs();
            }
            writer = new FileWriter(new File(AUDIT_FILE), true);
            writer.write(line);
            writer.write("\n");
            writer.flush();
        } catch (IOException e) {
            Logger.warningPrintln("[privacy] audit append failed (ignored): " + e);
        } catch (Throwable t) {
            Logger.warningPrintln("[privacy] audit append failed (ignored): " + t);
        } finally {
            if (writer != null) {
                try {
                    writer.close();
                } catch (IOException ignored) {
                    // best effort close; stream is already flushed
                }
            }
        }
    }
}
