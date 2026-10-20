using System.Text.Json.Nodes;

namespace LocalAi.Broker;

/// <summary>Ergebnis der Audit-Prüfung (AC03): Hashkette plus Freigabeinvarianten über das ganze Journal.</summary>
public sealed record AuditReport(
    int Entries,
    int Sessions,
    int Proposals,
    int Approvals,
    int Denials,
    int Invocations,
    int EmergencyStops,
    IReadOnlyList<string> Problems,
    string LastHash)
{
    public bool Ok => Problems.Count == 0;

    public JsonObject ToJson() => new()
    {
        ["ok"] = Ok,
        ["entries"] = Entries,
        ["sessions"] = Sessions,
        ["proposals"] = Proposals,
        ["approvals"] = Approvals,
        ["denials"] = Denials,
        ["invocations"] = Invocations,
        ["emergencyStops"] = EmergencyStops,
        ["lastHash"] = LastHash,
        ["problems"] = new JsonArray(Problems.Select(p => (JsonNode?)p).ToArray()),
    };
}

/// <summary>
/// Prüft das Audit-Journal über die Hashkette hinaus (AC03): Jede Ausführung (<c>invocation.started</c>) hat genau
/// eine vorherige ausdrückliche Erlaubnis mit identischem Vorschlags-Hash, in derselben Broker-Sitzung, ohne vorherige
/// Ablehnung; jeder Vorschlag wird höchstens einmal ausgeführt (SEC-03); nach einem Neustart wird kein Vorschlag einer
/// früheren Sitzung ausgeführt (SEC-12).
/// </summary>
public static class AuditCheck
{
    private sealed class Proposal
    {
        public required string Sha { get; init; }
        public required int Session { get; init; }
        public string Tool { get; init; } = "";
        public int Approvals;
        public int Denials;
        public string? ApprovedSha;
        public int Invocations;
    }

    /// <summary>Prüft Hashkette (wirft <see cref="AuditTamperException"/>) und Freigabeinvarianten.</summary>
    public static AuditReport Verify(string journalPath) => Check(AuditJournal.ReadVerified(journalPath));

    private sealed class Grant
    {
        public required int Session { get; init; }
        public required int Max { get; init; }
        public int Used;
        public bool Closed;
    }

    public static AuditReport Check(IReadOnlyList<JournalEntry> entries)
    {
        var problems = new List<string>();
        var grants = new Dictionary<string, Grant>(StringComparer.Ordinal);
        var proposals = new Dictionary<string, Proposal>(StringComparer.Ordinal);
        int session = 0, approvals = 0, denials = 0, invocations = 0, stops = 0;
        foreach (var e in entries)
        {
            string? Str(string name) => e.Data[name]?.GetValueKind() == System.Text.Json.JsonValueKind.String ? e.Data[name]!.GetValue<string>() : null;
            var id = Str("proposalId");
            switch (e.Type)
            {
                case "broker.started":
                    session++;
                    break;
                case "emergency.stop":
                    stops++;
                    break;
                case "proposal.created":
                {
                    var pid = e.Data["proposal"]?["proposalId"]?.GetValue<string>();
                    var sha = Str("sha256");
                    if (pid is null || sha is null)
                    {
                        problems.Add($"#{e.Seq}: Vorschlag ohne Kennung oder Hash");
                        break;
                    }
                    if (!proposals.TryAdd(pid, new Proposal { Sha = sha, Session = session, Tool = e.Data["proposal"]?["tool"]?.GetValue<string>() ?? "" }))
                    {
                        problems.Add($"#{e.Seq}: Vorschlag {pid} doppelt angelegt");
                    }
                    break;
                }
                case "decision":
                {
                    if (id is null || !proposals.TryGetValue(id, out var p))
                    {
                        problems.Add($"#{e.Seq}: Entscheidung für unbekannten Vorschlag {id}");
                        break;
                    }
                    var kind = Str("decision");
                    var sha = Str("sha256");
                    if (sha != p.Sha)
                    {
                        problems.Add($"#{e.Seq}: Entscheidung für {id} mit abweichendem Hash");
                    }
                    if (kind == "Approve")
                    {
                        approvals++;
                        p.Approvals++;
                        p.ApprovedSha = sha;
                    }
                    else if (kind == "SessionGrant")
                    {
                        // M9: nur Befehle einer freigegebenen, offenen Debug-Sitzung derselben Broker-Sitzung, im Budget.
                        var grantId = Str("grant");
                        if (p.Tool is not ("OsDebug.Command" or "OsDebug.Stop"))
                        {
                            problems.Add($"#{e.Seq}: Sitzungsfreigabe für {p.Tool} ({id}) nicht erlaubt");
                        }
                        else if (grantId is null || !grants.TryGetValue(grantId, out var g) || g.Closed || g.Session != session)
                        {
                            problems.Add($"#{e.Seq}: Sitzungsfreigabe für {id} ohne offene, freigegebene Debug-Sitzung");
                        }
                        else if (p.Tool == "OsDebug.Command" && ++g.Used > g.Max)
                        {
                            problems.Add($"#{e.Seq}: Debug-Sitzung {grantId} über ihr Befehlsbudget ({g.Max}) hinaus genutzt");
                        }
                        approvals++;
                        p.Approvals++;
                        p.ApprovedSha = sha;
                    }
                    else
                    {
                        denials++;
                        p.Denials++;
                    }
                    if (p.Approvals + p.Denials > 1)
                    {
                        problems.Add($"#{e.Seq}: mehr als eine Entscheidung für {id}");
                    }
                    break;
                }
                case "osdebug.granted":
                {
                    // Nur nach genau einer ausdrücklich freigegebenen und ausgeführten OsDebug.Start.
                    if (id is null || !proposals.TryGetValue(id, out var start) || start.Tool != "OsDebug.Start" || start.Invocations != 1
                        || start.Approvals != 1 || start.ApprovedSha != start.Sha || start.Session != session)
                    {
                        problems.Add($"#{e.Seq}: Debug-Sitzung {id} ohne freigegebenen, ausgeführten Start");
                        break;
                    }
                    var max = e.Data["maxCommands"]?.GetValueKind() == System.Text.Json.JsonValueKind.Number ? e.Data["maxCommands"]!.GetValue<int>() : 0;
                    grants[id] = new Grant { Session = session, Max = max };
                    break;
                }
                case "osdebug.closed":
                    if (id is not null && grants.TryGetValue(id, out var closed))
                    {
                        closed.Closed = true;
                    }
                    break;
                case "invocation.started":
                {
                    invocations++;
                    if (id is null || !proposals.TryGetValue(id, out var p))
                    {
                        problems.Add($"#{e.Seq}: Ausführung ohne bekannten Vorschlag ({id})");
                        break;
                    }
                    p.Invocations++;
                    if (p.Approvals != 1 || p.Denials != 0 || p.ApprovedSha != p.Sha)
                    {
                        problems.Add($"#{e.Seq}: Ausführung von {id} ohne genau eine passende Erlaubnis");
                    }
                    if (p.Invocations > 1)
                    {
                        problems.Add($"#{e.Seq}: {id} mehr als einmal ausgeführt");
                    }
                    if (p.Session != session)
                    {
                        problems.Add($"#{e.Seq}: {id} aus einer früheren Broker-Sitzung ausgeführt");
                    }
                    break;
                }
            }
        }
        return new AuditReport(entries.Count, session, proposals.Count, approvals, denials, invocations, stops, problems,
            entries.Count == 0 ? AuditJournal.GenesisHash : entries[^1].Hash);
    }
}
