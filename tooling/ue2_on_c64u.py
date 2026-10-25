#!/usr/bin/env python3
"""Refresh, build, upload and start a C64U UE2 update.

Run from this checkout with: tooling/ue2_on_c64u.py
Credentials are taken only from environment variables and are never printed.
"""

from __future__ import annotations

import argparse
import ftplib
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "ue2-on-c64u"
DEFAULT_HOST = "192.168.1.146"
REMOTE_DIR = os.environ.get("C64U_FIRMWARE_DIR", "/USB0/firmware/ue2-on-c64u")
sys.path.insert(0, str(ROOT / "tests" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "e2e" / "lib"))
from browser import Browser  # noqa: E402
from rest_backend import RestBackend  # noqa: E402
from report import Failure  # noqa: E402


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def upload(host: str, artifact: Path) -> str:
    user = os.environ.get("C64U_FTP_USER", "anonymous")
    password = os.environ.get("C64U_FTP_PASSWORD", "")
    remote_dir = REMOTE_DIR
    with ftplib.FTP(host, timeout=30) as ftp:
        ftp.login(user, password)
        root = {entry.rsplit("/", 1)[-1].upper() for entry in ftp.nlst("/")}
        if remote_dir.strip("/").split("/", 1)[0].upper() not in root:
            if "USB0" in remote_dir.upper() and "USB2" in root:
                remote_dir = "/USB2/Firmware/ue2-on-c64u"
                print(f"Requested USB0 volume is not mounted; using {remote_dir}", flush=True)
            else:
                raise RuntimeError(f"Requested firmware volume is not mounted: {REMOTE_DIR}")
        remote = f"{remote_dir}/{artifact.name}"
        current = ""
        for part in remote_dir.strip("/").split("/"):
            current += "/" + part
            try:
                ftp.mkd(current)
            except ftplib.error_perm:
                pass
            try:
                ftp.cwd(current)
            except ftplib.error_perm as exc:
                raise RuntimeError(f"Remote directory is not mounted/accessible: {current}") from exc
        temp = artifact.name + ".uploading"
        with artifact.open("rb") as source:
            ftp.storbinary(f"STOR {temp}", source)
        if ftp.size(temp) != artifact.stat().st_size:
            raise RuntimeError("FTP upload size verification failed")
        try:
            ftp.delete(artifact.name)
        except ftplib.error_perm:
            pass
        ftp.rename(temp, artifact.name)
        if ftp.size(artifact.name) != artifact.stat().st_size:
            raise RuntimeError("Final FTP artifact size verification failed")
    return remote


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("C64U_HOST", DEFAULT_HOST))
    args = parser.parse_args()

    if subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip() != BRANCH:
        parser.error(f"must run on branch {BRANCH}")
    if subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode or subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=ROOT
    ).returncode:
        parser.error("tracked working tree changes must be committed before upstream synchronization")

    run("git", "fetch", "upstream", "master")
    has_main = subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/main"], cwd=ROOT).returncode == 0
    if has_main:
        run("git", "switch", "main")
    else:
        run("git", "switch", "--create", "main", "upstream/master")
    run("git", "merge", "--ff-only", "upstream/master")
    run("git", "switch", BRANCH)
    run("git", "merge", "main")
    run("./build-tool", "u64ii")

    version = subprocess.check_output(["git", "rev-parse", "--short=8", "HEAD"], cwd=ROOT, text=True).strip()
    artifact = ROOT / f"update_{version}.ue2"
    if not artifact.is_file():
        raise RuntimeError(f"Build completed without producing expected artifact {artifact.name}")
    remote = upload(args.host, artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    print(f"Uploaded {artifact.name} ({artifact.stat().st_size} bytes, SHA256 {digest}) to {remote}")

    try:
        backend = RestBackend(args.host, os.environ.get("C64U_REST_PASSWORD") or None,
                              interface_type=None)
        browser = Browser(backend, range(2, 24), 24)
        backend.ensure_ready()
        remote_dir = remote.rsplit("/", 1)[0]
        browser.go_to_directory(remote_dir)
        browser.select_entry(artifact.name)
        labels = browser.open_context_menu()
        if not any(label.split("||", 1)[0].strip() == "Run Update" for label in labels):
            raise RuntimeError(f"{artifact.name} has no Run Update action; menu offers {labels}")
        browser.choose_overlay_item(labels, "Run Update")
        print(f"Started the verified Run Update action for {remote_dir}/{artifact.name} by REST keyboard injection.")
    except (OSError, urllib.error.URLError, Failure) as exc:
        raise RuntimeError(f"C64U REST interaction failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, RuntimeError, *ftplib.all_errors) as exc:
        print(f"ue2-on-c64u: {exc}", file=sys.stderr)
        sys.exit(1)
