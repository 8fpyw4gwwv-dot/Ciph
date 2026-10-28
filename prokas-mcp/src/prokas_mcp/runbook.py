"""GUI runbook for exporting a Berichtswesen list via computer use."""

from __future__ import annotations

RUNBOOK = """\
# PROKAS Berichtswesen – Export per Bildschirmsteuerung

Auftrag: **{job_id}** · Liste **{z_code}** ({title}) · Berichtszeitraum **{date_from} – {date_to}**
Zieldatei: `{expected_file}`
Spätestens fertig bis: {deadline} (Zeitlimit {timeout_min} Min.)
{gui_notes}
Diese Anleitung ist eine Checkliste. Schritte genau so ausführen, nichts anderes in PROKAS anklicken.

## NIEMALS
- **Strg+A drücken** – in PROKAS heißt das *Abbrechen*, nicht „alles markieren“. Text in einem Feld markieren: dreifach klicken.
- **„Abbrechen“ in Eingabemasken klicken** – kann das ganze Modul schließen.
- **„In PROKAS übernehmen“**, **„Markierte Artikel in Gruppe einfügen“** oder irgendeinen anderen Punkt im Menü *Auswerten* außer *Excel starten* / *Export in Editor* anklicken (diese ändern Preise, Gruppen oder Lager).
- Fenster anderer Benutzer/Arbeitsplätze schließen.
- **Unbekannte Dialoge bestätigen.** Stattdessen: Screenshot, `cancel_job("{job_id}", "<was zu sehen war>")` aufrufen, dem Inhaber melden.
- Zahlen-/Datumsfelder: nur einmal in ein **leeres** Feld tippen, dann **Tab**. Steht danach ein falscher Wert drin: Eingabe von vorne beginnen (Feld dreifach klicken, neu tippen) – nicht herumkorrigieren.

## Schritte
1. **Screenshot.** Prüfen, dass das PROKAS-Fenster (Prozess `prostartx86.exe`) aktiv ist.
   Der Fokus springt oft auf „Übersicht Artikel“: dann das richtige Element in der Taskleiste anklicken und erneut Screenshot.
2. **Berichtswesen öffnen:** Programmstarter **4 → 2 → 1**
   (oder Ordner *PROKAS7 Programme* → **Berichtswesen Java**). Ist Berichtswesen schon offen: dessen Taskleisten-Eintrag anklicken.
3. Button **Ausführen** → bei **Listennr** `{z_code}` eingeben → Tab →
   **Berichtszeitraum** von `{date_from}` bis `{date_to}` eingeben (jeweils leeres Feld, dann Tab) → **Ausführen**.
   Fragt die Liste nach weiteren Angaben, die oben nicht genannt sind: Screenshot, abbrechen per `cancel_job`, melden.
4. Das **Rexx-Statusfenster** abwarten: alle **15 Sekunden** einen Screenshot, bis die Ergebnisliste erscheint
   (höchstens bis {deadline}). Nichts anklicken, solange es läuft.
5. In der Ergebnisliste: Menü **Auswerten → Excel starten**.
   In Excel: **Datei → Speichern unter** → als Dateiname genau
   `{expected_file}`
   eintragen (Dateityp *Excel-Arbeitsmappe (*.xlsx)*) → **Speichern** → Excel schließen (Änderungen nicht erneut speichern).
   - **Falls Excel nicht verfügbar ist:** **Auswerten → Export in Editor** → im Editor **Datei → Speichern unter** →
     `{expected_file_txt}` → Speichern → Editor schließen.
6. Das Ergebnisfenster der Liste schließen (Fenster-X bzw. *Beenden*). Sonst nichts verändern; Berichtswesen darf offen bleiben.
7. `ingest_export("{job_id}")` aufrufen. Der Connector liest die Datei, entfernt personenbezogene Daten und liefert die Zahlen.

Klappt etwas nicht: Screenshot machen, `cancel_job("{job_id}", "<Grund>")` aufrufen und dem Inhaber kurz schreiben, was zu sehen war.
"""

GENERIC_PLACEHOLDERS = {
    "job_id": "<job_id>",
    "z_code": "<Z-Code>",
    "title": "<Listenname>",
    "date_from": "<TT.MM.JJJJ>",
    "date_to": "<TT.MM.JJJJ>",
    "expected_file": "<expected_file aus run_report>",
    "expected_file_txt": "<expected_file, aber mit Endung .txt>",
    "deadline": "<deadline>",
    "timeout_min": "20",
    "gui_notes": "",
}


def render_runbook(**values) -> str:
    v = dict(GENERIC_PLACEHOLDERS)
    v.update({k: x for k, x in values.items() if x is not None})
    if v.get("gui_notes"):
        v["gui_notes"] = f"\n**Hinweis zu dieser Liste:** {v['gui_notes']}\n"
    return RUNBOOK.format(**v)
