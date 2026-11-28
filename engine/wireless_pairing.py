"""Helpers for Android's official Wireless Debugging QR pairing flow.

The QR payload and mDNS service names follow the ADB Wi-Fi pairing protocol.
This module keeps parsing and transport de-duplication deterministic/testable.
"""

import re
from pathlib import Path


_SERVICE_RE = re.compile(r"^studio-[A-Za-z0-9]{8,16}$")
_PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{10,32}$")


def build_adb_pairing_qr_payload(service_name: str, password: str) -> str:
    """Build Android Studio/AOSP's WIFI:T:ADB QR payload."""
    service_name = str(service_name or "")
    password = str(password or "")
    if not _SERVICE_RE.fullmatch(service_name):
        raise ValueError("Invalid temporary ADB pairing service name.")
    if not _PASSWORD_RE.fullmatch(password):
        raise ValueError("Invalid temporary ADB pairing password.")
    return f"WIFI:T:ADB;S:{service_name};P:{password};;"


def _split_endpoint(value: str):
    value = str(value or "").strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or closing + 1 >= len(value) or value[closing + 1] != ":":
            return None
        host = value[1:closing]
        port_text = value[closing + 2:]
    else:
        if ":" not in value:
            return None
        host, port_text = value.rsplit(":", 1)
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        return None
    if not host or not 1 <= port <= 65535:
        return None
    return host, port


def parse_adb_mdns_services(
    output: str,
    service_type: str,
    *,
    instance_name: str = "",
    host: str = "",
):
    """Parse 'adb mdns services' lines without relying on column spacing."""
    requested_type = str(service_type or "").rstrip(".").casefold()
    requested_instance = str(instance_name or "").rstrip(".").casefold()
    requested_host = str(host or "").strip().strip("[]").casefold()
    matches = []

    for line in str(output or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        service_index = next(
            (
                index
                for index, token in enumerate(parts)
                if token.rstrip(".").casefold() == requested_type
            ),
            -1,
        )
        if service_index <= 0 or service_index + 1 >= len(parts):
            continue

        instance = parts[service_index - 1].rstrip(".")
        endpoint = parts[service_index + 1]
        parsed = _split_endpoint(endpoint)
        if parsed is None:
            continue

        resolved_host, port = parsed
        if requested_instance and instance.casefold() != requested_instance:
            continue
        if requested_host and resolved_host.strip("[]").casefold() != requested_host:
            continue

        matches.append(
            {
                "instance": instance,
                "service_type": requested_type,
                "host": resolved_host,
                "port": port,
                "endpoint": endpoint,
            }
        )
    return matches


def deduplicate_adb_devices(devices, preferred_serials=()):
    """Collapse ADB transports for one physical phone into a single UI row.

    Only a real Android serial exposed by ro.serialno/ro.boot.serialno is used
    for grouping. If it is absent, the transport stays independent; model names
    are never used because separate phones may share the same model.
    """
    preferred = {str(value) for value in (preferred_serials or ()) if str(value)}
    groups = {}
    independent = []

    for index, original in enumerate(devices or ()):
        item = dict(original)
        physical_id = str(item.get("physical_id") or "").strip()
        if not physical_id or physical_id.casefold() in {
            "unknown", "null", "none", "0"
        }:
            independent.append((index, item))
            continue
        groups.setdefault(physical_id.casefold(), []).append((index, item))

    selected = list(independent)
    for entries in groups.values():
        def preference(entry):
            index, item = entry
            serial = str(item.get("serial") or "")
            if serial in preferred:
                rank = 0
            elif ":" not in serial and "_adb-tls-connect._tcp" not in serial and not serial.startswith("adb-"):
                rank = 1
            elif "_adb-tls-connect._tcp" in serial:
                rank = 2
            elif ":" in serial:
                rank = 3
            else:
                rank = 4
            return rank, index

        selected.append(min(entries, key=preference))

    selected.sort(key=lambda entry: entry[0])
    return [item for _, item in selected]


def save_adb_pairing_qr(path, service_name: str, password: str):
    """Render an Android ADB pairing QR as PNG using Qt (no Pillow required)."""
    import qrcode
    from PySide6.QtGui import QImage

    payload = build_adb_pairing_qr_payload(service_name, password)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if not matrix or not matrix[0]:
        raise RuntimeError("QR generator returned an empty matrix.")

    scale = 8
    side = len(matrix) * scale
    image = QImage(side, side, QImage.Format.Format_RGB32)
    if image.isNull():
        raise RuntimeError("Qt could not allocate the QR image.")
    image.fill(0xFFFFFFFF)

    black = 0xFF111827
    for row_index, row in enumerate(matrix):
        for column_index, is_dark in enumerate(row):
            if not is_dark:
                continue
            left = column_index * scale
            top = row_index * scale
            for y in range(top, top + scale):
                for x in range(left, left + scale):
                    image.setPixel(x, y, black)

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output), "PNG"):
        raise OSError(f"Could not save ADB pairing QR image: {output}")
    return output
