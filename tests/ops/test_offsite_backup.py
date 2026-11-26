from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "saas_offsite_backup.sh"


def _prepare(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    archive = backup_dir / "tloomberg-saas-test.dump.age"
    archive.write_bytes(b"age-encryption.org/v1\n" + b"encrypted-payload")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_file = Path(str(archive) + ".sha256")
    checksum_file.write_text(checksum + "\n", encoding="ascii")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "rclone-calls.txt"
    fake = bin_dir / "rclone"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$RCLONE_CAPTURE\"\n"
        "case \"$1\" in copyto) exit 0 ;; lsf) printf 'object\\n'; exit 0 ;; esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    env = os.environ.copy()
    env.update({
        "SAAS_BACKUP_REMOTE": "s3:private-bucket/tloomberg",
        "SAAS_BACKUP_DIR": str(backup_dir),
        "RCLONE_CAPTURE": str(capture),
        "RCLONE_LOCAL_ARCHIVE": str(archive),
        "RCLONE_LOCAL_CHECKSUM": str(checksum_file),
        "PATH": str(bin_dir) + os.pathsep + env["PATH"],
    })
    return archive, capture, env


def test_offsite_backup_uploads_only_age_archive_and_checksum_immutably(tmp_path):
    archive, capture, env = _prepare(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = capture.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 6
    assert all("--immutable" in line and "--checksum" in line for line in calls[:2])
    assert archive.name in calls[0]
    assert archive.name + ".sha256" in calls[1]
    assert "Off-host encrypted backup uploaded and verified:" in result.stdout


def test_offsite_backup_refuses_plaintext_or_unencrypted_artifacts(tmp_path):
    archive, capture, env = _prepare(tmp_path)
    archive.write_bytes(b"plain postgres custom format")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "age encryption header" in result.stderr
    assert not capture.exists()


def test_offsite_backup_rejects_checksum_mismatch_before_network_upload(tmp_path):
    archive, capture, env = _prepare(tmp_path)
    Path(str(archive) + ".sha256").write_text("0" * 64, encoding="ascii")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert "SHA-256 verification failed" in result.stderr
    assert not capture.exists()


def test_offsite_backup_requires_named_remote_and_encrypted_archive(tmp_path):
    archive, capture, env = _prepare(tmp_path)
    env["SAAS_BACKUP_REMOTE"] = str(tmp_path / "local-folder")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "rclone remote" in result.stderr
    assert not capture.exists()

def test_offsite_backup_detects_remote_object_corruption(tmp_path):
    archive, capture, env = _prepare(tmp_path)
    env["RCLONE_CORRUPT_REMOTE"] = "1"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 4
    assert "Off-host backup content verification failed" in result.stderr
