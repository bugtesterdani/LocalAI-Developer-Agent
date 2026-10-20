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
    IReadOnlySet<string> AllowedArguments,
    IReadOnlyList<FolderRight> RequiredRights,
    bool AllowRootTarget = false,
    bool RequiresSnapshot = true,
    bool RequiresGrant = true);

/// <summary>Ordnerrechte und bekannte Snapshots – vom Host bereitgestellt.</summary>
public interface IPolicyContext
{
    FolderGrant? FindGrant(string grantId);

    bool HasSnapshot(string snapshotId, string grantId);

    IReadOnlyList<FolderGrant> Grants { get; }

    /// <summary>Installierte Toolchains der Sandbox; null = Toolchain-Aktionen gesperrt (alles außer hyperv).</summary>
    IToolchainCatalog? Toolchains => null;

    /// <summary>Webabrufe (Web.Fetch/Web.Content); null = ausgeschaltet.</summary>
    IWebCaptures? WebCaptures => null;

    /// <summary>Vorbereitete Übernahmen (Workspace.ExportPatch); null = nicht verfügbar.</summary>
    IExportStaging? Exports => null;

    /// <summary>Betriebssystem-Debugging mit QEMU (M9); null = nicht eingerichtet.</summary>
    OsDebugSettings? OsDebug => null;

    /// <summary>Ist <paramref name="snapshotId"/> aus <paramref name="baseId"/> abgeleitet (Patch.Apply-Kette)?</summary>
    bool IsDerivedFrom(string snapshotId, string baseId, string grantId) => false;
}

/// <summary>
/// Nur für Tests: ein Grant "g-test" mit allen Leserechten, jeder Snapshot gilt als vorhanden.
/// Wird ausschließlich im Testmodus des Brokers verwendet.
/// </summary>
public sealed class TestPolicyContext : IPolicyContext
{
    public static readonly FolderGrant Grant = new("g-test", "/test", [FolderRight.List, FolderRight.Read], Recursive: true);

    public IReadOnlyList<FolderGrant> Grants { get; } = [Grant];

    public FolderGrant? FindGrant(string grantId) => grantId == Grant.GrantId ? Grant : null;

    public bool HasSnapshot(string snapshotId, string grantId) => grantId == Grant.GrantId;
}

/// <summary>
/// Schema- und Vorabpolicy-Prüfung (Zustand Proposed → AwaitingApproval oder Rejected).
/// Prüft Form, Ordnerrechte und Snapshot-Zugehörigkeit. Die endgültige Prüfung jedes Zugriffs
/// erfolgt zusätzlich in der Sandbox gegen das Snapshot-Manifest.
/// </summary>
public sealed class ProposalPolicy
{
    private readonly BrokerOptions _options;
    private readonly IPolicyContext _context;
    private readonly Dictionary<string, ToolDefinition> _tools;

    public ProposalPolicy(BrokerOptions options, IPolicyContext context)
    {
        _options = options;
        _context = context;
        FolderRight[] read = [FolderRight.Read];
        _tools = new Dictionary<string, ToolDefinition>(StringComparer.Ordinal)
        {
            ["Workspace.Snapshot"] = new("Workspace.Snapshot", "1", 0, 0, false,
                new HashSet<string> { "grantId", "root" }, [FolderRight.List, FolderRight.Read], RequiresSnapshot: false),
            ["File.List"] = new("File.List", "1", 1, 50, true, new HashSet<string> { "grantId", "depth" },
                [FolderRight.List], AllowRootTarget: true),
            ["File.Read"] = new("File.Read", "1", 1, 1, false, new HashSet<string> { "grantId", "maxBytes" }, read),
            ["File.ReadMany"] = new("File.ReadMany", "1", 1, options.MaxTargetsPerProposal, true,
                new HashSet<string> { "grantId", "maxBytes" }, read),
            ["File.Search"] = new("File.Search", "1", 1, 50, true, new HashSet<string> { "grantId", "query", "maxResults" },
                read, AllowRootTarget: true),
            // Schreibt nur in einen abgeleiteten Snapshot in der Sandbox (Arbeitskopie), nie in den Hostordner.
            // Benötigte Rechte (Create/Modify/Delete) hängen von den Dateien im Patch ab.
            ["Patch.Apply"] = new("Patch.Apply", "1", 1, options.MaxTargetsPerProposal, true,
                new HashSet<string> { "grantId", "patch" }, read),
            ["Repo.Index"] = new("Repo.Index", "1", 0, 0, false, new HashSet<string> { "grantId", "query", "maxChars" },
                [FolderRight.List, FolderRight.Read]),
            // M6: genau eine HTTPS-URL; Rohdaten bleiben in der Quarantäne (SEC-09). Einzelaufruf, ohne Ordner.
            ["Web.Fetch"] = new("Web.Fetch", "1", 0, 0, false, new HashSet<string> { "url" }, [],
                RequiresSnapshot: false, RequiresGrant: false),
            // Zweite, eigene Entscheidung: exakt dieser bereinigte Text (Hash) an genau diesen Agenten.
            ["Web.Content"] = new("Web.Content", "1", 0, 0, false, new HashSet<string> { "captureId", "textSha256" }, [],
                RequiresSnapshot: false, RequiresGrant: false),
            // Übernahme vorbereiten: Änderungen der Sandbox ins Staging des Brokers holen (nur lesend).
            ["Workspace.Diff"] = new("Workspace.Diff", "1", 0, 0, false, new HashSet<string> { "grantId", "baseSnapshotId" },
                [FolderRight.Read]),
            // Übernahme in den Projektordner: Diff je Datei, abwählbar; Rechte je Änderungsart.
            ["Workspace.ExportPatch"] = new("Workspace.ExportPatch", "1", 1, options.MaxTargetsPerProposal, true,
                new HashSet<string> { "grantId", "stagingId", "patch" }, [FolderRight.Read], RequiresSnapshot: false),
        };
        // M9: Start = eigene Freigabe (Sitzung mit Grenzen); Befehle/Stopp laufen im Rahmen der freigegebenen Sitzung.
        _tools[OsDebugTools.Start] = new(OsDebugTools.Start, "1", 0, 0, false, OsDebugTools.StartArguments,
            [FolderRight.Read, FolderRight.Execute]);
        _tools[OsDebugTools.Command] = new(OsDebugTools.Command, "1", 0, 0, false, OsDebugTools.CommandArguments, [],
            RequiresSnapshot: false, RequiresGrant: false);
        _tools[OsDebugTools.Stop] = new(OsDebugTools.Stop, "1", 0, 0, false, new HashSet<string> { "sessionId" }, [],
            RequiresSnapshot: false, RequiresGrant: false);
        foreach (var name in ToolchainTools.Actions.Keys)
        {
            // Ausführende Tools bleiben Einzelaufrufe ohne Ziele (Spezifikation §5 Sammelfreigabe).
            _tools[name] = new(name, "1", 0, 0, false, new HashSet<string> { "grantId", "toolchain", "parameters" },
                [FolderRight.Read, FolderRight.Execute]);
        }
    }

    public IReadOnlyList<FolderGrant> Grants => _context.Grants;

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
        var grant = tool.RequiresGrant ? ValidateGrant(request, tool) : null;

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
            var path = PathRules.NormalizeRelative(target.Path, tool.AllowRootTarget);
            if (grant is { Recursive: false } && path.Contains('/'))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden,
                    $"Grant {grant.GrantId} ist nicht rekursiv – Unterordner sind nicht freigegeben: {path}");
            }
            normalized.Add(target with { Path = path });
        }
        if (tool.Name == "Patch.Apply")
        {
            ValidatePatch(request.Arguments, grant!, normalized);
        }
        else if (tool.Name is "Web.Fetch" or "Web.Content")
        {
            ValidateWeb(tool.Name, request.Arguments);
        }
        else if (tool.Name == "Workspace.Diff")
        {
            var baseId = StringArgument(request.Arguments, "baseSnapshotId");
            if (baseId is null || !_context.HasSnapshot(baseId, grant!.GrantId))
            {
                throw new BrokerException(BrokerErrorCodes.Conflict, "baseSnapshotId ist für diesen Grant unbekannt");
            }
            if (baseId == request.SnapshotId)
            {
                throw new BrokerException(BrokerErrorCodes.Conflict, "Keine Änderungen: Snapshot entspricht dem Projektstand");
            }
            if (!_context.IsDerivedFrom(request.SnapshotId, baseId, grant.GrantId))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden, "Snapshot ist nicht aus dem angegebenen Projektstand abgeleitet");
            }
        }
        else if (tool.Name == "Workspace.ExportPatch")
        {
            ValidateExport(request.Arguments, grant!, normalized);
        }
        else if (ToolchainTools.Actions.ContainsKey(tool.Name))
        {
            ToolchainTools.Validate(tool.Name, request.Arguments, _context.Toolchains);
        }
        else if (tool.Name == OsDebugTools.Start)
        {
            OsDebugTools.ValidateStart(request.Arguments, _context.OsDebug);
        }
        else if (OsDebugTools.IsSessionTool(tool.Name))
        {
            if (_context.OsDebug is null)
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden, "Betriebssystem-Debugging ist für dieses Projekt nicht eingerichtet");
            }
            OsDebugTools.ValidateCommand(tool.Name, request.Arguments);
        }
        return normalized;
    }

    private static string? StringArgument(JsonElement arguments, string name) =>
        arguments.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private void ValidateWeb(string tool, JsonElement arguments)
    {
        var captures = _context.WebCaptures
                       ?? throw new BrokerException(BrokerErrorCodes.Forbidden, "Webabrufe sind ausgeschaltet (config/localai.json → web.enabled)");
        if (tool == "Web.Fetch")
        {
            var url = StringArgument(arguments, "url");
            var (normalized, host) = UrlPolicy.Validate(url);
            if (normalized != url)
            {
                throw Invalid("url ohne Fragment (#…) angeben");
            }
            if (!UrlPolicy.HostAllowed(host, captures.AllowedHosts))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden,
                    $"Host {host} steht nicht in web.allowedHosts ({string.Join(", ", captures.AllowedHosts)})");
            }
            return;
        }
        var captureId = StringArgument(arguments, "captureId");
        var sha = StringArgument(arguments, "textSha256");
        var capture = (captureId is null ? null : captures.Find(captureId))
                      ?? throw new BrokerException(BrokerErrorCodes.Conflict, "Webinhalt unbekannt oder abgelaufen – erneut abrufen");
        if (sha is null || !ProposalHashing.IsSha256Hex(sha) || sha != capture.TextSha256)
        {
            throw new BrokerException(BrokerErrorCodes.Conflict, "textSha256 passt nicht zum bereinigten Text dieses Abrufs (SEC-10)");
        }
    }

    /// <summary>Übernahme: Ziele = Dateien des Stagings, Diff = vom Broker berechneter Diff, Rechte je Änderungsart.</summary>
    private void ValidateExport(JsonElement arguments, FolderGrant grant, IReadOnlyList<ToolTarget> targets)
    {
        var exports = _context.Exports
                      ?? throw new BrokerException(BrokerErrorCodes.Forbidden, "Übernahme ist in dieser Betriebsart nicht verfügbar");
        var stagingId = StringArgument(arguments, "stagingId");
        var staging = (stagingId is null ? null : exports.Find(stagingId))
                      ?? throw new BrokerException(BrokerErrorCodes.Conflict, "Übernahme unbekannt oder abgelaufen – Workspace.Diff erneut ausführen");
        if (staging.GrantId != grant.GrantId)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, "Übernahme gehört zu einem anderen Ordner");
        }
        if (StringArgument(arguments, "patch") != staging.Patch)
        {
            throw Invalid("patch muss genau der vom Broker berechnete Diff der Übernahme sein");
        }
        var files = staging.Files.ToDictionary(f => f.Path, StringComparer.Ordinal);
        var paths = targets.Select(t => t.Path).ToHashSet(StringComparer.Ordinal);
        if (!paths.SetEquals(files.Keys) || paths.Count != targets.Count)
        {
            throw Invalid("Ziele müssen genau die Dateien der Übernahme sein: " + string.Join(", ", files.Keys.Order(StringComparer.Ordinal)));
        }
        foreach (var file in staging.Files)
        {
            var right = file.Kind switch
            {
                ExportFileKind.Create => FolderRight.Create,
                ExportFileKind.Delete => FolderRight.Delete,
                _ => FolderRight.Modify,
            };
            if (!grant.Rights.Contains(right))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden,
                    $"Grant {grant.GrantId} hat nicht das Recht {right} für {file.Path} – in der Projektdatei freigeben");
            }
        }
    }

    /// <summary>Ziele = genau die Dateien des Patches; Rechte je Änderungsart (SEC-05).</summary>
    private static void ValidatePatch(JsonElement arguments, FolderGrant grant, IReadOnlyList<ToolTarget> targets)
    {
        var patch = arguments.TryGetProperty("patch", out var p) && p.ValueKind == JsonValueKind.String ? p.GetString()! : null;
        if (string.IsNullOrWhiteSpace(patch))
        {
            throw Invalid("Patch.Apply benötigt das Argument patch (Unified Diff)");
        }
        var files = PatchFiles.Parse(patch);
        var paths = targets.Select(t => t.Path).ToHashSet(StringComparer.Ordinal);
        var inPatch = files.Select(f => f.Path).ToHashSet(StringComparer.Ordinal);
        if (!paths.SetEquals(inPatch) || paths.Count != targets.Count)
        {
            throw Invalid("Ziele müssen genau die Dateien des Patches sein (je Datei ein Ziel): "
                          + string.Join(", ", inPatch.Order(StringComparer.Ordinal)));
        }
        foreach (var file in files)
        {
            var right = file.Kind switch
            {
                PatchFileKind.Create => FolderRight.Create,
                PatchFileKind.Delete => FolderRight.Delete,
                _ => FolderRight.Modify,
            };
            if (!grant.Rights.Contains(right))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden,
                    $"Grant {grant.GrantId} hat nicht das Recht {right} für {file.Path} – in der Projektdatei freigeben");
            }
            if (!grant.Recursive && file.Path.Contains('/'))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden, $"Grant {grant.GrantId} ist nicht rekursiv: {file.Path}");
            }
        }
    }

    private FolderGrant ValidateGrant(ProposalRequest request, ToolDefinition tool)
    {
        var grantId = request.Arguments.TryGetProperty("grantId", out var g) && g.ValueKind == JsonValueKind.String
            ? g.GetString()!
            : throw Invalid($"{tool.Name} benötigt das Argument grantId");
        var grant = _context.FindGrant(grantId)
                    ?? throw new BrokerException(BrokerErrorCodes.Forbidden, $"Kein freigegebener Ordner mit grantId '{grantId}'");
        var missing = tool.RequiredRights.Where(r => !grant.Rights.Contains(r)).ToList();
        if (missing.Count > 0)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden,
                $"Grant {grantId} hat nicht die Rechte {string.Join(", ", missing)} für {tool.Name}");
        }
        if (tool.Name == "Workspace.Snapshot")
        {
            var root = request.Arguments.TryGetProperty("root", out var r) && r.ValueKind == JsonValueKind.String ? r.GetString() : null;
            if (!string.Equals(root, grant.Root, StringComparison.Ordinal))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden,
                    $"root muss exakt dem freigegebenen Ordner entsprechen ({grant.Root})");
            }
        }
        if (tool.RequiresSnapshot && !_context.HasSnapshot(request.SnapshotId, grantId))
        {
            throw new BrokerException(BrokerErrorCodes.Conflict,
                $"Snapshot '{request.SnapshotId}' ist für Grant {grantId} unbekannt – zuerst Workspace.Snapshot ausführen");
        }
        return grant;
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
        RequireIntRange(arguments, "depth", 0, 10);
        RequireIntRange(arguments, "maxResults", 1, 2000);
        RequireIntRange(arguments, "maxChars", 1000, 60_000);
        if (arguments.TryGetProperty("query", out var query)
            && (query.ValueKind != JsonValueKind.String || query.GetString() is not { Length: >= 1 and <= 200 } q || q.Any(char.IsControl)))
        {
            throw Invalid("query muss 1 bis 200 Zeichen ohne Steuerzeichen haben");
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

    private static void RequireIntRange(JsonElement arguments, string name, long low, long high)
    {
        if (arguments.TryGetProperty(name, out var value)
            && (value.ValueKind != JsonValueKind.Number || !value.TryGetInt64(out var v) || v < low || v > high))
        {
            throw Invalid($"{name} muss zwischen {low} und {high} liegen");
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
/// Formale Pfadregeln für relative Pfade innerhalb eines Ordnergrants (identisch zu localai/pathrules.py).
/// Ein String-Präfixvergleich genügt nicht – die endgültige Prüfung erfolgt in der Sandbox gegen das Snapshot-Manifest.
/// </summary>
public static class PathRules
{
    public const string Root = ".";

    private static readonly HashSet<string> ReservedNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    };

    private static readonly char[] ForbiddenChars = [':', '*', '?', '"', '<', '>', '|'];

    /// <summary>Gibt den normalisierten relativen Pfad mit '/' zurück oder wirft 403.</summary>
    public static string NormalizeRelative(string? path, bool allowRoot = false)
    {
        if (allowRoot && path == Root)
        {
            return Root;
        }
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
