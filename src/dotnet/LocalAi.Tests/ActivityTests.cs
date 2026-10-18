using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Desktop.Core;
using ActivityEvent = LocalAi.Contracts.ActivityEvent;
using ActivityKind = LocalAi.Contracts.ActivityKind;

namespace LocalAi.Tests;

/// <summary>M3/B17: Live-Aktivität im Broker und in der Oberfläche, Abbruch pro Agent.</summary>
public sealed class ActivityTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "localai-activity-" + Guid.NewGuid().ToString("N"));
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 10, 18, 9, 0, 0, TimeSpan.Zero));
    private readonly AuditJournal _journal;

    public ActivityTests()
    {
        Directory.CreateDirectory(_dir);
        _journal = new AuditJournal(Path.Combine(_dir, "audit.db"), _clock);
        _journal.OpenAndReplay();
    }

    private static ActivityEvent Event(string agent, ActivityKind kind, string? text = null, AgentState? state = null, string run = "run-1") =>
        new(0, "2000-01-01T00:00:00.000Z", run, agent, kind, Text: text, State: state);

    [Fact, Trait("Pruefung", "AC13")]
    public async Task Hub_vergibt_Reihenfolge_und_Zeit_selbst_und_kuerzt_Texte()
    {
        var hub = new ActivityHub(_journal, _clock);
        hub.AddUserChat("run-1", "Hallo");
        hub.Report(new ActivityReport("run-1", [
            Event("Planner", ActivityKind.State, "plant", AgentState.Generating) with { Seq = 999 },
            Event("Coder", ActivityKind.Output, new string('x', 50_000)),
            Event("Orchestrator", ActivityKind.Heartbeat),
        ]));
        var events = await hub.WaitAsync(0, TimeSpan.Zero, CancellationToken.None);
        Assert.Equal([1L, 2L, 3L], events.Select(e => e.Seq));
        Assert.Equal("User", events[0].Agent);
        Assert.All(events, e => Assert.Equal(ContractTime.ToContract(_clock.UtcNow), e.At));
        Assert.True(events[2].Text!.Length < ActivityHub.MaxTextChars + 20);
        Assert.DoesNotContain(events, e => e.Kind == ActivityKind.Heartbeat);
        Assert.Equal(_clock.UtcNow, hub.LastReport);
        Assert.Empty(await hub.WaitAsync(3, TimeSpan.FromMilliseconds(20), CancellationToken.None));
    }

    [Fact]
    public void Hub_lehnt_fremde_Absender_und_Ueberflutung_ab()
    {
        var hub = new ActivityHub(_journal, _clock);
        var user = Assert.Throws<BrokerException>(() => hub.Report(new ActivityReport("run-1", [Event("User", ActivityKind.Chat, "ich bin du")])));
        Assert.Equal(BrokerErrorCodes.Unprocessable, user.Code);
        var mixed = Assert.Throws<BrokerException>(() => hub.Report(new ActivityReport("run-1", [Event("Coder", ActivityKind.Chat, "x", run: "run-2")])));
        Assert.Equal(BrokerErrorCodes.Unprocessable, mixed.Code);
        var flood = Enumerable.Range(0, ActivityHub.MaxEventsPerReport + 1).Select(_ => Event("Coder", ActivityKind.Output, "t")).ToList();
        Assert.Equal(BrokerErrorCodes.TooMany, Assert.Throws<BrokerException>(() => hub.Report(new ActivityReport("run-1", flood))).Code);
    }

    [Fact]
    public void Zustaende_und_Chat_kommen_ins_Journal_Tokens_nicht()
    {
        var hub = new ActivityHub(_journal, _clock);
        hub.Report(new ActivityReport("run-1", [
            Event("Coder", ActivityKind.State, "arbeitet", AgentState.Generating),
            Event("Coder", ActivityKind.Output, "geheimer Tokenstrom"),
            Event("Coder", ActivityKind.Chat, "Patch fertig"),
        ]));
        _journal.Dispose();
        var entries = new AuditJournal(Path.Combine(_dir, "audit.db"), _clock).OpenAndReplay().Where(e => e.Type == "activity").ToList();
        Assert.Equal(["State", "Chat"], entries.Select(e => e.Data["kind"]!.GetValue<string>()));
        Assert.DoesNotContain(entries, e => e.Data.ToJsonString().Contains("Tokenstrom", StringComparison.Ordinal));
    }

    [Fact]
    public async Task Inbox_liefert_Auftraege_ab_Position_und_zaehlt_Lebenszeichen()
    {
        var inbox = new OrchestratorInbox(_clock);
        inbox.Post(OrchestratorCommandKind.Chat, text: "alt");
        var after = inbox.CurrentSeq;
        var waiting = inbox.WaitAsync(after, TimeSpan.FromSeconds(5), CancellationToken.None);
        inbox.Post(OrchestratorCommandKind.CancelAgent, agent: "Coder");
        var got = await waiting;
        Assert.Single(got);
        Assert.Equal(OrchestratorCommandKind.CancelAgent, got[0].Kind);
        Assert.Equal(_clock.UtcNow, inbox.LastPoll);
    }

    [Fact, Trait("Pruefung", "AC13")]
    public void Abbruch_eines_Agenten_trifft_nur_dessen_Vorschlaege()
    {
        var adapter = new CountingTestAdapter();
        var broker = new ApprovalBroker(new BrokerOptions(), adapter, _journal, _clock, new TestPolicyContext());
        broker.Start(runBackgroundLoops: false);
        ProposalView Submit(AgentRole agent, string run = "run-1") => broker.Submit(new ProposalRequest(1, run, "t", agent, "File.Read", "1", "x",
            JsonDocument.Parse("""{"grantId":"g-test"}""").RootElement.Clone(), [new ToolTarget("t1", "a.cs")], "snap", Guid.NewGuid().ToString("N")));
        var coder = Submit(AgentRole.Coder);
        var tester = Submit(AgentRole.Tester);
        var otherRun = Submit(AgentRole.Coder, "run-2");
        Assert.Equal(1, broker.CancelProposals("run-1", AgentRole.Coder, "Abbruch durch Nutzer"));
        Assert.Equal(ProposalState.Invalidated, broker.Get(coder.Proposal.ProposalId).State);
        Assert.Equal(ProposalState.AwaitingApproval, broker.Get(tester.Proposal.ProposalId).State);
        Assert.Equal(ProposalState.AwaitingApproval, broker.Get(otherRun.Proposal.ProposalId).State);
        Assert.Throws<BrokerException>(() => broker.Decide(new UserDecision(coder.Proposal.ProposalId, coder.ProposalSha256, DecisionKind.Approve)));
        Assert.Equal(1, broker.CancelProposals("run-1", null, "Orchestrator beendet"));
        Assert.Equal(0, adapter.Invocations);
    }

    // ------------------------------------------------------------------ Oberfläche

    [Fact, Trait("Pruefung", "AC13")]
    public void Agentenansicht_zeigt_Zustand_Watchdog_und_buendelt_Text()
    {
        var agent = new AgentViewModel("Coder");
        var changes = new List<string>();
        agent.PropertyChanged += (_, e) => changes.Add(e.PropertyName!);
        agent.Apply(Event("Coder", ActivityKind.State, "Schritt 1/20 · ~900 Token Eingabe", AgentState.Generating), "09:00:00");
        Assert.True(agent.CanCancel);
        for (var i = 0; i < 500; i++)
        {
            agent.Apply(Event("Coder", ActivityKind.Output, "tok "), "09:00:01");
        }
        Assert.DoesNotContain(nameof(AgentViewModel.OutputText), changes);
        agent.Publish();
        Assert.Single(changes, c => c == nameof(AgentViewModel.OutputText));
        Assert.Contains("── Schritt 1/20", agent.OutputText);
        Assert.EndsWith("tok ", agent.OutputText);
        agent.Apply(Event("Coder", ActivityKind.Warning, "Seit 45 s keine Tokens – hängt?") with { Stalled = true }, "09:01:00");
        Assert.True(agent.IsStalled);
        Assert.Contains("hängt?", agent.StateText);
        agent.Apply(Event("Coder", ActivityKind.Output, "weiter"), "09:01:05");
        Assert.False(agent.IsStalled);
        agent.Apply(Event("Coder", ActivityKind.Metric) with { Tokens = 120, TokensPerSecond = 31.5, ElapsedMs = 4200, Model = "qwen2.5-coder:14b" }, "09:01:06");
        Assert.Contains("31,5 Token/s", agent.MetricText);
        agent.Apply(Event("Coder", ActivityKind.State, "fertig", AgentState.Done), "09:02:00");
        Assert.False(agent.CanCancel);
        Assert.Equal("fertig", agent.StateText);
    }

    [Fact]
    public void Lange_Ausgaben_werden_vorne_gekuerzt()
    {
        var agent = new AgentViewModel("Planner");
        agent.Apply(Event("Planner", ActivityKind.Output, new string('a', AgentViewModel.MaxStreamChars)), "t");
        agent.Apply(Event("Planner", ActivityKind.Output, "ENDE"), "t");
        agent.Publish();
        Assert.True(agent.OutputText.Length <= AgentViewModel.MaxStreamChars + 1);
        Assert.EndsWith("ENDE", agent.OutputText);
    }

    [Fact, Trait("Pruefung", "AC13")]
    public void Puffer_stellt_hoechstens_einen_Auftrag_an_die_Oberflaeche()
    {
        var posted = new System.Collections.Concurrent.ConcurrentQueue<Action>();
        var applied = new List<int>();
        using var buffer = new ActivityBuffer(batch => applied.Add(batch.Count), posted.Enqueue, TimeSpan.FromMilliseconds(20));
        for (var i = 0; i < 1000; i++)
        {
            buffer.Enqueue([Event("Coder", ActivityKind.Output, "t")]);
        }
        for (var i = 0; i < 300 && posted.IsEmpty; i++)
        {
            Thread.Sleep(10);
        }
        Thread.Sleep(150); // weitere Takte: solange der UI-Thread nicht abgearbeitet hat, kein zweiter Auftrag
        Assert.Single(posted);
        posted.TryDequeue(out var drain);
        drain!();
        Assert.Equal([1000], applied);
    }

    [Fact]
    public async Task Chat_senden_und_Antworten_anzeigen()
    {
        var api = new ChatApi();
        await using var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"), throttle: false);
        Assert.False(main.SendChatCommand.CanExecute(null));
        main.ChatInput = "Ergänze einen CSV-Import";
        Assert.True(main.SendChatCommand.CanExecute(null));
        main.SendChatCommand.Execute(null);
        await WaitUntil(() => api.Sent.Count == 1);
        Assert.Equal("Ergänze einen CSV-Import", api.Sent[0]);
        Assert.Equal(string.Empty, main.ChatInput);
        main.ApplyActivity([
            Event("User", ActivityKind.Chat, "Ergänze einen CSV-Import"),
            Event("Planner", ActivityKind.Chat, "Plan: T1 · Coder · Parser"),
            Event("Coder", ActivityKind.Proposal, "Lesen src/a.cs – wartet auf Freigabe", AgentState.WaitingApproval),
        ]);
        Assert.Equal(["Du", "Planner"], main.Chat.Select(c => c.Sender));
        Assert.True(main.Chat[0].IsUser && main.HasChat);
        var coder = main.Agents.Single(a => a.Name == "Coder");
        Assert.Equal("wartet auf Freigabe", coder.StateText);
        main.SelectedAgent = coder;
        Assert.Equal(1, main.SelectedTab);
        coder.CancelCommand.Execute(null);
        await WaitUntil(() => api.Cancelled.Count == 1);
        Assert.Equal("Coder", api.Cancelled[0]);
    }

    [Fact]
    public async Task Chat_senden_mit_asynchronem_UI_Thread_wie_im_echten_Fenster()
    {
        // Wie Dispatcher.BeginInvoke: Aufträge laufen erst später, nicht sofort.
        var queue = new System.Collections.Concurrent.ConcurrentQueue<Action>();
        var api = new ChatApi();
        await using var main = new MainViewModel(api, api, queue.Enqueue, Guid.NewGuid().ToString("D"), throttle: false);
        main.ChatInput = "Hallo Agenten";
        main.SendChatCommand.Execute(null);
        await WaitUntil(() => api.Sent.Count == 1);
        Assert.Equal("Hallo Agenten", api.Sent[0]);
        Assert.Equal(string.Empty, main.ChatInput);
        api.Fail = true;
        main.ChatInput = "geht verloren?";
        main.SendChatCommand.Execute(null);
        for (var i = 0; i < 100 && main.ChatInput.Length == 0; i++)
        {
            while (queue.TryDequeue(out var action))
            {
                action();
            }
            await Task.Delay(10, TestContext.Current.CancellationToken);
        }
        Assert.Equal("geht verloren?", main.ChatInput);
        Assert.NotNull(main.ErrorText);
    }

    [Fact, Trait("Pruefung", "SEC-22")]
    public async Task Haengender_Orchestrator_wird_angezeigt_und_Not_Aus_sperrt_Chat()
    {
        var api = new ChatApi();
        await using var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"), throttle: false);
        main.ApplyEvents([new BrokerEvent(1, "2026-10-18T09:00:00.000Z", "orchestrator.unresponsive", Message: "Orchestrator meldet sich seit 31 s nicht – hängt?")]);
        Assert.True(main.IsOrchestratorProblem);
        Assert.Contains("hängt?", main.OrchestratorText);
        main.ApplyEvents([new BrokerEvent(2, "2026-10-18T09:00:01.000Z", "orchestrator.responsive", Message: "Orchestrator meldet sich wieder")]);
        Assert.False(main.IsOrchestratorProblem);
        main.ChatInput = "hallo";
        main.ApplyEvents([new BrokerEvent(3, "2026-10-18T09:00:02.000Z", "emergency.stop", Message: "Not-Aus ausgelöst")]);
        Assert.False(main.SendChatCommand.CanExecute(null));
    }

    private static async Task WaitUntil(Func<bool> condition)
    {
        for (var i = 0; i < 200 && !condition(); i++)
        {
            await Task.Delay(10);
        }
        Assert.True(condition());
    }

    public void Dispose()
    {
        _journal.Dispose();
        try
        {
            Directory.Delete(_dir, recursive: true);
        }
        catch (IOException)
        {
        }
    }

    private sealed class ChatApi : IBrokerUiApi
    {
        public List<string> Sent { get; } = [];
        public List<string> Cancelled { get; } = [];
        public Task<IReadOnlyList<ProposalView>> PendingAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<ProposalView>>([]);
        public Task<ProposalView> DecideAsync(UserDecision decision, CancellationToken ct) => throw new NotSupportedException();
        public Task<ProposalView> NarrowAsync(NarrowRequest request, CancellationToken ct) => throw new NotSupportedException();
        public Task<IReadOnlyList<BrokerEvent>> WaitEventsAsync(long afterSeq, int timeoutMs, CancellationToken ct) => Task.FromResult<IReadOnlyList<BrokerEvent>>([]);
        public Task EmergencyStopAsync(CancellationToken ct) => Task.CompletedTask;
        public Task ReleaseAsync(CancellationToken ct) => Task.CompletedTask;
        public Task<JsonElement> StartFakeAgentAsync(CancellationToken ct) => throw new NotSupportedException();
        public Task<IReadOnlyList<FolderGrant>> GrantsAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<FolderGrant>>([]);
        public Task<JsonElement> StatusAsync(CancellationToken ct) => Task.FromResult(JsonDocument.Parse("{}").RootElement.Clone());
        public Task<IReadOnlyList<ToolchainInfo>> ScanToolchainsAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<ToolchainInfo>>([]);
        public Task RegisterToolchainAsync(string directory, string digest, CancellationToken ct) => Task.CompletedTask;
        public Task UnregisterToolchainAsync(string name, CancellationToken ct) => Task.CompletedTask;
        public bool Fail { get; set; }
        public Task<string> ChatSendAsync(string text, CancellationToken ct)
        {
            if (Fail)
            {
                throw new BrokerCallException(409, "Not-Aus ist aktiv – erst aufheben");
            }
            Sent.Add(text);
            return Task.FromResult("run-1");
        }
        public Task AgentCancelAsync(string agent, CancellationToken ct)
        {
            Cancelled.Add(agent);
            return Task.CompletedTask;
        }
        public Task RunCancelAsync(CancellationToken ct) => Task.CompletedTask;
        public Task<IReadOnlyList<ActivityEvent>> WaitActivityAsync(long afterSeq, int timeoutMs, CancellationToken ct) =>
            Task.FromResult<IReadOnlyList<ActivityEvent>>([]);
        public Task OrchestratorStopAsync(CancellationToken ct) => Task.CompletedTask;
        public int Rebuilds { get; private set; }
        public Task RebuildSandboxAsync(CancellationToken ct)
        {
            Rebuilds++;
            return Task.CompletedTask;
        }
    }
}
