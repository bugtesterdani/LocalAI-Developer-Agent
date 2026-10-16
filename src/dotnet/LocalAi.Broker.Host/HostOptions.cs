using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace LocalAi.Broker.Host;

public enum CallerRole
{
    Ui,
    Orchestrator,
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

public sealed record OrchestratorCommand(string Command, IReadOnlyList<string> Arguments, string WorkingDirectory);

public sealed record HostOptions
{
    public string DataDirectory { get; init; } = DefaultDataDirectory();
    public string? PipeName { get; init; }
    public TimeSpan ProposalTtl { get; init; } = TimeSpan.FromMinutes(5);
    public TimeSpan TestAdapterDelay { get; init; } = TimeSpan.Zero;
    public bool IgnoreStdin { get; init; }
    public OrchestratorCommand? FakeAgent { get; init; }

    public static HostOptions Parse(string[] args)
    {
        var options = new HostOptions();
        string? configPath = null;
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
                default: throw new ArgumentException($"Unbekannte Option: {args[i]}");
            }
        }
        configPath ??= FindConfig();
        if (configPath is not null && File.Exists(configPath))
        {
            options = options with { FakeAgent = ReadFakeAgent(configPath) };
        }
        return options;
    }

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

    private static OrchestratorCommand? ReadFakeAgent(string configPath)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(configPath));
        if (!doc.RootElement.TryGetProperty("fakeAgent", out var agent))
        {
            return null;
        }
        var repoRoot = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(configPath))!, ".."));
        var command = agent.GetProperty("command").GetString()!;
        var arguments = agent.GetProperty("arguments").EnumerateArray().Select(a => a.GetString()!).ToList();
        var workingDirectory = agent.TryGetProperty("workingDirectory", out var wd) ? wd.GetString()! : ".";
        return new OrchestratorCommand(command, arguments, Path.GetFullPath(Path.Combine(repoRoot, workingDirectory)));
    }
}
