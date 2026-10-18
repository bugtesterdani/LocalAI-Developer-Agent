using System.Text.Json.Nodes;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>
/// Live-Aktivität der Agenten (Spezifikation §11, R14). Reine Anzeige: nichts hier ändert Vorschläge,
/// Freigaben oder Capabilities. Der Orchestrator meldet, die Oberfläche liest.
/// Zustände, Aufgaben, Chat und Fehler kommen zusätzlich ins Audit-Journal, gestreamte Tokens nicht.
/// </summary>
public sealed class ActivityHub(AuditJournal journal, IClock clock)
{
    public const int MaxTextChars = 16_384;
    public const int MaxEventsPerReport = 500;
    public const int MaxEventsKept = 20_000;
    public const int MaxEventsPerRead = 2_000;
    private const int MaxJournalTextChars = 4_000;

    /// <summary>Absender, die der Orchestrator verwenden darf. "User" setzt nur der Broker selbst.</summary>
    public static readonly IReadOnlySet<string> OrchestratorAgents =
        new HashSet<string>(Enum.GetNames<AgentRole>().Append("Orchestrator"), StringComparer.Ordinal);

    private static readonly HashSet<ActivityKind> Journaled =
        [ActivityKind.State, ActivityKind.Task, ActivityKind.Chat, ActivityKind.Error, ActivityKind.Proposal];

    private readonly Lock _gate = new();
    private readonly List<ActivityEvent> _events = [];
    private TaskCompletionSource _signal = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private long _seq;
    private DateTimeOffset _lastReport = DateTimeOffset.MinValue;

    /// <summary>Zeitpunkt der letzten Meldung des Orchestrators (für den Hänger-Watchdog, SEC-22).</summary>
    public DateTimeOffset LastReport
    {
        get { lock (_gate) { return _lastReport; } }
    }

    /// <summary>Übernimmt Meldungen des Orchestrators. seq und at setzt der Broker; zu lange Texte werden gekürzt.</summary>
    public int Report(ActivityReport report)
    {
        if (report.Events.Count > MaxEventsPerReport)
        {
            throw new BrokerException(BrokerErrorCodes.TooMany, $"Höchstens {MaxEventsPerReport} Aktivitätsereignisse pro Meldung");
        }
        if (string.IsNullOrWhiteSpace(report.RunId) || report.RunId.Length > 100)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, "runId fehlt oder ist zu lang");
        }
        foreach (var e in report.Events)
        {
            if (!OrchestratorAgents.Contains(e.Agent))
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Unbekannter Absender: {e.Agent}");
            }
            if (e.RunId != report.RunId)
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, "runId der Ereignisse passt nicht zur Meldung");
            }
        }
        lock (_gate)
        {
            _lastReport = clock.UtcNow;
            foreach (var e in report.Events)
            {
                if (e.Kind == ActivityKind.Heartbeat)
                {
                    continue; // nur Lebenszeichen, nicht anzeigen
                }
                Add(e with { Text = Clip(e.Text), TaskId = Clip(e.TaskId, 200), Model = Clip(e.Model, 200) });
            }
            if (report.Events.Any(e => e.Kind != ActivityKind.Heartbeat))
            {
                Signal();
            }
            return report.Events.Count;
        }
    }

    /// <summary>Nachricht des Nutzers im Chat (nur über den UI-Token).</summary>
    public ActivityEvent AddUserChat(string runId, string text)
    {
        lock (_gate)
        {
            var e = Add(new ActivityEvent(0, "", runId, "User", ActivityKind.Chat, Text: Clip(text)));
            Signal();
            return e;
        }
    }

    /// <summary>Hinweis des Brokers selbst (z. B. Orchestrator beendet), erscheint im Feed des Orchestrators.</summary>
    public void AddBrokerNotice(string runId, ActivityKind kind, string text, AgentState? state = null)
    {
        lock (_gate)
        {
            Add(new ActivityEvent(0, "", runId, "Orchestrator", kind, Text: Clip(text), State: state));
            Signal();
        }
    }

    public async Task<IReadOnlyList<ActivityEvent>> WaitAsync(long afterSeq, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            Task signal;
            lock (_gate)
            {
                var start = _events.FindIndex(e => e.Seq > afterSeq);
                if (start >= 0)
                {
                    return _events.GetRange(start, Math.Min(MaxEventsPerRead, _events.Count - start));
                }
                signal = _signal.Task;
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

    private ActivityEvent Add(ActivityEvent e)
    {
        var stored = e with { Seq = ++_seq, At = ContractTime.ToContract(clock.UtcNow) };
        _events.Add(stored);
        if (_events.Count > MaxEventsKept)
        {
            _events.RemoveRange(0, _events.Count - MaxEventsKept);
        }
        if (Journaled.Contains(stored.Kind))
        {
            try
            {
                journal.Append("activity", new JsonObject
                {
                    ["runId"] = stored.RunId,
                    ["agent"] = stored.Agent,
                    ["kind"] = stored.Kind.ToString(),
                    ["state"] = stored.State?.ToString(),
                    ["taskId"] = stored.TaskId,
                    ["text"] = Clip(stored.Text, MaxJournalTextChars),
                });
            }
            catch (ObjectDisposedException)
            {
            }
        }
        return stored;
    }

    private void Signal()
    {
        var previous = _signal;
        _signal = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        previous.TrySetResult();
    }

    private static string? Clip(string? text, int max = MaxTextChars) =>
        text is null || text.Length <= max ? text : text[..max] + " …[gekürzt]";
}

/// <summary>
/// Aufträge der Oberfläche an den Orchestrator: Chatnachrichten und Abbrüche. Der Orchestrator holt sie per
/// Long-Polling ab. Abbrüche wirken zusätzlich sofort im Broker (offene Vorschläge des Agenten werden ungültig).
/// </summary>
public sealed class OrchestratorInbox(IClock clock)
{
    private const int MaxKept = 1_000;
    private readonly Lock _gate = new();
    private readonly List<OrchestratorCommand> _commands = [];
    private TaskCompletionSource _signal = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private long _seq;
    private DateTimeOffset _lastPoll = DateTimeOffset.MinValue;

    /// <summary>Letzte Abfrage durch den Orchestrator (Lebenszeichen).</summary>
    public DateTimeOffset LastPoll
    {
        get { lock (_gate) { return _lastPoll; } }
    }

    public OrchestratorCommand Post(OrchestratorCommandKind kind, string? agent = null, string? text = null)
    {
        lock (_gate)
        {
            var command = new OrchestratorCommand(++_seq, ContractTime.ToContract(clock.UtcNow), kind, agent, text);
            _commands.Add(command);
            if (_commands.Count > MaxKept)
            {
                _commands.RemoveRange(0, _commands.Count - MaxKept);
            }
            var previous = _signal;
            _signal = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            previous.TrySetResult();
            return command;
        }
    }

    /// <summary>Verwirft alte Aufträge (neuer Orchestrator-Prozess beginnt ohne Altlasten, außer der ersten Chatnachricht).</summary>
    public long CurrentSeq
    {
        get { lock (_gate) { return _seq; } }
    }

    public async Task<IReadOnlyList<OrchestratorCommand>> WaitAsync(long afterSeq, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            Task signal;
            lock (_gate)
            {
                _lastPoll = clock.UtcNow;
                var found = _commands.Where(c => c.Seq > afterSeq).ToList();
                if (found.Count > 0)
                {
                    return found;
                }
                signal = _signal.Task;
            }
            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                lock (_gate)
                {
                    _lastPoll = clock.UtcNow;
                }
                return [];
            }
            await Task.WhenAny(signal, Task.Delay(remaining, cancellationToken)).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
        }
    }
}
