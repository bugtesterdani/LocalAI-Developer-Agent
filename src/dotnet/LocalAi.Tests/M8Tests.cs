using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Broker.Host;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// M8: Linux-Gast – Gastauswahl (Toolchain → Projekt → Standard), Linux-Toolchains, mehrere VMs mit Start bei Bedarf
/// und Herunterfahren im Leerlauf, Snapshot-Abgleich zwischen den VMs, Seed-Laufwerk für cloud-init.
/// </summary>
public sealed class M8Tests : IAsyncDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-m8-").FullName;
    private readonly List<IAsyncDisposable> _cleanup = [];

    public async ValueTask DisposeAsync()
    {
        foreach (var item in _cleanup)
        {
            await item.DisposeAsync();
        }
        try
        {
            FileTools.DeleteTree(_dir);
        }
        catch (IOException)
        {
        }
    }

    private static ProjectConfig Project(GuestOs? guest = null, IReadOnlyList<string>? toolchains = null,
        Dictionary<string, GuestOs>? toolchainGuests = null) =>
        new(1, "p", "p", [], toolchains, guest, toolchainGuests);

    private static readonly Dictionary<string, GuestOs> Manifests = new()
    {
        ["dotnet"] = GuestOs.Windows,
        ["python"] = GuestOs.Windows,
        ["dotnet-linux"] = GuestOs.Linux,
        ["python-linux"] = GuestOs.Linux,
    };

    private static GuestOs? ManifestGuest(string name) => Manifests.TryGetValue(name, out var g) ? g : null;

    // ---------------------------------------------------------------- Gastauswahl

    [Fact, Trait("Pruefung", "M8-Gastauswahl")]
    public void Projekt_Gast_ueberschreibt_den_Standard_und_Toolchain_Gast_das_Projekt()
    {
        // Nur Standard aus config/localai.json.
        var plan = GuestSelection.Resolve(Project(toolchains: ["dotnet"]), GuestOs.Windows, ManifestGuest);
        Assert.Equal(GuestOs.Windows, plan.Home);
        Assert.Equal([GuestOs.Windows], plan.Required);

        // Projekt-Gast überschreibt den Standard; Linux-Toolchain läuft in der Linux-VM.
        plan = GuestSelection.Resolve(Project(GuestOs.Linux, ["python-linux"]), GuestOs.Windows, ManifestGuest);
        Assert.Equal(GuestOs.Linux, plan.Home);
        Assert.Equal([GuestOs.Linux], plan.Required);
        Assert.Empty(plan.Notes);

        // Toolchain-Eintrag überschreibt den Projekt-Gast: zwei VMs, jede Toolchain in ihrer.
        plan = GuestSelection.Resolve(Project(GuestOs.Windows, ["dotnet", "python-linux"], new() { ["python-linux"] = GuestOs.Linux }),
            GuestOs.Windows, ManifestGuest);
        Assert.Equal(GuestOs.Windows, plan.Home);
        Assert.Equal([GuestOs.Windows, GuestOs.Linux], plan.Required);
        Assert.Equal(GuestOs.Linux, plan.GuestOf("python-linux"));
        Assert.Equal(["dotnet"], plan.ToolchainsFor(GuestOs.Windows, ["dotnet", "python-linux"]));
        Assert.Equal(["python-linux"], plan.ToolchainsFor(GuestOs.Linux, ["dotnet", "python-linux"]));
    }

    [Fact, Trait("Pruefung", "M8-Gastauswahl")]
    public void Geerbter_Gast_weicht_dem_Manifest_ausdruecklicher_Widerspruch_ist_ein_Fehler()
    {
        var plan = GuestSelection.Resolve(Project(GuestOs.Windows, ["dotnet", "dotnet-linux"]), GuestOs.Windows, ManifestGuest);
        Assert.Equal(GuestOs.Linux, plan.GuestOf("dotnet-linux"));
        Assert.Contains(plan.Notes, n => n.Contains("dotnet-linux", StringComparison.Ordinal) && n.Contains("Linux-VM", StringComparison.Ordinal));

        var ex = Assert.Throws<InvalidDataException>(() => GuestSelection.Resolve(
            Project(null, ["dotnet"], new() { ["dotnet"] = GuestOs.Linux }), GuestOs.Windows, ManifestGuest));
        Assert.Contains("nur unter Windows", ex.Message, StringComparison.Ordinal);
        Assert.Contains("dotnet-linux", ex.Message, StringComparison.Ordinal);

        ex = Assert.Throws<InvalidDataException>(() => GuestSelection.Resolve(
            Project(null, ["dotnet"], new() { ["unbekannt"] = GuestOs.Linux }), GuestOs.Windows, ManifestGuest));
        Assert.Contains("keine Toolchain des Projekts", ex.Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "M8-Gastauswahl")]
    public void Projektdatei_mit_Gast_und_Toolchain_Gaesten_wird_gelesen()
    {
        var json = """
            { "schemaVersion": 1, "projectId": "p", "name": "P", "grants": [], "toolchains": ["dotnet", "python-linux"],
              "guestOs": "Windows", "toolchainGuests": { "python-linux": "Linux" } }
            """;
        var project = ContractJson.Deserialize<ProjectConfig>(json);
        Assert.Equal(GuestOs.Windows, project.GuestOs);
        Assert.Equal(GuestOs.Linux, project.ToolchainGuests!["python-linux"]);
        // Alte Projektdateien ohne die neuen Felder bleiben gültig.
        Assert.Null(ContractJson.Deserialize<ProjectConfig>("""{ "schemaVersion": 1, "projectId": "p", "name": "P", "grants": [] }""").GuestOs);
    }

    [Fact, Trait("Pruefung", "M8-Konfiguration")]
    public void Konfiguration_liest_Linux_VM_Standardgast_und_Leerlauf()
    {
        var repo = Directory.CreateDirectory(Path.Combine(_dir, "repo")).FullName;
        var cfg = Directory.CreateDirectory(Path.Combine(repo, "config")).FullName;
        var file = Path.Combine(cfg, "localai.json");
        File.WriteAllText(file, """
            { "sandbox": { "mode": "hyperv", "defaultGuestOs": "linux",
                           "hyperv": { "imagePath": "win.iso", "imageName": "Windows 11 Pro" },
                           "hypervLinux": { "imagePath": "linux/debian-13-generic-amd64.raw", "idleMinutes": 10, "stopAction": "Save" } } }
            """);
        var options = HostOptions.Parse(["--config", file, "--data", Path.Combine(_dir, "data", "broker")]);
        Assert.Equal(GuestOs.Linux, options.DefaultGuestOs);
        Assert.Equal(30, options.HyperV!.IdleMinutes);
        Assert.Equal("ShutDown", options.HyperV.StopAction);
        var linux = options.HyperVLinux!;
        Assert.True(linux.IsLinux);
        Assert.Equal("LocalAi-Linux", linux.VmName);
        Assert.Equal(Path.Combine(_dir, "data", "vm-linux"), linux.VmDirectory);
        Assert.Equal(Path.Combine(repo, "linux", "linux-gast"), linux.LinuxPrerequisites);
        Assert.Equal(10, linux.IdleMinutes);
        Assert.Equal("Save", linux.StopVmAction);
        linux.Validate();
        Assert.NotEqual(options.HyperV.VmDirectory, linux.VmDirectory);

        File.WriteAllText(file, """{ "sandbox": { "defaultGuestOs": "BeOS" } }""");
        Assert.Throws<JsonException>(() => HostOptions.Parse(["--config", file, "--data", Path.Combine(_dir, "data", "broker")]));
    }

    [Theory, Trait("Pruefung", "M8-Konfiguration")]
    [InlineData("disk.iso", 30, "ShutDown", "Rohformat")]
    [InlineData("disk.raw", -1, "ShutDown", "idleMinutes")]
    [InlineData("disk.raw", 30, "Hibernate", "stopAction")]
    public void Ungueltige_Linux_Einstellungen_werden_abgewiesen(string image, int idle, string stop, string expected)
    {
        var settings = new HyperVSettings { Guest = GuestOs.Linux, ImagePath = image, VmDirectory = _dir, IdleMinutes = idle, StopAction = stop, MemoryMB = 4096, OsDiskGB = 64 };
        var ex = Assert.Throws<InvalidDataException>(settings.Validate);
        Assert.Contains(expected, ex.Message, StringComparison.Ordinal);
    }

    // ---------------------------------------------------------------- Linux-Toolchains

    [Theory, Trait("Pruefung", "M8-Toolchains")]
    [InlineData("python-linux")]
    [InlineData("dotnet-linux")]
    public void Linux_Vorlagen_sind_gueltig(string name)
    {
        var dir = Path.Combine(ContractTests.RepoRoot(), "toolchains", name);
        var manifest = ToolchainValidator.Parse(File.ReadAllText(Path.Combine(dir, "toolchain.json")));
        Assert.Equal(GuestOs.Linux, manifest.Guest);
        // Installationsdatei liegt nicht im Repository (wird geladen) – hier als vorhanden angenommen.
        var problems = ToolchainValidator.Validate(manifest, rel => rel == manifest.Install.File || File.Exists(Path.Combine(dir, rel)),
            rel => Directory.Exists(Path.Combine(dir, rel)));
        Assert.True(problems.Count == 0, string.Join("; ", problems));
    }

    [Fact, Trait("Pruefung", "M8-Toolchains")]
    public void Linux_Manifest_braucht_Linux_Pfade_und_nur_erlaubte_Systemprogramme()
    {
        var dir = Path.Combine(ContractTests.RepoRoot(), "toolchains", "python-linux");
        var good = ToolchainValidator.Parse(File.ReadAllText(Path.Combine(dir, "toolchain.json")));
        List<string> Check(ToolchainManifest m) => ToolchainValidator.Validate(m, _ => true, _ => true);

        Assert.Contains(Check(good with { GuestPath = @"C:\Toolchains\python-linux" }), p => p.Contains("/opt/toolchains/python-linux", StringComparison.Ordinal));
        Assert.Contains(Check(good with { Binaries = [new ToolchainBinary("python", @"python\bin\python3")] }), p => p.Contains("'/'", StringComparison.Ordinal));
        Assert.Contains(Check(good with { Binaries = [new ToolchainBinary("python", @"{system}\cmd.exe")] }), p => p.Contains("{system}/bash", StringComparison.Ordinal));
        Assert.Contains(Check(good with { Binaries = [new ToolchainBinary("python", "{system}/curl")] }), p => p.Contains("{system}/bash", StringComparison.Ordinal));
        Assert.Empty(Check(good with { Binaries = [new ToolchainBinary("python", "{system}/python3")] }));
        Assert.Empty(Check(good with { Install = good.Install with { Kind = ToolchainInstallKind.Installer, File = "gcc.deb" } }));
        Assert.Contains(Check(good with { Install = good.Install with { Kind = ToolchainInstallKind.Installer, File = "setup.exe" } }),
            p => p.Contains(".deb", StringComparison.Ordinal));
        Assert.Contains(Check(good with { Environment = new Dictionary<string, string> { ["LD_PRELOAD"] = "x.so" } }),
            p => p.Contains("LD_PRELOAD", StringComparison.Ordinal));
        // Ein Windows-Manifest darf keine Linux-Archive verwenden.
        var windows = good with { Guest = null, GuestPath = @"C:\Toolchains\python-linux", Binaries = [new ToolchainBinary("python", "python.exe")] };
        Assert.Contains(Check(windows), p => p.Contains("ZIP", StringComparison.Ordinal));
    }

    [Fact, Trait("Pruefung", "M8-Toolchains")]
    public void Gastsystem_im_Manifest_muss_genau_geschrieben_sein()
    {
        var tcRoot = Directory.CreateDirectory(Path.Combine(_dir, "tc")).FullName;
        var tc = Directory.CreateDirectory(Path.Combine(tcRoot, "werkzeug")).FullName;
        var json = File.ReadAllText(Path.Combine(ContractTests.RepoRoot(), "toolchains", "python-linux", "toolchain.json"))
            .Replace("\"python-linux\"", "\"werkzeug\"", StringComparison.Ordinal).Replace("/python-linux\"", "/werkzeug\"", StringComparison.Ordinal);
        File.WriteAllText(Path.Combine(tc, "toolchain.json"), json.Replace("\"guest\": \"Linux\"", "\"guest\": \"linux\"", StringComparison.Ordinal));
        var registry = new ToolchainRegistry(Path.Combine(_dir, "toolchains.json"), tcRoot, ["werkzeug"], SystemClock.Instance);
        var info = registry.Inspect(tc, null, TestContext.Current.CancellationToken).Info;
        Assert.Contains(info.Problems, p => p.Contains("genau so geschrieben", StringComparison.Ordinal));
        Assert.Equal(GuestOs.Linux, info.Guest);
    }

    // ---------------------------------------------------------------- Seed-Laufwerk und Offline-Bausteine

    private string Prerequisites(bool corrupt = false)
    {
        var dir = Directory.CreateDirectory(Path.Combine(_dir, "linux-gast")).FullName;
        Directory.CreateDirectory(Path.Combine(dir, "wheels"));
        Directory.CreateDirectory(Path.Combine(dir, "debs"));
        var files = new Dictionary<string, string>
        {
            ["cpython-3.13.5+20250612-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"] = "python",
            ["wheels/pydantic-2.12.0-py3-none-any.whl"] = "a",
            ["wheels/pydantic_core-2.41.1-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"] = "b",
            ["wheels/annotated_types-0.7.0-py3-none-any.whl"] = "c",
            ["wheels/typing_extensions-4.15.0-py3-none-any.whl"] = "d",
            ["wheels/typing_inspection-0.4.2-py3-none-any.whl"] = "e",
            ["debs/hyperv-daemons_6.12.48-1_amd64.deb"] = "f",
        };
        var hashes = new Dictionary<string, string>();
        foreach (var (rel, content) in files)
        {
            File.WriteAllText(Path.Combine(dir, rel), content);
            hashes[rel] = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(content)));
        }
        if (corrupt)
        {
            File.WriteAllText(Path.Combine(dir, "debs", "hyperv-daemons_6.12.48-1_amd64.deb"), "verändert");
        }
        File.WriteAllText(Path.Combine(dir, LinuxPrerequisites.ManifestFile), JsonSerializer.Serialize(new
        {
            schemaVersion = 1,
            python = files.Keys.First(),
            pythonVersion = "3.13.5",
            wheels = files.Keys.Where(k => k.StartsWith("wheels/", StringComparison.Ordinal)),
            debs = files.Keys.Where(k => k.StartsWith("debs/", StringComparison.Ordinal)),
            sha256 = hashes,
        }));
        return dir;
    }

    private HyperVSettings LinuxSettings(string? prerequisites = null) => new()
    {
        Guest = GuestOs.Linux,
        ImagePath = CreateFile("debian-13-generic-amd64.raw"),
        VmName = "LocalAi-Linux",
        VmDirectory = Path.Combine(_dir, "vm-linux"),
        MemoryMB = 4096,
        OsDiskGB = 64,
        TransferDiskMB = 32,
        BuildTimeout = TimeSpan.FromSeconds(60),
        LinuxPrerequisites = prerequisites,
    };

    private string CreateFile(string name)
    {
        var path = Path.Combine(_dir, name);
        File.WriteAllText(path, "x");
        return path;
    }

    [Fact, Trait("Pruefung", "M8-Aufbau")]
    public void Seed_Laufwerk_enthaelt_cloud_init_Python_Worker_und_geprueften_Bausteine()
    {
        var sources = GuestPayloadSources.ForLinux(ContractTests.RepoRoot(), Prerequisites());
        var guest = Path.Combine(_dir, "guest");
        GuestPayloadBuilder.Build(guest, sources, new byte[32], LinuxSettings(sources.LinuxPrerequisites), ["python-linux"]);
        var seed = Path.Combine(guest, "seed");
        var userData = File.ReadAllText(Path.Combine(seed, "user-data"));
        Assert.StartsWith("#cloud-config\n", userData, StringComparison.Ordinal);
        Assert.Contains("users: []", userData, StringComparison.Ordinal);
        Assert.Contains("package_update: false", userData, StringComparison.Ordinal);
        Assert.Contains("LABEL=CIDATA", userData, StringComparison.Ordinal);
        Assert.DoesNotContain("chpasswd", userData, StringComparison.Ordinal);
        Assert.Contains("instance-id: localai-", File.ReadAllText(Path.Combine(seed, "meta-data")), StringComparison.Ordinal);
        var root = Path.Combine(seed, "localai");
        Assert.Equal(new string('0', 64), File.ReadAllText(Path.Combine(root, "session.key")));
        Assert.DoesNotContain("\r", File.ReadAllText(Path.Combine(root, "provision.sh")), StringComparison.Ordinal);
        Assert.True(File.Exists(Path.Combine(root, "pkg", "localai", "worker", "__main__.py")));
        Assert.True(File.Exists(Path.Combine(root, "pkg", "localai", "guest", "provision.py")));
        Assert.False(Directory.Exists(Path.Combine(root, "pkg", "localai", "orchestrator")));
        var files = File.ReadAllText(Path.Combine(root, "prereq", "files.txt"));
        Assert.Contains("deb debs/hyperv-daemons_6.12.48-1_amd64.deb\n", files, StringComparison.Ordinal);
        Assert.StartsWith("python cpython-3.13.5+", files, StringComparison.Ordinal);
        using var config = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "guest-config.json")));
        Assert.Equal("Linux", config.RootElement.GetProperty("guest").GetString());
        Assert.Equal(32 * 1024 * 1024, config.RootElement.GetProperty("transferDiskBytes").GetInt64());
        Assert.Equal("python-linux", config.RootElement.GetProperty("toolchains")[0].GetString());
        Assert.Equal(Path.Combine(guest, "seed", "localai", "toolchain-src"), GuestPayloadBuilder.ToolchainStaging(guest, GuestOs.Linux));

        // Zur Fehlersuche (keepVmOnExit): Kennwort für die Konsole, sonst nie.
        var debug = Path.Combine(_dir, "guest-debug");
        GuestPayloadBuilder.Build(debug, sources, new byte[32], LinuxSettings(sources.LinuxPrerequisites) with { KeepVmOnExit = true }, []);
        Assert.Contains("chpasswd", File.ReadAllText(Path.Combine(debug, "seed", "user-data")), StringComparison.Ordinal);
        Assert.True(File.Exists(Path.Combine(_dir, "vm-linux", "gast-kennwort.txt")));
    }

    [Fact, Trait("Pruefung", "M8-Aufbau")]
    public void Veraenderter_Linux_Baustein_bricht_den_Aufbau_ab()
    {
        var sources = GuestPayloadSources.ForLinux(ContractTests.RepoRoot(), Prerequisites(corrupt: true));
        var ex = Assert.Throws<InvalidDataException>(() =>
            GuestPayloadBuilder.Build(Path.Combine(_dir, "g"), sources, new byte[32], LinuxSettings(sources.LinuxPrerequisites), []));
        Assert.Contains("Hash weicht", ex.Message, StringComparison.Ordinal);
        File.Delete(Path.Combine(_dir, "linux-gast", LinuxPrerequisites.ManifestFile));
        ex = Assert.Throws<InvalidDataException>(() => LinuxPrerequisites.Load(Path.Combine(_dir, "linux-gast")));
        Assert.Contains("Get-LocalAiLinuxPrerequisites.ps1", ex.Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "M8-Aufbau")]
    public void Fingerabdruck_der_Linux_VM_folgt_Abbild_Bausteinen_und_Toolchains()
    {
        var sources = GuestPayloadSources.ForLinux(ContractTests.RepoRoot(), Prerequisites());
        var settings = LinuxSettings(sources.LinuxPrerequisites);
        var reg = new ToolchainRegistration("python-linux", "3.13.5", "/tc", "m", "d1", new Dictionary<string, string>(), "2026-10-20T00:00:00.000Z");
        var basis = SandboxFingerprint.ComputeBase(settings, sources, [reg], ["python-linux"]);
        Assert.Equal(basis, SandboxFingerprint.ComputeBase(settings, sources, [reg], ["python-linux"]));
        Assert.NotEqual(basis, SandboxFingerprint.ComputeBase(settings, sources, [reg with { Digest = "d2" }], ["python-linux"]));
        Assert.NotEqual(basis, SandboxFingerprint.ComputeBase(settings with { MemoryMB = 8192 }, sources, [reg], ["python-linux"]));
        var manifest = Path.Combine(sources.LinuxPrerequisites!, LinuxPrerequisites.ManifestFile);
        File.AppendAllText(manifest, " ");
        Assert.NotEqual(basis, SandboxFingerprint.ComputeBase(settings, sources, [reg], ["python-linux"]));
        Assert.NotEqual(SandboxFingerprint.ComputeCode(settings, sources),
            SandboxFingerprint.ComputeCode(settings with { Guest = GuestOs.Windows, ImageName = "x" }, sources));
    }

    // ---------------------------------------------------------------- Start bei Bedarf, Leerlauf, mehrere VMs

    private HyperVSettings WindowsSettings() => new()
    {
        ImagePath = CreateFile("win.iso"),
        ImageName = "Windows 11 Pro",
        VmDirectory = Path.Combine(_dir, "vm"),
        TransferDiskMB = 32,
        BuildTimeout = TimeSpan.FromSeconds(60),
    };

    private async Task<(HyperVSandbox Vm, HyperVTests.SimulatedHost Host, SandboxSession Session)> StartVmAsync(HyperVSettings settings,
        TimeSpan? idle = null, Action<HyperVTests.SimulatedHost>? configure = null)
    {
        var session = new SandboxSession("s-" + Guid.NewGuid().ToString("N")[..8]);
        var host = new HyperVTests.SimulatedHost(settings, Path.Combine(_dir, "sim-" + settings.VmName));
        configure?.Invoke(host);
        var vm = new HyperVSandbox(settings, host, guestDir =>
        {
            var root = Directory.CreateDirectory(Path.Combine(guestDir, "LocalAi")).FullName;
            File.WriteAllText(Path.Combine(root, "session.key"), Convert.ToHexStringLower(session.Key));
            return Task.CompletedTask;
        })
        {
            IdleTimeout = idle ?? TimeSpan.Zero,
            IdleCheckInterval = TimeSpan.FromMilliseconds(100),
            CopyRetryDelay = TimeSpan.FromMilliseconds(50),
        };
        _cleanup.Add(vm);
        _cleanup.Add(host);
        vm.Start();
        await WaitAsync(() => vm.Status.State is SandboxState.Ready or SandboxState.Locked);
        Assert.True(vm.Status.State == SandboxState.Ready, vm.Status.Text);
        return (vm, host, session);
    }

    private static async Task WaitAsync(Func<bool> condition, int seconds = 90)
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (!condition() && DateTime.UtcNow < deadline)
        {
            await Task.Delay(50, TestContext.Current.CancellationToken);
        }
    }

    private string ProjectFolder()
    {
        var root = Directory.CreateDirectory(Path.Combine(_dir, "projekt")).FullName;
        File.WriteAllText(Path.Combine(root, "README.md"), "# Projekt\n");
        return root;
    }

    private static ToolProposal Proposal(string tool, string snapshot, object args, IReadOnlyList<ToolTarget>? targets = null)
    {
        var now = DateTimeOffset.UtcNow;
        return new ToolProposal(1, Guid.NewGuid().ToString("D"), "run", "task", AgentRole.Coder, tool, "1", "Test",
            JsonSerializer.SerializeToElement(args), targets ?? [], snapshot, 1, "sbx-hyperv",
            ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
    }

    private SandboxToolAdapter Adapter(SandboxSession session, HyperVSandbox vm, SnapshotRegistry registry, string root, string name,
        Action<ToolProposal, SnapshotManifest, string>? retain = null)
    {
        var grant = new FolderGrant("p", root, [FolderRight.List, FolderRight.Read, FolderRight.Modify, FolderRight.Create], true);
        var context = new ProjectPolicyContext(new ProjectConfig(1, "p", "p", [grant]), registry);
        return new SandboxToolAdapter(session, vm, context, registry, Path.Combine(_dir, "scratch-" + name), SystemClock.Instance)
        {
            JobTimeout = TimeSpan.FromSeconds(30),
            SnapshotTimeout = TimeSpan.FromSeconds(30),
            RetainSnapshot = retain,
        };
    }

    [Fact, Trait("Pruefung", "M8-Leerlauf")]
    public async Task Unbenutzte_VM_faehrt_herunter_und_startet_beim_naechsten_Job()
    {
        var (vm, host, session) = await StartVmAsync(WindowsSettings(), TimeSpan.FromSeconds(1), h => h.CopyFailuresAfterStart = 2);
        await WaitAsync(() => vm.Status.State == SandboxState.Stopped, 30);
        Assert.True(vm.Status.State == SandboxState.Stopped, vm.Status.Text);
        Assert.Contains("heruntergefahren, startet beim nächsten Job", vm.Status.Text, StringComparison.Ordinal);
        Assert.StartsWith("Offline-VM bereit", vm.Status.Text, StringComparison.Ordinal);
        Assert.Equal(["Stop"], host.StateActions);
        Assert.Equal("Off", host.State);
        // Not-Aus bei ausgeschalteter VM: nichts läuft, nichts wird gesperrt.
        Assert.Contains("keine laufende Ausführung", await vm.EmergencyStopAsync(), StringComparison.Ordinal);

        var registry = new SnapshotRegistry();
        var root = ProjectFolder();
        var adapter = Adapter(session, vm, registry, root, "idle");
        var snap = await adapter.ExecuteAsync(Proposal("Workspace.Snapshot", "-", new { grantId = "p", root }), "inv-1", TestContext.Current.CancellationToken);
        Assert.True(snap.Status == ResultStatus.Succeeded, snap.TargetResults[0].Message);
        Assert.Equal(["Stop", "Start"], host.StateActions);
        // Copy-VMFile scheiterte zweimal direkt nach dem Start (Gastdienst noch nicht bereit) und wurde wiederholt.
        Assert.Equal(1, host.JobsCopied);
        // Und wieder aus, sobald nichts mehr zu tun ist.
        await WaitAsync(() => vm.Status.State == SandboxState.Stopped, 30);
        Assert.Equal(["Stop", "Start", "Stop"], host.StateActions);
    }

    [Fact, Trait("Pruefung", "M8-Leerlauf")]
    public async Task Ohne_Leerlaufzeit_bleibt_die_VM_an()
    {
        var (vm, host, _) = await StartVmAsync(WindowsSettings() with { IdleMinutes = 0 });
        await Task.Delay(500, TestContext.Current.CancellationToken);
        Assert.Equal(SandboxState.Ready, vm.Status.State);
        Assert.Empty(host.StateActions);
    }

    [Fact, Trait("Pruefung", "M8-Abgleich")]
    public async Task Toolchain_Aktion_in_der_Linux_VM_bekommt_den_Snapshot_samt_Patches_abgeglichen()
    {
        var (win, winHost, winSession) = await StartVmAsync(WindowsSettings());
        var (lin, linHost, linSession) = await StartVmAsync(LinuxSettings());
        var plan = GuestSelection.Resolve(Project(GuestOs.Windows, ["dotnet", "python-linux"]), GuestOs.Windows, ManifestGuest);
        var registry = new SnapshotRegistry();
        var root = ProjectFolder();
        MultiGuestToolAdapter? multi = null;
        var adapters = new Dictionary<GuestOs, SandboxToolAdapter>
        {
            [GuestOs.Windows] = Adapter(winSession, win, registry, root, "win", (p, m, a) => multi?.RetainSnapshot(p, m, a)),
            [GuestOs.Linux] = Adapter(linSession, lin, registry, root, "lin"),
        };
        var infos = new List<string>();
        multi = new MultiGuestToolAdapter(plan, adapters, Path.Combine(_dir, "abgleich")) { Info = infos.Add };
        var ct = TestContext.Current.CancellationToken;

        var snap = await multi.ExecuteAsync(Proposal("Workspace.Snapshot", "-", new { grantId = "p", root }), "i1", ct);
        Assert.True(snap.Status == ResultStatus.Succeeded, snap.TargetResults[0].Message);
        var baseId = snap.TargetResults.Single(t => t.TargetId == "snapshot").Text!;
        const string Patch = "--- /dev/null\n+++ b/src/neu.txt\n@@ -0,0 +1 @@\n+hallo aus dem Patch\n";
        var patched = await multi.ExecuteAsync(Proposal("Patch.Apply", baseId, new { grantId = "p", patch = Patch }, [new ToolTarget("t1", "src/neu.txt")]), "i2", ct);
        Assert.True(patched.Status == ResultStatus.Succeeded, patched.TargetResults[0].Message);
        var derived = patched.TargetResults.Single(t => t.TargetId == "snapshot").Text!;
        Assert.NotEqual(baseId, derived);
        // Bisher arbeitete nur die Heimat-VM.
        Assert.Equal(0, linHost.JobsCopied);
        Assert.Equal(2, winHost.JobsCopied);

        // Toolchain der Linux-VM: Basis und Patch werden dort wiederholt, dann läuft die Aktion dort.
        var run = await multi.ExecuteAsync(Proposal("Toolchain.Run", derived, new { grantId = "p", toolchain = "python-linux", parameters = new { } }), "i3", ct);
        Assert.Equal(3, linHost.JobsCopied);
        Assert.Equal(2, winHost.JobsCopied);
        Assert.True(multi.IsPresent(GuestOs.Linux, baseId) && multi.IsPresent(GuestOs.Linux, derived));
        Assert.Contains(infos, i => i.Contains("Linux-VM übertragen (2 Schritt(e))", StringComparison.Ordinal));
        // Der simulierte Gast hat keine Toolchains – die Aktion selbst scheitert dort, aber nicht am Abgleich.
        Assert.DoesNotContain("übertragen werden", run.TargetResults[0].Message ?? "", StringComparison.Ordinal);
        Assert.Contains("Toolchain-Aktionen", run.TargetResults[0].Message ?? "", StringComparison.Ordinal);

        // Zweiter Lauf auf demselben Stand: kein erneuter Abgleich.
        await multi.ExecuteAsync(Proposal("Toolchain.Test", derived, new { grantId = "p", toolchain = "python-linux", parameters = new { } }), "i4", ct);
        Assert.Equal(4, linHost.JobsCopied);
        Assert.Single(infos);

        // Unbekannter Stand lässt sich nicht abgleichen – klare Meldung statt Ausführung.
        var unknown = await multi.ExecuteAsync(Proposal("Toolchain.Run", new string('a', 64), new { grantId = "p", toolchain = "python-linux", parameters = new { } }), "i5", ct);
        Assert.Equal(ResultStatus.Failed, unknown.Status);
        Assert.Contains("in die Linux-VM übertragen werden", unknown.TargetResults[0].Message, StringComparison.Ordinal);
        Assert.Equal(4, linHost.JobsCopied);
        // Windows-Toolchain bleibt in der Heimat-VM.
        await multi.ExecuteAsync(Proposal("Toolchain.Run", derived, new { grantId = "p", toolchain = "dotnet", parameters = new { } }), "i6", ct);
        Assert.Equal(3, winHost.JobsCopied);
        multi.Dispose();
        Assert.False(Directory.Exists(Path.Combine(_dir, "abgleich")));
    }

    [Fact, Trait("Pruefung", "M8-Mehrere-VMs")]
    public async Task Sammelzustand_mehrerer_VMs_und_Not_Aus_fuer_alle()
    {
        var (win, _, _) = await StartVmAsync(WindowsSettings());
        var (lin, _, _) = await StartVmAsync(LinuxSettings());
        var pool = new SandboxPool(new Dictionary<GuestOs, HyperVSandbox> { [GuestOs.Windows] = win, [GuestOs.Linux] = lin });
        Assert.StartsWith("Windows: Offline-VM bereit", pool.StatusText, StringComparison.Ordinal);
        Assert.Contains(" | Linux: Offline-VM bereit", pool.StatusText, StringComparison.Ordinal);
        var stop = await pool.EmergencyStopAsync();
        Assert.Contains("Windows: keine laufende Ausführung", stop, StringComparison.Ordinal);
        Assert.Contains("Linux: keine laufende Ausführung", stop, StringComparison.Ordinal);
        lin.Lock("Test");
        string? last = null;
        pool.StatusChanged += t => last = t;
        win.Lock("auch");
        Assert.Contains("Linux: gesperrt: Test", last, StringComparison.Ordinal);
        Assert.Contains("Windows: gesperrt: auch", last, StringComparison.Ordinal);
        var single = new SandboxPool(new Dictionary<GuestOs, HyperVSandbox> { [GuestOs.Linux] = lin });
        Assert.Equal("gesperrt: Test", single.StatusText);
    }
}
