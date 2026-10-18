using System.Text.Json;
using System.Text.RegularExpressions;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>Patch.Apply im Broker: Kopfzeilen lesen, Pfade nach den Pfadregeln normalisieren.</summary>
public static class PatchFiles
{
    public static IReadOnlyList<PatchFile> Parse(string patch)
    {
        IReadOnlyList<PatchFile> files;
        try
        {
            files = PatchHeaders.Parse(patch);
        }
        catch (PatchFormatException ex)
        {
            throw new BrokerException(ex.TooLarge ? BrokerErrorCodes.TooMany : BrokerErrorCodes.Unprocessable, ex.Message);
        }
        var normalized = files.Select(f => f with { Path = PathRules.NormalizeRelative(f.Path) }).ToList();
        var duplicate = normalized.GroupBy(f => f.Path, StringComparer.Ordinal).FirstOrDefault(g => g.Count() > 1);
        if (duplicate is not null)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Datei kommt mehrfach im Patch vor: {duplicate.Key}");
        }
        return normalized;
    }

    public static string Section(string patch, string path) => PatchHeaders.Section(patch, path);
}

/// <summary>Toolchains, die in der aktuellen Sandbox installiert sind (nur Betriebsart hyperv).</summary>
public interface IToolchainCatalog
{
    /// <summary>Geprüftes Manifest oder null, wenn die Toolchain für das Projekt nicht verfügbar ist.</summary>
    ToolchainManifest? Find(string name);

    IReadOnlyList<ToolchainManifest> Available { get; }
}

/// <summary>Feste Zuordnung Tool → Manifestaktion; keine anderen Aktionen sind erreichbar (SEC-20).</summary>
public static class ToolchainTools
{
    public static readonly IReadOnlyDictionary<string, string[]> Actions = new Dictionary<string, string[]>(StringComparer.Ordinal)
    {
        ["Toolchain.Restore"] = ["restore"],
        ["Toolchain.Build"] = ["build"],
        ["Toolchain.Test"] = ["test"],
        ["Toolchain.Run"] = ["run"],
        ["Toolchain.Lint"] = ["lint", "format"],
    };

    private static readonly Regex Name = new("^[a-z][a-z0-9-]{0,31}$");
    private static readonly Regex SafeText = new(@"^[A-Za-z0-9 ._:=~,/\\+#@()!&|*-]{1,200}$");

    public static (string ActionName, ToolchainAction Action) Resolve(ToolchainManifest manifest, string tool)
    {
        foreach (var name in Actions.TryGetValue(tool, out var names) ? names : [])
        {
            if (manifest.Actions.TryGetValue(name, out var action))
            {
                return (name, action);
            }
        }
        throw new BrokerException(BrokerErrorCodes.Forbidden,
            $"Toolchain '{manifest.Name}' hat keine Aktion für {tool} (vorhanden: {string.Join(", ", manifest.Actions.Keys.Order(StringComparer.Ordinal))})");
    }

    /// <summary>Prüft Toolchain, Aktion und typisierte Parameter. Wirft BrokerException (403/422).</summary>
    public static (ToolchainManifest Manifest, string ActionName, ToolchainAction Action) Validate(
        string tool, JsonElement arguments, IToolchainCatalog? catalog)
    {
        if (catalog is null)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden,
                "Toolchain-Aktionen gibt es nur in der Betriebsart hyperv (Offline-VM) – nie auf dem Host");
        }
        var name = arguments.TryGetProperty("toolchain", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString()! : null;
        if (name is null || !Name.IsMatch(name))
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, "toolchain fehlt oder ist ungültig");
        }
        var manifest = catalog.Find(name) ?? throw new BrokerException(BrokerErrorCodes.Forbidden,
            $"Toolchain '{name}' ist für dieses Projekt nicht registriert bzw. nicht in der Sandbox installiert");
        var (actionName, action) = Resolve(manifest, tool);
        var declared = action.Parameters ?? new Dictionary<string, ToolchainParameter>();
        var values = arguments.TryGetProperty("parameters", out var p) ? p : default;
        if (values.ValueKind is not (JsonValueKind.Object or JsonValueKind.Undefined))
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, "parameters muss ein Objekt sein");
        }
        var given = values.ValueKind == JsonValueKind.Object
            ? values.EnumerateObject().ToDictionary(x => x.Name, x => x.Value, StringComparer.Ordinal)
            : new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        var unknown = given.Keys.Where(k => !declared.ContainsKey(k)).Order(StringComparer.Ordinal).ToList();
        if (unknown.Count > 0)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, $"Nicht deklarierte Parameter: {string.Join(", ", unknown)}");
        }
        var missing = declared.Keys.Where(k => !given.ContainsKey(k)).Order(StringComparer.Ordinal).ToList();
        if (missing.Count > 0)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Parameter fehlen: {string.Join(", ", missing)}");
        }
        foreach (var (pname, spec) in declared)
        {
            var value = given[pname];
            string text;
            switch (spec.Type)
            {
                case ToolchainParameterType.Integer:
                    if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var number))
                    {
                        throw Bad(pname, "ganze Zahl erwartet");
                    }
                    text = number.ToString(System.Globalization.CultureInfo.InvariantCulture);
                    break;
                case ToolchainParameterType.Choice:
                    text = value.ValueKind == JsonValueKind.String ? value.GetString()! : throw Bad(pname, "Text erwartet");
                    if (!(spec.Values ?? []).Contains(text, StringComparer.Ordinal))
                    {
                        throw Bad(pname, $"erlaubt sind {string.Join(", ", spec.Values ?? [])}");
                    }
                    break;
                case ToolchainParameterType.Path:
                    text = value.ValueKind == JsonValueKind.String ? value.GetString()! : throw Bad(pname, "Pfad erwartet");
                    text = PathRules.NormalizeRelative(text.Replace('\\', '/'), allowRoot: true);
                    if (text.StartsWith('-'))
                    {
                        throw Bad(pname, "Pfad darf nicht mit '-' beginnen");
                    }
                    break;
                default:
                    text = value.ValueKind == JsonValueKind.String ? value.GetString()! : throw Bad(pname, "Text erwartet");
                    if (spec.Pattern is null && !SafeText.IsMatch(text))
                    {
                        throw Bad(pname, "unzulässige Zeichen");
                    }
                    if (text.StartsWith('-'))
                    {
                        throw Bad(pname, "Wert darf nicht mit '-' beginnen (keine zusätzlichen Optionen)");
                    }
                    break;
            }
            if (spec.Pattern is not null && !Regex.IsMatch(text, $"^(?:{spec.Pattern})$", RegexOptions.None, TimeSpan.FromMilliseconds(200)))
            {
                throw Bad(pname, $"entspricht nicht dem Muster {spec.Pattern}");
            }
        }
        return (manifest, actionName, action);
    }

    private static BrokerException Bad(string name, string message) => new(BrokerErrorCodes.Forbidden, $"Parameter {name}: {message}");
}
