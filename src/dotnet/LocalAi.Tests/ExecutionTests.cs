using System.Diagnostics;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// M4: Patch.Apply (Abwahl pro Datei, abgeleitete Snapshots), Toolchain.* (typisierte Parameter, SEC-20, nie auf
/// dem Host), Repo.Index – Policy im Broker und Ende-zu-Ende mit dem echten Python-Worker.
/// </summary>
public sealed class ExecutionTests : IAsyncLifetime
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-m4-").FullName;
    private readonly List<Process> _workers = [];

    public ValueTask InitializeAsync() => ValueTask.CompletedTask;

    public ValueTask DisposeAsync()
    {
        foreach (var p in _workers)
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
        try
        {
            FileTools.DeleteTree(_dir);
        }
        catch (IOException)
        {
        }
        return ValueTask.CompletedTask;
    }

    private const string Patch = """
        --- a/src/Calc.cs
        +++ b/src/Calc.cs
        @@ -1,3 +1,4 @@
         class Calc
         {
        +    public int Add(int a, int b) => a + b;
         }
        --- /dev/null
        +++ b/tests/CalcTests.cs
        @@ -0,0 +1 @@
        +class CalcTests {}
        """;

    private sealed class TestCatalog(params ToolchainManifest[] manifests) : IToolchainCatalog
    {
        public IReadOnlyList<ToolchainManifest> Available => manifests;

        public ToolchainManifest? Find(string name) => manifests.FirstOrDefault(m => m.Name == name);
    }

    private static string ManifestJson(string name = "pyfake") => $$"""
        {
          "schemaVersion": 1, "name": "{{name}}", "version": "1", "displayName": "Fake", "guestPath": "C:\\Toolchains\\{{name}}",
          "license": { "proprietary": false }, "binaries": [ { "name": "python", "path": "python.exe" } ],
          "install": { "kind": "None", "expectedMinutes": 1, "check": { "binary": "python", "arguments": ["--version"] } },
          "actions": {
            "test": { "binary": "python", "arguments": ["{script}", "{results}"],
                      "parameters": { "script": { "type": "Path" } }, "timeoutSeconds": 30 },
            "build": { "binary": "python", "arguments": ["-c", "print(1)", "{configuration}"],
                       "parameters": { "configuration": { "type": "Choice", "values": ["Debug", "Release"] } } },
            "run": { "binary": "python", "arguments": ["{script}", "{filter}", "{count}"],
                     "parameters": { "script": { "type": "Path" }, "filter": { "type": "Text" }, "count": { "type": "Integer" } } }
          },
          "testResultFormat": "JUnit"
        }
        """;

    private static ToolchainManifest Manifest(string name = "pyfake") => ToolchainValidator.Parse(ManifestJson(name));

    private static ProposalRequest Request(string tool, object arguments, string snapshot = "s1", params string[] paths) => new(
        SchemaVersion: 1, RunId: "r", TaskId: "t", Agent: AgentRole.Coder, Tool: tool, ToolVersion: "1", Purpose: "Test",
        Arguments: JsonSerializer.SerializeToElement(arguments), Targets: paths.Select((p, i) => new ToolTarget($"t{i + 1}", p)).ToList(),
        SnapshotId: snapshot, IdempotencyKey: Guid.NewGuid().ToString("N"));

    private static ProposalPolicy Policy(IReadOnlyList<FolderRight> rights, IToolchainCatalog? catalog, out SnapshotRegistry registry)
    {
        registry = new SnapshotRegistry();
        registry.Add("s1", "g");
        var project = new ProjectConfig(1, "p", "P", [new FolderGrant("g", "/p", rights, true)]);
        return new ProposalPolicy(new BrokerOptions(), new ProjectPolicyContext(project, registry, catalog));
    }

    private static int Code(Action action) => Assert.Throws<BrokerException>(action).Code;

    // ------------------------------------------------------------------ Patch.Apply – Policy

    [Fact, Trait("Pruefung", "B08")]
    public void Patch_Ziele_muessen_genau_die_Dateien_des_Patches_sein()
    {
        var policy = Policy([FolderRight.List, FolderRight.Read, FolderRight.Modify, FolderRight.Create], null, out _);
        var ok = policy.Validate(Request("Patch.Apply", new { grantId = "g", patch = Patch }, "s1", "src/Calc.cs", "tests/CalcTests.cs"));
        Assert.Equal(2, ok.Count);
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Patch.Apply", new { grantId = "g", patch = Patch }, "s1", "src/Calc.cs"))));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Patch.Apply", new { grantId = "g", patch = Patch }, "s1",
            "src/Calc.cs", "tests/CalcTests.cs", "README.md"))));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Patch.Apply", new { grantId = "g", patch = "kein diff" }, "s1", "x"))));
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Patch.Apply",
            new { grantId = "g", patch = "--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n" }, "s1", "x"))));
    }

    [Fact, Trait("Pruefung", "SEC-05")]
    public void Patch_braucht_passende_Ordnerrechte()
    {
        var nurAendern = Policy([FolderRight.List, FolderRight.Read, FolderRight.Modify], null, out _);
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => nurAendern.Validate(Request("Patch.Apply", new { grantId = "g", patch = Patch }, "s1",
            "src/Calc.cs", "tests/CalcTests.cs"))));
        var loeschen = "--- a/README.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n";
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => nurAendern.Validate(Request("Patch.Apply", new { grantId = "g", patch = loeschen }, "s1", "README.md"))));
        var mitLoeschen = Policy([FolderRight.Read, FolderRight.Delete], null, out _);
        mitLoeschen.Validate(Request("Patch.Apply", new { grantId = "g", patch = loeschen }, "s1", "README.md"));
    }

    [Fact, Trait("Pruefung", "B08")]
    public void Patch_Abschnitt_je_Datei_fuer_die_Freigabekarte()
    {
        var section = PatchFiles.Section(Patch, "tests/CalcTests.cs");
        Assert.StartsWith("--- /dev/null", section, StringComparison.Ordinal);
        Assert.DoesNotContain("Calc.cs\n", section, StringComparison.Ordinal);
        Assert.Equal(new[] { new PatchFile("src/Calc.cs", PatchFileKind.Modify), new PatchFile("tests/CalcTests.cs", PatchFileKind.Create) },
            PatchFiles.Parse(Patch));
    }

    // ------------------------------------------------------------------ Toolchain.* – Policy (SEC-20)

    [Fact, Trait("Pruefung", "SEC-20")]
    public void Nicht_deklarierte_Aktion_oder_freies_Kommando_wird_abgelehnt()
    {
        var policy = Policy([FolderRight.Read, FolderRight.Execute], new TestCatalog(Manifest()), out _);
        policy.Validate(Request("Toolchain.Test", new { grantId = "g", toolchain = "pyfake", parameters = new { script = "t/run.py" } }));
        policy.Validate(Request("Toolchain.Run", new { grantId = "g", toolchain = "pyfake", parameters = new { script = "a.py", filter = "Name~Csv", count = 3 } }));

        // Keine Aktion lint/format/restore im Manifest
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Toolchain.Lint", new { grantId = "g", toolchain = "pyfake" }))));
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Toolchain.Shell", new { grantId = "g", command = "cmd /c del" }))));
        // Freies Kommando als Argument
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Toolchain.Test",
            new { grantId = "g", toolchain = "pyfake", command = "rm -rf /", parameters = new { script = "a.py" } }))));
        // Parameter: nicht deklariert, falscher Typ, Option eingeschmuggelt, Pfad hinaus, Wert außerhalb der Auswahl
        foreach (var parameters in new object[]
                 {
                     new { script = "a.py", extra = "x" },
                     new { script = 5 },
                     new { script = "--version" },
                     new { script = "../../Windows/System32/cmd.exe" },
                     new { script = "a&calc.cmd" },
                     new { script = "x%PATH%.cmd" },
                 })
        {
            Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Toolchain.Test", new { grantId = "g", toolchain = "pyfake", parameters }))));
        }
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Toolchain.Build",
            new { grantId = "g", toolchain = "pyfake", parameters = new { configuration = "Debug; del *" } }))));
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Toolchain.Run",
            new { grantId = "g", toolchain = "pyfake", parameters = new { script = "a.py", filter = "-o evil", count = 1 } }))));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Toolchain.Test", new { grantId = "g", toolchain = "pyfake" }))));
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Toolchain.Test",
            new { grantId = "g", toolchain = "andere", parameters = new { script = "a.py" } }))));
    }

    [Fact, Trait("Pruefung", "SEC-20")]
    public void Toolchain_braucht_Execute_und_Betriebsart_hyperv()
    {
        var ohneExecute = Policy([FolderRight.Read], new TestCatalog(Manifest()), out _);
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => ohneExecute.Validate(Request("Toolchain.Test",
            new { grantId = "g", toolchain = "pyfake", parameters = new { script = "a.py" } }))));
        // dev-local-unsafe/none: kein Katalog → nie Ausführung auf dem Host
        var ohneVm = Policy([FolderRight.Read, FolderRight.Execute], null, out _);
        var ex = Assert.Throws<BrokerException>(() => ohneVm.Validate(Request("Toolchain.Test",
            new { grantId = "g", toolchain = "pyfake", parameters = new { script = "a.py" } })));
        Assert.Contains("hyperv", ex.Message, StringComparison.Ordinal);
        // Ziele gibt es bei ausführenden Tools nicht (Einzelaufruf ohne Sammelfreigabe)
        var lesen = Policy([FolderRight.List, FolderRight.Read], null, out _);
        lesen.Validate(Request("Repo.Index", new { grantId = "g", maxChars = 8000 }));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => lesen.Validate(Request("Repo.Index", new { grantId = "g" }, "s1", "a.cs"))));
    }

    [Fact, Trait("Pruefung", "R09")]
    public void Projektdatei_erlaubt_Execute_und_Delete_ausdruecklich()
    {
        var root = Directory.CreateDirectory(Path.Combine(_dir, "p")).FullName;
        var path = Path.Combine(_dir, "p.json");
        File.WriteAllText(path, """{"schemaVersion":1,"projectId":"p","name":"P","grants":[{"grantId":"g","root":"p","rights":["Read","Execute","Delete"],"recursive":true}]}""");
        Assert.Equal(new[] { FolderRight.Read, FolderRight.Execute, FolderRight.Delete }, ProjectLoader.Load(path, _dir).Grants[0].Rights);
        File.WriteAllText(path, """{"schemaVersion":1,"projectId":"p","name":"P","grants":[{"grantId":"g","root":"p","rights":["Read","Read"],"recursive":true}]}""");
        Assert.Throws<InvalidDataException>(() => ProjectLoader.Load(path, _dir));
        Assert.True(Directory.Exists(root));
    }

    [Theory, Trait("Pruefung", "M4-E2E")]
    [InlineData("m4-csharp-project.json")]
    [InlineData("m4-python-project.json")]
    public void Beispielprojekte_fuer_M4_sind_gueltig(string file)
    {
        var project = ProjectLoader.Load(Path.Combine(ContractTests.RepoRoot(), "config", file), ContractTests.RepoRoot());
        var grant = Assert.Single(project.Grants);
        Assert.Contains(FolderRight.Execute, grant.Rights);
        Assert.Contains(FolderRight.Modify, grant.Rights);
        Assert.DoesNotContain(FolderRight.Delete, grant.Rights);
        Assert.Equal(["dotnet", "python"], project.Toolchains!);
    }

    [Theory, Trait("Pruefung", "M5")]
    [InlineData("m5-cpp-project.json")]
    [InlineData("m5-powershell-project.json")]
    [InlineData("m5-batch-project.json")]
    [InlineData("m5-hdevelop-project.json")]
    public void Beispielprojekte_fuer_M5_sind_gueltig_und_teilen_eine_VM(string file)
    {
        var project = ProjectLoader.Load(Path.Combine(ContractTests.RepoRoot(), "config", file), ContractTests.RepoRoot());
        var grant = Assert.Single(project.Grants);
        Assert.Contains(FolderRight.Execute, grant.Rights);
        Assert.True(Directory.Exists(grant.Root), grant.Root);
        // Gleiche Toolchains in allen M5-Projekten: Wechsel zwischen den Beispielen baut die VM nicht neu auf (reuseVm).
        Assert.Equal(["cpp", "powershell", "batch", "hdevelop"], project.Toolchains!);
    }

    [Fact, Trait("Pruefung", "B08")]
    public void No_restore_in_Aktionen_wird_beim_Registrieren_gemeldet()
    {
        var json = ManifestJson().Replace("\"arguments\": [\"-c\", \"print(1)\", \"{configuration}\"]",
            "\"arguments\": [\"-c\", \"print(1)\", \"--no-restore\", \"{configuration}\"]", StringComparison.Ordinal);
        var problems = ToolchainValidator.Validate(ToolchainValidator.Parse(json), _ => true);
        Assert.Contains(problems, p => p.Contains("--no-restore entfernen", StringComparison.Ordinal));
    }

    [Fact, Trait("Pruefung", "B08")]
    public void Abgeleiteter_Snapshot_nur_zum_bekannten_Basissnapshot_desselben_Grants()
    {
        var registry = new SnapshotRegistry();
        registry.Add("basis", "g");
        Assert.True(registry.AddDerived("neu", "basis", "g"));
        Assert.True(registry.Contains("neu", "g"));
        Assert.Equal("basis", registry.ParentOf("neu"));
        Assert.False(registry.AddDerived("x", "unbekannt", "g"));
        Assert.False(registry.AddDerived("y", "basis", "anderer"));
        Assert.False(registry.Contains("x", "g"));
    }

    [Fact, Trait("Pruefung", "B08")]
    public void Vorlage_dotnet_ist_gueltig_und_holt_Pakete_nur_aus_der_Offline_Quelle()
    {
        var manifest = ToolchainValidator.Parse(File.ReadAllText(Path.Combine(ContractTests.RepoRoot(), "toolchains", "dotnet", "toolchain.json")));
        Assert.Empty(ToolchainValidator.Validate(manifest, _ => true));
        foreach (var name in new[] { "restore", "build", "test", "run" })
        {
            Assert.Contains("-p:RestoreSources={packages}", manifest.Actions[name].Arguments);
        }
        var python = ToolchainValidator.Parse(File.ReadAllText(Path.Combine(ContractTests.RepoRoot(), "toolchains", "python", "toolchain.json")));
        Assert.Empty(ToolchainValidator.Validate(python, _ => true));
        Assert.Empty(ToolchainValidator.Validate(Manifest(), _ => true));
    }

    // ------------------------------------------------------------------ Ende zu Ende mit dem Python-Worker

    private (SandboxToolAdapter Adapter, SnapshotRegistry Registry, string Root) StartWorker(bool withToolchains)
    {
        var root = Directory.CreateDirectory(Path.Combine(_dir, "projekt")).FullName;
        Directory.CreateDirectory(Path.Combine(root, "src"));
        File.WriteAllText(Path.Combine(root, "src", "Calc.cs"), "class Calc\r\n{\r\n}\r\n");
        Directory.CreateDirectory(Path.Combine(root, "t"));
        File.WriteAllText(Path.Combine(root, "t", "run.py"), """
            import pathlib, sys
            pathlib.Path(sys.argv[1], "r.xml").write_text('<testsuite><testcase classname="Calc" name="Add"/><testcase classname="Calc" name="Sub"><failure message="erwartet 1">x</failure></testcase></testsuite>')
            print("2 Tests, 1 Fehler")
            sys.exit(1)
            """);

        var session = new SandboxSession("s-" + Guid.NewGuid().ToString("N")[..8]);
        var sandbox = Path.Combine(_dir, "sandbox");
        var transport = new LocalDirectoryTransport(Path.Combine(sandbox, "inbox"), Path.Combine(sandbox, "outbox"));
        var keyFile = Path.Combine(sandbox, "session.key");
        File.WriteAllText(keyFile, Convert.ToHexStringLower(session.Key));
        var args = new List<string> { "--inbox", transport.Inbox, "--outbox", transport.Outbox, "--work", Path.Combine(sandbox, "work"), "--key-file", keyFile, "--poll-ms", "20" };
        IToolchainCatalog? catalog = null;
        if (withToolchains)
        {
            var manifests = Directory.CreateDirectory(Path.Combine(_dir, "toolchains", "pyfake")).FullName;
            File.WriteAllText(Path.Combine(manifests, "toolchain.json"), ManifestJson());
            var install = Directory.CreateDirectory(Path.Combine(_dir, "install", "pyfake")).FullName;
            var exe = Path.Combine(install, "python.exe");
            var python3 = (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator)
                .Select(d => Path.Combine(d, "python3")).First(File.Exists);
            File.WriteAllText(exe, $"#!/bin/sh\nexec {python3} \"$@\"\n");
            if (!OperatingSystem.IsWindows())
            {
                File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            }
            args.AddRange(["--toolchains", Path.Combine(_dir, "toolchains"), "--toolchain-root", Path.Combine(_dir, "install"), "--no-low-integrity"]);
            catalog = new TestCatalog(Manifest());
        }
        var start = new ProcessStartInfo("uv") { WorkingDirectory = ContractTests.RepoRoot(), UseShellExecute = false, RedirectStandardInput = true };
        foreach (var a in new[] { "run", "--no-sync", "--project", "src/python", "python", "-m", "localai.worker" }.Concat(args))
        {
            start.ArgumentList.Add(a);
        }
        start.Environment["PYTHONUTF8"] = "1";
        var process = Process.Start(start)!;
        process.StandardInput.Close();
        _workers.Add(process);

        var grant = new FolderGrant("g", root, [FolderRight.List, FolderRight.Read, FolderRight.Modify, FolderRight.Create, FolderRight.Execute], true);
        var registry = new SnapshotRegistry();
        var context = new ProjectPolicyContext(new ProjectConfig(1, "p", "p", [grant]), registry, catalog);
        var adapter = new SandboxToolAdapter(session, transport, context, registry, Path.Combine(_dir, "scratch"), SystemClock.Instance)
        {
            JobTimeout = TimeSpan.FromSeconds(60),
            SnapshotTimeout = TimeSpan.FromSeconds(60),
            ExecutionMargin = TimeSpan.FromSeconds(60),
        };
        return (adapter, registry, root);
    }

    private static ToolProposal Proposal(string tool, string snapshot, object arguments, params string[] paths)
    {
        var now = DateTimeOffset.UtcNow;
        return new ToolProposal(1, Guid.NewGuid().ToString("D"), "run", "task", AgentRole.Coder, tool, "1", "Test",
            JsonSerializer.SerializeToElement(arguments), paths.Select((p, i) => new ToolTarget($"t{i + 1}", p)).ToList(), snapshot, 1, "sbx",
            ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
    }

    private static string Detail(ToolResult r) => string.Join(" | ", r.TargetResults.Select(t => $"{t.TargetId}: {t.Status} {t.Message}"));

    [Fact, Trait("Pruefung", "M4-E2E")]
    public async Task Snapshot_Patch_Index_und_Lesen_im_abgeleiteten_Snapshot()
    {
        var (adapter, registry, root) = StartWorker(withToolchains: false);
        var ct = TestContext.Current.CancellationToken;
        var snap = await adapter.ExecuteAsync(Proposal("Workspace.Snapshot", "-", new { grantId = "g", root }), "i0", ct);
        Assert.True(snap.Status == ResultStatus.Succeeded, Detail(snap));
        var baseId = snap.TargetResults.Single(t => t.TargetId == "snapshot").Text!;

        // Nur die Testdatei ausgewählt (Abwahl von src/Calc.cs)
        var narrowed = await adapter.ExecuteAsync(Proposal("Patch.Apply", baseId, new { grantId = "g", patch = Patch }, "tests/CalcTests.cs"), "i1", ct);
        Assert.True(narrowed.Status == ResultStatus.Succeeded, Detail(narrowed));
        var onlyTests = narrowed.TargetResults.Single(t => t.TargetId == "snapshot").Text!;
        var calc = await adapter.ExecuteAsync(Proposal("File.Read", onlyTests, new { grantId = "g" }, "src/Calc.cs"), "i2", ct);
        Assert.Equal("class Calc\r\n{\r\n}\r\n", calc.TargetResults[0].Text);

        var full = await adapter.ExecuteAsync(Proposal("Patch.Apply", baseId, new { grantId = "g", patch = Patch }, "src/Calc.cs", "tests/CalcTests.cs"), "i3", ct);
        Assert.True(full.Status == ResultStatus.Succeeded, Detail(full));
        var derived = full.TargetResults.Single(t => t.TargetId == "snapshot").Text!;
        Assert.True(registry.Contains(derived, "g"));
        Assert.Equal(baseId, registry.ParentOf(derived));
        var read = await adapter.ExecuteAsync(Proposal("File.Read", derived, new { grantId = "g" }, "src/Calc.cs"), "i4", ct);
        Assert.Equal("class Calc\r\n{\r\n    public int Add(int a, int b) => a + b;\r\n}\r\n", read.TargetResults[0].Text);
        // Hostordner bleibt unverändert
        Assert.Equal("class Calc\r\n{\r\n}\r\n", File.ReadAllText(Path.Combine(root, "src", "Calc.cs")));
        Assert.False(File.Exists(Path.Combine(root, "tests", "CalcTests.cs")));

        var map = await adapter.ExecuteAsync(Proposal("Repo.Index", derived, new { grantId = "g" }), "i5", ct);
        Assert.True(map.Status == ResultStatus.Succeeded, Detail(map));
        Assert.Contains("src/Calc.cs [C#", map.TargetResults.Single().Text, StringComparison.Ordinal);
        Assert.Contains("method Add", map.TargetResults.Single().Text, StringComparison.Ordinal);

        // Ohne VM-Freischaltung führt der Worker nichts aus
        var denied = await adapter.ExecuteAsync(Proposal("Toolchain.Test", derived, new { grantId = "g", toolchain = "pyfake", parameters = new { script = "t/run.py" } }), "i6", ct);
        Assert.Equal(ResultStatus.Failed, denied.Status);
        Assert.Contains("nie auf dem Host", denied.TargetResults[0].Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "M4-E2E")]
    public async Task Toolchain_Test_liefert_Exitcode_Log_und_einheitlichen_Testbericht()
    {
        if (OperatingSystem.IsWindows())
        {
            Assert.Skip("Toolchain-Ersatz ist ein POSIX-Skript; unter Windows prüft das die VM (docs/pruefung-M4.md)");
        }
        var (adapter, _, root) = StartWorker(withToolchains: true);
        var ct = TestContext.Current.CancellationToken;
        var snap = await adapter.ExecuteAsync(Proposal("Workspace.Snapshot", "-", new { grantId = "g", root }), "i0", ct);
        var baseId = snap.TargetResults.Single(t => t.TargetId == "snapshot").Text!;
        var proposal = Proposal("Toolchain.Test", baseId, new { grantId = "g", toolchain = "pyfake", parameters = new { script = "t/run.py" } });
        Assert.Equal(TimeSpan.FromSeconds(90), adapter.TimeoutFor(proposal));
        var result = await adapter.ExecuteAsync(proposal, "i1", ct);
        Assert.True(result.Status == ResultStatus.Succeeded, Detail(result));
        Assert.Equal(1L, result.ExitCode);
        Assert.Contains("2 Tests, 1 Fehler", result.TargetResults.Single(t => t.TargetId == "log").Text, StringComparison.Ordinal);
        var report = ContractJson.Deserialize<TestReport>(result.TargetResults.Single(t => t.TargetId == "tests").Text!);
        Assert.Equal((2, 1, 1), ((int)report.Total, (int)report.Passed, (int)report.Failed));
        Assert.Equal("Calc.Sub", report.Failures.Single().Name);
        Assert.Equal("erwartet 1", report.Failures.Single().Message);
    }
}
