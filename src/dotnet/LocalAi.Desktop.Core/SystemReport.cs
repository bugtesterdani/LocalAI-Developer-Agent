using System.Text;
using System.Text.Json;

namespace LocalAi.Desktop.Core;

/// <summary>Lesbare Darstellung von Versionsmanifest und Audit-Prüfung (Reiter SYSTEM).</summary>
public static class SystemReport
{
    private static string Str(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v)
            ? v.ValueKind switch
            {
                JsonValueKind.String => v.GetString() ?? "",
                JsonValueKind.Null or JsonValueKind.Undefined => "",
                _ => v.ToString(),
            }
            : "";

    public static string PathOf(JsonElement result) => Str(result, "path");

    /// <summary>(Text, Probleme): Probleme = fehlende Pflicht-Voraussetzungen, je Zeile.</summary>
    public static (string Text, string Problems) FormatManifest(JsonElement m)
    {
        if (m.ValueKind != JsonValueKind.Object)
        {
            return (UiText.Get("SystemNotLoaded"), "");
        }
        var sb = new StringBuilder();
        sb.AppendLine($"{Str(m, "product")} {Str(m, "version")} (Commit {Str(m, "commit")})");
        sb.AppendLine($"Betriebsart: {Str(m, "mode")} · .NET {Str(m, "dotnet")} · {Str(m, "os")} ({Str(m, "architecture")})");
        if (m.TryGetProperty("python", out var py) && py.ValueKind == JsonValueKind.Object)
        {
            var packages = py.TryGetProperty("packages", out var pk) && pk.ValueKind == JsonValueKind.Object
                ? string.Join(", ", pk.EnumerateObject().Where(p => p.Name is "pydantic" or "pydantic_core" or "localai").Select(p => $"{p.Name} {p.Value}"))
                : "";
            sb.AppendLine($"Python: {Str(py, "version")}{(packages.Length > 0 ? " · " + packages : "")}");
        }
        if (m.TryGetProperty("prompts", out var prompts) && prompts.ValueKind == JsonValueKind.Array)
        {
            sb.AppendLine("Prompts: " + string.Join(", ", prompts.EnumerateArray().Select(p => $"{Str(p, "name")} v{Str(p, "version")}")));
        }
        sb.AppendLine($"Datenordner: {Str(m, "dataDirectory")}");
        if (m.TryGetProperty("config", out var cfg) && cfg.ValueKind == JsonValueKind.Object)
        {
            sb.AppendLine($"Konfiguration: {Str(cfg, "path")} (SHA-256 {Str(cfg, "sha256")[..Math.Min(12, Str(cfg, "sha256").Length)]})");
        }
        if (m.TryGetProperty("project", out var project) && project.ValueKind == JsonValueKind.Object)
        {
            sb.AppendLine($"Projekt: {Str(project, "path")}");
        }
        if (m.TryGetProperty("toolchains", out var tcs) && tcs.ValueKind == JsonValueKind.Array && tcs.GetArrayLength() > 0)
        {
            sb.AppendLine("Toolchains: " + string.Join(", ", tcs.EnumerateArray().Select(t => $"{Str(t, "name")} {Str(t, "version")} ({Str(t, "digest")[..Math.Min(12, Str(t, "digest").Length)]})")));
        }
        if (m.TryGetProperty("web", out var web) && web.ValueKind == JsonValueKind.Object)
        {
            sb.AppendLine("Web: " + (Str(web, "enabled") == "True" || Str(web, "enabled") == "true" ? Str(web, "route") : "ausgeschaltet"));
        }
        var problems = new StringBuilder();
        if (m.TryGetProperty("offline", out var checks) && checks.ValueKind == JsonValueKind.Array)
        {
            sb.AppendLine();
            sb.AppendLine("Offline-Voraussetzungen:");
            foreach (var c in checks.EnumerateArray())
            {
                var ok = c.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
                var required = !c.TryGetProperty("required", out var r) || r.ValueKind == JsonValueKind.True;
                var mark = ok ? "✓" : required ? "✗" : "–";
                sb.AppendLine($"  {mark} {Str(c, "item")}: {Str(c, "detail")}");
                if (!ok && required)
                {
                    problems.AppendLine($"{Str(c, "item")}: {Str(c, "detail")}");
                }
            }
        }
        return (InertText.Clean(sb.ToString().TrimEnd(), 20_000), InertText.Clean(problems.ToString().TrimEnd(), 4_000));
    }

    public static string FormatAudit(JsonElement r)
    {
        if (r.ValueKind != JsonValueKind.Object)
        {
            return "";
        }
        var ok = r.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
        var sb = new StringBuilder();
        sb.AppendLine(UiText.Get(ok ? "AuditOk" : "AuditProblems"));
        if (r.TryGetProperty("entries", out _))
        {
            sb.AppendLine(UiText.Format("AuditCounts", Str(r, "entries"), Str(r, "sessions"), Str(r, "proposals"), Str(r, "approvals"),
                Str(r, "denials"), Str(r, "invocations"), Str(r, "emergencyStops")));
        }
        if (r.TryGetProperty("problems", out var problems) && problems.ValueKind == JsonValueKind.Array)
        {
            foreach (var p in problems.EnumerateArray().Take(50))
            {
                sb.AppendLine("• " + p.GetString());
            }
        }
        return InertText.Clean(sb.ToString().TrimEnd(), 10_000);
    }
}
