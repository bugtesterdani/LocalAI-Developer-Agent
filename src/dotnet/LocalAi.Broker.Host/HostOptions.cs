using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace LocalAi.Broker.Host;

public enum CallerRole
{
    Ui,
    Orchestrator,
}

/// <summary>Betriebsart der Ausführung.</summary>
public enum SandboxMode
{
    /// <summary>Keine Sandbox: Vorschläge werden angenommen, Ausführung ist gesperrt.</summary>
    None,

    /// <summary>Nur für automatische Tests: Testadapter zählt Aufrufe, Test-Grant "g-test".</summary>
    Test,

    /// <summary>Entwicklung: Worker läuft als lokaler Prozess auf dem Host. UNSICHER, kein VM-Schutz.</summary>
    DevLocalUnsafe,

    /// <summary>Offline-VM über Hyper-V (M2b).</summary>
    HyperV,
}

/// <summary>Zufällige Tokens pro Broker-Start; nur der UI-Token darf Entscheidungen treffen.</summary>
public sealed record RoleTokens(string Ui, string Orchestrator)
{
    public static RoleTokens Create() => new(NewToken(), NewToken());

    public CallerRole? Resolve(string? token)
    {
        if (string.IsNullOrEmpty(token))
        {
            return null;
        }
        var bytes = Encoding.ASCII.GetBytes(token);
        if (CryptographicOperations.FixedTimeEquals(bytes, Encoding.ASCII.GetBytes(Ui)))
        {
            return CallerRole.Ui;
        }
        if (CryptographicOperations.FixedTimeEquals(bytes, Encoding.ASCII.GetBytes(Orchestrator)))
        {
            return CallerRole.Orchestrator;
        }
        return null;
    }

    private static string NewToken() => Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(32));
}

public sealed record ProcessCommand(string Command, IReadOnlyList<string> Arguments, string WorkingDirectory);

public sealed record HostOptions
{
    public string DataDirectory { get; init; } = DefaultDataDirectory();
    public string? PipeName { get; init; }
    public TimeSpan ProposalTtl { get; init; } = TimeSpan.FromMinutes(5);
    public TimeSpan TestAdapterDelay { get; init; } = TimeSpan.Zero;
    public bool IgnoreStdin { get; init; }
    public SandboxMode Mode { get; init; } = SandboxMode.None;
    public string? ProjectPath { get; init; }
    public string RepoRoot { get; init; } = AppContext.BaseDirectory;
    public ProcessCommand? FakeAgent { get; init; }
    public ProcessCommand? DevWorker { get; init; }
    public ProcessCommand? Orchestrator { get; init; }
    public TimeSpan OrchestratorHeartbeatTimeout { get; init; } = TimeSpan.FromSeconds(30);
    public LocalAi.Sandbox.HyperVSettings? HyperV { get; init; }
    public string? ToolchainsDirectory { get; init; }

    /// <summary>Web.Fetch (M6); null = ausgeschaltet (ohne Konfiguration: eingeschaltet mit Standardwerten).</summary>
    public WebSettings? Web { get; init; }

    public static HostOptions Parse(string[] args)
    {
        var options = new HostOptions();
        string? configPath = null;
        SandboxMode? mode = null;
        string? project = null;
        for (var i = 0; i < args.Length; i++)
        {
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"Wert fehlt für {args[i]}");
            switch (args[i])
            {
                case "--data": options = options with { DataDirectory = Next() }; break;
                case "--pipe": options = options with { PipeName = Next() }; break;
                case "--ttl-seconds": options = options with { ProposalTtl = TimeSpan.FromSeconds(int.Parse(Next())) }; break;
                case "--test-adapter-delay-ms": options = options with { TestAdapterDelay = TimeSpan.FromMilliseconds(int.Parse(Next())) }; break;
                case "--ignore-stdin": options = options with { IgnoreStdin = true }; break;
                case "--config": configPath = Next(); break;
                case "--mode": mode = ParseMode(Next()); break;
                case "--project": project = Path.GetFullPath(Next()); break;
                default: throw new ArgumentException($"Unbekannte Option: {args[i]}");
            }
        }
        configPath ??= FindConfig();
        if (configPath is not null && File.Exists(configPath))
        {
            options = ApplyConfig(options, configPath);
        }
        if (mode is not null)
        {
            options = options with { Mode = mode.Value };
        }
        if (project is not null)
        {
            options = options with { ProjectPath = project };
        }
        return options;
    }

    public static SandboxMode ParseMode(string value) => value switch
    {
        "none" => SandboxMode.None,
        "test" => SandboxMode.Test,
        "dev-local-unsafe" => SandboxMode.DevLocalUnsafe,
        "hyperv" => SandboxMode.HyperV,
        _ => throw new ArgumentException($"Unbekannte Betriebsart: {value} (none, test, dev-local-unsafe, hyperv)"),
    };

    public static string ModeName(SandboxMode mode) => mode switch
    {
        SandboxMode.None => "none",
        SandboxMode.Test => "test",
        SandboxMode.DevLocalUnsafe => "dev-local-unsafe",
        SandboxMode.HyperV => "hyperv",
        _ => "unbekannt",
    };

    private static string DefaultDataDirectory()
    {
        var root = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(string.IsNullOrEmpty(root) ? AppContext.BaseDirectory : root, "LocalAi", "broker");
    }

    /// <summary>Sucht config/localai.json vom Programmverzeichnis aufwärts (Repository-Wurzel).</summary>
    private static string? FindConfig()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, "config", "localai.json");
            if (File.Exists(candidate))
            {
                return candidate;
            }
            dir = dir.Parent;
        }
        return null;
    }

    private static HostOptions ApplyConfig(HostOptions options, string configPath)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(configPath));
        var root = doc.RootElement;
        var repoRoot = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(configPath))!, ".."));
        options = options with { RepoRoot = repoRoot };
        if (root.TryGetProperty("fakeAgent", out var agent))
        {
            options = options with { FakeAgent = ReadCommand(agent, repoRoot) };
        }
        if (root.TryGetProperty("orchestrator", out var orchestrator) && orchestrator.ValueKind == JsonValueKind.Object)
        {
            options = options with { Orchestrator = ReadCommand(orchestrator, repoRoot) };
            if (orchestrator.TryGetProperty("heartbeatTimeoutSeconds", out var hb) && hb.ValueKind == JsonValueKind.Number)
            {
                options = options with { OrchestratorHeartbeatTimeout = TimeSpan.FromSeconds(Math.Max(2, hb.GetInt32())) };
            }
        }
        else
        {
            // Ohne eigenen Abschnitt: Standardaufruf über uv (eigene localai.json bleibt unverändert gültig).
            options = options with
            {
                Orchestrator = new ProcessCommand("uv", ["run", "--no-sync", "--project", "src/python", "python", "-m", "localai.orchestrator"], repoRoot),
            };
        }
        options = options with { Web = ReadWeb(root, repoRoot) };
        if (root.TryGetProperty("toolchains", out var toolchains) && toolchains.TryGetProperty("directory", out var tcDir)
            && tcDir.ValueKind == JsonValueKind.String && tcDir.GetString()!.Length > 0)
        {
            options = options with { ToolchainsDirectory = Path.GetFullPath(Path.Combine(repoRoot, tcDir.GetString()!)) };
        }
        if (root.TryGetProperty("project", out var project))
        {
            options = options with { ProjectPath = Path.GetFullPath(Path.Combine(repoRoot, project.GetString()!)) };
        }
        if (root.TryGetProperty("sandbox", out var sandbox))
        {
            if (sandbox.TryGetProperty("mode", out var mode))
            {
                options = options with { Mode = ParseMode(mode.GetString()!) };
            }
            if (sandbox.TryGetProperty("devWorker", out var worker))
            {
                options = options with { DevWorker = ReadCommand(worker, repoRoot) };
            }
            if (sandbox.TryGetProperty("hyperv", out var hyperv) && hyperv.ValueKind == JsonValueKind.Object)
            {
                options = options with { HyperV = ReadHyperV(hyperv, repoRoot, options.DataDirectory) };
            }
        }
        return options;
    }

    private static LocalAi.Sandbox.HyperVSettings ReadHyperV(JsonElement e, string repoRoot, string dataDirectory)
    {
        string? Str(string name) => e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String && v.GetString()!.Length > 0 ? v.GetString() : null;
        int? Int(string name) => e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : null;
        var image = Str("imagePath") ?? throw new JsonException("sandbox.hyperv.imagePath fehlt (Pfad zur Windows-ISO)");
        var vmDirectory = Str("vmDirectory") is { } dir
            ? Path.GetFullPath(Path.Combine(repoRoot, dir))
            : Path.Combine(Path.GetDirectoryName(Path.GetFullPath(dataDirectory))!, "vm");
        var defaults = new LocalAi.Sandbox.HyperVSettings { ImagePath = "-", VmDirectory = "-" };
        return new LocalAi.Sandbox.HyperVSettings
        {
            ImagePath = Path.GetFullPath(Path.Combine(repoRoot, image)),
            ImageName = Str("imageName"),
            ImageIndex = Int("imageIndex"),
            VmName = Str("vmName") ?? defaults.VmName,
            VmDirectory = vmDirectory,
            MemoryMB = Int("memoryMB") ?? defaults.MemoryMB,
            Processors = Int("processors") ?? defaults.Processors,
            OsDiskGB = Int("osDiskGB") ?? defaults.OsDiskGB,
            TransferDiskMB = Int("transferDiskMB") ?? defaults.TransferDiskMB,
            BuildTimeout = TimeSpan.FromMinutes(Int("buildTimeoutMinutes") ?? (int)defaults.BuildTimeout.TotalMinutes),
            ProductKey = Str("productKey"),
            KeepVmOnExit = e.TryGetProperty("keepVmOnExit", out var keep) && keep.ValueKind == JsonValueKind.True,
            ReuseVm = e.TryGetProperty("reuseVm", out var reuse) && reuse.ValueKind == JsonValueKind.True,
            LowIntegrityTools = !(e.TryGetProperty("lowIntegrityTools", out var low) && low.ValueKind == JsonValueKind.False),
            JobTimeoutMinutes = Math.Clamp(Int("jobTimeoutMinutes") ?? defaults.JobTimeoutMinutes, 1, 600),
            SnapshotTimeoutMinutes = Math.Clamp(Int("snapshotTimeoutMinutes") ?? defaults.SnapshotTimeoutMinutes, 1, 600),
            ToolchainExtraMinutes = Math.Clamp(Int("toolchainExtraMinutes") ?? defaults.ToolchainExtraMinutes, 1, 600),
            PauseOnNoAnswer = !(e.TryGetProperty("pauseOnNoAnswer", out var pause) && pause.ValueKind == JsonValueKind.False),
        };
    }

    private static WebSettings? ReadWeb(JsonElement root, string repoRoot)
    {
        var command = new ProcessCommand("uv", ["run", "--no-sync", "--project", "src/python", "python", "-m", "localai.web"], repoRoot);
        if (!root.TryGetProperty("web", out var web) || web.ValueKind != JsonValueKind.Object)
        {
            return new WebSettings { Command = command };
        }
        if (web.TryGetProperty("enabled", out var enabled) && enabled.ValueKind == JsonValueKind.False)
        {
            return null;
        }
        int? Int(string name) => web.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : null;
        var defaults = new WebSettings { Command = command };
        return new WebSettings
        {
            Command = web.TryGetProperty("command", out var c) && c.ValueKind == JsonValueKind.Object ? ReadCommand(c, repoRoot) : command,
            MaxBytes = Math.Clamp(Int("maxBytes") ?? (int)defaults.MaxBytes, 1024, 16 * 1024 * 1024),
            TimeoutSeconds = Math.Clamp(Int("timeoutSeconds") ?? defaults.TimeoutSeconds, 1, 120),
            MaxChars = Math.Clamp(Int("maxChars") ?? defaults.MaxChars, 1000, 200_000),
            LowIntegrity = !(web.TryGetProperty("lowIntegrity", out var low) && low.ValueKind == JsonValueKind.False),
            AllowedHosts = ReadAllowedHosts(web),
            CaFile = OptionalFile(web, "caFile", repoRoot),
            Proxy = ReadProxy(web, repoRoot),
        };
    }

    private static string? OptionalString(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String && v.GetString()!.Trim().Length > 0 ? v.GetString()!.Trim() : null;

    /// <summary>Datei muss beim Start vorhanden sein (sonst würde jeder Abruf erst im Fetcher scheitern).</summary>
    private static string? OptionalFile(JsonElement e, string name, string repoRoot)
    {
        var value = OptionalString(e, name);
        if (value is null)
        {
            return null;
        }
        var full = Path.GetFullPath(Path.Combine(repoRoot, value));
        return File.Exists(full) ? full : throw new JsonException($"web: Datei für {name} nicht gefunden: {full}");
    }

    private static IReadOnlyList<string> ReadAllowedHosts(JsonElement web)
    {
        if (!web.TryGetProperty("allowedHosts", out var list) || list.ValueKind != JsonValueKind.Array)
        {
            return [];
        }
        var result = new List<string>();
        foreach (var item in list.EnumerateArray())
        {
            var text = item.ValueKind == JsonValueKind.String ? item.GetString()! : item.ToString();
            result.Add(LocalAi.Broker.UrlPolicy.NormalizeAllowedHost(text)
                       ?? throw new JsonException($"web.allowedHosts: ungültiger Eintrag „{text}“ (z. B. docs.python.org oder *.microsoft.com)"));
        }
        return result;
    }

    private static WebProxySettings? ReadProxy(JsonElement web, string repoRoot)
    {
        if (!web.TryGetProperty("proxy", out var proxy) || proxy.ValueKind != JsonValueKind.Object)
        {
            return null;
        }
        var url = OptionalString(proxy, "url");
        if (url is null)
        {
            return null;
        }
        bool Flag(string name) => proxy.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;
        return WebProxySettings.Parse(url, OptionalString(proxy, "username"), OptionalFile(proxy, "passwordFile", repoRoot),
            Flag("dnsViaProxy"), Flag("connectByAddress"));
    }

    private static ProcessCommand ReadCommand(JsonElement element, string repoRoot)
    {
        var command = element.GetProperty("command").GetString()!;
        var arguments = element.GetProperty("arguments").EnumerateArray().Select(a => a.GetString()!).ToList();
        var workingDirectory = element.TryGetProperty("workingDirectory", out var wd) ? wd.GetString()! : ".";
        return new ProcessCommand(command, arguments, Path.GetFullPath(Path.Combine(repoRoot, workingDirectory)));
    }
}
