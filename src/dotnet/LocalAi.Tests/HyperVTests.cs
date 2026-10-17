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

        public Task<HostCheck> CheckAsync(CancellationToken cancellationToken) => Task.FromResult(new HostCheck(true, "simuliert"));

        public async Task BuildAsync(string requestFile, string statusFile, Action<string> progress, CancellationToken cancellationToken)
        {
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
            _processes.Add(StartWorker(["--inbox", Path.Combine(GuestRoot, "inbox"), "--work", Path.Combine(GuestRoot, "work"),
                "--key-file", key, "--require-ready", "--poll-ms", "20", .. common]));
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

        public Task<VmInfo> GetVmInfoAsync(CancellationToken cancellationToken) => Task.FromResult(new VmInfo(true, "Running", Nics));

        public Task RemoveVmAsync(CancellationToken cancellationToken)
        {
            StopAll();
            return Task.CompletedTask;
        }

        public ValueTask DisposeAsync()
        {
            StopAll();
            return ValueTask.CompletedTask;
        }

        private void StopAll()
        {
            foreach (var p in _processes)
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
