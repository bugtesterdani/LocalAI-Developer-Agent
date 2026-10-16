using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Tests;

/// <summary>Broker-Tests im selben Prozess mit einstellbarer Uhr (deterministisch).</summary>
public sealed class BrokerTests : IAsyncDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "localai-tests-" + Guid.NewGuid().ToString("N"));
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 10, 16, 12, 0, 0, TimeSpan.Zero));
    private readonly CountingTestAdapter _adapter = new();
    private ApprovalBroker _broker;

    public BrokerTests()
    {
        Directory.CreateDirectory(_dir);
        _broker = NewBroker();
    }

    private string JournalPath => Path.Combine(_dir, "audit.jsonl");

    private ApprovalBroker NewBroker(bool start = true)
    {
        var broker = new ApprovalBroker(new BrokerOptions(), _adapter, new AuditJournal(JournalPath, _clock), _clock);
        if (start)
        {
            broker.Start(runBackgroundLoops: false);
        }
        return broker;
    }

    private static ProposalRequest Request(string tool = "File.Read", int targets = 1, string? key = null) => new(
        SchemaVersion: 1,
        RunId: "run-1",
        TaskId: "task-1",
        Agent: AgentRole.Coder,
        Tool: tool,
        ToolVersion: "1",
        Purpose: "Test",
        Arguments: JsonDocument.Parse("{}").RootElement.Clone(),
        Targets: Enumerable.Range(1, targets).Select(i => new ToolTarget($"t{i}", $"src/Datei{i}.cs")).ToList(),
        SnapshotId: "snap-1",
        IdempotencyKey: key ?? Guid.NewGuid().ToString("N"));

    private ProposalView Approve(ProposalView view) =>
        _broker.Decide(new UserDecision(view.Proposal.ProposalId, view.ProposalSha256, DecisionKind.Approve));

    [Fact, Trait("Pruefung", "SEC-01")]
    public async Task Ohne_Entscheidung_keine_Ausfuehrung()
    {
        var view = _broker.Submit(Request());
        Assert.False(await _broker.DispatchAsync(view.Proposal.ProposalId));
        _clock.Advance(TimeSpan.FromMinutes(6));
        _broker.ExpireDue();
        Assert.Equal(ProposalState.Expired, _broker.Get(view.Proposal.ProposalId).State);
        var ex = Assert.Throws<BrokerException>(() => Approve(view));
        Assert.Equal(BrokerErrorCodes.Gone, ex.Code);
        Assert.Equal(0, _adapter.Invocations);
    }

    [Fact, Trait("Pruefung", "SEC-01")]
    public void Ablehnung_fuehrt_nichts_aus()
    {
        var view = _broker.Submit(Request());
        _broker.Decide(new UserDecision(view.Proposal.ProposalId, view.ProposalSha256, DecisionKind.Deny));
        Assert.Equal(ProposalState.Denied, _broker.Get(view.Proposal.ProposalId).State);
        Assert.Equal(0, _adapter.Invocations);
    }

    [Fact, Trait("Pruefung", "SEC-02")]
    public void Falscher_Hash_wird_abgelehnt()
    {
        var a = _broker.Submit(Request());
        var b = _broker.Submit(Request());
        var ex = Assert.Throws<BrokerException>(() =>
            _broker.Decide(new UserDecision(a.Proposal.ProposalId, b.ProposalSha256, DecisionKind.Approve)));
        Assert.Equal(BrokerErrorCodes.Conflict, ex.Code);
        Assert.Equal(ProposalState.AwaitingApproval, _broker.Get(a.Proposal.ProposalId).State);
    }

    [Fact, Trait("Pruefung", "SEC-02")]
    public void Idempotenzschluessel_kann_keine_Parameter_aendern()
    {
        _broker.Submit(Request(key: "k1"));
        var changed = Request(key: "k1") with { Purpose = "anderer Zweck" };
        var ex = Assert.Throws<BrokerException>(() => _broker.Submit(changed));
        Assert.Equal(BrokerErrorCodes.Conflict, ex.Code);
    }

    [Fact, Trait("Pruefung", "SEC-03")]
    public async Task Parallele_Dispatches_fuehren_genau_einmal_aus()
    {
        var view = Approve(_broker.Submit(Request()));
        var id = view.Proposal.ProposalId;
        var results = await Task.WhenAll(Enumerable.Range(0, 32).Select(_ => Task.Run(() => _broker.DispatchAsync(id))));
        Assert.Equal(1, results.Count(r => r));
        Assert.Equal(1, _adapter.Invocations);
        Assert.Equal(ProposalState.Succeeded, _broker.Get(id).State);
        Assert.False(await _broker.DispatchAsync(id));
        Assert.Equal(1, _adapter.Invocations);
    }

    [Fact, Trait("Pruefung", "SEC-03")]
    public void Zweite_Entscheidung_wird_abgelehnt()
    {
        var view = _broker.Submit(Request());
        Approve(view);
        var ex = Assert.Throws<BrokerException>(() => Approve(view));
        Assert.Equal(BrokerErrorCodes.Conflict, ex.Code);
    }

    [Fact, Trait("Pruefung", "SEC-12")]
    public async Task Neustart_invalidiert_alte_Freigaben_ohne_Wiederholung()
    {
        var pending = _broker.Submit(Request());
        var approved = Approve(_broker.Submit(Request()));
        await _broker.DisposeAsync();

        _broker = NewBroker();
        Assert.Equal(ProposalState.Invalidated, _broker.Get(pending.Proposal.ProposalId).State);
        Assert.Equal(ProposalState.Invalidated, _broker.Get(approved.Proposal.ProposalId).State);
        Assert.False(await _broker.DispatchAsync(approved.Proposal.ProposalId));
        var ex = Assert.Throws<BrokerException>(() => Approve(pending));
        Assert.Equal(BrokerErrorCodes.Conflict, ex.Code);
        Assert.Equal(0, _adapter.Invocations);
    }

    [Fact, Trait("Pruefung", "SEC-23")]
    public async Task Sammelfreigabe_Abwahl_fuehrt_nur_ausgewaehlte_Ziele_aus()
    {
        var original = _broker.Submit(Request("File.ReadMany", 5));
        var narrowed = _broker.Narrow(new NarrowRequest(original.Proposal.ProposalId, original.ProposalSha256, ["t1", "t3", "t5"]));
        Assert.True(narrowed.ProposalSha256 != original.ProposalSha256, "neuer Hash");
        Assert.Equal(original.Proposal.ProposalId, narrowed.Proposal.ParentProposalId);
        Assert.Equal(ProposalState.Superseded, _broker.Get(original.Proposal.ProposalId).State);

        // Alte Fassung kann nicht mehr freigegeben werden.
        Assert.Equal(BrokerErrorCodes.Conflict, Assert.Throws<BrokerException>(() => Approve(original)).Code);

        Approve(narrowed);
        Assert.True(await _broker.DispatchAsync(narrowed.Proposal.ProposalId), "Dispatch");
        var executed = _adapter.ExecutedTargets.Select(t => t.TargetId).OrderBy(x => x).ToArray();
        Assert.Equal("t1,t3,t5", string.Join(',', executed));

        var outcome = _broker.Outcome(original.Proposal.ProposalId);
        Assert.Equal(narrowed.Proposal.ProposalId, outcome.FinalProposalId);
        Assert.Equal("t2,t4", string.Join(',', outcome.Result!.SkippedTargetIds!));
    }

    [Fact, Trait("Pruefung", "SEC-23")]
    public void Sammelfreigabe_kann_nicht_erweitert_werden()
    {
        var original = _broker.Submit(Request("File.ReadMany", 3));
        var ex = Assert.Throws<BrokerException>(() =>
            _broker.Narrow(new NarrowRequest(original.Proposal.ProposalId, original.ProposalSha256, ["t1", "t99"])));
        Assert.Equal(BrokerErrorCodes.Unprocessable, ex.Code);
        Assert.Equal(BrokerErrorCodes.Unprocessable, Assert.Throws<BrokerException>(() =>
            _broker.Narrow(new NarrowRequest(original.Proposal.ProposalId, original.ProposalSha256, []))).Code);
        Assert.Equal(BrokerErrorCodes.Conflict, Assert.Throws<BrokerException>(() =>
            _broker.Narrow(new NarrowRequest(original.Proposal.ProposalId, original.ProposalSha256, ["t1", "t2", "t3"]))).Code);
    }

    [Fact]
    public void Einzel_Read_unterstuetzt_keine_Abwahl_und_nur_ein_Ziel()
    {
        Assert.Equal(BrokerErrorCodes.Unprocessable, Assert.Throws<BrokerException>(() => _broker.Submit(Request("File.Read", 2))).Code);
        Assert.Equal(BrokerErrorCodes.TooMany, Assert.Throws<BrokerException>(() => _broker.Submit(Request("File.ReadMany", 201))).Code);
        Assert.Equal(BrokerErrorCodes.Forbidden, Assert.Throws<BrokerException>(() => _broker.Submit(Request("Shell.Execute"))).Code);
    }

    [Fact, Trait("Pruefung", "SEC-15")]
    public async Task NotAus_invalidiert_und_blockiert()
    {
        var pending = _broker.Submit(Request());
        var approved = Approve(_broker.Submit(Request()));
        _broker.EmergencyStop();
        Assert.Equal(ProposalState.Invalidated, _broker.Get(pending.Proposal.ProposalId).State);
        Assert.False(await _broker.DispatchAsync(approved.Proposal.ProposalId));
        Assert.Equal(BrokerErrorCodes.Conflict, Assert.Throws<BrokerException>(() => _broker.Submit(Request())).Code);
        _broker.ReleaseEmergencyStop();
        _broker.Submit(Request());
        Assert.Equal(0, _adapter.Invocations);
    }

    [Fact]
    public async Task Journal_Manipulation_wird_erkannt()
    {
        _broker.Submit(Request());
        await _broker.DisposeAsync();
        Assert.True(AuditJournal.Verify(JournalPath) >= 2, "Journal hat Einträge");

        var lines = File.ReadAllLines(JournalPath);
        lines[1] = lines[1].Replace("\"Test\"", "\"Manipuliert\"");
        File.WriteAllLines(JournalPath, lines);
        Assert.Throws<AuditTamperException>(() => AuditJournal.Verify(JournalPath));
        _broker = NewBroker(start: false);
        Assert.Throws<AuditTamperException>(() => _broker.Start(runBackgroundLoops: false));
    }

    [Fact]
    public async Task Unvollstaendige_letzte_Journalzeile_wird_toleriert()
    {
        _broker.Submit(Request());
        await _broker.DisposeAsync();
        File.AppendAllText(JournalPath, "{\"seq\":99,\"halbe Zeile");
        _broker = NewBroker();
        Assert.True(AuditJournal.Verify(JournalPath) >= 3, "Journal nach Wiederanlauf gültig");
    }

    public async ValueTask DisposeAsync()
    {
        await _broker.DisposeAsync();
        try
        {
            Directory.Delete(_dir, recursive: true);
        }
        catch (IOException)
        {
        }
    }
}
