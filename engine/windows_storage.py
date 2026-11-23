"""Windows removable-volume discovery for USB flash-drive destinations."""
import ctypes
import os
from ctypes import wintypes


DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
BUS_TYPE_USB = 7
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400


class _StoragePropertyQuery(ctypes.Structure):
    _fields_ = [
        ("property_id", wintypes.DWORD),
        ("query_type", wintypes.DWORD),
        ("additional_parameters", ctypes.c_ubyte * 1),
    ]


def _is_usb_storage_volume(kernel32, drive_letter):
    """Identify USB-bus volumes that Windows reports as fixed disks."""
    device_path = "\\\\.\\" + str(drive_letter)[0] + ":"
    try:
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateFileW(
            device_path,
            0x80000000,
            0x00000001 | 0x00000002,
            None,
            3,
            0,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in (None, invalid_handle):
            return False

        try:
            query = _StoragePropertyQuery(0, 0, (0,))
            output = ctypes.create_string_buffer(4096)
            returned = wintypes.DWORD()
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(output),
                ctypes.sizeof(output),
                ctypes.byref(returned),
                None,
            )
            if not ok or returned.value < 32:
                return False
            bus_type = int.from_bytes(output.raw[28:32], "little")
            return bus_type == BUS_TYPE_USB
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        print("USB BUS IDENTIFICATION WARNING:", device_path, exc)
        return False




def _format_capacity(value):
    size = float(max(int(value or 0), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def enumerate_removable_storage_devices():
    """Return mounted removable volumes; never enumerate fixed internal disks."""
    if os.name != "nt":
        return []

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetLogicalDrives.restype = wintypes.DWORD
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetDriveTypeW.restype = wintypes.UINT

        kernel32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        kernel32.GetVolumeInformationW.restype = wintypes.BOOL

        kernel32.GetDiskFreeSpaceExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL

        drive_mask = int(kernel32.GetLogicalDrives())
    except Exception as exc:
        print("USB STORAGE API INITIALIZATION FAILED:", exc)
        return []

    devices = []
    for index in range(26):
        if not (drive_mask & (1 << index)):
            continue

        letter = chr(ord("A") + index)
        root = letter + ":\\"
        try:
            drive_type = kernel32.GetDriveTypeW(root)
            if drive_type == DRIVE_REMOVABLE:
                pass
            elif drive_type == DRIVE_FIXED and _is_usb_storage_volume(
                kernel32, letter + ":"
            ):
                # Some USB flash drives advertise themselves as fixed disks.
                pass
            else:
                continue

            volume_name = ctypes.create_unicode_buffer(261)
            filesystem_name = ctypes.create_unicode_buffer(261)
            volume_serial = wintypes.DWORD()
            max_component = wintypes.DWORD()
            filesystem_flags = wintypes.DWORD()

            if not kernel32.GetVolumeInformationW(
                root,
                volume_name,
                len(volume_name),
                ctypes.byref(volume_serial),
                ctypes.byref(max_component),
                ctypes.byref(filesystem_flags),
                filesystem_name,
                len(filesystem_name),
            ):
                # Ignore empty card readers and volumes Windows cannot identify.
                continue

            free_bytes = ctypes.c_ulonglong()
            total_bytes = ctypes.c_ulonglong()
            total_free_bytes = ctypes.c_ulonglong()
            if not kernel32.GetDiskFreeSpaceExW(
                root,
                ctypes.byref(free_bytes),
                ctypes.byref(total_bytes),
                ctypes.byref(total_free_bytes),
            ):
                continue

            label = volume_name.value.strip() or "USB Flash"
            capacity = int(total_bytes.value)
            available = int(free_bytes.value)
            serial = f"USBVOL:{int(volume_serial.value):08X}:{letter}:"
            display = f"{label} ({letter}:)"
            devices.append({
                "serial": serial,
                "id": serial,
                "type": "usb_storage",
                "name": display,
                "model": display,
                "display_name": display,
                "root": root,
                "drive": letter + ":",
                "filesystem": filesystem_name.value,
                "volume_serial": f"{int(volume_serial.value):08X}",
                "total_bytes": capacity,
                "free_bytes": available,
                "capacity_text": _format_capacity(capacity),
                "free_text": _format_capacity(available),
                "units": 1,
                "unit_price": 500,
                "billing_value": 500,
            })
        except Exception as exc:
            print("USB STORAGE ENUMERATION WARNING:", root, exc)

    return sorted(devices, key=lambda item: item["drive"].casefold())
