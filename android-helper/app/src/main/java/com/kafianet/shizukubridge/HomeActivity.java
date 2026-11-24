package com.kafianet.shizukubridge;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

public final class HomeActivity extends Activity {
    private TextView status;
    private TextView progressLabel;
    private ProgressBar progress;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(buildUi());
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshLocalCopyState();
    }

    private ScrollView buildUi() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(28, 34, 28, 34);
        root.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView title = new TextView(this);
        title.setText("KafiaNet Assistant");
        title.setTextSize(25);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        title.setGravity(Gravity.CENTER);
        root.addView(title, matchWrap());

        TextView subtitle = new TextView(this);
        subtitle.setText("مساعد واحد لمحرك النقل وShizuku وPUBG وeFootball");
        subtitle.setTextSize(14);
        subtitle.setGravity(Gravity.CENTER);
        subtitle.setPadding(0, 8, 0, 24);
        root.addView(subtitle, matchWrap());

        addAction(root, "فتح محرك النقل العادي / Shizuku", () -> {
            startActivity(new Intent(this, MainActivity.class));
        });

        addAction(root, "السماح بمجلد PUBG / استكمال النقل من الجذر", () -> {
            Intent intent = new Intent(this, MainActivity.class);
            intent.putExtra("kafianet_action", "request_pubg_tree");
            intent.putExtra("package", "com.tencent.ig");
            startActivity(intent);
        });

        addAction(root, "مساحة eFootball / Ziru_27", () -> {
            startActivity(new Intent(this, EFootballActivity.class));
        });

        TextView localTitle = new TextView(this);
        localTitle.setText("النقل المحلي على الهاتف");
        localTitle.setTextSize(17);
        localTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        localTitle.setPadding(0, 28, 0, 8);
        root.addView(localTitle, matchWrap());

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        root.addView(progress, matchWrap());

        progressLabel = new TextView(this);
        progressLabel.setTextSize(14);
        progressLabel.setPadding(0, 6, 0, 4);
        root.addView(progressLabel, matchWrap());

        status = new TextView(this);
        status.setTextSize(13);
        status.setPadding(0, 4, 0, 12);
        root.addView(status, matchWrap());

        TextView note = new TextView(this);
        note.setText(
                "بعد ظهور بدء النقل المحلي ونسبة التقدم، تتم القراءة والكتابة داخل الهاتف نفسه. "
                + "لا يحتاج هذا الجزء إلى بقاء كابل USB متصلاً. اترك المساعد يعمل ولا تستخدم "
                + "إيقاف إجباري للتطبيق أثناء النقل."
        );
        note.setTextSize(12);
        note.setPadding(0, 20, 0, 0);
        root.addView(note, matchWrap());

        scroll.addView(root);
        return scroll;
    }

    private void addAction(LinearLayout root, String label, Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setOnClickListener(view -> action.run());
        LinearLayout.LayoutParams params = matchWrap();
        params.topMargin = 8;
        root.addView(button, params);
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
    }

    private void refreshLocalCopyState() {
        SharedPreferences prefs = getSharedPreferences(
                PubgOfflineFinalizeService.PREFS, MODE_PRIVATE
        );
        String state = prefs.getString(PubgOfflineFinalizeService.KEY_STATE, "idle");
        int percent = prefs.getInt(PubgOfflineFinalizeService.KEY_PERCENT, 0);
        int copied = prefs.getInt(PubgOfflineFinalizeService.KEY_COPIED_FILES, 0);
        int total = prefs.getInt(PubgOfflineFinalizeService.KEY_TOTAL_FILES, 0);
        long bytes = prefs.getLong(PubgOfflineFinalizeService.KEY_COPIED_BYTES, 0L);
        long totalBytes = prefs.getLong(PubgOfflineFinalizeService.KEY_TOTAL_BYTES, 0L);
        String current = prefs.getString(PubgOfflineFinalizeService.KEY_CURRENT_FILE, "");
        String error = prefs.getString(PubgOfflineFinalizeService.KEY_ERROR, "");
        if (progress != null) progress.setProgress(percent);
        if (progressLabel != null) {
            progressLabel.setText(percent + "% — " + copied + "/" + total
                    + " ملف — " + formatBytes(bytes) + " / " + formatBytes(totalBytes));
        }
        if (status == null) return;
        if ("running".equals(state)) {
            status.setText("المساعد ينقل البيانات محلياً. يمكنك فصل USB بأمان.\n" + current);
        } else if ("completed".equals(state)) {
            status.setText("اكتمل النقل إلى مجلد اللعبة. نسخة الجذر الأصلية بقيت كما هي.");
        } else if ("failed".equals(state) || "interrupted".equals(state)) {
            status.setText("لم يكتمل النقل المحلي: " + error
                    + "\nنسخة الجذر لم تُحذف؛ أصلح السبب ثم أعد المحاولة.");
        } else if ("cancelled".equals(state)) {
            status.setText("أُلغي النقل. ملفات الجذر الأصلية لم تُحذف.");
        } else {
            status.setText("لا توجد عملية نقل محلية نشطة.");
        }
    }

    private String formatBytes(long value) {
        double amount = Math.max(0L, value);
        String[] units = {"B", "KB", "MB", "GB", "TB"};
        int index = 0;
        while (amount >= 1024.0 && index < units.length - 1) {
            amount /= 1024.0;
            index++;
        }
        return String.format(java.util.Locale.ROOT, "%.1f %s", amount, units[index]);
    }
}
