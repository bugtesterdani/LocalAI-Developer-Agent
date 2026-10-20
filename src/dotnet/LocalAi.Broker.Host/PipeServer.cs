using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;
using System.Text.Json.Nodes;
using LocalAi.Broker;
using LocalAi.Contracts;
using M = LocalAi.Contracts.PipeProtocol.Methods;

namespace LocalAi.Broker.Host;

/// <summary>
/// Named-Pipe-Server des Brokers. Es gibt keine execute(command)-Methode: Ausführung erfolgt nur
/// brokerintern nach einer Entscheidung über den UI-Token.
/// </summary>
public sealed class PipeServer(string pipeName, RoleTokens tokens, ApprovalBroker broker, CountingTestAdapter? testAdapter, ChildProcesses children, HostOptions options)
{
    private static readonly IReadOnlyDictionary<string, CallerRole[]> Permissions = new Dictionary<string, CallerRole[]>
    {
        [M.Ping] = [CallerRole.Ui, CallerRole.Orchestrator],
        [M.Status] = [CallerRole.Ui],
        [M.Submit] = [CallerRole.Orchestrator],
        [M.Outcome] = [CallerRole.Ui, CallerRole.Orchestrator],
        [M.Pending] = [CallerRole.Ui],
        [M.Get] = [CallerRole.Ui],
        [M.Decide] = [CallerRole.Ui],
        [M.Narrow] = [CallerRole.Ui],
        [M.WaitEvents] = [CallerRole.Ui, CallerRole.Orchestrator],
        [M.EmergencyStop] = [CallerRole.Ui],
        [M.Release] = [CallerRole.Ui],
        [M.StartFakeAgent] = [CallerRole.Ui],
        [M.ProjectGrants] = [CallerRole.Ui, CallerRole.Orchestrator],
        [M.ToolchainsScan] = [CallerRole.Ui],
        [M.ToolchainsRegister] = [CallerRole.Ui],
        [M.ToolchainsUnregister] = [CallerRole.Ui],
        [M.ToolchainsAvailable] = [CallerRole.Ui, CallerRole.Orchestrator],
        [M.ChatSend] = [CallerRole.Ui],
        [M.AgentCancel] = [CallerRole.Ui],
        [M.RunCancel] = [CallerRole.Ui],
        [M.ActivityWait] = [CallerRole.Ui],
        [M.ActivityReport] = [CallerRole.Orchestrator],
        [M.OrchestratorWait] = [CallerRole.Orchestrator],
        [M.OrchestratorStatus] = [CallerRole.Ui],
        [M.OrchestratorStop] = [CallerRole.Ui],
        [M.SandboxRebuild] = [CallerRole.Ui],
        [M.SandboxContinue] = [CallerRole.Ui],
        // Nur die Oberfläche sieht den Text eines Webabrufs vor der Inhaltsfreigabe (SEC-09).
        [M.WebCapture] = [CallerRole.Ui],
        [M.SystemManifest] = [CallerRole.Ui],
        [M.AuditVerify] = [CallerRole.Ui],
        [M.DiagnosticsExport] = [CallerRole.Ui],
        [M.BackupCreate] = [CallerRole.Ui],
    };

    public const int MaxChatChars = 16_000;

    /// <summary>Aktueller Zustand der Offline-VM (nur Betriebsart hyperv).</summary>
    public Func<string?> SandboxStatus { get; init; } = () => null;

    /// <summary>„VM neu aufbauen“ (nur Betriebsart hyperv).</summary>
    public Func<Task>? RebuildSandbox { get; init; }

    /// <summary>„VM weiter verwenden“ nach ausgebliebener Antwort (nur hyperv).</summary>
    public Func<Task<string>>? ContinueSandbox { get; init; }

    /// <summary>Registrierte Toolchains (administrative Aktionen nur über den UI-Token).</summary>
    public LocalAi.Sandbox.ToolchainRegistry? Toolchains { get; init; }

    /// <summary>Webabrufe (Inhaltsvorschau für die Inhaltsfreigabe); null = ausgeschaltet.</summary>
    public IWebCaptures? WebCaptures { get; init; }

    /// <summary>Not-Aus: laufende Webabrufe und Ausführung beenden; liefert eine kurze Beschreibung.</summary>
    public Func<Task<string>>? StopExecution { get; init; }

    /// <summary>Nach „Not-Aus aufheben“ (z. B. Entwicklungs-Worker neu starten).</summary>
    public Action? AfterRelease { get; init; }

    /// <summary>Versions- und Offline-Manifest (AC01) – bei Aufruf neu erstellt.</summary>
    public Func<JsonObject>? BuildManifest { get; init; }

    /// <summary>In der Sandbox ausführbare Toolchains (nur hyperv); sonst null.</summary>
    public IToolchainCatalog? ToolchainCatalog { get; init; }

    /// <summary>Live-Aktivität (Anzeige) und Aufträge an den Orchestrator.</summary>
    public required ActivityHub Activity { get; init; }

    public required OrchestratorInbox Inbox { get; init; }

    public required OrchestratorSupervisor Orchestrator { get; init; }

    /// <summary>Mehrere wartende Instanzen, damit gleichzeitige Verbindungen nicht als "Pipe beschäftigt" scheitern.</summary>
    private const int ListeningInstances = 4;

    public Task RunAsync(CancellationToken cancellationToken) =>
        Task.WhenAll(Enumerable.Range(0, ListeningInstances).Select(_ => AcceptLoopAsync(cancellationToken)));

    private async Task AcceptLoopAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            var server = new NamedPipeServerStream(
                pipeName,
                PipeDirection.InOut,
                NamedPipeServerStream.MaxAllowedServerInstances,
                PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
            try
            {
                await server.WaitForConnectionAsync(cancellationToken).ConfigureAwait(false);
            }
            catch
            {
                await server.DisposeAsync().ConfigureAwait(false);
                throw;
            }
            _ = Task.Run(() => HandleConnectionAsync(server, cancellationToken), cancellationToken);
        }
    }

    private async Task HandleConnectionAsync(NamedPipeServerStream stream, CancellationToken cancellationToken)
    {
        await using (stream.ConfigureAwait(false))
        {
            try
            {
                while (!cancellationToken.IsCancellationRequested)
                {
                    var frame = await PipeProtocol.ReadFrameAsync(stream, cancellationToken).ConfigureAwait(false);
                    if (frame is null)
                    {
                        return;
                    }
                    var response = await HandleFrameAsync(frame, cancellationToken).ConfigureAwait(false);
                    await PipeProtocol.WriteFrameAsync(stream, JsonSerializer.SerializeToUtf8Bytes(response), cancellationToken).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException)
            {
            }
            catch (IOException)
            {
                // Client hat die Verbindung getrennt.
            }
            catch (InvalidDataException)
            {
                // Protokollverletzung: Verbindung schließen.
            }
        }
    }

    private async Task<JsonObject> HandleFrameAsync(byte[] frame, CancellationToken cancellationToken)
    {
        JsonNode? id = null;
        try
        {
            JsonObject request;
            try
            {
                request = JsonNode.Parse(frame)?.AsObject() ?? throw new JsonException();
            }
            catch (Exception ex) when (ex is JsonException or InvalidOperationException)
            {
                throw new BrokerException(BrokerErrorCodes.BadRequest, "Anfrage ist kein gültiges JSON-Objekt");
            }
            id = request["id"]?.DeepClone();
            var method = request["method"]?.GetValue<string>() ?? throw new BrokerException(BrokerErrorCodes.BadRequest, "method fehlt");
            var role = tokens.Resolve(request["token"]?.GetValue<string>())
                       ?? throw new BrokerException(BrokerErrorCodes.Forbidden, "Nicht authentifiziert");
            if (!Permissions.TryGetValue(method, out var allowed))
            {
                throw new BrokerException(BrokerErrorCodes.BadRequest, $"Unbekannte Methode: {method}");
            }
            if (!allowed.Contains(role))
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden, $"Rolle {role} darf {method} nicht aufrufen");
            }
            var parameters = request["params"] is JsonObject p ? JsonSerializer.SerializeToElement(p) : JsonSerializer.SerializeToElement(new JsonObject());
            var result = await DispatchAsync(method, role, parameters, cancellationToken).ConfigureAwait(false);
            return new JsonObject { ["id"] = id, ["ok"] = true, ["result"] = result };
        }
        catch (BrokerException ex)
        {
            return Error(id, ex.Code, ex.Message);
        }
        catch (JsonException ex)
        {
            return Error(id, BrokerErrorCodes.Unprocessable, $"Ungültige Parameter: {ex.Message}");
        }
        catch (CanonicalJsonException ex)
        {
            return Error(id, BrokerErrorCodes.Unprocessable, ex.Message);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Interner Fehler: {ex}");
            return Error(id, BrokerErrorCodes.Internal, "Interner Fehler im Broker");
        }
    }

    private async Task<JsonNode?> DispatchAsync(string method, CallerRole role, JsonElement p, CancellationToken cancellationToken)
    {
        switch (method)
        {
            case M.Ping:
                return new JsonObject
                {
                    ["role"] = role.ToString(),
                    ["sessionId"] = broker.SessionId,
                    ["stopped"] = broker.IsStopped,
                    ["mode"] = HostOptions.ModeName(options.Mode),
                };
            case M.Status:
                return new JsonObject
                {
                    ["sessionId"] = broker.SessionId,
                    ["stopped"] = broker.IsStopped,
                    ["pending"] = broker.Pending().Count,
                    ["mode"] = HostOptions.ModeName(options.Mode),
                    ["sandbox"] = SandboxStatus(),
                    ["testAdapterInvocations"] = testAdapter?.Invocations ?? 0,
                    ["web"] = options.Web is null
                        ? new JsonObject { ["enabled"] = false }
                        : new JsonObject
                        {
                            ["enabled"] = true,
                            ["route"] = options.Web.RouteText,
                            ["proxy"] = options.Web.Proxy?.Url,
                            ["allowedHosts"] = new JsonArray(options.Web.AllowedHosts.Select(h => (JsonNode?)h).ToArray()),
                        },
                };
            case M.Submit:
                return Node(broker.Submit(ContractJson.Deserialize<ProposalRequest>(p)));
            case M.Outcome:
                return Node(broker.Outcome(RequireString(p, "proposalId")));
            case M.Pending:
                return Node(broker.Pending());
            case M.Get:
                return Node(broker.Get(RequireString(p, "proposalId")));
            case M.Decide:
                return Node(broker.Decide(ContractJson.Deserialize<UserDecision>(p)));
            case M.Narrow:
                return Node(broker.Narrow(ContractJson.Deserialize<NarrowRequest>(p)));
            case M.WaitEvents:
            {
                var after = p.TryGetProperty("afterSeq", out var a) ? a.GetInt64() : 0;
                var timeoutMs = p.TryGetProperty("timeoutMs", out var t) ? Math.Clamp(t.GetInt32(), 0, 30_000) : 10_000;
                return Node(await broker.WaitEventsAsync(after, TimeSpan.FromMilliseconds(timeoutMs), cancellationToken).ConfigureAwait(false));
            }
            case M.EmergencyStop:
            {
                broker.EmergencyStop();
                // Not-Aus wirkt auch, wenn der Orchestrator hängt: Prozess wird beendet (SEC-22).
                Orchestrator.Stop("Not-Aus");
                // SEC-15: laufende Webabrufe und Ausführung (VM bzw. Entwicklungs-Worker) beenden.
                var detail = StopExecution is null ? "" : await StopExecution().ConfigureAwait(false);
                if (detail.Length > 0)
                {
                    broker.PublishInfo("run.emergencyStop", "Not-Aus: " + detail);
                }
                return new JsonObject { ["stopped"] = true, ["detail"] = detail };
            }
            case M.Release:
                broker.ReleaseEmergencyStop();
                AfterRelease?.Invoke();
                return new JsonObject { ["stopped"] = false };
            case M.SystemManifest:
            {
                var manifest = (BuildManifest ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "Kein Manifest verfügbar"))();
                VersionManifest.Write(manifest, options.DataDirectory);
                return manifest;
            }
            case M.AuditVerify:
            {
                var path = Path.Combine(options.DataDirectory, "audit.db");
                try
                {
                    return AuditCheck.Verify(path).ToJson();
                }
                catch (AuditTamperException ex)
                {
                    return new JsonObject { ["ok"] = false, ["problems"] = new JsonArray(ex.Message) };
                }
            }
            case M.DiagnosticsExport:
            {
                var includeLogs = p.TryGetProperty("includeLogs", out var il) && il.ValueKind == JsonValueKind.True;
                AuditReport? audit = null;
                string? auditError = null;
                try
                {
                    audit = AuditCheck.Verify(Path.Combine(options.DataDirectory, "audit.db"));
                }
                catch (Exception ex) when (ex is AuditTamperException or IOException or Microsoft.Data.Sqlite.SqliteException)
                {
                    auditError = ex.Message;
                }
                var manifest = BuildManifest?.Invoke() ?? new JsonObject();
                var events = await broker.WaitEventsAsync(0, TimeSpan.Zero, cancellationToken).ConfigureAwait(false);
                var file = DiagnosticsExport.Create(options.DataDirectory, manifest, audit, auditError, broker.StateCounts(), events, includeLogs,
                    DateTimeOffset.Now);
                broker.PublishInfo("run.diagnosticsExported", $"Diagnosepaket erstellt: {file}" + (includeLogs ? " (mit Protokollen)" : ""));
                return new JsonObject { ["path"] = file, ["includeLogs"] = includeLogs };
            }
            case M.BackupCreate:
            {
                var file = BackupArchive.Create(options.DataDirectory, options.ConfigPath, options.ProjectPath, options.RepoRoot, DateTimeOffset.Now);
                broker.PublishInfo("run.backupCreated", $"Sicherung erstellt: {file}");
                return new JsonObject { ["path"] = file };
            }
            case M.WebCapture:
            {
                var store = WebCaptures ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "Webabrufe sind ausgeschaltet");
                var id = p.TryGetProperty("captureId", out var c) && c.ValueKind == JsonValueKind.String ? c.GetString()! : "";
                return Node(store.Find(id) ?? throw new BrokerException(BrokerErrorCodes.NotFound, "Webinhalt nicht gefunden oder abgelaufen"));
            }
            case M.StartFakeAgent:
                return StartFakeAgent();
            case M.ProjectGrants:
                return Node(broker.Grants);
            case M.ToolchainsScan:
                return Node(await Task.Run(() => RequireToolchains().Scan(cancellationToken), cancellationToken).ConfigureAwait(false));
            case M.ToolchainsRegister:
            {
                var directory = RequireString(p, "directory");
                var digest = RequireString(p, "digest");
                try
                {
                    var registration = await Task.Run(() => RequireToolchains().Register(directory, digest, cancellationToken), cancellationToken).ConfigureAwait(false);
                    broker.PublishInfo("toolchain.registered",
                        $"{registration.Name} {registration.Version} registriert ({registration.Files.Count} Dateien, Prüfsumme {registration.Digest[..16]}…)");
                    return Node(registration);
                }
                catch (LocalAi.Sandbox.ToolchainException ex)
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, ex.Message);
                }
            }
            case M.ToolchainsUnregister:
            {
                var name = RequireString(p, "name");
                var removed = RequireToolchains().Unregister(name);
                if (removed)
                {
                    broker.PublishInfo("toolchain.unregistered", $"{name} entfernt");
                }
                return new JsonObject { ["removed"] = removed };
            }
            case M.ToolchainsAvailable:
            {
                // Nur Aktionen und Parameter – keine Pfade des Hosts.
                var list = new JsonArray();
                foreach (var manifest in ToolchainCatalog?.Available ?? [])
                {
                    var actions = new JsonObject();
                    foreach (var (name, action) in manifest.Actions)
                    {
                        var parameters = new JsonObject();
                        foreach (var (pname, spec) in action.Parameters ?? new Dictionary<string, ToolchainParameter>())
                        {
                            parameters[pname] = JsonSerializer.SerializeToNode(spec, ContractJson.Options);
                        }
                        actions[name] = new JsonObject { ["parameters"] = parameters, ["timeoutSeconds"] = action.TimeoutSeconds ?? 120 };
                    }
                    list.Add(new JsonObject
                    {
                        ["name"] = manifest.Name,
                        ["displayName"] = manifest.DisplayName,
                        ["version"] = manifest.Version,
                        ["guest"] = (manifest.Guest ?? GuestOs.Windows).ToString(),
                        ["testResultFormat"] = manifest.TestResultFormat.ToString(),
                        ["actions"] = actions,
                    });
                }
                return new JsonObject
                {
                    ["enabled"] = ToolchainCatalog is not null,
                    ["reason"] = ToolchainCatalog is null ? "Toolchain-Aktionen nur in der Betriebsart hyperv" : null,
                    ["toolchains"] = list,
                };
            }
            case M.ChatSend:
            {
                var text = RequireString(p, "text").Trim();
                if (text.Length == 0 || text.Length > MaxChatChars)
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Nachricht leer oder länger als {MaxChatChars} Zeichen");
                }
                if (broker.IsStopped)
                {
                    throw new BrokerException(BrokerErrorCodes.Conflict, "Not-Aus ist aktiv – erst aufheben");
                }
                var runId = Orchestrator.EnsureRunning();
                Activity.AddUserChat(runId, text);
                Inbox.Post(OrchestratorCommandKind.Chat, text: text);
                return new JsonObject { ["runId"] = runId };
            }
            case M.AgentCancel:
            {
                var agent = RequireString(p, "agent");
                if (!Enum.TryParse<AgentRole>(agent, ignoreCase: false, out var agentRole) || !Enum.IsDefined(agentRole))
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Unbekannter Agent: {agent}");
                }
                var cancelled = Orchestrator.RunId is { } run ? broker.CancelProposals(run, agentRole, $"Abbruch von {agentRole} durch Nutzer") : 0;
                Inbox.Post(OrchestratorCommandKind.CancelAgent, agent: agentRole.ToString());
                broker.PublishInfo("run.agentCancelled", $"{agentRole} abgebrochen ({cancelled} Vorschläge)");
                return new JsonObject { ["cancelledProposals"] = cancelled };
            }
            case M.RunCancel:
            {
                var cancelled = Orchestrator.RunId is { } run ? broker.CancelProposals(run, null, "Run durch Nutzer abgebrochen") : 0;
                Inbox.Post(OrchestratorCommandKind.CancelRun);
                broker.PublishInfo("run.cancelled", $"Run abgebrochen ({cancelled} Vorschläge)");
                return new JsonObject { ["cancelledProposals"] = cancelled };
            }
            case M.ActivityWait:
            {
                var after = p.TryGetProperty("afterSeq", out var a) ? a.GetInt64() : 0;
                var timeoutMs = p.TryGetProperty("timeoutMs", out var t) ? Math.Clamp(t.GetInt32(), 0, 30_000) : 10_000;
                return Node(await Activity.WaitAsync(after, TimeSpan.FromMilliseconds(timeoutMs), cancellationToken).ConfigureAwait(false));
            }
            case M.ActivityReport:
            {
                var report = ContractJson.Deserialize<ActivityReport>(p);
                // Nur der aktuell vom Broker gestartete Orchestrator darf melden (kein Einschleusen fremder Runs).
                if (Orchestrator.RunId is not { } current || report.RunId != current)
                {
                    throw new BrokerException(BrokerErrorCodes.Conflict, "Kein laufender Run mit dieser Kennung");
                }
                return new JsonObject { ["accepted"] = Activity.Report(report) };
            }
            case M.OrchestratorWait:
            {
                var after = p.TryGetProperty("afterSeq", out var a) ? a.GetInt64() : 0;
                var timeoutMs = p.TryGetProperty("timeoutMs", out var t) ? Math.Clamp(t.GetInt32(), 0, 30_000) : 10_000;
                return Node(await Inbox.WaitAsync(after, TimeSpan.FromMilliseconds(timeoutMs), cancellationToken).ConfigureAwait(false));
            }
            case M.SandboxRebuild:
            {
                var rebuild = RebuildSandbox ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "Neuaufbau gibt es nur in der Betriebsart hyperv");
                broker.PublishInfo("sandbox.status", "VM wird neu aufgebaut (auf Wunsch)");
                await rebuild().ConfigureAwait(false);
                return new JsonObject { ["rebuilding"] = true };
            }
            case M.SandboxContinue:
            {
                var resume = ContinueSandbox ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "Gibt es nur in der Betriebsart hyperv");
                var text = await resume().ConfigureAwait(false);
                broker.PublishInfo("sandbox.status", text);
                return new JsonObject { ["sandbox"] = text };
            }
            case M.OrchestratorStatus:
                return Orchestrator.Status();
            case M.OrchestratorStop:
                return new JsonObject { ["stopped"] = Orchestrator.Stop("durch Nutzer beendet") };
            default:
                throw new BrokerException(BrokerErrorCodes.BadRequest, $"Unbekannte Methode: {method}");
        }
    }

    private JsonObject StartFakeAgent()
    {
        var command = options.FakeAgent
                      ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "Kein Fake-Agent in config/localai.json konfiguriert (Datei nicht gefunden?)");
        var logPath = Path.Combine(options.DataDirectory, "fake-agent.log");
        try
        {
            // Der Orchestrator erhält nur seinen eigenen Token, nie den UI-Token.
            var process = children.Start(command, [], new Dictionary<string, string>
            {
                ["LOCALAI_PIPE"] = pipeName,
                ["LOCALAI_TOKEN"] = tokens.Orchestrator,
            }, logPath);
            return new JsonObject { ["pid"] = process.Id, ["log"] = logPath };
        }
        catch (ChildProcessException ex)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Fake-Agent: {ex.Message}");
        }
    }

    private LocalAi.Sandbox.ToolchainRegistry RequireToolchains() =>
        Toolchains ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "Toolchains sind in dieser Betriebsart nicht verfügbar");

    private static string RequireString(JsonElement p, string name) =>
        p.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()!
            : throw new BrokerException(BrokerErrorCodes.Unprocessable, $"{name} fehlt");

    private static JsonNode? Node<T>(T value) => JsonSerializer.SerializeToNode(value, ContractJson.Options);

    private static JsonObject Error(JsonNode? id, int code, string message) => new()
    {
        ["id"] = id,
        ["ok"] = false,
        ["error"] = new JsonObject { ["code"] = code, ["message"] = message },
    };
}
