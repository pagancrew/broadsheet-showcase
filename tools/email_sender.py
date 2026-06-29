"""
tools/email_sender.py

Sends the daily digest as an HTML email via Brevo SMTP.

Required env vars:
  SENDER_EMAIL      — the verified From address (set up in Brevo → Senders)
  BREVO_SMTP_LOGIN  — auto-generated SMTP login from Brevo → SMTP & API → SMTP
  BREVO_SMTP_KEY    — SMTP key generated in the same section (shown once)
"""

import html
import logging
import os
import smtplib
import urllib.parse
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _esc(s) -> str:
    """HTML-escape any user/LLM-supplied string before interpolating into HTML."""
    return html.escape("" if s is None else str(s), quote=True)


def _coerce_recipient(r) -> dict:
    """Normalise a recipient to {email, subscriber_id}. Accepts plain strings for backwards compat."""
    if isinstance(r, str):
        return {"email": r, "subscriber_id": "owner"}
    return {"email": r["email"], "subscriber_id": r.get("subscriber_id", "owner")}


def send_digest(
    stories: list[dict],
    subject: str | None = None,
    recipients: list | None = None,
    notion_url: str | None = None,
    alerts_text: str = "",
    feedback_endpoint: str = "",
    run_report: dict | None = None,
    candidates_url: str | None = None,
    run_log_url: str | None = None,
) -> bool:
    """
    Send one individual email per subscriber.

    recipients accepts list[str] (legacy) or list[{email, subscriber_id}].
    Each subscriber gets their own email with their subscriber_id baked into
    all feedback URLs so votes are tracked per person in the Votes database.

    Returns True if at least one email was sent successfully.
    """
    sender     = os.environ.get("SENDER_EMAIL")
    smtp_login = os.environ.get("BREVO_SMTP_LOGIN")
    smtp_key   = os.environ.get("BREVO_SMTP_KEY")

    if not sender or not smtp_login or not smtp_key:
        logger.error("SENDER_EMAIL, BREVO_SMTP_LOGIN, or BREVO_SMTP_KEY not set — skipping email")
        return False

    # Use recipients from args or env
    if not recipients:
        env_recipients = os.environ.get("DIGEST_RECIPIENT_EMAILS", "")
        recipients = [r.strip() for r in env_recipients.split(",") if r.strip()]

    if not recipients:
        logger.error("No email recipients configured")
        return False

    subscribers = [_coerce_recipient(r) for r in recipients]

    today = date.today().strftime("%A, %-d %B %Y")
    if not subject:
        subject = f"Broadsheet — {today}"

    any_sent = False
    for sub in subscribers:
        email_addr    = sub["email"]
        subscriber_id = sub["subscriber_id"]

        html_body = _build_html(
            stories=stories,
            date_str=today,
            notion_url=notion_url,
            alerts_text=alerts_text,
            feedback_endpoint=feedback_endpoint,
            run_report=run_report,
            subscriber_id=subscriber_id,
            candidates_url=candidates_url,
            run_log_url=run_log_url,
        )
        text_body = _build_plaintext(
            stories, notion_url, alerts_text,
            feedback_endpoint, run_report, subscriber_id=subscriber_id,
            candidates_url=candidates_url, run_log_url=run_log_url,
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = email_addr
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP("smtp-relay.brevo.com", 587) as server:
                server.starttls()
                server.login(smtp_login, smtp_key)
                server.sendmail(sender, [email_addr], msg.as_string())
            logger.info(f"Email sent to {email_addr} (sub={subscriber_id})")
            any_sent = True
        except Exception as e:
            logger.error(f"Email send failed for {email_addr}: {e}")

    return any_sent


# ---------------------------------------------------------------------------
# Email formatters
# ---------------------------------------------------------------------------

def _build_html(
    stories: list[dict],
    date_str: str,
    notion_url: str | None,
    alerts_text: str,
    feedback_endpoint: str = "",
    run_report: dict | None = None,
    subscriber_id: str = "owner",
    candidates_url: str | None = None,
    run_log_url: str | None = None,
) -> str:
    ep = f"{feedback_endpoint}/api/feedback" if feedback_endpoint else ""
    sub_param = f"&sub={urllib.parse.quote(subscriber_id, safe='')}" if ep else ""

    lines = [
        "<html><body style='font-family: Georgia, serif; font-size: 18px; max-width: 680px; margin: 0 auto; color: #222;'>",
    ]
    lines.append(
        f"<h1 style='font-size: 1.4em; border-bottom: 2px solid #222; padding-bottom: 8px;'>Broadsheet — {date_str}</h1>"
    )

    if run_report:
        scoring = run_report.get("scoring", "")
        summaries = run_report.get("summaries", "")
        tavily_note = run_report.get("tavily", "")
        warn = "⚠ " if "equal scoring" in scoring or "failed" in summaries or "limit reached" in tavily_note else ""
        report_line = (
            f"{warn}<strong>Run report</strong> — "
            f"Scoring: {_esc(scoring)} &nbsp;·&nbsp; "
            f"Summaries: {_esc(summaries)}"
        )
        if tavily_note:
            report_line += f" &nbsp;·&nbsp; {_esc(tavily_note)}"
        lines.append(
            f"<p style='font-size:11px; color:#999; border:1px solid #eee; border-radius:4px; "
            f"padding:6px 10px; margin-bottom:12px;'>"
            f"{report_line}"
            f"</p>"
        )

    if candidates_url or run_log_url:
        link_parts = []
        if candidates_url:
            link_parts.append(
                f"<a href='{candidates_url}' style='color:#666;'>Today's candidates →</a>"
            )
        if run_log_url:
            link_parts.append(
                f"<a href='{run_log_url}' style='color:#666;'>Today's run log →</a>"
            )
        lines.append(
            f"<p style='font-size:11px; color:#999; border:1px solid #eee; border-radius:4px; "
            f"padding:6px 10px; margin-bottom:12px;'>"
            f"{' &nbsp;·&nbsp; '.join(link_parts)} "
            f"&nbsp;<span style='color:#bbb;'>(workspace access required)</span>"
            f"</p>"
        )

    # Group stories by category
    categories = {}
    for s in stories:
        cat = s.get("category_label", s.get("category", "Other"))
        categories.setdefault(cat, []).append(s)

    for cat_label, cat_stories in categories.items():
        lines.append(f"<h2 style='font-size: 1.1em; margin-top: 2em; border-bottom: 1px solid #ccc;'>{_esc(cat_label)}</h2>")
        for s in cat_stories:
            title = s.get("title", "Untitled")
            url = s.get("url", "#")
            source = s.get("source", "")
            summary = s.get("summary", "")
            pid = s.get("notion_page_id")

            # Title link: routes through Vercel (records ⭐) then redirects to article
            if ep and pid:
                encoded_url = urllib.parse.quote(url, safe="")
                title_href = f"{ep}?id={pid}{sub_param}&v=top&url={encoded_url}"
            else:
                title_href = url

            lines.append("<div style='margin-bottom: 1.5em;'>")

            paywall_badge = " <span style='color:#999; font-size:0.8em;'>[£💸 Paywall]</span>" if s.get("paywalled") else ""

            lines.append(
                f"<p style='margin: 0 0 4px 0;'><strong><a href='{title_href}' style='color:#222; text-decoration:none;'>{_esc(title)}</a></strong>{paywall_badge}"
                f"<span style='color:#888; font-size:0.85em;'> — {_esc(source)}</span></p>"
                f"<p style='margin: 0; line-height: 1.5;'>{_esc(summary)}</p>"
            )
            lines.append("</div>")

    if alerts_text:
        lines.append(
            f"<div style='background:#fff3cd; padding:12px; border-radius:4px; margin-top:2em;'>"
            f"<pre style='white-space:pre-wrap; font-family:monospace; font-size:0.85em;'>{_esc(alerts_text)}</pre>"
            f"</div>"
        )

    lines.append("</body></html>")
    return "\n".join(lines)


def _build_plaintext(
    stories: list[dict],
    notion_url: str | None,
    alerts_text: str,
    feedback_endpoint: str = "",
    run_report: dict | None = None,
    subscriber_id: str = "owner",
    candidates_url: str | None = None,
    run_log_url: str | None = None,
) -> str:
    lines = []
    sub_param = f"&sub={urllib.parse.quote(subscriber_id, safe='')}" if feedback_endpoint else ""

    if run_report:
        report_parts = [
            f"Scoring: {run_report.get('scoring', '?')}",
            f"Summaries: {run_report.get('summaries', '?')}",
        ]
        if run_report.get("tavily"):
            report_parts.append(run_report["tavily"])
        lines.append(f"Run report — {' | '.join(report_parts)}\n")

    if candidates_url:
        lines.append(f"Today's candidates: {candidates_url}")
    if run_log_url:
        lines.append(f"Today's run log:    {run_log_url}")
    if candidates_url or run_log_url:
        lines.append("(workspace access required)\n")

    categories = {}
    for s in stories:
        cat = s.get("category_label", s.get("category", "Other"))
        categories.setdefault(cat, []).append(s)

    for cat_label, cat_stories in categories.items():
        lines.append(f"\n{'=' * 40}")
        lines.append(cat_label.upper())
        lines.append("=" * 40)
        for s in cat_stories:
            lines.append(f"\n{s.get('title', 'Untitled')} — {s.get('source', '')}")
            pid = s.get("notion_page_id")
            url = s.get("url", "")
            if feedback_endpoint and pid:
                encoded_url = urllib.parse.quote(url, safe="")
                read_url = f"{feedback_endpoint}/api/feedback?id={pid}{sub_param}&v=top&url={encoded_url}"
                lines.append(f"  Read: {read_url}")
            else:
                lines.append(url)
            lines.append(s.get("summary", ""))

    if alerts_text:
        lines.append(f"\n\n{alerts_text}")

    return "\n".join(lines)
