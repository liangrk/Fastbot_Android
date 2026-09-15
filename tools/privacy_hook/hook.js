/*
 * Fastbot M4 privacy extension (plan item M4.5, optional, rooted devices only).
 * Frida sensitive-API monitor injected into the target app.
 *
 * What it does
 *   Hooks the sensitive Java APIs of tools/privacy_hook/apis.json (Java layer via
 *   Java.use) and, on each hit, emits ONE line to stdout:
 *
 *     @@AUDIT@@{"ts": 1789500000000, "type": "sensitive_api", "activity": "...",
 *               "detail": "api=...; permission= data; package=...; args=...", "source": "frida"}
 *
 *   PC-side tools/privacy_hook/collect.py filters stdout on the @@AUDIT@@ prefix,
 *   validates loosely and appends records conforming to tools/schemas/audit.schema.json
 *   (missing optional fields widget/screenshot are filled with null there).
 *
 * Usage
 *   frida -U -f <package> -l hook.js       (spawn, recommended)
 *   frida -U -n <process> -l hook.js       (attach)
 *   ...or via tools/privacy_hook/collect.py, which wraps this script with the
 *   apis.json manifest injected as globalThis.APIS_OVERRIDE.
 *
 * Manifest resolution (first hit wins):
 *   1. globalThis.APIS_OVERRIDE  - manifest object injected by collect.py's -l wrapper
 *   2. MANIFEST_DEVICE_FILE constant below: DEVICE path of a JSON file with the
 *      same shape as apis.json ({"apis": [...]})
 *   3. FALLBACK_APIS             - embedded default list, mirrors apis.json, monitors all
 *
 * Configuration via constants (documented; edit before use):
 *   MANIFEST_DEVICE_FILE - '' (default) = monitor ALL APIs of the default list.
 *                          Example: var MANIFEST_DEVICE_FILE = '/data/local/tmp/fastbot_apis.json';
 *   AUDIT_PREFIX         - line prefix collect.py filters on (keep in sync with collect.py).
 *   ARGS_LIMIT           - max chars of the args summary inside detail.
 *
 * Field notes (audit.schema.json is frozen, additionalProperties=false):
 *   - type is "sensitive_api" - the schema's enum value for an API hit
 *     (audit.schema.json has no "api_hit").
 *   - the schema has no top-level "package" property, so the package travels inside
 *     detail as a "package=<pkg>" kv-pair (collect.py also knows --package).
 *   - widget/screenshot are omitted here; collect.py fills them with null per schema.
 *   - READ_CLIPBOARD is a descriptive tag: clipboard reads have no dedicated runtime
 *     permission (Android 10+ restricts them by window focus instead).
 *   - camera2: android.hardware.camera2.CameraDevice is abstract - the hookable
 *     "open*" entry point is CameraManager.openCamera (manifest entry for that purpose).
 *
 * Robustness contract
 *   Every hook is individually try/catch-guarded: an API-level difference (e.g.
 *   getDeviceId absent on API 30+), a missing class or a failing overload is logged
 *   and skipped - a hook failure never crashes the app.
 */

'use strict';

/* ------------------------- configuration constants ------------------------- */

// Include-list JSON file path on the DEVICE (same shape as apis.json).
// '' (default) -> monitor all APIs of the embedded default list below.
var MANIFEST_DEVICE_FILE = ''; // e.g. '/data/local/tmp/fastbot_apis.json'

// Line prefix collect.py filters on. Do not change without collect.py.
var AUDIT_PREFIX = '@@AUDIT@@';

// Max chars for the args summary inside detail.
var ARGS_LIMIT = 120;

/* Embedded default manifest - mirrors tools/privacy_hook/apis.json. */
var FALLBACK_APIS = [
  {"class": "android.telephony.TelephonyManager", "method": "getDeviceId", "permission": "READ_PHONE_STATE", "risk": "high"},
  {"class": "android.telephony.TelephonyManager", "method": "getImei", "permission": "READ_PHONE_STATE", "risk": "high"},
  {"class": "android.telephony.TelephonyManager", "method": "getSubscriberId", "permission": "READ_PHONE_STATE", "risk": "high"},
  {"class": "android.telephony.TelephonyManager", "method": "getSimSerialNumber", "permission": "READ_PHONE_STATE", "risk": "high"},
  {"class": "android.telephony.TelephonyManager", "method": "getLine1Number", "permission": "READ_PHONE_STATE", "risk": "high"},
  {"class": "android.location.LocationManager", "method": "getLastKnownLocation", "permission": "ACCESS_FINE_LOCATION", "risk": "high"},
  {"class": "android.location.LocationManager", "method": "requestLocationUpdates", "permission": "ACCESS_FINE_LOCATION", "risk": "high"},
  {"class": "android.content.ClipboardManager", "method": "getPrimaryClip", "permission": "READ_CLIPBOARD", "risk": "high"},
  {"class": "android.content.ContentResolver", "method": "query", "uri": "content://com.android.contacts", "permission": "READ_CONTACTS", "risk": "high"},
  {"class": "android.content.ContentResolver", "method": "query", "uri": "content://sms", "permission": "READ_SMS", "risk": "high"},
  {"class": "android.content.ContentResolver", "method": "query", "uri": "content://call_log", "permission": "READ_CALL_LOG", "risk": "high"},
  {"class": "android.hardware.Camera", "method": "open", "permission": "CAMERA", "risk": "high"},
  {"class": "android.hardware.camera2.CameraManager", "method": "openCamera", "permission": "CAMERA", "risk": "high"},
  {"class": "android.media.MediaRecorder", "method": "setAudioSource", "permission": "RECORD_AUDIO", "risk": "high"},
  {"class": "android.bluetooth.BluetoothAdapter", "method": "getDefaultAdapter", "permission": "BLUETOOTH", "risk": "medium"}
];

/* --------------------------------- helpers --------------------------------- */

function logWarn(message) {
  console.warn('[fastbot-hook] ' + message);
}

/* Best-effort current activity name ('(unknown)' when unavailable). */
function currentActivityName() {
  try {
    var activityThread = Java.use('android.app.ActivityThread');
    var activity = activityThread.currentActivity();
    if (activity !== null) {
      return String(activity.getClass().getName());
    }
  } catch (e) {
    /* best-effort only */
  }
  return '(unknown)';
}

/* Best-effort current package name ('(unknown)' when unavailable). */
function currentPackageName() {
  try {
    var name = Java.use('android.app.ActivityThread').currentPackageName();
    if (name !== null) {
      return String(name);
    }
  } catch (e) {
    /* fall through to the Application fallback */
  }
  try {
    var app = Java.use('android.app.ActivityThread').currentApplication();
    if (app !== null) {
      return String(app.getPackageName());
    }
  } catch (e) {
    /* best-effort only */
  }
  return '(unknown)';
}

/* Read a manifest JSON file from the device filesystem. Returns null on any
 * failure (never throws) - caller falls back to the embedded default list. */
function readDeviceManifest(path) {
  try {
    var File = Java.use('java.io.File');
    var Scanner = Java.use('java.util.Scanner');
    var file = File.$new(path);
    if (file === null || !file.exists()) {
      logWarn('manifest file not found on device: ' + path);
      return null;
    }
    var scanner = Scanner.$new(file); // Scanner(File)
    var text = scanner.useDelimiter('\\A').next();
    scanner.close();
    return JSON.parse(text);
  } catch (e) {
    logWarn('device manifest unreadable (' + path + '): ' + e.message);
    return null;
  }
}

/* Summarize hook arguments, truncated to ARGS_LIMIT chars. */
function summarizeArgs(args) {
  var parts = [];
  try {
    for (var i = 0; i < args.length; i++) {
      var text;
      try {
        text = (args[i] === null || args[i] === undefined) ? 'null' : String(args[i].toString());
      } catch (e) {
        text = '<unavailable>';
      }
      parts.push(text);
    }
  } catch (e) {
    parts.push('<unavailable>');
  }
  var joined = parts.join(', ');
  if (joined.length > ARGS_LIMIT) {
    joined = joined.substring(0, ARGS_LIMIT - 3) + '...';
  }
  return joined;
}

/* detail = "api=<class.method>; permission=<perm>; package=<pkg>; args=<summary>" */
function buildDetail(className, entry, args) {
  return 'api=' + className + '.' + entry.method +
    '; permission=' + (entry.permission || 'unknown') +
    '; package=' + currentPackageName() +
    '; args=' + summarizeArgs(args);
}

/* Does a manifest entry match this call? Entries without a "uri" filter always
 * match; entries with one require args[0].toString() to contain the filter. */
function entryMatches(entry, args) {
  if (!entry.uri) {
    return true;
  }
  try {
    if (args.length > 0 && args[0] !== null && args[0] !== undefined) {
      var uri = String(args[0].toString()).toLowerCase();
      return uri.indexOf(String(entry.uri).toLowerCase()) !== -1;
    }
  } catch (e) {
    /* fall through */
  }
  return false;
}

/* Emit ONE @@AUDIT@@ JSON line for a matched manifest entry. */
function emitRecord(entry, className, args) {
  try {
    var record = {
      ts: Date.now(),
      type: 'sensitive_api',
      activity: currentActivityName(),
      detail: buildDetail(className, entry, args),
      source: 'frida'
    };
    console.log(AUDIT_PREFIX + JSON.stringify(record));
  } catch (e) {
    /* audit failure must never crash the app */
    logWarn('audit emit failed: ' + e.message);
  }
}

/* Report the first matching entry for a hooked call (uri-filter aware). */
function reportMatch(entries, className, args) {
  for (var i = 0; i < entries.length; i++) {
    try {
      if (entryMatches(entries[i], args)) {
        emitRecord(entries[i], className, args);
        return;
      }
    } catch (e) {
      logWarn('match check failed: ' + e.message);
    }
  }
}

/* ------------------------------ hook installation ------------------------------ */

/* Resolve the manifest: override > device file > embedded fallback. Returns a
 * flat, de-duplicated array of entries with class+method present. */
function resolveManifest() {
  var manifest = null;
  var origin = 'embedded default (monitor all)';
  if (typeof APIS_OVERRIDE !== 'undefined' && APIS_OVERRIDE !== null) {
    manifest = APIS_OVERRIDE;
    origin = 'injected override (collect.py wrapper)';
  } else if (MANIFEST_DEVICE_FILE !== '') {
    manifest = readDeviceManifest(MANIFEST_DEVICE_FILE);
    origin = 'device file: ' + MANIFEST_DEVICE_FILE;
  }
  if (manifest === null || manifest === undefined || manifest.apis === null || manifest.apis === undefined) {
    manifest = { apis: FALLBACK_APIS };
    origin = 'embedded default (monitor all)';
  }
  var valid = [];
  var seen = {};
  manifest.apis.forEach(function (entry) {
    try {
      if (entry && typeof entry.class === 'string' && entry.class !== '' &&
          typeof entry.method === 'string' && entry.method !== '') {
        var key = entry.class + '#' + entry.method + '#' + (entry.uri || '');
        if (!seen[key]) {
          seen[key] = true;
          valid.push(entry);
        }
        return;
      }
      logWarn('manifest entry missing class/method: ' + JSON.stringify(entry));
    } catch (e) {
      logWarn('bad manifest entry skipped: ' + e.message);
    }
  });
  if (valid.length === 0) {
    logWarn('manifest has no valid entries; using embedded default');
    valid = FALLBACK_APIS;
    origin = 'embedded default (monitor all)';
  }
  logWarn('manifest: ' + valid.length + ' apis from ' + origin);
  return valid;
}

/* Group entries by class#method so one hook serves several manifest entries
 * (e.g. the three ContentResolver.query uri filters). */
function groupByClassMethod(entries) {
  var groups = {};
  entries.forEach(function (entry) {
    var key = entry.class + '#' + entry.method;
    if (!groups[key]) {
      groups[key] = { cls: entry.class, method: entry.method, entries: [] };
    }
    groups[key].entries.push(entry);
    groups[key].cls = entry.class;
    groups[key].method = entry.method;
  });
  return groups;
}

/* Hook all overloads of one class#method group. Errors are logged + skipped. */
function hookMethodGroup(clazz, group) {
  var overloads;
  try {
    overloads = clazz[group.method].overloads;
  } catch (e) {
    logWarn('method missing on this API level, skipped: ' + group.cls + '.' + group.method);
    return;
  }
  if (!overloads || overloads.length === 0) {
    logWarn('no overloads found, skipped: ' + group.cls + '.' + group.method);
    return;
  }
  var installed = 0;
  overloads.forEach(function (overload) {
    try {
      overload.implementation = function () {
        var args = Array.prototype.slice.call(arguments);
        var ret;
        try {
          ret = this[group.method].apply(this, args);
        } finally {
          reportMatch(group.entries, group.cls, args);
        }
        return ret;
      };
      installed++;
    } catch (e) {
      logWarn('overload hook failed, skipped: ' + group.cls + '.' + group.method + ': ' + e.message);
    }
  });
  if (installed === 0) {
    logWarn('no overload could be hooked: ' + group.cls + '.' + group.method);
  }
}

/* Install every manifest hook; per-class failures are logged + skipped. */
function installHooks(entries) {
  var groups = groupByClassMethod(entries);
  Object.keys(groups).forEach(function (key) {
    var group = groups[key];
    var clazz;
    try {
      clazz = Java.use(group.cls);
    } catch (e) {
      logWarn('class not found on this device, skipped: ' + group.cls);
      return;
    }
    hookMethodGroup(clazz, group);
  });
}

/* --------------------------------- entry point --------------------------------- */

function main() {
  if (!Java.available) {
    logWarn('Java runtime not available; nothing monitored');
    return;
  }
  Java.perform(function () {
    try {
      installHooks(resolveManifest());
    } catch (e) {
      logWarn('hook installation failed (app continues): ' + e.message);
    }
  });
}

try {
  main();
} catch (e) {
  logWarn('fatal init error (app continues): ' + e.message);
}
