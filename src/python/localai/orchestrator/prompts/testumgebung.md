<!-- Hinweise für die Agenten, wenn Prüfprogramme der Testumgebung (*.tproj.json, *.tsim.json) bearbeitet werden.
     Bausteinlisten erzeugt mit: testumgebung-cli katalog (Übersicht) bzw. katalog <typ> (Details).
     Bei neuer Testumgebung-Version neu erzeugen; lint zeigt bei Fehlern ohnehin den aktuellen Katalogauszug. -->
## Testumgebung test programs (*.tproj.json) and scenarios (*.tsim.json)

Project file (UTF-8 JSON, no comments): {"format":"testumgebung.projekt","formatVersion":1,"name":"<unique test id>",
"version":"0.1","description":"purpose, assumptions","variables":[...],"init":GROUP,"loop":GROUP,"finalization":GROUP,
"simulation":[{"name":"<resource>","channel":"ai0","unit":"V","value":230}]}
- GROUP: {"id","name","variables":[],"repeat":{"mode":"Once"},"resultBeforeRepeat":"Keep","children":[...]}. loop uses
  "repeat":{"mode":"Endless"}. Modes: Once, Count(count), Duration(durationMs), While/Until(expression), Endless.
- Every entry in "children" has "art": "gruppe" (a GROUP) or "schritt" (a step). IDs are unique in the whole file.
- Step: {"art":"schritt","id":"u","name":"Ausgangsspannung","block":"<type>","parameters":{...}}. ALL parameter values
  are strings ("250", "true"). Use only block types and parameter names from the catalog below.
- Variable: {"name":"Spannung","type":"Double","initial":"0"}; types Bool, Int, Double, String, DateTime; initial is a
  string. Names must be valid identifiers.
- One device under test per loop iteration: a group in loop with "dutRun":true,"dutIdVariable":"Kennung" that first
  asks for the serial number (block werker, Art Eingabe, Datentyp Text, Ziel Kennung) and then measures/evaluates.
- Expressions (limits, conditions, values) may use variables: "Untergrenze":"GrenzwertMin",
  "Bedingung":"Spannung >= 220 && Spannung <= 240". A string literal inside an expression needs quotes: "\"A\"".
- Code step: block "code", Sprache "C#" or "Python", Quelltext = method body (not a program), Timeout in seconds.
  Group variables are fields (C#) / local names (Python); changed values are written back. Return Pass(), Fail("why"),
  Error("why"), NResult() or Measured(value, low, high, "unit"). Escape newlines as \n and quotes as \" in JSON.
- Hardware only through abstract resource names (Ressource). Add each one to "simulation" for testing. Never invent
  device commands, limits, units or timeouts – take them from the goal or ask; mark assumptions in "description".

Scenario file (one simulated run; write several: normal case, limits exactly at and just outside, FAIL, invalid
operator input, operator abort): {"format":"testumgebung.szenario","formatVersion":1,"name":"...",
"project":"<relative path to .tproj.json>","duts":2,"simulation":{"<resource>":230.0 or [229.9, 230.1]},
"answers":[{"step":"<step id>","values":["SN-1","SN-2"]} or {"step":"<step id>","value":"x"}],
"abort":{"atStep":"<step id>","occurrence":1} (optional),
"expect":{"status":"PASS|FAIL|ERROR|PASS + WERROR","duts":[{"dutId":"SN-1","rating":"PASS"}],
"steps":[{"step":"<step id>","rating":"PASS","value":230.0,"tolerance":0.01,"count":2,"occurrence":1}]}}
- Instructions are confirmed automatically; every input/choice needs an answer, otherwise the run is aborted.
- Expected values come from the requirement, never from copying the current output. Unknown fields are errors.

Verify with toolchain testumgebung: lint {path} (structure and block parameters; shows the catalog entry on errors),
build {path} (compiles code steps), test {path} (all scenarios, JUnit), run {script: one .tsim.json} (step-by-step
trace with values, limits and answers).

### Block catalog (type – name (required parameters))
## Ablauf
fehler – Fehler erzeugen
warten – Warten (Pflicht: Dauer)
## Analog messen / ausgeben
analog.ausgeben – Ausgabe setzen (Pflicht: Ressource, Wert)
analog.messen – Messung (Pflicht: Ressource)
kurve.aufnehmen – Kurve aufnehmen (Pflicht: Ressourcen, Abtastrate, Dauer)
## Auswahl & Auftrag
auswahl.test – Test auswählen (Pflicht: Testkennung)
## Bewertung
bewertung – Bewertung (Pflicht: Bedingung)
bewertung.setzen – Bewertung setzen
## CodeBlocks
baustein – Baustein (Pflicht: Baustein)
code – CodeBlock (Pflicht: Quelltext)
gruppenbaustein – Gruppenbaustein (Pflicht: Baustein)
## Digitale Ein-/Ausgänge
digital.lesen – Digitaleingang lesen (Pflicht: Ressource)
digital.setzen – Digitalausgang setzen (Pflicht: Ressource)
## Kommunikation
can.botschaft.senden – CAN-Botschaft senden (DBC) (Pflicht: Ressource, Datenbank, Botschaft)
can.empfangen – CAN empfangen (Pflicht: Ressource, Id, Zeitlimit)
can.senden – CAN senden (Pflicht: Ressource, Id)
can.signal.lesen – CAN-Signal lesen (DBC) (Pflicht: Ressource, Datenbank, Signal)
doip.suche – DoIP Fahrzeugsuche (Pflicht: Zeitlimit)
j1939.adresse – J1939 Adresse beanspruchen (Pflicht: Ressource, Adresse, Name)
j1939.empfangen – J1939 empfangen (Pflicht: Ressource, Zeitlimit)
j1939.senden – J1939 senden (Pflicht: Ressource, Quelle)
komm.abfrage – Abfrage (Pflicht: Ressource, Daten, Zeitlimit)
komm.empfangen – Empfangen (Pflicht: Ressource, Zeitlimit)
komm.senden – Senden (Pflicht: Ressource, Daten)
komm.verbindung – Verbindung konfigurieren (Pflicht: Ressource)
lin.fahrplan – LIN-Fahrplan (Pflicht: Ressource, Tabelle)
lin.lesen – LIN lesen (Pflicht: Ressource, Frame)
lin.senden – LIN senden (Pflicht: Ressource, Frame)
modbus.lesen – Modbus lesen (Pflicht: Ressource, Adresse)
modbus.schreiben – Modbus schreiben (Pflicht: Ressource, Adresse, Wert)
netz.http – HTTP-Anfrage (GET / POST / …) (Pflicht: Ressource, Zeitlimit, Pfad)
netz.sftp.download – SFTP-Datei herunterladen (Pflicht: Ressource, Zeitlimit, Lokal, Remote)
netz.sftp.upload – SFTP-Datei hochladen (Pflicht: Ressource, Zeitlimit, Lokal, Remote)
netz.ssh – SSH-Befehl (Pflicht: Ressource, Zeitlimit, Befehl)
opcua.zugriff – OPC UA (Pflicht: Ressource)
uds.anfrage – UDS-Diagnose (Pflicht: Ressource, Anfrage, Zeitlimit)
uds.sicherheit – UDS-Sicherheitszugang (Pflicht: Ressource, Stufe, Zeitlimit)
## Kurvenausgabe
kurve.laden – Kurve laden (Pflicht: Ressource, Kurve)
kurve.starten – Kurvenausgabe starten (Pflicht: Ressource)
kurve.stoppen – Kurvenausgabe stoppen (Pflicht: Ressource)
kurve.uebertragen – Kurve übertragen (Pflicht: Ressource)
## Prozesse
prozess.ausgabe-warten – Auf Prozessausgabe warten (Pflicht: Prozess, Muster)
prozess.beenden – Prozess beenden (Pflicht: Prozess)
prozess.senden – An Prozess senden (Pflicht: Prozess)
prozess.start – Prozess starten (Pflicht: Datei)
prozess.status – Prozessstatus abfragen (Pflicht: Prozess)
prozess.warten – Auf Prozess warten (Pflicht: Prozess)
## Variablen
wert.setzen – Wert setzen (Pflicht: Ziel, Ausdruck)
## Werkerinteraktion
werker – Werkerinteraktion (Pflicht: Text)
## Zwei Prüfplätze
pruefplatz.freigabe.erteilen – Andere Seite freigeben (Pflicht: Kanal)
pruefplatz.freigabe.warten – Freigabe abwarten (Pflicht: Kanal)

### Parameters of common blocks
werker – Werkerinteraktion [Werkerinteraktion]
  Anweisung, Bild mit Bestätigung, Eingabe oder Auswahl. Ausgang: Antwort bzw. Eingabewert.
  - Art (Choice, Standard „Anweisung“, Auswahl: Anweisung | Bild mit Bestätigung | Eingabe | Auswahl)
  - Text (Text, Pflicht, Standard „Bitte bestätigen“)
  - Bild (Image)
  - Datentyp (Choice, Standard „Zahl“, Auswahl: Zahl | Ganzzahl | Text | Ja/Nein)
  - Pflicht (Bool, Standard „true“)
  - Standardwert (Text)
  - Min (Number)
  - Max (Number)
  - Einheit (Text)
  - ErlaubteWerte (Text) – durch ; getrennt, z. B. i. O.;n. i. O.
  - Warten (Bool, Standard „true“)
  - Ziel (Variable)
  - Anzeige (Choice, Standard „Vorherigen Inhalt ersetzen“, Auswahl: Vorherigen Inhalt ersetzen | Bis Gruppenende sichtbar)
analog.messen – Messung [Analog messen / ausgeben]
  Liest einen Messwert, schreibt ihn in die Zielvariable und bewertet gegen Grenzwerte.
  - Ressource (Resource, Pflicht, Standard „Spannung_Pruefling“)
  - Ziel (Variable)
  - Untergrenze (Expression) – Zahl oder Ausdruck, z. B. GrenzwertMin
  - Obergrenze (Expression)
  - Einheit (Text, Standard „V“)
analog.ausgeben – Ausgabe setzen [Analog messen / ausgeben]
  Setzt einen analogen Ausgang bzw. eine Quelle (unbewertet).
  - Ressource (Resource, Pflicht, Standard „AC_Quelle“)
  - Wert (Expression, Pflicht, Standard „0“)
digital.lesen – Digitaleingang lesen [Digitale Ein-/Ausgänge]
  Liest einen digitalen Eingang; optional Bewertung gegen Erwartungswert.
  - Ressource (Resource, Pflicht, Standard „Digital_Start“)
  - Ziel (Variable)
  - Erwartet (Choice, Auswahl:  | true | false)
digital.setzen – Digitalausgang setzen [Digitale Ein-/Ausgänge]
  Setzt einen digitalen Ausgang (unbewertet).
  - Ressource (Resource, Pflicht, Standard „Digital_Start“)
  - Wert (Bool, Standard „true“)
bewertung – Bewertung [Bewertung]
  PASS, wenn die Bedingung erfüllt ist, sonst FAIL.
  - Bedingung (Expression, Pflicht, Standard „true“) – z. B. Spannung >= GrenzwertMin && Spannung <= GrenzwertMax
  - Wert (Expression)
  - Einheit (Text)
  - Meldung (Text)
bewertung.setzen – Bewertung setzen [Bewertung]
  Setzt eine feste Bewertung.
  - Bewertung (Choice, Standard „PASS“, Auswahl: NRESULT | PASS | FAIL | ERROR)
wert.setzen – Wert setzen [Variablen]
  Zielvariable = Ausdruck (unbewertet).
  - Ziel (Variable, Pflicht)
  - Ausdruck (Expression, Pflicht, Standard „0“)
warten – Warten [Ablauf]
  Wartet die angegebene Zeit (abbrechbar).
  - Dauer (Number, Pflicht, Standard „1000“)
fehler – Fehler erzeugen [Ablauf]
  Erzeugt einen Ausführungsfehler (ERROR) – für Simulation und Tests.
  - Meldung (Text, Standard „Simulierter Fehler“)
code – CodeBlock [CodeBlocks]
  C#- oder Python-Code im Ablauf. Greift auf die Variablen seines Ordners zu (als Felder bzw. lokale Namen).
  - Sprache (Choice, Standard „C#“, Auswahl: C# | Python | C++)
  - Quelltext (Code, Pflicht, Standard „return Pass();“)
  - Timeout (Number, Standard „5“) – Reagiert der Code nicht auf den Abbruch, wird der Worker-Prozess danach beendet.
