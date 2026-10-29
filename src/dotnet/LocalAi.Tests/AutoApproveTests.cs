using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// autoApprove: Tools der Projektdatei werden erst nach ausdrücklicher Bestätigung im Run automatisch genehmigt –
/// nur Sandbox-Tools, nur dieser Run, protokolliert; Not-Aus, Abbruch und neue Anfrage beenden die Freigabe.
/// </summary>
public sealed class AutoApproveTests : IAsyncDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-auto-").FullName;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 10, 29, 8, 0, 0, TimeSpan.Zero));
    private readonly CountingTestAdapter _adapter = new();
    private static readonly IReadOnlyList<string> Tools = ["File.List", "File.Read"];

    public async ValueTask DisposeAsync()
    {
        await Task.Yield();
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        FileTools.DeleteTree(_dir);
    }

    private string JournalPath => Path.Combine(_dir, "audit.db");

    private ApprovalBroker NewBroker(IReadOnlyList<string>? tools = null)
    {
        var broker = new ApprovalBroker(new BrokerOptions { AutoApproveTools = tools ?? Tools }, _adapter,
            new AuditJournal(JournalPath, _clock), _clock, new TestPolicyContext());
        broker.Start(runBackgroundLoops: false);
        return broker;
    }

    private static ProposalRequest Request(string tool, object args, IReadOnlyList<ToolTarget>? targets = null, string run = "run-1",
                                           AgentRole agent = AgentRole.Planner) =>
        new(1, run, "T0", agent, tool, "1", "Test", JsonSerializer.SerializeToElement(args), targets ?? [], "snap-1",
            Guid.NewGuid().ToString("N"));

    private static ProposalRequest List(string run = "run-1") =>
        Request("File.List", new { grantId = "g-test", depth = 1 }, [new ToolTarget("t1", ".")], run);

    private static ProposalRequest Confirm(IReadOnlyList<string>? tools = null, string run = "run-1") =>
        Request(AutoApprove.Tool, new { tools = tools ?? Tools }, run: run);

    private static async Task<ProposalView> ConfirmAsync(ApprovalBroker broker, string run = "run-1")
    {
        var confirm = broker.Submit(Confirm(run: run));
        Assert.Equal(ProposalState.AwaitingApproval, confirm.State); // nie automatisch – immer eine Nutzerentscheidung
        broker.Decide(new UserDecision(confirm.Proposal.ProposalId, confirm.ProposalSha256, DecisionKind.Approve));
        Assert.True(await broker.DispatchAsync(confirm.Proposal.ProposalId));
        Assert.Equal(ProposalState.Succeeded, broker.Get(confirm.Proposal.ProposalId).State);
        return confirm;
    }

    [Fact, Trait("Pruefung", "autoApprove")]
    public async Task Erst_nach_Bestaetigung_im_Run_automatisch_genehmigt()
    {
        await using var broker = NewBroker();
        // Ohne Bestätigung: wie immer einzeln.
        Assert.Equal(ProposalState.AwaitingApproval, broker.Submit(List()).State);

        await ConfirmAsync(broker);
        Assert.Equal(Tools, broker.ActiveAutoApprove("run-1"));
        Assert.Equal(0, _adapter.Invocations); // die Bestätigung selbst führt nichts aus

        var list = broker.Submit(List());
        Assert.Equal(ProposalState.Approved, list.State);
        Assert.True(await broker.DispatchAsync(list.Proposal.ProposalId));
        Assert.Equal(ProposalState.Succeeded, broker.Get(list.Proposal.ProposalId).State);

        // Nicht eingeschaltetes Tool und anderer Run: weiterhin einzeln.
        var many = broker.Submit(Request("File.ReadMany", new { grantId = "g-test" }, [new ToolTarget("a", "a.txt"), new ToolTarget("b", "b.txt")]));
        Assert.Equal(ProposalState.AwaitingApproval, many.State);
        Assert.Equal(ProposalState.AwaitingApproval, broker.Submit(List("run-2")).State);
        // Weiterhin volle Prüfung: unzulässiger Pfad wird abgewiesen, nicht genehmigt.
        Assert.Throws<BrokerException>(() => broker.Submit(Request("File.List", new { grantId = "g-test" }, [new ToolTarget("t1", "../x")])));

        var audit = AuditCheck.Check(AuditJournal.ReadVerified(JournalPath));
        Assert.True(audit.Ok, string.Join("; ", audit.Problems));
        var entries = AuditJournal.ReadVerified(JournalPath);
        Assert.Contains(entries, e => e.Type == "autoapprove.granted");
        Assert.Contains(entries, e => e.Type == "decision" && e.Data["decision"]?.GetValue<string>() == "AutoApprove");
    }

    [Fact, Trait("Pruefung", "autoApprove")]
    public async Task Abgelehnt_oder_abgelaufen_bleibt_alles_einzeln()
    {
        await using var broker = NewBroker();
        var denied = broker.Submit(Confirm());
        broker.Decide(new UserDecision(denied.Proposal.ProposalId, denied.ProposalSha256, DecisionKind.Deny));
        Assert.Empty(broker.ActiveAutoApprove("run-1"));
        Assert.Equal(ProposalState.AwaitingApproval, broker.Submit(List()).State);

        broker.Submit(Confirm());
        _clock.Advance(TimeSpan.FromMinutes(10));
        broker.ExpireDue();
        Assert.Empty(broker.ActiveAutoApprove("run-1"));
        Assert.Equal(ProposalState.AwaitingApproval, broker.Submit(List()).State);
    }

    [Fact, Trait("Pruefung", "autoApprove")]
    public async Task Not_Aus_Abbruch_und_neue_Anfrage_beenden_die_Freigabe()
    {
        await using var broker = NewBroker();
        await ConfirmAsync(broker);
        broker.CancelProposals("run-1", AgentRole.Coder, "Agent abgebrochen"); // ein Agent: Freigabe des Runs bleibt
        Assert.NotEmpty(broker.ActiveAutoApprove("run-1"));
        broker.CancelProposals("run-1", null, "Run abgebrochen");
        Assert.Empty(broker.ActiveAutoApprove("run-1"));
        Assert.Equal(ProposalState.AwaitingApproval, broker.Submit(List()).State);

        await ConfirmAsync(broker);
        // Neue Bestätigungsanfrage (nächster Run/Ziel): die bisherige endet sofort, auch wenn die neue abgelehnt wird.
        var again = broker.Submit(Confirm());
        Assert.Empty(broker.ActiveAutoApprove("run-1"));
        broker.Decide(new UserDecision(again.Proposal.ProposalId, again.ProposalSha256, DecisionKind.Deny));
        Assert.Equal(ProposalState.AwaitingApproval, broker.Submit(List()).State);

        await ConfirmAsync(broker);
        broker.EmergencyStop();
        Assert.Empty(broker.ActiveAutoApprove("run-1"));
        broker.ReleaseEmergencyStop();
        Assert.Equal(ProposalState.AwaitingApproval, broker.Submit(List()).State);

        var audit = AuditCheck.Check(AuditJournal.ReadVerified(JournalPath));
        Assert.True(audit.Ok, string.Join("; ", audit.Problems));
    }

    [Fact, Trait("Pruefung", "autoApprove")]
    public async Task Neustart_beendet_die_Freigabe()
    {
        await using (var broker = NewBroker())
        {
            await ConfirmAsync(broker);
        }
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        await using var restarted = NewBroker();
        Assert.Empty(restarted.ActiveAutoApprove("run-1"));
        Assert.Equal(ProposalState.AwaitingApproval, restarted.Submit(List()).State);
    }

    [Fact, Trait("Pruefung", "autoApprove")]
    public async Task Bestaetigung_nur_fuer_genau_die_Liste_der_Projektdatei()
    {
        await using (var broker = NewBroker())
        {
            Assert.Equal(BrokerErrorCodes.Forbidden, Assert.Throws<BrokerException>(() => broker.Submit(Confirm(["File.List"]))).Code);
            Assert.Equal(BrokerErrorCodes.Forbidden,
                Assert.Throws<BrokerException>(() => broker.Submit(Confirm(["File.List", "File.Read", "Patch.Apply"]))).Code);
            Assert.Equal(BrokerErrorCodes.Forbidden,
                Assert.Throws<BrokerException>(() => broker.Submit(Confirm(["File.Read", "File.List"]))).Code);
        }
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        await using var off = NewBroker([]);
        Assert.Contains("nicht eingeschaltet", Assert.Throws<BrokerException>(() => off.Submit(Confirm())).Message, StringComparison.Ordinal);
    }

    [Theory, Trait("Pruefung", "autoApprove")]
    [InlineData("Workspace.Snapshot")]
    [InlineData("Workspace.Diff")]
    [InlineData("Workspace.ExportPatch")]
    [InlineData("Web.Fetch")]
    [InlineData("Web.Content")]
    [InlineData("OsDebug.Start")]
    [InlineData("Run.AutoApprove")]
    public void Projektdatei_mit_nie_erlaubtem_Tool_wird_abgewiesen(string tool)
    {
        var ex = Assert.Throws<InvalidDataException>(() => AutoApprove.Parse(new Dictionary<string, bool> { [tool] = true }));
        Assert.Contains("nie automatisch", ex.Message, StringComparison.Ordinal);
        // Ausdrücklich ausgeschaltet ist erlaubt (Dokumentation in der Projektdatei).
        Assert.Empty(AutoApprove.Parse(new Dictionary<string, bool> { [tool] = false }));
    }

    [Fact, Trait("Pruefung", "autoApprove")]
    public void Projektdatei_wird_gelesen_und_unbekannte_Tools_abgewiesen()
    {
        Assert.Empty(AutoApprove.Parse(null));
        Assert.Equal(["File.List", "Patch.Apply", "Toolchain.Test"], AutoApprove.Parse(new Dictionary<string, bool>
        {
            ["Toolchain.Test"] = true, ["File.List"] = true, ["Patch.Apply"] = true, ["File.Read"] = false,
        }));
        Assert.Contains("unbekanntes Tool", Assert.Throws<InvalidDataException>(() =>
            AutoApprove.Parse(new Dictionary<string, bool> { ["File.Delete"] = true })).Message, StringComparison.Ordinal);

        var project = ContractJson.Deserialize<ProjectConfig>("""
            {"schemaVersion":1,"projectId":"p","name":"P","grants":[],"autoApprove":{"File.List":true,"Workspace.Snapshot":false}}
            """);
        Assert.Equal(["File.List"], AutoApprove.Parse(project.AutoApprove));
    }

    [Fact, Trait("Pruefung", "autoApprove")]
    public async Task Audit_erkennt_gefaelschte_automatische_Freigabe()
    {
        await using (var broker = NewBroker())
        {
            await ConfirmAsync(broker);
            var list = broker.Submit(List());
            Assert.True(await broker.DispatchAsync(list.Proposal.ProposalId));
        }
        var entries = AuditJournal.ReadVerified(JournalPath);
        Assert.True(AuditCheck.Check(entries).Ok);
        // Ohne die Bestätigung (autoapprove.granted) ist die automatische Entscheidung ein Fund.
        var withoutGrant = entries.Where(e => e.Type != "autoapprove.granted").ToList();
        Assert.Contains(AuditCheck.Check(withoutGrant).Problems, p => p.Contains("ohne passende, bestätigte Liste", StringComparison.Ordinal));
        // Eine Bestätigung, die nie ausgeführt wurde, öffnet keine Freigabe.
        var forged = entries.Where(e => e.Type != "invocation.started" || e.Data["proposalId"]?.GetValue<string>() != Confirmed(entries)).ToList();
        Assert.Contains(AuditCheck.Check(forged).Problems, p => p.Contains("ohne bestätigte Liste", StringComparison.Ordinal));
    }

    private static string Confirmed(IReadOnlyList<JournalEntry> entries) =>
        entries.First(e => e.Type == "autoapprove.granted").Data["proposalId"]!.GetValue<string>();

    [Fact, Trait("Pruefung", "autoApprove")]
    public void Nie_erlaubte_und_erlaubte_Tools_sind_getrennt_und_bekannt()
    {
        Assert.Empty(AutoApprove.Eligible.Keys.Intersect(AutoApprove.Never.Keys));
        var policy = new ProposalPolicy(new BrokerOptions(), new TestPolicyContext());
        foreach (var tool in AutoApprove.Eligible.Keys.Concat(AutoApprove.Never.Keys))
        {
            Assert.Equal(tool, policy.GetTool(tool).Name); // jedes genannte Tool existiert
        }
    }
}
