"""Sends the rendered HTML digest via an SMTP relay.

Kept separate from fetching/rendering so a consumer could swap it for a Slack
post, a workflow artifact, or nothing at all.

Connects over STARTTLS (submission port, typically 587) with certificate and
hostname verification. Implicit-TLS ports (465) are not supported.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_TIMEOUT = 30


def send_email(
    html_body: str,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_addr: str,
    to_addr: str,
    subject: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText("This email requires an HTML-capable mail client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=_TIMEOUT) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def send_email_from_env(html_body: str, *, subject: str) -> None:
    """send_email(), reading the relay and recipient from the environment:
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, DIGEST_FROM_EMAIL,
    DIGEST_TO_EMAIL.
    """
    send_email(
        html_body,
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=int(os.environ["SMTP_PORT"]),
        smtp_user=os.environ["SMTP_USERNAME"],
        smtp_password=os.environ["SMTP_PASSWORD"],
        from_addr=os.environ["DIGEST_FROM_EMAIL"],
        to_addr=os.environ["DIGEST_TO_EMAIL"],
        subject=subject,
    )
