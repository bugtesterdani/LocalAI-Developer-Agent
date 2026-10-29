using System.Text.Json;

namespace LocalAi.Broker;

/// <summary>
/// Automatische Freigabe („autoApprove“ in der Projektdatei): Tools, die der Broker in einem Run selbst genehmigt.
/// <list type="bullet">
/// <item>Nur Tools, die ausschließlich in der Sandbox wirken (Snapshot lesen, Arbeitskopie ändern, Offline-VM ausführen).
/// Alles, was den Projektordner auf dem Rechner liest oder schreibt, ins Netz geht oder eine Debug-Sitzung öffnet, ist fest
/// ausgeschlossen – auch wenn es in der Projektdatei steht (dann startet der Broker nicht).</item>
/// <item>Wirksam erst, nachdem der Nutzer die Liste zu Beginn jedes Runs ausdrücklich bestätigt hat
/// (eigener Vorschlag <see cref="Tool"/>); abgelehnt oder abgelaufen = jede Aktion einzeln freigeben.</item>
/// <item>Die Bestätigung gilt nur für diesen Run und diese Broker-Sitzung; Not-Aus, Abbruch, Neustart und eine neue
/// Bestätigungsanfrage beenden sie.</item>
/// <item>Jeder automatisch genehmigte Vorschlag wird wie jeder andere geprüft (Ziele, Rechte, Snapshot), protokolliert und
/// in der Aktivität angezeigt.</item>
/// </list>
/// </summary>
public static class AutoApprove
{
    /// <summary>Bestätigung der automatischen Freigabe für einen Run (nie selbst automatisch).</summary>
    public const string Tool = "Run.AutoApprove";

    /// <summary>Tools, die automatisch genehmigt werden dürfen, mit Kurzbeschreibung (für Fehlermeldungen und Anzeige).</summary>
    public static readonly IReadOnlyDictionary<string, string> Eligible = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["File.List"] = "Dateien im Snapshot auflisten (Sandbox)",
        ["File.Read"] = "eine Datei im Snapshot lesen (Sandbox)",
        ["File.ReadMany"] = "mehrere Dateien im Snapshot lesen (Sandbox)",
        ["File.Search"] = "im Snapshot suchen (Sandbox)",
        ["Repo.Index"] = "Symbolindex des Snapshots erstellen (Sandbox)",
        ["Patch.Apply"] = "Änderungen auf die Arbeitskopie in der Sandbox anwenden (nicht auf den Projektordner)",
        ["Toolchain.Restore"] = "Pakete wiederherstellen in der Offline-VM",
        ["Toolchain.Build"] = "bauen in der Offline-VM",
        ["Toolchain.Test"] = "Tests ausführen in der Offline-VM",
        ["Toolchain.Run"] = "Programm ausführen in der Offline-VM",
        ["Toolchain.Lint"] = "Lint/Format in der Offline-VM",
    };

    /// <summary>Fest ausgeschlossen, mit Begründung.</summary>
    public static readonly IReadOnlyDictionary<string, string> Never = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["Workspace.Snapshot"] = "liest den Projektordner auf dem Rechner",
        ["Workspace.Diff"] = "liest den Projektordner auf dem Rechner",
        ["Workspace.ExportPatch"] = "schreibt in den Projektordner auf dem Rechner",
        ["Web.Fetch"] = "geht ins Internet",
        ["Web.Content"] = "gibt Webinhalte an einen Agenten",
        [OsDebugTools.Start] = "öffnet eine Debug-Sitzung mit eigenen Grenzen",
        [OsDebugTools.Command] = "läuft nur im Rahmen einer freigegebenen Debug-Sitzung",
        [OsDebugTools.Stop] = "läuft nur im Rahmen einer freigegebenen Debug-Sitzung",
        [Tool] = "ist die Bestätigung selbst",
    };

    /// <summary>
    /// Liest „autoApprove“ der Projektdatei: liefert die eingeschalteten Tools (sortiert). Unbekannte und fest
    /// ausgeschlossene Tools mit <c>true</c> sind ein Fehler – nichts wird stillschweigend eingeschaltet oder übergangen.
    /// </summary>
    public static IReadOnlyList<string> Parse(IReadOnlyDictionary<string, bool>? config)
    {
        var enabled = new List<string>();
        foreach (var (tool, on) in config ?? new Dictionary<string, bool>())
        {
            if (Never.TryGetValue(tool, out var why))
            {
                if (on)
                {
                    throw new InvalidDataException($"autoApprove: {tool} kann nie automatisch genehmigt werden ({why})");
                }
                continue;
            }
            if (!Eligible.ContainsKey(tool))
            {
                throw new InvalidDataException(
                    $"autoApprove: unbekanntes Tool '{tool}' – möglich sind {string.Join(", ", Eligible.Keys)}");
            }
            if (on)
            {
                enabled.Add(tool);
            }
        }
        enabled.Sort(StringComparer.Ordinal);
        return enabled;
    }

    /// <summary>Die Tools eines Vorschlags Run.AutoApprove (Argument „tools“) – genau die eingeschalteten, sortiert.</summary>
    public static IReadOnlyList<string> RequireTools(JsonElement arguments, IReadOnlyList<string> configured)
    {
        if (configured.Count == 0)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, "autoApprove ist in der Projektdatei nicht eingeschaltet");
        }
        if (!arguments.TryGetProperty("tools", out var tools) || tools.ValueKind != JsonValueKind.Array)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, "Run.AutoApprove: tools fehlt");
        }
        var list = new List<string>();
        foreach (var item in tools.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String)
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, "Run.AutoApprove: tools enthält keinen Text");
            }
            list.Add(item.GetString()!);
        }
        if (!list.SequenceEqual(configured, StringComparer.Ordinal))
        {
            // Nie mehr oder weniger als in der Projektdatei: der Nutzer bestätigt genau diese Liste.
            throw new BrokerException(BrokerErrorCodes.Forbidden,
                $"Run.AutoApprove: tools muss genau der Liste der Projektdatei entsprechen ({string.Join(", ", configured)})");
        }
        return list;
    }
}
