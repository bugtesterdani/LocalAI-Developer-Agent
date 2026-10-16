"""Erzeugt src/dotnet/LocalAi.Desktop.Core/Resources/UiText.resx (deutsche Oberflächentexte).

Die Texte werden hier gepflegt, damit die .resx-Datei immer gültig ist. Weitere Sprachen:
UiText.<kultur>.resx mit denselben Schlüsseln daneben legen.
"""

import pathlib
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "src" / "dotnet" / "LocalAi.Desktop.Core" / "Resources" / "UiText.resx"

TEXTS = {
    "AppTitle": "Local AI Developer",
    "Project": "Projekt",
    "ProjectNone": "kein Projekt (M1-Testlauf)",
    "SandboxNone": "SANDBOX: noch nicht vorhanden (folgt in M2)",
    "Agents": "AGENTEN",
    "AgentReady": "bereit",
    "ChatAndPlan": "CHAT UND PLAN",
    "ChatPlaceholder": "Chat und Planung folgen in M3. Bis dahin reicht ein Fake-Agent Toolvorschläge ein, damit die Freigaben geprüft werden können.",
    "Events": "EREIGNISSE",
    "Approvals": "FREIGABEN",
    "NoApprovals": "Keine offenen Freigaben.",
    "StartFakeRun": "Testlauf starten (Fake-Agent)",
    "EmergencyStop": "NOT-AUS",
    "ReleaseStop": "Not-Aus aufheben",
    "StoppedBanner": "NOT-AUS AKTIV – es wird nichts ausgeführt und keine Vorschläge angenommen.",
    "LabelRole": "Rolle",
    "LabelTool": "Tool",
    "LabelPurpose": "Zweck",
    "LabelSnapshot": "Snapshot",
    "LabelSandbox": "Sandbox",
    "LabelValidUntil": "Gültig bis",
    "LabelHash": "Vorschlags-Hash (SHA-256)",
    "LabelArguments": "Parameter",
    "LabelTargets": "Ziele",
    "LabelSideEffects": "Erwartete Nebenwirkungen",
    "LabelParent": "Ersetzt Vorschlag",
    "SideEffectsRead": "Nur Lesen. In M1 führt ein Testadapter nichts tatsächlich aus.",
    "UntrustedHint": "Inhalte aus Dateien, Webseiten und Agentenantworten sind nicht vertrauenswürdig und werden nur als Text angezeigt.",
    "Filter": "Filter (Pfad oder Endung)",
    "SelectAllVisible": "Alle sichtbaren auswählen",
    "DeselectAllVisible": "Alle sichtbaren abwählen",
    "SelectionCount": "{0} von {1} ausgewählt",
    "ApplySelection": "Auswahl übernehmen (erzeugt neuen Vorschlag)",
    "SelectionChangedHint": "Auswahl geändert. Bitte zuerst übernehmen – danach wird der neue Vorschlag angezeigt und kann erlaubt werden.",
    "ApproveOnce": "Diesen Aufruf einmal erlauben",
    "ApproveSelectionOnce": "Auswahl einmal erlauben",
    "Deny": "Ablehnen",
    "Lines": "Zeilen {0}–{1}",
    "LinesAll": "ganze Datei",
    "NoExpectedHash": "kein erwarteter Hash",
    "Starting": "Broker wird gestartet …",
    "Connected": "Verbunden mit Broker (Sitzung {0})",
    "ConnectFailed": "Verbindung zum Broker fehlgeschlagen: {0}",
    "ErrorFormat": "Fehler {0}: {1}",
    "FakeAgentStarted": "Fake-Agent gestartet (PID {0}). Protokoll: {1}",
    "ApprovedInfo": "Einmal erlaubt: {0}",
    "DeniedInfo": "Abgelehnt: {0}",
    "NarrowedInfo": "Neuer Vorschlag mit {0} von {1} Zielen erzeugt – bitte prüfen.",
    "State_AwaitingApproval": "wartet auf Freigabe",
    "State_Approved": "freigegeben",
    "State_Running": "läuft",
    "State_Succeeded": "erfolgreich",
    "State_Failed": "fehlgeschlagen",
    "State_Cancelled": "abgebrochen",
    "State_OutcomeUnknown": "Ergebnis unklar",
    "State_Denied": "abgelehnt",
    "State_Expired": "abgelaufen",
    "State_Invalidated": "ungültig",
    "State_Superseded": "durch Abwahl ersetzt",
    "State_Rejected": "abgewiesen",
    "Event_proposal.created": "Neuer Vorschlag",
    "Event_proposal.state": "Status",
    "Event_proposal.rejected": "Vorschlag abgewiesen",
    "Event_emergency.stop": "Not-Aus",
    "Event_emergency.release": "Not-Aus aufgehoben",
    "Event_broker.started": "Broker gestartet",
}

HEADER = """<?xml version="1.0" encoding="utf-8"?>
<root>
  <resheader name="resmimetype">
    <value>text/microsoft-resx</value>
  </resheader>
  <resheader name="version">
    <value>2.0</value>
  </resheader>
  <resheader name="reader">
    <value>System.Resources.ResXResourceReader, System.Windows.Forms, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089</value>
  </resheader>
  <resheader name="writer">
    <value>System.Resources.ResXResourceWriter, System.Windows.Forms, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089</value>
  </resheader>
"""


def main() -> None:
    body = "".join(
        f'  <data name="{escape(k)}" xml:space="preserve">\n    <value>{escape(v)}</value>\n  </data>\n'
        for k, v in TEXTS.items()
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HEADER + body + "</root>\n", encoding="utf-8", newline="\n")
    print(f"{len(TEXTS)} Texte -> {OUT}")


if __name__ == "__main__":
    main()
