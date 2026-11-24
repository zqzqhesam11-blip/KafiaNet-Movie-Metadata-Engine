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
    "Alt+F4 is not intercepted while another app is foreground",
    not guard_with((0xA4,), foreground=False)._should_block_keyboard_event(
        guard_type.VK_F4, True
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
guard_check = lock_body.find("if (!guardReady)")
lock_transition = lock_body.find("fullscreenLocked = true")
check(
    "fullscreen lock does not activate when native Windows protection fails",
    guard_check >= 0 and lock_transition > guard_check,
)

print("WINDOWS FULLSCREEN PROTECTION SIMULATION: PASS")
