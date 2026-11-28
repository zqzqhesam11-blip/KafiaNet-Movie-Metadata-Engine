package com.kafianet.shizukubridge;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.provider.DocumentsContract.Document;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class EFootballActivity extends Activity {
    private static final int REQUEST_TREE = 8107;
    private static final int SERVER_PORT = 18766;
    private static final String PREFS = "efootball_saf";
    private static final String PREF_TREE_URI = "tree_uri";
    private static final String EXPECTED_PROVIDER_AUTHORITY = "com.ziru.official.MTDataFilesProvider";

    private final ExecutorService workers = Executors.newCachedThreadPool();
    private volatile ServerSocket serverSocket;
    private volatile Uri treeUri;
    private volatile boolean probePassed = false;
    private volatile String rootDisplayName = "";
    private volatile String sessionToken = "";
    private volatile String activeJob = "";
    private volatile int expectedFiles = 0;
    private volatile int copiedFiles = 0;
    private volatile long copiedBytes = 0L;
    private volatile String jobError = "";

    private TextView statusView;
    private TextView tokenView;
    private Button chooseButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        BridgeKeepAliveService.startSession(this);
        sessionToken = UUID.randomUUID().toString().replace("-", "");
        readSession(getIntent());
        buildUi();
        startHttpServer();
        restoreTreeSelection();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readSession(intent);
        if (treeUri != null && probePassed) {
            showStatus("تم ربط KAFIA NET CONTROL PRO. مساحة eFootball جاهزة للنسخ.");
        }
    }

    private void readSession(Intent intent) {
        if (intent == null) return;
        String supplied = intent.getStringExtra("token");
        if (supplied == null || !supplied.matches("[A-Za-z0-9_-]{32,128}")) return;
        sessionToken = supplied;
        if (tokenView != null) {
            tokenView.setText("اتصال الكمبيوتر: تم إنشاء جلسة آمنة لهذا النقل.");
        }
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(28, 36, 28, 36);
        layout.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView title = new TextView(this);
        title.setText("KafiaNet Assistant — مساحة eFootball");
        title.setTextSize(22);
        title.setGravity(Gravity.CENTER);
        layout.addView(title);

        TextView instructions = new TextView(this);
        instructions.setText(
                "اربط مساحة ملفات نسخة eFootball Ziru_27 مرة واحدة.\n\n"
                + "اضغط اختيار المجلد، ثم افتح eFootball من القائمة الجانبية "
                + "واختر مجلد data نفسه، وليس files.\n"
                + "سيختبر التطبيق صلاحية الإنشاء والكتابة وإعادة التسمية والحذف "
                + "قبل السماح للكمبيوتر بالنسخ المباشر عبر SAF."
        );
        instructions.setTextSize(16);
        instructions.setPadding(0, 24, 0, 24);
        layout.addView(instructions);

        chooseButton = new Button(this);
        chooseButton.setText("اختيار مجلد data داخل مساحة eFootball");
        chooseButton.setOnClickListener(v -> chooseTree());
        layout.addView(chooseButton);

        tokenView = new TextView(this);
        tokenView.setTextIsSelectable(true);
        tokenView.setTextSize(14);
        tokenView.setPadding(0, 24, 0, 12);
        layout.addView(tokenView);

        statusView = new TextView(this);
        statusView.setTextSize(16);
        statusView.setTextIsSelectable(true);
        statusView.setPadding(0, 16, 0, 12);
        layout.addView(statusView);

        TextView note = new TextView(this);
        note.setText(
                "يستخدم الكمبيوتر ADB forward محليًا للاتصال بهذا المساعد.\n"
                + "لا يتم نقل الملفات إلى مجلد وسيط في الهاتف. "
                + "تُستبدل الملفات المتطابقة فقط بعد استقبال الملف المؤقت كاملًا، "
                + "وتبقى الملفات الأخرى كما هي."
        );
        note.setTextSize(13);
        note.setPadding(0, 20, 0, 0);
        layout.addView(note);

        scroll.addView(layout);
        setContentView(scroll);
        tokenView.setText("اتصال الكمبيوتر: يتم تمرير رمز جلسة آمن تلقائيًا عند بدء النقل.");
        showStatus("بانتظار اختيار مجلد data من مساحة eFootball.");
    }

    private void showStatus(String message) {
        runOnUiThread(() -> {
            if (statusView != null) statusView.setText(message);
        });
    }

    private void chooseTree() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        intent.addFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION
        );
        startActivityForResult(intent, REQUEST_TREE);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_TREE || resultCode != RESULT_OK || data == null) return;

        Uri selected = data.getData();
        if (selected == null) {
            showStatus("لم يتم اختيار مجلد.");
            return;
        }

        if (!EXPECTED_PROVIDER_AUTHORITY.equals(selected.getAuthority())) {
            treeUri = null;
            rootDisplayName = "";
            probePassed = false;
            getPreferences(MODE_PRIVATE).edit().remove(PREF_TREE_URI).apply();
            showStatus("المزوّد المختار ليس مساحة eFootball الخاصة بـ Ziru_27. اختر eFootball من القائمة الجانبية.");
            return;
        }

        int granted = data.getFlags()
                & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        try {
            getContentResolver().takePersistableUriPermission(selected, granted);
        } catch (Throwable failure) {
            showStatus("تعذر حفظ صلاحية SAF بشكل دائم: " + failure);
            return;
        }

        treeUri = selected;
        rootDisplayName = queryDisplayName(getRootDocumentUri());
        if (!"data".equalsIgnoreCase(rootDisplayName)) {
            treeUri = null;
            rootDisplayName = "";
            probePassed = false;
            getPreferences(MODE_PRIVATE).edit().remove(PREF_TREE_URI).apply();
            showStatus(
                    "المجلد المختار اسمه: " + queryDisplayName(selected) + ". "
                    + "ارجع إلى مساحة eFootball واختر المجلد data نفسه، وليس files."
            );
            return;
        }

        getPreferences(MODE_PRIVATE).edit().putString(PREF_TREE_URI, selected.toString()).apply();
        runWriteProbe();
    }

    private void restoreTreeSelection() {
        String saved = getPreferences(MODE_PRIVATE).getString(PREF_TREE_URI, "");
        if (saved == null || saved.isEmpty()) return;
        try {
            treeUri = Uri.parse(saved);
            if (!EXPECTED_PROVIDER_AUTHORITY.equals(treeUri.getAuthority())) {
                treeUri = null;
                getPreferences(MODE_PRIVATE).edit().remove(PREF_TREE_URI).apply();
                showStatus("صلاحية التخزين المحفوظة لا تخص مزوّد Ziru_27؛ أعد اختيار مساحة eFootball.");
                return;
            }
            rootDisplayName = queryDisplayName(getRootDocumentUri());
            boolean persisted = false;
            for (android.content.UriPermission permission :
                    getContentResolver().getPersistedUriPermissions()) {
                if (permission.getUri().equals(treeUri) && permission.isWritePermission()) {
                    persisted = true;
                    break;
                }
            }
            if (persisted && "data".equalsIgnoreCase(rootDisplayName)) {
                runWriteProbe();
            } else {
                treeUri = null;
                rootDisplayName = "";
                showStatus("صلاحية المجلد السابقة غير متاحة. أعد اختيار مجلد data.");
            }
        } catch (Throwable failure) {
            treeUri = null;
            showStatus("تعذر استعادة صلاحية المجلد: " + failure);
        }
    }

    private Uri getRootDocumentUri() {
        Uri selected = treeUri;
        if (selected == null) return null;
        String documentId = DocumentsContract.getTreeDocumentId(selected);
        return DocumentsContract.buildDocumentUriUsingTree(selected, documentId);
    }

    private String queryDisplayName(Uri documentUri) {
        if (documentUri == null) return "";
        try (Cursor cursor = getContentResolver().query(
                documentUri,
                new String[]{Document.COLUMN_DISPLAY_NAME},
                null, null, null
        )) {
            if (cursor != null && cursor.moveToFirst()) {
                return cursor.getString(0);
            }
        } catch (Throwable ignored) {
        }
        return "";
    }

    private int queryFlags(Uri documentUri) {
        if (documentUri == null) return 0;
        try (Cursor cursor = getContentResolver().query(
                documentUri,
                new String[]{Document.COLUMN_FLAGS},
                null, null, null
        )) {
            if (cursor != null && cursor.moveToFirst()) return cursor.getInt(0);
        } catch (Throwable ignored) {
        }
        return 0;
    }

    private void runWriteProbe() {
        probePassed = false;
        showStatus("تم اختيار data. يجري اختبار إنشاء وكتابة وإعادة تسمية ملف مؤقت...");
        workers.execute(() -> {
            Uri probe = null;
            try {
                Uri root = getRootDocumentUri();
                int flags = queryFlags(root);
                if ((flags & Document.FLAG_DIR_SUPPORTS_CREATE) == 0) {
                    throw new IllegalStateException("المزوّد لا يعلن دعم إنشاء الملفات داخل مجلد data.");
                }

                String suffix = UUID.randomUUID().toString().replace("-", "");
                String firstName = ".kafianet-saf-probe-" + suffix + ".tmp";
                probe = DocumentsContract.createDocument(
                        getContentResolver(), root, "application/octet-stream", firstName
                );
                if (probe == null) throw new IllegalStateException("تعذر إنشاء ملف الاختبار المؤقت.");

                byte[] marker = "KAFIANET_SAF_WRITE_PROBE".getBytes(StandardCharsets.UTF_8);
                try (OutputStream output = getContentResolver().openOutputStream(probe, "w")) {
                    if (output == null) throw new IllegalStateException("مزوّد الملفات لم يفتح تدفق الكتابة.");
                    output.write(marker);
                    output.flush();
                }

                Uri renamed = DocumentsContract.renameDocument(
                        getContentResolver(), probe, ".kafianet-saf-probe-" + suffix + ".renamed"
                );
                if (renamed == null) {
                    throw new IllegalStateException("الكتابة نجحت لكن إعادة تسمية الملف غير مدعومة.");
                }
                probe = renamed;

                if (!DocumentsContract.deleteDocument(getContentResolver(), probe)) {
                    throw new IllegalStateException("تعذر حذف ملف الاختبار المؤقت بعد نجاح الكتابة.");
                }
                probe = null;
                probePassed = true;
                showStatus(
                        "جاهز للاختبار. المجلد: eFootball / data\n"
                        + "المزوّد: " + treeUri.getAuthority() + "\n"
                        + "تم اختبار الإنشاء والكتابة وإعادة التسمية والحذف بنجاح.\n"
                        + "المنفذ المحلي: " + SERVER_PORT
                );
            } catch (Throwable failure) {
                probePassed = false;
                showStatus("فشل اختبار صلاحية الكتابة في مساحة eFootball: " + failure);
            } finally {
                if (probe != null) {
                    try { DocumentsContract.deleteDocument(getContentResolver(), probe); }
                    catch (Throwable ignored) { }
                }
            }
        });
    }

    private void startHttpServer() {
        workers.execute(() -> {
            try {
                serverSocket = new ServerSocket(
                        SERVER_PORT, 12, InetAddress.getByName("127.0.0.1")
                );
                while (!serverSocket.isClosed()) {
                    Socket client = serverSocket.accept();
                    workers.execute(() -> handleClient(client));
                }
            } catch (Throwable failure) {
                showStatus("تعذر تشغيل خادم الاختبار المحلي: " + failure);
            }
        });
    }

    private void handleClient(Socket socket) {
        try (Socket client = socket) {
            client.setSoTimeout(6 * 60 * 60 * 1000);
            InputStream input = client.getInputStream();
            String headerText = readHeaders(input);
            String[] lines = headerText.split("\\r\\n");
            if (lines.length == 0) {
                writeResponse(client, 400, new JSONObject().put("error", "missing request"));
                return;
            }

            String[] request = lines[0].split(" ");
            if (request.length < 2) {
                writeResponse(client, 400, new JSONObject().put("error", "invalid request line"));
                return;
            }
            String method = request[0].toUpperCase(Locale.ROOT);
            String path = request[1].split("\\?", 2)[0];
            Map<String, String> headers = new ConcurrentHashMap<>();
            for (String line : lines) {
                int split = line.indexOf(':');
                if (split > 0) {
                    headers.put(
                            line.substring(0, split).trim().toLowerCase(Locale.ROOT),
                            line.substring(split + 1).trim()
                    );
                }
            }

            if (!sessionToken.equals(headers.getOrDefault("x-kafia-token", ""))) {
                writeResponse(client, 403, new JSONObject().put("error", "invalid session token"));
                return;
            }

            long contentLength = parseLong(headers.get("content-length"), 0L);
            if (contentLength < 0) {
                writeResponse(client, 400, new JSONObject().put("error", "invalid content length"));
                return;
            }

            if ("GET".equals(method) && "/health".equals(path)) {
                JSONObject health = new JSONObject()
                        .put("ready", treeUri != null && probePassed)
                        .put("rootName", rootDisplayName)
                        .put("authority", treeUri == null ? "" : treeUri.getAuthority())
                        .put("writeProbe", probePassed)
                        .put("port", SERVER_PORT);
                writeResponse(client, 200, health);
                return;
            }

            if ("POST".equals(method) && "/start".equals(path)) {
                JSONObject body = readJson(input, contentLength);
                if (treeUri == null || !probePassed
                        || !"data".equalsIgnoreCase(rootDisplayName)
                        || !EXPECTED_PROVIDER_AUTHORITY.equals(treeUri.getAuthority())) {
                    writeResponse(client, 400, new JSONObject().put("error", "eFootball data folder is not selected or writable"));
                    return;
                }
                activeJob = body.optString("jobId", "");
                expectedFiles = body.optInt("fileCount", 0);
                copiedFiles = 0;
                copiedBytes = 0L;
                jobError = "";
                if (activeJob.isEmpty() || expectedFiles < 1) {
                    writeResponse(client, 400, new JSONObject().put("error", "invalid job metadata"));
                    return;
                }
                BridgeKeepAliveService.transferStarted(this, activeJob);
                writeResponse(client, 200, new JSONObject().put("accepted", true).put("state", "running"));
                return;
            }

            if ("POST".equals(method) && "/file".equals(path)) {
                String jobId = headers.getOrDefault("x-kafia-job", "");
                String encodedPath = headers.getOrDefault("x-kafia-relative", "");
                String relativePath = URLDecoder.decode(encodedPath, "UTF-8");
                if (!activeJob.equals(jobId) || activeJob.isEmpty()) {
                    writeResponse(client, 400, new JSONObject().put("error", "no matching active job"));
                    return;
                }
                JSONObject result = receiveFile(input, relativePath, contentLength);
                writeResponse(client, result.optBoolean("accepted", false) ? 200 : 400, result);
                return;
            }

            if ("POST".equals(method) && "/finish".equals(path)) {
                JSONObject body = readJson(input, contentLength);
                String jobId = body.optString("jobId", "");
                if (!activeJob.equals(jobId) || activeJob.isEmpty()) {
                    writeResponse(client, 400, new JSONObject().put("error", "no matching active job"));
                    return;
                }
                boolean complete = jobError.isEmpty() && copiedFiles == expectedFiles;
                String finishError = jobError;
                if (finishError.isEmpty() && copiedFiles != expectedFiles) {
                    finishError = "File count mismatch: " + copiedFiles + "/" + expectedFiles;
                }
                JSONObject result = new JSONObject()
                        .put("success", complete)
                        .put("copiedFiles", copiedFiles)
                        .put("expectedFiles", expectedFiles)
                        .put("copiedBytes", copiedBytes)
                        .put("error", finishError);
                activeJob = "";
                writeResponse(client, 200, result);
                BridgeKeepAliveService.transferFinished(this, jobId);
                return;
            }

            writeResponse(client, 404, new JSONObject().put("error", "not found"));
        } catch (Throwable failure) {
            android.util.Log.e("KafiaNetEFootballSaf", "HTTP request failed", failure);
            try {
                if (!socket.isClosed()) {
                    writeResponse(socket, 400, new JSONObject().put("error", failure.toString()));
                }
            } catch (Throwable ignored) {
            }
        }
    }

    private JSONObject receiveFile(InputStream input, String relativePath, long contentLength) throws Exception {
        Uri temporary = null;
        Uri backup = null;
        try {
            if (contentLength < 0 || contentLength > (1L << 40)) {
                throw new IllegalArgumentException("invalid file content length");
            }
            String[] segments = validateRelativePath(relativePath);
            Uri parent = getRootDocumentUri();
            for (int i = 0; i < segments.length - 1; i++) {
                parent = ensureDirectory(parent, segments[i]);
            }
            String fileName = segments[segments.length - 1];
            Uri existing = findChild(parent, fileName);
            if (existing != null && isDirectory(existing)) {
                throw new IllegalStateException("a directory already uses the target file name");
            }

            String suffix = UUID.randomUUID().toString().replace("-", "");
            temporary = DocumentsContract.createDocument(
                    getContentResolver(), parent, "application/octet-stream",
                    ".kafianet-" + suffix + ".part"
            );
            if (temporary == null) throw new IllegalStateException("could not create temporary SAF document");

            long remaining = contentLength;
            long received = 0L;
            byte[] buffer = new byte[1024 * 1024];
            try (OutputStream output = getContentResolver().openOutputStream(temporary, "w")) {
                if (output == null) throw new IllegalStateException("SAF provider returned no output stream");
                while (remaining > 0) {
                    int requested = (int) Math.min((long) buffer.length, remaining);
                    int count = input.read(buffer, 0, requested);
                    if (count < 0) throw new EOFException("network stream ended before the file was received");
                    if (count == 0) continue;
                    output.write(buffer, 0, count);
                    received += count;
                    remaining -= count;
                }
                output.flush();
            }

            if (existing != null) {
                backup = DocumentsContract.renameDocument(
                        getContentResolver(), existing, ".kafianet-backup-" + suffix
                );
                if (backup == null) {
                    throw new IllegalStateException("provider cannot safely stage the existing file for replacement");
                }
            }

            Uri completed = DocumentsContract.renameDocument(
                    getContentResolver(), temporary, fileName
            );
            if (completed == null) {
                if (backup != null) {
                    Uri restored = DocumentsContract.renameDocument(
                            getContentResolver(), backup, fileName
                    );
                    backup = null;
                }
                throw new IllegalStateException("provider could not commit the completed temporary file");
            }
            temporary = null;

            if (backup != null) {
                DocumentsContract.deleteDocument(getContentResolver(), backup);
                backup = null;
            }

            copiedFiles++;
            copiedBytes += received;
            showStatus("النسخ جارٍ عبر SAF: " + copiedFiles + " / " + expectedFiles
                    + " ملف — " + relativePath);
            return new JSONObject()
                    .put("accepted", true)
                    .put("relativePath", relativePath)
                    .put("receivedBytes", received);
        } catch (Throwable failure) {
            jobError = failure.toString();
            if (temporary != null) {
                try { DocumentsContract.deleteDocument(getContentResolver(), temporary); }
                catch (Throwable ignored) { }
            }
            if (backup != null) {
                try {
                    String originalName = relativePath.substring(relativePath.lastIndexOf('/') + 1);
                    DocumentsContract.renameDocument(getContentResolver(), backup, originalName);
                } catch (Throwable ignored) {
                }
            }
            android.util.Log.e("KafiaNetEFootballSaf", "SAF file write failed: " + relativePath, failure);
            return new JSONObject()
                    .put("accepted", false)
                    .put("relativePath", relativePath)
                    .put("error", failure.toString());
        }
    }

    private String[] validateRelativePath(String relativePath) {
        if (relativePath == null || relativePath.trim().isEmpty()) {
            throw new IllegalArgumentException("relative file path is empty");
        }
        String normalized = relativePath.replace('\\', '/');
        if (normalized.startsWith("/") || normalized.contains(":") || normalized.indexOf('\0') >= 0) {
            throw new SecurityException("absolute or invalid destination path rejected");
        }
        String[] segments = normalized.split("/", -1);
        for (String segment : segments) {
            if (segment.isEmpty() || ".".equals(segment) || "..".equals(segment)) {
                throw new SecurityException("unsafe relative path segment rejected");
            }
        }
        return segments;
    }

    private Uri ensureDirectory(Uri parent, String name) throws Exception {
        Uri existing = findChild(parent, name);
        if (existing != null) {
            if (!isDirectory(existing)) {
                throw new IllegalStateException("a file blocks destination directory: " + name);
            }
            return existing;
        }
        Uri created = DocumentsContract.createDocument(
                getContentResolver(), parent, Document.MIME_TYPE_DIR, name
        );
        if (created == null) throw new IllegalStateException("could not create destination directory: " + name);
        return created;
    }

    private Uri findChild(Uri parent, String name) throws Exception {
        String parentId = DocumentsContract.getDocumentId(parent);
        Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentId);
        String[] projection = {
                Document.COLUMN_DOCUMENT_ID,
                Document.COLUMN_DISPLAY_NAME,
                Document.COLUMN_MIME_TYPE
        };
        try (Cursor cursor = getContentResolver().query(children, projection, null, null, null)) {
            if (cursor == null) throw new IllegalStateException("SAF provider returned no child listing");
            int idColumn = cursor.getColumnIndexOrThrow(Document.COLUMN_DOCUMENT_ID);
            int nameColumn = cursor.getColumnIndexOrThrow(Document.COLUMN_DISPLAY_NAME);
            while (cursor.moveToNext()) {
                if (name.equals(cursor.getString(nameColumn))) {
                    String id = cursor.getString(idColumn);
                    return DocumentsContract.buildDocumentUriUsingTree(treeUri, id);
                }
            }
        }
        return null;
    }

    private boolean isDirectory(Uri documentUri) {
        try (Cursor cursor = getContentResolver().query(
                documentUri, new String[]{Document.COLUMN_MIME_TYPE}, null, null, null
        )) {
            return cursor != null && cursor.moveToFirst()
                    && Document.MIME_TYPE_DIR.equals(cursor.getString(0));
        } catch (Throwable ignored) {
            return false;
        }
    }

    private JSONObject readJson(InputStream input, long length) throws Exception {
        if (length > 1024 * 1024) throw new IllegalArgumentException("JSON request body is too large");
        byte[] body = readExactly(input, (int) length);
        return new JSONObject(new String(body, StandardCharsets.UTF_8));
    }

    private byte[] readExactly(InputStream input, int length) throws Exception {
        byte[] result = new byte[length];
        int offset = 0;
        while (offset < length) {
            int count = input.read(result, offset, length - offset);
            if (count < 0) throw new EOFException("incomplete HTTP request body");
            if (count == 0) continue;
            offset += count;
        }
        return result;
    }

    private String readHeaders(InputStream input) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        int state = 0;
        while (output.size() < 65536) {
            int value = input.read();
            if (value < 0) throw new EOFException("connection closed before HTTP headers");
            output.write(value);
            if ((state == 0 || state == 2) && value == '\r') state++;
            else if ((state == 1 || state == 3) && value == '\n') state++;
            else state = value == '\r' ? 1 : 0;
            if (state == 4) return output.toString("ISO-8859-1");
        }
        throw new IllegalArgumentException("HTTP headers are too large");
    }

    private long parseLong(String value, long fallback) {
        try { return Long.parseLong(value == null ? "" : value); }
        catch (NumberFormatException ignored) { return fallback; }
    }

    private void writeResponse(Socket socket, int code, JSONObject body) throws Exception {
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        String reason = code == 200 ? "OK" : (code == 403 ? "Forbidden" : "Error");
        String headers = "HTTP/1.1 " + code + " " + reason + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Content-Length: " + bytes.length + "\r\n"
                + "Connection: close\r\n\r\n";
        OutputStream output = socket.getOutputStream();
        output.write(headers.getBytes(StandardCharsets.ISO_8859_1));
        output.write(bytes);
        output.flush();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        String unfinishedJob = activeJob;
        activeJob = "";
        if (!unfinishedJob.isEmpty()) {
            BridgeKeepAliveService.transferFinished(this, unfinishedJob);
        }
        BridgeKeepAliveService.stopSession(this);
        try { if (serverSocket != null) serverSocket.close(); } catch (Throwable ignored) { }
        workers.shutdownNow();
    }
}
