"""Protocol, mDNS discovery, QR rendering, and same-phone transport tests."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.wireless_pairing import (
    build_adb_pairing_qr_payload,
    deduplicate_adb_devices,
    parse_adb_mdns_services,
    save_adb_pairing_qr,
)


def test_qr_payload_matches_android_pairing_format():
    payload = build_adb_pairing_qr_payload(
        "studio-AbCd234567",
        "CNPlayHubQR1234",
    )
    assert payload == "WIFI:T:ADB;S:studio-AbCd234567;P:CNPlayHubQR1234;;"

    for service_name, password in (
        ("studio-too-short", "CNPlayHubQR1234"),
        ("not-studio-AbCd2345", "CNPlayHubQR1234"),
        ("studio-AbCd234567", "has spaces 123"),
        ("studio-AbCd234567", "short"),
    ):
        try:
            build_adb_pairing_qr_payload(service_name, password)
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe QR payload input was accepted.")


def test_mdns_pairing_service_discovery_is_exact():
    output = """List of discovered mdns services
studio-AbCd234567  _adb-tls-pairing._tcp  192.168.1.106:46109
adb-RF8M92Q8EEM-zB0Wgc  _adb-tls-connect._tcp  192.168.1.106:45313
studio-OTHER987654  _adb-tls-pairing._tcp  192.168.1.107:37001
"""
    pairing = parse_adb_mdns_services(
        output,
        "_adb-tls-pairing._tcp",
        instance_name="studio-AbCd234567",
    )
    assert len(pairing) == 1
    assert pairing[0]["host"] == "192.168.1.106"
    assert pairing[0]["port"] == 46109
    assert pairing[0]["endpoint"] == "192.168.1.106:46109"

    connect = parse_adb_mdns_services(
        output,
        "_adb-tls-connect._tcp",
        host="192.168.1.106",
    )
    assert len(connect) == 1
    assert connect[0]["instance"] == "adb-RF8M92Q8EEM-zB0Wgc"
    assert connect[0]["port"] == 45313

    assert not parse_adb_mdns_services(
        output,
        "_adb-tls-pairing._tcp",
        instance_name="studio-MISSING1234",
    )
    assert not parse_adb_mdns_services(
        output,
        "_adb-tls-connect._tcp",
        host="192.168.1.999",
    )


def test_mdns_parser_supports_bracketed_ipv6_and_ignores_bad_lines():
    output = """noise without endpoint
studio-AbCd234567 _adb-tls-pairing._tcp [fe80::1234]:37031
broken _adb-tls-pairing._tcp 192.168.1.5:not-a-port
"""
    parsed = parse_adb_mdns_services(
        output,
        "_adb-tls-pairing._tcp",
    )
    assert len(parsed) == 1
    assert parsed[0]["host"] == "fe80::1234"
    assert parsed[0]["port"] == 37031


def test_same_physical_phone_is_shown_once_without_hiding_identical_models():
    phone = [
        {
            "serial": "192.168.1.106:38587",
            "physical_id": "RF8M92Q8EEM",
            "model": "SM-N975U",
        },
        {
            "serial": "adb-RF8M92Q8EEM-zB0Wgc._adb-tls-connect._tcp",
            "physical_id": "RF8M92Q8EEM",
            "model": "SM-N975U",
        },
        {
            "serial": "RF8M92Q8EEM",
            "physical_id": "RF8M92Q8EEM",
            "model": "SM-N975U",
        },
        {
            "serial": "OTHER_NOTE10",
            "physical_id": "OTHER_NOTE10",
            "model": "SM-N975U",
        },
        {
            "serial": "MODEL_ONLY_A",
            "physical_id": "",
            "model": "SM-N975U",
        },
        {
            "serial": "MODEL_ONLY_B",
            "physical_id": "",
            "model": "SM-N975U",
        },
    ]

    preferred = deduplicate_adb_devices(
        phone,
        preferred_serials={"192.168.1.106:38587"},
    )
    assert [row["serial"] for row in preferred] == [
        "192.168.1.106:38587",
        "OTHER_NOTE10",
        "MODEL_ONLY_A",
        "MODEL_ONLY_B",
    ]

    automatic = deduplicate_adb_devices(phone)
    assert "RF8M92Q8EEM" in [row["serial"] for row in automatic]
    assert sum(
        row.get("physical_id") == "RF8M92Q8EEM" for row in automatic
    ) == 1


def test_qr_png_is_renderable_and_contains_a_quiet_zone():
    with TemporaryDirectory(prefix="cn-playhub-wireless-qr-") as directory:
        path = save_adb_pairing_qr(
            Path(directory) / "pairing.png",
            "studio-AbCd234567",
            "CNPlayHubQR1234",
        )
        assert path.is_file()
        data = path.read_bytes()
        assert data[:8] == bytes((137, 80, 78, 71, 13, 10, 26, 10))
        assert len(data) > 256

        from PySide6.QtGui import QImage
        image = QImage(str(path))
        assert not image.isNull()
        assert image.width() == image.height()
        assert image.width() >= 200


def main():
    test_qr_payload_matches_android_pairing_format()
    test_mdns_pairing_service_discovery_is_exact()
    test_mdns_parser_supports_bracketed_ipv6_and_ignores_bad_lines()
    test_same_physical_phone_is_shown_once_without_hiding_identical_models()
    test_qr_png_is_renderable_and_contains_a_quiet_zone()
    print("WIRELESS QR PAIRING SIMULATION: PASS")
    print("QR payload, mDNS discovery, IPv6 parsing, device de-duplication, PNG render: PASS")


if __name__ == "__main__":
    main()
