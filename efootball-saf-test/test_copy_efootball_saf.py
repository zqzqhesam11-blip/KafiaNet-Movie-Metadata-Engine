#!/usr/bin/env python3
"""Stream D:\\games\\...\\2027\\data into the Ziru_27 eFootball SAF data tree."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

DEFAULT_SOURCE = Path(r"D:\games\بيس (eFootball™)\2027\data")
DEFAULT_ADB = Path(r"C:\Program Files (x86)\platform-tools\adb.exe")
PORT = 18766
CHUNK_SIZE = 1024 * 1024


def run_adb(adb: str, args: list[str]) -> str:
    result = subprocess.run(
        [adb, *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "ADB command failed").strip())
    return result.stdout.strip()


def connected_devices(adb: str) -> list[str]:
    output = run_adb(adb, ["devices"])
    found = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            found.append(parts[0])
    return found


def ensure_forward(adb: str, serial: str) -> bool:
    output = run_adb(adb, ["forward", "--list"])
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == f"tcp:{PORT}":
            if parts[0] == serial and parts[2] == f"tcp:{PORT}":
                return False
            raise RuntimeError(
                f"ADB port {PORT} is already forwarded by another device or target: {line}"
            )
    run_adb(adb, ["-s", serial, "forward", f"tcp:{PORT}", f"tcp:{PORT}"])
    return True


def api_json(token: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    headers = {"X-Kafia-Token": token}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(body))
    connection.request("GET" if body is None else "POST", path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Helper returned invalid JSON ({response.status}): {raw[:300]!r}") from exc
    if response.status != 200:
        raise RuntimeError(data.get("error") or f"Helper HTTP {response.status}")
    return data


def collect_source_files(source: Path) -> list[Path]:
    if not source.is_dir():
        raise FileNotFoundError(f"Source folder does not exist: {source}")

    files: list[Path] = []
    for root, directories, names in os.walk(source, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name for name in directories
            if not (root_path / name).is_symlink()
        ]
        for name in names:
            candidate = root_path / name
            if candidate.is_file() and not candidate.is_symlink():
                files.append(candidate)
    files.sort(key=lambda item: str(item.relative_to(source)).casefold())
    return files


def stream_one_file(token: str, job_id: str, source_root: Path, file_path: Path) -> int:
    relative = file_path.relative_to(source_root).as_posix()
    # One filesystem metadata lookup is required to frame the HTTP stream.
    # The destination is never stat'ed or re-read after writing.
    content_length = file_path.stat().st_size

    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=None)
    connection.putrequest("POST", "/file")
    connection.putheader("X-Kafia-Token", token)
    connection.putheader("X-Kafia-Job", job_id)
    connection.putheader("X-Kafia-Relative", quote(relative, safe=""))
    connection.putheader("Content-Type", "application/octet-stream")
    connection.putheader("Content-Length", str(content_length))
    connection.endheaders()

    sent = 0
    with file_path.open("rb") as source:
        while True:
            block = source.read(CHUNK_SIZE)
            if not block:
                break
            connection.send(block)
            sent += len(block)

    response = connection.getresponse()
    raw = response.read()
    connection.close()
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid response for {relative}: {raw[:300]!r}") from exc
    if response.status != 200 or not result.get("accepted"):
        raise RuntimeError(result.get("error") or f"Failed writing {relative}")
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="اختبار النسخ المباشر إلى مساحة eFootball الخاصة بنسخة Ziru_27 عبر SAF."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB)
    parser.add_argument("--serial", default="")
    parser.add_argument("--token", required=True, help="رمز الجلسة المعروض على شاشة تطبيق الاختبار")
    args = parser.parse_args()

    adb = str(args.adb) if args.adb.exists() else "adb"
    source = args.source.resolve()
    files = collect_source_files(source)
    if not files:
        raise RuntimeError(f"No files found under source folder: {source}")

    devices = connected_devices(adb)
    if args.serial:
        if args.serial not in devices:
            raise RuntimeError(f"Selected ADB device is not connected: {args.serial}")
        serial = args.serial
    elif len(devices) == 1:
        serial = devices[0]
    elif not devices:
        raise RuntimeError("No authorized ADB device found. Connect the phone and enable USB debugging.")
    else:
        raise RuntimeError("More than one ADB device is connected. Pass --serial SERIAL.")

    created_forward = ensure_forward(adb, serial)
    try:
        health = api_json(args.token, "/health")
        if not health.get("ready") or health.get("rootName", "").casefold() != "data":
            raise RuntimeError(
                "The helper is not ready. Select eFootball > data in the phone's SAF picker first."
            )

        print("=== PREFLIGHT ===")
        print(f"Phone: {serial}")
        print(f"Source: {source}")
        print(f"Destination: eFootball / data ({health.get('authority', 'SAF')})")
        print(f"Files to stream: {len(files)}")
        print("Mode: direct SAF stream; replace matching names; keep unrelated destination files.")
        print("No destination size scan, no post-copy size verification, and no sync.")
        print("\nStarting copy...")

        job_id = uuid.uuid4().hex
        started = api_json(args.token, "/start", {"jobId": job_id, "fileCount": len(files)})
        if not started.get("accepted"):
            raise RuntimeError(started.get("error", "The helper rejected the copy job."))

        total_sent = 0
        for index, file_path in enumerate(files, start=1):
            total_sent += stream_one_file(args.token, job_id, source, file_path)
            relative = file_path.relative_to(source).as_posix()
            print(f"[{index}/{len(files)}] {relative}")

        finished = api_json(args.token, "/finish", {"jobId": job_id})
        if not finished.get("success"):
            raise RuntimeError(finished.get("error") or "The helper reported an incomplete copy.")

        print("\n=== COMPLETE ===")
        print(f"Files streamed: {finished.get('copiedFiles', 0)}")
        print(f"Bytes streamed: {total_sent:,}")
        print("Existing files with matching names were replaced.")
        print("Other destination files were left untouched.")
        print("Source files were not moved, renamed, or deleted.")
        return 0
    finally:
        if created_forward:
            try:
                run_adb(adb, ["-s", serial, "forward", "--remove", f"tcp:{PORT}"])
            except Exception:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled by user. Source files remain untouched.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
