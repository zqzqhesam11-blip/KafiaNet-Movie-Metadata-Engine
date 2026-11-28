package com.kafianet.shizukubridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Holds the helper process at foreground-service priority while a desktop
 * transfer session is open. Screen and CPU wake locks are held only while
 * an actual transfer is active, and always have a bounded timeout.
 */
public final class BridgeKeepAliveService extends Service {
    private static final String ACTION_SESSION_START =
            "com.kafianet.shizukubridge.action.SESSION_START";
    private static final String ACTION_TRANSFER_STARTED =
            "com.kafianet.shizukubridge.action.TRANSFER_STARTED";
    private static final String ACTION_TRANSFER_FINISHED =
            "com.kafianet.shizukubridge.action.TRANSFER_FINISHED";
    private static final String ACTION_SESSION_STOP =
            "com.kafianet.shizukubridge.action.SESSION_STOP";
    private static final String EXTRA_JOB_ID = "job_id";

    private static final String CHANNEL_ID = "kafianet_bridge_session";
    private static final int NOTIFICATION_ID = 73031;
    private static final long WAKE_LOCK_TIMEOUT_MS = 12L * 60L * 60L * 1000L;

    private static volatile BridgeKeepAliveService instance;

    private final Set<String> activeTransfers = ConcurrentHashMap.newKeySet();
    private PowerManager.WakeLock cpuWakeLock;
    private PowerManager.WakeLock screenWakeLock;
    private volatile boolean foregroundStarted;
    private boolean stopWhenIdle;

    public static void startSession(Context context) {
        dispatch(context, ACTION_SESSION_START, null);
    }

    public static void transferStarted(Context context, String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return;
        BridgeKeepAliveService current = instance;
        if (current != null) {
            current.beginTransfer(jobId);
        } else {
            dispatch(context, ACTION_TRANSFER_STARTED, jobId);
        }
    }

    public static void transferFinished(Context context, String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return;
        BridgeKeepAliveService current = instance;
        if (current != null) current.finishTransfer(jobId);
    }

    public static void stopSession(Context context) {
        BridgeKeepAliveService current = instance;
        if (current != null) {
            current.requestStopWhenIdle();
        }
    }

    private static void dispatch(Context context, String action, String jobId) {
        Context app = context.getApplicationContext();
        Intent intent = new Intent(app, BridgeKeepAliveService.class);
        intent.setAction(action);
        if (jobId != null) intent.putExtra(EXTRA_JOB_ID, jobId);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                app.startForegroundService(intent);
            } else {
                app.startService(intent);
            }
        } catch (Throwable failure) {
            android.util.Log.e("KafiaNetBridge", "Could not start foreground transfer session", failure);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        instance = this;
        String action = intent == null ? ACTION_SESSION_START : intent.getAction();
        enterForeground();

        if (ACTION_TRANSFER_STARTED.equals(action)) {
            beginTransfer(intent.getStringExtra(EXTRA_JOB_ID));
        } else if (ACTION_TRANSFER_FINISHED.equals(action)) {
            finishTransfer(intent.getStringExtra(EXTRA_JOB_ID));
        } else if (ACTION_SESSION_STOP.equals(action)) {
            requestStopWhenIdle();
        } else {
            stopWhenIdle = false;
            updateNotification();
        }
        return START_STICKY;
    }

    private synchronized void beginTransfer(String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return;
        stopWhenIdle = false;
        activeTransfers.add(jobId);
        enterForeground();
        acquireTransferWakeLocks();
        updateNotification();
    }

    private synchronized void finishTransfer(String jobId) {
        if (jobId != null) activeTransfers.remove(jobId);
        if (activeTransfers.isEmpty()) {
            releaseTransferWakeLocks();
            if (stopWhenIdle) {
                stopServiceNow();
                return;
            }
        }
        updateNotification();
    }

    private synchronized void requestStopWhenIdle() {
        stopWhenIdle = true;
        if (activeTransfers.isEmpty()) {
            stopServiceNow();
        } else {
            updateNotification();
        }
    }

    private synchronized void enterForeground() {
        if (foregroundStarted) return;
        startForeground(NOTIFICATION_ID, buildNotification());
        foregroundStarted = true;
    }

    private void updateNotification() {
        Notification notification = buildNotification();
        if (foregroundStarted) {
            NotificationManager manager =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (manager != null) manager.notify(NOTIFICATION_ID, notification);
        }
    }

    private Notification buildNotification() {
        boolean transferring = !activeTransfers.isEmpty();
        String message = transferring
                ? "النقل جارٍ — سيستمر عند الضغط على Home"
                : "المساعد نشط — يمكنك الرجوع إليه من الإشعار";
        Intent open = new Intent(this, HomeActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pending = PendingIntent.getActivity(this, 73031, open, pendingFlags);

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle("KafiaNet Assistant")
                .setContentText(message)
                .setContentIntent(pending)
                .setCategory(Notification.CATEGORY_PROGRESS)
                .setOngoing(true)
                .setOnlyAlertOnce(true);
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            builder.setPriority(Notification.PRIORITY_LOW);
        }
        return builder.build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "جلسة مساعد KafiaNet والنقل",
                NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("إشعار مستمر أثناء جلسة النقل من الكمبيوتر إلى الهاتف.");
        channel.setShowBadge(false);
        NotificationManager manager =
                (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.createNotificationChannel(channel);
    }

    @SuppressWarnings("deprecation")
    private void acquireTransferWakeLocks() {
        PowerManager power = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (power == null) {
            android.util.Log.e("KafiaNetBridge", "PowerManager is unavailable; transfer continues without wake locks.");
            return;
        }

        try {
            if (cpuWakeLock == null || !cpuWakeLock.isHeld()) {
                cpuWakeLock = power.newWakeLock(
                        PowerManager.PARTIAL_WAKE_LOCK,
                        "KafiaNetAssistant:BridgeTransferCpu"
                );
                cpuWakeLock.setReferenceCounted(false);
                cpuWakeLock.acquire(WAKE_LOCK_TIMEOUT_MS);
            }

            if (screenWakeLock == null || !screenWakeLock.isHeld()) {
                screenWakeLock = power.newWakeLock(
                        PowerManager.SCREEN_DIM_WAKE_LOCK,
                        "KafiaNetAssistant:BridgeTransferScreen"
                );
                screenWakeLock.setReferenceCounted(false);
                screenWakeLock.acquire(WAKE_LOCK_TIMEOUT_MS);
            }
        } catch (Throwable failure) {
            android.util.Log.e(
                    "KafiaNetBridge",
                    "Could not acquire all transfer wake locks; keeping the active service alive.",
                    failure
            );
        }
    }

    private void releaseTransferWakeLocks() {
        PowerManager.WakeLock display = screenWakeLock;
        screenWakeLock = null;
        if (display != null && display.isHeld()) {
            try {
                display.release();
            } catch (Throwable failure) {
                android.util.Log.w("KafiaNetBridge", "Screen wake lock release warning", failure);
            }
        }

        PowerManager.WakeLock cpu = cpuWakeLock;
        cpuWakeLock = null;
        if (cpu != null && cpu.isHeld()) {
            try {
                cpu.release();
            } catch (Throwable failure) {
                android.util.Log.w("KafiaNetBridge", "CPU wake lock release warning", failure);
            }
        }
    }

    private synchronized void stopServiceNow() {
        releaseTransferWakeLocks();
        if (foregroundStarted) {
            stopForeground(true);
            foregroundStarted = false;
        }
        stopSelf();
    }

    @Override
    public void onDestroy() {
        releaseTransferWakeLocks();
        activeTransfers.clear();
        if (instance == this) instance = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
