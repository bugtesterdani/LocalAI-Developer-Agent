using LocalAi.Contracts;
using LocalAi.Desktop.Core;

namespace LocalAi.Tests;

/// <summary>
/// Ende-zu-Ende ohne Fenster: echter Broker-Prozess + Python-Fake-Agent + Oberflächenlogik.
/// Wird übersprungen, wenn uv nicht installiert ist.
/// </summary>
public sealed class EndToEndTests(ITestOutputHelper output)
{
    private readonly Lock _ui = new();
    private readonly System.Diagnostics.Stopwatch _clock = System.Diagnostics.Stopwatch.StartNew();
    private string? _agentLog;

    /// <summary>Zeitstempel je Schritt, sichtbar mit --logger "console;verbosity=detailed".</summary>
    private void Step(string text) => output.WriteLine($"[{_clock.Elapsed.TotalSeconds,6:F1} s] {text}");

    [Fact, Trait("Pruefung", "M1-E2E")]
    public async Task Fake_Agent_Freigaben_mit_Abwahl_ueber_die_Oberflaechenlogik()
    {
        Assert.SkipUnless(CommandExists("uv"), "uv nicht gefunden – Ende-zu-Ende-Test übersprungen");

        var data = Path.Combine(Path.GetTempPath(), "localai-e2e-" + Guid.NewGuid().ToString("N"));
        Step("Broker wird gestartet");
        var broker = await BrokerProcess.StartAsync(CancellationToken.None, ["--data", data]);
        BrokerConnection? requestsConn = null, eventsConn = null;
        MainViewModel? mainVm = null;
        try
        {
            var hs = broker.Handshake;
            _agentLog = Path.Combine(hs.DataDirectory, "fake-agent.log");
            Step($"Datenordner: {hs.DataDirectory}");
            var requests = requestsConn = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            var events = eventsConn = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            var main = mainVm = new MainViewModel(requests, events, a => { lock (_ui) { a(); } }, hs.SessionId, hs.Mode);
            Assert.Equal("dev-local-unsafe", hs.Mode);
            await main.InitializeAsync();
            Step("Oberflächenlogik verbunden, Fake-Agent wird gestartet (uv run)");

            main.StartFakeRunCommand.Execute(null);
            await WaitAsync(() => main.InfoText is not null || main.ErrorText is not null, TimeSpan.FromSeconds(15), "Rückmeldung zum Start des Fake-Agenten");
            lock (_ui)
            {
                Assert.True(main.ErrorText is null, "Start des Fake-Agenten fehlgeschlagen: " + main.ErrorText + "\n" + ReadShared(_agentLog!));
                Step(main.InfoText!);
            }
            // 1. Snapshot und 2. Auflisten einzeln erlauben (echter Worker liest den Demo-Ordner).
            foreach (var tool in new[] { "Workspace.Snapshot", "File.List" })
            {
                await WaitAsync(() => main.Approvals.Any(c => c.Proposal.Tool == tool), TimeSpan.FromSeconds(90), $"Vorschlag {tool}");
                ApprovalCardViewModel card;
                lock (_ui)
                {
                    card = main.Approvals.Single(c => c.Proposal.Tool == tool);
                    Assert.True(card.CanApprove, $"{tool} kann erlaubt werden");
                }
                Step($"{tool} wird erlaubt");
                card.ApproveCommand.Execute(null);
                await WaitAsync(() => !main.Approvals.Contains(card), TimeSpan.FromSeconds(10), $"{tool} verlässt die Queue");
            }

            await WaitAsync(() => main.Approvals.Count == 2, TimeSpan.FromSeconds(30), "zwei Lese-Vorschläge vom Fake-Agenten");
            Step("Zwei Lese-Vorschläge sind da");
            await WaitAsync(() => main.EventLog.Any(e => e.Text.Contains("abgewiesen", StringComparison.Ordinal)), TimeSpan.FromSeconds(10),
                "unzulässiger Pfad abgewiesen");

            ApprovalCardViewModel batch, single;
            lock (_ui)
            {
                batch = main.Approvals.Single(c => c.IsBatch);
                single = main.Approvals.Single(c => !c.IsBatch);
                Assert.Equal(7, batch.TotalCount);
                batch.Targets[0].IsSelected = false; // README.md abwählen
                batch.Targets[5].IsSelected = false; // tests/ImporterTests.cs abwählen
                Assert.True(!batch.CanApprove, "Erlauben gesperrt nach Abwahl");
            }

            Step("Abwahl wird übernommen");
            batch.ApplySelectionCommand.Execute(null);
            await WaitAsync(() => main.Approvals.Any(c => c.Proposal.ParentProposalId == batch.ProposalId), TimeSpan.FromSeconds(10), "neuer Vorschlag nach Abwahl");
            ApprovalCardViewModel narrowed;
            lock (_ui)
            {
                narrowed = main.Approvals.Single(c => c.Proposal.ParentProposalId == batch.ProposalId);
                Assert.Equal(5, narrowed.TotalCount);
                Assert.True(narrowed.CanApprove, "neuer Vorschlag kann erlaubt werden");
                Assert.True(!main.Approvals.Contains(batch), "alter Vorschlag verschwindet");
            }

            Step("Erlauben und Ablehnen");
            narrowed.ApproveCommand.Execute(null);
            single.DenyCommand.Execute(null);
            await WaitAsync(() => main.Approvals.Count == 0, TimeSpan.FromSeconds(10), "Queue leer");
            await WaitAsync(() => main.EventLog.Any(e => e.Text.Contains(UiText.State(ProposalState.Succeeded), StringComparison.Ordinal)),
                TimeSpan.FromSeconds(10), "Ausführung erfolgreich");

            Step("Warte auf Abschluss des Fake-Agenten");
            var log = Path.Combine(hs.DataDirectory, "fake-agent.log");
            await WaitAsync(() => ReadShared(log).Contains("fertig", StringComparison.Ordinal), TimeSpan.FromSeconds(20), "Fake-Agent fertig");
            var text = ReadShared(log);
            Assert.True(text.Contains("Unzulässiger Pfad korrekt abgewiesen", StringComparison.Ordinal), "Pfadabweisung im Agentenprotokoll");
            Assert.True(text.Contains("vom Nutzer abgewählt: ['t1', 't6']", StringComparison.Ordinal), "Agent erfährt die Abwahl: " + text);
            Assert.True(text.Contains("Denied", StringComparison.Ordinal), "Agent erfährt die Ablehnung");
            Assert.True(text.Contains("7 Dateien gefunden", StringComparison.Ordinal), "Auflisten über den Worker");
            Assert.True(text.Contains("erste Zeile: # Format", StringComparison.Ordinal), "echter Dateiinhalt aus dem Snapshot");
            Step("Prüfungen bestanden");
        }
        finally
        {
            // Jeder Schritt einzeln und zeitlich begrenzt, damit ein Hänger benannt wird statt den Testlauf zu blockieren.
            await CleanupAsync("Oberflächenlogik beenden", mainVm);
            await CleanupAsync("Ereignis-Verbindung schließen", eventsConn);
            await CleanupAsync("Anfrage-Verbindung schließen", requestsConn);
            await CleanupAsync("Broker-Prozess beenden", broker);
        }
    }

    private async Task CleanupAsync(string what, IAsyncDisposable? resource)
    {
        if (resource is null)
        {
            return;
        }
        Step($"{what} …");
        var dispose = resource.DisposeAsync().AsTask();
        if (await Task.WhenAny(dispose, Task.Delay(TimeSpan.FromSeconds(15))) != dispose)
        {
            Assert.Fail($"Aufräumen hängt: '{what}' kehrt nach 15 s nicht zurück");
        }
        await dispose;
        Step($"{what}: erledigt");
    }

    private async Task WaitAsync(Func<bool> condition, TimeSpan timeout, string what)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            lock (_ui)
            {
                if (condition())
                {
                    return;
                }
            }
            if (DateTime.UtcNow > deadline)
            {
                var agent = _agentLog is null ? "(noch kein Datenordner)" : ReadShared(_agentLog);
                Assert.Fail($"Zeitüberschreitung nach {timeout.TotalSeconds:F0} s: {what}\n" +
                            $"--- Protokoll des Fake-Agenten ({_agentLog}) ---\n" +
                            (agent.Length == 0 ? "(leer – Agent hat nichts ausgegeben oder wurde nicht gestartet)" : agent));
            }
            await Task.Delay(100);
        }
    }

    /// <summary>Liest eine Datei, die ein anderer Prozess gerade beschreibt (unter Windows nötig).</summary>
    private static string ReadShared(string path)
    {
        if (!File.Exists(path))
        {
            return string.Empty;
        }
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(stream);
            return reader.ReadToEnd();
        }
        catch (IOException)
        {
            return string.Empty; // gerade gesperrt – nächster Versuch im Warteintervall
        }
    }

    private static bool CommandExists(string name) =>
        (Environment.GetEnvironmentVariable("PATH") ?? "")
            .Split(Path.PathSeparator)
            .Any(dir => File.Exists(Path.Combine(dir, name)) || File.Exists(Path.Combine(dir, name + ".exe")));
}
