"""
Early-warning mails sent by the Lambdas through SES (they used to go through the BFF).

- matches: one mail per client, to all its active users, with every new alert of the run.
- sync_failed / match_failed: one mail to the super admins.

Same subject, layout and texts as the BFF alert mail (lib/mail/templates/alert.ts, lib/watchlist-alerts.ts).
Env: WATCHLIST_APP_URL (link base, e.g. https://app.nuwa.space/v2), WATCHLIST_MAIL_FROM, WATCHLIST_MAIL_REPLY_TO,
WATCHLIST_MAIL_REGION, WATCHLIST_MAIL_PROVIDER ("ses" or "log": log only, nothing is sent).
"""

from __future__ import annotations

import html
import os
from typing import Any
from urllib.parse import urlencode

from nuwa_obs_log import log_phase

SEVERITY_COLOR = {
    "critical": "#dc2626",
    "high": "#ea580c",
    "medium": "#d97706",
    "low": "#2563eb",
    "info": "#64748b",
}

_MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def app_url() -> str:
    return (os.environ.get("WATCHLIST_APP_URL") or "https://app.nuwa.space/v2").rstrip("/")


def format_list_date(ymd: str | None) -> str:
    try:
        y, m, d = (int(x) for x in str(ymd or "")[:10].split("-"))
        return f"{d} {_MONTHS_ES[m - 1]} {y}"
    except (ValueError, IndexError):
        return ""


def list_label(m: dict[str, Any]) -> str:
    if m.get("listCode") == "sat":
        return m.get("sourceName") or "SAT"
    lists = m.get("lists") or []
    return "OFAC SDN" if "SDN List" in lists else f"OFAC {lists[0] if lists else 'Consolidated'}"


def match_line(m: dict[str, Any]) -> str:
    """One line per match, as the alert detail shows it."""
    who = f"{m['subjectRoleLabel']} {m['subjectName']}" if m.get("subjectRoleLabel") else m.get("subjectName", "")
    when = f", designada el {format_list_date(m['designatedOn'])}" if m.get("designatedOn") else ""
    how = f" (por {m['matchedByLabel']}{when})" if m.get("matchedByLabel") else (f" ({when[2:]})" if when else "")
    review = " Se propone revisar." if m.get("severity") == "medium" else ""
    return f"{who} → {list_label(m)}: {m.get('recordName', '')}{how}.{review}"


def screening_path(alert: dict[str, Any]) -> str:
    """Screening form prefilled for the entity (it does not run): same as buildEntityScreeningSearchPath."""
    name = str(alert.get("entityName") or "").strip()
    party = "individual" if alert.get("partyType") == "individual" else "organization"
    params: dict[str, str] = {"type": "screening", "partyType": party}
    if alert.get("entityId"):
        params["entityId"] = str(alert["entityId"])
    if party == "organization":
        if name:
            params["orgName"] = name
    elif name:
        # MX convention: the last two words are the surnames.
        words = name.split()
        params["entity"] = name
        params["firstName"] = " ".join(words[:-2]) if len(words) > 2 else words[0]
        params["lastName"] = " ".join(words[-2:]) if len(words) > 2 else " ".join(words[1:])
    return f"/searches?{urlencode(params)}"


def digest(alerts: list[dict[str, Any]]) -> dict[str, str]:
    """Title, body and severity of the client's mail."""
    matches = sum(len(a.get("matches") or []) for a in alerts)
    severity = "high" if any(a.get("severity") == "high" for a in alerts) else "medium"
    title = (
        f"Alerta temprana: {alerts[0]['title']}"
        if len(alerts) == 1
        else f"Alerta temprana: {matches} coincidencias nuevas en listas ({len(alerts)} entidades)"
    )
    blocks = []
    for a in alerts:
        lines = [f"■ {a['entityName']}"]
        lines += [f"  • {match_line(m)}" for m in a.get("matches") or []]
        lines.append(f"  Correr screening completo: {app_url()}{screening_path(a)}")
        blocks.append("\n".join(lines))
    description = "\n".join(
        [
            "Nuwa revisó esta mañana sus entidades, socios y representantes contra la lista OFAC (actualizada anoche)"
            " y las listas del SAT de riesgo alto.",
            "",
            *blocks,
            "",
            "Las alertas están en Alertas y en la campana. Ningún screening se corrió automáticamente: usted decide"
            " si correr el screening completo.",
        ]
    )
    return {"title": title[:300], "description": description, "severity": severity}


def failure_digest(event: str, message: str) -> dict[str, str]:
    what = "la actualización diaria de OFAC" if event == "sync_failed" else "el cruce diario de alerta temprana"
    keep = (
        "La lista conserva su última versión buena."
        if event == "sync_failed"
        else "Las entidades no se revisaron hoy; el siguiente cruce revisa también lo de hoy."
    )
    return {
        "title": f"Alerta temprana: falló {what}",
        "description": f"{message[:1500]}\n\n{keep} Revíselo en Panel Admin → Fuentes de Datos → Alerta temprana.",
        "severity": "high",
    }


def build_email(title: str, description: str, severity: str, action_url: str) -> dict[str, str]:
    """Same layout as the BFF alert template; the body keeps its line breaks."""
    color = SEVERITY_COLOR.get(severity, SEVERITY_COLOR["info"])
    subject = f"[NUWA ALERTA] {title}"
    text = f"{title}\n\n{description}\n\nVer en NUWA: {action_url}"
    esc = html.escape
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#111">
  <div style="border-left:4px solid {color};padding:12px 16px;background:#f9fafb;border-radius:0 8px 8px 0;margin-bottom:20px">
    <h1 style="font-size:18px;margin:0 0 4px">{esc(title)}</h1>
  </div>
  <p style="font-size:14px;line-height:1.5;white-space:pre-line">{esc(description)}</p>
  <p style="margin-top:24px">
    <a href="{esc(action_url)}" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;padding:10px 18px;border-radius:6px;font-size:14px">Abrir en NUWA</a>
  </p>
</body></html>"""
    return {"subject": subject, "html": body, "text": text}


_ses = None


def _ses_client():
    global _ses
    if _ses is None:
        import boto3  # lazy: local tests run without it

        _ses = boto3.client("ses", region_name=os.environ.get("WATCHLIST_MAIL_REGION") or "us-east-1")
    return _ses


def send(recipients: list[str], mail: dict[str, str]) -> bool:
    """One message to every recipient (To:, like the BFF did). False on error: the caller records it."""
    if not recipients:
        return False
    if (os.environ.get("WATCHLIST_MAIL_PROVIDER") or "ses").lower() == "log":
        log_phase("watchlist_mail", f"log only -> {len(recipients)}: {mail['subject']}")
        return True
    reply_to = os.environ.get("WATCHLIST_MAIL_REPLY_TO") or "soporte@nuwa.space"
    try:
        _ses_client().send_email(
            Source=os.environ.get("WATCHLIST_MAIL_FROM") or "alertas@nuwa.space",
            Destination={"ToAddresses": recipients},
            ReplyToAddresses=[reply_to],
            Message={
                "Subject": {"Data": mail["subject"], "Charset": "UTF-8"},
                "Body": {"Html": {"Data": mail["html"], "Charset": "UTF-8"}, "Text": {"Data": mail["text"], "Charset": "UTF-8"}},
            },
        )
        return True
    except Exception as e:  # noqa: BLE001
        log_phase("watchlist_mail", f"send failed: {e}")
        return False


def mail_matches(recipients: list[str], alerts: list[dict[str, Any]]) -> bool:
    d = digest(alerts)
    return send(recipients, build_email(d["title"], d["description"], d["severity"], f"{app_url()}/alerts"))


def mail_failure(event: str, message: str, recipients: list[str]) -> bool:
    d = failure_digest(event, message)
    return send(recipients, build_email(d["title"], d["description"], d["severity"], f"{app_url()}/admin"))
