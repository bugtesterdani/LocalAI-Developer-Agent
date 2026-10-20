using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>
/// Approval Broker: einzige Stelle, die Vorschläge freigibt und ausführt (Spezifikation §6).
/// Invarianten:
/// <list type="bullet">
/// <item>Ohne explizite Nutzerentscheidung mit identischem Hash wird nichts ausgeführt.</item>
/// <item>Jede Freigabe erzeugt genau eine einmalige Capability, die beim Dispatch atomar konsumiert wird.</item>
/// <item>Keine automatischen Wiederholungen; nach Neustart sind alle alten Freigaben ungültig.</item>
/// <item>Sammelfreigaben lassen sich nur verkleinern (Abwahl), nie erweitern.</item>
/// <item>Ausnahme mit eigener, ausdrücklicher Freigabe (M9): Eine freigegebene Debug-Sitzung (<c>OsDebug.Start</c>) erlaubt
/// Befehle derselben Sitzung (<c>OsDebug.Command/Stop</c>) desselben Runs und Agenten – nur bis zur freigegebenen Zahl und
/// Dauer, Eingaben nur mit allowInput; jeder Befehl wird als eigener Vorschlag mit Bezug auf die Sitzung protokolliert.
/// Not-Aus und Abbruch beenden die Sitzung.</item>
/// </list>
/// </summary>
public sealed class ApprovalBroker : IAsyncDisposable
{
    private const int MaxEventsKept = 10_000;

    private sealed class Capability
    {
        public required string Id { get; init; }
        public required string ProposalSha256 { get; init; }
        public required string SessionId { get; init; }
        public required long PolicyRevision { get; init; }
        public bool Consumed { get; set; }
    }

    private sealed class Entry
    {
        public required ToolProposal Proposal { get; init; }
        public required string Sha256 { get; init; }
        public ProposalState State { get; set; } = ProposalState.AwaitingApproval;
        public string? StateReason { get; set; }
        public Capability? Capability { get; set; }
        public string? SupersededBy { get; set; }
        public ToolResult? Result { get; set; }
        public CancellationTokenSource? RunCts { get; set; }
    }

    private readonly BrokerOptions _options;
    private readonly ProposalPolicy _policy;
    private readonly IToolAdapter _adapter;
    private readonly AuditJournal _journal;
    private readonly IClock _clock;
    private sealed class DebugGrant
    {
        public required string StartId { get; init; }
        public required string RunId { get; init; }
        public required AgentRole Agent { get; init; }
        public required int MaxCommands { get; init; }
        public required DateTimeOffset Deadline { get; init; }
        public required bool AllowInput { get; init; }
        public int Used { get; set; }
        public bool Closed { get; set; }
    }

    private readonly Lock _gate = new();
    private readonly Dictionary<string, DebugGrant> _debugGrants = new(StringComparer.Ordinal);
    private readonly Dictionary<string, Entry> _entries = new(StringComparer.Ordinal);
    private readonly Dictionary<string, (string RequestSha256, string ProposalId)> _idempotency = new(StringComparer.Ordinal);
    private readonly List<BrokerEvent> _events = [];
    private readonly Channel<string> _dispatchQueue = Channel.CreateUnbounded<string>();
    private readonly CancellationTokenSource _shutdown = new();
    private readonly SemaphoreSlim _parallelJobs = new(2, 2);
    private TaskCompletionSource _eventSignal = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private long _eventSeq;
    private bool _stopped;
    private bool _started;
    private int _disposed;
    private Task? _dispatcherLoop;
    private Task? _sweeperLoop;

    public ApprovalBroker(BrokerOptions options, IToolAdapter adapter, AuditJournal journal, IClock? clock, IPolicyContext policyContext)
    {
        _options = options;
        _policy = new ProposalPolicy(options, policyContext);
        _adapter = adapter;
        _journal = journal;
        _clock = clock ?? SystemClock.Instance;
        SessionId = Guid.NewGuid().ToString("D");
    }

    /// <summary>Kennung dieser Broker-Sitzung. Capabilities sind an sie gebunden.</summary>
    public string SessionId { get; }

    /// <summary>Freigegebene Ordner (für Agenten und Oberfläche lesbar, nie änderbar).</summary>
    public IReadOnlyList<FolderGrant> Grants => _policy.Grants;

    public bool IsStopped
    {
        get { lock (_gate) { return _stopped; } }
    }

    /// <summary>
    /// Liest das Journal, setzt alle nicht abgeschlossenen Vorgänge auf ungültig bzw. "OutcomeUnknown"
    /// und startet optional Dispatcher und Ablaufprüfung.
    /// </summary>
    public void Start(bool runBackgroundLoops = true)
    {
        lock (_gate)
        {
            if (_started)
            {
                throw new InvalidOperationException("Broker läuft bereits");
            }
            _started = true;
            Replay(_journal.OpenAndReplay());
            _journal.Append("broker.started", new JsonObject { ["sessionId"] = SessionId });
            foreach (var entry in _entries.Values)
            {
                switch (entry.State)
                {
                    case ProposalState.AwaitingApproval:
                    case ProposalState.Approved:
                        SetState(entry, ProposalState.Invalidated, "Neustart: alte Vorschläge und Freigaben sind ungültig");
                        break;
                    case ProposalState.Running:
                        SetState(entry, ProposalState.OutcomeUnknown, "Neustart während der Ausführung: Ergebnis unklar, keine Wiederholung");
                        break;
                }
            }
            AddEvent("broker.started", null, _stopped ? "Not-Aus ist weiterhin aktiv" : null);
        }

        if (runBackgroundLoops)
        {
            _dispatcherLoop = Task.Run(DispatcherLoopAsync);
            _sweeperLoop = Task.Run(SweeperLoopAsync);
        }
    }

    // ------------------------------------------------------------------ Orchestrator

    public ProposalView Submit(ProposalRequest request)
    {
        lock (_gate)
        {
            try
            {
                EnsureStarted();
                if (_stopped)
                {
                    throw new BrokerException(BrokerErrorCodes.Conflict, "Not-Aus ist aktiv – es werden keine Vorschläge angenommen");
                }

                var targets = _policy.Validate(request);
                var requestSha = CanonicalJson.Sha256Hex(request);

                if (_idempotency.TryGetValue(request.IdempotencyKey, out var known))
                {
                    if (known.RequestSha256 == requestSha)
                    {
                        return View(_entries[known.ProposalId]);
                    }
                    throw new BrokerException(BrokerErrorCodes.Conflict, "idempotencyKey wurde bereits für einen anderen Vorschlag verwendet");
                }

                var grant = OsDebugTools.IsSessionTool(request.Tool) ? RequireDebugGrant(request) : null;
                var open = _entries.Values.Count(e => e.State is ProposalState.AwaitingApproval or ProposalState.Approved);
                if (open >= _options.MaxOpenProposals)
                {
                    throw new BrokerException(BrokerErrorCodes.TooMany, "Zu viele offene Vorschläge");
                }

                var now = _clock.UtcNow;
                var proposal = new ToolProposal(
                    SchemaVersion: ContractJson.SchemaVersion,
                    ProposalId: NewId(),
                    RunId: request.RunId,
                    TaskId: request.TaskId,
                    Agent: request.Agent,
                    Tool: request.Tool,
                    ToolVersion: request.ToolVersion,
                    Purpose: request.Purpose,
                    Arguments: request.Arguments.Clone(),
                    Targets: targets,
                    SnapshotId: request.SnapshotId,
                    PolicyRevision: _options.PolicyRevision,
                    SandboxId: _options.SandboxId,
                    CreatedAt: ContractTime.ToContract(now),
                    ExpiresAt: ContractTime.ToContract(now + _options.ProposalTtl));

                var entry = CreateEntry(proposal, request.IdempotencyKey, requestSha);
                _idempotency[request.IdempotencyKey] = (requestSha, proposal.ProposalId);
                AddEvent("proposal.created", entry, null);
                if (grant is not null)
                {
                    ApproveInSession(entry, grant);
                }
                return View(entry);
            }
            catch (BrokerException ex)
            {
                if (_started)
                {
                    _journal.Append("proposal.rejected", new JsonObject
                    {
                        ["tool"] = request.Tool,
                        ["agent"] = request.Agent.ToString(),
                        ["runId"] = request.RunId,
                        ["code"] = ex.Code,
                        ["reason"] = ex.Message,
                    });
                    AddEvent("proposal.rejected", null, $"{request.Tool}: {ex.Message}", request.RunId);
                }
                throw;
            }
        }
    }

    public ProposalOutcome Outcome(string proposalId)
    {
        lock (_gate)
        {
            var entry = Find(proposalId);
            var hops = 0;
            while (entry.SupersededBy is not null && hops++ < 1000)
            {
                entry = _entries[entry.SupersededBy];
            }
            var isFinal = entry.State is not (ProposalState.AwaitingApproval or ProposalState.Approved or ProposalState.Running);
            return new ProposalOutcome(proposalId, entry.Proposal.ProposalId, entry.State, isFinal, entry.Result);
        }
    }

    // ------------------------------------------------------------------ UI

    /// <summary>Anzahl Vorschläge je Werkzeug und Zustand (Diagnose, ohne Argumente oder Inhalte).</summary>
    public IReadOnlyDictionary<string, IReadOnlyDictionary<string, int>> StateCounts()
    {
        lock (_gate)
        {
            return _entries.Values
                .GroupBy(e => e.Proposal.Tool, StringComparer.Ordinal)
                .ToDictionary(g => g.Key, g => (IReadOnlyDictionary<string, int>)g.GroupBy(e => e.State.ToString())
                    .ToDictionary(s => s.Key, s => s.Count(), StringComparer.Ordinal), StringComparer.Ordinal);
        }
    }

    public IReadOnlyList<ProposalView> Pending()
    {
        lock (_gate)
        {
            ExpireDue();
            return _entries.Values
                .Where(e => e.State == ProposalState.AwaitingApproval)
                .OrderBy(e => e.Proposal.CreatedAt, StringComparer.Ordinal)
                .Select(View)
                .ToList();
        }
    }

    public ProposalView Get(string proposalId)
    {
        lock (_gate)
        {
            return View(Find(proposalId));
        }
    }

    public ProposalView Decide(UserDecision decision)
    {
        lock (_gate)
        {
            EnsureStarted();
            var entry = RequireOpen(decision.ProposalId, decision.ProposalSha256);

            if (decision.Decision == DecisionKind.Deny)
            {
                Journal("decision", entry, new JsonObject { ["decision"] = "Deny", ["sha256"] = decision.ProposalSha256 });
                SetState(entry, ProposalState.Denied, "Vom Nutzer abgelehnt");
                return View(entry);
            }

            var capability = new Capability
            {
                Id = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(32)),
                ProposalSha256 = entry.Sha256,
                SessionId = SessionId,
                PolicyRevision = _options.PolicyRevision,
            };
            entry.Capability = capability;
            // Die Capability selbst wird nie protokolliert oder herausgegeben, nur ihr Fingerabdruck.
            Journal("decision", entry, new JsonObject
            {
                ["decision"] = "Approve",
                ["sha256"] = decision.ProposalSha256,
                ["sessionId"] = SessionId,
                ["capabilityFingerprint"] = CanonicalJson.Sha256Hex(capability.Id)[..16],
            });
            SetState(entry, ProposalState.Approved, "Vom Nutzer einmal erlaubt");
            _dispatchQueue.Writer.TryWrite(entry.Proposal.ProposalId);
            return View(entry);
        }
    }

    // ------------------------------------------------------------------ Debug-Sitzungen (M9)

    private DebugGrant RequireDebugGrant(ProposalRequest request)
    {
        var sessionId = request.Arguments.GetProperty("sessionId").GetString()!;
        if (!_debugGrants.TryGetValue(sessionId, out var grant) || grant.Closed)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, "Keine freigegebene, laufende Debug-Sitzung mit dieser Kennung – zuerst OsDebug.Start");
        }
        if (grant.RunId != request.RunId || grant.Agent != request.Agent)
        {
            throw new BrokerException(BrokerErrorCodes.Forbidden, "Die Debug-Sitzung gehört einem anderen Run bzw. Agenten");
        }
        if (_clock.UtcNow > grant.Deadline)
        {
            CloseGrant(grant, "freigegebene Dauer abgelaufen");
            throw new BrokerException(BrokerErrorCodes.Gone, "Die freigegebene Dauer der Debug-Sitzung ist abgelaufen – neue Sitzung vorschlagen");
        }
        if (request.Tool == OsDebugTools.Command)
        {
            if (grant.Used >= grant.MaxCommands)
            {
                throw new BrokerException(BrokerErrorCodes.TooMany,
                    $"Die Debug-Sitzung hat ihre {grant.MaxCommands} freigegebenen Befehle verbraucht – OsDebug.Stop und neue Sitzung vorschlagen");
            }
            var command = request.Arguments.GetProperty("command").GetString()!;
            if (OsDebugTools.InputCommands.Contains(command) && !grant.AllowInput)
            {
                throw new BrokerException(BrokerErrorCodes.Forbidden, "Tastatur und Maus sind für diese Debug-Sitzung nicht freigegeben");
            }
        }
        return grant;
    }

    private void ApproveInSession(Entry entry, DebugGrant grant)
    {
        var capability = new Capability
        {
            Id = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(32)),
            ProposalSha256 = entry.Sha256,
            SessionId = SessionId,
            PolicyRevision = _options.PolicyRevision,
        };
        entry.Capability = capability;
        if (entry.Proposal.Tool == OsDebugTools.Command)
        {
            grant.Used++;
        }
        Journal("decision", entry, new JsonObject
        {
            ["decision"] = "SessionGrant",
            ["sha256"] = entry.Sha256,
            ["sessionId"] = SessionId,
            ["grant"] = grant.StartId,
            ["used"] = grant.Used,
            ["capabilityFingerprint"] = CanonicalJson.Sha256Hex(capability.Id)[..16],
        });
        SetState(entry, ProposalState.Approved, $"Im Rahmen der freigegebenen Debug-Sitzung ({grant.Used}/{grant.MaxCommands} Befehle)");
        if (entry.Proposal.Tool == OsDebugTools.Stop)
        {
            CloseGrant(grant, "OsDebug.Stop");
        }
        _dispatchQueue.Writer.TryWrite(entry.Proposal.ProposalId);
    }

    private void CloseGrant(DebugGrant grant, string reason)
    {
        if (grant.Closed)
        {
            return;
        }
        grant.Closed = true;
        _journal.Append("osdebug.closed", new JsonObject { ["proposalId"] = grant.StartId, ["reason"] = reason, ["used"] = grant.Used });
        AddEvent("osdebug.closed", null, $"Debug-Sitzung beendet ({reason}, {grant.Used} Befehle)", grant.RunId);
    }

    private void OpenGrant(Entry start)
    {
        var args = start.Proposal.Arguments;
        var minutes = args.GetProperty("maxMinutes").GetInt32();
        var grant = new DebugGrant
        {
            StartId = start.Proposal.ProposalId,
            RunId = start.Proposal.RunId,
            Agent = start.Proposal.Agent,
            MaxCommands = args.GetProperty("maxCommands").GetInt32(),
            Deadline = _clock.UtcNow + TimeSpan.FromMinutes(minutes),
            AllowInput = args.TryGetProperty("allowInput", out var input) && input.ValueKind == JsonValueKind.True,
        };
        _debugGrants[grant.StartId] = grant;
        Journal("osdebug.granted", start, new JsonObject
        {
            ["maxCommands"] = grant.MaxCommands,
            ["deadline"] = ContractTime.ToContract(grant.Deadline),
            ["allowInput"] = grant.AllowInput,
            ["runId"] = grant.RunId,
            ["agent"] = grant.Agent.ToString(),
        });
        AddEvent("osdebug.granted", start, $"Debug-Sitzung offen: höchstens {grant.MaxCommands} Befehle, {minutes} min" +
                                           (grant.AllowInput ? ", Tastatur/Maus erlaubt" : ""));
    }

    /// <summary>Der letzte Not-Aus hat offene Debug-Sitzungen beendet – dann muss auch QEMU in der VM enden.</summary>
    public bool StopClosedDebugSessions { get; private set; }

    /// <summary>Offene Debug-Sitzungen (für Not-Aus der VM und Anzeige).</summary>
    public int OpenDebugSessions
    {
        get
        {
            lock (_gate)
            {
                return _debugGrants.Values.Count(g => !g.Closed && _clock.UtcNow <= g.Deadline);
            }
        }
    }

    /// <summary>Sammelfreigabe: behält nur die genannten Ziele und erzeugt einen neuen Vorschlag.</summary>
    public ProposalView Narrow(NarrowRequest request)
    {
        lock (_gate)
        {
            EnsureStarted();
            var entry = RequireOpen(request.ProposalId, request.ProposalSha256);
            var tool = _policy.GetTool(entry.Proposal.Tool);
            if (!tool.SupportsNarrowing)
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, $"{tool.Name} unterstützt keine Abwahl einzelner Ziele");
            }

            var keep = request.KeepTargetIds ?? throw new BrokerException(BrokerErrorCodes.Unprocessable, "keepTargetIds fehlt");
            if (keep.Count == 0)
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, "Mindestens ein Ziel muss ausgewählt bleiben – sonst bitte ablehnen");
            }
            var keepSet = new HashSet<string>(StringComparer.Ordinal);
            foreach (var id in keep)
            {
                if (!keepSet.Add(id))
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Ziel doppelt angegeben: {id}");
                }
            }
            var known = entry.Proposal.Targets.Select(t => t.TargetId).ToHashSet(StringComparer.Ordinal);
            var unknown = keepSet.FirstOrDefault(id => !known.Contains(id));
            if (unknown is not null)
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Ziel '{unknown}' gehört nicht zum Vorschlag – Ziele können nur abgewählt, nicht ergänzt werden");
            }
            if (keepSet.Count == known.Count)
            {
                throw new BrokerException(BrokerErrorCodes.Conflict, "Auswahl unverändert – kein neuer Vorschlag nötig");
            }

            var now = _clock.UtcNow;
            var narrowed = entry.Proposal with
            {
                ProposalId = NewId(),
                ParentProposalId = entry.Proposal.ProposalId,
                Targets = entry.Proposal.Targets.Where(t => keepSet.Contains(t.TargetId)).ToList(),
                PolicyRevision = _options.PolicyRevision,
                CreatedAt = ContractTime.ToContract(now),
                ExpiresAt = ContractTime.ToContract(now + _options.ProposalTtl),
            };
            var child = CreateEntry(narrowed, null, null);
            entry.SupersededBy = narrowed.ProposalId;
            SetState(entry, ProposalState.Superseded, $"Durch Abwahl ersetzt: {narrowed.ProposalId}", narrowed.ProposalId);
            AddEvent("proposal.created", child, $"Abwahl von {entry.Proposal.ProposalId}: {keepSet.Count} von {known.Count} Zielen");
            return View(child);
        }
    }

    /// <summary>Not-Aus: stoppt Dispatch, invalidiert offene Freigaben, bricht laufende Jobs ab.</summary>
    public void EmergencyStop()
    {
        lock (_gate)
        {
            EnsureStarted();
            _stopped = true;
            _journal.Append("emergency.stop", new JsonObject());
            StopClosedDebugSessions = _debugGrants.Values.Any(g => !g.Closed);
            foreach (var grant in _debugGrants.Values)
            {
                CloseGrant(grant, "Not-Aus");
            }
            foreach (var entry in _entries.Values)
            {
                if (entry.State is ProposalState.AwaitingApproval or ProposalState.Approved)
                {
                    SetState(entry, ProposalState.Invalidated, "Not-Aus");
                }
                else if (entry.State == ProposalState.Running)
                {
                    entry.RunCts?.Cancel();
                }
            }
            AddEvent("emergency.stop", null, "Not-Aus ausgelöst");
        }
    }

    public void ReleaseEmergencyStop()
    {
        lock (_gate)
        {
            EnsureStarted();
            if (!_stopped)
            {
                return;
            }
            _stopped = false;
            _journal.Append("emergency.release", new JsonObject());
            AddEvent("emergency.release", null, "Not-Aus aufgehoben");
        }
    }

    /// <summary>
    /// Infrastruktur-Ereignis (z. B. Sandbox-Status) ins Journal und an die Oberfläche. Kein Vorschlag, keine Freigabe.
    /// </summary>
    public void PublishInfo(string type, string message)
    {
        if (!InfoPrefixes.Any(prefix => type.StartsWith(prefix, StringComparison.Ordinal)))
        {
            throw new ArgumentException("Nur sandbox.*, toolchain.*, orchestrator.* und run.* Ereignisse", nameof(type));
        }
        lock (_gate)
        {
            if (Volatile.Read(ref _disposed) == 1)
            {
                return;
            }
            try
            {
                _journal.Append(type, new JsonObject { ["message"] = message });
                AddEvent(type, null, message);
            }
            catch (ObjectDisposedException)
            {
            }
        }
    }

    private static readonly string[] InfoPrefixes = ["sandbox.", "toolchain.", "orchestrator.", "run."];

    /// <summary>
    /// Bricht Vorschläge eines Runs ab (optional nur eines Agenten): offene und freigegebene werden ungültig,
    /// laufende abgebrochen. Für Abbruch pro Agent und bei abgestürztem Orchestrator (SEC-22).
    /// </summary>
    public int CancelProposals(string runId, AgentRole? agent, string reason)
    {
        lock (_gate)
        {
            EnsureStarted();
            var count = 0;
            foreach (var grant in _debugGrants.Values.Where(g => g.RunId == runId && (agent is null || g.Agent == agent)))
            {
                CloseGrant(grant, reason);
            }
            foreach (var entry in _entries.Values.Where(e => e.Proposal.RunId == runId && (agent is null || e.Proposal.Agent == agent)).ToList())
            {
                if (entry.State is ProposalState.AwaitingApproval or ProposalState.Approved)
                {
                    SetState(entry, ProposalState.Invalidated, reason);
                    count++;
                }
                else if (entry.State == ProposalState.Running)
                {
                    entry.RunCts?.Cancel();
                    count++;
                }
            }
            return count;
        }
    }

    /// <summary>Wartet auf Ereignisse nach <paramref name="afterSeq"/> (Long-Polling).</summary>
    public async Task<IReadOnlyList<BrokerEvent>> WaitEventsAsync(long afterSeq, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            Task signal;
            lock (_gate)
            {
                var found = _events.Where(e => e.Seq > afterSeq).ToList();
                if (found.Count > 0)
                {
                    return found;
                }
                signal = _eventSignal.Task;
            }
            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                return [];
            }
            await Task.WhenAny(signal, Task.Delay(remaining, cancellationToken)).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
        }
    }

    // ------------------------------------------------------------------ Dispatch

    /// <summary>
    /// Führt einen freigegebenen Vorschlag höchstens einmal aus. Prüft Capability, Hash, Sitzung,
    /// Policyrevision und Ablaufzeit unmittelbar vor dem Start erneut und konsumiert die Capability atomar.
    /// </summary>
    public async Task<bool> DispatchAsync(string proposalId)
    {
        ToolProposal proposal;
        string invocationId;
        CancellationTokenSource runCts;
        lock (_gate)
        {
            if (!_entries.TryGetValue(proposalId, out var entry) || entry.State != ProposalState.Approved)
            {
                return false;
            }
            var capability = entry.Capability;
            if (capability is null || capability.Consumed)
            {
                return false;
            }
            if (_stopped)
            {
                SetState(entry, ProposalState.Invalidated, "Not-Aus");
                return false;
            }
            if (_clock.UtcNow > ContractTime.Parse(entry.Proposal.ExpiresAt))
            {
                SetState(entry, ProposalState.Expired, "Freigabe vor dem Start abgelaufen");
                return false;
            }
            if (capability.PolicyRevision != _options.PolicyRevision || capability.SessionId != SessionId)
            {
                SetState(entry, ProposalState.Invalidated, "Policy oder Sitzung hat sich geändert");
                return false;
            }
            var recomputed = ProposalHashing.Sha256(entry.Proposal);
            if (recomputed != capability.ProposalSha256 || recomputed != entry.Sha256)
            {
                SetState(entry, ProposalState.Invalidated, "Hash des Vorschlags stimmt nicht mit der Freigabe überein");
                return false;
            }

            capability.Consumed = true;
            invocationId = NewId();
            proposal = entry.Proposal;
            runCts = CancellationTokenSource.CreateLinkedTokenSource(_shutdown.Token);
            entry.RunCts = runCts;
            Journal("invocation.started", entry, new JsonObject { ["invocationId"] = invocationId });
            SetState(entry, ProposalState.Running, null);
        }

        ToolResult result;
        try
        {
            result = await _adapter.ExecuteAsync(proposal, invocationId, runCts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            result = EmptyResult(proposal, invocationId, ResultStatus.Cancelled);
        }
        catch (Exception ex)
        {
            result = EmptyResult(proposal, invocationId, ResultStatus.Failed) with
            {
                TargetResults = [new TargetResult("*", ResultStatus.Failed, Message: ex.Message)],
            };
        }

        lock (_gate)
        {
            var entry = _entries[proposalId];
            entry.RunCts = null;
            runCts.Dispose();
            result = result with
            {
                InvocationId = invocationId,
                ProposalId = proposalId,
                Untrusted = true,
                SkippedTargetIds = SkippedTargets(entry),
            };
            entry.Result = result;
            Journal("invocation.finished", entry, new JsonObject
            {
                ["invocationId"] = invocationId,
                ["result"] = JsonSerializer.SerializeToNode(result, ContractJson.Options),
            });
            var state = result.Status switch
            {
                ResultStatus.Succeeded => ProposalState.Succeeded,
                ResultStatus.Cancelled => ProposalState.Cancelled,
                _ => ProposalState.Failed,
            };
            SetState(entry, state, null);
            if (proposal.Tool == OsDebugTools.Start && result.Status == ResultStatus.Succeeded && !_stopped)
            {
                OpenGrant(entry);
            }
        }
        return true;
    }

    /// <summary>Markiert abgelaufene Vorschläge (wird periodisch aufgerufen).</summary>
    public void ExpireDue()
    {
        lock (_gate)
        {
            var now = _clock.UtcNow;
            foreach (var entry in _entries.Values)
            {
                if (entry.State is ProposalState.AwaitingApproval or ProposalState.Approved
                    && now > ContractTime.Parse(entry.Proposal.ExpiresAt))
                {
                    SetState(entry, ProposalState.Expired, "Zeit abgelaufen – warten bedeutet niemals Zustimmung");
                }
            }
        }
    }

    // ------------------------------------------------------------------ intern

    private async Task DispatcherLoopAsync()
    {
        try
        {
            await foreach (var id in _dispatchQueue.Reader.ReadAllAsync(_shutdown.Token).ConfigureAwait(false))
            {
                await _parallelJobs.WaitAsync(_shutdown.Token).ConfigureAwait(false);
                _ = Task.Run(async () =>
                {
                    try
                    {
                        await DispatchAsync(id).ConfigureAwait(false);
                    }
                    finally
                    {
                        _parallelJobs.Release();
                    }
                });
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    private async Task SweeperLoopAsync()
    {
        try
        {
            while (!_shutdown.IsCancellationRequested)
            {
                await Task.Delay(TimeSpan.FromSeconds(1), _shutdown.Token).ConfigureAwait(false);
                ExpireDue();
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    private Entry RequireOpen(string proposalId, string proposalSha256)
    {
        if (!ProposalHashing.IsSha256Hex(proposalSha256))
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, "proposalSha256 ist kein SHA-256");
        }
        var entry = Find(proposalId);
        if (entry.State == ProposalState.AwaitingApproval && _clock.UtcNow > ContractTime.Parse(entry.Proposal.ExpiresAt))
        {
            SetState(entry, ProposalState.Expired, "Zeit abgelaufen – warten bedeutet niemals Zustimmung");
        }
        if (entry.State == ProposalState.Expired)
        {
            throw new BrokerException(BrokerErrorCodes.Gone, "Vorschlag ist abgelaufen");
        }
        if (entry.State != ProposalState.AwaitingApproval)
        {
            throw new BrokerException(BrokerErrorCodes.Conflict, $"Vorschlag ist nicht mehr offen ({entry.State})");
        }
        if (_stopped)
        {
            throw new BrokerException(BrokerErrorCodes.Conflict, "Not-Aus ist aktiv");
        }
        if (!CryptographicOperations.FixedTimeEquals(
                System.Text.Encoding.ASCII.GetBytes(proposalSha256),
                System.Text.Encoding.ASCII.GetBytes(entry.Sha256)))
        {
            throw new BrokerException(BrokerErrorCodes.Conflict, "Hash stimmt nicht mit dem Vorschlag überein – bitte die aktuelle Vorschau prüfen");
        }
        if (ProposalHashing.Sha256(entry.Proposal) != entry.Sha256)
        {
            throw new BrokerException(BrokerErrorCodes.Internal, "Interner Hashfehler – Vorschlag wird nicht freigegeben");
        }
        return entry;
    }

    private Entry Find(string proposalId) =>
        _entries.TryGetValue(proposalId ?? string.Empty, out var entry)
            ? entry
            : throw new BrokerException(BrokerErrorCodes.NotFound, $"Vorschlag nicht gefunden: {proposalId}");

    private Entry CreateEntry(ToolProposal proposal, string? idempotencyKey, string? requestSha256)
    {
        var sha = ProposalHashing.Sha256(proposal);
        var entry = new Entry { Proposal = proposal, Sha256 = sha };
        _entries[proposal.ProposalId] = entry;
        _journal.Append("proposal.created", new JsonObject
        {
            ["proposal"] = JsonSerializer.SerializeToNode(proposal, ContractJson.Options),
            ["sha256"] = sha,
            ["idempotencyKey"] = idempotencyKey,
            ["requestSha256"] = requestSha256,
        });
        return entry;
    }

    private void SetState(Entry entry, ProposalState state, string? reason, string? supersededBy = null)
    {
        entry.State = state;
        entry.StateReason = reason;
        var data = new JsonObject { ["state"] = state.ToString(), ["reason"] = reason };
        if (supersededBy is not null)
        {
            data["supersededBy"] = supersededBy;
        }
        Journal("proposal.state", entry, data);
        AddEvent("proposal.state", entry, reason);
    }

    private void Journal(string type, Entry entry, JsonObject data)
    {
        data["proposalId"] = entry.Proposal.ProposalId;
        _journal.Append(type, data);
    }

    private void AddEvent(string type, Entry? entry, string? message, string? runId = null)
    {
        var evt = new BrokerEvent(
            Seq: ++_eventSeq,
            At: ContractTime.ToContract(_clock.UtcNow),
            Type: type,
            ProposalId: entry?.Proposal.ProposalId,
            RunId: entry?.Proposal.RunId ?? runId,
            State: entry?.State,
            Message: message);
        _events.Add(evt);
        if (_events.Count > MaxEventsKept)
        {
            _events.RemoveRange(0, _events.Count - MaxEventsKept);
        }
        var previous = _eventSignal;
        _eventSignal = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        previous.TrySetResult();
    }

    private IReadOnlyList<string>? SkippedTargets(Entry entry)
    {
        var current = entry.Proposal.Targets.Select(t => t.TargetId).ToHashSet(StringComparer.Ordinal);
        var root = entry;
        var hops = 0;
        while (root.Proposal.ParentProposalId is { } parent && _entries.TryGetValue(parent, out var p) && hops++ < 1000)
        {
            root = p;
        }
        var skipped = root.Proposal.Targets.Select(t => t.TargetId).Where(id => !current.Contains(id)).ToList();
        return skipped.Count > 0 ? skipped : null;
    }

    private void Replay(IReadOnlyList<JournalEntry> journal)
    {
        foreach (var record in journal)
        {
            var data = record.Data;
            var proposalId = data["proposalId"]?.GetValue<string>();
            switch (record.Type)
            {
                case "proposal.created":
                {
                    var proposal = ContractJson.Deserialize<ToolProposal>(data["proposal"]!.ToJsonString());
                    var sha = data["sha256"]!.GetValue<string>();
                    if (ProposalHashing.Sha256(proposal) != sha)
                    {
                        throw new AuditTamperException($"Vorschlag {proposal.ProposalId} im Journal passt nicht zu seinem Hash");
                    }
                    _entries[proposal.ProposalId] = new Entry { Proposal = proposal, Sha256 = sha };
                    var key = data["idempotencyKey"]?.GetValue<string>();
                    var requestSha = data["requestSha256"]?.GetValue<string>();
                    if (key is not null && requestSha is not null)
                    {
                        _idempotency[key] = (requestSha, proposal.ProposalId);
                    }
                    break;
                }
                case "proposal.state" when proposalId is not null && _entries.TryGetValue(proposalId, out var entry):
                    entry.State = Enum.Parse<ProposalState>(data["state"]!.GetValue<string>());
                    entry.StateReason = data["reason"]?.GetValue<string>();
                    entry.SupersededBy = data["supersededBy"]?.GetValue<string>() ?? entry.SupersededBy;
                    break;
                case "invocation.finished" when proposalId is not null && _entries.TryGetValue(proposalId, out var finished):
                    finished.Result = ContractJson.Deserialize<ToolResult>(data["result"]!.ToJsonString());
                    break;
                case "emergency.stop":
                    _stopped = true;
                    break;
                case "emergency.release":
                    _stopped = false;
                    break;
            }
        }
    }

    private static ToolResult EmptyResult(ToolProposal proposal, string invocationId, ResultStatus status) =>
        new(invocationId, proposal.ProposalId, status, proposal.SnapshotId, [],
            CanonicalJson.Sha256Hex(string.Empty), Truncated: false, Untrusted: true);

    private static ProposalView View(Entry entry) => new(entry.Proposal, entry.Sha256, entry.State);

    private void EnsureStarted()
    {
        if (!_started)
        {
            throw new InvalidOperationException("Broker wurde nicht gestartet");
        }
    }

    private static string NewId() => Guid.NewGuid().ToString("D");

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 1)
        {
            return;
        }
        await _shutdown.CancelAsync().ConfigureAwait(false);
        _dispatchQueue.Writer.TryComplete();
        foreach (var task in new[] { _dispatcherLoop, _sweeperLoop })
        {
            if (task is not null)
            {
                try
                {
                    await task.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }
        }
        _journal.Dispose();
        _shutdown.Dispose();
        _parallelJobs.Dispose();
    }
}
