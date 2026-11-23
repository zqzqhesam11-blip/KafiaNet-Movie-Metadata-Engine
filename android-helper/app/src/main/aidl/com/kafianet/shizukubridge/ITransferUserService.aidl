package com.kafianet.shizukubridge;

import android.os.ParcelFileDescriptor;

interface ITransferUserService {
    void destroy() = 16777114;
    String ping() = 1;
    String inspectDestination(String destination) = 16;
    String getCopyStatus(String jobId) = 4;
    void cancelCopy(String jobId) = 5;
    boolean beginDirectStreamJob(String jobId, String destination, long totalBytes, int totalFiles) = 10;
    boolean startDirectStreamFile(String jobId, String fileId, String destination, String relativePath, long expectedSize, long modifiedTime, in ParcelFileDescriptor source) = 13;
    String getDirectStreamFileStatus(String fileId) = 14;
    void finishDirectStreamJob(String jobId) = 15;
}
