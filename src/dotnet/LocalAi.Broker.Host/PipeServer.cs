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
public sealed class PipeServer(string pipeName, RoleTokens tokens, ApprovalBroker broker, CountingTestAdapter adapter, HostOptions options)
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
    };

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
                return new JsonObject { ["role"] = role.ToString(), ["sessionId"] = broker.SessionId, ["stopped"] = broker.IsStopped };
            case M.Status:
                return new JsonObject
                {
                    ["sessionId"] = broker.SessionId,
                    ["stopped"] = broker.IsStopped,
                    ["pending"] = broker.Pending().Count,
                    ["testAdapterInvocations"] = adapter.Invocations,
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
                broker.EmergencyStop();
                return new JsonObject { ["stopped"] = true };
            case M.Release:
                broker.ReleaseEmergencyStop();
                return new JsonObject { ["stopped"] = false };
            case M.StartFakeAgent:
                return StartFakeAgent();
            default:
                throw new BrokerException(BrokerErrorCodes.BadRequest, $"Unbekannte Methode: {method}");
        }
    }

    private JsonObject StartFakeAgent()
    {
        var command = options.FakeAgent
                      ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "Kein Fake-Agent in config/localai.json konfiguriert");
        var start = new ProcessStartInfo(command.Command)
        {
            WorkingDirectory = command.WorkingDirectory,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new System.Text.UTF8Encoding(false),
            StandardErrorEncoding = new System.Text.UTF8Encoding(false),
            CreateNoWindow = true,
        };
        foreach (var argument in command.Arguments)
        {
            start.ArgumentList.Add(argument);
        }
        // Der Orchestrator erhält nur seinen eigenen Token, nie den UI-Token.
        start.Environment["LOCALAI_PIPE"] = pipeName;
        start.Environment["LOCALAI_TOKEN"] = tokens.Orchestrator;
        start.Environment.Remove("LOCALAI_UI_TOKEN");
        start.Environment["PYTHONUTF8"] = "1";
        start.Environment["PYTHONIOENCODING"] = "utf-8";

        var logPath = Path.Combine(options.DataDirectory, "fake-agent.log");
        var process = Process.Start(start) ?? throw new BrokerException(BrokerErrorCodes.Internal, "Fake-Agent konnte nicht gestartet werden");
        _ = PumpAsync(process.StandardOutput, logPath);
        _ = PumpAsync(process.StandardError, logPath);
        return new JsonObject { ["pid"] = process.Id, ["log"] = logPath };
    }

    private static readonly SemaphoreSlim LogGate = new(1, 1);

    /// <summary>
    /// Schreibt die Ausgabe des Fake-Agenten ins Protokoll. Die Datei wird mit FileShare.ReadWrite geöffnet,
    /// damit sie unter Windows während des Laufs gelesen werden kann.
    /// </summary>
    private static async Task PumpAsync(StreamReader reader, string logPath)
    {
        try
        {
            while (await reader.ReadLineAsync().ConfigureAwait(false) is { } line)
            {
                await LogGate.WaitAsync().ConfigureAwait(false);
                try
                {
                    await using var stream = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
                    await using var writer = new StreamWriter(stream, new System.Text.UTF8Encoding(false));
                    await writer.WriteLineAsync(line).ConfigureAwait(false);
                }
                finally
                {
                    LogGate.Release();
                }
            }
        }
        catch (IOException)
        {
        }
    }

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
