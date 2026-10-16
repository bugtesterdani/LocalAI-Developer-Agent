using System.Reflection;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml.Linq;
using LocalAi.Contracts;
using LocalAi.Desktop.Core;

namespace LocalAi.Tests;

/// <summary>
/// Prüft Oberflächenlogik ohne WPF. Die XAML-Prüfung ersetzt den WPF-Compiler nicht,
/// fängt aber Tippfehler bei Text-Schlüsseln und Binding-Namen ab.
/// </summary>
public sealed partial class DesktopTests
{
    private static readonly Type[] ViewModelTypes =
        [typeof(MainViewModel), typeof(ApprovalCardViewModel), typeof(TargetItemViewModel), typeof(AgentItem), typeof(EventLine)];

    private static string XamlPath() =>
        Path.Combine(ContractTests.RepoRoot(), "src", "dotnet", "LocalAi.Desktop", "MainWindow.xaml");

    [GeneratedRegex(@"\{loc:T\s+(?<key>[A-Za-z0-9_.]+)\}")]
    private static partial Regex TextKeyRegex();

    [GeneratedRegex(@"\{Binding\s+(?<path>[A-Za-z][A-Za-z0-9_.]*)(?=\s*[,}])")]
    private static partial Regex BindingRegex();

    [Test("AC16")]
    public void Xaml_ist_wohlgeformt_und_alle_Textschluessel_existieren()
    {
        var text = File.ReadAllText(XamlPath());
        XDocument.Parse(text);
        var keys = TextKeyRegex().Matches(text).Select(m => m.Groups["key"].Value).Distinct().ToList();
        Assert.True(keys.Count > 20, "Texte gefunden");
        var missing = keys.Where(k => !UiText.Exists(k)).ToList();
        Assert.True(missing.Count == 0, "Fehlende Texte: " + string.Join(", ", missing));
    }

    [Test]
    public void Alle_Bindings_verweisen_auf_vorhandene_Eigenschaften()
    {
        var text = File.ReadAllText(XamlPath());
        var properties = ViewModelTypes
            .SelectMany(t => t.GetProperties(BindingFlags.Public | BindingFlags.Instance))
            .Select(p => p.Name)
            .ToHashSet(StringComparer.Ordinal);
        var paths = BindingRegex().Matches(text).Select(m => m.Groups["path"].Value).Distinct();
        var missing = paths
            .Select(p => p.StartsWith("DataContext.", StringComparison.Ordinal) ? p["DataContext.".Length..] : p)
            .Where(p => !properties.Contains(p.Split('.')[0]))
            .ToList();
        Assert.True(missing.Count == 0, "Unbekannte Bindings: " + string.Join(", ", missing));
    }

    [Test("AC16")]
    public void Zustandstexte_sind_vollstaendig_deutsch()
    {
        foreach (var state in Enum.GetValues<ProposalState>())
        {
            Assert.True(UiText.Exists($"State_{state}"), $"Text für Zustand {state}");
        }
        Assert.Equal("Ablehnen", UiText.Get("Deny"));
    }

    [Test("SEC-23")]
    public void Sammelfreigabe_Karte_sperrt_Erlauben_bis_Auswahl_uebernommen()
    {
        var api = new FakeApi();
        var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"));
        var card = new ApprovalCardViewModel(View("File.ReadMany", 4), main);

        Assert.True(card.IsBatch, "Sammelfreigabe");
        Assert.True(card.CanApprove, "unveränderte Auswahl darf erlaubt werden");
        Assert.Equal("4 von 4 ausgewählt", card.CountText);

        card.Targets[1].IsSelected = false;
        card.Targets[3].IsSelected = false;
        Assert.Equal("2 von 4 ausgewählt", card.CountText);
        Assert.True(!card.CanApprove, "geänderte Auswahl darf nicht direkt erlaubt werden");
        Assert.True(card.CanApplySelection, "Auswahl kann übernommen werden");
        Assert.Equal("t1,t3", string.Join(',', card.BuildNarrowRequest().KeepTargetIds));

        card.FilterText = "Datei3";
        Assert.Equal(1, card.VisibleTargets.Count);
        card.DeselectAllVisibleCommand.Execute(null);
        Assert.Equal("1 von 4 ausgewählt", card.CountText);
        card.FilterText = "";
        card.SelectAllVisibleCommand.Execute(null);
        Assert.True(card.CanApprove, "alle wieder ausgewählt");
    }

    [Test]
    public void Einzelaufruf_hat_keine_Abwahl()
    {
        var api = new FakeApi();
        var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"));
        var card = new ApprovalCardViewModel(View("File.Read", 1), main);
        Assert.True(!card.IsBatch, "kein Batch");
        Assert.Equal("Diesen Aufruf einmal erlauben", card.ApproveText);
        Assert.True(!card.SelectAllVisibleCommand.CanExecute(null), "keine Sammelauswahl");
    }

    [Test]
    public void Kartenabgleich_behaelt_Auswahl_bestehender_Karten()
    {
        var api = new FakeApi();
        var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"));
        var a = View("File.ReadMany", 3);
        var b = View("File.Read", 1);
        main.SyncApprovals([a, b]);
        main.Approvals[0].Targets[0].IsSelected = false;
        main.SyncApprovals([a, b]);
        Assert.Equal(2, main.Approvals.Count);
        Assert.True(!main.Approvals[0].Targets[0].IsSelected, "Auswahl bleibt erhalten");
        main.SyncApprovals([b]);
        Assert.Equal(1, main.Approvals.Count);
        Assert.Equal(b.Proposal.ProposalId, main.SelectedApproval!.ProposalId);
    }

    [Test("SEC-15")]
    public void NotAus_sperrt_Erlauben_in_allen_Karten()
    {
        var api = new FakeApi();
        var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"));
        main.SyncApprovals([View("File.Read", 1)]);
        main.ApplyEvents([new BrokerEvent(1, ContractTime.ToContract(DateTimeOffset.UtcNow), "emergency.stop")]);
        Assert.True(main.IsStopped, "Not-Aus aktiv");
        Assert.True(!main.Approvals[0].CanApprove, "Erlauben gesperrt");
        Assert.Equal(1, main.EventLog.Count);
    }

    private static ProposalView View(string tool, int targets)
    {
        var now = DateTimeOffset.UtcNow;
        var proposal = new ToolProposal(1, Guid.NewGuid().ToString("D"), "run", "task", AgentRole.Tester, tool, "1", "Test",
            JsonDocument.Parse("{}").RootElement.Clone(),
            Enumerable.Range(1, targets).Select(i => new ToolTarget($"t{i}", $"src/Datei{i}.cs")).ToList(),
            "snap", 1, "sbx", ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
        return new ProposalView(proposal, ProposalHashing.Sha256(proposal), ProposalState.AwaitingApproval);
    }

    private sealed class FakeApi : IBrokerUiApi
    {
        public Task<IReadOnlyList<ProposalView>> PendingAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<ProposalView>>([]);
        public Task<ProposalView> DecideAsync(UserDecision decision, CancellationToken ct) => throw new NotSupportedException();
        public Task<ProposalView> NarrowAsync(NarrowRequest request, CancellationToken ct) => throw new NotSupportedException();
        public Task<IReadOnlyList<BrokerEvent>> WaitEventsAsync(long afterSeq, int timeoutMs, CancellationToken ct) => Task.FromResult<IReadOnlyList<BrokerEvent>>([]);
        public Task EmergencyStopAsync(CancellationToken ct) => Task.CompletedTask;
        public Task ReleaseAsync(CancellationToken ct) => Task.CompletedTask;
        public Task<JsonElement> StartFakeAgentAsync(CancellationToken ct) => throw new NotSupportedException();
    }
}
