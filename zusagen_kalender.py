#!/usr/bin/env python3
"""
Baut aus Teamsystems (SV Bergstedt) einen Kalender, der NUR die Termine
enthält, bei denen du auf "ja" (grün) stehst.

Ablauf:
  1. Mit deinem Teamsystems-Login einloggen
  2. Seite "Zusagen/Absagen" lesen -> welche Termine sind grün?
  3. Deinen persönlichen iCal-Feed von Teamsystems laden (Uhrzeiten, Orte)
  4. Nur die grünen Termine in eine neue .ics-Datei schreiben

Benötigte Umgebungsvariablen:
  TS_USER   – Teamsystems-Benutzername
  TS_PASS   – Teamsystems-Passwort
  TS_BASE   – optional, Standard: https://svbergstedt.teamsystems.de

Aufruf:
  python zusagen_kalender.py ausgabe.ics

Aus Datenschutzgründen gibt das Skript nur Zahlen aus, keine Terminnamen
(die Logs von GitHub Actions sind bei öffentlichen Repos öffentlich).
"""

import os
import re
import sys

import requests
from bs4 import BeautifulSoup

BASE = os.environ.get("TS_BASE", "https://svbergstedt.teamsystems.de").rstrip("/")
CAL_NAME = "Hockey – Zusagen"
USER_AGENT = "Mozilla/5.0 (Zusagen-Kalender; private use)"


class KalenderFehler(Exception):
    pass


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def norm(text: str) -> str:
    """Leerzeichen vereinheitlichen."""
    return re.sub(r"\s+", " ", text or "").strip()


def ics_unescape(text: str) -> str:
    return (text.replace("\\n", " ").replace("\\N", " ")
                .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def unfold(block: str) -> list[str]:
    """iCal-Zeilen 'entfalten' (Fortsetzungszeilen beginnen mit Leerzeichen/Tab)."""
    lines: list[str] = []
    for line in re.split(r"\r?\n", block):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def prop(lines: list[str], key: str) -> str:
    for line in lines:
        name = re.split(r"[;:]", line, maxsplit=1)[0]
        if name == key and ":" in line:
            return line.split(":", 1)[1]
    return ""


# ---------------------------------------------------------------------------
# Teamsystems
# ---------------------------------------------------------------------------

def login(session: requests.Session, user: str, password: str) -> None:
    r = session.get(f"{BASE}/login", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", attrs={"name": "_csrf_token"})
    data = {
        "_username": user,
        "_password": password,
        "_remember_me": "on",
    }
    if token_input and token_input.get("value"):
        data["_csrf_token"] = token_input["value"]
    r = session.post(f"{BASE}/login_check", data=data, timeout=30)
    r.raise_for_status()
    if "/login" in r.url and "Ausloggen" not in r.text:
        raise KalenderFehler("Login fehlgeschlagen – Benutzername/Passwort prüfen.")


def accepted_invitations(html: str) -> tuple[set[tuple[str, str]], int]:
    """Gibt (Menge von (JJJJMMTT, Name) mit Zusage 'ja', Anzahl aller Einladungen) zurück."""
    soup = BeautifulSoup(html, "html.parser")
    invitations = soup.select("div.invitation")
    accepted: set[tuple[str, str]] = set()
    for inv in invitations:
        classes = inv.get("class", [])
        if "invitation_state_accept" not in classes:
            continue
        dt_el = inv.select_one(".datetime")
        name_el = inv.select_one(".event_name")
        if not dt_el or not name_el:
            continue
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", dt_el.get_text(" "))
        if not m:
            continue
        day, month, year = m.groups()
        accepted.add((f"{year}{month}{day}", norm(name_el.get_text(" "))))
    return accepted, len(invitations)


def find_ical_url(html: str) -> str:
    m = re.search(r"https?://[^\s\"'<>]*icalendar/person/export/[^\s\"'<>]*", html)
    if not m:
        raise KalenderFehler("Persönliche iCal-URL nicht gefunden.")
    url = m.group(0)
    return url.replace("http://", "https://", 1)


# ---------------------------------------------------------------------------
# Kalender bauen
# ---------------------------------------------------------------------------

def build_calendar(ics_text: str, accepted: set[tuple[str, str]]) -> tuple[str, int]:
    parts = ics_text.split("BEGIN:VEVENT")
    header = parts[0]
    blocks = parts[1:]

    # Kopfzeilen übernehmen (inkl. Zeitzone), eigenen Kalendernamen setzen
    header_lines = [l for l in re.split(r"\r?\n", header) if l.strip()]
    header_lines = [l for l in header_lines
                    if not l.startswith(("X-WR-CALNAME", "REFRESH-INTERVAL", "X-PUBLISHED-TTL"))]
    insert_at = 1  # direkt nach BEGIN:VCALENDAR
    header_lines[insert_at:insert_at] = [
        f"X-WR-CALNAME:{CAL_NAME}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]

    kept: list[str] = []
    matched: set[tuple[str, str]] = set()
    for block in blocks:
        body = block.split("END:VEVENT")[0]
        lines = unfold(body)
        start = prop(lines, "DTSTART")
        summary = ics_unescape(prop(lines, "SUMMARY"))
        name = norm(re.sub(r"^\([^)]*\)\s*", "", summary))  # "(H) " o.ä. entfernen
        key = (start[:8], name)
        if key in accepted:
            kept.append("BEGIN:VEVENT" + body.rstrip("\r\n") + "\r\nEND:VEVENT")
            matched.add(key)

    out = "\r\n".join(header_lines) + "\r\n"
    for ev in kept:
        out += ev.strip("\r\n") + "\r\n"
    out += "END:VCALENDAR\r\n"
    return out, len(accepted - matched)


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) != 2:
        print("Aufruf: python zusagen_kalender.py ausgabe.ics", file=sys.stderr)
        return 2
    out_path = sys.argv[1]
    user = os.environ.get("TS_USER")
    password = os.environ.get("TS_PASS")
    if not user or not password:
        print("TS_USER und TS_PASS müssen gesetzt sein.", file=sys.stderr)
        return 2

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    try:
        login(s, user, password)

        r = s.get(f"{BASE}/zusagen-absagen", timeout=30)
        r.raise_for_status()
        accepted, total = accepted_invitations(r.text)
        if total == 0:
            # Lieber nichts veröffentlichen als einen leeren Kalender
            raise KalenderFehler("Keine Einladungen gefunden – Seite evtl. geändert.")

        r = s.get(f"{BASE}/meine-termine", timeout=30)
        r.raise_for_status()
        ical_url = find_ical_url(r.text)

        r = s.get(ical_url, timeout=60)
        r.raise_for_status()
        if "BEGIN:VCALENDAR" not in r.text:
            raise KalenderFehler("iCal-Feed sieht ungültig aus.")

        calendar, unmatched = build_calendar(r.text, accepted)
    except (KalenderFehler, requests.RequestException) as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(calendar)

    print(f"Einladungen: {total}, davon zugesagt: {len(accepted)}, "
          f"im Kalender: {len(accepted) - unmatched}, nicht zuordenbar: {unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
