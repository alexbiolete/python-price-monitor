"""The digest email. Its look copies codename-dojo's notification emails
(apps/backend/src/common/email.ts: renderEmail) so both read as one system:
same palette, wordmark, card, kicker/title/intro, pill buttons and footer."""

import hashlib
import html
import socket
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger("price_monitor")

# Same tones as the dashboard's emails (light only: mail apps differ too much on dark)
ACCENT = "#4f7cf0"
INK = "#18181b"
MUTED = "#71717a"
LINE = "#e4e4e7"
SOFT = "#f4f4f5"
GOOD = "#15803d"
GOOD_BG = "#dcfce7"  # the good signs' box: light green, its text GOOD
# The risk levels: yellow, orange, red (background, text, label)
RISK_STYLE = {
    "low": ("#fef9c3", "#854d0e", "Slight risk"),
    "medium": ("#ffedd5", "#9a3412", "Possible scam"),
    "high": ("#fee2e2", "#991b1b", "Likely scam"),
}
SHOWN_RISK = tuple(RISK_STYLE)
NOTE_INK = "#3f3f46"  # the "notes" box: grey, darker than MUTED to read on SOFT
# After the hidden preview text: blank characters, so inbox previews stop there instead of running
# on into the email's first visible words (the title, "PRICE MONITOR")
PREVIEW_STOP = "&#847;&zwnj;&nbsp;" * 90
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
    # Always a photo cell (a grey square without one): rows with and without photos share one table, so a
    # missing cell would push the title into the narrow photo column of the rows around it
    photo = (f'<img src="{esc(item["image_url"])}" width="48" height="48" alt="" '
             f'style="display:block;width:48px;height:48px;border-radius:8px;object-fit:cover;background:{SOFT}">'
             if item.get("image_url") else
             f'<div style="width:48px;height:48px;border-radius:8px;background:{SOFT}"></div>')
    thumb = f'<td style="padding:10px 10px 10px 0;border-top:1px solid {LINE};width:48px;vertical-align:top">{photo}</td>'
    meta = " · ".join(x for x in (item.get("location"), item.get("platform_label")) if x)
    return (
        "<tr>" + thumb +
        f'<td style="padding:10px 0;border-top:1px solid {LINE};vertical-align:top">'
        f'<a href="{esc(item["url"])}" style="color:{INK};font-size:14px;font-weight:600;line-height:1.4;text-decoration:none">{esc(item["title"])}</a>'
        + (f'<div style="margin-top:2px;color:{MUTED};font-size:12px">{esc(meta)}</div>' if meta else "")
        + _notes_html(item) + _good_html(item) + _risk_html(item)
        + f'<div style="margin-top:4px"><a href="{esc(item["url"])}" style="color:{ACCENT};font-size:12px;font-weight:600;text-decoration:none">View listing →</a></div>'
        "</td>"
        f'<td align="right" style="padding:10px 0 10px 10px;border-top:1px solid {LINE};vertical-align:top;white-space:nowrap;font-size:14px;color:{INK}">{price_html}</td>'
        "</tr>"
    )


def _capitalized(text):
    return text[:1].upper() + text[1:]


def _box(heading, items, background, ink):
    """A rounded box: its heading on one line, then one bullet per item (as the dashboard's view)"""
    bullets = "".join(f'<li style="margin:2px 0">{esc(_capitalized(x))}</li>' for x in items)
    return (f'<div style="margin-top:6px;padding:8px 12px;border-radius:10px;background:{background};color:{ink};font-size:12px;line-height:1.45">'
            f'<div style="font-weight:700">{heading}</div>'
            f'<ul style="margin:4px 0 0;padding-left:18px">{bullets}</ul></div>')


def _risk_html(item):
    """The warning signs (each with points), under the notes: the level's colour, its name, the signs"""
    level, reasons = item.get("risk_level"), item.get("risk_reasons") or []
    if level not in SHOWN_RISK or not reasons:
        return ""
    bg, ink, label = RISK_STYLE[level]
    return _box(f"⚠ {esc(label)}", reasons, bg, ink)


def _good_html(item):
    """Good signs (an old seller account, a well-rated seller, buyer protection, an offer to meet), in a
    green box between the notes and the warning signs (the dashboard's "Good signs")"""
    good = item.get("risk_good") or []
    return _box("✓ Good signs", good, GOOD_BG, GOOD) if good else ""


def _notes_html(item):
    """Notes: worth knowing, no points (no reviews yet, a messaging app, the same photo on other ads), in a
    grey box before the warning signs (the dashboard's "Notes")"""
    notes = item.get("risk_notes") or []
    return _box("ⓘ Notes", notes, SOFT, NOTE_INK) if notes else ""


def _detail_html(item):
    """How the price adds up (eBay's total), small under it"""
    if not item.get("price_detail"):
        return ""
    return (f'<div style="margin-top:2px;color:{MUTED};font-size:11px;font-weight:400;white-space:normal;'
            f'max-width:180px;line-height:1.35">{esc(item["price_detail"])}</div>')


def _risk_text(item):
    text = ""
    if item.get("risk_notes"):
        text += f"\n      ⓘ Notes: {'; '.join(item['risk_notes'])}"
    if item.get("risk_good"):
        text += f"\n      ✓ Good signs: {'; '.join(item['risk_good'])}"
    if item.get("risk_level") in SHOWN_RISK and item.get("risk_reasons"):
        text += f"\n      ⚠ {RISK_STYLE[item['risk_level']][2]}: {'; '.join(item['risk_reasons'])}"
    return text


def _section(title, rows):
    return (f'<div style="margin-top:14px;color:{MUTED};font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase">{esc(title)}</div>'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:4px;border-bottom:1px solid {LINE}">{"".join(rows)}</table>')


def by_product(reports, merge=False):
    """The emails to send: [(product, reports)], one per product (the search's name: one search per
    product per platform), in their order; merged: one for all, product None"""
    if merge:
        return [(None, reports)] if reports else []
    groups = {}
    for r in reports:
        groups.setdefault(r["search"]["name"], []).append(r)
    return list(groups.items())


def render_digest(reports, dashboard_url=None, product=None):
    """reports: [{"search": {...}, "new": [...], "drops": [...]}], one product's, or all (product None)
    -> (subject, html, text)"""
    n_new = sum(len(r["new"]) for r in reports)
    n_drops = sum(len(r["drops"]) for r in reports)
    title = summary(n_new, n_drops)
    title = title[0].upper() + title[1:]
    if product:
        platforms = list(dict.fromkeys(r["search"]["platform_label"] for r in reports))
        intro = "On " + ", ".join(platforms)
    else:
        products = list(dict.fromkeys(r["search"]["name"] for r in reports))
        intro = ("For " + ", ".join(products)) if len(products) <= 3 else f"For {len(products)} products"
    flagged = sum(1 for r in reports for x in r["new"] + r["drops"] if x.get("risk_level") in SHOWN_RISK)
    if flagged:
        intro += f" · {flagged} with warning signs"
    # A small label above the heading ("PRICE MONITOR"); empty: none
    kicker = setting("EMAIL_KICKER", "Price monitor")
    # Which copy sent it (ENVIRONMENT, e.g. production on the server, local on a laptop; shown in
    # capitals like the label), next to
    # the label; unset: the machine's name
    environment = setting("ENVIRONMENT", socket.gethostname().split(".")[0])
    footer = setting("EMAIL_FOOTER", "A notification from your price monitor.")
    # The same subject every time for a product, so mail apps keep its emails in one conversation (by
    # the subject, and the reply headers send_email adds for it); what's new leads the inbox preview
    subject = (setting("EMAIL_SUBJECT", "New listing updates for {product}").replace("{product}", product) if product
               else setting("EMAIL_SUBJECT_ALL", "New listing updates"))
    preview = f"{title} · {intro}"

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
                    f'<span style="color:{GOOD};font-size:12px;font-weight:600">−{d["drop_pct"]:.0f}%</span>' + _detail_html(d)))
                text.append(f"  ↓ {d['title']}: {fmt_price(d['old_price'], d['currency'])} -> "
                            f"{fmt_price(d['price'], d['currency'])} (-{d['drop_pct']:.0f}%) {d['url']}" + _risk_text(d))
            body.append(_section("Price drops", rows))
        if r["new"]:
            rows = [_listing_row(n, f'<b>{fmt_price(n["price"], n["currency"])}</b>' + _detail_html(n))
                    for n in sorted(r["new"], key=lambda n: n["price_ron"])]
            text += [f"  + {n['title']}: {fmt_price(n['price'], n['currency'])}"
                     + (f" ({n['price_detail']})" if n.get("price_detail") else "") + f" {n['url']}" + _risk_text(n)
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
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">{esc(preview)}{PREVIEW_STOP}</div>
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
    msg["Date"] = formatdate(localtime=True)
    # One conversation per subject: mail apps that group by the reply headers (Proton Mail) rather
    # than the subject see every digest as a reply to the same first message (which needn't exist)
    domain = (from_addr or user or "localhost").rpartition("@")[2] or "localhost"
    thread = f"<{hashlib.sha1(subject.encode()).hexdigest()[:16]}.price-monitor@{domain}>"
    msg["Message-ID"] = make_msgid("price-monitor", domain=domain)
    msg["In-Reply-To"] = thread
    msg["References"] = thread
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [a.strip() for a in to.split(",") if a.strip()]
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, password)
        smtp.sendmail(from_addr or user, recipients, msg.as_string())
    log.info("Email sent to %s: %s", to, subject)
    return True
