using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>
/// Betriebssystem-Debugging (M9) aus der Projektdatei (<c>osDebug</c>), geprüft beim Laden. <see cref="WorkerConfig"/> ist
/// genau das, was der Worker zum Start bekommt – ein Agent kann davon nichts ändern (die Policy vergleicht es).
/// </summary>
public sealed record OsDebugSettings(string GrantId, JsonElement WorkerConfig, int MaxCommands, int MaxMinutes, GuestOs Guest)
{
    public const int DefaultMaxCommands = 500;
    public const int DefaultMaxMinutes = 60;
    private static readonly HashSet<string> Machines = new(StringComparer.Ordinal) { "pc", "q35" };

    public static OsDebugSettings FromProject(OsDebugConfig c)
    {
        void Require(bool ok, string message)
        {
            if (!ok)
            {
                throw new InvalidDataException("osDebug: " + message);
            }
        }
        string image;
        try
        {
            image = PathRules.NormalizeRelative(c.Image);
        }
        catch (BrokerException ex)
        {
            throw new InvalidDataException($"osDebug.image: {ex.Message}");
        }
        Require(c.Machine is null || Machines.Contains(c.Machine), "machine: pc oder q35");
        Require(c.MemoryMB is null or (>= 16 and <= 4096), "memoryMB: 16 bis 4096");
        Require(c.Cpus is null or (>= 1 and <= 4), "cpus: 1 bis 4");
        Require(c.MaxCommands is null or (>= 1 and <= 5000), "maxCommands: 1 bis 5000");
        Require(c.MaxMinutes is null or (>= 1 and <= 480), "maxMinutes: 1 bis 480");
        Require((c.Guest ?? GuestOs.Linux) == GuestOs.Linux, "guest: QEMU läuft in der Linux-VM (Linux)");
        var config = new JsonObject
        {
            ["image"] = image,
            ["media"] = (c.Media ?? OsDebugMedia.Cdrom).ToString(),
            ["machine"] = c.Machine ?? "pc",
            ["memoryMB"] = c.MemoryMB ?? 128,
            ["cpus"] = c.Cpus ?? 1,
            ["firmware"] = (c.Firmware ?? OsDebugFirmware.Bios).ToString(),
            ["accel"] = (c.Accel ?? OsDebugAccel.Tcg).ToString(),
        };
        if (c.Build is { } b)
        {
            Require(Regex.IsMatch(b.Toolchain, "^[a-z][a-z0-9-]{0,31}$"), "build.toolchain ungültig");
            Require(b.Action is "build" or "run", "build.action: build oder run");
            Require(b.Parameters is null || b.Parameters.Value.ValueKind == JsonValueKind.Object, "build.parameters muss ein Objekt sein");
            config["build"] = new JsonObject
            {
                ["toolchain"] = b.Toolchain,
                ["action"] = b.Action,
                ["parameters"] = b.Parameters is { } p ? JsonNode.Parse(p.GetRawText()) : new JsonObject(),
            };
        }
        return new OsDebugSettings(c.GrantId, JsonSerializer.SerializeToElement(config), (int)(c.MaxCommands ?? DefaultMaxCommands),
            (int)(c.MaxMinutes ?? DefaultMaxMinutes), GuestOs.Linux);
    }

    /// <summary>Toolchain des Builds (muss im Projekt gewählt sein).</summary>
    public string? BuildToolchain =>
        WorkerConfig.TryGetProperty("build", out var b) ? b.GetProperty("toolchain").GetString() : null;
}

/// <summary>Prüfung der Werkzeuge OsDebug.Start/Command/Stop (Form und Grenzen; der Worker prüft erneut).</summary>
public static class OsDebugTools
{
    public const string Start = "OsDebug.Start";
    public const string Command = "OsDebug.Command";
    public const string Stop = "OsDebug.Stop";

    public static readonly IReadOnlySet<string> StartArguments =
        new HashSet<string>(StringComparer.Ordinal) { "grantId", "config", "maxCommands", "maxMinutes", "allowInput", "haltAtStart" };

    public static readonly IReadOnlySet<string> CommandArguments = new HashSet<string>(StringComparer.Ordinal)
    {
        "sessionId", "command", "address", "count", "length", "physical", "kind", "remove", "waitMs", "keys", "text", "dx", "dy",
        "button", "action", "topic", "mode",
    };

    /// <summary>Befehle; die zweite Menge sind Eingaben in die VM (nur mit allowInput).</summary>
    public static readonly IReadOnlySet<string> Commands = new HashSet<string>(StringComparer.Ordinal)
    {
        "continue", "pause", "step", "break", "registers", "info", "memory", "disassemble", "screenshot", "screen-text", "keys",
        "mouse", "serial", "reset", "events",
    };

    public static readonly IReadOnlySet<string> InputCommands = new HashSet<string>(StringComparer.Ordinal) { "keys", "mouse" };

    public static bool IsSessionTool(string tool) => tool is Command or Stop;

    private static readonly Regex SessionId = new("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");

    public static void ValidateStart(JsonElement args, OsDebugSettings? settings)
    {
        if (settings is null)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, "Betriebssystem-Debugging ist für dieses Projekt nicht eingerichtet (osDebug in der Projektdatei)");
        }
        if (!args.TryGetProperty("grantId", out var g) || g.GetString() != settings.GrantId)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, $"OsDebug.Start nur für den Ordner {settings.GrantId}");
        }
        if (!args.TryGetProperty("config", out var config)
            || CanonicalJson.Canonicalize(config) != CanonicalJson.Canonicalize(settings.WorkerConfig))
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, "config muss genau der Einstellung osDebug der Projektdatei entsprechen");
        }
        Range(args, "maxCommands", 1, settings.MaxCommands, required: true);
        Range(args, "maxMinutes", 1, settings.MaxMinutes, required: true);
        Bool(args, "allowInput");
        Bool(args, "haltAtStart");
    }

    public static void ValidateCommand(string tool, JsonElement args)
    {
        if (!args.TryGetProperty("sessionId", out var s) || s.ValueKind != JsonValueKind.String || !SessionId.IsMatch(s.GetString()!))
        {
            throw Invalid("sessionId fehlt oder ist ungültig");
        }
        if (tool == Stop)
        {
            if (args.EnumerateObject().Any(p => p.Name != "sessionId"))
            {
                throw Invalid("OsDebug.Stop hat nur sessionId");
            }
            return;
        }
        var command = args.TryGetProperty("command", out var c) && c.ValueKind == JsonValueKind.String ? c.GetString()! : "";
        if (!Commands.Contains(command))
        {
            throw Invalid($"command: {string.Join(", ", Commands.Order(StringComparer.Ordinal))}");
        }
        foreach (var p in args.EnumerateObject())
        {
            var ok = p.Name switch
            {
                "address" => p.Value.ValueKind == JsonValueKind.String && Regex.IsMatch(p.Value.GetString()!, "^(0x)?[0-9a-fA-F]{1,16}$"),
                "count" or "length" or "waitMs" or "dx" or "dy" => p.Value.ValueKind == JsonValueKind.Number && p.Value.TryGetInt64(out var n)
                                                                    && n is >= -2000 and <= 600_000,
                "physical" or "remove" => p.Value.ValueKind is JsonValueKind.True or JsonValueKind.False,
                "kind" => p.Value.GetString() is "sw" or "hw",
                "button" => p.Value.GetString() is "left" or "right" or "middle",
                "action" => p.Value.GetString() is "click" or "down" or "up",
                "mode" => p.Value.ValueKind == JsonValueKind.String ? p.Value.GetString() == "auto" : p.Value.ValueKind == JsonValueKind.Number,
                "topic" => p.Value.ValueKind == JsonValueKind.String && Regex.IsMatch(p.Value.GetString()!, "^[a-z]{1,12}$"),
                "text" => p.Value.ValueKind == JsonValueKind.String && p.Value.GetString()!.Length is > 0 and <= 200,
                "keys" => p.Value.ValueKind == JsonValueKind.Array && p.Value.GetArrayLength() is > 0 and <= 200
                          && p.Value.EnumerateArray().All(k => k.ValueKind == JsonValueKind.String && Regex.IsMatch(k.GetString()!, "^[a-z0-9_-]{1,40}$")),
                _ => true,
            };
            if (!ok)
            {
                throw Invalid($"{p.Name}: ungültiger Wert");
            }
        }
    }

    private static void Range(JsonElement args, string name, long low, long high, bool required)
    {
        if (!args.TryGetProperty(name, out var v))
        {
            if (required)
            {
                throw Invalid($"{name} fehlt");
            }
            return;
        }
        if (v.ValueKind != JsonValueKind.Number || !v.TryGetInt64(out var n) || n < low || n > high)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, $"{name} muss zwischen {low} und {high} liegen (Grenze aus osDebug)");
        }
    }

    private static void Bool(JsonElement args, string name)
    {
        if (args.TryGetProperty(name, out var v) && v.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
        {
            throw Invalid($"{name} muss true oder false sein");
        }
    }

    private static BrokerException Invalid(string message) => new(BrokerErrorCodes.Unprocessable, message);
}
