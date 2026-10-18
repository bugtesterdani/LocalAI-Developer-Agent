using System.Diagnostics;
using System.Text;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// VM-Betrieb ohne Hyper-V: ein simulierter Host führt den echten Python-Worker im VM-Modus aus
/// (Eingang wie Copy-VMFile, Rückgabe als Rohlaufwerk, das beim Abhängen zur VHDX wird, Signal wie KVP).
/// Geprüft wird der gesamte Hostpfad: Aufbau-Ablauf, Selbsttest, Jobs, Laufwerkswechsel, Sperren.
/// </summary>
public sealed class HyperVTests : IAsyncLifetime
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-hv-").FullName;
    private readonly List<IAsyncDisposable> _cleanup = [];

    public ValueTask InitializeAsync() => ValueTask.CompletedTask;

    public async ValueTask DisposeAsync()
    {
        foreach (var item in _cleanup)
        {
            await item.DisposeAsync();
        }
        try
        {
            LocalAi.Sandbox.FileTools.DeleteTree(_dir);
        }
        catch (IOException)
        {
        }
    }

    private HyperVSettings Settings() => new()
    {
        ImagePath = CreateFile("win.iso"),
        ImageName = "Windows 11 Pro",
        VmDirectory = Path.Combine(_dir, "vm"),
        TransferDiskMB = 32,
        BuildTimeout = TimeSpan.FromSeconds(60),
    };

    private string CreateFile(string name)
    {
        var path = Path.Combine(_dir, name);
        File.WriteAllText(path, "x");
        return path;
    }

    private async Task<(HyperVSandbox Vm, SimulatedHost Host, SandboxSession Session)> StartAsync(Action<SimulatedHost>? configure = null)
    {
        var settings = Settings();
        var session = new SandboxSession("s-" + Guid.NewGuid().ToString("N")[..8]);
        var host = new SimulatedHost(settings, Path.Combine(_dir, "guest-sim"));
        configure?.Invoke(host);
        var vm = new HyperVSandbox(settings, host, guestDir =>
        {
            var root = Directory.CreateDirectory(Path.Combine(guestDir, "LocalAi")).FullName;
            File.WriteAllText(Path.Combine(root, "session.key"), Convert.ToHexStringLower(session.Key));
            return Task.CompletedTask;
        });
        _cleanup.Add(vm);
        _cleanup.Add(host);
        vm.Start();
        var deadline = DateTime.UtcNow.AddSeconds(90);
        while (vm.Status.State is not (SandboxState.Ready or SandboxState.Locked) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(100, TestContext.Current.CancellationToken);
        }
        return (vm, host, session);
    }

    private string Project()
    {
        var root = Directory.CreateDirectory(Path.Combine(_dir, "projekt")).FullName;
        File.WriteAllText(Path.Combine(root, "README.md"), "# Projekt in der VM\nZeile 2\n");
        Directory.CreateDirectory(Path.Combine(root, "src"));
        File.WriteAllText(Path.Combine(root, "src", "Main.cs"), "class Main {}\n");
        return root;
    }

    private static ToolProposal Proposal(string tool, string grantId, string snapshot, IReadOnlyList<ToolTarget> targets, object? extra = null)
    {
        var now = DateTimeOffset.UtcNow;
        var args = new Dictionary<string, object> { ["grantId"] = grantId };
        if (extra is string root)
        {
            args["root"] = root;
        }
        return new ToolProposal(1, Guid.NewGuid().ToString("D"), "run", "task", AgentRole.Coder, tool, "1", "Test",
            JsonSerializer.SerializeToElement(args), targets, snapshot, 1, "sbx-hyperv",
            ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
    }

    [Fact, Trait("Pruefung", "M2b")]
    public async Task Aufbau_Selbsttest_und_Roundtrips_ueber_die_Transfer_VHDX()
    {
        var (vm, host, session) = await StartAsync();
        Assert.Equal(SandboxState.Ready, vm.Status.State);
        Assert.Contains("Selbsttest bestanden", vm.Status.Text, StringComparison.Ordinal);
        Assert.Contains(host.Progress, p => p.StartsWith("Schritt 1/2", StringComparison.Ordinal));

        var root = Project();
        var grant = new FolderGrant("p", root, [FolderRight.List, FolderRight.Read], true);
        var registry = new SnapshotRegistry();
        var context = new ProjectPolicyContext(new ProjectConfig(1, "p", "p", [grant]), registry);
        var adapter = new SandboxToolAdapter(session, vm, context, registry, Path.Combine(_dir, "scratch"), SystemClock.Instance)
        {
            JobTimeout = TimeSpan.FromSeconds(30),
            SnapshotTimeout = TimeSpan.FromSeconds(30),
        };
        var ct = TestContext.Current.CancellationToken;

        var snap = await adapter.ExecuteAsync(Proposal("Workspace.Snapshot", "p", "-", [], root), "inv-0", ct);
        Assert.True(snap.Status == ResultStatus.Succeeded, $"Snapshot: {snap.TargetResults[0].Message} (Sandbox: {vm.Status.Text})");
        var snapshotId = snap.TargetResults.Single(t => t.TargetId == "snapshot").Text!;

        const int Roundtrips = 12;
        for (var i = 0; i < Roundtrips; i++)
        {
            var read = await adapter.ExecuteAsync(
                Proposal("File.Read", "p", snapshotId, [new ToolTarget("t1", i % 2 == 0 ? "README.md" : "src/Main.cs")]), $"inv-{i + 1}", ct);
            Assert.True(read.Status == ResultStatus.Succeeded,
                $"Lesezugriff {i + 1}: {read.Status} – {read.TargetResults[0].Message} (Sandbox: {vm.Status.Text})");
            Assert.True(read.Untrusted);
            Assert.Equal(i % 2 == 0 ? "# Projekt in der VM\nZeile 2\n" : "class Main {}\n", read.TargetResults[0].Text);
        }
        Assert.Equal(Roundtrips + 2, host.Swaps); // Bericht + Snapshot + Lesezugriffe
        Assert.Empty(Directory.GetFiles(Path.Combine(host.GuestRoot, "inbox")));
    }

    private async Task<HyperVSandbox> StartReusableAsync(HyperVSettings settings, SimulatedHost host, SandboxSession session, string fingerprint,
        string? code = null, Func<string, Task>? buildUpdate = null)
    {
        var vm = new HyperVSandbox(settings, host, guestDir =>
        {
            var root = Directory.CreateDirectory(Path.Combine(guestDir, "LocalAi")).FullName;
            File.WriteAllText(Path.Combine(root, "session.key"), Convert.ToHexStringLower(session.Key));
            return Task.CompletedTask;
        }, session: session, fingerprint: () => fingerprint, codeFingerprint: code is null ? null : () => code, buildUpdate: buildUpdate);
        vm.Start();
        var deadline = DateTime.UtcNow.AddSeconds(90);
        while (vm.Status.State is not (SandboxState.Ready or SandboxState.Locked) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(100, TestContext.Current.CancellationToken);
        }
        return vm;
    }

    private async Task<ToolResult> ReadOnceAsync(HyperVSandbox vm, SandboxSession session, string root, string inv)
    {
        var grant = new FolderGrant("p", root, [FolderRight.List, FolderRight.Read], true);
        var registry = new SnapshotRegistry();
        var context = new ProjectPolicyContext(new ProjectConfig(1, "p", "p", [grant]), registry);
        var adapter = new SandboxToolAdapter(session, vm, context, registry, Path.Combine(_dir, "scratch-" + inv), SystemClock.Instance)
        {
            JobTimeout = TimeSpan.FromSeconds(30),
            SnapshotTimeout = TimeSpan.FromSeconds(30),
        };
        var ct = TestContext.Current.CancellationToken;
        var snap = await adapter.ExecuteAsync(Proposal("Workspace.Snapshot", "p", "-", [], root), inv + "-s", ct);
        Assert.True(snap.Status == ResultStatus.Succeeded, $"Snapshot: {snap.TargetResults[0].Message} (Sandbox: {vm.Status.Text})");
        var id = snap.TargetResults.Single(t => t.TargetId == "snapshot").Text!;
        return await adapter.ExecuteAsync(Proposal("File.Read", "p", id, [new ToolTarget("t1", "README.md")]), inv + "-r", ct);
    }

    [Fact, Trait("Pruefung", "M3-Wiederverwendung")]
    public async Task Passende_VM_wird_fortgesetzt_statt_neu_aufgebaut()
    {
        var settings = Settings() with { ReuseVm = true };
        var host = new SimulatedHost(settings, Path.Combine(_dir, "guest-sim"));
        _cleanup.Add(host);
        var root = Project();

        Assert.Null(HyperVSandbox.LoadReusableSession(settings));
        var first = new SandboxSession("s-erste");
        var vm1 = await StartReusableAsync(settings, host, first, "fp-1");
        Assert.Equal(SandboxState.Ready, vm1.Status.State);
        Assert.Equal(ResultStatus.Succeeded, (await ReadOnceAsync(vm1, first, root, "a")).Status);
        // Absturz während der Sitzung (kein sauberes Ende): keine Wiederverwendung.
        Assert.Null(HyperVSandbox.LoadReusableSession(settings));
        await vm1.DisposeAsync();
        Assert.Equal(["Save"], host.StateActions);
        Assert.Contains("gespeichert", vm1.Status.Text, StringComparison.Ordinal);

        // Programm neu gestartet: gleiche Sitzung, Sequenz geht weiter, kein Neuaufbau.
        var second = HyperVSandbox.LoadReusableSession(settings);
        Assert.NotNull(second);
        Assert.Equal("s-erste", second!.SessionId);
        Assert.Equal(first.LastSequence, second.LastSequence);
        var vm2 = await StartReusableAsync(settings, host, second, "fp-1");
        Assert.True(vm2.Status.State == SandboxState.Ready, vm2.Status.Text);
        Assert.Contains("wiederverwendet", vm2.Status.Text, StringComparison.Ordinal);
        Assert.Equal(1, host.Builds);
        var read = await ReadOnceAsync(vm2, second, root, "b");
        Assert.True(read.Status == ResultStatus.Succeeded, read.TargetResults[0].Message);
        Assert.Equal("# Projekt in der VM\nZeile 2\n", read.TargetResults[0].Text);
        await vm2.DisposeAsync();

        // Anderer Stand (z. B. Worker oder Toolchain geändert): Neuaufbau.
        var third = HyperVSandbox.LoadReusableSession(settings)!;
        var vm3 = await StartReusableAsync(settings, host, third, "fp-2");
        _cleanup.Add(vm3);
        Assert.Equal(SandboxState.Ready, vm3.Status.State);
        Assert.Equal(2, host.Builds);
        Assert.Equal(ResultStatus.Succeeded, (await ReadOnceAsync(vm3, third, root, "c")).Status);
    }

    [Fact, Trait("Pruefung", "M4-Gast-Update")]
    public async Task Nur_Code_geaendert_laufende_VM_wird_aktualisiert_statt_neu_aufgebaut()
    {
        var settings = Settings() with { ReuseVm = true };
        var host = new SimulatedHost(settings, Path.Combine(_dir, "guest-sim"));
        _cleanup.Add(host);
        var root = Project();
        // Quelle des Updates: kleines Paket localai und die drei Gastskripte.
        var src = Directory.CreateDirectory(Path.Combine(_dir, "update-src")).FullName;
        var pkg = Directory.CreateDirectory(Path.Combine(src, "localai", "worker")).FullName;
        File.WriteAllText(Path.Combine(src, "localai", "__init__.py"), "");
        File.WriteAllText(Path.Combine(pkg, "marker.py"), "VERSION = 2\n");
        var scripts = Directory.CreateDirectory(Path.Combine(src, "guest")).FullName;
        foreach (var f in GuestPayloadBuilder.GuestScriptFiles)
        {
            File.WriteAllText(Path.Combine(scripts, f), "# " + f);
        }
        var sources = new GuestPayloadSources(src, src, Path.Combine(src, "localai"), scripts, []);
        var updates = 0;
        Task Build(string zip)
        {
            updates++;
            GuestPayloadBuilder.BuildUpdate(zip, sources, settings.TransferDiskBytes, ["dotnet"], true);
            return Task.CompletedTask;
        }

        var session = new SandboxSession("s-upd");
        var vm1 = await StartReusableAsync(settings, host, session, "basis-1", "code-1", Build);
        Assert.Equal(SandboxState.Ready, vm1.Status.State);
        await vm1.DisposeAsync();
        Assert.Equal(0, updates);

        // Nur der Code ist neu: VM wird fortgesetzt und aktualisiert, kein Neuaufbau.
        var resumed = HyperVSandbox.LoadReusableSession(settings)!;
        var vm2 = await StartReusableAsync(settings, host, resumed, "basis-1", "code-2", Build);
        Assert.True(vm2.Status.State == SandboxState.Ready, vm2.Status.Text);
        Assert.Contains("aktualisiert", vm2.Status.Text, StringComparison.Ordinal);
        Assert.Equal(1, host.Builds);
        Assert.Equal(1, updates);
        Assert.Equal("VERSION = 2\n", File.ReadAllText(Path.Combine(host.PackageDir, "worker", "marker.py")));
        Assert.Equal("# update.ps1", File.ReadAllText(Path.Combine(host.GuestScriptsDir, "update.ps1")));
        Assert.Contains("\"lowIntegrityTools\":true", File.ReadAllText(Path.Combine(host.GuestScriptsDir, "guest-config.json")), StringComparison.Ordinal);
        // Worker hat sich nach dem Update neu gestartet und arbeitet normal weiter.
        var deadline = DateTime.UtcNow.AddSeconds(30);
        while (host.WorkerRestarts == 0 && DateTime.UtcNow < deadline)
        {
            await Task.Delay(100, TestContext.Current.CancellationToken);
        }
        Assert.Equal(1, host.WorkerRestarts);
        var read = await ReadOnceAsync(vm2, resumed, root, "u");
        Assert.True(read.Status == ResultStatus.Succeeded, read.TargetResults[0].Message);
        await vm2.DisposeAsync();

        // Gleicher Code beim nächsten Start: kein weiteres Update.
        var again = HyperVSandbox.LoadReusableSession(settings)!;
        var vm3 = await StartReusableAsync(settings, host, again, "basis-1", "code-2", Build);
        _cleanup.Add(vm3);
        Assert.True(vm3.Status.State == SandboxState.Ready, vm3.Status.Text);
        Assert.DoesNotContain("aktualisiert", vm3.Status.Text, StringComparison.Ordinal);
        Assert.Equal(1, updates);
        Assert.Equal(1, host.Builds);
    }

    [Fact, Trait("Pruefung", "M4-Gast-Update")]
    public async Task Zweite_Instanz_wartet_bis_die_erste_gespeichert_hat_und_setzt_dann_fort()
    {
        var settings = Settings() with { ReuseVm = true };
        var host = new SimulatedHost(settings, Path.Combine(_dir, "guest-sim"));
        _cleanup.Add(host);
        var vm1 = await StartReusableAsync(settings, host, new SandboxSession("s-1"), "basis", "code");
        Assert.Equal(SandboxState.Ready, vm1.Status.State);

        // Zweite Instanz mit kurzer Wartezeit: gibt auf, ohne die VM oder den Zustand der ersten anzufassen.
        var impatient = new HyperVSandbox(settings, host, _ => Task.CompletedTask, session: new SandboxSession("s-x"),
            fingerprint: () => "basis") { InstanceLockWait = TimeSpan.FromSeconds(1) };
        impatient.Start();
        await impatient.WhenSettled;
        Assert.Equal(SandboxState.Locked, impatient.Status.State);
        Assert.Contains("anderen Instanz", impatient.Status.Text, StringComparison.Ordinal);
        await impatient.DisposeAsync();
        Assert.Equal(1, host.Builds);
        Assert.Empty(host.StateActions);

        // Instanz startet, während die erste noch läuft; die erste wird danach beendet (speichert) → Fortsetzen.
        Assert.Null(HyperVSandbox.LoadReusableSession(settings)); // erste Instanz läuft noch
        var waiting = new SandboxSession("s-neu");
        var startTask = Task.Run(() => StartReusableAsync(settings, host, waiting, "basis", "code"), TestContext.Current.CancellationToken);
        await Task.Delay(1500, TestContext.Current.CancellationToken);
        await vm1.DisposeAsync();
        var vm2 = await startTask;
        _cleanup.Add(vm2);
        Assert.True(vm2.Status.State == SandboxState.Ready, vm2.Status.Text);
        Assert.Contains("wiederverwendet", vm2.Status.Text, StringComparison.Ordinal);
        Assert.Equal(1, host.Builds);
        Assert.Equal("s-1", waiting.SessionId);
        var read = await ReadOnceAsync(vm2, waiting, Project(), "w");
        Assert.True(read.Status == ResultStatus.Succeeded, read.TargetResults[0].Message);
    }

    [Fact, Trait("Pruefung", "M4-Gast-Update")]
    public async Task Grundlage_geaendert_baut_trotz_Gast_Update_neu_auf()
    {
        var settings = Settings() with { ReuseVm = true };
        var host = new SimulatedHost(settings, Path.Combine(_dir, "guest-sim"));
        _cleanup.Add(host);
        var updates = 0;
        Task Build(string zip)
        {
            updates++;
            return Task.CompletedTask;
        }
        var vm1 = await StartReusableAsync(settings, host, new SandboxSession("s-b"), "basis-1", "code-1", Build);
        await vm1.DisposeAsync();
        var vm2 = await StartReusableAsync(settings, host, HyperVSandbox.LoadReusableSession(settings)!, "basis-2", "code-2", Build);
        _cleanup.Add(vm2);
        Assert.Equal(SandboxState.Ready, vm2.Status.State);
        Assert.Equal(2, host.Builds);
        Assert.Equal(0, updates);
    }

    [Fact, Trait("Pruefung", "M3-Wiederverwendung")]
    public async Task Gesperrte_VM_wird_nie_wiederverwendet_und_Neuaufbau_auf_Knopfdruck()
    {
        var settings = Settings() with { ReuseVm = true };
        var host = new SimulatedHost(settings, Path.Combine(_dir, "guest-sim"));
        _cleanup.Add(host);
        var session = new SandboxSession("s-x");
        var vm = await StartReusableAsync(settings, host, session, "fp");
        _cleanup.Add(vm);
        Assert.Equal(SandboxState.Ready, vm.Status.State);
        vm.Lock("Test");
        Assert.Null(HyperVSandbox.LoadReusableSession(settings));
        await vm.RebuildAsync();
        var deadline = DateTime.UtcNow.AddSeconds(90);
        while (vm.Status.State != SandboxState.Ready && DateTime.UtcNow < deadline)
        {
            await Task.Delay(100, TestContext.Current.CancellationToken);
        }
        Assert.True(vm.Status.State == SandboxState.Ready, vm.Status.Text);
        Assert.Equal(2, host.Builds);
        Assert.Equal(ResultStatus.Succeeded, (await ReadOnceAsync(vm, session, Project(), "d")).Status);
    }

    [Fact, Trait("Pruefung", "M3-Wiederverwendung")]
    public void Fingerabdruck_aendert_sich_nur_mit_dem_Gaststand()
    {
        var src = Directory.CreateDirectory(Path.Combine(_dir, "fp")).FullName;
        var pkg = Directory.CreateDirectory(Path.Combine(src, "localai")).FullName;
        Directory.CreateDirectory(Path.Combine(pkg, "worker"));
        Directory.CreateDirectory(Path.Combine(pkg, "orchestrator"));
        var scripts = Directory.CreateDirectory(Path.Combine(src, "guest")).FullName;
        var site = Directory.CreateDirectory(Path.Combine(src, "site")).FullName;
        Directory.CreateDirectory(Path.Combine(site, "pydantic"));
        File.WriteAllText(Path.Combine(pkg, "worker", "tools.py"), "a");
        File.WriteAllText(Path.Combine(pkg, "orchestrator", "run.py"), "a");
        File.WriteAllText(Path.Combine(scripts, "provision.ps1"), "a");
        var sources = new GuestPayloadSources(src, site, pkg, scripts, ["pydantic"]);
        var settings = Settings();
        var reg = new ToolchainRegistration("dotnet", "10", "/tc", "m", "digest-1", new Dictionary<string, string>(), "2026-10-18T00:00:00.000Z");
        string Fp(ToolchainRegistration r) => SandboxFingerprint.Compute(settings, sources, [r], ["dotnet"]);
        var baseline = Fp(reg);
        Assert.Equal(baseline, Fp(reg));
        File.WriteAllText(Path.Combine(pkg, "orchestrator", "run.py"), "b");
        Assert.Equal(baseline, Fp(reg)); // Orchestrator läuft nicht im Gast
        Assert.NotEqual(baseline, Fp(reg with { Digest = "digest-2" }));
        Assert.NotEqual(baseline, SandboxFingerprint.Compute(settings with { MemoryMB = 4096 }, sources, [reg], ["dotnet"]));
        File.WriteAllText(Path.Combine(scripts, "provision.ps1"), "b");
        var scriptChanged = Fp(reg);
        Assert.NotEqual(baseline, scriptChanged);
        File.WriteAllText(Path.Combine(pkg, "worker", "tools.py"), "b");
        Assert.NotEqual(scriptChanged, Fp(reg));

        // Aufteilung für das Gast-Update: Code (Worker, Skripte, Ausführungsoption) getrennt von der Grundlage.
        var basis = SandboxFingerprint.ComputeBase(settings, sources, [reg], ["dotnet"]);
        var code = SandboxFingerprint.ComputeCode(settings, sources);
        File.WriteAllText(Path.Combine(pkg, "worker", "tools.py"), "c");
        Assert.Equal(basis, SandboxFingerprint.ComputeBase(settings, sources, [reg], ["dotnet"]));
        Assert.NotEqual(code, SandboxFingerprint.ComputeCode(settings, sources));
        Assert.NotEqual(code, SandboxFingerprint.ComputeCode(settings with { LowIntegrityTools = false }, sources));
        Assert.NotEqual(basis, SandboxFingerprint.ComputeBase(settings with { MemoryMB = 4096 }, sources, [reg], ["dotnet"]));
        Assert.NotEqual(basis, SandboxFingerprint.ComputeBase(settings, sources, [reg with { Digest = "digest-3" }], ["dotnet"]));
    }

    [Fact, Trait("Pruefung", "SEC-24")]
    public async Task Fehler_im_Bericht_sperren_die_Sandbox()
    {
        var (vm, _, _) = await StartAsync(h => h.ReportJson = Report(errors: ["Toolchain fehlt"]));
        Assert.Equal(SandboxState.Locked, vm.Status.State);
        Assert.Contains("Toolchain fehlt", vm.Status.Text, StringComparison.Ordinal);
        var reply = await vm.ExchangeAsync(new JobEnvelope(DummyJob(1), "x"), null, TimeSpan.FromSeconds(1), TestContext.Current.CancellationToken);
        Assert.Contains("nicht bereit", reply.Rejection, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "SEC-24")]
    public async Task Netzwerkkarte_sperrt_die_Sandbox()
    {
        var (vm, _, _) = await StartAsync(h => h.Nics = 1);
        Assert.Equal(SandboxState.Locked, vm.Status.State);
        Assert.Contains("Netzwerkkarte", vm.Status.Text, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "SEC-06")]
    public async Task Erreichbares_Netz_im_Gast_sperrt_die_Sandbox()
    {
        var (vm, _, _) = await StartAsync(h => h.ReportJson = Report(reachable: true));
        Assert.Equal(SandboxState.Locked, vm.Status.State);
        Assert.Contains("Netzwerkprüfung", vm.Status.Text, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "SEC-18")]
    public async Task Fehlende_Antwort_sperrt_ohne_Wiederholung()
    {
        var (vm, host, session) = await StartAsync();
        Assert.Equal(SandboxState.Ready, vm.Status.State);
        host.DropJobs = true;
        var job = DummyJob(session.NextSequence()) with { SessionId = session.SessionId };
        var reply = await vm.ExchangeAsync(JobSigner.Sign(job, session.Key), null, TimeSpan.FromSeconds(2), TestContext.Current.CancellationToken);
        Assert.Contains("Ergebnis unklar", reply.Rejection, StringComparison.Ordinal);
        Assert.Equal(SandboxState.Locked, vm.Status.State);
        var second = await vm.ExchangeAsync(JobSigner.Sign(job with { Sequence = session.NextSequence() }, session.Key), null,
            TimeSpan.FromSeconds(2), TestContext.Current.CancellationToken);
        Assert.Contains("nicht bereit", second.Rejection, StringComparison.Ordinal);
        Assert.Equal(1, host.JobsCopied);
    }

    [Fact, Trait("Pruefung", "SEC-24")]
    public void Bericht_wird_streng_bewertet()
    {
        Assert.True(GuestReport.Evaluate(Encoding.UTF8.GetBytes(Report()), 0).Ok);
        Assert.False(GuestReport.Evaluate(Encoding.UTF8.GetBytes(Report()), 1).Ok);
        Assert.False(GuestReport.Evaluate(Encoding.UTF8.GetBytes(Report(adapters: 1)), 0).Ok);
        Assert.False(GuestReport.Evaluate(Encoding.UTF8.GetBytes(Report(probes: 2)), 0).Ok);
        Assert.False(GuestReport.Evaluate(Encoding.UTF8.GetBytes(Report(running: false)), 0).Ok);
        Assert.False(GuestReport.Evaluate("kein json"u8.ToArray(), 0).Ok);
        Assert.False(GuestReport.Evaluate("""{"schemaVersion":2}"""u8.ToArray(), 0).Ok);
        Assert.Equal("a b", GuestReport.Clip("a\u001bb"));
    }

    [Fact, Trait("Pruefung", "SEC-18")]
    public async Task Signale_haben_ein_festes_Format()
    {
        var board = new SignalBoard();
        Assert.False(board.Post("result:1; rm -rf"));
        Assert.False(board.Post("anderes:1"));
        Assert.True(board.Post("result:7"));
        Assert.True(await board.WaitForAsync("result:7", TimeSpan.FromSeconds(1), TestContext.Current.CancellationToken));
        Assert.False(await board.WaitForAsync("result:8", TimeSpan.FromMilliseconds(100), TestContext.Current.CancellationToken));
        Assert.Contains("noch keine Meldung", board.DescribeGuest(), StringComparison.Ordinal);
        Assert.False(board.PostProgress("Start; rm"));
        Assert.True(board.PostProgress("setupcomplete"));
        Assert.False(board.PostHeartbeat("Ok Apps"));
        Assert.True(board.PostHeartbeat("OkApplicationsUnknown"));
        Assert.Equal("Windows eingerichtet, Provisionierung startet; Heartbeat: Windows läuft", board.DescribeGuest());
    }

    [Fact, Trait("Pruefung", "R17")]
    public void Einstellungen_werden_geprueft()
    {
        var ok = Settings();
        ok.Validate();
        Assert.Throws<InvalidDataException>(() => (ok with { VmName = "x'; Remove-VM" }).Validate());
        Assert.Throws<InvalidDataException>(() => (ok with { ImageIndex = 3 }).Validate());
        Assert.Throws<InvalidDataException>(() => (ok with { TransferDiskMB = 1 }).Validate());
        Assert.Throws<InvalidDataException>(() => (ok with { ProductKey = "abc" }).Validate());
    }

    [Fact, Trait("Pruefung", "SEC-21")]
    public void Gastdateien_enthalten_nur_Laufzeit_und_Worker()
    {
        var home = Directory.CreateDirectory(Path.Combine(_dir, "pyhome")).FullName;
        File.WriteAllText(Path.Combine(home, "python.exe"), "exe");
        Directory.CreateDirectory(Path.Combine(home, "Lib", "json"));
        File.WriteAllText(Path.Combine(home, "Lib", "json", "__init__.py"), "");
        Directory.CreateDirectory(Path.Combine(home, "Lib", "test"));
        File.WriteAllText(Path.Combine(home, "Lib", "test", "big.py"), "");
        Directory.CreateDirectory(Path.Combine(home, "Lib", "site-packages", "pip"));
        var site = Directory.CreateDirectory(Path.Combine(_dir, "site")).FullName;
        foreach (var d in new[] { "pydantic", "pydantic-2.0.dist-info", "pydantic_core", "annotated_types", "typing_inspection", "pytest", "_pytest" })
        {
            Directory.CreateDirectory(Path.Combine(site, d, "__pycache__"));
            File.WriteAllText(Path.Combine(site, d, "x.py"), "");
        }
        File.WriteAllText(Path.Combine(site, "typing_extensions.py"), "");
        File.WriteAllText(Path.Combine(site, "_editable_impl_localai.pth"), "C:/host/pfad");
        var pkg = Directory.CreateDirectory(Path.Combine(_dir, "localai", "worker")).Parent!.FullName;
        File.WriteAllText(Path.Combine(pkg, "worker", "__main__.py"), "");
        Directory.CreateDirectory(Path.Combine(pkg, "__pycache__"));
        File.WriteAllText(Path.Combine(pkg, "__pycache__", "a.pyc"), "");
        var scripts = Path.Combine(ContractTests.RepoRoot(), "scripts", "sandbox", "guest");
        var sources = new GuestPayloadSources(home, site, pkg, scripts, GuestPayloadSources.DefaultPackages);
        var guest = Path.Combine(_dir, "guest");

        GuestPayloadBuilder.Build(guest, sources, new byte[32], 32 * 1024 * 1024);

        var root = Path.Combine(guest, "LocalAi");
        var outSite = Path.Combine(root, "python", "Lib", "site-packages");
        Assert.True(File.Exists(Path.Combine(root, "python", "python.exe")));
        Assert.True(File.Exists(Path.Combine(root, "python", "Lib", "json", "__init__.py")));
        Assert.False(Directory.Exists(Path.Combine(root, "python", "Lib", "test")));
        Assert.False(Directory.Exists(Path.Combine(outSite, "pip")));
        Assert.True(Directory.Exists(Path.Combine(outSite, "pydantic")));
        Assert.True(Directory.Exists(Path.Combine(outSite, "pydantic-2.0.dist-info")));
        Assert.True(File.Exists(Path.Combine(outSite, "typing_extensions.py")));
        Assert.False(Directory.Exists(Path.Combine(outSite, "pytest")));
        Assert.False(Directory.Exists(Path.Combine(outSite, "_pytest")));
        Assert.False(File.Exists(Path.Combine(outSite, "_editable_impl_localai.pth")));
        Assert.False(Directory.Exists(Path.Combine(outSite, "pydantic", "__pycache__")));
        Assert.True(File.Exists(Path.Combine(outSite, "localai", "worker", "__main__.py")));
        Assert.False(Directory.Exists(Path.Combine(outSite, "localai", "__pycache__")));
        Assert.Equal(new string('0', 64), File.ReadAllText(Path.Combine(root, "session.key")));
        Assert.Contains("33554432", File.ReadAllText(Path.Combine(root, "guest-config.json")), StringComparison.Ordinal);
        Assert.True(File.Exists(Path.Combine(root, "provision.ps1")));
        Assert.True(File.Exists(Path.Combine(guest, "SetupComplete.cmd")));

        var incomplete = sources with { Packages = [.. GuestPayloadSources.DefaultPackages, "fehlt"] };
        Assert.Throws<InvalidDataException>(() => GuestPayloadBuilder.Build(Path.Combine(_dir, "guest2"), incomplete, new byte[32], 1));
    }

    [Fact, Trait("Pruefung", "M2b")]
    public async Task PowerShell_Skripte_sind_gueltig()
    {
        var shell = new[] { "pwsh", "powershell.exe" }.Select(FindOnPath).FirstOrDefault(s => s is not null);
        if (shell is null)
        {
            Assert.Skip("Weder pwsh noch powershell.exe gefunden");
        }
        var start = new ProcessStartInfo(shell!)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
        };
        foreach (var a in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                     Path.Combine(ContractTests.RepoRoot(), "scripts", "sandbox", "tests", "Test-SandboxScripts.ps1") })
        {
            start.ArgumentList.Add(a);
        }
        using var process = Process.Start(start)!;
        process.StandardInput.Close();
        var output = await process.StandardOutput.ReadToEndAsync(TestContext.Current.CancellationToken);
        var errors = await process.StandardError.ReadToEndAsync(TestContext.Current.CancellationToken);
        await process.WaitForExitAsync(TestContext.Current.CancellationToken);
        Assert.True(process.ExitCode == 0, $"{shell}: {output}\n{errors}");
        Assert.Contains("OK", output, StringComparison.Ordinal);
    }

    private static string? FindOnPath(string exe) =>
        (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator)
            .Concat([Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".dotnet", "tools")])
            .SelectMany(d => new[] { Path.Combine(d, exe), Path.Combine(d, exe + ".exe") })
            .FirstOrDefault(File.Exists);

    internal static string Report(string[]? errors = null, bool reachable = false, int adapters = 0, int probes = 6, bool running = true) =>
        JsonSerializer.Serialize(new
        {
            schemaVersion = 1,
            computerName = "LOCALAI-SBX",
            networkAdapters = adapters,
            probes = Enumerable.Range(0, probes).Select(i => new { name = $"p{i}", reachable = reachable && i == 0 }),
            worker = new { running, python = "3.12.9" },
            errors = errors ?? [],
        });

    private static SandboxJob DummyJob(long sequence)
    {
        var now = DateTimeOffset.UtcNow;
        var proposal = Proposal("File.Read", "p", "snap", [new ToolTarget("t1", "README.md")]);
        return new SandboxJob(1, Guid.NewGuid().ToString("D"), sequence, "s", "inv", proposal, ProposalHashing.Sha256(proposal),
            ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
    }

    /// <summary>Hyper-V-Ersatz: der echte Python-Worker im VM-Modus mit Datei-Eingang, Rohlaufwerk und Signaldatei.</summary>
    private sealed class SimulatedHost(HyperVSettings settings, string guestRoot) : IHyperVHost, IAsyncDisposable
    {
        private readonly List<Process> _processes = [];
        private string Raw => Path.Combine(GuestRoot, "transfer.raw");
        private string SignalFile => Path.Combine(GuestRoot, "signal.txt");

        public string GuestRoot { get; } = guestRoot;
        public List<string> Progress { get; } = [];
        public string ReportJson { get; set; } = Report();
        public int Nics { get; set; }
        public bool DropJobs { get; set; }
        public int Swaps { get; private set; }
        public int JobsCopied { get; private set; }
        public int Builds { get; private set; }
        public List<string> StateActions { get; } = [];
        public int WorkerRestarts { get; private set; }
        public string PackageDir => Path.Combine(GuestRoot, "pkg", "localai");
        public string GuestScriptsDir => Path.Combine(GuestRoot, "root");
        private string[]? _workerArgs;
        private volatile bool _stopping;

        public Task<HostCheck> CheckAsync(CancellationToken cancellationToken) => Task.FromResult(new HostCheck(true, "simuliert"));

        public async Task BuildAsync(string requestFile, string statusFile, Action<string> progress, CancellationToken cancellationToken)
        {
            Builds++;
            StopAll();
            // Neuaufbau = frischer Gast: kein Zustand aus einer früheren VM.
            if (Directory.Exists(Path.Combine(GuestRoot, "work")))
            {
                Directory.Delete(Path.Combine(GuestRoot, "work"), recursive: true);
            }
            using var request = JsonDocument.Parse(await File.ReadAllTextAsync(requestFile, cancellationToken));
            var guestDir = request.RootElement.GetProperty("guestDirectory").GetString()!;
            Assert.Equal(settings.VmName, request.RootElement.GetProperty("vmName").GetString());
            foreach (var step in new[] { "Schritt 1/2: Abbild", "Schritt 2/2: Start" })
            {
                Progress.Add(step);
                progress(step);
            }
            Directory.CreateDirectory(Path.Combine(GuestRoot, "inbox"));
            await File.WriteAllBytesAsync(Raw, new byte[settings.TransferDiskBytes], cancellationToken);
            var key = Path.Combine(guestDir, "LocalAi", "session.key");
            var common = new[] { "--transfer-disk", Raw, "--transfer-size", settings.TransferDiskBytes.ToString(), "--signal-file", SignalFile };
            // Schlüssel wie im echten Gast in die VM kopieren (die Gastdateien des Hosts werden später gelöscht).
            var guestKey = Path.Combine(GuestRoot, "session.key");
            File.Copy(key, guestKey, overwrite: true);
            Directory.CreateDirectory(GuestScriptsDir);
            _workerArgs = ["--inbox", Path.Combine(GuestRoot, "inbox"), "--work", Path.Combine(GuestRoot, "work"),
                "--key-file", guestKey, "--require-ready", "--poll-ms", "20", "--guest-root", GuestScriptsDir, "--package-dir", PackageDir, .. common];
            StartSupervised(_workerArgs);
            // Provisionierung: Bericht über das Transfer-Laufwerk.
            var report = Path.Combine(GuestRoot, "report.json");
            await File.WriteAllTextAsync(report, ReportJson, cancellationToken);
            using var reporter = StartWorker(["--report", report, .. common]);
            await reporter.WaitForExitAsync(cancellationToken);
            Assert.Equal(0, reporter.ExitCode);
        }

        public async Task WatchSignalsAsync(SignalBoard board, Action<string> problem, CancellationToken cancellationToken)
        {
            string? last = null;
            board.PostHeartbeat("OkApplicationsHealthy");
            while (!cancellationToken.IsCancellationRequested)
            {
                string? value = null;
                try
                {
                    // Mit FileShare.Delete, damit der Worker die Datei währenddessen ersetzen kann (Windows).
                    await using var stream = new FileStream(SignalFile, FileMode.Open, FileAccess.Read,
                        FileShare.ReadWrite | FileShare.Delete);
                    using var reader = new StreamReader(stream);
                    value = (await reader.ReadToEndAsync(cancellationToken)).Trim();
                }
                catch (Exception ex) when (ex is FileNotFoundException or IOException or UnauthorizedAccessException)
                {
                }
                if (value is not null && value != last)
                {
                    last = value;
                    board.Post(value);
                }
                await Task.Delay(20, cancellationToken);
            }
        }

        public Task<int> CopyToGuestAsync(IReadOnlyList<string> files, string guestDirectory, CancellationToken cancellationToken)
        {
            Assert.Equal(HyperVSandbox.GuestInbox, guestDirectory);
            Assert.EndsWith(".ready", files[^1], StringComparison.Ordinal);
            JobsCopied++;
            if (!DropJobs)
            {
                foreach (var f in files)
                {
                    File.Copy(f, Path.Combine(GuestRoot, "inbox", Path.GetFileName(f)));
                }
            }
            return Task.FromResult(Nics);
        }

        public Task<int> SwapTransferDiskAsync(string currentDisk, string templateDisk, string newDisk, string detachedPath, CancellationToken cancellationToken)
        {
            Swaps++;
            var content = File.ReadAllBytes(Raw);
            VhdxTestWriter.Create(detachedPath, new VhdxTestWriter.Options(content.Length, Fixed: false), content);
            File.WriteAllBytes(Raw, new byte[content.Length]);
            return Task.FromResult(Nics);
        }

        public Task<VmInfo> GetVmInfoAsync(CancellationToken cancellationToken) => Task.FromResult(new VmInfo(_workerArgs is not null, "Saved", Nics));

        /// <summary>Speichern = Worker anhalten (Gastdateien bleiben), Fortsetzen = Worker mit demselben Zustand weiter.</summary>
        public Task<VmInfo> SetVmStateAsync(string action, CancellationToken cancellationToken)
        {
            StateActions.Add(action);
            if (action == "Save")
            {
                StopAll();
            }
            else if (Nics == 0 && _workerArgs is not null && _processes.All(p => p.HasExited))
            {
                StartSupervised(_workerArgs);
            }
            return Task.FromResult(new VmInfo(true, action == "Save" ? "Saved" : "Running", Nics));
        }

        public Task RemoveVmAsync(CancellationToken cancellationToken)
        {
            StopAll();
            _workerArgs = null;
            return Task.CompletedTask;
        }

        public ValueTask DisposeAsync()
        {
            StopAll();
            return ValueTask.CompletedTask;
        }

        /// <summary>Wie worker.cmd im Gast: Exitcode 75 (nach Gast-Update) → Worker neu starten.</summary>
        private void StartSupervised(string[] args)
        {
            _stopping = false;
            var process = StartWorker(args);
            process.EnableRaisingEvents = true;
            process.Exited += (_, _) =>
            {
                if (!_stopping && process.ExitCode == 75)
                {
                    WorkerRestarts++;
                    lock (_processes)
                    {
                        StartSupervised(args);
                    }
                }
            };
            lock (_processes)
            {
                _processes.Add(process);
            }
        }

        private void StopAll()
        {
            _stopping = true;
            foreach (var p in _processes.ToList())
            {
                try
                {
                    if (!p.HasExited)
                    {
                        p.Kill(entireProcessTree: true);
                    }
                }
                catch (InvalidOperationException)
                {
                }
                p.Dispose();
            }
            _processes.Clear();
        }

        private static Process StartWorker(IEnumerable<string> args)
        {
            var start = new ProcessStartInfo("uv")
            {
                WorkingDirectory = ContractTests.RepoRoot(),
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
            };
            foreach (var a in new[] { "run", "--no-sync", "--project", "src/python", "python", "-m", "localai.worker" }.Concat(args))
            {
                start.ArgumentList.Add(a);
            }
            start.Environment["PYTHONUTF8"] = "1";
            var process = Process.Start(start)!;
            process.StandardInput.Close();
            process.OutputDataReceived += (_, _) => { };
            process.ErrorDataReceived += (_, _) => { };
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            return process;
        }
    }
}
