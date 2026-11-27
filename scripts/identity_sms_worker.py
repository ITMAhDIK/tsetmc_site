#!/usr/bin/env python3
"""Run the Identity database's encrypted SMS outbox worker."""
import os
from saas_core.notifications import run_sms_worker

if __name__ == "__main__":
    os.environ["SAAS_DATABASE_URL"] = os.environ.get("IDENTITY_DATABASE_URL", "")
    os.environ["SAAS_EXPECTED_DATABASE_NAME"] = os.environ.get("IDENTITY_EXPECTED_DATABASE_NAME", "")
    os.environ["SAAS_OUTBOX_FERNET_KEY"] = os.environ.get("IDENTITY_OUTBOX_FERNET_KEY", "")
    run_sms_worker()
