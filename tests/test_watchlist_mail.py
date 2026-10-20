"""Early-warning mails built by the Lambdas (watchlist_mail): same texts as the BFF had."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

import watchlist_mail as mail  # noqa: E402

GREEN = {
    "alertId": "a-1",
    "clientId": 8,
    "entityId": "e-green",
    "entityName": "Green Agropacific",
    "partyType": "organization",
    "severity": "high",
    "title": "2 coincidencias nuevas de Green Agropacific en OFAC SDN",
    "matches": [
        {
            "listCode": "ofac",
            "lists": ["SDN List"],
            "recordName": "Bubux Baby Shoes S.A. de C.V.",
            "subjectName": "Bubux Baby Shoes S.A. de C.V.",
            "subjectRole": "business_partner",
            "subjectRoleLabel": "Socio comercial",
            "matchedByLabel": "nombre",
            "severity": "high",
            "designatedOn": "2026-07-23",
        },
        {
            "listCode": "sat",
            "sourceName": "SAT 69-B Definitivos",
            "recordName": "CRA CONSULTORIA DEL MAYA",
            "subjectName": "CRA Consultoría del Maya",
            "matchedByLabel": "nombre con una letra de diferencia",
            "severity": "medium",
        },
    ],
}


def test_match_line_and_dates():
    assert mail.format_list_date("2026-07-23") == "23 jul 2026" and mail.format_list_date(None) == ""
    assert mail.match_line(GREEN["matches"][0]) == (
        "Socio comercial Bubux Baby Shoes S.A. de C.V. → OFAC SDN: Bubux Baby Shoes S.A. de C.V."
        " (por nombre, designada el 23 jul 2026)."
    )
    assert mail.match_line(GREEN["matches"][1]).endswith("(por nombre con una letra de diferencia). Se propone revisar.")


def test_digest_one_mail_with_every_alert_and_the_screening_link(monkeypatch):
    monkeypatch.setenv("WATCHLIST_APP_URL", "https://dev.nuwa.space/v2/")
    other = {**GREEN, "alertId": "a-2", "entityId": "e-2", "entityName": "Agropecuaria Amateq", "severity": "medium"}
    d = mail.digest([GREEN, other])
    assert d["title"] == "Alerta temprana: 4 coincidencias nuevas en listas (2 entidades)" and d["severity"] == "high"
    assert "■ Green Agropacific" in d["description"] and "■ Agropecuaria Amateq" in d["description"]
    assert "Ningún screening se corrió automáticamente" in d["description"]
    link = next(l for l in d["description"].splitlines() if "Correr screening completo" in l).split(": ", 1)[1]
    q = parse_qs(urlparse(link).query)
    assert link.startswith("https://dev.nuwa.space/v2/searches?")
    assert q == {"type": ["screening"], "partyType": ["organization"], "entityId": ["e-green"], "orgName": ["Green Agropacific"]}
    assert mail.digest([GREEN])["title"] == f"Alerta temprana: {GREEN['title']}"


def test_person_link_splits_the_surnames():
    q = parse_qs(urlparse(mail.screening_path({"entityName": "Liliana Cisneros Tapia", "partyType": "individual"})).query)
    assert (q["firstName"], q["lastName"], q["entity"]) == (["Liliana"], ["Cisneros Tapia"], ["Liliana Cisneros Tapia"])


def test_email_escapes_and_keeps_line_breaks():
    e = mail.build_email("Alerta temprana: <x>", "a\nb & c", "high", "https://app.nuwa.space/v2/alerts")
    assert e["subject"] == "[NUWA ALERTA] Alerta temprana: <x>"
    assert "&lt;x&gt;" in e["html"] and "a\nb &amp; c" in e["html"] and "white-space:pre-line" in e["html"]
    assert e["text"].endswith("Ver en NUWA: https://app.nuwa.space/v2/alerts")


def test_send_uses_ses_with_every_recipient(monkeypatch):
    calls = []

    class FakeSes:
        def send_email(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(mail, "_ses_client", lambda: FakeSes())
    monkeypatch.delenv("WATCHLIST_MAIL_PROVIDER", raising=False)
    assert mail.mail_matches(["qa@nuwa.space", "dgoletto@sadahcontrol.com.mx"], [GREEN])
    kw = calls[0]
    assert kw["Source"] == "alertas@nuwa.space" and kw["ReplyToAddresses"] == ["soporte@nuwa.space"]
    assert kw["Destination"] == {"ToAddresses": ["qa@nuwa.space", "dgoletto@sadahcontrol.com.mx"]}
    assert kw["Message"]["Subject"]["Data"].startswith("[NUWA ALERTA] Alerta temprana: ")
    assert not mail.send([], {"subject": "", "html": "", "text": ""})


def test_failure_mail_and_ses_error(monkeypatch):
    d = mail.failure_digest("sync_failed", "HTTP Error 503")
    assert d["title"] == "Alerta temprana: falló la actualización diaria de OFAC"
    assert "La lista conserva su última versión buena." in d["description"]

    class Broken:
        def send_email(self, **kw):
            raise RuntimeError("MessageRejected")

    monkeypatch.setattr(mail, "_ses_client", lambda: Broken())
    assert mail.mail_failure("match_failed", "boom", ["admin@nuwa.space"]) is False
