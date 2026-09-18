/*
 * Copyright 2020 Advanced Software Technologies Lab at ETH Zurich, Switzerland
 *
 * Modified - Copyright (c) 2020 Bytedance Inc.
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

package com.android.commands.monkey.framework;

import android.app.ActivityManager;
import android.app.ActivityManager.RunningTaskInfo;
import android.app.ActivityManagerNative;
import android.app.IActivityManager;
import android.app.IApplicationThread;
import android.app.ProfilerInfo;
import android.content.IIntentReceiver;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.IPackageManager;
import android.content.pm.PackageManager;
import android.content.pm.PermissionInfo;
import android.content.pm.ResolveInfo;
import android.os.Bundle;
import android.os.IBinder;
import android.os.IPowerManager;
import android.os.RemoteException;
import android.os.UserHandle;
import android.view.IWindowManager;
import android.view.inputmethod.InputMethodInfo;

import com.android.commands.monkey.utils.Logger;
import com.android.internal.view.IInputMethodManager;
import com.android.commands.monkey.utils.ContextUtils;


import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.List;

/**
 * @author Zhao Zhang, Tianxiao Gu
 */

/**
 * Reflect utils
 */
public class APIAdapter {


    private static Method getTasksMethod = null;
    private static boolean getTasksResolved = false;

    private static final String MONKEY_PACKAGE = "com.android.commands.monkey";

    private static Method freezeRotationMethod = null;
    private static boolean freezeRotationResolved = false;
    private static Method thawRotationMethod = null;
    private static boolean thawRotationResolved = false;

    /**
     * Quiet signature probe: returns null instead of logging/aborting when the
     * method is absent, so callers can fall through to the next candidate
     * signature or a shell fallback.
     */
    private static Method probeMethod(Class<?> clazz, String name, Class<?>... types) {
        try {
            Method method = clazz.getMethod(name, types);
            method.setAccessible(true);
            return method;
        } catch (NoSuchMethodException | NoSuchMethodError | SecurityException e) {
            return null;
        }
    }

    private static boolean invokeVoid(Method method, Object receiver, Object... args) {
        try {
            method.invoke(receiver, args);
            return true;
        } catch (IllegalAccessException | IllegalArgumentException | InvocationTargetException e) {
            Logger.warningPrintln(method.getName() + " invoke failed: " + e);
            return false;
        }
    }

    /**
     * Like {@link #invokeVoid} but rethrows a wrapped RemoteException so
     * callers can keep the original "system_server died" halt semantics
     * (INJECT_ERROR_REMOTE_EXCEPTION) instead of silently dropping events.
     */
    private static boolean invokeVoidChecked(Method method, Object receiver, Object... args) throws RemoteException {
        try {
            method.invoke(receiver, args);
            return true;
        } catch (IllegalAccessException | IllegalArgumentException e) {
            Logger.warningPrintln(method.getName() + " invoke failed: " + e);
            return false;
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause();
            if (cause instanceof RemoteException) {
                throw (RemoteException) cause;
            }
            Logger.warningPrintln(method.getName() + " invoke failed: " + cause);
            return false;
        }
    }

    /**
     * Freeze the default display rotation. Probes both the legacy
     * freezeRotation(int) and the newer freezeRotation(int, String) AIDL
     * signatures; returns false when neither exists (caller should skip the
     * rotation action instead of crashing with NoSuchMethodError).
     */
    public static boolean freezeRotation(IWindowManager iwm, int rotation) throws RemoteException {
        if (!freezeRotationResolved) {
            Class<?> clazz = iwm.getClass();
            freezeRotationMethod = probeMethod(clazz, "freezeRotation", int.class);
            if (freezeRotationMethod == null) {
                freezeRotationMethod = probeMethod(clazz, "freezeRotation", int.class, String.class);
            }
            freezeRotationResolved = true;
            if (freezeRotationMethod == null) {
                Logger.warningPrintln("freezeRotation is not available on this OS version; rotation events will be skipped");
            }
        }
        if (freezeRotationMethod == null) {
            return false;
        }
        if (freezeRotationMethod.getParameterTypes().length == 1) {
            return invokeVoidChecked(freezeRotationMethod, iwm, rotation);
        }
        return invokeVoidChecked(freezeRotationMethod, iwm, rotation, MONKEY_PACKAGE);
    }

    /**
     * Thaw the default display rotation. Probes thawRotation() and the newer
     * thawRotation(String); returns false when neither exists.
     */
    public static boolean thawRotation(IWindowManager iwm) throws RemoteException {
        if (!thawRotationResolved) {
            Class<?> clazz = iwm.getClass();
            thawRotationMethod = probeMethod(clazz, "thawRotation");
            if (thawRotationMethod == null) {
                thawRotationMethod = probeMethod(clazz, "thawRotation", String.class);
            }
            thawRotationResolved = true;
        }
        if (thawRotationMethod == null) {
            return false;
        }
        if (thawRotationMethod.getParameterTypes().length == 0) {
            return invokeVoidChecked(thawRotationMethod, iwm);
        }
        return invokeVoidChecked(thawRotationMethod, iwm, MONKEY_PACKAGE);
    }

    /**
     * Stop a package by probing forceStopPackage(String, int) and the older
     * forceStopPackage(String); returns false when neither exists.
     */
    public static boolean forceStopPackage(IActivityManager am, String packageName, int userId) {
        Class<?> clazz = am.getClass();
        Method method = probeMethod(clazz, "forceStopPackage", String.class, int.class);
        if (method != null) {
            return invokeVoid(method, am, packageName, userId);
        }
        method = probeMethod(clazz, "forceStopPackage", String.class);
        if (method != null) {
            return invokeVoid(method, am, packageName);
        }
        Logger.warningPrintln("forceStopPackage is not available on this OS version");
        return false;
    }

    /**
     * Probe isInteractive on the power manager binder proxy; null when the
     * probe or the invocation fails.
     */
    public static Boolean isInteractive(IPowerManager pm) {
        Class<?> clazz = pm.getClass();
        Method method = probeMethod(clazz, "isInteractive");
        if (method == null) {
            return null;
        }
        try {
            return (Boolean) method.invoke(pm);
        } catch (IllegalAccessException | IllegalArgumentException | InvocationTargetException e) {
            return null;
        }
    }

    /**
     * Probe getRunningAppProcesses (no-arg and int-arg variants) on the
     * activity manager binder proxy; null when neither exists, the invocation
     * fails, or the result is not a plain List (e.g. a ParceledListSlice on
     * newer AIDLs) so the caller can fall back to a shell probe.
     */
    public static List<?> getRunningAppProcesses(IActivityManager am) {
        Class<?> clazz = am.getClass();
        Method method = probeMethod(clazz, "getRunningAppProcesses");
        if (method == null) {
            method = probeMethod(clazz, "getRunningAppProcesses", int.class);
        }
        if (method == null) {
            Logger.warningPrintln("getRunningAppProcesses is not available on this OS version");
            return null;
        }
        try {
            Object result;
            if (method.getParameterTypes().length == 0) {
                result = method.invoke(am);
            } else {
                result = method.invoke(am, UserHandle.myUserId());
            }
            return (result instanceof List) ? (List<?>) result : null;
        } catch (IllegalAccessException | IllegalArgumentException | InvocationTargetException e) {
            Logger.warningPrintln("getRunningAppProcesses invoke failed: " + e);
            return null;
        }
    }

    private static Method findMethod(Class<?> clazz, String name, Class<?>... types) {
        Method method = null;
        try {
            method = clazz.getMethod(name, types);
            method.setAccessible(true);
        } catch (NoSuchMethodException e) {
            Logger.errorPrintln("findMethod() error, NoSuchMethodException happened, there is no such method: "+name);
        } catch (java.lang.NoSuchMethodError e) {
            Logger.errorPrintln("findMethod() error, NoSuchMethodError happened,, there is no such method: "+name);
        } catch (SecurityException e) {
            e.printStackTrace();
            System.exit(1);
        }
        return method;
    }

    public static PermissionInfo getPermissionInfo(IPackageManager ipm, String perm, int flags) {
        Class<?> clazz = ipm.getClass();
        String name = "getPermissionInfo";
        Method method = probeMethod(clazz, name, String.class, int.class);
        if (method != null) {
            return (PermissionInfo) invoke(method, ipm, perm, flags);
        }
        method = probeMethod(clazz, name, String.class, String.class, int.class);
        if (method != null) {
            return (PermissionInfo) invoke(method, ipm, perm, "shell", flags);
        }
        Logger.warningPrintln("getPermissionInfo is not available on this OS version");
        return null;
    }

    public static void registerReceiver(IActivityManager am, IIntentReceiver receiver, IntentFilter filter, int userId) {
        Class<?> clazz = am.getClass();
        String name = "registerReceiver";
        Method method = probeMethod(clazz, name, IApplicationThread.class, String.class, IIntentReceiver.class,
                IntentFilter.class, String.class, int.class);
        if (method != null) {
            invoke(method, am, null, null, receiver, filter, null, userId);
            return;
        }
        method = probeMethod(clazz, name, IApplicationThread.class, String.class, IIntentReceiver.class,
                IntentFilter.class, String.class, int.class, boolean.class);
        if (method != null) {
            invoke(method, am, null, null, receiver, filter, null, userId, false);
            return;
        }
        method = probeMethod(clazz, name, IApplicationThread.class, String.class, IIntentReceiver.class,
                IntentFilter.class, String.class, int.class, int.class);
        if (method != null) {
            invoke(method, am, null, null, receiver, filter, null, userId, 0);
            return;
        }
        Logger.warningPrintln("registerReceiver is not available on this OS version");
    }

    public static IActivityManager getActivityManager() {
        {
            Class<?> clazz = ActivityManagerNative.class;
            String name = "getDefault";
            Method method = findMethod(clazz, name);
            if (method != null) {
                return (IActivityManager) invoke(method, null);
            }
        }
        {
            Class<?> clazz = ActivityManager.class;
            String name = "getService";
            Method method = findMethod(clazz, name);
            if (method != null) {
                return (IActivityManager) invoke(method, null);
            }
        }
        Logger.println("Cannot getActivityManager");
        System.exit(1);
        return null;
    }

    private static Object invoke(Method method, Object reciver, Object... args) {
        try {
            return method.invoke(reciver, args);
        } catch (IllegalAccessException | IllegalArgumentException | InvocationTargetException e) {
            Logger.warningPrintln(method.getName() + " invoke failed: " + e);
            return null;
        }
    }

    private static Object invokej(Method method, Object reciver, Object... args) {
        try {
            return method.invoke(reciver, args);
        } catch (IllegalAccessException | IllegalArgumentException | InvocationTargetException e) {
            e.printStackTrace();
            return null;
        }
    }

    private static Object invokek(Method method, Object reciver, Object... args) {
        try {
            return method.invoke(reciver, args);
        } catch (IllegalAccessException | IllegalArgumentException | InvocationTargetException | SecurityException e) {
            return null;
        }
    }

    public static List<ResolveInfo> queryIntentActivities(PackageManager mPm, Intent intent) {
        return mPm.queryIntentActivities(intent, 0);
    }

    @SuppressWarnings("unchecked")
    public static List<RunningTaskInfo> getTasks(IActivityManager iAm, int maxNum) {
        if (!getTasksResolved) {
            Class<?> clazz = iAm.getClass();
            String name = "getTasks";
            getTasksMethod = probeMethod(clazz, name, int.class, int.class);
            if (getTasksMethod == null) {
                getTasksMethod = probeMethod(clazz, name, int.class);
            }
            if (getTasksMethod == null) {
                Logger.warningPrintln("getTasks is not available on this OS version; top-activity lookups will return null");
            }
            getTasksResolved = true;
        }
        if (getTasksMethod == null) {
            return null;
        }
        int parameterCount = getTasksMethod.getParameterTypes().length;
        if (parameterCount == 2) {
            return (List<RunningTaskInfo>) invokej(getTasksMethod, iAm, maxNum, 0 /* flags */);
        } else { // 1
            return (List<RunningTaskInfo>) invokej(getTasksMethod, iAm, maxNum);
        }
    }


    public static void setActivityController(IActivityManager mAm, Object controller) {
        Class<?> clazz = mAm.getClass();
        String name = "setActivityController";
        Method method = probeMethod(clazz, name, android.app.IActivityController.class);
        if (method != null) {
            invoke(method, mAm, controller);
            return;
        }
        method = probeMethod(clazz, name, android.app.IActivityController.class, boolean.class);
        if (method != null) {
            invoke(method, mAm, controller, true);
            return;
        }
        Logger.warningPrintln("setActivityController is not available on this OS version; ANR/crash capture via IActivityController is disabled");
    }

    public static void broadcastIntent(IActivityManager mAm, Intent paramIntent) {
        Class<?> c0 = mAm.getClass();
        String c1 = "broadcastIntent";
        Method m0 = probeMethod(c0, c1, IApplicationThread.class,
                Intent.class, String.class, IIntentReceiver.class,
                int.class, String.class, Bundle.class,
                String[].class, int.class, Bundle.class,
                boolean.class, boolean.class, int.class);
        if (m0 != null) {
            invoke(m0, mAm, null, paramIntent, null, null, 0, null, null, null, 0, null, false, false, 0);
            return;
        }
        m0 = probeMethod(c0, c1, IApplicationThread.class,
                Intent.class, String.class, IIntentReceiver.class,
                int.class, String.class, Bundle.class,
                String.class, int.class,
                boolean.class, boolean.class, int.class);
        if (m0 != null) {
            invoke(m0, mAm, null, paramIntent, null, null, 0, null, null, null, 0, false, false, 0);
            return;
        }
        m0 = probeMethod(c0, "broadcastIntentWithFeature", IApplicationThread.class,
                String.class, Intent.class, String.class, IIntentReceiver.class,
                int.class, String.class, Bundle.class,
                String[].class, int.class, Bundle.class,
                boolean.class, boolean.class, int.class);
        if (m0 != null) {
            invoke(m0, mAm, null, null, paramIntent, null, null, 0, null, null, null, 0, null, false, false, 0);
            return;
        }
        Logger.warningPrintln("broadcastIntent is not available on this OS version");
    }


    public static Object startActivity(IActivityManager mAm, Intent paramIntent) {
        Class<?> c0 = mAm.getClass();
        String c1 = "startActivity";
        Method m0 = findMethod(c0, c1, IApplicationThread.class,
                String.class, Intent.class, String.class, IBinder.class,
                String.class, int.class, int.class, ProfilerInfo.class, Bundle.class);

        if (m0 != null) {
            return invokek(m0, mAm, null, null, paramIntent, null, null, null, 0, 0, null, null);
        }
        System.out.format("Cannot resolve m0: " + c1);
        return null;
    }

    @SuppressWarnings("unchecked")
    public static List<InputMethodInfo> getEnabledInputMethodList(IInputMethodManager iIMM) {
        Class<?> clazz = iIMM.getClass();
        String name = "getEnabledInputMethodList";
        Method method = probeMethod(clazz, name);
        if (method != null) {
            return (List<InputMethodInfo>) invoke(method, iIMM);
        }
        method = probeMethod(clazz, name, int.class);
        if (method != null) {
            return (List<InputMethodInfo>) invoke(method, iIMM, 0);
        }
        Logger.warningPrintln("getEnabledInputMethodList is not available on this OS version");
        return null;
    }

    public static boolean setInputMethod(IInputMethodManager iIMM, String ime) {
        Class<?> clazz = iIMM.getClass();
        String name = "setInputMethod";
        Method method = findMethod(clazz, name, IBinder.class, String.class);
        if (method != null) {
            if (invokej(method, iIMM, null, ime) != null) {
                return true;
            }
        }
        return false;
    }

    public static String getSerial() {
        Class<?> classType = null;
        String serial = "unknown";
        try {
            classType = Class.forName("android.os.SystemProperties");
            Method getMethod = classType.getDeclaredMethod("get", String.class);
            serial = (String) getMethod.invoke(classType, new Object[]{"ro.serialno"});
        } catch (Exception e) {
            e.printStackTrace();
        }
        Logger.println("// device serial number is " + serial);
        return serial;
    }
}
