using System.Diagnostics;
using System.Text.Json.Nodes;
using LocalAi.Broker;
using LocalAi.Contracts;
using ActivityKind = LocalAi.Contracts.ActivityKind;

namespace LocalAi.Broker.Host;

/// <summary>
/// Startet und überwacht den Python-Orchestrator (SEC-22): Der Orchestrator erhält nur seinen eigenen Token.
/// Stürzt er ab, werden offene Vorschläge seines Runs ungültig; meldet er sich nicht mehr, erscheint ein
/// Hinweis in der Oberfläche. Not-Aus beendet ihn sofort. Die Oberfläche bleibt in jedem Fall bedienbar,
/// weil sie nie auf den Orchestrator wartet.
/// </summary>
public sealed class OrchestratorSupervisor : IAsyncDisposable
{
    private readonly ProcessCommand? _command;
    private readonly ChildProcesses _children;
    private readonly ApprovalBroker _broker;
    private readonly ActivityHub _hub;
    private readonly OrchestratorInbox _inbox;
    private readonly IClock _clock;
    private readonly string _pipeName;
    private readonly string _token;
    private readonly string _logPath;
    private readonly string _dataDirectory;
    private readonly Lock _gate = new();
    private readonly CancellationTokenSource _shutdown = new();
    private readonly Task _watchdog;
    private Process? _process;
    private string? _runId;
    private DateTimeOffset _startedAt;
    private bool _unresponsive;
    private bool _stopRequested;

    public OrchestratorSupervisor(ProcessCommand? command, ChildProcesses children, ApprovalBroker broker, ActivityHub hub,
        OrchestratorInbox inbox, IClock clock, string pipeName, string orchestratorToken, string dataDirectory, TimeSpan heartbeatTimeout)
    {
        _command = command;
        _children = children;
        _broker = broker;
        _hub = hub;
        _inbox = inbox;
        _clock = clock;
        _pipeName = pipeName;
        _token = orchestratorToken;
        _dataDirectory = dataDirectory;
        _logPath = Path.Combine(dataDirectory, "orchestrator.log");
        HeartbeatTimeout = heartbeatTimeout;
        _watchdog = Task.Run(WatchdogLoopAsync);
    }

    public TimeSpan HeartbeatTimeout { get; }

    public string LogPath => _logPath;

    /// <summary>Kennung des aktuellen Runs (auch nach Ende des Prozesses, bis ein neuer startet).</summary>
    public string? RunId
    {
        get { lock (_gate) { return _runId; } }
    }

    public bool IsRunning
    {
        get { lock (_gate) { return _process is { HasExited: false }; } }
    }

    /// <summary>Startet den Orchestrator, falls er nicht läuft. Liefert die Run-Kennung.</summary>
    public string EnsureRunning()
    {
        lock (_gate)
        {
            if (_process is { HasExited: false } && _runId is not null)
            {
                return _runId;
            }
            var command = _command ?? throw new BrokerException(BrokerErrorCodes.Unprocessable,
                "Kein Orchestrator in config/localai.json konfiguriert (Abschnitt \"orchestrator\")");
            var runId = $"run-{Guid.NewGuid():N}"[..12];
            Process process;
            try
            {
                process = _children.Start(command, [], new Dictionary<string, string>
                {
                    ["LOCALAI_PIPE"] = _pipeName,
                    ["LOCALAI_TOKEN"] = _token,
                    ["LOCALAI_RUN_ID"] = runId,
                    // Nur Aufträge ab jetzt: die gleich folgende Chatnachricht, keine aus früheren Läufen.
                    ["LOCALAI_INBOX_AFTER"] = _inbox.CurrentSeq.ToString(System.Globalization.CultureInfo.InvariantCulture),
                    ["LOCALAI_DATA"] = _dataDirectory,
                }, _logPath);
            }
            catch (ChildProcessException ex)
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Orchestrator: {ex.Message}");
            }
            _process = process;
            _runId = runId;
            _startedAt = _clock.UtcNow;
            _unresponsive = false;
            _stopRequested = false;
            _broker.PublishInfo("orchestrator.started", $"Orchestrator gestartet (PID {process.Id}, Run {runId})");
            _ = Task.Run(() => WatchExitAsync(process, runId));
            return runId;
        }
    }

    /// <summary>Beendet den Orchestrator samt Kindprozessen (Not-Aus, Nutzer, Herunterfahren).</summary>
    public bool Stop(string reason)
    {
        Process? process;
        string? runId;
        lock (_gate)
        {
            process = _process;
            runId = _runId;
            if (process is null || process.HasExited)
            {
                return false;
            }
            _stopRequested = true;
        }
        try
        {
            process.Kill(entireProcessTree: true);
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
        {
        }
        if (runId is not null)
        {
            var cancelled = _broker.CancelProposals(runId, null, reason);
            _hub.AddBrokerNotice(runId, ActivityKind.Warning, $"Orchestrator beendet: {reason}", AgentState.Cancelled);
            _broker.PublishInfo("orchestrator.stopped", $"Orchestrator beendet: {reason} ({cancelled} Vorschläge abgebrochen)");
        }
        return true;
    }

    public JsonObject Status()
    {
        lock (_gate)
        {
            return new JsonObject
            {
                ["configured"] = _command is not null,
                ["running"] = _process is { HasExited: false },
                ["runId"] = _runId,
                ["pid"] = _process is { HasExited: false } p ? p.Id : null,
                ["unresponsive"] = _unresponsive,
                ["log"] = _logPath,
            };
        }
    }

    private async Task WatchExitAsync(Process process, string runId)
    {
        try
        {
            await process.WaitForExitAsync(_shutdown.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return;
        }
        bool requested;
        lock (_gate)
        {
            requested = _stopRequested && ReferenceEquals(process, _process);
        }
        if (requested)
        {
            return;
        }
        int? exitCode = null;
        try
        {
            exitCode = process.ExitCode;
        }
        catch (InvalidOperationException)
        {
        }
        // Kein Tool ohne Freigabe – und auch keine verwaisten Freigaben: offene Vorschläge des Runs werden ungültig.
        var cancelled = _broker.CancelProposals(runId, null, "Orchestrator beendet");
        var text = exitCode == 0
            ? "Orchestrator beendet"
            : $"Orchestrator unerwartet beendet (Exitcode {exitCode}); {cancelled} offene Vorschläge ungültig. Protokoll: {_logPath}";
        _hub.AddBrokerNotice(runId, exitCode == 0 ? ActivityKind.State : ActivityKind.Error, text, exitCode == 0 ? AgentState.Done : AgentState.Failed);
        _broker.PublishInfo("orchestrator.exited", text);
    }

    private async Task WatchdogLoopAsync()
    {
        while (!_shutdown.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(1), _shutdown.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            string? message = null;
            string type = "";
            lock (_gate)
            {
                if (_process is not { HasExited: false })
                {
                    continue;
                }
                var last = new[] { _hub.LastReport, _inbox.LastPoll, _startedAt }.Max();
                var silent = _clock.UtcNow - last;
                if (!_unresponsive && silent > HeartbeatTimeout)
                {
                    _unresponsive = true;
                    type = "orchestrator.unresponsive";
                    message = $"Orchestrator meldet sich seit {silent.TotalSeconds:0} s nicht – hängt? Beenden ist jederzeit möglich, Freigaben bleiben gesperrt.";
                }
                else if (_unresponsive && silent <= HeartbeatTimeout)
                {
                    _unresponsive = false;
                    type = "orchestrator.responsive";
                    message = "Orchestrator meldet sich wieder";
                }
            }
            if (message is not null)
            {
                try
                {
                    _broker.PublishInfo(type, message);
                }
                catch (ObjectDisposedException)
                {
                }
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        Stop("Broker wird beendet");
        await _shutdown.CancelAsync().ConfigureAwait(false);
        try
        {
            await _watchdog.ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
        }
        _shutdown.Dispose();
    }
}
