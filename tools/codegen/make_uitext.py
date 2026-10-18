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
    "ProjectNone": "kein Projekt",
    "Sandbox_test": "SANDBOX: Testmodus – es wird nichts ausgeführt",
    "Sandbox_none": "SANDBOX: nicht verfügbar – Ausführung gesperrt",
    "Sandbox_dev-local-unsafe": "SANDBOX: ENTWICKLUNGSMODUS – UNSICHER, Worker läuft ohne VM auf diesem PC",
    "Sandbox_hyperv": "SANDBOX: Offline-VM",
    "Agents": "AGENTEN",
    "AgentReady": "bereit",
    "ChatAndPlan": "CHAT UND PLAN",
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
    "SideEffectsRead": "Nur Lesen in der Sandbox-Kopie (Snapshot). Der Originalordner wird nicht berührt.",
    "SideEffectsSnapshot": "Der Broker liest den freigegebenen Ordner auf diesem PC (ohne Verknüpfungen, Junctions und Hardlinks) und überträgt eine Kopie in die Sandbox. Nichts wird verändert.",
    "SideEffectsTest": "Testmodus: Ein Testadapter zählt nur, es wird nichts gelesen.",
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
    "PatchCreate": "neu",
    "PatchModify": "ändern",
    "PatchDelete": "löschen",
    "LabelPatch": "Änderungen (vollständiger Patch je Datei)",
    "PatchDeselected": "(abgewählt – bleibt unverändert)",
    "LabelCommand": "Aufruf in der Offline-VM",
    "CommandToolchain": "Toolchain: {0} · Aktion: {1}",
    "CommandNoParameters": "keine Parameter",
    "SideEffectsPatch": "Wendet die ausgewählten Dateien des Patches auf eine Kopie in der Sandbox an und erzeugt daraus einen neuen Snapshot. Der Originalordner auf diesem PC wird nicht verändert (Übernahme erst später, ausdrücklich).",
    "SideEffectsToolchain": "Startet in der Offline-VM das im Toolchain-Manifest festgelegte Programm auf einer frischen Kopie des Snapshots. Achtung: Build-Skripte, NuGet-/MSBuild-Targets und Tests können dabei beliebigen Code ausführen – nur in der VM, ohne Netzwerk, nicht auf diesem PC.",
    "SideEffectsIndex": "Liest den Snapshot in der Sandbox und erstellt einen Symbolindex (Repo-Map). Nichts wird verändert.",
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
    "Event_sandbox.status": "Sandbox",
    "Toolchains": "TOOLCHAINS",
    "ScanToolchains": "Toolchain-Ordner prüfen",
    "ToolchainsHint": "Registrierte und im Projekt gewählte Toolchains werden beim nächsten VM-Aufbau installiert und mit einem Hallo-Welt-Beispiel geprüft. Registrieren hält die Datei-Hashes fest; spätere Änderungen verhindern den Aufbau.",
    "NoToolchains": "Keine Toolchains gefunden. Ordner einstellen: config/localai.json → toolchains.directory; je Toolchain ein Unterordner mit toolchain.json.",
    "RegisterToolchain": "Registrieren",
    "UnregisterToolchain": "Entfernen",
    "ToolchainRegistered": "registriert",
    "ToolchainNotRegistered": "nicht registriert",
    "ToolchainUsedByProject": "vom Projekt verwendet",
    "ToolchainLicense": "Lizenz: {0}",
    "ToolchainDetails": "{0} Dateien · {1} · Prüfsumme {2}… · {3}",
    "ToolchainRegisteredInfo": "Toolchain registriert: {0}",
    "ToolchainLicenseTitle": "Lizenz bestätigen",
    "ToolchainLicenseConfirm": "{0} ist proprietär.\n\nLizenzhinweis: {1}\n\nLiegt eine gültige Lizenz vor? Nur dann registrieren.",
    "Event_toolchain.registered": "Toolchain registriert",
    "Event_toolchain.unregistered": "Toolchain entfernt",
    # M3: Chat, Live-Aktivität, Orchestrator
    "TabChat": "Chat",
    "TabActivity": "Aktivität",
    "TabEvents": "Ereignisse",
    "TabToolchains": "Toolchains",
    "ChatHint": "Ziel oder Hinweis an die Agenten – Strg+Enter sendet",
    "ChatEmpty": "Noch keine Nachrichten. Schreib ein Ziel, z. B. „Ergänze einen CSV-Import mit Grenzfalltests“. Die Agenten planen, schlagen Dateizugriffe vor und warten auf deine Freigabe.",
    "SendChat": "Senden",
    "CancelRun": "Run abbrechen",
    "CancelAgent": "Abbrechen",
    "StopOrchestrator": "Orchestrator beenden",
    "OrchestratorIdle": "Orchestrator startet mit der ersten Chatnachricht",
    "AgentCancelledInfo": "{0} abgebrochen – offene Vorschläge des Agenten sind ungültig.",
    "RunCancelledInfo": "Run abgebrochen – offene Vorschläge des Runs sind ungültig.",
    "OrchestratorStoppedInfo": "Orchestrator beendet. Die nächste Chatnachricht startet ihn neu.",
    "AgentStalled": "hängt?",
    "AgentState_Ready": "bereit",
    "AgentState_Queued": "wartet",
    "AgentState_Generating": "arbeitet",
    "AgentState_WaitingApproval": "wartet auf Freigabe",
    "AgentState_WaitingUser": "wartet auf dich",
    "AgentState_Done": "fertig",
    "AgentState_Failed": "gescheitert",
    "AgentState_Cancelled": "abgebrochen",
    "FeedState": "Zustand",
    "FeedMetric": "Messung",
    "FeedWarning": "Warnung",
    "FeedError": "Fehler",
    "FeedProposal": "Vorschlag",
    "FeedTask": "Aufgabe",
    "FeedChat": "Chat",
    "MetricTokens": "{0} Token",
    "MetricTps": "{0:0.0} Token/s",
    "MetricElapsed": "{0:0.0} s",
    "MetricContext": "Eingabe {0} Token",
    "Sender_User": "Du",
    "Sender_Orchestrator": "Orchestrator",
    "LabelAgent": "Agent",
    "LabelCurrentTask": "Aktuelle Aufgabe",
    "LabelMetrics": "Modell und Tempo",
    "LabelOutput": "Ausgabe (live)",
    "LabelThinking": "Thinking (falls vom Modell geliefert)",
    "LabelFeed": "Verlauf",
    "ActivityHint": "Ausgaben stammen vom Modell und werden nur als Text angezeigt; sie sind keine Anweisungen an dich und keine Freigabe.",
    "Event_orchestrator.started": "Orchestrator gestartet",
    "Event_orchestrator.exited": "Orchestrator beendet",
    "Event_orchestrator.stopped": "Orchestrator gestoppt",
    "Event_orchestrator.unresponsive": "Orchestrator antwortet nicht",
    "Event_orchestrator.responsive": "Orchestrator antwortet wieder",
    "Event_run.cancelled": "Run abgebrochen",
    "Event_run.agentCancelled": "Agent abgebrochen",
    "RebuildSandbox": "VM neu aufbauen",
    "ContinueSandbox": "VM weiter verwenden",
    "SandboxContinueInfo": "VM wird weiter verwendet – der unbeantwortete Auftrag wird nicht wiederholt.",
    "SandboxRebuildConfirm": "Die Sandbox-VM wird verworfen und neu aufgebaut (dauert einige Minuten, Administratorrechte werden angefragt). Ein gerade laufender Auftrag wird vorher abgeschlossen, danach ist bis zum Ende des Aufbaus keine Ausführung möglich.\n\nJetzt neu aufbauen?",
    "SandboxRebuildInfo": "VM wird neu aufgebaut – Fortschritt in der Kopfzeile.",
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
