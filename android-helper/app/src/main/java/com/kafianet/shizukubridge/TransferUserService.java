package com.kafianet.shizukubridge;

import android.os.ParcelFileDescriptor;
import android.os.Process;
import android.system.Os;
import android.system.OsConstants;
import android.system.ErrnoException;
import android.system.StructStat;

import androidx.annotation.Keep;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

@Keep
public final class TransferUserService extends ITransferUserService.Stub {
    private final Map<String, CopyJob> jobs = new ConcurrentHashMap<>();
    private final Map<String, StreamFileJob> streamFiles = new ConcurrentHashMap<>();
    private volatile String lastStartError = "";

    private static final class CopyJob {
        final AtomicBoolean cancelled = new AtomicBoolean(false);
        volatile String state = "queued";
        volatile String error = "";
        volatile long totalBytes = 0;
        volatile long copiedBytes = 0;
        volatile int totalFiles = 0;
        volatile int copiedFiles = 0;
        volatile int skippedFiles = 0;
        volatile long skippedBytes = 0;
        final Set<String> completedPaths = ConcurrentHashMap.newKeySet();
        volatile long startedAt = System.currentTimeMillis();

        JSONObject toJson() {
            JSONObject json = new JSONObject();
            try {
                json.put("state", state);
                json.put("success", "complete".equals(state));
                json.put("error", error);
                json.put("totalBytes", totalBytes);
                json.put("copiedBytes", copiedBytes);
                json.put("totalFiles", totalFiles);
                json.put("copiedFiles", copiedFiles);
                json.put("skippedFiles", skippedFiles);
                json.put("skippedBytes", skippedBytes);
                json.put("percent", totalBytes > 0
                        ? Math.min(100, (int) (copiedBytes * 100L / totalBytes))
                        : ("complete".equals(state) ? 100 : 0));
                long elapsed = Math.max(1L, System.currentTimeMillis() - startedAt);
                json.put("bytesPerSecond", copiedBytes * 1000L / elapsed);
            } catch (Exception ignored) {
            }
            return json;
        }
    }

    private static final class StreamFileJob {
        volatile String state = "queued";
        volatile String error = "";
        final CountDownLatch completed = new CountDownLatch(1);

        JSONObject toJson() {
            JSONObject json = new JSONObject();
            try {
                json.put("state", state);
                json.put("error", error);
            } catch (Exception ignored) {
            }
            return json;
        }
    }

    public TransferUserService() {
    }

    @Override
    public String inspectDestination(String destination) {
        JSONObject result = new JSONObject();
        try {
            File root = checkedDestination(destination);
            StructStat stat = Os.stat(root.getAbsolutePath());
            boolean directory = OsConstants.S_ISDIR(stat.st_mode);
            result.put("path", root.getAbsolutePath());
            result.put("exists", true);
            result.put("missing", false);
            result.put("isDirectory", directory);
            result.put("canWrite", directory && root.canWrite());
        } catch (ErrnoException error) {
            boolean missing = error.errno == OsConstants.ENOENT
                    || error.errno == OsConstants.ENOTDIR;
            try {
                result.put("exists", false);
                result.put("missing", missing);
                result.put("isDirectory", false);
                result.put("error", error.toString());
            } catch (Exception ignored) {
            }
        } catch (Throwable error) {
            try {
                result.put("exists", false);
                result.put("missing", false);
                result.put("isDirectory", false);
                result.put("error", error.toString());
            } catch (Exception ignored) {
            }
        }
        return result.toString();
    }

    @Override
    public String ping() {
        return "uid=" + Process.myUid() + ";pid=" + Process.myPid();
    }

    private File checkedDestination(String path) throws Exception {
        if (path == null || path.trim().isEmpty()) {
            throw new IllegalArgumentException("Destination path is empty");
        }
        File requested = new File(path);
        if (!requested.isAbsolute()) {
            throw new SecurityException("Destination path must be absolute");
        }
        // Do not restrict the user-selected destination to Android/data or
        // compare it with a package-specific hard-coded game path.
        return requested.getCanonicalFile();
    }

    @Override
    public String getCopyStatus(String jobId) {
        CopyJob job = jobs.get(jobId);
        if (job == null) return "{\"state\":\"missing\",\"success\":false}";
        return job.toJson().toString();
    }

    @Override
    public void cancelCopy(String jobId) {
        CopyJob job = jobs.get(jobId);
        if (job != null) {
            job.cancelled.set(true);
            if ("running".equals(job.state)) job.state = "cancelled";
        }
    }

    private File checkedRelativeDestination(String destination, String relativePath)
            throws Exception {
        File root = checkedDestination(destination);
        String[] segments = validatedRelativeSegments(relativePath);
        File current = root;
        for (int i = 0; i < segments.length - 1; i++) {
            File next = new File(current, segments[i]);
            if (!next.exists()) {
                try {
                    Os.mkdir(next.getAbsolutePath(), 0775);
                } catch (ErrnoException error) {
                    if (error.errno != OsConstants.EEXIST) throw error;
                }
            }
            if (!next.isDirectory()) {
                throw new java.io.IOException("Destination parent is not a directory: " + next);
            }
            current = next;
        }
        return new File(current, segments[segments.length - 1]).getAbsoluteFile();
    }

    private String[] validatedRelativeSegments(String relativePath) {
        String relative = relativePath == null ? "" : relativePath.replace('\\', '/');
        if (relative.isEmpty() || relative.startsWith("/") || relative.contains(":")
                || relative.indexOf('\0') >= 0) {
            throw new SecurityException("Invalid relative destination path");
        }
        String[] segments = relative.split("/", -1);
        for (String segment : segments) {
            if (segment.isEmpty() || ".".equals(segment) || "..".equals(segment)) {
                throw new SecurityException("Unsafe relative destination path");
            }
        }
        return segments;
    }

    private File resolveRelativeDestination(File root, String relativePath) {
        String[] segments = validatedRelativeSegments(relativePath);
        File current = root;
        for (String segment : segments) current = new File(current, segment);
        return current.getAbsoluteFile();
    }

    private static boolean isExplicitPermissionFailure(Throwable error) {
        Throwable current = error;
        while (current != null) {
            if (current instanceof ErrnoException) {
                int errno = ((ErrnoException) current).errno;
                if (errno == OsConstants.EACCES || errno == OsConstants.EPERM) return true;
            }
            String message = String.valueOf(current.getMessage()).toLowerCase(java.util.Locale.ROOT);
            if (message.contains("permission denied") || message.contains("operation not permitted")
                    || message.contains("eacces") || message.contains("errno 13")
                    || message.contains("eperm") || message.contains("errno 1")) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }

    private static String describeWriteFailure(Throwable error) {
        String detail = String.valueOf(error);
        if (isExplicitPermissionFailure(error)) {
            return "KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED: " + detail;
        }
        return detail;
    }

    @Override
    public String checkDirectStreamFiles(String jobId, String destination, String filesJson) {
        JSONObject result = new JSONObject();
        JSONArray skipped = new JSONArray();
        long skippedBytes = 0L;
        int skippedCount = 0;
        try {
            CopyJob job = jobs.get(jobId);
            if (job == null || !"running".equals(job.state) || job.cancelled.get()) {
                throw new IllegalStateException("Direct stream job is not running");
            }
            File syncRoot = checkedDestination(destination);
            JSONArray files = new JSONArray(filesJson == null ? "[]" : filesJson);
            for (int index = 0; index < files.length(); index++) {
                JSONObject item = files.getJSONObject(index);
                String relativePath = item.getString("path");
                long expectedSize = item.getLong("size");
                long modifiedTimeMs = item.getLong("modifiedMs");
                if (expectedSize < 0 || modifiedTimeMs < 0) {
                    throw new IllegalArgumentException("Invalid source file metadata");
                }

                File target = resolveRelativeDestination(syncRoot, relativePath);
                boolean matches = false;
                try {
                    StructStat stat = Os.lstat(target.getAbsolutePath());
                    matches = OsConstants.S_ISREG(stat.st_mode)
                            && stat.st_size == expectedSize
                            && stat.st_mtime == (modifiedTimeMs / 1000L);
                } catch (ErrnoException error) {
                    if (error.errno != OsConstants.ENOENT
                            && error.errno != OsConstants.ENOTDIR
                            && error.errno != OsConstants.EACCES
                            && error.errno != OsConstants.EPERM) {
                        throw error;
                    }
                    // If metadata cannot be inspected, do not skip the file.
                    // Let the actual writer report a precise EACCES/EPERM if it
                    // also cannot create or replace the destination file.
                }

                if (matches) {
                    synchronized (job) {
                        if (job.completedPaths.add(relativePath)) {
                            job.copiedFiles++;
                            job.copiedBytes += expectedSize;
                            job.skippedFiles++;
                            job.skippedBytes += expectedSize;
                            skippedCount++;
                            skippedBytes += expectedSize;
                            skipped.put(relativePath);
                        }
                    }
                }
            }
            result.put("ok", true);
            result.put("skipped", skipped);
            result.put("skippedFiles", skippedCount);
            result.put("skippedBytes", skippedBytes);
        } catch (Throwable error) {
            result = new JSONObject();
            try {
                result.put("ok", false);
                result.put("error", error.toString());
            } catch (Exception ignored) {
            }
            android.util.Log.e("KafiaNetBridge", "Direct-stream sync check failed", error);
        }
        return result.toString();
    }

    @Override
    public String getLastStartError() {
        return lastStartError;
    }

    @Override
    public boolean beginDirectStreamJob(
            String jobId, String destination, long totalBytes, int totalFiles) {
        try {
            if (jobId == null || jobId.isEmpty() || totalBytes < 0 || totalFiles < 0) {
                return false;
            }
            CopyJob existing = jobs.get(jobId);
            if (existing != null && "running".equals(existing.state)) return false;

            // Register the job without probing or pre-validating the selected
            // destination. Actual filesystem errors are reported by the writer.
            checkedDestination(destination);

            CopyJob job = new CopyJob();
            job.totalBytes = totalBytes;
            job.totalFiles = totalFiles;
            job.state = "running";
            jobs.put(jobId, job);
            return true;
        } catch (Throwable error) {
            android.util.Log.e("KafiaNetBridge", "Could not begin direct stream job", error);
            return false;
        }
    }

    @Override
    public boolean startDirectStreamFile(
            String jobId,
            String fileId,
            String destination,
            String relativePath,
            long expectedSize,
            long modifiedTime,
            ParcelFileDescriptor source) {
        lastStartError = "";
        ParcelFileDescriptor reader = null;
        try {
            if (source == null || jobId == null || fileId == null
                    || fileId.isEmpty() || expectedSize < 0) {
                return false;
            }
            CopyJob job = jobs.get(jobId);
            if (job == null || !"running".equals(job.state) || job.cancelled.get()) {
                return false;
            }
            if (streamFiles.containsKey(fileId)) return false;

            File target = checkedRelativeDestination(destination, relativePath);
            File parent = target.getParentFile();
            if (parent == null || !parent.isDirectory()) {
                lastStartError = "KAFIANET_SHIZUKU_FILE_START_FAILED: destination parent is missing";
                return false;
            }
            synchronized (job) {
                if (job.completedPaths.contains(relativePath)) {
                    lastStartError = "KAFIANET_SHIZUKU_FILE_ALREADY_COMPLETE: " + relativePath;
                    return false;
                }
            }

            reader = ParcelFileDescriptor.dup(source.getFileDescriptor());
            StreamFileJob stream = new StreamFileJob();
            stream.state = "running";
            streamFiles.put(fileId, stream);
            final ParcelFileDescriptor inputDescriptor = reader;
            reader = null;

            Thread worker = new Thread(() -> {
                File temporary = null;
                try (ParcelFileDescriptor.AutoCloseInputStream input =
                             new ParcelFileDescriptor.AutoCloseInputStream(inputDescriptor)) {
                    if (job.cancelled.get()) throw new InterruptedException("cancelled");

                    String safeId = fileId.replaceAll("[^A-Za-z0-9_-]", "");
                    temporary = new File(
                            parent,
                            "." + target.getName() + ".kafianet-" + safeId + ".part"
                    );
                    if (temporary.exists() && !temporary.delete()) {
                        throw new java.io.IOException("Cannot clear previous temporary file");
                    }

                    byte[] buffer = new byte[4 * 1024 * 1024];
                    long written = 0;
                    try (FileOutputStream output = new FileOutputStream(temporary, false)) {
                        int count;
                        while ((count = input.read(buffer)) >= 0) {
                            if (job.cancelled.get()) {
                                throw new InterruptedException("cancelled");
                            }
                            if (count == 0) continue;
                            if (written + count > expectedSize) {
                                throw new java.io.IOException("Stream exceeded expected file size");
                            }
                            output.write(buffer, 0, count);
                            written += count;
                            synchronized (job) {
                                job.copiedBytes = Math.min(
                                        job.totalBytes,
                                        job.copiedBytes + count
                                );
                            }
                        }
                        output.flush();
                    }

                    // Count bytes while receiving; never re-read the destination file for size verification.
                    if (written != expectedSize) {
                        throw new java.io.IOException(
                                "Size verification failed: expected " + expectedSize
                                        + ", received " + written
                        );
                    }
                    if (job.cancelled.get()) {
                        throw new InterruptedException("cancelled");
                    }

                    // Write beside the final file, then atomically replace it.
                    // This avoids opening an existing read-only Custom.ini or PAK in-place.
                    Os.rename(
                            temporary.getAbsolutePath(),
                            target.getAbsolutePath()
                    );
                    temporary = null;
                    if (modifiedTime > 0) target.setLastModified(modifiedTime);

                    synchronized (job) {
                        if (!job.completedPaths.add(relativePath)) {
                            throw new IllegalStateException("Direct-stream path was already counted: " + relativePath);
                        }
                        job.copiedFiles++;
                    }
                    stream.state = "complete";
                } catch (Throwable error) {
                    stream.error = describeWriteFailure(error);
                    stream.state = job.cancelled.get() ? "cancelled" : "failed";
                    job.error = stream.error;
                    job.state = stream.state;
                    android.util.Log.e(
                            "KafiaNetBridge",
                            "Direct stream file failed: " + relativePath,
                            error
                    );
                } finally {
                    if (temporary != null && temporary.exists() && !temporary.delete()) {
                        android.util.Log.w(
                                "KafiaNetBridge",
                                "Could not remove partial direct-stream file: " + temporary
                        );
                    }
                    stream.completed.countDown();
                }
            }, "KafiaNet-Direct-Stream-" + fileId);
            worker.setDaemon(true);
            worker.start();
            return true;
        } catch (Throwable error) {
            lastStartError = describeWriteFailure(error);
            android.util.Log.e("KafiaNetBridge", "Could not start direct stream file", error);
            if (reader != null) {
                try { reader.close(); } catch (Exception ignored) { }
            }
            return false;
        } finally {
            if (source != null) {
                try { source.close(); } catch (Exception ignored) { }
            }
        }
    }

    @Override
    public String getDirectStreamFileStatus(String fileId) {
        StreamFileJob stream = streamFiles.get(fileId);
        if (stream == null) return "{\"state\":\"missing\",\"error\":\"stream not found\"}";
        try {
            // Block until the writer commits this file; avoid polling delays for thousands of small files.
            if (!stream.completed.await(6L, TimeUnit.HOURS)) {
                return "{\"state\":\"failed\",\"error\":\"Timed out waiting for the destination writer\"}";
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            return "{\"state\":\"failed\",\"error\":\"Interrupted while waiting for destination writer\"}";
        }
        String result = stream.toJson().toString();
        if ("complete".equals(stream.state) || "failed".equals(stream.state)
                || "cancelled".equals(stream.state)) {
            streamFiles.remove(fileId, stream);
        }
        return result;
    }

    @Override
    public void finishDirectStreamJob(String jobId) {
        CopyJob job = jobs.get(jobId);
        if (job == null) throw new IllegalStateException("Direct stream job not found");
        synchronized (job) {
            if (job.cancelled.get()) {
                job.state = "cancelled";
                job.error = "cancelled";
            } else if (job.state.equals("failed")) {
                // Preserve the first concrete file error.
            } else if (job.copiedFiles != job.totalFiles || job.copiedBytes != job.totalBytes) {
                job.state = "failed";
                job.error = "Direct stream incomplete: "
                        + job.copiedFiles + "/" + job.totalFiles + " files, "
                        + job.copiedBytes + "/" + job.totalBytes + " bytes";
            } else {
                job.state = "complete";
                job.error = "";
            }
        }
    }

    @Override
    public void destroy() {
        for (CopyJob job : jobs.values()) job.cancelled.set(true);
        System.exit(0);
    }
}
