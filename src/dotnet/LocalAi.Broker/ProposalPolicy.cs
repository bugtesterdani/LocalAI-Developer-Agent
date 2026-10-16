using System.Text.Json;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>Beschreibung eines bekannten Tools (Schema- und Vorabpolicy).</summary>
public sealed record ToolDefinition(
    string Name,
    string Version,
    int MinTargets,
    int MaxTargets,
    bool SupportsNarrowing,
    IReadOnlySet<string> AllowedArguments);

/// <summary>
/// Schema- und Vorabpolicy-Prüfung (Zustand Proposed → AwaitingApproval oder Rejected).
/// Prüft nur die Form. Ordnerrechte folgen in M2 (B05).
/// </summary>
public sealed class ProposalPolicy
{
    private readonly BrokerOptions _options;
    private readonly Dictionary<string, ToolDefinition> _tools;

    public ProposalPolicy(BrokerOptions options)
    {
        _options = options;
        _tools = new Dictionary<string, ToolDefinition>(StringComparer.Ordinal)
        {
            ["File.Read"] = new("File.Read", "1", 1, 1, false, new HashSet<string> { "maxBytes" }),
            ["File.ReadMany"] = new("File.ReadMany", "1", 1, options.MaxTargetsPerProposal, true, new HashSet<string> { "maxBytes" }),
        };
    }

    public ToolDefinition GetTool(string name) =>
        _tools.TryGetValue(name, out var tool)
            ? tool
            : throw new BrokerException(BrokerErrorCodes.Forbidden, $"Unbekanntes Tool: {name}");

    /// <summary>Prüft eine Anfrage und gibt die normalisierten Ziele zurück.</summary>
    public IReadOnlyList<ToolTarget> Validate(ProposalRequest request)
    {
        if (request.SchemaVersion != ContractJson.SchemaVersion)
        {
            throw Invalid($"Schemaversion {request.SchemaVersion} wird nicht unterstützt");
        }
        RequireText(request.RunId, "runId", 128);
        RequireText(request.TaskId, "taskId", 128);
        RequireText(request.IdempotencyKey, "idempotencyKey", 128);
        RequireText(request.Purpose, "purpose", 2000, allowLineBreaks: true);
        RequireText(request.SnapshotId, "snapshotId", 128);
        if (!Enum.IsDefined(request.Agent))
        {
            throw Invalid("Unbekannte Agentenrolle");
        }

        var tool = GetTool(request.Tool);
        if (request.ToolVersion != tool.Version)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden,
                $"Toolversion {request.ToolVersion} für {tool.Name} nicht erlaubt (erwartet {tool.Version})");
        }

        ValidateArguments(request.Arguments, tool);

        var targets = request.Targets ?? throw Invalid("targets fehlt");
        if (targets.Count < tool.MinTargets || targets.Count > tool.MaxTargets)
        {
            throw new BrokerException(
                targets.Count > tool.MaxTargets && tool.SupportsNarrowing ? BrokerErrorCodes.TooMany : BrokerErrorCodes.Unprocessable,
                $"{tool.Name} erlaubt {tool.MinTargets} bis {tool.MaxTargets} Ziele, erhalten: {targets.Count}");
        }

        var ids = new HashSet<string>(StringComparer.Ordinal);
        var normalized = new List<ToolTarget>(targets.Count);
        foreach (var target in targets)
        {
            RequireText(target.TargetId, "targetId", 64);
            if (!ids.Add(target.TargetId))
            {
                throw Invalid($"Doppelte targetId: {target.TargetId}");
            }
            if (target.ExpectedSha256 is not null && !ProposalHashing.IsSha256Hex(target.ExpectedSha256))
            {
                throw Invalid($"expectedSha256 von {target.TargetId} ist kein SHA-256 (64 Hexzeichen, klein)");
            }
            if (target.LineStart is not null || target.LineEnd is not null)
            {
                var start = target.LineStart ?? 1;
                var end = target.LineEnd ?? long.MaxValue;
                if (start < 1 || end < start)
                {
                    throw Invalid($"Ungültiger Zeilenbereich bei {target.TargetId}");
                }
            }
            normalized.Add(target with { Path = PathRules.NormalizeRelative(target.Path) });
        }
        return normalized;
    }

    private void ValidateArguments(JsonElement arguments, ToolDefinition tool)
    {
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("arguments muss ein JSON-Objekt sein");
        }
        foreach (var property in arguments.EnumerateObject())
        {
            if (!tool.AllowedArguments.Contains(property.Name))
            {
                throw Invalid($"Argument '{property.Name}' ist für {tool.Name} nicht erlaubt");
            }
        }
        if (arguments.TryGetProperty("maxBytes", out var maxBytes))
        {
            var limit = tool.SupportsNarrowing ? _options.MaxBatchBytes : _options.MaxSingleReadBytes;
            if (maxBytes.ValueKind != JsonValueKind.Number || !maxBytes.TryGetInt64(out var value) || value < 1 || value > limit)
            {
                throw new BrokerException(BrokerErrorCodes.TooMany, $"maxBytes muss zwischen 1 und {limit} liegen");
            }
        }
        // Kanonisierbarkeit erzwingen (keine Gleitkommazahlen, keine doppelten Schlüssel).
        try
        {
            CanonicalJson.Canonicalize(arguments);
        }
        catch (CanonicalJsonException ex)
        {
            throw Invalid($"arguments nicht kanonisierbar: {ex.Message}");
        }
    }

    private static void RequireText(string? value, string field, int maxLength, bool allowLineBreaks = false)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw Invalid($"{field} fehlt");
        }
        if (value.Length > maxLength)
        {
            throw Invalid($"{field} ist länger als {maxLength} Zeichen");
        }
        if (value.Any(c => char.IsControl(c) && !(allowLineBreaks && c is '\n' or '\t')))
        {
            throw Invalid($"{field} enthält Steuerzeichen");
        }
    }

    private static BrokerException Invalid(string message) => new(BrokerErrorCodes.Unprocessable, message);
}

/// <summary>
/// Formale Pfadregeln für relative Pfade innerhalb eines Ordnergrants.
/// Ein String-Präfixvergleich genügt nicht – die endgültige Handle-Prüfung folgt im Worker (B05).
/// </summary>
public static class PathRules
{
    private static readonly HashSet<string> ReservedNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    };

    private static readonly char[] ForbiddenChars = [':', '*', '?', '"', '<', '>', '|'];

    /// <summary>Gibt den normalisierten relativen Pfad mit '/' zurück oder wirft 403.</summary>
    public static string NormalizeRelative(string? path)
    {
        if (string.IsNullOrEmpty(path) || path.Length > 1024)
        {
            throw Denied("Pfad fehlt oder ist zu lang");
        }
        if (path.Any(c => c < 0x20 || c == 0x7f))
        {
            throw Denied("Pfad enthält Steuerzeichen");
        }
        if (path.IndexOfAny(ForbiddenChars) >= 0)
        {
            throw Denied("Pfad enthält unzulässige Zeichen (z. B. ':' für Laufwerk oder Alternate Data Stream)");
        }
        if (path[0] is '/' or '\\')
        {
            throw Denied("Absolute, UNC- und Gerätepfade sind nicht erlaubt");
        }
        var segments = path.Split('/', '\\');
        foreach (var segment in segments)
        {
            if (segment.Length == 0)
            {
                throw Denied("Leeres Pfadsegment");
            }
            if (segment is "." or "..")
            {
                throw Denied("'.' und '..' sind nicht erlaubt");
            }
            if (segment.EndsWith('.') || segment.EndsWith(' ') || segment.StartsWith(' '))
            {
                throw Denied("Pfadsegmente dürfen nicht mit Punkt oder Leerzeichen enden");
            }
            var stem = segment.Split('.')[0];
            if (ReservedNames.Contains(stem))
            {
                throw Denied($"Reservierter Gerätename: {segment}");
            }
        }
        return string.Join('/', segments);
    }

    private static BrokerException Denied(string message) => new(BrokerErrorCodes.Forbidden, message);
}
