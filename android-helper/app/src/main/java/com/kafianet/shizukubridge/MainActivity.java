package com.kafianet.shizukubridge;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.provider.DocumentsContract;
import android.content.UriPermission;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.view.Gravity;
import android.widget.LinearLayout;
import android.widget.TextView;


import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.UUID;
import java.util.Map;
import java.util.Collections;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import rikka.shizuku.Shizuku;

public final class MainActivity extends Activity {
    private static final int REQUEST_SHIZUKU = 7301;
    private static final int REQUEST_LOCAL_TREE = 7302;
    private static final int REQUEST_SOURCE_READ_PERMISSION = 7303;
    private static final String EXTRA_KAFIANET_ACTION = "kafianet_action";
    private static final String ACTION_REQUEST_LOCAL_FINALIZE = "request_local_finalize";
    private static final String TREE_PREFS = "kafianet_storage_permissions";
    private static final String SOURCE_ROOT_EXTRA = "source_root";
    private static final String TARGET_RELATIVE_EXTRA = "target_relative";
    private static final String GAME_EXTRA = "game";
    private static final int SERVER_PORT = 18765;
    private static final long SHIZUKU_PERMISSION_WAIT_TIMEOUT_MS = 15L * 60L * 1000L;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService workers = Executors.newCachedThreadPool();
    private final Map<String, JSONObject> jobs = new ConcurrentHashMap<>();
    private final Map<String, String> directStreamDestinations = new ConcurrentHashMap<>();
    private final Set<String> cancelledJobs = Collections.newSetFromMap(new ConcurrentHashMap<>());

    private volatile String state = "starting";
    private volatile String error = "";
    private volatile String sessionToken = "";
    private volatile String targetPackage = "";
    private volatile ITransferUserService userService;
    private volatile ServerSocket serverSocket;
    private volatile TextView messageView;
    private volatile boolean bindingService = false;
    private volatile boolean serviceBound = false;
    private volatile boolean permissionRequestInFlight = false;
    private volatile long permissionRequestStartedAt = 0L;
    private String pendingSourceRoot = "";
    private String pendingTargetRelative = "";
    private String pendingGame = "PUBG Mobile";
    private boolean shizukuListenersRegistered = false;
    private boolean httpServerStarted = false;

    // Poll permission state if the first-run Shizuku result callback is delayed.
    // This resumes the current transfer as soon as the user grants permission.
    private final Runnable permissionGrantMonitor = new Runnable() {
        @Override
        public void run() {
            if (!permissionRequestInFlight || isFinishing()) return;
            try {
                if (Shizuku.pingBinder()
                        && Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED) {
                    permissionRequestInFlight = false;
                    permissionRequestStartedAt = 0L;
                    main.removeCallbacks(this);
                    android.util.Log.i("KafiaNetBridge",
                            "Shizuku permission detected while waiting; continuing setup.");
                    bindTransferService();
                    return;
                }
            } catch (Throwable ignored) {
                // Wait while Shizuku starts or its permission UI is open.
            }
            long elapsed = SystemClock.elapsedRealtime() - permissionRequestStartedAt;
            if (elapsed >= SHIZUKU_PERMISSION_WAIT_TIMEOUT_MS) {
                permissionRequestInFlight = false;
                permissionRequestStartedAt = 0L;
                fail("انتهت مهلة انتظار صلاحية Shizuku. امنح التطبيق الإذن ثم أعد المحاولة.");
                return;
            }
            main.postDelayed(this, 500L);
        }
    };


    private final Shizuku.OnBinderReceivedListener binderReceived = this::initializeShizuku;
    private final Shizuku.OnBinderDeadListener binderDead = () -> {
        userService = null;
        bindingService = false;
        serviceBound = false;
        permissionRequestInFlight = false;
        permissionRequestStartedAt = 0L;
        main.removeCallbacks(permissionGrantMonitor);
        state = "waiting_shizuku";
        showMessage("انقطع اتصال Shizuku. عند إعادة تشغيله سيعيد KafiaNet ربط خدمة النقل.");
    };
    private final Shizuku.OnRequestPermissionResultListener permissionResult =
            (requestCode, grantResult) -> {
                if (requestCode != REQUEST_SHIZUKU) return;
                boolean granted = grantResult == PackageManager.PERMISSION_GRANTED;
                if (!granted) {
                    try {
                        granted = Shizuku.pingBinder()
                                && Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED;
                    } catch (Throwable ignored) {
                    }
                }
                permissionRequestInFlight = false;
                permissionRequestStartedAt = 0L;
                main.removeCallbacks(permissionGrantMonitor);
                final boolean permissionGranted = granted;
                main.post(() -> {
                    if (permissionGranted) {
                        android.util.Log.i("KafiaNetBridge",
                                "Shizuku permission granted; continuing setup.");
                        bindTransferService();
                    } else {
                        fail("لم يمنح الهاتف تطبيق KafiaNet إذن Shizuku.");
                    }
                });
            };

    private final ServiceConnection serviceConnection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder binder) {
            bindingService = false;
            if (binder == null) {
                serviceBound = false;
                fail("KAFIANET_SHIZUKU_USER_SERVICE_BIND_FAILED: وصل Shizuku إلى KafiaNet دون Binder صالح لخدمة النقل.");
                return;
            }
            serviceBound = true;
            userService = ITransferUserService.Stub.asInterface(binder);
            main.post(() -> {
                try {
                    ITransferUserService connected = userService;
                    if (connected == null) {
                        throw new IllegalStateException("Shizuku connected without a usable file service.");
                    }
                    String identity = connected.ping();
                    if (!identity.contains("uid=2000") && !identity.contains("uid=0")) {
                        fail("Shizuku لم يعمل بهوية ADB Shell أو Root (المتوقع UID 2000 أو UID 0): " + identity);
                        return;
                    }
                    prepareDestinationAccess();
                } catch (Throwable failure) {
                    fail("KAFIANET_SHIZUKU_USER_SERVICE_INIT_FAILED: تعذر تهيئة خدمة نقل Shizuku: " + failure);
                }
            });
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            bindingService = false;
            serviceBound = false;
            userService = null;
            state = "service_disconnected";
            error = "KAFIANET_SHIZUKU_USER_SERVICE_DISCONNECTED: انقطع اتصال خدمة Shizuku.";
            showMessage(error + " سيحاول KafiaNet إعادة الربط عند عودة الخادم.");
        }
    };

    private final Shizuku.UserServiceArgs serviceArgs =
            new Shizuku.UserServiceArgs(new ComponentName(
                    BuildConfig.APPLICATION_ID,
                    TransferUserService.class.getName()))
                    .daemon(false)
                    .processNameSuffix("transfer")
                    .debuggable(false)
                    .version(13)
                    .tag("kafianet-file-transfer");

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(createContent());
        readSession(getIntent());
        if (isLocalFinalizeRequest(getIntent())) {
            requestLocalFinalizeTreeAccess();
        } else {
            activateBridgeMode();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readSession(intent);
        if (isLocalFinalizeRequest(intent)) {
            requestLocalFinalizeTreeAccess();
            return;
        }
        activateBridgeMode();
        if (userService != null && Shizuku.pingBinder()) {
            prepareDestinationAccess();
        } else {
            initializeShizuku();
        }
    }

    private boolean isLocalFinalizeRequest(Intent intent) {
        return intent != null
                && ACTION_REQUEST_LOCAL_FINALIZE.equals(
                        intent.getStringExtra(EXTRA_KAFIANET_ACTION)
                );
    }

    private void activateBridgeMode() {
        if (!httpServerStarted) {
            httpServerStarted = true;
            startHttpServer();
        }
        if (!shizukuListenersRegistered) {
            shizukuListenersRegistered = true;
            // Register the result/death callbacks before the sticky binder listener:
            // the sticky callback may run synchronously when Shizuku is already connected.
            Shizuku.addRequestPermissionResultListener(permissionResult);
            Shizuku.addBinderDeadListener(binderDead);
            Shizuku.addBinderReceivedListenerSticky(binderReceived);
        }
    }

    private void requestLocalFinalizeTreeAccess() {
        targetPackage = getIntent().getStringExtra("package");
        if (targetPackage == null || !targetPackage.matches("[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+")) {
            showMessage("معرّف حزمة اللعبة غير صالح؛ لم نفتح منتقي الملفات.");
            return;
        }
        pendingSourceRoot = getIntent().getStringExtra(SOURCE_ROOT_EXTRA);
        pendingTargetRelative = getIntent().getStringExtra(TARGET_RELATIVE_EXTRA);
        pendingGame = getIntent().getStringExtra(GAME_EXTRA);
        if (pendingSourceRoot == null || pendingTargetRelative == null
                || pendingSourceRoot.trim().isEmpty() || pendingTargetRelative.trim().isEmpty()) {
            showMessage("بيانات النقل المحلي غير مكتملة. أعد تشغيل العملية من تطبيق الكمبيوتر.");
            return;
        }
        if (pendingGame == null || pendingGame.trim().isEmpty()) pendingGame = targetPackage;

        if (Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(android.Manifest.permission.READ_EXTERNAL_STORAGE)
                        != PackageManager.PERMISSION_GRANTED) {
            showMessage("اسمح للمساعد بقراءة مجلد النسخة الموجودة في الجذر أولاً.");
            requestPermissions(
                    new String[]{android.Manifest.permission.READ_EXTERNAL_STORAGE},
                    REQUEST_SOURCE_READ_PERMISSION
            );
            return;
        }
        Uri savedTree = getPersistedTreeForPackage(targetPackage);
        if (savedTree != null) {
            showMessage("صلاحية مجلد " + targetPackage + " محفوظة. يجري بدء النقل المحلي...");
            startLocalFinalizeService(savedTree);
            return;
        }
        openLocalFinalizeTreePicker();
    }

    private void openLocalFinalizeTreePicker() {
        try {
            Uri initialUri = DocumentsContract.buildDocumentUri(
                    "com.android.externalstorage.documents",
                    "primary:Android/data/" + targetPackage
            );
            Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
            // EXTRA_INITIAL_URI was added in API 26. On API 24-25 open the
            // picker normally and let the user navigate to the PUBG directory.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                picker.putExtra(DocumentsContract.EXTRA_INITIAL_URI, initialUri);
            }
            picker.addFlags(
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                            | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                            | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION
            );
            showMessage("اختر مجلد " + targetPackage
                    + " داخل Android/data ثم اضغط «استخدام هذا المجلد». بعد الموافقة يبدأ المساعد النقل محلياً.");
            startActivityForResult(picker, REQUEST_LOCAL_TREE);
        } catch (Throwable failure) {
            android.util.Log.e("KafiaNetBridge", "Could not open Android/data SAF tree picker", failure);
            showMessage("تعذر فتح نافذة مجلد اللعبة: " + failure.getMessage());
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_LOCAL_TREE) return;

        if (resultCode != Activity.RESULT_OK || data == null || data.getData() == null) {
            showMessage("تم إلغاء الإذن. بقيت ملفات الجذر كما هي ولم يبدأ النقل المحلي.");
            return;
        }

        Uri selectedTree = data.getData();
        try {
            if (!DocumentsContract.isTreeUri(selectedTree)) {
                showMessage("الاختيار غير صالح: لم يُرجع النظام صلاحية مجلد كاملة.");
                return;
            }

            String selectedDocumentId = DocumentsContract.getTreeDocumentId(selectedTree);
            String expectedDocumentId = "primary:Android/data/" + targetPackage;
            if (!"com.android.externalstorage.documents".equals(selectedTree.getAuthority())
                    || !expectedDocumentId.equals(selectedDocumentId)) {
                showMessage(
                        "لم يتم حفظ الإذن. اختر مجلد " + targetPackage + " نفسه، وليس Android/data أو مجلداً آخر."
                );
                return;
            }

            int grantFlags = data.getFlags() & (
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            );
            if ((grantFlags & Intent.FLAG_GRANT_READ_URI_PERMISSION) == 0
                    || (grantFlags & Intent.FLAG_GRANT_WRITE_URI_PERMISSION) == 0) {
                showMessage("النظام لم يمنح صلاحية القراءة والكتابة معاً؛ لم يتم اعتماد هذا الاختيار.");
                return;
            }

            getContentResolver().takePersistableUriPermission(selectedTree, grantFlags);

            boolean persisted = false;
            for (UriPermission permission : getContentResolver().getPersistedUriPermissions()) {
                if (selectedTree.equals(permission.getUri())
                        && permission.isReadPermission()
                        && permission.isWritePermission()) {
                    persisted = true;
                    break;
                }
            }
            if (!persisted) {
                showMessage("لم يستطع النظام حفظ صلاحية مجلد PUBG بشكل دائم. أعد المحاولة.");
                return;
            }

            getSharedPreferences(TREE_PREFS, MODE_PRIVATE)
                    .edit()
                    .putString(treeUriKey(targetPackage), selectedTree.toString())
                    .apply();

            showMessage("تم حفظ إذن مجلد " + targetPackage
                    + ". يجري الآن تشغيل النقل داخل الهاتف...");
            startLocalFinalizeService(selectedTree);
            android.util.Log.i("KafiaNetBridge", "Persisted local SAF tree grant: " + selectedTree);
        } catch (Throwable failure) {
            android.util.Log.e("KafiaNetBridge", "Could not persist PUBG SAF tree grant", failure);
            showMessage("تعذر حفظ إذن مجلد PUBG: " + failure.getMessage());
        }
    }

    private String treeUriKey(String packageName) {
        return "tree_uri_" + packageName;
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_SOURCE_READ_PERMISSION) return;
        if (grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            openLocalFinalizeTreePicker();
        } else {
            showMessage("لم تُمنح صلاحية قراءة مجلد الجذر. بقيت الملفات كما هي ولم يبدأ النقل.");
        }
    }

    private void startLocalFinalizeService(Uri selectedTree) {
        try {
            getSharedPreferences(PubgOfflineFinalizeService.PREFS, MODE_PRIVATE)
                    .edit()
                    .putString(PubgOfflineFinalizeService.KEY_SOURCE_ROOT, pendingSourceRoot)
                    .putString(PubgOfflineFinalizeService.KEY_TARGET_PACKAGE, targetPackage)
                    .putString(PubgOfflineFinalizeService.KEY_TARGET_RELATIVE, pendingTargetRelative)
                    .putString(PubgOfflineFinalizeService.KEY_GAME, pendingGame)
                    .apply();

            Intent service = new Intent(this, PubgOfflineFinalizeService.class);
            service.setAction(PubgOfflineFinalizeService.ACTION_START);
            service.putExtra(PubgOfflineFinalizeService.EXTRA_SOURCE_ROOT, pendingSourceRoot);
            service.putExtra(PubgOfflineFinalizeService.EXTRA_TARGET_PACKAGE, targetPackage);
            service.putExtra(PubgOfflineFinalizeService.EXTRA_TARGET_RELATIVE, pendingTargetRelative);
            service.putExtra(PubgOfflineFinalizeService.EXTRA_GAME, pendingGame);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(service);
            } else {
                startService(service);
            }
            showMessage("بدأ النقل المحلي على الهاتف. انتظر ظهور النسبة قبل فصل كابل USB.");
            Intent home = new Intent(this, HomeActivity.class);
            home.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP);
            startActivity(home);
            android.util.Log.i("KafiaNetBridge",
                    "Started phone-local SAF copy for " + targetPackage + " using " + selectedTree);
        } catch (Throwable failure) {
            android.util.Log.e("KafiaNetBridge", "Could not start local SAF copy service", failure);
            showMessage("تم حفظ الإذن، لكن تعذر بدء خدمة النقل المحلي: " + failure.getMessage()
                    + "\nنسخة الجذر ما زالت محفوظة.");
        }
    }

    private Uri getPersistedTreeForPackage(String packageName) {
        if (packageName == null || packageName.isEmpty()) return null;
        String stored = getSharedPreferences(TREE_PREFS, MODE_PRIVATE)
                .getString(treeUriKey(packageName), "");
        if (stored == null || stored.isEmpty()) return null;
        Uri expectedUri = Uri.parse(stored);
        String expectedId = "primary:Android/data/" + packageName;
        if (!DocumentsContract.isTreeUri(expectedUri)
                || !"com.android.externalstorage.documents".equals(expectedUri.getAuthority())
                || !expectedId.equals(DocumentsContract.getTreeDocumentId(expectedUri))) {
            return null;
        }
        for (UriPermission permission : getContentResolver().getPersistedUriPermissions()) {
            if (expectedUri.equals(permission.getUri())
                    && permission.isReadPermission()
                    && permission.isWritePermission()) {
                return expectedUri;
            }
        }
        return null;
    }

    private boolean hasPersistedPubgTreePermission() {
        return getPersistedTreeForPackage(targetPackage) != null;
    }

    private void readSession(Intent intent) {
        if (intent == null) return;
        String newToken = intent.getStringExtra("token");
        String newPackage = intent.getStringExtra("package");
        if (newToken != null && !newToken.isEmpty()) sessionToken = newToken;
        if (newPackage != null) targetPackage = newPackage;
        if (targetPackage.isEmpty()) targetPackage = "com.tencent.ig";
        showMessage("يجري تجهيز صلاحية نقل ملفات اللعبة على هذا الهاتف فقط...");
    }

    private LinearLayout createContent() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setGravity(Gravity.CENTER);
        layout.setPadding(36, 36, 36, 36);
        messageView = new TextView(this);
        messageView.setTextSize(18);
        messageView.setGravity(Gravity.CENTER);
        messageView.setText("KafiaNet — جاري تهيئة Shizuku");
        layout.addView(messageView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        return layout;
    }

    private void showMessage(String message) {
        main.post(() -> {
            if (messageView != null) messageView.setText(message);
        });
    }

    private void fail(String reason) {
        permissionRequestInFlight = false;
        permissionRequestStartedAt = 0L;
        main.removeCallbacks(permissionGrantMonitor);
        state = "failed";
        error = reason == null ? "Unknown error" : reason;
        showMessage("تعذر تجهيز النسخ إلى مجلد اللعبة.\n" + error);
    }

    private void initializeShizuku() {
        try {
            if (!Shizuku.pingBinder()) {
                state = "waiting_shizuku";
                showMessage("بانتظار خادم Shizuku. افتح تطبيق Shizuku وشغّل الخدمة ثم أعد المحاولة.");
                return;
            }
            if (Shizuku.isPreV11()) {
                fail("إصدار Android قديم ولا يدعم واجهة Shizuku المطلوبة.");
                return;
            }
            if (userService != null) {
                prepareDestinationAccess();
                return;
            }
            if (bindingService || serviceBound) {
                return;
            }
            if (Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED) {
                bindTransferService();
            } else {
                if (permissionRequestInFlight) return;
                state = "permission_required";
                permissionRequestInFlight = true;
                permissionRequestStartedAt = SystemClock.elapsedRealtime();
                showMessage("بانتظار موافقتك على صلاحية Shizuku على الهاتف...");
                main.removeCallbacks(permissionGrantMonitor);
                main.postDelayed(permissionGrantMonitor, 500L);
                Shizuku.requestPermission(REQUEST_SHIZUKU);
            }
        } catch (Throwable failure) {
            permissionRequestInFlight = false;
            permissionRequestStartedAt = 0L;
            main.removeCallbacks(permissionGrantMonitor);
            fail("KAFIANET_SHIZUKU_PERMISSION_OR_BINDER_ERROR: تعذر الاتصال بخدمة Shizuku أو التحقق من الإذن: " + failure);
        }
    }

    private void bindTransferService() {
        try {
            if (!Shizuku.pingBinder()) {
                state = "waiting_shizuku";
                showMessage("خادم Shizuku غير متصل؛ لم يبدأ النقل.");
                return;
            }
            if (userService != null) {
                prepareDestinationAccess();
                return;
            }
            if (bindingService || serviceBound) return;

            bindingService = true;
            state = "binding_service";
            showMessage("تم منح إذن Shizuku. جاري ربط محرك النقل...");
            Shizuku.bindUserService(serviceArgs, serviceConnection);
        } catch (Throwable failure) {
            bindingService = false;
            serviceBound = false;
            fail("KAFIANET_SHIZUKU_USER_SERVICE_BIND_FAILED: تعذر ربط محرك النقل داخل Shizuku: " + failure);
        }
    }

    private void prepareDestinationAccess() {
        if (userService == null) return;
        try {
            // Do not probe, compare, or pre-authorize a destination here.
            // The transfer job supplies its own destination at /stream/start.
            state = "ready_direct";
            error = "";
            showMessage(
                    "Shizuku جاهز. سيكتب الملفات مباشرة إلى مسار النقل الذي يرسله الكمبيوتر."
            );
        } catch (Throwable failure) {
            String detail = String.valueOf(failure);
            String lowered = detail.toLowerCase(Locale.ROOT);
            if (lowered.contains("kafianet_destination_missing")) {
                fail("KAFIANET_DESTINATION_MISSING: " + detail);
            } else if (lowered.contains("permission denied")
                    || lowered.contains("eacces")
                    || lowered.contains("operation not permitted")
                    || lowered.contains("cannot prepare/write")) {
                fail("KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED: تعذر فحص صلاحية Saved: " + detail);
            } else {
                fail("KAFIANET_SHIZUKU_DESTINATION_CHECK_FAILED: تعذر فحص صلاحية Shizuku: " + detail);
            }
        }
    }

    private void startHttpServer() {
        workers.execute(() -> {
            try {
                serverSocket = new ServerSocket(SERVER_PORT, 12,
                        InetAddress.getByName("127.0.0.1"));
                while (!serverSocket.isClosed()) {
                    Socket client = serverSocket.accept();
                    workers.execute(() -> handleHttp(client));
                }
            } catch (Throwable failure) {
                android.util.Log.e("KafiaNetBridge", "HTTP server failed", failure);
            }
        });
    }

    private void handleHttp(Socket socket) {
        // Keep the client socket open through the catch block. A try-with-resources
        // around the request body closes it before the error response can be sent,
        // which previously hid the actual Shizuku/binder/stream exception.
        Socket client = socket;
        String requestLine = "unknown";
        String peer = "unknown";
        try {
            if (client == null) return;
            peer = String.valueOf(client.getRemoteSocketAddress());
            client.setSoTimeout(6 * 60 * 60 * 1000);
            BufferedInputStream input = new BufferedInputStream(client.getInputStream());
            ByteArrayOutputStream headerBytes = new ByteArrayOutputStream();
            int matched = 0;
            while (headerBytes.size() < 65536) {
                int value = input.read();
                if (value < 0) return;
                headerBytes.write(value);
                if ((matched == 0 && value == '\r')
                        || (matched == 2 && value == '\r')) {
                    matched++;
                } else if ((matched == 1 && value == '\n')
                        || (matched == 3 && value == '\n')) {
                    matched++;
                } else {
                    matched = value == '\r' ? 1 : 0;
                }
                if (matched == 4) break;
            }
            String headers = headerBytes.toString(StandardCharsets.ISO_8859_1.name());
            String[] lines = headers.split("\r\n");
            if (lines.length == 0) return;
            requestLine = lines[0];
            String[] request = lines[0].split(" ");
            if (request.length < 2) return;
            String method = request[0].toUpperCase(Locale.ROOT);
            String path = request[1];
            Map<String, String> headerMap = new ConcurrentHashMap<>();
            for (String line : lines) {
                int separator = line.indexOf(':');
                if (separator < 0) continue;
                headerMap.put(
                        line.substring(0, separator).trim().toLowerCase(Locale.ROOT),
                        line.substring(separator + 1).trim()
                );
            }

            String token = headerMap.getOrDefault("x-kafia-token", "");
            if (sessionToken.isEmpty() || !sessionToken.equals(token)) {
                writeResponse(client, 403, new JSONObject().put("error", "forbidden"));
                return;
            }

            long contentLength;
            try {
                contentLength = Long.parseLong(headerMap.getOrDefault("content-length", "0"));
            } catch (NumberFormatException invalidLength) {
                writeResponse(client, 400, new JSONObject().put("error", "invalid content length"));
                return;
            }
            if (contentLength < 0) {
                writeResponse(client, 400, new JSONObject().put("error", "invalid content length"));
                return;
            }

            // The HTTP body is streamed through a pipe into the Shizuku UID=2000
            // service. No intermediate copy is made in helper app storage.
            if ("POST".equals(method) && "/stream/file".equals(path)) {
                if (contentLength != parseHeaderLong(headerMap, "x-kafia-size", -1L)) {
                    writeResponse(client, 400, new JSONObject().put("error", "file size and HTTP body length differ"));
                    return;
                }
                JSONObject result = receiveDirectStreamFile(input, headerMap, contentLength);
                writeResponse(client, result.optBoolean("accepted", false) ? 200 : 400, result);
                return;
            }

            if (contentLength > 2 * 1024 * 1024) {
                writeResponse(client, 413, new JSONObject().put("error", "JSON request body too large"));
                return;
            }
            int bodyLength = (int) contentLength;
            byte[] body = new byte[bodyLength];
            int read = 0;
            while (read < bodyLength) {
                int count = input.read(body, read, bodyLength - read);
                if (count < 0) break;
                read += count;
            }
            if (read != bodyLength) {
                writeResponse(client, 400, new JSONObject().put("error", "incomplete request body"));
                return;
            }

            JSONObject result;
            boolean shutdownRequested = false;
            if ("GET".equals(method) && path.startsWith("/health")) {
                result = health();
            } else if ("GET".equals(method) && path.startsWith("/status")) {
                android.net.Uri parsed = android.net.Uri.parse(path);
                String jobId = parsed.getQueryParameter("jobId");
                if (userService != null) {
                    result = new JSONObject(userService.getCopyStatus(jobId));
                } else {
                    result = jobs.get(jobId);
                }
                if (result == null) result = new JSONObject().put("state", "missing").put("success", false);
            } else if ("POST".equals(method) && "/cancel".equals(path)) {
                JSONObject payload = new JSONObject(new String(body, 0, read, StandardCharsets.UTF_8));
                String jobId = payload.getString("jobId");
                cancelledJobs.add(jobId);
                if (userService != null) userService.cancelCopy(jobId);
                directStreamDestinations.remove(jobId);
                result = new JSONObject().put("accepted", true);
            } else if ("POST".equals(method) && "/destination/check".equals(path)) {
                JSONObject payload = new JSONObject(new String(body, 0, read, StandardCharsets.UTF_8));
                String destination = payload.getString("destination");
                if (userService == null || !state.startsWith("ready_")) {
                    throw new IllegalStateException("Shizuku is not ready for destination inspection");
                }
                result = new JSONObject(userService.inspectDestination(destination));
            } else if ("POST".equals(method) && "/stream/check".equals(path)) {
                JSONObject payload = new JSONObject(new String(body, 0, read, StandardCharsets.UTF_8));
                String jobId = payload.getString("jobId");
                JSONArray files = payload.getJSONArray("files");
                String destination = directStreamDestinations.get(jobId);
                if (userService == null || !state.startsWith("ready_")
                        || destination == null || destination.isEmpty()) {
                    throw new IllegalStateException("No active Shizuku stream job for sync check");
                }
                result = new JSONObject(userService.checkDirectStreamFiles(
                        jobId, destination, files.toString()
                ));
            } else if ("POST".equals(method) && "/stream/start".equals(path)) {
                JSONObject payload = new JSONObject(new String(body, 0, read, StandardCharsets.UTF_8));
                String jobId = payload.getString("jobId");
                String destination = payload.getString("destination");
                long totalBytes = payload.getLong("totalBytes");
                int totalFiles = payload.getInt("totalFiles");
                if (userService == null || !state.startsWith("ready_")) {
                    throw new IllegalStateException("Shizuku is not ready");
                }
                boolean accepted = userService.beginDirectStreamJob(
                        jobId, destination, totalBytes, totalFiles
                );
                if (!accepted) {
                    throw new IllegalStateException(
                            "Shizuku could not create a direct-stream job."
                    );
                }
                directStreamDestinations.put(jobId, destination);
                result = new JSONObject()
                        .put("accepted", true)
                        .put("state", "running");
            } else if ("POST".equals(method) && "/stream/finish".equals(path)) {
                JSONObject payload = new JSONObject(new String(body, 0, read, StandardCharsets.UTF_8));
                String jobId = payload.getString("jobId");
                if (userService == null) throw new IllegalStateException("Shizuku is not ready");
                userService.finishDirectStreamJob(jobId);
                result = new JSONObject(userService.getCopyStatus(jobId));
                directStreamDestinations.remove(jobId);
            } else if ("POST".equals(method) && "/shutdown".equals(path)) {
                result = new JSONObject().put("accepted", true);
                shutdownRequested = true;
            } else {
                writeResponse(client, 404, new JSONObject().put("error", "not found"));
                return;
            }
            writeResponse(client, 200, result);
            if (shutdownRequested) scheduleShutdownAfterResponse();
        } catch (Throwable failure) {
            android.util.Log.e(
                    "KafiaNetBridge",
                    "HTTP request failed [" + requestLine + "] peer=" + peer,
                    failure
            );
            try {
                if (client != null && !client.isClosed()) {
                    JSONObject errorBody = new JSONObject()
                            .put("error", failure.toString())
                            .put("exception", failure.getClass().getName())
                            .put("request", requestLine);
                    writeResponse(client, 400, errorBody);
                }
            } catch (Throwable responseFailure) {
                android.util.Log.w(
                        "KafiaNetBridge",
                        "Could not send HTTP error response [" + requestLine + "] peer=" + peer,
                        responseFailure
                );
            }
        } finally {
            if (client != null) {
                try {
                    client.close();
                } catch (Throwable closeFailure) {
                    android.util.Log.w("KafiaNetBridge", "Could not close HTTP client socket", closeFailure);
                }
            }
        }
    }

    private long parseHeaderLong(Map<String, String> headers, String key, long fallback) {
        try {
            return Long.parseLong(headers.getOrDefault(key, String.valueOf(fallback)));
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private JSONObject receiveDirectStreamFile(
            InputStream input, Map<String, String> headers, long contentLength) throws Exception {
        String jobId = headers.getOrDefault("x-kafia-job", "");
        String encodedRelative = headers.getOrDefault("x-kafia-relative", "");
        String relativePath = URLDecoder.decode(encodedRelative, "UTF-8");
        long expectedSize = parseHeaderLong(headers, "x-kafia-size", -1L);
        long modifiedTime = parseHeaderLong(headers, "x-kafia-modified", 0L);
        if (jobId.isEmpty() || relativePath.isEmpty() || expectedSize < 0
                || expectedSize != contentLength) {
            return new JSONObject().put("accepted", false).put("error", "invalid stream metadata");
        }
        if (userService == null || !state.startsWith("ready_")) {
            return new JSONObject().put("accepted", false).put("error", "Shizuku is not ready");
        }
        String destination = directStreamDestinations.get(jobId);
        if (destination == null || destination.isEmpty()) {
            return new JSONObject().put("accepted", false)
                    .put("error", "direct stream destination was not registered for this job");
        }

        JSONObject current = new JSONObject(userService.getCopyStatus(jobId));
        if (!"running".equals(current.optString("state"))) {
            return new JSONObject().put("accepted", false).put("error", "direct stream job is not running");
        }

        String fileId = UUID.randomUUID().toString().replace("-", "");
        ParcelFileDescriptor[] pipe = ParcelFileDescriptor.createPipe();
        boolean started = false;
        try {
            started = userService.startDirectStreamFile(
                    jobId,
                    fileId,
                    destination,
                    relativePath,
                    expectedSize,
                    modifiedTime,
                    pipe[0]
            );
            if (!started) {
                try { pipe[1].close(); } catch (Exception ignored) { }
                String startError = userService.getLastStartError();
                return new JSONObject().put("accepted", false)
                        .put("error", startError == null || startError.isEmpty()
                                ? "KAFIANET_SHIZUKU_FILE_START_FAILED: Shizuku could not start the destination file writer"
                                : startError);
            }
        } finally {
            try { pipe[0].close(); } catch (Exception ignored) { }
        }

        long received = 0;
        try (ParcelFileDescriptor.AutoCloseOutputStream output =
                     new ParcelFileDescriptor.AutoCloseOutputStream(pipe[1])) {
            byte[] buffer = new byte[4 * 1024 * 1024];
            while (received < contentLength) {
                int requested = (int) Math.min(buffer.length, contentLength - received);
                int count = input.read(buffer, 0, requested);
                if (count < 0) {
                    throw new java.io.EOFException(
                            "Connection closed after " + received + " of " + contentLength + " bytes"
                    );
                }
                if (count == 0) continue;
                output.write(buffer, 0, count);
                received += count;
            }
            output.flush();
        }

        // getDirectStreamFileStatus waits on a completion latch in the UserService.
        // This avoids a 100ms polling delay after every individual file.
        JSONObject status = new JSONObject(userService.getDirectStreamFileStatus(fileId));
        String fileState = status.optString("state");
        if ("complete".equals(fileState)) {
            return new JSONObject().put("accepted", true)
                    .put("copiedBytes", expectedSize)
                    .put("relativePath", relativePath);
        }
        return new JSONObject().put("accepted", false)
                .put("error", status.optString("error", "Shizuku file writer failed"))
                .put("relativePath", relativePath);
    }

    private JSONObject health() throws Exception {
        boolean binder = Shizuku.pingBinder();
        boolean permission = binder && Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED;
        boolean serviceReady = userService != null;
        JSONObject result = new JSONObject();
        result.put("state", state);
        result.put("error", error);
        result.put("binder", binder);
        result.put("permission", permission);
        result.put("userService", serviceReady);
        result.put("bindingService", bindingService);
        result.put("serviceBound", serviceBound);
        result.put("permissionRequestInFlight", permissionRequestInFlight);
        result.put("uid", serviceReady ? userService.ping() : "");
        result.put("manufacturer", android.os.Build.MANUFACTURER);
        result.put("model", android.os.Build.MODEL);
        result.put("sdk", android.os.Build.VERSION.SDK_INT);
        result.put("ready", state.startsWith("ready_") && serviceReady);
        result.put("pubgTreePermission", hasPersistedPubgTreePermission());
        return result;
    }

    private void updateJob(String jobId, String jobState, boolean success, String message,
                           long copied, long total, int copiedFiles, int totalFiles, long started) {
        try {
            JSONObject status = new JSONObject();
            status.put("state", jobState);
            status.put("success", success);
            status.put("error", message);
            status.put("copiedBytes", copied);
            status.put("totalBytes", total);
            status.put("copiedFiles", copiedFiles);
            status.put("totalFiles", totalFiles);
            status.put("percent", total > 0 ? Math.min(100, (int) (copied * 100L / total))
                    : (success ? 100 : 0));
            long elapsed = Math.max(1L, System.currentTimeMillis() - started);
            status.put("bytesPerSecond", copied * 1000L / elapsed);
            jobs.put(jobId, status);
        } catch (Throwable ignored) {
        }
    }

    private void writeResponse(Socket client, int code, JSONObject body) throws Exception {
        if (client == null || client.isClosed()) return;
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        String reason = code == 200 ? "OK" : (code == 403 ? "Forbidden" : "Error");
        String header = "HTTP/1.1 " + code + " " + reason + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Content-Length: " + bytes.length + "\r\n"
                + "Connection: close\r\n\r\n";
        OutputStream output = client.getOutputStream();
        output.write(header.getBytes(StandardCharsets.ISO_8859_1));
        output.write(bytes);
        output.flush();
    }

    private void scheduleShutdownAfterResponse() {
        workers.execute(() -> {
            try {
                Thread.sleep(250L);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }

            try {
                if (userService != null || serviceBound || bindingService) {
                    Shizuku.unbindUserService(serviceArgs, serviceConnection, true);
                }
            } catch (Throwable failure) {
                android.util.Log.w("KafiaNetBridge", "Could not unbind Shizuku UserService during cleanup", failure);
            } finally {
                userService = null;
                serviceBound = false;
                bindingService = false;
                permissionRequestInFlight = false;
                permissionRequestStartedAt = 0L;
                main.removeCallbacks(permissionGrantMonitor);
            }

            try {
                if (serverSocket != null) serverSocket.close();
            } catch (Exception ignored) {
            }
            main.post(this::finish);
        });
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (shizukuListenersRegistered) {
            Shizuku.removeBinderReceivedListener(binderReceived);
            Shizuku.removeBinderDeadListener(binderDead);
            Shizuku.removeRequestPermissionResultListener(permissionResult);
        }
        main.removeCallbacks(permissionGrantMonitor);
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (Exception ignored) {
        }
        try {
            if (userService != null || serviceBound || bindingService) {
                Shizuku.unbindUserService(serviceArgs, serviceConnection, true);
            }
        } catch (Throwable ignored) {
        }
        userService = null;
        serviceBound = false;
        bindingService = false;
        permissionRequestInFlight = false;
        workers.shutdownNow();
    }
}
