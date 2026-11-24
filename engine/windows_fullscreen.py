"""Windows-only keyboard, Task Manager and taskbar guard for the fullscreen lock.

This is an application-level convenience barrier, not a replacement for
Windows Assigned Access, Shell Launcher, or Keyboard Filter policies.
"""
import ctypes
import os
from ctypes import wintypes


class WindowsFullscreenProtection:
    """Temporarily hide the Windows taskbar and block common escape keys."""

    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105

    VK_TAB = 0x09
    VK_RETURN = 0x0D
    VK_ESCAPE = 0x1B
    VK_SPACE = 0x20
    VK_F4 = 0x73
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    VK_APPS = 0x5D

    CONTROL_KEYS = {0x11, 0xA2, 0xA3}
    ALT_KEYS = {0x12, 0xA4, 0xA5}
    SHIFT_KEYS = {0x10, 0xA0, 0xA1}
    BLOCKED_KEYS = {VK_LWIN, VK_RWIN, VK_APPS}

    SW_HIDE = 0
    SW_SHOW = 5

    TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

    # Per-user policy: only the signed-in Windows account is affected.
    # A recovery marker preserves the exact previous registry value so a
    # forced process exit can be repaired at the next application launch.
    TASK_MANAGER_POLICY_KEY = (
        r"Software\Microsoft\Windows\CurrentVersion\Policies\System"
    )
    TASK_MANAGER_RECOVERY_KEY = (
        r"Software\KafiaNetControlPro\FullscreenProtection"
    )
    TASK_MANAGER_VALUE = "DisableTaskMgr"
    RECOVERY_ACTIVE_VALUE = "ProtectionActive"
    RECOVERY_PREVIOUS_EXISTS_VALUE = "PreviousValueExists"
    RECOVERY_PREVIOUS_VALUE = "PreviousValue"

    class _KeyboardEvent(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    def __init__(self):
        self._enabled = False
        self.last_error = ""
        self._user32 = None
        self._kernel32 = None
        self._hook = None
        self._hook_callback = None
        self._enum_callback = None
        self._hook_callback_type = None
        self._enum_callback_type = None
        self._taskbar_visibility = {}
        self._pressed_modifiers = set()
        # Restore an interrupted lock from a previous process before allowing
        # the normal desktop session to continue.
        self._recover_stale_task_manager_policy()

    @property
    def enabled(self):
        return self._enabled

    def _initialize_apis(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._hook_callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p
        )
        self._enum_callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        self._user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            self._hook_callback_type,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p
        ]
        self._user32.CallNextHookEx.restype = ctypes.c_ssize_t

        self._user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self._user32.FindWindowW.restype = wintypes.HWND
        self._user32.EnumWindows.argtypes = [
            self._enum_callback_type, wintypes.LPARAM
        ]
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.GetClassNameW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int
        ]
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    @staticmethod
    def _registry_api(winreg_module=None):
        if winreg_module is not None:
            return winreg_module
        if os.name != "nt":
            return None
        import winreg
        return winreg

    @staticmethod
    def _close_registry_key(winreg, key):
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass

    def _notify_policy_change(self):
        """Ask the current Windows session to refresh per-user policy state."""
        if os.name != "nt":
            return
        try:
            user32 = self._user32 or ctypes.WinDLL("user32", use_last_error=True)
            send_message = user32.SendMessageTimeoutW
            send_message.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPCWSTR,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_size_t),
            ]
            send_message.restype = wintypes.LPARAM
            result = ctypes.c_size_t()
            send_message(
                wintypes.HWND(0xFFFF),  # HWND_BROADCAST
                0x001A,                 # WM_SETTINGCHANGE
                0,
                "Policy",
                0x0002,                 # SMTO_ABORTIFHUNG
                2000,
                ctypes.byref(result),
            )
        except Exception as exc:
            print("WINDOWS POLICY NOTIFICATION WARNING:", exc)

    def _write_task_manager_disabled_policy(self, winreg, policy_key):
        """Write and read back the per-user Task Manager restriction."""
        winreg.SetValueEx(
            policy_key, self.TASK_MANAGER_VALUE, 0, winreg.REG_DWORD, 1
        )
        actual_value, actual_type = winreg.QueryValueEx(
            policy_key, self.TASK_MANAGER_VALUE
        )
        if actual_type != winreg.REG_DWORD or int(actual_value or 0) != 1:
            raise RuntimeError(
                "Windows did not retain the per-user DisableTaskMgr policy."
            )

    def _set_task_manager_disabled(self, winreg_module=None):
        """Temporarily disable Task Manager for this Windows user only."""
        winreg = self._registry_api(winreg_module)
        if winreg is None:
            return True

        state_key = None
        policy_key = None
        try:
            # Idempotency matters when the fullscreen protection is reapplied:
            # never replace the original snapshot with our already-set value.
            try:
                state_key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    self.TASK_MANAGER_RECOVERY_KEY,
                    0,
                    winreg.KEY_READ,
                )
            except FileNotFoundError:
                state_key = None

            if state_key is not None:
                try:
                    active, _ = winreg.QueryValueEx(
                        state_key, self.RECOVERY_ACTIVE_VALUE
                    )
                except FileNotFoundError:
                    active = 0
                if int(active or 0) == 1:
                    policy_key = winreg.CreateKeyEx(
                        winreg.HKEY_CURRENT_USER,
                        self.TASK_MANAGER_POLICY_KEY,
                        0,
                        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
                    )
                    self._write_task_manager_disabled_policy(winreg, policy_key)
                    self._notify_policy_change()
                    return True
            self._close_registry_key(winreg, state_key)
            state_key = None

            # Read and snapshot the exact previous value (including registry
            # type) before changing it. If absent, restore by deleting ours.
            previous_exists = False
            previous_data = None
            previous_type = None
            try:
                policy_key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    self.TASK_MANAGER_POLICY_KEY,
                    0,
                    winreg.KEY_READ,
                )
                try:
                    previous_data, previous_type = winreg.QueryValueEx(
                        policy_key, self.TASK_MANAGER_VALUE
                    )
                    previous_exists = True
                except FileNotFoundError:
                    pass
            except FileNotFoundError:
                policy_key = None
            finally:
                self._close_registry_key(winreg, policy_key)
                policy_key = None

            state_key = winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                self.TASK_MANAGER_RECOVERY_KEY,
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
            )
            winreg.SetValueEx(
                state_key, self.RECOVERY_PREVIOUS_EXISTS_VALUE, 0,
                winreg.REG_DWORD, 1 if previous_exists else 0
            )
            if previous_exists:
                winreg.SetValueEx(
                    state_key, self.RECOVERY_PREVIOUS_VALUE, 0,
                    previous_type, previous_data
                )
            winreg.SetValueEx(
                state_key, self.RECOVERY_ACTIVE_VALUE, 0,
                winreg.REG_DWORD, 1
            )
            self._close_registry_key(winreg, state_key)
            state_key = None

            policy_key = winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                self.TASK_MANAGER_POLICY_KEY,
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
            )
            self._write_task_manager_disabled_policy(winreg, policy_key)
            self._notify_policy_change()
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            print("WINDOWS TASK MANAGER DISABLE FAILED:", exc)
            self._restore_task_manager_policy(winreg)
            return False
        finally:
            self._close_registry_key(winreg, policy_key)
            self._close_registry_key(winreg, state_key)

    def _restore_task_manager_policy(self, winreg_module=None):
        """Restore the user's original Task Manager policy without touching other settings."""
        winreg = self._registry_api(winreg_module)
        if winreg is None:
            return True

        state_key = None
        policy_key = None
        try:
            try:
                state_key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    self.TASK_MANAGER_RECOVERY_KEY,
                    0,
                    winreg.KEY_READ,
                )
            except FileNotFoundError:
                return True

            try:
                active, _ = winreg.QueryValueEx(
                    state_key, self.RECOVERY_ACTIVE_VALUE
                )
            except FileNotFoundError:
                active = 0
            if int(active or 0) != 1:
                return True

            try:
                previous_exists, _ = winreg.QueryValueEx(
                    state_key, self.RECOVERY_PREVIOUS_EXISTS_VALUE
                )
            except FileNotFoundError:
                previous_exists = 0

            previous_value = None
            if int(previous_exists or 0) == 1:
                previous_value = winreg.QueryValueEx(
                    state_key, self.RECOVERY_PREVIOUS_VALUE
                )

            self._close_registry_key(winreg, state_key)
            state_key = None

            if int(previous_exists or 0) == 1:
                policy_key = winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER,
                    self.TASK_MANAGER_POLICY_KEY,
                    0,
                    winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
                )
                winreg.SetValueEx(
                    policy_key,
                    self.TASK_MANAGER_VALUE,
                    0,
                    previous_value[1],
                    previous_value[0],
                )
            else:
                try:
                    policy_key = winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        self.TASK_MANAGER_POLICY_KEY,
                        0,
                        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
                    )
                    try:
                        winreg.DeleteValue(policy_key, self.TASK_MANAGER_VALUE)
                    except FileNotFoundError:
                        pass
                except FileNotFoundError:
                    policy_key = None

            self._close_registry_key(winreg, policy_key)
            policy_key = None

            # Remove only our private recovery values/key. The policy key and
            # every unrelated value under it are left intact.
            try:
                state_key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    self.TASK_MANAGER_RECOVERY_KEY,
                    0,
                    winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
                )
                for value_name in (
                    self.RECOVERY_ACTIVE_VALUE,
                    self.RECOVERY_PREVIOUS_EXISTS_VALUE,
                    self.RECOVERY_PREVIOUS_VALUE,
                ):
                    try:
                        winreg.DeleteValue(state_key, value_name)
                    except FileNotFoundError:
                        pass
            finally:
                self._close_registry_key(winreg, state_key)
                state_key = None

            try:
                winreg.DeleteKey(
                    winreg.HKEY_CURRENT_USER,
                    self.TASK_MANAGER_RECOVERY_KEY,
                )
            except FileNotFoundError:
                pass

            self._notify_policy_change()
            return True
        except Exception as exc:
            print("WINDOWS TASK MANAGER RESTORE FAILED:", exc)
            return False
        finally:
            self._close_registry_key(winreg, policy_key)
            self._close_registry_key(winreg, state_key)

    def _recover_stale_task_manager_policy(self):
        """Repair the user policy left behind if a previous process was killed."""
        if os.name != "nt":
            return True
        restored = self._restore_task_manager_policy()
        if restored:
            print("WINDOWS TASK MANAGER STALE POLICY RECOVERY: CHECKED")
        return restored

    def enable(self):
        if os.name != "nt":
            self.last_error = "Windows-only fullscreen protection."
            return False
        self.last_error = ""
        if self._enabled:
            if not self._set_task_manager_disabled():
                self.disable()
                return False
            self.reapply_taskbar_visibility()
            return True

        try:
            self._initialize_apis()
            if not self._set_task_manager_disabled():
                raise RuntimeError(
                    self.last_error
                    or "The current Windows user's Task Manager policy could not be disabled."
                )
            self._pressed_modifiers.clear()
            self._hook_callback = self._hook_callback_type(
                self._keyboard_hook_proc
            )
            self._enabled = True
            module_handle = self._kernel32.GetModuleHandleW(None)
            self._hook = self._user32.SetWindowsHookExW(
                self.WH_KEYBOARD_LL,
                self._hook_callback,
                module_handle,
                0,
            )
            if not self._hook:
                error = ctypes.get_last_error()
                raise OSError(error, "SetWindowsHookExW failed")

            if not self.reapply_taskbar_visibility():
                raise RuntimeError(
                    "Windows taskbar windows were not found; protection was not enabled."
                )
            return True
        except Exception as exc:
            if not self.last_error:
                self.last_error = f"{type(exc).__name__}: {exc}"
            print("WINDOWS FULLSCREEN PROTECTION ENABLE FAILED:", exc)
            self.disable()
            return False

    def disable(self):
        self._enabled = False
        if self._user32 is not None and self._hook:
            try:
                self._user32.UnhookWindowsHookEx(self._hook)
            except Exception as exc:
                print("WINDOWS KEYBOARD HOOK RELEASE WARNING:", exc)
        self._hook = None
        self._hook_callback = None
        self._pressed_modifiers.clear()
        self._restore_taskbars()
        restored = self._restore_task_manager_policy()
        if not restored:
            print(
                "WINDOWS FULLSCREEN WARNING: Task Manager policy restore "
                "will be retried at the next application launch."
            )
        return restored

    def _taskbar_handles(self):
        if self._user32 is None:
            return []

        found = set()
        primary = self._user32.FindWindowW("Shell_TrayWnd", None)
        if primary:
            found.add(int(primary))

        def collect(hwnd, _lparam):
            try:
                class_name = ctypes.create_unicode_buffer(128)
                self._user32.GetClassNameW(hwnd, class_name, len(class_name))
                if class_name.value in self.TASKBAR_CLASSES:
                    found.add(int(hwnd))
            except Exception:
                pass
            return True

        self._enum_callback = self._enum_callback_type(collect)
        self._user32.EnumWindows(self._enum_callback, 0)
        return sorted(found)

    def reapply_taskbar_visibility(self):
        """Hide taskbars again if Explorer recreated or showed them."""
        if not self._enabled or self._user32 is None:
            return False

        handles = self._taskbar_handles()
        for handle in handles:
            hwnd = wintypes.HWND(handle)
            if handle not in self._taskbar_visibility:
                self._taskbar_visibility[handle] = bool(
                    self._user32.IsWindowVisible(hwnd)
                )
            self._user32.ShowWindow(hwnd, self.SW_HIDE)
        return bool(handles)

    def _restore_taskbars(self):
        if self._user32 is None:
            self._taskbar_visibility.clear()
            return

        for handle, was_visible in list(self._taskbar_visibility.items()):
            try:
                hwnd = wintypes.HWND(handle)
                if self._user32.IsWindow(hwnd):
                    self._user32.ShowWindow(
                        hwnd, self.SW_SHOW if was_visible else self.SW_HIDE
                    )
            except Exception as exc:
                print("WINDOWS TASKBAR RESTORE WARNING:", exc)
        self._taskbar_visibility.clear()

    def _application_is_foreground(self):
        if self._user32 is None:
            return False
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return False
        process_id = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return process_id.value == os.getpid()

    def _should_block_keyboard_event(self, key, is_down):
        """Suppress kiosk escape shortcuts only while this app is foreground."""
        if not self._application_is_foreground():
            return False

        if key in self.BLOCKED_KEYS:
            return True

        control_down = bool(self._pressed_modifiers & self.CONTROL_KEYS)
        alt_down = bool(self._pressed_modifiers & self.ALT_KEYS)

        # Block Start / Task Manager shortcuts, app switching, system menu,
        # and Alt+F4 while the password-protected fullscreen lock is active.
        if is_down and key == self.VK_ESCAPE and (control_down or alt_down):
            return True
        if is_down and key == self.VK_TAB and alt_down:
            return True
        if is_down and key == self.VK_SPACE and alt_down:
            return True
        if is_down and key == self.VK_F4 and alt_down:
            return True
        return False

    def _keyboard_hook_proc(self, code, message, data_pointer):
        try:
            if code >= self.HC_ACTION and self._enabled and data_pointer:
                event = ctypes.cast(
                    data_pointer, ctypes.POINTER(self._KeyboardEvent)
                ).contents
                key = int(event.vkCode)
                message = int(message)
                is_down = message in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
                is_up = message in (self.WM_KEYUP, self.WM_SYSKEYUP)

                if is_down:
                    if key in self.CONTROL_KEYS | self.ALT_KEYS | self.SHIFT_KEYS:
                        self._pressed_modifiers.add(key)
                elif is_up:
                    self._pressed_modifiers.discard(key)

                if self._should_block_keyboard_event(key, is_down):
                    return 1
        except Exception as exc:
            # A hook must never crash the GUI because a key event was malformed.
            print("WINDOWS KEYBOARD FILTER WARNING:", exc)

        if self._user32 is not None:
            return self._user32.CallNextHookEx(
                self._hook, code, message, data_pointer
            )
        return 0
