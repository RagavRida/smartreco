"""Email delivery for the proactive daily digest (bonus).

When SMTP is not configured the email is rendered and written to
data/outbox/ instead of being sent, so the feature is demoable without a mail
server and never crashes a scheduled run.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, settings

logger = logging.getLogger(__name__)

OUTBOX = DATA_DIR / "outbox"


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send_email(to_address: str, subject: str, html_body: str, text_body: str = "") -> dict[str, Any]:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = to_address
    message.set_content(text_body or _strip_html(html_body))
    message.add_alternative(html_body, subtype="html")

    if not smtp_configured():
        path = _write_outbox(to_address, subject, html_body)
        logger.info("SMTP not configured — digest written to %s", path)
        return {"sent": False, "outbox_path": str(path), "reason": "smtp_not_configured"}

    try:
        if settings.smtp_use_tls:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
                server.starttls()
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
        logger.info("Digest email sent to %s", to_address)
        return {"sent": True, "reason": "ok"}
    except Exception as exc:  # noqa: BLE001 - a mail failure must not kill the job
        path = _write_outbox(to_address, subject, html_body)
        logger.error("Digest send failed (%s) — written to %s", str(exc)[:160], path)
        return {"sent": False, "outbox_path": str(path), "reason": str(exc)[:200]}


def _write_outbox(to_address: str, subject: str, html_body: str) -> Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in to_address)[:40]
    path = OUTBOX / f"{stamp}-{safe}.html"
    path.write_text(f"<!-- To: {to_address} | Subject: {subject} -->\n{html_body}", encoding="utf-8")
    return path


def _strip_html(html: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", html)


def render_digest_html(
    *,
    greeting: str,
    body: str,
    closing: str,
    items: list[dict[str, Any]],
    base_url: str = "http://localhost:8000",
) -> str:
    cards = []
    for item in items:
        cards.append(
            f"""
      <tr><td style="padding:0 0 14px 0;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border:1px solid #e5e7eb;border-radius:10px;">
          <tr><td style="padding:16px 18px;">
            <div style="font:600 16px/1.35 -apple-system,Segoe UI,Roboto,sans-serif;color:#111827;">
              {escape(str(item.get('title', '')))}
            </div>
            <div style="font:500 12px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#6d28d9;
                        text-transform:uppercase;letter-spacing:.04em;margin:6px 0 8px;">
              {escape(str(item.get('hook', '') or item.get('category', '')))}
            </div>
            <div style="font:400 14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:#4b5563;">
              {escape(str(item.get('reason', '')))}
            </div>
            <div style="margin-top:12px;">
              <a href="{base_url}/product/{escape(str(item.get('slug', '')))}"
                 style="font:600 13px/1 -apple-system,Segoe UI,Roboto,sans-serif;color:#ffffff;
                        background:#6d28d9;padding:10px 16px;border-radius:7px;
                        text-decoration:none;display:inline-block;">View course</a>
            </div>
          </td></tr>
        </table>
      </td></tr>"""
        )

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f6f5fb;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:14px;padding:32px;">
        <tr><td>
          <div style="font:700 13px/1 -apple-system,Segoe UI,Roboto,sans-serif;color:#6d28d9;
                      letter-spacing:.08em;text-transform:uppercase;">SmartReco</div>
          <div style="font:600 22px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;color:#111827;
                      margin:14px 0 12px;">{escape(greeting)}</div>
          <div style="font:400 15px/1.65 -apple-system,Segoe UI,Roboto,sans-serif;color:#374151;
                      margin-bottom:24px;">{escape(body)}</div>
          <table width="100%" cellpadding="0" cellspacing="0">{''.join(cards)}</table>
          <div style="font:400 14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:#6b7280;
                      margin-top:8px;">{escape(closing)}</div>
          <div style="font:400 12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#9ca3af;
                      margin-top:26px;border-top:1px solid #eef0f4;padding-top:16px;">
            You are receiving this because daily recommendations are on for your SmartReco account.
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
