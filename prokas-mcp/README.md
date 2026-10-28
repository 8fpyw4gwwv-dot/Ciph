# PROKAS-MCP – Claude-Connector für PROKAS7 Berichtswesen

Lokaler MCP-Connector für die **easyApotheke im Nedderfeld Center**. Claude kann damit Berichtswesen-Listen (Z-Listen) aus PROKAS7 lesen, auswerten und Vorschläge (Preisdateien, Bestelllisten) vorbereiten, auch wenn Sie die Frage vom iPhone oder von zu Hause stellen.

**Grundsätze**

- **Nur lesend.** Keine DB2-Verbindung, kein Schreiben in PROKAS-Ordner. Es wird nie etwas gebucht, bepreist, bestellt oder gelöscht.
- **Daten kommen nur aus Z-Listen-Exporten.** PROKAS hat keine offizielle Schnittstelle.
- **Änderungen nur als Vorschlagsdatei** in der Outbox, jeweils mit lesbarer Vorschau (`.md`). Importieren tut immer ein Mensch.
- **Datenschutz (DSGVO, § 203 StGB):** Personenbezogene Spalten (Name, Kunde, Geburtsdatum, Adresse, Telefon, E-Mail, Versichertennummer, Arzt, Rezept-ID, Patient …) werden entfernt, **bevor** Claude etwas sieht. Im Protokoll stehen nur Spaltennamen, nie Inhalte.

---

## Inhalt

1. [So funktioniert es](#1-so-funktioniert-es)
2. [Installation](#2-installation)
3. [Ordner und Berechtigungen](#3-ordner-und-berechtigungen)
4. [Erster Start: Beispieldateien prüfen](#4-erster-start-beispieldateien-prüfen)
5. [Nachtset im Berichtswesen einrichten](#5-nachtset-im-berichtswesen-einrichten)
6. [Benutzung vom iPhone](#6-benutzung-vom-iphone)
7. [Werkzeuge](#7-werkzeuge)
8. [Freigegebene Listen](#8-freigegebene-listen)
9. [Sperrzeiten](#9-sperrzeiten)
10. [cli_status.json lesen](#10-cli_statusjson-lesen)
11. [Protokolle](#11-protokolle)
12. [Fehlerbehebung](#12-fehlerbehebung)
13. [Tests](#13-tests)
14. [Grenzen](#14-grenzen)

---

## 1. So funktioniert es

Wenn Claude eine Liste braucht (z. B. „Ladenhüter“ = Z0127), versucht der Connector drei Wege, in dieser Reihenfolge:

| Weg | Wann | Was passiert |
| --- | --- | --- |
| **Datei** (`mode=file`) | Es liegt schon ein frischer Export im Exportordner (jünger als 36 h, gleiche Liste und gleicher Zeitraum) | Die Datei wird sofort gelesen. PROKAS wird nicht angefasst. |
| **Kommandozeile** (`mode=cli`) | `probe_cli` hat einen Weg gefunden, Listen ohne Bildschirm zu erzeugen | Die Liste wird im Hintergrund erzeugt und gelesen. |
| **Bildschirmsteuerung** (`needs_computer_use`) | Sonst, oder wenn die Kommandozeile scheitert (im selben Aufruf) | Der Connector legt einen Auftrag an und liefert Claude eine Schritt-für-Schritt-Anleitung (Runbook). Claude bedient Berichtswesen am Bildschirm, speichert die Liste als Excel und ruft `ingest_export` auf. |

Es läuft immer nur **ein** Erzeugungsauftrag gleichzeitig. Weitere warten in einer Warteschlange.

Bei PROKAS ist keine Kommandozeile dokumentiert. Rechnen Sie also mit **Datei** (Nachtset) und **Bildschirmsteuerung**. Das ist der Normalfall.

---

## 2. Installation

Voraussetzungen: Windows-PC mit PROKAS7, Claude Desktop, Internet für die einmalige Installation.

1. Diesen Ordner `prokas-mcp` an einen festen Ort kopieren, z. B. `C:\Claude\prokas-mcp`.
   **Nicht** in einen PROKAS-Ordner (`P:\BW`, `P:\PROKAS` …). Der Connector verweigert das.
2. Doppelklick auf **`setup_prokas_mcp.bat`**.
   - Installiert bei Bedarf [uv](https://docs.astral.sh/uv/) und Python 3.12,
   - richtet die Pakete ein (MCP-SDK, pandas, openpyxl, xlrd),
   - legt die Ordner an und führt den Selbsttest aus,
   - zeigt am Ende den Eintrag für Claude Desktop an.
3. In **Claude Desktop**: *Einstellungen → Entwickler → Konfiguration bearbeiten*. Das öffnet `%APPDATA%\Claude\claude_desktop_config.json`. Den angezeigten Eintrag übernehmen, zum Beispiel:

   ```json
   {
     "mcpServers": {
       "prokas": {
         "command": "C:\\Claude\\prokas-mcp\\.venv\\Scripts\\python.exe",
         "args": ["-m", "prokas_mcp"],
         "env": { "PROKAS_MCP_CONFIG": "C:\\Claude\\prokas-mcp\\config.yaml" }
       }
     }
   }
   ```

   Gibt es dort schon `mcpServers` (z. B. für den Mail-Connector), nur den Block `"prokas": {…}` hinzufügen.
   Alternative: `"command": "C:\\Claude\\prokas-mcp\\start_prokas_mcp.bat"` ohne `args`.
4. Claude Desktop **ganz beenden** (auch im Infobereich) und neu starten. Unter den Werkzeugen erscheint „prokas“.
5. In Claude schreiben: **„Wie ist der PROKAS-Connector-Status?“** Claude ruft `connector_status` auf.

Einstellungen stehen in **`config.yaml`**, mit Kommentaren. Nach Änderungen Claude Desktop neu starten.

---

## 3. Ordner und Berechtigungen

| Ordner (Standard) | Zweck | Recht für den Windows-Benutzer, unter dem Claude Desktop läuft |
| --- | --- | --- |
| `P:\Claude\Exporte` | Hier speichert Berichtswesen die Listen (Excel/Editor, Nachtset) | Lesen + Schreiben (Excel speichert hier) |
| `P:\Claude\Outbox` | Vorschläge: Preisdateien, Bestelllisten, Mapping-Vorschläge | Ändern |
| `P:\Claude\Jobs` | Aufträge, Sperrdatei, `cli_status.json` | Ändern |
| `P:\Claude\Logs` | Audit-Log, Server-Log | Ändern |

Ordner anlegen und Rechte setzen (Eingabeaufforderung **als Administrator**, `BENUTZER` ersetzen):

```bat
mkdir P:\Claude\Exporte P:\Claude\Outbox P:\Claude\Jobs P:\Claude\Logs
icacls P:\Claude /grant BENUTZER:(OI)(CI)M
```

- Die PROKAS-Ordner bleiben unverändert. Der Connector liest dort nur (`probe_cli`) und weigert sich zu starten, wenn einer seiner Ordner in einem PROKAS-Ordner liegt.
- Der PROKAS-Bediener **„Claude“** sollte nur Rechte für das **Berichtswesen** haben (lesend). Das ist die zweite Sicherung, falls bei der Bildschirmsteuerung etwas schiefgeht.
- Auf `P:\Claude\Exporte` sollten nur Sie und dieser PC Zugriff haben. Dort liegen Rohexporte, die noch personenbezogene Spalten enthalten können.

---

## 4. Erster Start: Beispieldateien prüfen

Die Spaltennamen der Z-Listen sind nicht offiziell dokumentiert. Der Connector hat für jede Liste eine **Spaltenzuordnung** (`mappings\Z0127.yaml` usw.) mit den üblichen Spaltennamen. Diese sind als `validated: false` markiert, bis sie an einer echten Datei geprüft wurden.

1. 3–5 Listen in Berichtswesen ausführen und per **Auswerten → Excel starten → Speichern unter** nach `P:\Claude\Exporte` speichern. Der **Dateiname muss die Z-Nummer enthalten**, z. B. `Z0127_Ladenhueter.xlsx`. Gut zum Start: Z0127, Z0125, Z0015, Z0051, Z0245.
2. Doppelklick auf **`selbsttest_prokas_mcp.bat`**. Für jede Datei steht dort `[OK]` oder `[!!]` mit dem Grund.
3. Oder in Claude: **„Prüfe die Spaltenzuordnung für Z0127.“** (`validate_mapping`). Sie bekommen:
   - erkannte Kodierung, Trennzeichen und Kopfzeile,
   - welche Spalte welchem Feld zugeordnet wurde und welche nicht,
   - Werte, die sich nicht als Zahl/Datum lesen ließen,
   - 3 Beispielzeilen (bereits ohne Personendaten),
   - in der Outbox einen **Vorschlag** `mapping_vorschlag_Z0127_….yaml` mit Erklärung (`.md`).
4. Wenn der Vorschlag stimmt: die `.yaml` als `mappings\Z0127.yaml` in den Connector-Ordner kopieren (ersetzen) und Claude Desktop neu starten. `validated: true` steht dann schon drin.

Nur Spalten aus der Zuordnung verlassen den Server (Allowlist). Unbekannte Spalten werden verworfen, nicht geraten.

---

## 5. Nachtset im Berichtswesen einrichten

Am schnellsten und ganz ohne Bildschirmsteuerung geht es, wenn PROKAS die wichtigsten Listen jede Nacht selbst exportiert. Standard-Nachtset:

| Liste | Inhalt |
| --- | --- |
| Z0015 | Nachfragen im Berichtszeitraum |
| Z0016 | Neinverkaufsprotokoll |
| Z0051 | Verfall in 3 Monaten |
| Z0125 | Spannenkiller Freiwahl < 20 % |
| Z0127 | Ladenhüter (kein Verkauf seit 2 Monaten, EK ab 50 €) |
| Z0165 | Rohertragsstatistik nach Status |
| Z0245 | Defektquote / Lieferfähigkeit |
| Z0327 | Inventurprüfliste alle EK Null |

Einrichtung im Berichtswesen Java (Automatikfunktion: Ausgabe der Auswertungen im Nachtjob, „Berichte“):

1. Berichtswesen öffnen (Programmstarter 4 → 2 → 1).
2. Den Bereich für **Berichte / automatische Ausführung** öffnen und einen neuen Bericht anlegen.
3. Auswertung wählen (z. B. `Z0015`), Rhythmus **täglich**, Berichtszeitraum z. B. „letzte 7 Tage“ bzw. „laufender Monat“.
4. Als Ausgabe **Export in Datei (Excel oder Text)** wählen, Ordner **`P:\Claude\Exporte`**.
5. Dateiname **mit Z-Nummer**, ideal mit Zeitraum: `Z0015_JJJJ-MM-TT_JJJJ-MM-TT.xlsx`. Ohne Zeitraum im Namen liest der Connector ihn aus der Kopfzeile der Datei („Berichtszeitraum: …“), falls vorhanden.
6. Für alle 8 Listen wiederholen.

> Die genauen Menünamen unterscheiden sich je nach Berichtswesen-Version. Wenn die Datei-Ausgabe im Nachtjob nicht angeboten wird, hilft die NOVENTI-Hotline. Ohne Nachtset funktioniert alles trotzdem, nur eben per Bildschirmsteuerung.

Prüfen: **„Ist das Nachtset vollständig?“** (`report_freshness`) warnt, wenn eine Liste fehlt oder älter als 36 Stunden ist.

---

## 6. Benutzung vom iPhone

In der Claude-App kurz schreiben, was Sie wissen wollen. Claude auf dem Apotheken-PC macht den Rest. Beispiele:

| Sie schreiben | Claude nutzt |
| --- | --- |
| „Morgen-Check“ | Prompt `morning_check`: Nachtset, Defektquote, Verfall, verlorene Verkäufe |
| „Hol Z0245 für letzte Woche“ | Prompt `pull_report` / `run_report` |
| „Welche Ladenhüter binden am meisten Geld?“ | `slow_movers` |
| „Spannen prüfen und Preise vorschlagen“ | Prompt `margin_review`: `margin_check`, danach **mit Ihrer Zustimmung** `stage_price_file` |
| „Was verfällt in den nächsten 3 Monaten?“ | `expiry_risk` |
| „Was haben wir diese Woche nicht liefern können?“ | `lost_sales` |
| „Kennzahlen letzter Monat“ | `kpi_snapshot` |
| „Was weißt du über PZN 12345678?“ | `get_article` |
| „Offene Posten?“ | `open_items` (nur Beträge und Daten) |

Zeiträume dürfen locker formuliert sein: „letzte 7 Tage“, „letzter Monat“, „Oktober 2026“, „Q3“, „01.10.2026-27.10.2026“.

Muss Claude eine Liste am Bildschirm erzeugen, dauert das je nach Liste einige Minuten. Während der Öffnungszeiten passiert das nur, wenn Sie es ausdrücklich erlauben („ja, auch jetzt“), weil es den Server belasten kann.

---

## 7. Werkzeuge

| Werkzeug | Zweck |
| --- | --- |
| `connector_status` | Gesundheitscheck: Ordner, Sperrzeit jetzt?, Kommandozeile, Aufträge, Nachtset |
| `run_report(z_code, date_from?, date_to?, period?, force_during_opening=false)` | Eine Liste holen (Datei → Kommandozeile → Bildschirmsteuerung) |
| `ingest_export(job_id)` | Nach dem Runbook: Datei abholen (wartet max. 5 Min.), lesen, bereinigen, Auftrag schließen |
| `job_status(job_id)`, `list_jobs()`, `cancel_job(job_id, reason)` | Aufträge und Warteschlange |
| `report_freshness()` | Letzter Export je Liste, Warnung bei fehlendem Nachtset |
| `list_available_reports()` | Freigegebene Listen mit Beschreibung und Prüfstatus |
| `probe_cli(force=false)` | Sucht (nur lesend) nach einem Weg, Listen ohne Bildschirm zu erzeugen |
| `validate_mapping(z_code?, file?)` | Spaltenzuordnung an echter Datei prüfen, Vorschlag in die Outbox |
| `get_article(pzn)` | Alles, was die Listen über einen Artikel wissen |
| `margin_check(threshold_pct=20, period?, target_margin_pct?)` | Spannenkiller mit VK-Vorschlägen (Z0125, Z0585, Z0165) |
| `lost_sales(period?)` | Nachfragen und Neinverkäufe je PZN (Z0015, Z0016) |
| `defect_rate(period?)` | Defektquote, Lieferfähigkeit (Z0245) |
| `expiry_risk(months=3)` | Verfall mit EK-Wert (Z0051), Verfallartikel ohne Datum (Z0342) |
| `slow_movers(days=60, min_ek=50)` | Ladenhüter nach gebundenem Kapital (Z0127) |
| `open_returns()` | Offene Retouren je Lieferant (Z0207) |
| `open_items()` | Offene Posten: Summen, Altersstruktur, Mahnstufen. Keine Kunden (Z0705) |
| `inventory_data_quality()` | EK fehlt/falsch, 8er-Artikel, Lagerwert > 500 € (Z0327, Z0117, Z0134, Z0322) |
| `kpi_snapshot(period?)` | Umsatz, Rohertrag %, Lagerumschlag, Defektquote (Z0003, Z0165, Z0105, Z0245) |
| `stage_price_file(items, valid_from, valid_to?, label)` | Preisdatei `PZN;Preis` (Windows-1252, Dezimalkomma, CRLF) + Vorschau alt/neu/Differenz in die Outbox |
| `stage_order_list(supplier, items)` | Bestellvorschlag `PZN;Menge` + Vorschau in die Outbox. Es wird nichts bestellt |

Analyse-Werkzeuge holen fehlende oder veraltete **Pflichtlisten** selbst über `run_report`. **Optionale** Listen (z. B. Z0585 bei `margin_check`) werden genutzt, wenn sie da sind, und nur mit `refresh_optional=true` erzeugt. So löst ein kurzer Morgen-Check keine Kette von Bildschirmaufträgen aus.

**Prompts:** `prokas_export_runbook` (die Bildschirm-Checkliste), `pull_report`, `morning_check`, `margin_review`.

---

## 8. Freigegebene Listen

Nur diese Listen dürfen erzeugt werden (`allowed_z_codes` in `config.yaml`). Alles andere wird mit klarer Meldung abgelehnt.

| Z-Code | Liste | Zeitraum nötig |
| --- | --- | --- |
| Z0003 | Erlösgruppenstatistik | ja |
| Z0007 | Einkaufsübersicht Großhandel | ja |
| Z0015 | Nachfragen Berichtszeitraum | ja |
| Z0016 | Neinverkaufsprotokoll | ja |
| Z0051 | Verfall in 3 Monaten | nein |
| Z0102 | Lagerwertverluste bei Preisermäßigung | ja |
| Z0105 | Lagerumschlag nach Lagerorten | ja |
| Z0117 | Inventurprüfliste falsche VEK | nein |
| Z0125 | Spannenkiller Freiwahl < 20 % | nein |
| Z0127 | Ladenhüter seit 2 Monaten, EK ab 50 € | ja (Stichtag = „bis“) |
| Z0134 | 8er-Artikel mit falscher EK-Pflege | nein |
| Z0165 | Rohertragsstatistik nach Status | ja |
| Z0207 | Retourenerinnerung | nein (fragt nach Lieferant) |
| Z0245 | Defektquote / Lieferfähigkeit | ja |
| Z0317 | Preis im Berichtszeitraum geändert | ja (Bearbeiter wird entfernt) |
| Z0322 | Inventurprüfliste Lagerwert > 500 € | nein |
| Z0327 | Inventurprüfliste alle EK Null | nein |
| Z0342 | Verfallartikel ohne Datum | nein |
| Z0585 | Spannenkontrolle | ja |
| Z0705 | Offene Posten Mahnliste (nur Beträge/Daten) | nein |
| Z0713 | BestRX Rote Lagerartikel | nein |
| Z0714 | BestRX Künftig rote Lagerartikel | nein |

---

## 9. Sperrzeiten

| Zeitfenster | Standard | Verhalten |
| --- | --- | --- |
| Nachtjob / Datensicherung | täglich 22:00–05:30 | **Nie** Listen erzeugen. Vorhandene Exporte werden weiter geliefert, notfalls ältere mit Warnhinweis. |
| Öffnungszeiten | Mo–Sa 10:00–20:00 | Nur mit `force_during_opening=true` (Claude fragt Sie vorher). |

Die Meldung nennt den nächstmöglichen Zeitpunkt. Einstellbar in `config.yaml` (`blocked_windows`, `opening_hours`).

---

## 10. cli_status.json lesen

Datei: `P:\Claude\Jobs\cli_status.json`. Wird beim ersten Start angelegt und nach einem PROKAS-Update automatisch erneuert.

| Feld | Bedeutung |
| --- | --- |
| `status` | `works` = Kommandozeile geht · `degraded` = zuletzt Fehler, wird weiter versucht · `failed` = abgeschaltet nach mehreren Fehlern · `not_found` = nichts gefunden (Normalfall bei PROKAS) · `untested` = Test wartet auf erlaubte Zeit · `disabled` = in config.yaml aus |
| `status_text` | Dasselbe in Worten |
| `template` | Der Befehl, der funktioniert hat (Platzhalter `{z_code}`, `{date_from}`, `{date_to}`, `{output_file}`) |
| `tested_at`, `last_success_at`, `last_error`, `last_error_at` | Zeitpunkte und letzter Fehlertext |
| `consecutive_failures` | Fehler in Folge. Ab `cli.max_failures` (3) wird auf `failed` gestellt |
| `fingerprint` | Prüfsumme der PROKAS-Programmdateien. Ändert sie sich (Update), wird neu geprüft |
| `candidates` | Gefundene Startdateien mit erkannten Parametern |
| `skipped_unsafe` | Dateien, die **absichtlich nicht** ausgeführt wurden (enthalten z. B. löschen, Sicherung, Import, buchen, bestellen) |

Getestet wird nur mit der harmlosen Prüfliste **Z0327**, einem Tag Zeitraum und Ausgabe nach `P:\Claude\Jobs\probe`. Kennt der NOVENTI-Support einen Befehl für Berichtswesen ohne Bildschirm, tragen Sie ihn unter `cli.template` in `config.yaml` ein und rufen `probe_cli(force=true)` auf (oder `start_prokas_mcp.bat probe`).

---

## 11. Protokolle

- **Audit-Log** `P:\Claude\Logs\audit-JJJJ-MM.jsonl`: eine Zeile pro Werkzeugaufruf mit Zeit, Werkzeug, Parametern, Dateien, Zeilenzahl, entfernten Spalten (nur Namen), Weg (`file`/`cli`/`gui`) und Dauer. **Nie Listeninhalte.**
- **Server-Log** `P:\Claude\Logs\server.log`: technische Meldungen.
- **Prüfergebnisse** `P:\Claude\Logs\mapping_validation.json`: letzte Prüfung je Liste.

---

## 12. Fehlerbehebung

| Problem | Lösung |
| --- | --- |
| „prokas“ fehlt in Claude Desktop | Claude Desktop ganz beenden und neu starten. Pfade in `claude_desktop_config.json` prüfen (doppelte `\\`). `selbsttest_prokas_mcp.bat` ausführen. Log: `%APPDATA%\Claude\logs\mcp-server-prokas.log` |
| `setup_prokas_mcp.bat`: uv kann nicht installiert werden | Internet/Proxy prüfen. uv von Hand installieren (docs.astral.sh/uv), dann Setup erneut starten |
| „Ordner fehlt / nicht beschreibbar“ | Laufwerk P: verbunden? Rechte setzen (Abschnitt 3) |
| „Liste Zxxxx ist nicht freigegeben“ | Gewollt. Nur Listen aus `allowed_z_codes` sind erlaubt |
| „Jetzt werden keine Listen erzeugt: Nachtjob“ | Gewollt. Nach 05:30 erneut fragen. Vorhandene Exporte werden trotzdem geliefert |
| „Öffnungszeit … force_during_opening“ | Claude fragt Sie. Mit „ja, auch jetzt“ wird erzeugt |
| „Pflichtspalten fehlen“ / „keine Kopfzeile gefunden“ | `validate_mapping('Zxxxx')` ausführen, Vorschlag aus der Outbox prüfen und übernehmen (Abschnitt 4) |
| `file_missing` nach dem Runbook | Excel hat unter anderem Namen/Ort gespeichert. Datei nach `P:\Claude\Exporte` verschieben (Z-Nummer im Namen) und `ingest_export` erneut aufrufen |
| Auftrag hängt / „es läuft schon ein Auftrag“ | `list_jobs()` zeigen lassen, dann `cancel_job(job_id)`. Aufträge laufen nach 20 Min. (`job_timeout_min`) automatisch ab |
| Fokus springt in PROKAS auf „Übersicht Artikel“ | Bekannt, steht im Runbook: richtiges Taskleistenelement anklicken, neuer Screenshot |
| Unbekannter Dialog in PROKAS | Claude bricht ab (`cancel_job`) und meldet sich. Nichts bestätigen |
| Zahlen falsch (z. B. 1000-fach) | `validate_mapping` zeigt Umwandlungsfehler. Meist falsche Spaltenzuordnung |
| Nachtset unvollständig | Automatikfunktion prüfen (Abschnitt 5). Dateinamen müssen die Z-Nummer enthalten |

---

## 13. Tests

Vor jeder Änderung oder nach einem Update:

```bat
cd C:\Claude\prokas-mcp
uv sync
uv run pytest
```

Die Tests nutzen anonymisierte Beispieldateien (`tests\fixtures`, fiktive Person „Erika Mustermann“) für alle 22 Listen und prüfen unter anderem:
deutsche Zahlen/Daten und Summenzeilen, Kodierung und Trennzeichen, dass Personendaten nie herausgehen (auch nicht ins Audit-Log), Allowlist und Sperrzeiten, die Auftragssperre (auch über mehrere Prozesse), den Wechsel Kommandozeile → Bildschirmsteuerung bei Fehlern, Zeitüberschreitung oder leerer Datei, die Vorschlagsdateien (Format, nie überschreiben, nur Outbox) und einen echten Start des Servers über stdio.

---

## 14. Grenzen

- **Spaltennamen sind Annahmen**, bis sie an echten Exporten geprüft sind (Abschnitt 4). Bis dahin weisen die Antworten darauf hin.
- **Keine Kommandozeile bekannt.** PROKAS dokumentiert keinen Weg, Berichtswesen ohne Bildschirm zu starten. Rechnen Sie mit Nachtset + Bildschirmsteuerung.
- **Die Listen enthalten nur auffällige Artikel.** `get_article` kennt nur Artikel, die in einer Liste vorkommen. Für vollständige Stammdaten in PROKAS nachsehen.
- **Summen** werden aus den Einzelzeilen gebildet (Summenzeilen der Liste werden entfernt). Bei Listen mit Zwischensummen bitte einmal gegen PROKAS abgleichen.
- **Über-Schwärzung ist gewollt:** Spalten wie „Gebühr“ oder „Anzahl Kunden“ werden durch die Denylist mit entfernt.
- **VK-Vorschläge** rechnen mit der MwSt aus der Liste (sonst 19 %) und runden auf 5 Cent auf. Verschreibungspflichtige Artikel werden ausgelassen. Es sind nur Vorschläge.
- **Nicht enthalten** (nur mit Ihrer schriftlichen Freigabe): direkter DB2-Zugriff, Aktionen, die Daten in PROKAS ändern, E-Mail-Versand, Bestellungen.
