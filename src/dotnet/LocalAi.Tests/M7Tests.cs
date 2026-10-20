using System.IO.Compression;
using System.Text.Json;
using System.Text.Json.Nodes;
using LocalAi.Broker;
using LocalAi.Broker.Host;
using LocalAi.Contracts;
using LocalAi.Desktop.Core;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// M7: Audit-Prüfung (AC03), randomisierte Freigabeinvarianten (Spezifikation §15), Versions-/Offline-Manifest (AC01),
/// Diagnosepaket und Sicherung (Runbook).
/// </summary>
public sealed class M7Tests : IAsyncDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-m7-").FullName;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 10, 19, 12, 0, 0, TimeSpan.Zero));
    private readonly CountingTestAdapter _adapter = new();

    public async ValueTask DisposeAsync()
    {
        await Task.Yield();
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        FileTools.DeleteTree(_dir);
    }

    private string JournalPath => Path.Combine(_dir, "audit.db");

    private ApprovalBroker NewBroker()
    {
        var broker = new ApprovalBroker(new BrokerOptions(), _adapter, new AuditJournal(JournalPath, _clock), _clock, new TestPolicyContext());
        broker.Start(runBackgroundLoops: false);
        return broker;
    }

    private static ProposalRequest Request(Random rng, string? tool = null)
    {
        var count = rng.Next(1, 6);
        tool ??= rng.Next(3) switch { 0 => "File.Read", 1 => "File.ReadMany", _ => "File.List" };
        if (tool != "File.ReadMany")
        {
            count = 1;
        }
        return new ProposalRequest(1, "run-1", $"task-{rng.Next(4)}", (AgentRole)rng.Next(5), tool, "1", $"Zweck {rng.Next(1000)}",
            JsonDocument.Parse("""{"grantId":"g-test"}""").RootElement.Clone(),
            Enumerable.Range(1, count).Select(i => new ToolTarget($"t{i}", $"src/D{rng.Next(100)}_{i}.cs")).ToList(),
            "snap-1", Guid.NewGuid().ToString("N"));
    }

    // ------------------------------------------------------------------ AC03 / §15: Invarianten unter Zufallsfolgen

    [Theory, Trait("Pruefung", "AC03"), Trait("Pruefung", "SEC-03"), Trait("Pruefung", "SEC-12")]
    [InlineData(1)]
    [InlineData(2)]
    [InlineData(3)]
    [InlineData(4)]
    [InlineData(5)]
    public async Task Zufaellige_Ablaeufe_halten_die_Freigabeinvarianten(int seed)
    {
        var rng = new Random(seed);
        var broker = NewBroker();
        var views = new List<ProposalView>();
        var approvedShas = new HashSet<string>(StringComparer.Ordinal);
        try
        {
            for (var step = 0; step < 300; step++)
            {
                var op = rng.Next(100);
                try
                {
                    if (op < 25 || views.Count == 0)
                    {
                        views.Add(broker.Submit(Request(rng)));
                    }
                    else if (op < 45)
                    {
                        var v = views[rng.Next(views.Count)];
                        // Manchmal mit falschem Hash (SEC-02): muss scheitern.
                        var sha = rng.Next(5) == 0 ? new string('0', 64) : v.ProposalSha256;
                        var decided = broker.Decide(new UserDecision(v.Proposal.ProposalId, sha, rng.Next(4) == 0 ? DecisionKind.Deny : DecisionKind.Approve));
                        Assert.Equal(v.ProposalSha256, sha);
                        if (decided.State == ProposalState.Approved)
                        {
                            approvedShas.Add(sha);
                        }
                    }
                    else if (op < 55)
                    {
                        var v = views[rng.Next(views.Count)];
                        var keep = v.Proposal.Targets.Where(_ => rng.Next(2) == 0).Select(t => t.TargetId).ToList();
                        if (rng.Next(6) == 0)
                        {
                            keep.Add("t99"); // Erweiterung (SEC-23): muss scheitern
                        }
                        var narrowed = broker.Narrow(new NarrowRequest(v.Proposal.ProposalId, v.ProposalSha256, keep));
                        Assert.DoesNotContain("t99", keep);
                        Assert.True(narrowed.Proposal.Targets.All(t => v.Proposal.Targets.Any(o => o.Path == t.Path)), "nur Teilmenge");
                        views.Add(narrowed);
                    }
                    else if (op < 80)
                    {
                        // Mehrere Dispatcher gleichzeitig (SEC-03)
                        var v = views[rng.Next(views.Count)];
                        await Task.WhenAll(Enumerable.Range(0, rng.Next(1, 4)).Select(_ => broker.DispatchAsync(v.Proposal.ProposalId)));
                    }
                    else if (op < 88)
                    {
                        _clock.Advance(TimeSpan.FromMinutes(rng.Next(0, 7)));
                        broker.ExpireDue();
                    }
                    else if (op < 93)
                    {
                        broker.EmergencyStop();
                        broker.ReleaseEmergencyStop();
                    }
                    else
                    {
                        // Neustart (SEC-12): alte Freigaben ungültig
                        await broker.DisposeAsync();
                        broker = NewBroker();
                    }
                }
                catch (BrokerException)
                {
                }
            }
        }
        finally
        {
            await broker.DisposeAsync();
        }
        var report = AuditCheck.Verify(JournalPath);
        Assert.True(report.Ok, string.Join("; ", report.Problems));
        Assert.Equal(_adapter.Invocations, report.Invocations);
        Assert.True(report.Invocations <= report.Approvals);
        Assert.True(report.Sessions >= 1);
    }

    [Fact, Trait("Pruefung", "AC03")]
    public void Audit_Pruefung_findet_Ausfuehrung_ohne_Erlaubnis()
    {
        JournalEntry E(long seq, string type, JsonObject data) => new(seq, "2026-10-19T12:00:00Z", type, data, "", "");
        var created = new JsonObject { ["proposal"] = new JsonObject { ["proposalId"] = "p1" }, ["sha256"] = "aa" };
        var ok = AuditCheck.Check([
            E(1, "broker.started", new JsonObject()), E(2, "proposal.created", created),
            E(3, "decision", new JsonObject { ["proposalId"] = "p1", ["decision"] = "Approve", ["sha256"] = "aa" }),
            E(4, "invocation.started", new JsonObject { ["proposalId"] = "p1" }),
        ]);
        Assert.True(ok.Ok);

        var missing = AuditCheck.Check([E(1, "broker.started", new JsonObject()), E(2, "proposal.created", created.DeepClone().AsObject()),
            E(3, "invocation.started", new JsonObject { ["proposalId"] = "p1" })]);
        Assert.Contains(missing.Problems, p => p.Contains("ohne genau eine passende Erlaubnis", StringComparison.Ordinal));

        var twice = AuditCheck.Check([E(1, "broker.started", new JsonObject()), E(2, "proposal.created", created.DeepClone().AsObject()),
            E(3, "decision", new JsonObject { ["proposalId"] = "p1", ["decision"] = "Approve", ["sha256"] = "aa" }),
            E(4, "invocation.started", new JsonObject { ["proposalId"] = "p1" }), E(5, "invocation.started", new JsonObject { ["proposalId"] = "p1" })]);
        Assert.Contains(twice.Problems, p => p.Contains("mehr als einmal", StringComparison.Ordinal));

        var otherSession = AuditCheck.Check([E(1, "broker.started", new JsonObject()), E(2, "proposal.created", created.DeepClone().AsObject()),
            E(3, "decision", new JsonObject { ["proposalId"] = "p1", ["decision"] = "Approve", ["sha256"] = "aa" }),
            E(4, "broker.started", new JsonObject()), E(5, "invocation.started", new JsonObject { ["proposalId"] = "p1" })]);
        Assert.Contains(otherSession.Problems, p => p.Contains("früheren Broker-Sitzung", StringComparison.Ordinal));

        var wrongHash = AuditCheck.Check([E(1, "broker.started", new JsonObject()), E(2, "proposal.created", created.DeepClone().AsObject()),
            E(3, "decision", new JsonObject { ["proposalId"] = "p1", ["decision"] = "Approve", ["sha256"] = "bb" }),
            E(4, "invocation.started", new JsonObject { ["proposalId"] = "p1" })]);
        Assert.Contains(wrongHash.Problems, p => p.Contains("abweichendem Hash", StringComparison.Ordinal));
    }

    [Fact, Trait("Pruefung", "SEC-02")]
    public void Jede_Aenderung_eines_Vorschlags_aendert_den_Hash()
    {
        var rng = new Random(7);
        for (var i = 0; i < 200; i++)
        {
            var req = Request(rng);
            var p = new ToolProposal(1, "id", req.RunId, req.TaskId, req.Agent, req.Tool, req.ToolVersion, req.Purpose, req.Arguments, req.Targets,
                req.SnapshotId, 1, "sbx", "2026-10-19T12:00:00Z", "2026-10-19T12:05:00Z");
            var sha = ProposalHashing.Sha256(p);
            ToolProposal[] variants =
            [
                p with { Tool = p.Tool == "File.Read" ? "File.List" : "File.Read" },
                p with { Purpose = p.Purpose + "x" },
                p with { Arguments = JsonDocument.Parse("""{"grantId":"g-andere"}""").RootElement.Clone() },
                p with { Targets = [.. p.Targets.Select((t, j) => j == 0 ? t with { Path = t.Path + "x" } : t)] },
                p with { Targets = [.. p.Targets, new ToolTarget("t9", "neu.cs")] },
                p with { SnapshotId = "snap-2" },
                p with { Agent = (AgentRole)(((int)p.Agent + 1) % 5) },
                p with { ExpiresAt = "2026-10-19T12:06:00Z" },
            ];
            Assert.All(variants, v => Assert.NotEqual(sha, ProposalHashing.Sha256(v)));
        }
    }

    // ------------------------------------------------------------------ AC05 / SEC-08 / SEC-14: Injection-Korpus

    public static TheoryData<string> Korpus()
    {
        var file = Path.Combine(ContractTests.RepoRoot(), "tests", "korpus", "injection.json");
        var data = new TheoryData<string>();
        foreach (var e in JsonNode.Parse(File.ReadAllText(file))!["eintraege"]!.AsArray())
        {
            data.Add(e!["id"]!.GetValue<string>());
        }
        return data;
    }

    private static string KorpusText(string id)
    {
        var file = Path.Combine(ContractTests.RepoRoot(), "tests", "korpus", "injection.json");
        return JsonNode.Parse(File.ReadAllText(file))!["eintraege"]!.AsArray().First(e => e!["id"]!.GetValue<string>() == id)!["text"]!.GetValue<string>();
    }

    [Theory, Trait("Pruefung", "AC05"), Trait("Pruefung", "SEC-14")]
    [MemberData(nameof(Korpus))]
    public void Korpus_wird_inert_angezeigt(string id)
    {
        var shown = InertText.Clean(KorpusText(id), 2000);
        Assert.DoesNotContain('\u001b', shown);
        Assert.DoesNotContain('\u202e', shown);
        Assert.DoesNotContain('\u200b', shown);
    }

    [Fact, Trait("Pruefung", "AC05"), Trait("Pruefung", "SEC-08")]
    public void Korpus_kann_keine_Werkzeuge_oder_Rechte_erzeugen()
    {
        var project = new ProjectConfig(1, "p", "P", [new FolderGrant("g", "/p", [FolderRight.List, FolderRight.Read], true)]);
        var policy = new ProposalPolicy(new BrokerOptions(), new ProjectPolicyContext(project, new SnapshotRegistry()));
        var rng = new Random(1);
        foreach (var tool in new[] { "Shell.Execute", "Approvals.Decide", "Grant.Update", "File.Delete" })
        {
            var request = Request(rng, "File.Read") with { Tool = tool };
            Assert.Throws<BrokerException>(() => policy.Validate(request));
        }
        // Rechte stammen nur aus der Projektdatei: ein Vorschlag kann Delete nicht mitbringen.
        var write = Request(rng, "Patch.Apply") with
        {
            Arguments = JsonSerializer.SerializeToElement(new { grantId = "g", patch = "--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", rights = new[] { "Delete" } }),
            Targets = [new ToolTarget("t1", "x")],
        };
        Assert.Throws<BrokerException>(() => policy.Validate(write));
    }

    // ------------------------------------------------------------------ Manifest, Diagnose, Sicherung (Runbook)

    private HostOptions Options(string? configPath = null) => new()
    {
        DataDirectory = Path.Combine(_dir, "data"), RepoRoot = ContractTests.RepoRoot(), Mode = SandboxMode.HyperV, ConfigPath = configPath,
        HyperV = new HyperVSettings { ImagePath = Path.Combine(_dir, "fehlt.iso"), VmDirectory = Path.Combine(_dir, "vm") },
        Web = new WebSettings { Command = new ProcessCommand("x", [], _dir) },
    };

    [Fact, Trait("Pruefung", "AC01")]
    public void Manifest_enthaelt_Versionen_und_meldet_fehlende_Offline_Voraussetzungen()
    {
        var manifest = VersionManifest.Build(Options(), null, ["dotnet"]);
        Assert.Equal("0.10.0", manifest["version"]!.GetValue<string>());
        Assert.NotNull(manifest["contractsSchemaSha256"]);
        Assert.Contains(manifest["prompts"]!.AsArray(), p => p!["name"]!.GetValue<string>() == "common" && p["version"]!.GetValue<int>() >= 4);
        var checks = manifest["offline"]!.AsArray();
        Assert.Contains(checks, c => c!["item"]!.GetValue<string>().StartsWith("Windows-Abbild", StringComparison.Ordinal) && !c["ok"]!.GetValue<bool>());
        Assert.Contains(checks, c => c!["item"]!.GetValue<string>() == "Toolchain dotnet" && c["detail"]!.GetValue<string>().Contains("nicht registriert", StringComparison.Ordinal));
        Assert.False(manifest["complete"]!.GetValue<bool>());

        var json = JsonSerializer.SerializeToElement(manifest);
        var (text, problems) = SystemReport.FormatManifest(json);
        Assert.Contains("Local AI Developer Agent 0.10.0", text, StringComparison.Ordinal);
        Assert.Contains("Windows-Abbild", problems, StringComparison.Ordinal);
        Directory.CreateDirectory(Path.Combine(_dir, "data"));
        VersionManifest.Write(manifest, Path.Combine(_dir, "data"));
        Assert.True(File.Exists(Path.Combine(_dir, "data", "manifest.json")));
    }

    [Fact]
    public async Task Diagnosepaket_ohne_Inhalte_und_Protokolle_nur_auf_Wunsch()
    {
        var data = Directory.CreateDirectory(Path.Combine(_dir, "data")).FullName;
        var broker = new ApprovalBroker(new BrokerOptions(), _adapter, new AuditJournal(Path.Combine(data, "audit.db"), _clock), _clock, new TestPolicyContext());
        broker.Start(runBackgroundLoops: false);
        var secret = Request(new Random(1), "File.Read") with { Purpose = "GEHEIMER-ZWECK", Arguments = JsonDocument.Parse("""{"grantId":"g-test","x":"GEHEIM-ARG"}""").RootElement.Clone() };
        try
        {
            broker.Submit(secret);
        }
        catch (BrokerException)
        {
            broker.Submit(secret with { Arguments = JsonDocument.Parse("""{"grantId":"g-test"}""").RootElement.Clone() });
        }
        File.WriteAllText(Path.Combine(data, "hyperv.log"), "LOGZEILE");
        var events = await broker.WaitEventsAsync(0, TimeSpan.Zero, TestContext.Current.CancellationToken);
        await broker.DisposeAsync();
        var audit = AuditCheck.Verify(Path.Combine(data, "audit.db"));
        var manifest = VersionManifest.Build(Options(), null, []);

        var file = DiagnosticsExport.Create(data, manifest, audit, null, new Dictionary<string, IReadOnlyDictionary<string, int>>
        {
            ["File.Read"] = new Dictionary<string, int> { ["AwaitingApproval"] = 1 },
        }, events, includeLogs: false, _clock.UtcNow);
        var all = ReadZip(file);
        Assert.Contains("manifest.json", all.Keys);
        Assert.Contains("audit.json", all.Keys);
        Assert.Contains("ereignisse.json", all.Keys);
        Assert.DoesNotContain(all.Keys, k => k.StartsWith("protokolle/", StringComparison.Ordinal));
        Assert.DoesNotContain(all.Values, v => v.Contains("GEHEIMER-ZWECK", StringComparison.Ordinal) || v.Contains("GEHEIM-ARG", StringComparison.Ordinal));

        var withLogs = ReadZip(DiagnosticsExport.Create(data, manifest, audit, null, new Dictionary<string, IReadOnlyDictionary<string, int>>(), events,
            includeLogs: true, _clock.UtcNow.AddSeconds(1)));
        Assert.Equal("LOGZEILE", withLogs["protokolle/hyperv.log"]);
    }

    [Fact]
    public async Task Sicherung_und_Wiederherstellung_ohne_Freigaben()
    {
        var data = Directory.CreateDirectory(Path.Combine(_dir, "data")).FullName;
        var config = Path.Combine(_dir, "localai.json");
        File.WriteAllText(config, """{"sandbox":{"mode":"none"}}""");
        File.WriteAllText(Path.Combine(data, "toolchains.json"), "[]");
        File.WriteAllText(Path.Combine(data, "run-journal.json"), """{"goal":"x"}""");
        var broker = new ApprovalBroker(new BrokerOptions(), _adapter, new AuditJournal(Path.Combine(data, "audit.db"), _clock), _clock, new TestPolicyContext());
        broker.Start(runBackgroundLoops: false);
        var rng = new Random(3);
        var approved = broker.Submit(Request(rng, "File.Read"));
        broker.Decide(new UserDecision(approved.Proposal.ProposalId, approved.ProposalSha256, DecisionKind.Approve));

        // Sicherung im laufenden Betrieb (Online-Backup)
        var zip = BackupArchive.Create(data, config, null, ContractTests.RepoRoot(), _clock.UtcNow);
        await broker.DisposeAsync();
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        var entries = ReadZip(zip);
        Assert.Contains("daten/audit.db", entries.Keys);
        Assert.Contains("config/localai.json", entries.Keys);
        Assert.DoesNotContain(entries.Values, v => v.Contains("capabilityId", StringComparison.OrdinalIgnoreCase));

        // Danach weiter gearbeitet: Wiederherstellung setzt auf den gesicherten Stand zurück.
        File.WriteAllText(Path.Combine(data, "run-journal.json"), """{"goal":"neuer"}""");
        var message = BackupArchive.Restore(zip, data, _clock.UtcNow.AddMinutes(1));
        Assert.Contains("unverändert", message, StringComparison.Ordinal);
        Assert.Equal("""{"goal":"x"}""", File.ReadAllText(Path.Combine(data, "run-journal.json")));
        Assert.Single(Directory.GetDirectories(data, "vor-wiederherstellung-*"));
        Assert.Equal("""{"sandbox":{"mode":"none"}}""", File.ReadAllText(config));

        // Neustart: die gesicherte Freigabe gilt nicht mehr (SEC-12), nichts wird ausgeführt.
        var restarted = new ApprovalBroker(new BrokerOptions(), _adapter, new AuditJournal(Path.Combine(data, "audit.db"), _clock), _clock, new TestPolicyContext());
        restarted.Start(runBackgroundLoops: false);
        Assert.Equal(ProposalState.Invalidated, restarted.Get(approved.Proposal.ProposalId).State);
        Assert.False(await restarted.DispatchAsync(approved.Proposal.ProposalId));
        await restarted.DisposeAsync();
        Assert.Equal(0, _adapter.Invocations);
    }

    [Fact]
    public async Task Veraenderte_Sicherung_wird_nicht_wiederhergestellt()
    {
        var data = Directory.CreateDirectory(Path.Combine(_dir, "data")).FullName;
        File.WriteAllText(Path.Combine(data, "toolchains.json"), "[]");
        var zip = BackupArchive.Create(data, null, null, ContractTests.RepoRoot(), _clock.UtcNow);
        using (var archive = ZipFile.Open(zip, ZipArchiveMode.Update))
        {
            archive.GetEntry("daten/toolchains.json")!.Delete();
            using var w = new StreamWriter(archive.CreateEntry("daten/toolchains.json").Open());
            await w.WriteAsync("""[{"name":"boese"}]""".AsMemory(), TestContext.Current.CancellationToken);
        }
        File.WriteAllText(Path.Combine(data, "toolchains.json"), "[\"aktuell\"]");
        Assert.Throws<InvalidDataException>(() => BackupArchive.Restore(zip, data, _clock.UtcNow));
        Assert.Equal("[\"aktuell\"]", File.ReadAllText(Path.Combine(data, "toolchains.json")));
        Assert.Empty(Directory.GetDirectories(data, "vor-wiederherstellung-*"));
    }

    private static Dictionary<string, string> ReadZip(string file)
    {
        using var zip = ZipFile.OpenRead(file);
        return zip.Entries.ToDictionary(e => e.FullName, e =>
        {
            using var r = new StreamReader(e.Open());
            return r.ReadToEnd();
        });
    }

    // ------------------------------------------------------------------ Oberfläche: Reiter SYSTEM

    private sealed class SystemApi : IBrokerUiApi
    {
        public bool? IncludeLogs { get; private set; }
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
        public Task<string> ChatSendAsync(string text, CancellationToken ct) => Task.FromResult("r");
        public Task AgentCancelAsync(string agent, CancellationToken ct) => Task.CompletedTask;
        public Task RunCancelAsync(CancellationToken ct) => Task.CompletedTask;
        public Task<IReadOnlyList<ActivityEvent>> WaitActivityAsync(long afterSeq, int timeoutMs, CancellationToken ct) => Task.FromResult<IReadOnlyList<ActivityEvent>>([]);
        public Task OrchestratorStopAsync(CancellationToken ct) => Task.CompletedTask;
        public Task RebuildSandboxAsync(CancellationToken ct) => Task.CompletedTask;

        public Task<JsonElement> AuditVerifyAsync(CancellationToken ct) => Task.FromResult(JsonDocument.Parse(
            """{"ok":false,"entries":10,"sessions":2,"proposals":3,"approvals":2,"denials":1,"invocations":2,"emergencyStops":0,"problems":["#7: x mehr als einmal ausgeführt"]}""").RootElement.Clone());

        public Task<JsonElement> DiagnosticsExportAsync(bool includeLogs, CancellationToken ct)
        {
            IncludeLogs = includeLogs;
            return Task.FromResult(JsonDocument.Parse("""{"path":"C:\\diag.zip"}""").RootElement.Clone());
        }

        public Task<JsonElement> BackupCreateAsync(CancellationToken ct) => Task.FromResult(JsonDocument.Parse("""{"path":"C:\\sicherung.zip"}""").RootElement.Clone());
    }

    [Fact]
    public async Task Reiter_System_zeigt_Audit_Diagnose_und_Sicherung()
    {
        var api = new SystemApi();
        var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"));
        main.VerifyAuditCommand.Execute(null);
        await WaitFor(() => main.SystemResultText.Length > 0);
        Assert.Contains("mehr als einmal ausgeführt", main.SystemResultText, StringComparison.Ordinal);
        Assert.Contains("2 ausgeführt", main.SystemResultText, StringComparison.Ordinal);

        main.IncludeLogs = false;
        main.ExportDiagnosticsCommand.Execute(null);
        await WaitFor(() => main.SystemResultText.Contains("diag.zip", StringComparison.Ordinal));
        Assert.False(api.IncludeLogs);
        Assert.Contains("ohne Protokolle", main.SystemResultText, StringComparison.Ordinal);

        main.CreateBackupCommand.Execute(null);
        await WaitFor(() => main.SystemResultText.Contains("sicherung.zip", StringComparison.Ordinal));
    }

    private static async Task WaitFor(Func<bool> condition)
    {
        for (var i = 0; i < 200 && !condition(); i++)
        {
            await Task.Delay(10);
        }
        Assert.True(condition());
    }
}
