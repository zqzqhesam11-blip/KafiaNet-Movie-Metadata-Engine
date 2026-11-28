# CN PlayHub — self-contained Windows build

The released installer is built from the current branch using PyInstaller. Python, PySide6, Qt DLLs, Qt Quick/QML runtime modules, ADB, application logic, QML screens, game images and game metadata are bundled or embedded; the installed application does not need an external Python runtime under D:\games\py.

The Android fallback is bundled into the Windows build. Direct ADB is tried first for every phone; only a phone that rejects writes to Android/data receives the Shizuku APK and CN PlayHub companion when they are missing. Android may require approval for Shizuku and a user-selected folder grant. Shizuku without Root runs as ADB Shell and cannot override every Android/OEM storage restriction.

## UI and build verification

- The build retains all PySide6 QML modules instead of pruning them to save package size.
- The frozen-app smoke test checks the sidebar, games dashboard, and transfer panel in the embedded QML.
- The smoke test emits a test transfer-list signal and verifies that the transfer panel actually becomes visible.
- The installer is not published unless the frozen UI and transfer-panel smoke tests pass.

The installer is self-contained, but no locally distributed executable can be guaranteed impossible to reverse-engineer. The repository is public, so source files committed here remain readable on GitHub; compiled packaging protects the shipped artifact from simple source browsing, not from a determined analyst.


## OEM compatibility diagnostics

The Settings page provides an optional, persisted, read-only ADB compatibility check. It reads the connected phone's manufacturer, model, Android version, ADB shell identity, Shizuku package presence, server process visibility, and CN PlayHub helper presence. It does not write to the phone, change developer settings, inject permission denials, or emulate MIUI/ColorOS/OxygenOS. Manufacturer-specific security toggles cannot be verified reliably from a different OEM's Android build; the page identifies the documented setting where applicable and asks the user to verify its actual state on that phone.

## Official Android Platform-Tools

The self-contained Windows build workflow replaces the checked-out `platform-tools` directory with Google's official Windows Platform-Tools archive from `dl.google.com` before validation and packaging. CI verifies the ADB version and required Windows files, then prints SHA-256 hashes for the archive and core binaries. Official upstream provenance reduces uncertainty about bundled binaries but cannot guarantee that every antivirus product will suppress a warning.


## Versioned game libraries

The eFootball and Grand Theft Auto cards discover version folders directly under each game's folder. Choosing a version is saved in the local settings and is reused by install, copy, launch, close and uninstall actions. Android copy treats `data` and `obb` as separate sources and routes them to `Android/data/<package>` and `Android/obb/<package>`. GTA package IDs are extracted from the selected APK manifest; the app will not guess a package path when it cannot verify one.


## Wireless Android pairing

The device picker includes **Add Android phone via QR**. CN PlayHub generates the official Android Studio/AOSP `WIFI:T:ADB` pairing QR, waits for the matching temporary `_adb-tls-pairing._tcp` mDNS service, runs the pairing command without asking the user to type the one-time secret, and then discovers/connects the phone's `_adb-tls-connect._tcp` endpoint. The QR secret is generated for one pairing attempt and is not written to settings or logs. Paired phones remain trusted by Android until the workstation is forgotten or debugging authorizations are revoked.

When Android reports the same physical phone through multiple ADB transports, CN PlayHub uses the phone's hardware serial (`ro.serialno` / `ro.boot.serialno`) to show one device row. It preserves a transport already selected by the user or used by an active operation.
