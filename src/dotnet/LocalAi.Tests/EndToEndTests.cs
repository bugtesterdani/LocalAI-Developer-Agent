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

    /// <summary>Zeitstempel je Schritt, sichtbar mit --logger "console;verbosity=detailed".</summary>
    private void Step(string text) => output.WriteLine($"[{_clock.Elapsed.TotalSeconds,6:F1} s] {text}");

    [Fact, Trait("Pruefung", "M1-E2E")]
    public async Task Fake_Agent_Freigaben_mit_Abwahl_ueber_die_Oberflaechenlogik()
    {
        Assert.SkipUnless(CommandExists("uv"), "uv nicht gefunden – Ende-zu-Ende-Test übersprungen");

        var data = Path.Combine(Path.GetTempPath(), "localai-e2e-" + Guid.NewGuid().ToString("N"));
        Step("Broker wird gestartet");
        await using var broker = await BrokerProcess.StartAsync(CancellationToken.None, ["--data", data]);
        var hs = broker.Handshake;
        await using var requests = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
        await using var events = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
        await using var main = new MainViewModel(requests, events, a => { lock (_ui) { a(); } }, hs.SessionId);
        await main.InitializeAsync();
        Step("Oberflächenlogik verbunden, Fake-Agent wird gestartet (uv run)");

        main.StartFakeRunCommand.Execute(null);
        await WaitAsync(() => main.Approvals.Count == 2, TimeSpan.FromSeconds(90), "zwei Vorschläge vom Fake-Agenten");
        Step("Zwei Vorschläge sind da");
        await WaitAsync(() => main.EventLog.Any(e => e.Text.Contains("abgewiesen", StringComparison.Ordinal)), TimeSpan.FromSeconds(10),
            "unzulässiger Pfad abgewiesen");

        ApprovalCardViewModel batch, single;
        lock (_ui)
        {
            batch = main.Approvals.Single(c => c.IsBatch);
            single = main.Approvals.Single(c => !c.IsBatch);
            batch.Targets[0].IsSelected = false; // src/Parser.cs abwählen
            batch.Targets[5].IsSelected = false; // README.md abwählen
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
        Step("Prüfungen bestanden, Broker wird beendet");
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
                Assert.Fail($"Zeitüberschreitung: {what}");
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
