import ssl
from email import message_from_bytes
from typing import ClassVar

import pytest

from mailer import send_email, send_email_from_env


def _send(**overrides):
    kwargs = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "user",
        "smtp_password": "pass",
        "from_addr": "from@example.com",
        "to_addr": "to@example.com",
        "subject": "GitHub digest",
    }
    kwargs.update(overrides)
    send_email("<html><body>hi</body></html>", **kwargs)


class FakeSMTP:
    instances: ClassVar[list] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls", context))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send_message", msg))


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    return FakeSMTP


def test_connects_with_timeout_verifies_tls_then_logs_in_and_sends(fake_smtp):
    _send()
    (smtp,) = fake_smtp.instances
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.timeout is not None
    name, context = smtp.calls[0]
    assert name == "starttls"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert smtp.calls[1] == ("login", "user", "pass")
    assert smtp.calls[2][0] == "send_message"


def test_message_has_plain_and_html_parts_and_headers(fake_smtp):
    _send()
    (smtp,) = fake_smtp.instances
    parsed = message_from_bytes(smtp.calls[2][1].as_bytes())
    assert {p.get_content_type() for p in parsed.walk()} >= {"text/plain", "text/html"}
    assert parsed["Subject"] == "GitHub digest"
    assert parsed["From"] == "from@example.com"
    assert parsed["To"] == "to@example.com"


def test_from_env_reads_settings(fake_smtp, monkeypatch):
    for key, value in {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "pass",
        "DIGEST_FROM_EMAIL": "from@example.com",
        "DIGEST_TO_EMAIL": "to@example.com",
    }.items():
        monkeypatch.setenv(key, value)

    send_email_from_env("<html>hi</html>", subject="GitHub digest")

    (smtp,) = fake_smtp.instances
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.calls[1] == ("login", "user", "pass")


def test_from_env_raises_on_missing_var(fake_smtp, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with pytest.raises(KeyError):
        send_email_from_env("<html>hi</html>", subject="GitHub digest")
