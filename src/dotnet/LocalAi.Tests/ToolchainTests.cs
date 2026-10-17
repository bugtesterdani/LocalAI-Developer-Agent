using System.Text;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Desktop.Core;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>Toolchain-System: Manifestprüfung, Registrierung mit Hashes, Bereitstellung für den VM-Aufbau (R12, SEC-19/-20/-24).</summary>
public sealed class ToolchainTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-tc-").FullName;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 10, 17, 9, 0, 0, TimeSpan.Zero));

    public void Dispose() => LocalAi.Sandbox.FileTools.DeleteTree(_dir);

    private static ToolchainManifest Reference(string name) =>
        ToolchainValidator.Parse(File.ReadAllText(Path.Combine(ContractTests.RepoRoot(), "toolchains", name, "toolchain.json")));

    private const string DemoManifest = """
        {
          "schemaVersion": 1, "name": "demo", "version": "1.0", "displayName": "Demo",
          "guestPath": "C:\\Toolchains\\demo",
          "license": { "proprietary": false, "note": "MIT" },
          "binaries": [ { "name": "demo", "path": "bin/demo.exe" } ],
          "install": { "kind": "Archive", "file": "demo.zip", "expectedMinutes": 1,
                       "check": { "binary": "demo", "arguments": ["--version"], "expectOutput": "1.0" } },
          "selfTest": { "directory": "selftest", "steps": [ { "binary": "demo", "arguments": ["{selftest}\\a.txt"] } ], "expectOutput": "ok" },
          "actions": { "build": { "binary": "demo", "arguments": ["build", "{project}"], "parameters": { "project": { "type": "Path" } } } },
          "testResultFormat": "ExitCode"
        }
        """;

    private string ToolchainsRoot => Path.Combine(_dir, "toolchains");

    private string CreateDemo(string folder = "demo-1.0", string manifest = DemoManifest)
    {
        var dir = Directory.CreateDirectory(Path.Combine(ToolchainsRoot, folder)).FullName;
        File.WriteAllText(Path.Combine(dir, "toolchain.json"), manifest);
        File.WriteAllBytes(Path.Combine(dir, "demo.zip"), Encoding.ASCII.GetBytes("zip-inhalt"));
        Directory.CreateDirectory(Path.Combine(dir, "selftest"));
        File.WriteAllText(Path.Combine(dir, "selftest", "a.txt"), "ok");
        return dir;
    }

    private ToolchainRegistry Registry(params string[] project) =>
        new(Path.Combine(_dir, "data", "toolchains.json"), ToolchainsRoot, project, _clock);

    private static ToolchainManifest Demo(Func<string, string>? change = null) =>
        ToolchainValidator.Parse(change is null ? DemoManifest : change(DemoManifest));

    [Theory, Trait("Pruefung", "R12")]
    [InlineData("dotnet")]
    [InlineData("python")]
    public void Referenz_Manifeste_sind_gueltig(string name)
    {
        var problems = ToolchainValidator.Validate(Reference(name), _ => true);
        Assert.True(problems.Count == 0, string.Join("; ", problems));
    }

    [Fact, Trait("Pruefung", "SEC-20")]
    public void Manifeste_ohne_freie_Kommandos()
    {
        Assert.Empty(ToolchainValidator.Validate(Demo(), _ => true));
        void Bad(string from, string to, string expected)
        {
            var problems = ToolchainValidator.Validate(Demo(m => m.Replace(from, to, StringComparison.Ordinal)), _ => true);
            Assert.Contains(problems, p => p.Contains(expected, StringComparison.Ordinal));
        }
        Bad(@"C:\\Toolchains\\demo", @"C:\\Windows", "guestPath");
        Bad("\"build\", \"{project}\"", "\"build\", \"--x={project}\"", "ganzes Argument");
        Bad("\"build\", \"{project}\"", "\"build\", \"{project}\", \"{other}\"", "nicht deklariert");
        Bad("\"actions\": { \"build\": { \"binary\": \"demo\"", "\"actions\": { \"build\": { \"binary\": \"cmd\"", "Binary cmd");
        Bad("\"actions\": { \"build\"", "\"actions\": { \"shell\"", "Unbekannte Aktion");
        Bad("\"proprietary\": false, \"note\": \"MIT\"", "\"proprietary\": true", "Lizenzhinweis");
        Bad("\"bin/demo.exe\"", "\"../demo.exe\"", "relative .exe");
        Bad("\"{selftest}\\\\a.txt\"", "\"{unbekannt}\"", "unbekannter Platzhalter");
        Bad("\"testResultFormat\"", "\"environment\": { \"PATH\": \"C:\\\\x\" }, \"testResultFormat\"", "PATH");
        Bad("{ \"type\": \"Path\" }", "{ \"type\": \"Choice\" }", "values");
        Bad("\"--version\"", "\"--version\\nformat c:\"", "Steuerzeichen");
        Assert.Contains(ToolchainValidator.Validate(Demo(), f => f != "demo.zip"), p => p.Contains("ZIP-Archiv", StringComparison.Ordinal));
        Assert.Throws<JsonException>(() => ToolchainValidator.Parse(DemoManifest.Replace("\"schemaVersion\": 1,", "\"schemaVersion\": 1, \"shell\": \"cmd\",", StringComparison.Ordinal)));
    }

    [Fact, Trait("Pruefung", "SEC-19")]
    public void Registrierung_haelt_Hashes_fest_und_erkennt_Aenderungen()
    {
        var dir = CreateDemo();
        var registry = Registry("demo");
        var info = Assert.Single(registry.Scan(TestContext.Current.CancellationToken));
        Assert.Empty(info.Problems);
        Assert.False(info.Registered);
        Assert.True(info.UsedByProject);
        Assert.Equal(2, info.FileCount); // demo.zip, selftest/a.txt (toolchain.json zählt extra)

        var ex = Assert.Throws<ToolchainException>(() => registry.Register(dir, new string('0', 64), TestContext.Current.CancellationToken));
        Assert.Contains("verändert", ex.Message, StringComparison.Ordinal);
        var registration = registry.Register(dir, info.Digest, TestContext.Current.CancellationToken);
        Assert.Equal("demo", registration.Name);
        Assert.Equal(2, registration.Files.Count);
        Assert.True(Assert.Single(registry.Scan(TestContext.Current.CancellationToken)).Registered);

        // Manipulation nach der Registrierung: sichtbar und Aufbau verweigert.
        File.WriteAllText(Path.Combine(dir, "demo.zip"), "manipuliert");
        Assert.Contains(Assert.Single(registry.Scan(TestContext.Current.CancellationToken)).Problems,
            p => p.Contains("seit der Registrierung", StringComparison.Ordinal));
        var stage = Path.Combine(_dir, "stage");
        Assert.Contains("Hash weicht ab", Assert.Throws<ToolchainException>(() => registry.Stage(stage, TestContext.Current.CancellationToken)).Message,
            StringComparison.Ordinal);

        File.WriteAllBytes(Path.Combine(dir, "demo.zip"), Encoding.ASCII.GetBytes("zip-inhalt"));
        Assert.Equal(["demo"], registry.Stage(Path.Combine(_dir, "stage2"), TestContext.Current.CancellationToken));
        Assert.True(File.Exists(Path.Combine(_dir, "stage2", "demo", "pinned.json")));
        Assert.True(File.Exists(Path.Combine(_dir, "stage2", "demo", "selftest", "a.txt")));

        File.WriteAllText(Path.Combine(dir, "neu.dll"), "x");
        Assert.Contains("neue Datei", Assert.Throws<ToolchainException>(() => registry.Stage(Path.Combine(_dir, "stage3"), TestContext.Current.CancellationToken)).Message,
            StringComparison.Ordinal);

        Assert.True(registry.Unregister("demo"));
        Assert.False(registry.Unregister("demo"));
    }

    [Fact, Trait("Pruefung", "SEC-24")]
    public void Projekt_Toolchain_ohne_Registrierung_verhindert_den_Aufbau()
    {
        CreateDemo();
        var ex = Assert.Throws<ToolchainException>(() => Registry("demo").Stage(Path.Combine(_dir, "stage"), TestContext.Current.CancellationToken));
        Assert.Contains("nicht registriert", ex.Message, StringComparison.Ordinal);
        Assert.Empty(Registry().Stage(Path.Combine(_dir, "stage-leer"), TestContext.Current.CancellationToken));
    }

    [Fact, Trait("Pruefung", "SEC-19")]
    public void Erwartete_Hashes_im_Manifest_und_fremde_Ordner()
    {
        var manifest = DemoManifest.Replace("\"testResultFormat\"", $"\"files\": {{ \"demo.zip\": \"{new string('a', 64)}\" }}, \"testResultFormat\"", StringComparison.Ordinal);
        var dir = CreateDemo(manifest: manifest);
        var info = Assert.Single(Registry().Scan(TestContext.Current.CancellationToken));
        Assert.Contains(info.Problems, p => p.Contains("Hash weicht ab: demo.zip", StringComparison.Ordinal));
        Assert.Throws<ToolchainException>(() => Registry().Register(dir, info.Digest, TestContext.Current.CancellationToken));

        var outside = Directory.CreateDirectory(Path.Combine(_dir, "woanders", "demo")).FullName;
        Assert.Contains("konfigurierten Toolchain-Ordner",
            Assert.Throws<ToolchainException>(() => Registry().Register(outside, "x", TestContext.Current.CancellationToken)).Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "SEC-05")]
    public void Verknuepfungen_im_Toolchain_Ordner_werden_abgelehnt()
    {
        var dir = CreateDemo();
        try
        {
            File.CreateSymbolicLink(Path.Combine(dir, "link.txt"), Path.Combine(_dir, "irgendwo.txt"));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            Assert.Skip("Symbolische Verknüpfungen hier nicht erlaubt");
        }
        Assert.Contains(Assert.Single(Registry().Scan(TestContext.Current.CancellationToken)).Problems,
            p => p.Contains("Verknüpfung", StringComparison.Ordinal));
    }

    [Fact, Trait("Pruefung", "SEC-24")]
    public void Bericht_prueft_alle_Projekt_Toolchains()
    {
        static byte[] Report(object toolchains) => JsonSerializer.SerializeToUtf8Bytes(new
        {
            schemaVersion = 1,
            networkAdapters = 0,
            probes = Enumerable.Range(0, 4).Select(i => new { name = $"p{i}", reachable = false }),
            worker = new { running = true, python = "3.12" },
            toolchains,
            errors = Array.Empty<string>(),
        });
        var ok = Report(new[] { new { name = "dotnet", version = "9.0.306", installed = true, selfTest = "ok", error = (string?)null } });
        var (good, text) = GuestReport.Evaluate(ok, 0, ["dotnet"]);
        Assert.True(good, text);
        Assert.Contains("dotnet 9.0.306 (Hallo-Welt ok)", text, StringComparison.Ordinal);
        Assert.False(GuestReport.Evaluate(ok, 0, ["dotnet", "python"]).Ok);
        var failed = Report(new[] { new { name = "dotnet", version = "9", installed = false, selfTest = "fehlgeschlagen", error = (string?)"Selbsttest: Ausgabe fehlt" } });
        Assert.Contains("Selbsttest", GuestReport.Evaluate(failed, 0, ["dotnet"]).Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "R12")]
    public async Task Oberflaeche_registriert_nur_nach_Bestaetigung()
    {
        var api = new ToolchainApi();
        var main = new MainViewModel(api, api, a => a(), Guid.NewGuid().ToString("D"));
        api.List.Add(Info("frei", proprietary: false));
        api.List.Add(Info("halcon", proprietary: true));
        api.List.Add(Info("kaputt", proprietary: false, problems: ["Hash weicht ab: x"]));
        await main.ScanToolchainsAsync();
        Assert.Equal(3, main.Toolchains.Count);
        Assert.False(main.Toolchains[2].RegisterCommand.CanExecute(null));
        Assert.Contains("Hash weicht ab", main.Toolchains[2].ProblemsText, StringComparison.Ordinal);

        await main.RegisterToolchainAsync(main.Toolchains[0]);
        Assert.Equal(["/tc/frei"], api.Registered);

        main.ConfirmLicense = _ => false;
        await main.RegisterToolchainAsync(main.Toolchains.Single(t => t.Name == "halcon"));
        Assert.Single(api.Registered);
        main.ConfirmLicense = info => info.Name == "halcon";
        await main.RegisterToolchainAsync(main.Toolchains.Single(t => t.Name == "halcon"));
        Assert.Equal(2, api.Registered.Count);
        Assert.Equal("1,5 MiB", ToolchainItemViewModel.FormatSize(1572864));
    }

    private static ToolchainInfo Info(string name, bool proprietary, IReadOnlyList<string>? problems = null) =>
        new(name, "1", name, "/tc/" + name, new string('d', 64), 3, 1000, false, proprietary, problems ?? [], true, LicenseNote: "Lizenz X");

    private sealed class ToolchainApi : IBrokerUiApi
    {
        public List<ToolchainInfo> List { get; } = [];
        public List<string> Registered { get; } = [];
        public Task<IReadOnlyList<ProposalView>> PendingAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<ProposalView>>([]);
        public Task<ProposalView> DecideAsync(UserDecision decision, CancellationToken ct) => throw new NotSupportedException();
        public Task<ProposalView> NarrowAsync(NarrowRequest request, CancellationToken ct) => throw new NotSupportedException();
        public Task<IReadOnlyList<BrokerEvent>> WaitEventsAsync(long afterSeq, int timeoutMs, CancellationToken ct) => Task.FromResult<IReadOnlyList<BrokerEvent>>([]);
        public Task EmergencyStopAsync(CancellationToken ct) => Task.CompletedTask;
        public Task ReleaseAsync(CancellationToken ct) => Task.CompletedTask;
        public Task<JsonElement> StartFakeAgentAsync(CancellationToken ct) => throw new NotSupportedException();
        public Task<IReadOnlyList<FolderGrant>> GrantsAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<FolderGrant>>([]);
        public Task<JsonElement> StatusAsync(CancellationToken ct) => Task.FromResult(JsonDocument.Parse("{}").RootElement.Clone());
        public Task<IReadOnlyList<ToolchainInfo>> ScanToolchainsAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<ToolchainInfo>>(List);
        public Task RegisterToolchainAsync(string directory, string digest, CancellationToken ct)
        {
            Registered.Add(directory);
            return Task.CompletedTask;
        }
        public Task UnregisterToolchainAsync(string name, CancellationToken ct) => Task.CompletedTask;
    }
}
