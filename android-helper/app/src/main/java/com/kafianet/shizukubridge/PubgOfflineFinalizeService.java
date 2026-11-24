package com.kafianet.shizukubridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;
import android.provider.DocumentsContract;
import android.provider.DocumentsContract.Document;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class PubgOfflineFinalizeService extends Service {
    public static final String ACTION_START = "com.kafianet.shizukubridge.action.PUBG_LOCAL_FINALIZE";
    public static final String EXTRA_SOURCE_ROOT = "source_root";
    public static final String EXTRA_TARGET_PACKAGE = "target_package";
    public static final String EXTRA_TARGET_RELATIVE = "target_relative";
    public static final String EXTRA_GAME = "game";

    static final String PREFS = "kafianet_pubg_local_finalize";
    static final String KEY_STATE = "state";
    static final String KEY_PERCENT = "percent";
    static final String KEY_COPIED_FILES = "copied_files";
    static final String KEY_TOTAL_FILES = "total_files";
    static final String KEY_COPIED_BYTES = "copied_bytes";
    static final String KEY_TOTAL_BYTES = "total_bytes";
    static final String KEY_CURRENT_FILE = "current_file";
    static final String KEY_ERROR = "error";
    static final String KEY_SOURCE_ROOT = "source_root";
    static final String KEY_TARGET_PACKAGE = "target_package";
    static final String KEY_TARGET_RELATIVE = "target_relative";
    static final String KEY_GAME = "game";

    private static final String TREE_PREFS = "kafianet_storage_permissions";
    private static final String TREE_URI_KEY = "pubg_tree_uri";
    private static final int NOTIFICATION_ID = 73021;
    private static final String CHANNEL_ID = "kafianet_local_game_copy";
    private static final int BUFFER_SIZE = 1024 * 1024;

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile boolean running;
    private volatile boolean cancelled;
    private volatile int percent;
    private volatile int copiedFiles;
    private volatile int totalFiles;
    private volatile long copiedBytes;
    private volatile long totalBytes;
    private volatile String currentFile = "";
    private volatile String gameName = "PUBG Mobile";

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (running) return START_NOT_STICKY;
        if (intent == null || !ACTION_START.equals(intent.getAction())) {
            updatePersistentState("failed", "لم تصل بيانات بدء النقل المحلي للمساعد.");
            stopSelf(startId);
            return START_NOT_STICKY;
        }

        String source = intent.getStringExtra(EXTRA_SOURCE_ROOT);
        String targetPackage = intent.getStringExtra(EXTRA_TARGET_PACKAGE);
        String targetRelative = intent.getStringExtra(EXTRA_TARGET_RELATIVE);
        gameName = safeGameName(intent.getStringExtra(EXTRA_GAME));

        if (source == null || targetPackage == null || targetRelative == null) {
            updatePersistentState("failed", "بيانات مصدر أو وجهة النقل ناقصة.");
            stopSelf(startId);
            return START_NOT_STICKY;
        }

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        prefs.edit()
                .putString(KEY_SOURCE_ROOT, source)
                .putString(KEY_TARGET_PACKAGE, targetPackage)
                .putString(KEY_TARGET_RELATIVE, targetRelative)
                .putString(KEY_GAME, gameName)
                .putString(KEY_ERROR, "")
                .apply();

        running = true;
        cancelled = false;
        startForeground(NOTIFICATION_ID, buildNotification("تم استلام المهمة المحلية", 0, true));
        updatePersistentState("running", "");
        worker.execute(() -> runLocalCopy(source, targetPackage, targetRelative, startId));
        return START_NOT_STICKY;
    }

    private void runLocalCopy(
            String sourceValue, String targetPackage, String targetRelative, int startId) {
        try {
            Uri treeUri = loadValidatedTree(targetPackage);
            File sourceRoot = validateSourceRoot(sourceValue);
            List<String> directories = new ArrayList<>();
            List<SourceFile> files = new ArrayList<>();
            collectSource(sourceRoot, "", directories, files);

            totalFiles = files.size();
            totalBytes = 0L;
            for (SourceFile file : files) totalBytes += file.length;
            copiedFiles = 0;
            copiedBytes = 0L;
            percent = 0;
            saveCounters("running", "");

            if (files.isEmpty()) {
                throw new IllegalStateException("مجلد الجذر لا يحتوي ملفات قابلة للنقل.");
            }

            Uri destination = buildDocumentUri(treeUri);
            for (String segment : validateRelativeSegments(targetRelative)) {
                destination = ensureDirectory(treeUri, destination, segment);
            }

            for (String relativeDirectory : directories) {
                checkCancelled();
                Uri parent = destination;
                for (String segment : relativeDirectory.split("/")) {
                    if (!segment.isEmpty()) {
                        parent = ensureDirectory(treeUri, parent, segment);
                    }
                }
            }

            for (SourceFile source : files) {
                checkCancelled();
                currentFile = source.relativePath;
                Uri parent = destination;
                String[] segments = source.relativePath.split("/");
                for (int i = 0; i < segments.length - 1; i++) {
                    parent = ensureDirectory(treeUri, parent, segments[i]);
                }
                copyOneFile(treeUri, parent, source);
                copiedFiles++;
                percent = calculatePercent();
                saveCounters("running", "");
                notifyProgress();
            }

            percent = 100;
            currentFile = "";
            saveCounters("completed", "");
            notifyProgress();
            showFinalNotification(
                    "اكتمل نقل " + gameName,
                    copiedFiles + " ملف — " + formatBytes(copiedBytes)
            );
        } catch (Throwable failure) {
            String detail = failure.getClass().getSimpleName() + ": " + String.valueOf(failure.getMessage());
            updatePersistentState(cancelled ? "cancelled" : "failed", detail);
            showFinalNotification(
                    cancelled ? "أُلغي النقل المحلي" : "فشل النقل المحلي",
                    detail
            );
            android.util.Log.e("KafiaNetLocalCopy", "Local SAF finalization failed", failure);
        } finally {
            running = false;
            stopForeground(false);
            stopSelf(startId);
        }
    }

    private Uri loadValidatedTree(String targetPackage) throws Exception {
        if (targetPackage == null || !targetPackage.matches("[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+")) {
            throw new SecurityException("معرّف حزمة اللعبة غير صالح.");
        }
        String saved = getSharedPreferences(TREE_PREFS, MODE_PRIVATE).getString(TREE_URI_KEY, "");
        if (saved == null || saved.isEmpty()) {
            throw new IllegalStateException("لم يتم حفظ إذن مجلد Android/data الخاص باللعبة.");
        }
        Uri uri = Uri.parse(saved);
        if (!"com.android.externalstorage.documents".equals(uri.getAuthority())
                || !DocumentsContract.isTreeUri(uri)) {
            throw new SecurityException("صلاحية المجلد المحفوظ ليست من مزوّد تخزين Android.");
        }
        String expectedId = "primary:Android/data/" + targetPackage;
        if (!expectedId.equals(DocumentsContract.getTreeDocumentId(uri))) {
            throw new SecurityException("المجلد الممنوح لا يطابق Package ID المطلوب: " + targetPackage);
        }

        boolean persisted = false;
        for (android.content.UriPermission permission : getContentResolver().getPersistedUriPermissions()) {
            if (uri.equals(permission.getUri())
                    && permission.isReadPermission()
                    && permission.isWritePermission()) {
                persisted = true;
                break;
            }
        }
        if (!persisted) throw new SecurityException("إذن القراءة والكتابة لم يعد محفوظاً.");
        return uri;
    }

    private File validateSourceRoot(String sourceValue) throws Exception {
        File storageRoot = Environment.getExternalStorageDirectory().getCanonicalFile();
        File source = new File(sourceValue).getCanonicalFile();
        if (!source.isDirectory() || source.equals(storageRoot)
                || source.getParentFile() == null
                || !storageRoot.equals(source.getParentFile())) {
            throw new SecurityException(
                    "مصدر النقل يجب أن يكون مجلد اللعبة المستقل مباشرة داخل الذاكرة الداخلية: "
                            + source
            );
        }
        if (checkSelfPermission(android.Manifest.permission.READ_EXTERNAL_STORAGE)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            throw new SecurityException("لم يمنح المستخدم صلاحية قراءة مجلد نسخة الجذر.");
        }
        return source;
    }

    private void collectSource(
            File directory, String relative, List<String> directories, List<SourceFile> files)
            throws Exception {
        checkCancelled();
        File[] children = directory.listFiles();
        if (children == null) {
            throw new IllegalStateException("تعذرت قراءة مجلد المصدر: " + directory);
        }
        java.util.Arrays.sort(children, Comparator.comparing(
                item -> item.getName().toLowerCase(Locale.ROOT)
        ));
        for (File child : children) {
            checkCancelled();
            File canonicalChild = child.getCanonicalFile();
            if (!canonicalChild.equals(child.getAbsoluteFile())) {
                throw new SecurityException("رُفض رابط رمزي داخل نسخة الجذر: " + child.getName());
            }
            String childRelative = relative.isEmpty()
                    ? child.getName() : relative + "/" + child.getName();
            if (child.isDirectory()) {
                directories.add(childRelative);
                collectSource(child, childRelative, directories, files);
            } else if (child.isFile()) {
                files.add(new SourceFile(child, childRelative, child.length()));
            }
        }
    }

    private void copyOneFile(Uri treeUri, Uri parent, SourceFile source) throws Exception {
        String fileName = source.file.getName();
        Uri existing = findChild(treeUri, parent, fileName);
        if (existing != null && isDirectory(existing)) {
            throw new IllegalStateException("مجلد موجود يمنع إنشاء الملف: " + source.relativePath);
        }

        String suffix = UUID.randomUUID().toString().replace("-", "");
        Uri temporary = DocumentsContract.createDocument(
                getContentResolver(), parent, "application/octet-stream",
                ".kafianet-" + suffix + ".part"
        );
        if (temporary == null) {
            throw new IllegalStateException("تعذر إنشاء ملف مؤقت داخل الوجهة: " + source.relativePath);
        }

        Uri backup = null;
        long written = 0L;
        try {
            byte[] buffer = new byte[BUFFER_SIZE];
            try (InputStream input = new FileInputStream(source.file);
                 OutputStream output = getContentResolver().openOutputStream(temporary, "w")) {
                if (output == null) throw new IllegalStateException("مزوّد Android لم يفتح ملف الوجهة.");
                int count;
                long lastUpdate = System.currentTimeMillis();
                while ((count = input.read(buffer)) != -1) {
                    checkCancelled();
                    output.write(buffer, 0, count);
                    written += count;
                    copiedBytes += count;
                    long now = System.currentTimeMillis();
                    if (now - lastUpdate >= 700L) {
                        percent = calculatePercent();
                        saveCounters("running", "");
                        notifyProgress();
                        lastUpdate = now;
                    }
                }
                output.flush();
            }

            if (written != source.length) {
                throw new IllegalStateException(
                        "عدد البايتات المكتوبة لا يطابق المصدر: " + source.relativePath
                );
            }

            if (existing != null) {
                backup = DocumentsContract.renameDocument(
                        getContentResolver(), existing, ".kafianet-backup-" + suffix
                );
                if (backup == null) {
                    throw new IllegalStateException(
                            "تعذر حفظ نسخة الملف السابق قبل الاستبدال: " + source.relativePath
                    );
                }
            }

            Uri completed = DocumentsContract.renameDocument(
                    getContentResolver(), temporary, fileName
            );
            if (completed == null) {
                if (backup != null) {
                    DocumentsContract.renameDocument(getContentResolver(), backup, fileName);
                    backup = null;
                }
                throw new IllegalStateException(
                        "مزوّد Android لا يدعم تثبيت الملف المكتمل باسمه النهائي: " + source.relativePath
                );
            }
            temporary = null;

            if (backup != null) {
                if (!DocumentsContract.deleteDocument(getContentResolver(), backup)) {
                    android.util.Log.w("KafiaNetLocalCopy",
                            "Could not delete replaced backup file: " + source.relativePath);
                }
                backup = null;
            }
        } catch (Throwable failure) {
            if (temporary != null) {
                try { DocumentsContract.deleteDocument(getContentResolver(), temporary); }
                catch (Throwable ignored) { }
            }
            if (backup != null) {
                try {
                    DocumentsContract.renameDocument(getContentResolver(), backup, fileName);
                } catch (Throwable restoreFailure) {
                    android.util.Log.e("KafiaNetLocalCopy",
                            "Could not restore previous destination file: " + source.relativePath,
                            restoreFailure);
                }
            }
            throw failure;
        }
    }

    private Uri buildDocumentUri(Uri treeUri) {
        String treeId = DocumentsContract.getTreeDocumentId(treeUri);
        return DocumentsContract.buildDocumentUriUsingTree(treeUri, treeId);
    }

    private Uri ensureDirectory(Uri treeUri, Uri parent, String name) throws Exception {
        Uri existing = findChild(treeUri, parent, name);
        if (existing != null) {
            if (!isDirectory(existing)) {
                throw new IllegalStateException("ملف موجود يمنع إنشاء المجلد: " + name);
            }
            return existing;
        }
        Uri created = DocumentsContract.createDocument(
                getContentResolver(), parent, Document.MIME_TYPE_DIR, name
        );
        if (created == null) throw new IllegalStateException("تعذر إنشاء المجلد داخل Android/data: " + name);
        return created;
    }

    private Uri findChild(Uri treeUri, Uri parent, String name) throws Exception {
        String parentId = DocumentsContract.getDocumentId(parent);
        Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentId);
        String[] projection = {
                Document.COLUMN_DOCUMENT_ID,
                Document.COLUMN_DISPLAY_NAME,
                Document.COLUMN_MIME_TYPE
        };
        try (Cursor cursor = getContentResolver().query(children, projection, null, null, null)) {
            if (cursor == null) throw new IllegalStateException("مزوّد SAF لم يُرجع قائمة محتويات المجلد.");
            int idColumn = cursor.getColumnIndexOrThrow(Document.COLUMN_DOCUMENT_ID);
            int nameColumn = cursor.getColumnIndexOrThrow(Document.COLUMN_DISPLAY_NAME);
            while (cursor.moveToNext()) {
                if (name.equals(cursor.getString(nameColumn))) {
                    return DocumentsContract.buildDocumentUriUsingTree(
                            treeUri, cursor.getString(idColumn)
                    );
                }
            }
        }
        return null;
    }

    private boolean isDirectory(Uri uri) {
        try (Cursor cursor = getContentResolver().query(
                uri, new String[]{Document.COLUMN_MIME_TYPE}, null, null, null
        )) {
            return cursor != null && cursor.moveToFirst()
                    && Document.MIME_TYPE_DIR.equals(cursor.getString(0));
        } catch (Throwable ignored) {
            return false;
        }
    }

    private String[] validateRelativeSegments(String value) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("المسار النسبي للوجهة فارغ.");
        }
        String normalized = value.replace('\\', '/');
        if (normalized.startsWith("/") || normalized.contains(":") || normalized.indexOf('\0') >= 0) {
            throw new SecurityException("تم رفض مسار وجهة مطلق أو غير صالح.");
        }
        String[] segments = normalized.split("/", -1);
        for (String segment : segments) {
            if (segment.isEmpty() || ".".equals(segment) || "..".equals(segment)) {
                throw new SecurityException("المسار النسبي يحتوي مقطعاً غير آمن.");
            }
        }
        return segments;
    }

    private int calculatePercent() {
        if (totalBytes > 0) return Math.max(0, Math.min(99, (int) (copiedBytes * 100L / totalBytes)));
        return totalFiles > 0 ? Math.max(0, Math.min(99, copiedFiles * 100 / totalFiles)) : 0;
    }

    private void checkCancelled() throws InterruptedException {
        if (cancelled || Thread.currentThread().isInterrupted()) {
            throw new InterruptedException("أُلغي النقل المحلي.");
        }
    }

    private void saveCounters(String state, String error) {
        SharedPreferences.Editor editor = getSharedPreferences(PREFS, MODE_PRIVATE).edit();
        editor.putString(KEY_STATE, state);
        editor.putInt(KEY_PERCENT, percent);
        editor.putInt(KEY_COPIED_FILES, copiedFiles);
        editor.putInt(KEY_TOTAL_FILES, totalFiles);
        editor.putLong(KEY_COPIED_BYTES, copiedBytes);
        editor.putLong(KEY_TOTAL_BYTES, totalBytes);
        editor.putString(KEY_CURRENT_FILE, currentFile == null ? "" : currentFile);
        editor.putString(KEY_ERROR, error == null ? "" : error);
        editor.apply();
    }

    private void updatePersistentState(String state, String error) {
        saveCounters(state, error);
    }

    private void notifyProgress() {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) manager.notify(NOTIFICATION_ID,
                buildNotification("نقل " + gameName + " محلياً — " + percent + "%",
                        percent, true));
    }

    private Notification buildNotification(String text, int progress, boolean ongoing) {
        Intent open = new Intent(this, HomeActivity.class);
        PendingIntent content = PendingIntent.getActivity(
                this, 73022, open,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0)
        );
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_upload)
                .setContentTitle("KafiaNet Assistant")
                .setContentText(text)
                .setContentIntent(content)
                .setOngoing(ongoing)
                .setOnlyAlertOnce(true)
                .setCategory(Notification.CATEGORY_PROGRESS)
                .setProgress(100, Math.max(0, Math.min(100, progress)), false);
        return builder.build();
    }

    private void showFinalNotification(String title, String details) {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager == null) return;
        Intent open = new Intent(this, HomeActivity.class);
        PendingIntent content = PendingIntent.getActivity(
                this, 73023, open,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0)
        );
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentTitle(title)
                .setContentText(details)
                .setContentIntent(content)
                .setAutoCancel(true)
                .setOnlyAlertOnce(true);
        manager.notify(NOTIFICATION_ID, builder.build());
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "نقل ملفات الألعاب محلياً", NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("يعرض تقدم نسخ ملفات اللعبة من مجلد الجذر إلى المجلد المحمي.");
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.createNotificationChannel(channel);
    }

    private String safeGameName(String value) {
        return value == null || value.trim().isEmpty() ? "PUBG Mobile" : value.trim();
    }

    private String formatBytes(long value) {
        double amount = Math.max(0L, value);
        String[] units = {"B", "KB", "MB", "GB", "TB"};
        int index = 0;
        while (amount >= 1024.0 && index < units.length - 1) {
            amount /= 1024.0;
            index++;
        }
        return String.format(Locale.ROOT, "%.1f %s", amount, units[index]);
    }

    @Override
    public void onTimeout(int startId, int fgsType) {
        updatePersistentState("interrupted", "انتهت مهلة خدمة النقل الخلفية في Android؛ افتح المساعد لاستئناف العملية.");
        stopForeground(true);
        stopSelf(startId);
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (worker != null) worker.shutdownNow();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private static final class SourceFile {
        final File file;
        final String relativePath;
        final long length;

        SourceFile(File file, String relativePath, long length) {
            this.file = file;
            this.relativePath = relativePath;
            this.length = Math.max(0L, length);
        }
    }
}
