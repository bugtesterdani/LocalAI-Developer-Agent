using System.Text.Json;
using System.Text.Json.Nodes;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// M9: Betriebssystem-Debugging – Freigabe einer Debug-Sitzung mit Grenzen, Befehle im Rahmen der Sitzung,
/// Audit-Prüfung, Not-Aus, Projektdatei und Gastauswahl.
/// </summary>
public sealed class M9Tests : IAsyncDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-m9-").FullName;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 10, 20, 8, 0, 0, TimeSpan.Zero));
    private readonly RecordingAdapter _adapter = new();

    public async ValueTask DisposeAsync()
    {
        await Task.Yield();
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        FileTools.DeleteTree(_dir);
    }

    private static readonly OsDebugConfig Config = new("os", "build/os.iso", OsDebugMedia.Cdrom, MaxCommands: 3, MaxMinutes: 10);

    private sealed class Context : IPolicyContext
    {
        public static readonly FolderGrant Grant = new("os", "/os", [FolderRight.List, FolderRight.Read, FolderRight.Execute], true);

        public IReadOnlyList<FolderGrant> Grants { get; } = [Grant];

        public FolderGrant? FindGrant(string grantId) => grantId == Grant.GrantId ? Grant : null;

        public bool HasSnapshot(string snapshotId, string grantId) => true;

        public OsDebugSettings? OsDebug { get; } = OsDebugSettings.FromProject(Config);
    }

    /// <summary>Tut so, als liefe QEMU: Start meldet die Sitzung, Befehle einen Zustand.</summary>
    private sealed class RecordingAdapter : IToolAdapter
    {
        public List<ToolProposal> Executed { get; } = [];

        public Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken)
        {
            lock (Executed)
            {
                Executed.Add(proposal);
            }
            var text = proposal.Tool == OsDebugTools.Start ? $$"""{"session":"{{proposal.ProposalId}}"}""" : """{"state":"angehalten"}""";
            return Task.FromResult(new ToolResult(invocationId, proposal.ProposalId, ResultStatus.Succeeded, proposal.SnapshotId,
                [new TargetResult("debug", ResultStatus.Succeeded, Text: text)], CanonicalJson.Sha256Hex(text), false, true));
        }
    }

    private ApprovalBroker NewBroker(string? journal = null)
    {
        var broker = new ApprovalBroker(new BrokerOptions(), _adapter, new AuditJournal(journal ?? Path.Combine(_dir, "audit.db"), _clock), _clock,
            new Context());
        broker.Start(runBackgroundLoops: false);
        return broker;
    }

    private static ProposalRequest Request(string tool, object args, string snapshot = "-", AgentRole agent = AgentRole.Debugger, string run = "run-1") =>
        new(1, run, "task-1", agent, tool, "1", "Bootvorgang untersuchen", JsonSerializer.SerializeToElement(args), [], snapshot,
            Guid.NewGuid().ToString("N"));

    private static object StartArgs(int maxCommands = 3, int maxMinutes = 10, bool allowInput = false, JsonElement? config = null) => new
    {
        grantId = "os",
        config = config ?? OsDebugSettings.FromProject(Config).WorkerConfig,
        maxCommands,
        maxMinutes,
        allowInput,
        haltAtStart = true,
    };

    private async Task<string> OpenSessionAsync(ApprovalBroker broker, bool allowInput = false)
    {
        var start = broker.Submit(Request(OsDebugTools.Start, StartArgs(allowInput: allowInput), "snap-1"));
        Assert.Equal(ProposalState.AwaitingApproval, start.State);
        broker.Decide(new UserDecision(start.Proposal.ProposalId, start.ProposalSha256, DecisionKind.Approve));
        Assert.True(await broker.DispatchAsync(start.Proposal.ProposalId));
        return start.Proposal.ProposalId;
    }

    private static object Cmd(string session, string command, object? extra = null)
    {
        var node = new JsonObject { ["sessionId"] = session, ["command"] = command };
        if (extra is not null)
        {
            foreach (var (k, v) in JsonSerializer.SerializeToNode(extra)!.AsObject())
            {
                node[k] = v?.DeepClone();
            }
        }
        return node;
    }

    [Fact, Trait("Pruefung", "M9-Sitzung")]
    public async Task Befehle_laufen_nur_im_Rahmen_der_freigegebenen_Sitzung()
    {
        await using var broker = NewBroker();
        // Ohne Sitzung: kein einziger Befehl.
        var none = Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command,
            Cmd("11111111-2222-3333-4444-555555555555", "registers"))));
        Assert.Equal(BrokerErrorCodes.Forbidden, none.Code);

        var session = await OpenSessionAsync(broker);
        Assert.Equal(1, broker.OpenDebugSessions);
        var cmd = broker.Submit(Request(OsDebugTools.Command, Cmd(session, "break", new { address = "0x7c00" })));
        Assert.Equal(ProposalState.Approved, cmd.State); // ohne Klick – aber als eigener Vorschlag
        Assert.True(await broker.DispatchAsync(cmd.Proposal.ProposalId));
        Assert.Equal(ProposalState.Succeeded, broker.Get(cmd.Proposal.ProposalId).State);
        Assert.Empty(broker.Pending());

        // Anderer Agent, anderer Run, Eingaben ohne allowInput: abgewiesen.
        Assert.Equal(BrokerErrorCodes.Forbidden,
            Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command, Cmd(session, "registers"), agent: AgentRole.Coder))).Code);
        Assert.Equal(BrokerErrorCodes.Forbidden,
            Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command, Cmd(session, "registers"), run: "run-2"))).Code);
        Assert.Contains("Tastatur", Assert.Throws<BrokerException>(() =>
            broker.Submit(Request(OsDebugTools.Command, Cmd(session, "keys", new { text = "a" })))).Message, StringComparison.Ordinal);

        // Budget: 3 Befehle.
        broker.Submit(Request(OsDebugTools.Command, Cmd(session, "step")));
        broker.Submit(Request(OsDebugTools.Command, Cmd(session, "registers")));
        var over = Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command, Cmd(session, "registers"))));
        Assert.Equal(BrokerErrorCodes.TooMany, over.Code);

        // Stopp ist immer möglich und schließt die Sitzung.
        var stop = broker.Submit(Request(OsDebugTools.Stop, new { sessionId = session }));
        Assert.Equal(ProposalState.Approved, stop.State);
        Assert.Equal(0, broker.OpenDebugSessions);
        Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Stop, new { sessionId = session })));

        var audit = AuditCheck.Check(AuditJournal.ReadVerified(Path.Combine(_dir, "audit.db")));
        Assert.True(audit.Ok, string.Join("; ", audit.Problems));
    }

    [Fact, Trait("Pruefung", "M9-Sitzung")]
    public async Task Sitzung_endet_nach_Dauer_und_mit_dem_Not_Aus()
    {
        await using var broker = NewBroker();
        var session = await OpenSessionAsync(broker, allowInput: true);
        Assert.Equal(ProposalState.Approved, broker.Submit(Request(OsDebugTools.Command, Cmd(session, "keys", new { text = "a" }))).State);
        _clock.Advance(TimeSpan.FromMinutes(11));
        Assert.Equal(BrokerErrorCodes.Gone, Assert.Throws<BrokerException>(() =>
            broker.Submit(Request(OsDebugTools.Command, Cmd(session, "registers")))).Code);

        var second = await OpenSessionAsync(broker);
        Assert.False(broker.StopClosedDebugSessions);
        broker.EmergencyStop();
        Assert.True(broker.StopClosedDebugSessions);
        broker.ReleaseEmergencyStop();
        Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command, Cmd(second, "registers"))));
    }

    [Fact, Trait("Pruefung", "M9-Sitzung")]
    public void Start_nur_mit_der_Einstellung_der_Projektdatei_und_innerhalb_der_Grenzen()
    {
        var broker = NewBroker();
        var other = JsonSerializer.SerializeToElement(new { image = "build/os.iso", media = "Cdrom", machine = "pc", memoryMB = 4096, cpus = 1,
            firmware = "Bios", accel = "Kvm" });
        Assert.Contains("osDebug der Projektdatei", Assert.Throws<BrokerException>(() =>
            broker.Submit(Request(OsDebugTools.Start, StartArgs(config: other), "snap-1"))).Message, StringComparison.Ordinal);
        Assert.Contains("Grenze aus osDebug", Assert.Throws<BrokerException>(() =>
            broker.Submit(Request(OsDebugTools.Start, StartArgs(maxCommands: 4), "snap-1"))).Message, StringComparison.Ordinal);
        Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Start, StartArgs(maxMinutes: 11), "snap-1")));
        Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command, Cmd("x", "registers"))));
        Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command,
            Cmd("11111111-2222-3333-4444-555555555555", "quit"))));
        Assert.Throws<BrokerException>(() => broker.Submit(Request(OsDebugTools.Command,
            Cmd("11111111-2222-3333-4444-555555555555", "memory", new { address = "0x7c00; quit" }))));
        _ = broker.DisposeAsync();
    }

    [Fact, Trait("Pruefung", "M9-Audit")]
    public async Task Audit_erkennt_Befehle_ohne_Sitzung_und_ueber_dem_Budget()
    {
        var path = Path.Combine(_dir, "audit.db");
        await using (var broker = NewBroker(path))
        {
            var session = await OpenSessionAsync(broker);
            broker.Submit(Request(OsDebugTools.Command, Cmd(session, "registers")));
        }
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        var entries = AuditJournal.ReadVerified(path).ToList();
        Assert.True(AuditCheck.Check(entries).Ok);
        var decision = entries.Last(e => e.Type == "decision" && e.Data["decision"]?.GetValue<string>() == "SessionGrant");
        var created = entries.Last(e => e.Type == "proposal.created");
        // Gefälschte Sitzungsfreigabe für einen Lesezugriff bzw. ohne Sitzung.
        var forged = new List<JournalEntry>(entries)
        {
            created with { Seq = 900, Data = new JsonObject { ["proposal"] = new JsonObject { ["proposalId"] = "p-x", ["tool"] = "File.Read" }, ["sha256"] = "s" } },
            decision with { Seq = 901, Data = new JsonObject { ["proposalId"] = "p-x", ["decision"] = "SessionGrant", ["sha256"] = "s", ["grant"] = "fehlt" } },
        };
        var report = AuditCheck.Check(forged);
        Assert.Contains(report.Problems, p => p.Contains("Sitzungsfreigabe für File.Read", StringComparison.Ordinal));
        // Sitzung, die nie freigegeben wurde.
        forged =
        [
            .. entries.Where(e => e.Type != "osdebug.granted"),
        ];
        Assert.Contains(AuditCheck.Check(forged).Problems, p => p.Contains("ohne offene, freigegebene Debug-Sitzung", StringComparison.Ordinal));
    }

    [Fact, Trait("Pruefung", "M9-Projekt")]
    public void Projektdatei_mit_osDebug_wird_geprueft_und_braucht_die_Linux_VM()
    {
        var root = Directory.CreateDirectory(Path.Combine(_dir, "danios")).FullName;
        var file = Path.Combine(_dir, "projekt.json");
        void Write(string extra, string rights = "\"List\", \"Read\", \"Execute\"") => File.WriteAllText(file, $$"""
            { "schemaVersion": 1, "projectId": "danios", "name": "DaniOS", "toolchains": ["cross-gcc"],
              "grants": [ { "grantId": "os", "root": "{{root.Replace("\\", "\\\\", StringComparison.Ordinal)}}", "rights": [{{rights}}], "recursive": true } ],
              "osDebug": { "grantId": "os", "image": "build/os.iso", "media": "Cdrom" {{extra}} } }
            """);
        Write(", \"build\": { \"toolchain\": \"cross-gcc\", \"action\": \"build\" }, \"maxCommands\": 800");
        var project = ProjectLoader.Load(file, _dir);
        var settings = OsDebugSettings.FromProject(project.OsDebug!);
        Assert.Equal(800, settings.MaxCommands);
        Assert.Equal(OsDebugSettings.DefaultMaxMinutes, settings.MaxMinutes);
        Assert.Equal("cross-gcc", settings.BuildToolchain);
        Assert.Equal("Tcg", settings.WorkerConfig.GetProperty("accel").GetString());

        var plan = GuestSelection.Resolve(project, GuestOs.Windows, _ => GuestOs.Linux);
        Assert.Equal([GuestOs.Windows, GuestOs.Linux], plan.Required);
        Assert.Equal(GuestOs.Linux, plan.OsDebug);
        var ex = Assert.Throws<InvalidDataException>(() => GuestSelection.Resolve(project, GuestOs.Windows, _ => GuestOs.Windows));
        Assert.Contains("osDebug.build.toolchain", ex.Message, StringComparison.Ordinal);

        Write(", \"build\": { \"toolchain\": \"fehlt\", \"action\": \"build\" }");
        Assert.Contains("nicht in toolchains", Assert.Throws<InvalidDataException>(() => ProjectLoader.Load(file, _dir)).Message, StringComparison.Ordinal);
        Write("", "\"List\", \"Read\"");
        Assert.Contains("Execute", Assert.Throws<InvalidDataException>(() => ProjectLoader.Load(file, _dir)).Message, StringComparison.Ordinal);
        Write(", \"image\": \"../geheim.iso\"");
        Assert.Contains("osDebug.image", Assert.Throws<InvalidDataException>(() => ProjectLoader.Load(file, _dir)).Message, StringComparison.Ordinal);
        Write(", \"memoryMB\": 99999");
        Assert.Contains("memoryMB", Assert.Throws<InvalidDataException>(() => ProjectLoader.Load(file, _dir)).Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "M9-Routing")]
    public void Debug_Werkzeuge_laufen_in_der_Linux_VM()
    {
        var plan = GuestSelection.Resolve(new ProjectConfig(1, "p", "p", [], [], GuestOs.Windows, OsDebug: Config), GuestOs.Windows, _ => null);
        Assert.Equal(GuestOs.Linux, plan.OsDebug);
        Assert.Equal([GuestOs.Windows, GuestOs.Linux], plan.Required);
    }
}
