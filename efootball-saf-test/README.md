# KafiaNet eFootball / Ziru_27 SAF test

This is an isolated test helper. It does not replace or modify the current CN PlayHub Shizuku helper.

## What it tests

- Selects the Ziru_27 eFootball storage through Android's Storage Access Framework (SAF).
- Requires selecting the **data** directory, not its child **files** directory.
- Performs a small temporary create/write/rename/delete probe and removes the probe file.
- Streams files from `D:\games\بيس (eFootball™)\2027\data` directly to the selected SAF tree.
- Replaces only same-name files by staging the incoming file, renaming the old file to a temporary backup, and committing the new file.
- Leaves unrelated destination files untouched.
- Does not move, rename, or delete source files.
- Does not scan destination file sizes or re-read destination files after copy. Source file length is read once to frame the HTTP request.

## Test steps

1. Install `KafiaNet-EFootball-SAF-Test.apk` on the phone. Its package ID is separate from the existing helper.
2. Open **KafiaNet eFootball SAF Test**.
3. Tap **اختيار مجلد data داخل مساحة eFootball**.
4. In the picker, open the eFootball storage provider from the left sidebar, navigate to `data`, select that folder, and grant access.
5. Wait until the app says the create/write/rename/delete probe succeeded.
6. Copy the session token shown on the app screen.
7. Connect the phone by USB with authorized ADB debugging, then run on PC:

```powershell
python .\test_copy_efootball_saf.py --token YOUR_SESSION_TOKEN
```

Optional arguments:

```powershell
python .\test_copy_efootball_saf.py --source "D:\games\بيس (eFootball™)\2027\data" --serial RF8M92Q8EEM --token YOUR_SESSION_TOKEN
```

The script automatically creates and later removes only its own ADB port-forward rule on TCP 18766. It refuses to take over a conflicting forward.

## Safety

This test writes into the selected eFootball `data` tree. Existing same-name files are replaced only after the new temporary file is fully received. Files and folders that do not match source names are not deleted. Stop the test if the app does not report the write probe as successful.
