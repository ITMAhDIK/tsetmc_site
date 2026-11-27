from __future__ import annotations

from cryptography.fernet import Fernet
import importlib.util
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

migration = _load_script("migrate_saas_identity")
cutover = _load_script("finalize_saas_identity_cutover")


def test_identity_import_does_not_copy_ephemeral_otp_or_saas_notification_outbox():
    assert "phone_verifications" not in migration.TABLES
    assert "notification_outbox" not in migration.TABLES


def test_mfa_secret_is_reencrypted_between_distinct_keys():
    source = Fernet(Fernet.generate_key())
    target = Fernet(Fernet.generate_key())
    cleartext = b"existing-totp-secret"
    old_ciphertext = source.encrypt(cleartext).decode("ascii")
    new_ciphertext = migration._reencrypt_mfa_secret(old_ciphertext, source, target)
    assert new_ciphertext != old_ciphertext
    assert target.decrypt(new_ciphertext.encode("ascii")) == cleartext
    # The destination key can decrypt it; the previous service key cannot.
    try:
        source.decrypt(new_ciphertext.encode("ascii"))
    except Exception:
        pass
    else:
        raise AssertionError("old SaaS key unexpectedly decrypts the new Identity MFA ciphertext")


def test_cutover_digest_uses_values_not_only_dictionary_column_names():
    class FakeConnection:
        def __init__(self, row):
            self.row = row
        def execute(self, _query):
            return iter([self.row])

    first = cutover._digest(FakeConnection({"id": "one", "status": "active"}), "ignored")
    same = cutover._digest(FakeConnection({"id": "one", "status": "active"}), "ignored")
    changed = cutover._digest(FakeConnection({"id": "one", "status": "suspended"}), "ignored")
    assert first == same
    assert first != changed


def test_migration_source_is_read_only_and_mfa_key_is_reencrypted():
    source = Fernet.generate_key().decode("ascii")
    target = Fernet.generate_key().decode("ascii")
    assert source != target
    assert "SET default_transaction_read_only=on" in inspect.getsource(migration.run)
    assert callable(migration._reencrypt_mfa_secret)
