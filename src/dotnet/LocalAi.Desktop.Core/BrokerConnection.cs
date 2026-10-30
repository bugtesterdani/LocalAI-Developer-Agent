using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;
using System.Text.Json.Nodes;
using LocalAi.Contracts;
using ActivityEvent = LocalAi.Contracts.ActivityEvent;
using M = LocalAi.Contracts.PipeProtocol.Methods;

namespace LocalAi.Desktop.Core;

public sealed class BrokerCallException(int code, string message) : Exception(message)
{
    public int Code { get; } = code;
}

/// <summary>Handshake-Zeile, die der Broker beim Start auf stdout ausgibt.</summary>
public sealed record BrokerHandshake(string Pipe, string SessionId, string UiToken, string DataDirectory, string Mode);

/// <summary>Startet LocalAi.Broker.Host als Kindprozess; schließt ihn über stdin-EOF.</summary>
public sealed class BrokerProcess : IAsyncDisposable
{
    private readonly Process _process;

    private BrokerProcess(Process process, BrokerHandshake handshake)
    {
        _process = process;
        Handshake = handshake;
    }

    public BrokerHandshake Handshake { get; }

    public static async Task<BrokerProcess> StartAsync(CancellationToken cancellationToken, IEnumerable<string>? extraArguments = null)
    {
        var baseDir = AppContext.BaseDirectory;
        var exe = Path.Combine(baseDir, OperatingSystem.IsWindows() ? "LocalAi.Broker.Host.exe" : "LocalAi.Broker.Host");
        var dll = Path.Combine(baseDir, "LocalAi.Broker.Host.dll");
        ProcessStartInfo start;
        if (File.Exists(exe))
        {
            start = new ProcessStartInfo(exe);
        }
        else if (File.Exists(dll))
        {
            start = new ProcessStartInfo("dotnet");
            start.ArgumentList.Add(dll);
        }
        else
        {
            throw new FileNotFoundException("LocalAi.Broker.Host wurde nicht neben der Anwendung gefunden", exe);
        }
        foreach (var argument in extraArguments ?? [])
        {
            start.ArgumentList.Add(argument);
        }
        start.UseShellExecute = false;
        start.RedirectStandardInput = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;
        start.StandardOutputEncoding = new System.Text.UTF8Encoding(false);
        start.StandardErrorEncoding = new System.Text.UTF8Encoding(false);
        start.CreateNoWindow = true;

        var process = Process.Start(start) ?? throw new InvalidOperationException("Broker konnte nicht gestartet werden");
        var line = await process.StandardOutput.ReadLineAsync(cancellationToken).ConfigureAwait(false);
        if (string.IsNullOrEmpty(line))
        {
            var error = await process.StandardError.ReadToEndAsync(cancellationToken).ConfigureAwait(false);
            throw new InvalidOperationException($"Broker lieferte keinen Handshake: {error}");
        }
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        var handshake = new BrokerHandshake(
            root.GetProperty("pipe").GetString()!,
            root.GetProperty("sessionId").GetString()!,
            root.GetProperty("uiToken").GetString()!,
            root.GetProperty("dataDirectory").GetString()!,
            root.TryGetProperty("mode", out var mode) ? mode.GetString()! : "none");
        // stderr weiter abholen, damit der Puffer nicht vollläuft.
        _ = Task.Run(async () =>
        {
            try
            {
                while (await process.StandardError.ReadLineAsync().ConfigureAwait(false) is not null)
                {
                }
            }
            catch (IOException)
            {
            }
        });
        return new BrokerProcess(process, handshake);
    }

    public async ValueTask DisposeAsync()
    {
        try
        {
            _process.StandardInput.Close();
            // Mit VM braucht das Herunterfahren länger: der Broker löscht die Sandbox-VM.
            using var timeout = new CancellationTokenSource(Handshake.Mode is "hyperv" or "qemu" ? TimeSpan.FromMinutes(3) : TimeSpan.FromSeconds(5));
            await _process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is OperationCanceledException or InvalidOperationException or IOException)
        {
            try
            {
                _process.Kill(entireProcessTree: true);
            }
            catch (InvalidOperationException)
            {
            }
        }
        _process.Dispose();
    }
}

/// <summary>Eine Pipe-Verbindung zum Broker mit dem UI-Token.</summary>
public sealed class BrokerConnection : IBrokerUiApi, IAsyncDisposable
{
    private readonly NamedPipeClientStream _stream;
    private readonly string _token;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private long _nextId;

    private BrokerConnection(NamedPipeClientStream stream, string token)
    {
        _stream = stream;
        _token = token;
    }

    public static async Task<BrokerConnection> ConnectAsync(string pipe, string token, CancellationToken cancellationToken)
    {
        var stream = new NamedPipeClientStream(".", pipe, PipeDirection.InOut, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        await stream.ConnectAsync(10_000, cancellationToken).ConfigureAwait(false);
        return new BrokerConnection(stream, token);
    }

    public async Task<JsonElement> CallAsync(string method, object? parameters, CancellationToken cancellationToken)
    {
        var id = Interlocked.Increment(ref _nextId);
        var request = new JsonObject
        {
            ["id"] = id,
            ["method"] = method,
            ["token"] = _token,
            ["params"] = parameters is null ? new JsonObject() : JsonSerializer.SerializeToNode(parameters, ContractJson.Options),
        };
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await PipeProtocol.WriteFrameAsync(_stream, JsonSerializer.SerializeToUtf8Bytes(request), cancellationToken).ConfigureAwait(false);
            var frame = await PipeProtocol.ReadFrameAsync(_stream, cancellationToken).ConfigureAwait(false)
                        ?? throw new IOException("Verbindung zum Broker getrennt");
            using var doc = JsonDocument.Parse(frame);
            var root = doc.RootElement;
            if (root.GetProperty("id").GetInt64() != id)
            {
                throw new IOException("Antwort passt nicht zur Anfrage");
            }
            if (!root.GetProperty("ok").GetBoolean())
            {
                var error = root.GetProperty("error");
                throw new BrokerCallException(error.GetProperty("code").GetInt32(), error.GetProperty("message").GetString() ?? "");
            }
            return root.GetProperty("result").Clone();
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<IReadOnlyList<ProposalView>> PendingAsync(CancellationToken ct) =>
        ContractJson.Deserialize<List<ProposalView>>(await CallAsync(M.Pending, null, ct).ConfigureAwait(false));

    public async Task<ProposalView> DecideAsync(UserDecision decision, CancellationToken ct) =>
        ContractJson.Deserialize<ProposalView>(await CallAsync(M.Decide, decision, ct).ConfigureAwait(false));

    public async Task<ProposalView> NarrowAsync(NarrowRequest request, CancellationToken ct) =>
        ContractJson.Deserialize<ProposalView>(await CallAsync(M.Narrow, request, ct).ConfigureAwait(false));

    public async Task<IReadOnlyList<BrokerEvent>> WaitEventsAsync(long afterSeq, int timeoutMs, CancellationToken ct) =>
        ContractJson.Deserialize<List<BrokerEvent>>(await CallAsync(M.WaitEvents,
            new Dictionary<string, long> { ["afterSeq"] = afterSeq, ["timeoutMs"] = timeoutMs }, ct).ConfigureAwait(false));

    public async Task<IReadOnlyList<LimitQuestion>> LimitPendingAsync(CancellationToken ct) =>
        ContractJson.Deserialize<List<LimitQuestion>>(await CallAsync(M.LimitPending, null, ct).ConfigureAwait(false));

    public Task LimitAnswerAsync(LimitAnswer answer, CancellationToken ct) => CallAsync(M.LimitAnswer, answer, ct);

    public Task EmergencyStopAsync(CancellationToken ct) => CallAsync(M.EmergencyStop, null, ct);

    public Task ReleaseAsync(CancellationToken ct) => CallAsync(M.Release, null, ct);

    public Task<JsonElement> StartFakeAgentAsync(CancellationToken ct) => CallAsync(M.StartFakeAgent, null, ct);

    public Task<JsonElement> StatusAsync(CancellationToken ct) => CallAsync(M.Status, null, ct);

    public async Task<IReadOnlyList<ToolchainInfo>> ScanToolchainsAsync(CancellationToken ct) =>
        ContractJson.Deserialize<List<ToolchainInfo>>(await CallAsync(M.ToolchainsScan, null, ct).ConfigureAwait(false));

    public Task RegisterToolchainAsync(string directory, string digest, CancellationToken ct) =>
        CallAsync(M.ToolchainsRegister, new { directory, digest }, ct);

    public Task UnregisterToolchainAsync(string name, CancellationToken ct) =>
        CallAsync(M.ToolchainsUnregister, new { name }, ct);

    public async Task<IReadOnlyList<FolderGrant>> GrantsAsync(CancellationToken ct) =>
        ContractJson.Deserialize<List<FolderGrant>>(await CallAsync(M.ProjectGrants, null, ct).ConfigureAwait(false));

    public async Task<string> ChatSendAsync(string text, CancellationToken ct) =>
        (await CallAsync(M.ChatSend, new { text }, ct).ConfigureAwait(false)).GetProperty("runId").GetString() ?? "";

    public Task AgentCancelAsync(string agent, CancellationToken ct) => CallAsync(M.AgentCancel, new { agent }, ct);

    public Task RunCancelAsync(CancellationToken ct) => CallAsync(M.RunCancel, null, ct);

    public async Task<IReadOnlyList<ActivityEvent>> WaitActivityAsync(long afterSeq, int timeoutMs, CancellationToken ct) =>
        ContractJson.Deserialize<List<ActivityEvent>>(await CallAsync(M.ActivityWait,
            new Dictionary<string, long> { ["afterSeq"] = afterSeq, ["timeoutMs"] = timeoutMs }, ct).ConfigureAwait(false));

    public Task OrchestratorStopAsync(CancellationToken ct) => CallAsync(M.OrchestratorStop, null, ct);

    public Task RebuildSandboxAsync(CancellationToken ct) => CallAsync(M.SandboxRebuild, null, ct);

    public Task ContinueSandboxAsync(CancellationToken ct) => CallAsync(M.SandboxContinue, null, ct);

    public async Task<WebCapture?> WebCaptureAsync(string captureId, CancellationToken ct) =>
        ContractJson.Deserialize<WebCapture>(await CallAsync(M.WebCapture, new { captureId }, ct).ConfigureAwait(false));

    public Task<JsonElement> ManifestAsync(CancellationToken ct) => CallAsync(M.SystemManifest, null, ct);

    public Task<JsonElement> AuditVerifyAsync(CancellationToken ct) => CallAsync(M.AuditVerify, null, ct);

    public Task<JsonElement> DiagnosticsExportAsync(bool includeLogs, CancellationToken ct) =>
        CallAsync(M.DiagnosticsExport, new { includeLogs }, ct);

    public Task<JsonElement> BackupCreateAsync(CancellationToken ct) => CallAsync(M.BackupCreate, null, ct);

    public async ValueTask DisposeAsync()
    {
        await _stream.DisposeAsync().ConfigureAwait(false);
        _gate.Dispose();
    }
}
