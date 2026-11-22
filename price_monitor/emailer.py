"""The digest email. Its look copies codename-dojo's notification emails
(apps/backend/src/common/email.ts: renderEmail) so both read as one system:
same palette, wordmark, card, kicker/title/intro, pill buttons and footer."""

import html
import socket
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger("price_monitor")

# Same tones as the dashboard's emails (light only: mail apps differ too much on dark)
ACCENT = "#4f7cf0"
INK = "#18181b"
MUTED = "#71717a"
LINE = "#e4e4e7"
SOFT = "#f4f4f5"
GOOD = "#15803d"
WARN_BG = "#fef3c7"
WARN_INK = "#92400e"
DANGER_BG = "#fee2e2"
DANGER_INK = "#991b1b"
SHOWN_RISK = ("medium", "high")
# After the hidden preview text: blank characters, so inbox previews stop there instead of running
# on into the email's first visible words (the title, "PRICE MONITOR")
PREVIEW_STOP = "&#847;&zwnj;&nbsp;" * 90  # "low" is stored and shown in the dashboard, not emailed
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_price(price, currency):
    amount = f"{price:,.0f}".replace(",", ".")
    return {"RON": f"{amount} lei", "EUR": f"{amount} €"}.get(currency, f"{amount} {currency}")


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def summary(n_new, n_drops):
    parts = []
    if n_new:
        parts.append(_plural(n_new, "new listing", "new listings"))
    if n_drops:
        parts.append(_plural(n_drops, "price drop", "price drops"))
    return ", ".join(parts)


def setting(key, default):
    return (os.getenv(key) or "").strip() or default


def wordmark():
    """The title at the top of the email (EMAIL_TITLE), like the dashboard's wordmark."""
    return setting("EMAIL_TITLE", "Price monitor")


def _listing_row(item, price_html):
    thumb = ""
    if item.get("image_url"):
        thumb = (f'<td style="padding:10px 10px 10px 0;border-top:1px solid {LINE};width:48px;vertical-align:top">'
                 f'<img src="{esc(item["image_url"])}" width="48" height="48" alt="" '
                 f'style="display:block;width:48px;height:48px;border-radius:8px;object-fit:cover;background:{SOFT}"></td>')
    meta = " · ".join(x for x in (item.get("location"), item.get("platform_label")) if x)
    return (
        "<tr>" + thumb +
        f'<td style="padding:10px 0;border-top:1px solid {LINE};vertical-align:top">'
        f'<a href="{esc(item["url"])}" style="color:{INK};font-size:14px;font-weight:600;line-height:1.4;text-decoration:none">{esc(item["title"])}</a>'
        + (f'<div style="margin-top:2px;color:{MUTED};font-size:12px">{esc(meta)}</div>' if meta else "")
        + _risk_html(item)
        + f'<div style="margin-top:4px"><a href="{esc(item["url"])}" style="color:{ACCENT};font-size:12px;font-weight:600;text-decoration:none">View listing →</a></div>'
        "</td>"
        f'<td align="right" style="padding:10px 0 10px 10px;border-top:1px solid {LINE};vertical-align:top;white-space:nowrap;font-size:14px;color:{INK}">{price_html}</td>'
        "</tr>"
    )


def _risk_html(item):
    level, reasons = item.get("risk_level"), item.get("risk_reasons") or []
    if level not in SHOWN_RISK or not reasons:
        return ""
    bg, ink = (DANGER_BG, DANGER_INK) if level == "high" else (WARN_BG, WARN_INK)
    label = "Likely scam" if level == "high" else "Possible scam"
    return (f'<div style="margin-top:6px;padding:6px 10px;border-radius:10px;background:{bg};color:{ink};font-size:12px;line-height:1.45">'
            f'<b>⚠ {label}:</b> {esc("; ".join(reasons))}</div>')


def _risk_text(item):
    if item.get("risk_level") not in SHOWN_RISK or not item.get("risk_reasons"):
        return ""
    return f"\n      ⚠ {'Likely' if item['risk_level'] == 'high' else 'Possible'} scam: {'; '.join(item['risk_reasons'])}"


def _section(title, rows):
    return (f'<div style="margin-top:14px;color:{MUTED};font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase">{esc(title)}</div>'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:4px;border-bottom:1px solid {LINE}">{"".join(rows)}</table>')


def render_digest(reports, dashboard_url=None):
    """reports: [{"search": {...}, "new": [...], "drops": [...]}] -> (subject, html, text)"""
    n_new = sum(len(r["new"]) for r in reports)
    n_drops = sum(len(r["drops"]) for r in reports)
    names = [r["search"]["name"] for r in reports]
    title = summary(n_new, n_drops)
    title = title[0].upper() + title[1:]
    intro = ("From " + ", ".join(names)) if len(names) <= 3 else f"From {len(names)} searches"
    flagged = sum(1 for r in reports for x in r["new"] + r["drops"] if x.get("risk_level") in SHOWN_RISK)
    if flagged:
        intro += f" · {flagged} flagged as {'a possible scam' if flagged == 1 else 'possible scams'}"
    # A small label above the heading ("PRICE MONITOR"); empty: none. And an optional start for the
    # subject ("Deals: 3 new…")
    kicker = setting("EMAIL_KICKER", "Price monitor")
    # Which copy sent it (ENVIRONMENT, e.g. production on the server, local on a laptop; shown in
    # capitals like the label), next to
    # the label; unset: the machine's name
    environment = setting("ENVIRONMENT", socket.gethostname().split(".")[0])
    prefix = setting("EMAIL_SUBJECT_PREFIX", "")
    footer = setting("EMAIL_FOOTER", "A notification from your price monitor.")
    subject = f"{prefix}: {summary(n_new, n_drops)}" if prefix else title

    body, text = [], [title, "", intro]
    for r in reports:
        s = r["search"]
        body.append(f'<h2 style="margin:24px 0 0;color:{INK};font-size:17px;line-height:1.3;font-weight:700">{esc(s["name"])}'
                    f'<span style="color:{MUTED};font-size:13px;font-weight:500"> · {esc(s["platform_label"])}</span></h2>')
        text += ["", f"== {s['name']} ({s['platform_label']})"]
        if r["drops"]:
            rows = []
            for d in sorted(r["drops"], key=lambda d: d["price_ron"]):
                rows.append(_listing_row(d,
                    f'<span style="color:{MUTED};text-decoration:line-through;font-size:12px">{fmt_price(d["old_price"], d["currency"])}</span><br>'
                    f'<b>{fmt_price(d["price"], d["currency"])}</b><br>'
                    f'<span style="color:{GOOD};font-size:12px;font-weight:600">−{d["drop_pct"]:.0f}%</span>'))
                text.append(f"  ↓ {d['title']}: {fmt_price(d['old_price'], d['currency'])} -> "
                            f"{fmt_price(d['price'], d['currency'])} (-{d['drop_pct']:.0f}%) {d['url']}" + _risk_text(d))
            body.append(_section("Price drops", rows))
        if r["new"]:
            rows = [_listing_row(n, f'<b>{fmt_price(n["price"], n["currency"])}</b>')
                    for n in sorted(r["new"], key=lambda n: n["price_ron"])]
            text += [f"  + {n['title']}: {fmt_price(n['price'], n['currency'])} {n['url']}" + _risk_text(n)
                     for n in sorted(r["new"], key=lambda n: n["price_ron"])]
            body.append(_section("New listings", rows))

    button = ""
    if dashboard_url:
        button = (f'<div style="margin-top:20px"><a href="{esc(dashboard_url)}" style="display:inline-block;margin:0 8px 8px 0;'
                  f'padding:9px 18px;border-radius:999px;background:{ACCENT};color:#ffffff;font-size:14px;font-weight:600;'
                  f'text-decoration:none">Open in dashboard</a></div>')
        text += ["", f"Open in dashboard: {dashboard_url}"]
    note = "You get this when a search finds a new listing, or a price drops by the threshold set for the monitor."
    text += ["", note, "", f"{footer} ({' · '.join(x for x in (wordmark(), kicker, environment) if x)})"]

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>{esc(title)}</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;font-family:{FONT};-webkit-font-smoothing:antialiased">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">{esc(intro)}{PREVIEW_STOP}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff">
  <tr><td align="center" style="padding:16px 12px">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px">
      <tr><td align="center" style="padding:4px 0 18px;border-bottom:1px solid {LINE};color:{INK};font-size:28px;line-height:1.2;font-weight:800;letter-spacing:-0.02em">{esc(wordmark())}</td></tr>
      <tr><td style="padding:16px 0 0">
        <div style="margin:0 0 6px;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase">{f'<span style="color:{ACCENT}">{esc(kicker)}</span>' if kicker else ''}{' · ' if kicker and environment else ''}{f'<span style="color:{MUTED}">{esc(environment)}</span>' if environment else ''}</div>
        <h1 style="margin:0;color:{INK};font-size:20px;line-height:1.3;font-weight:700">{esc(title)}</h1>
        <p style="margin:4px 0 0;color:{MUTED};font-size:14px;line-height:1.5">{esc(intro)}</p>
        {"".join(body)}
        {button}
        <p style="margin:16px 0 0;color:{MUTED};font-size:12px;line-height:1.5">{esc(note)}</p>
      </td></tr>
      <tr><td style="padding:16px 0 0;color:#a1a1aa;font-size:12px;line-height:1.5">{esc(footer)}</td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
    return subject, page, "\n".join(text)


def send_email(subject, html_body, text_body, to):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT") or 587)
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    if not all([host, user, password, to]):
        log.warning("SMTP settings or recipient missing; printing the email instead")
        print(f"Subject: {subject}\nTo: {to}\n\n{text_body}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    from_name = setting("EMAIL_FROM_NAME", wordmark())
    from_addr = setting("EMAIL_FROM_ADDRESS", user)
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [a.strip() for a in to.split(",") if a.strip()]
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, password)
        smtp.sendmail(from_addr or user, recipients, msg.as_string())
    log.info("Email sent to %s: %s", to, subject)
    return True
