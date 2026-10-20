# Lizenzen und Freigabe von Releases

Der eigene Code wird unter MIT veröffentlicht; siehe [LICENSE](../LICENSE).
Als Rechteinhaber ist der GitHub-Name bugtesterdani eingetragen. Das setzt
voraus, dass keine Arbeitgeberrechte, übernommenen Fremdtexte oder Rechte
anderer Mitwirkender entgegenstehen. Fremder Code darf nicht durch Entfernen
seiner bisherigen Hinweise nach MIT umetikettiert werden.

Die Übersicht [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md) trennt die
eigene Lizenz von Abhängigkeiten, externen Werkzeugen und Modellgewichten.

## Automatische Sammlung

`packaging/package.py stage` übernimmt LICENSE, THIRD-PARTY-NOTICES.md und
den Ordner licenses/. Vor dem Bau eines Installers wird zusätzlich ausgeführt:

```text
python packaging/licenses.py --stage build/stage --runtime <Python-Installationsordner> --runtime-version <X.Y.Z> --assets <Desktop/obj/project.assets.json> --assets <Broker/obj/project.assets.json>
```

Die genaue Python-Version wird im Workflow über sys.version der installierten
Laufzeit erfasst; uv-Verzeichnisnamen enthalten unter Windows nicht immer die
Patchversion. Der Sammler arbeitet offline, führt keine Paketprogramme aus und verwendet
nur die beim Build tatsächlich vorhandenen Metadaten und Originaltexte.
Er sammelt veröffentlichte direkte und transitive NuGet-Laufzeitabhängigkeiten
aus deps.json, Python-Pakete aus dist-info und Lizenzhinweise der Python-Laufzeit.
Paketordner und Paketpfade stammen aus den angegebenen project.assets.json.
Reine Buildwerkzeuge werden nicht weitergegeben und sind nicht Bestandteil
dieses Laufzeitinventars; ihre Nutzungsbedingungen gelten beim Bauen weiterhin.
Auch .NET-Runtimepacks aus den veröffentlichten deps.json werden berücksichtigt.

Fehlt ein Paket, eine Lizenzdeklaration oder ein benötigter Originaltext, wird
mit Fehler abgebrochen. Keine Platzhalter und keine generische MIT-Lizenz als
Ersatz für fremde Copyright-Hinweise verwenden. Für Pakete, die keinen Text
mitliefern, kann ein versionsgenauer, manuell geprüfter Nachweis unter
`licenses/overrides/<nuget|python>/<paketname>/<version>/` hinterlegt werden.
Er muss `review.json` mit `license`, `source` (HTTPS-URL zur genauen Version)
und `reason` sowie mindestens einen Originaltext (LICENSE/NOTICE/COPYING)
enthalten. Dies ist ein bewusst zu prüfender Ausnahmeweg, kein automatischer
Lizenzdownload. Der Nachweis wird in das Inventar übernommen. Gemeinsame Originaltexte können
alternativ über versionsgenaue Einträge in licenses/overrides.json referenziert
werden. Jede Ausnahme nennt die geprüfte Upstream-Version bzw. einen Commit;
bei Paketupdates gibt es keinen automatischen Rückgriff auf ältere Versionen.

Alle gesammelten Dateien werden mit SHA-256 und relativen Pfaden dokumentiert.
Die vollständige Python-Laufzeit einschließlich vorhandener Lizenzordner bleibt
zusätzlich im Paket. Der Sammler ist ein Nachweis der gesammelten Metadaten,
keine Garantie, dass jeder eingebettete native Bestandteil in den Metadaten
korrekt beschrieben ist.

## Vor einer Veröffentlichung prüfen

- Inventar und alle Warnungen für die konkrete Windows-/Linux-Ausgabe prüfen.
- Native Bibliotheken, Schriftdateien und Laufzeit-Notices anhand der tatsächlichen
  Binärpakete abgleichen; erforderliche zusätzliche Texte versionsgenau ergänzen.
- Bei Copyleft-Komponenten die jeweilige Kombination und gegebenenfalls
  Quellcodebereitstellung gesondert prüfen. Der Sammler entscheidet keine
  Lizenzkompatibilität anhand einer bloßen Lizenzbezeichnung.
- Keine Windows-ISOs, HALCON-Lizenzen, fremden Toolchain-Bundles oder
  Modellgewichte ohne passende Weitergaberechte hochladen.
- MSVC-/Windows-/HALCON-Nutzungsrechte für die tatsächlichen Nutzer, Projekte
  und VMs prüfen. MIT gewährt diese Rechte nicht.
- Bei eigenen Gastabbildern oder Offline-Linux-Paketen alle enthaltenen
  Komponenten samt zugehöriger Quellcodepflichten erfassen; sie gehören nicht
  zu den automatisch hier erzeugten Anwendungsinstallern.
- Eigentums- und Beitragsrechte am eigenen Code bestätigen; bei übernommenem
  oder modellgeneriertem Code nach fremden Lizenzhinweisen suchen.

## Umfang der technischen Prüfung

Die neue CI prüft die Sammlung mit temporären Testpaketen und die Aufnahme der
Hinweise in ZIP-Archive. Der Release-Workflow sammelt die tatsächlichen
Abhängigkeiten auf beiden Plattformen vor dem Installerbau; bei Lücken stoppt er.
Das ersetzt keine Rechtsberatung und keine abschließende Prüfung persönlicher
Lizenzverträge oder aller Inhalte eines fertigen Binärpakets.
