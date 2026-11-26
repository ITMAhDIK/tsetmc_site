import json
from urllib.parse import parse_qs

import pytest

from saas_core.notifications import (
    KavenegarSmsSender,
    SmsDeliveryError,
    format_otp_message,
)


def test_otp_sms_message_contains_only_the_code_and_expiry_notice():
    message = format_otp_message("123456", "login")
    assert "123456" in message
    assert "۵ دقیقه" in message
    assert "+989" not in message


@pytest.mark.parametrize("otp,purpose", [
    ("12345", "login"),
    ("1234567", "login"),
    ("12A456", "login"),
    ("123456", "withdrawal"),
])
def test_otp_sms_message_rejects_invalid_payload(otp, purpose):
    with pytest.raises(ValueError):
        format_otp_message(otp, purpose)


def test_kavenegar_sender_uses_https_and_form_encoded_message(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b'{"return":{"status":200,"message":"OK"}}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        captured["form"] = parse_qs(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("saas_core.notifications.urlopen", fake_urlopen)
    sender = KavenegarSmsSender(api_key="a" * 32, sender="10004346")
    sender.send("+989000000001", format_otp_message("654321", "login"))

    assert captured["url"].startswith("https://api.kavenegar.com/v1/")
    assert captured["url"].endswith("/sms/send.json")
    assert captured["method"] == "POST"
    assert captured["form"]["receptor"] == ["09000000001"]
    assert captured["form"]["sender"] == ["10004346"]
    assert "654321" in captured["form"]["message"][0]


def test_kavenegar_rejection_does_not_leak_provider_body_or_api_key(monkeypatch):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b'{"return":{"status":401,"message":"private provider details"}}'

    monkeypatch.setattr("saas_core.notifications.urlopen", lambda *_a, **_k: FakeResponse())
    secret = "b" * 32
    sender = KavenegarSmsSender(api_key=secret, sender="10004346")

    with pytest.raises(SmsDeliveryError) as error:
        sender.send("+989000000001", format_otp_message("123456", "login"))

    assert secret not in str(error.value)
    assert "private provider details" not in str(error.value)


def test_kavenegar_requires_https_api_key_and_sender_configuration():
    with pytest.raises(ValueError):
        KavenegarSmsSender(api_key="", sender="10004346")
    with pytest.raises(ValueError):
        KavenegarSmsSender(api_key="a" * 32, sender="")
