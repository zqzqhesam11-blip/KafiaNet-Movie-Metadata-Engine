"""Regression checks for the Windows password-protected fullscreen guard."""
from pathlib import Path
import sys

# Running a script by path sets sys.path[0] to validation/, not the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.windows_fullscreen import WindowsFullscreenProtection


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("PASS:", label)


def guard_with(modifiers=(), foreground=True):
    # Bypass __init__: tests must not read or mutate the real Windows registry.
    guard = object.__new__(WindowsFullscreenProtection)
    guard._pressed_modifiers = set(modifiers)
    guard._application_is_foreground = lambda: foreground
    return guard


guard_type = WindowsFullscreenProtection

check(
    "Alt+F4 is blocked while the protected app is foreground",
    guard_with((0xA4,))._should_block_keyboard_event(guard_type.VK_F4, True),
)
check(
    "Alt+F4 is not blocked when Alt is not pressed",
    not guard_with(())._should_block_keyboard_event(guard_type.VK_F4, True),
)
check(
    "Alt+F4 stays blocked if another app briefly receives focus",
    guard_with((0xA4,), foreground=False)._should_block_keyboard_event(
        guard_type.VK_F4, True
    ),
)
check(
    "Alt+Tab stays blocked after focus leaves the protected app",
    guard_with((0xA4,), foreground=False)._should_block_keyboard_event(
        guard_type.VK_TAB, True
    ),
)
check(
    "Windows key stays blocked after focus leaves the protected app",
    guard_with((), foreground=False)._should_block_keyboard_event(
        guard_type.VK_LWIN, True
    ),
)
check(
    "Ctrl+Shift+Esc is blocked",
    guard_with((0xA2, 0xA1))._should_block_keyboard_event(
        guard_type.VK_ESCAPE, True
    ),
)
check(
    "Ctrl+Esc is blocked",
    guard_with((0xA2,))._should_block_keyboard_event(
        guard_type.VK_ESCAPE, True
    ),
)
check(
    "Alt+Tab is blocked",
    guard_with((0xA4,))._should_block_keyboard_event(guard_type.VK_TAB, True),
)
check(
    "Alt+Space is blocked",
    guard_with((0xA4,))._should_block_keyboard_event(
        guard_type.VK_SPACE, True
    ),
)
check(
    "Windows key is blocked while the protected app is foreground",
    guard_with(())._should_block_keyboard_event(guard_type.VK_LWIN, True),
)
check(
    "ordinary Tab remains available",
    not guard_with(())._should_block_keyboard_event(guard_type.VK_TAB, True),
)

repo_root = Path(__file__).resolve().parents[1]
qml = (repo_root / "qml" / "main.qml").read_text(encoding="utf-8")
lock_body = qml.split("function enterFullscreenLock()", 1)[1].split(
    "function openFullscreenPasswordDialog", 1
)[0]
guard_setup = lock_body.find("var guardReady")
guard_state = lock_body.find("fullscreenGuardEnabled = guardReady")
guard_failure = lock_body.find("if (!guardReady)")
failure_end = lock_body.find("wasMaximizedBeforeLock", guard_failure)
failure_handling = (
    lock_body[guard_failure:failure_end]
    if guard_failure >= 0 and failure_end > guard_failure
    else ""
)
lock_transition = lock_body.find("fullscreenLocked = true")
check(
    "fullscreen refuses a protected lock when native Windows protection is unavailable",
    guard_setup >= 0
    and guard_failure > guard_setup
    and "return" in failure_handling
    and lock_transition > failure_end,
)
protection_source = (repo_root / "engine" / "windows_fullscreen.py").read_text(
    encoding="utf-8"
)
lock_filter_start = protection_source.index("def _should_block_keyboard_event")
lock_filter_end = protection_source.index("def _keyboard_hook_proc", lock_filter_start)
lock_filter = protection_source[lock_filter_start:lock_filter_end]
check(
    "global escape-key filter does not depend on app foreground",
    "_application_is_foreground" not in lock_filter,
)
check(
    "fullscreen guard pins the native window above desktop windows",
    "def enforce_locked_desktop(self)" in protection_source
    and "HWND_TOPMOST" in protection_source
    and "SetWindowPos" in protection_source
    and "actual_style & self.WS_EX_TOPMOST" in protection_source,
)
check(
    "fullscreen guard confirms taskbars are hidden after each reapply",
    "self._user32.ShowWindow(hwnd, self.SW_HIDE)" in protection_source
    and "self._user32.IsWindowVisible(hwnd)" in protection_source
    and "return bool(handles) and all_hidden" in protection_source,
)
check(
    "Start, context-menu, Ctrl+Esc and Alt+Tab escape keys are blocked",
    "BLOCKED_KEYS = {VK_LWIN, VK_RWIN, VK_APPS}" in protection_source
    and "if key in self.BLOCKED_KEYS:" in protection_source
    and "key == self.VK_ESCAPE and (control_down or alt_down)" in protection_source
    and "key == self.VK_TAB and alt_down" in protection_source,
)
app_source = (repo_root / "app_qml.py").read_text(encoding="utf-8")
check(
    "native-protection failure clearly warns that desktop and taskbar remain accessible",
    "يمكن الخروج إلى سطح المكتب أو إظهار شريط المهام." in app_source,
)
check(
    "fullscreen enforcement timer runs every 100 ms",
    "self._fullscreen_taskbar_timer.setInterval(100)" in app_source
    and "self._windows_fullscreen_protection.enforce_locked_desktop"
        in app_source,
)
check(
    "main QML window is discoverable for native topmost enforcement",
    'objectName: "mainWindow"' in qml
    and "QGuiApplication.topLevelWindows()" in app_source,
)

print("WINDOWS FULLSCREEN PROTECTION SIMULATION: PASS")
