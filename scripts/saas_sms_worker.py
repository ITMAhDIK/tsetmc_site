#!/usr/bin/env python3
"""Run the dedicated SaaS SMS notification outbox worker."""
from saas_core.notifications import run_sms_worker

if __name__ == "__main__":
    run_sms_worker()
