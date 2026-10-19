using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.Text;
using LocalAi.Contracts;

namespace LocalAi.Desktop.Core;

/// <summary>Eine Zeile im Chat. Inhalte sind immer inerter Text (kein Markup, keine Links).</summary>
public sealed record ChatEntry(string Time, string Sender, string Text, bool IsUser)
{
    /// <summary>Untrusted (Agenten, Webinhalte): inert und begrenzt (SEC-14).</summary>
    public string Text { get; init; } = InertText.Clean(Text, 50_000);
}

/// <summary>Eintrag im Aktivitätsfeed eines Agenten (Zustände, Vorschläge, Warnungen, Kennzahlen).</summary>
public sealed record FeedEntry(string Time, string Kind, string Text)
{
    public string Text { get; init; } = InertText.Clean(Text, 20_000);
}

/// <summary>Live-Zustand eines Agenten (B17). Wird nur auf dem UI-Thread verändert.</summary>
public sealed class AgentViewModel : ObservableObject
{
    public const int MaxStreamChars = 60_000;
    public const int MaxEntries = 400;

    private readonly StringBuilder _output = new();
    private readonly StringBuilder _thinking = new();
    private string _stateText = UiText.Get("AgentReady");
    private AgentState _state = AgentState.Ready;
    private string _taskText = "–";
    private string _detailText = "";
    private string _metricText = "";
    private bool _isStalled;
    private bool _outputDirty;
    private bool _thinkingDirty;
    private string _outputText = "";
    private string _thinkingText = "";

    public AgentViewModel(string name, Func<AgentViewModel, Task>? cancel = null)
    {
        Name = name;
        CancelCommand = new RelayCommand(() => cancel?.Invoke(this) ?? Task.CompletedTask, () => CanCancel);
    }

    public string Name { get; }
    public bool IsRole => Name != "Orchestrator";
    public ObservableCollection<FeedEntry> Entries { get; } = [];
    public RelayCommand CancelCommand { get; }

    public AgentState State => _state;

    /// <summary>Zustand als Text (nicht nur Farbe), mit „hängt?“ bei ausbleibenden Tokens.</summary>
    public string StateText
    {
        get => _isStalled ? $"{_stateText} · {UiText.Get("AgentStalled")}" : _stateText;
    }

    public string TaskText
    {
        get => _taskText;
        private set => Set(ref _taskText, value);
    }

    public string DetailText
    {
        get => _detailText;
        private set => Set(ref _detailText, value);
    }

    public string MetricText
    {
        get => _metricText;
        private set => Set(ref _metricText, value);
    }

    public bool IsStalled
    {
        get => _isStalled;
        private set
        {
            if (Set(ref _isStalled, value))
            {
                OnPropertyChanged(nameof(StateText));
            }
        }
    }

    public bool IsBusy => _state is AgentState.Generating or AgentState.Queued or AgentState.WaitingApproval or AgentState.WaitingUser;
    public bool CanCancel => IsRole && IsBusy;

    public string OutputText => _outputText;
    public string ThinkingText => _thinkingText;
    public bool HasThinking => _thinking.Length > 0;

    internal void Apply(ActivityEvent e, string time)
    {
        switch (e.Kind)
        {
            case ActivityKind.Output:
                Append(_output, e.Text);
                _outputDirty = true;
                IsStalled = false;
                break;
            case ActivityKind.Thinking:
                Append(_thinking, e.Text);
                _thinkingDirty = true;
                IsStalled = false;
                break;
            case ActivityKind.State:
                if (e.State is { } state)
                {
                    var wasBusy = IsBusy;
                    _state = state;
                    _stateText = UiText.AgentState(state);
                    if (state is not AgentState.Generating)
                    {
                        IsStalled = false;
                    }
                    OnPropertyChanged(nameof(State));
                    OnPropertyChanged(nameof(StateText));
                    if (wasBusy != IsBusy)
                    {
                        OnPropertyChanged(nameof(IsBusy));
                        OnPropertyChanged(nameof(CanCancel));
                        CancelCommand.Refresh();
                    }
                    if (state == AgentState.Generating && e.Text?.StartsWith("Schritt", StringComparison.Ordinal) == true)
                    {
                        // Neuer Modellschritt: Trennlinie in der Ausgabe.
                        Append(_output, (_output.Length > 0 ? "\n\n" : "") + $"── {e.Text} ──\n");
                        _outputDirty = true;
                    }
                }
                if (e.TaskId is { } task && !string.IsNullOrEmpty(e.Text) && e.Text.StartsWith("Aufgabe", StringComparison.Ordinal))
                {
                    TaskText = e.Text;
                }
                if (!string.IsNullOrEmpty(e.Text))
                {
                    DetailText = e.Text;
                    AddEntry(time, UiText.Get("FeedState"), $"{_stateText}: {e.Text}");
                }
                break;
            case ActivityKind.Metric:
                if (e.Stalled == false)
                {
                    IsStalled = false;
                }
                var parts = new List<string>();
                if (e.Model is not null)
                {
                    parts.Add(e.Model);
                }
                if (e.Tokens is { } tokens)
                {
                    parts.Add(UiText.Format("MetricTokens", tokens));
                }
                if (e.TokensPerSecond is { } tps)
                {
                    parts.Add(UiText.Format("MetricTps", tps));
                }
                if (e.ElapsedMs is { } ms)
                {
                    parts.Add(UiText.Format("MetricElapsed", ms / 1000.0));
                }
                if (e.ContextTokens is { } ctx)
                {
                    parts.Add(UiText.Format("MetricContext", ctx));
                }
                if (parts.Count > 0)
                {
                    MetricText = string.Join(" · ", parts);
                }
                if (!string.IsNullOrEmpty(e.Text))
                {
                    AddEntry(time, UiText.Get("FeedMetric"), e.Text);
                }
                break;
            case ActivityKind.Warning:
                if (e.Stalled == true)
                {
                    IsStalled = true;
                }
                AddEntry(time, UiText.Get("FeedWarning"), e.Text ?? "");
                break;
            case ActivityKind.Error:
                AddEntry(time, UiText.Get("FeedError"), e.Text ?? "");
                break;
            case ActivityKind.Proposal:
                AddEntry(time, UiText.Get("FeedProposal"), e.Text ?? "");
                if (e.State is { } waiting)
                {
                    _state = waiting;
                    _stateText = UiText.AgentState(waiting);
                    OnPropertyChanged(nameof(StateText));
                    OnPropertyChanged(nameof(IsBusy));
                    OnPropertyChanged(nameof(CanCancel));
                    CancelCommand.Refresh();
                }
                break;
            case ActivityKind.Task:
                AddEntry(time, UiText.Get("FeedTask"), e.Text ?? "");
                break;
            case ActivityKind.Chat:
                AddEntry(time, UiText.Get("FeedChat"), Shorten(e.Text ?? "", 300));
                break;
        }
    }

    /// <summary>Gesammelte Textänderungen einmal pro Takt an die Oberfläche geben (gedrosseltes Rendering).</summary>
    internal void Publish()
    {
        if (_outputDirty)
        {
            _outputDirty = false;
            _outputText = InertText.Clean(_output.ToString());
            OnPropertyChanged(nameof(OutputText));
        }
        if (_thinkingDirty)
        {
            _thinkingDirty = false;
            _thinkingText = InertText.Clean(_thinking.ToString());
            OnPropertyChanged(nameof(ThinkingText));
            OnPropertyChanged(nameof(HasThinking));
        }
    }

    private void AddEntry(string time, string kind, string text)
    {
        Entries.Add(new FeedEntry(time, kind, text));
        while (Entries.Count > MaxEntries)
        {
            Entries.RemoveAt(0);
        }
    }

    private static void Append(StringBuilder target, string? text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return;
        }
        target.Append(text);
        if (target.Length > MaxStreamChars)
        {
            // Älteres verwerfen: die Ansicht bleibt schnell, der Verlauf steht im Protokoll des Orchestrators.
            var cut = target.Length - MaxStreamChars;
            target.Remove(0, cut);
            target.Insert(0, "…");
        }
    }

    private static string Shorten(string text, int max) => text.Length <= max ? text : text[..max] + " …";
}

/// <summary>
/// Puffert Aktivitätsereignisse aus dem Hintergrund und wendet sie gebündelt auf dem UI-Thread an
/// (höchstens alle <see cref="Interval"/>). So bleibt die Oberfläche auch bei schnellem Tokenstrom bedienbar.
/// </summary>
public sealed class ActivityBuffer : IDisposable
{
    private readonly ConcurrentQueue<ActivityEvent> _queue = new();
    private readonly Action<IReadOnlyList<ActivityEvent>> _apply;
    private readonly Action<Action> _post;
    private readonly Timer? _timer;
    private int _scheduled;

    public ActivityBuffer(Action<IReadOnlyList<ActivityEvent>> apply, Action<Action> post, TimeSpan? interval = null, bool useTimer = true)
    {
        _apply = apply;
        _post = post;
        Interval = interval ?? TimeSpan.FromMilliseconds(150);
        if (useTimer)
        {
            _timer = new Timer(_ => Schedule(), null, Interval, Interval);
        }
    }

    public TimeSpan Interval { get; }

    public int Pending => _queue.Count;

    public void Enqueue(IEnumerable<ActivityEvent> events)
    {
        foreach (var e in events)
        {
            _queue.Enqueue(e);
        }
    }

    /// <summary>Höchstens ein ausstehender Auftrag an den UI-Thread gleichzeitig.</summary>
    private void Schedule()
    {
        if (_queue.IsEmpty || Interlocked.Exchange(ref _scheduled, 1) == 1)
        {
            return;
        }
        _post(Drain);
    }

    /// <summary>Wendet alles Gepufferte an (auch direkt aus Tests).</summary>
    public void Drain()
    {
        var batch = new List<ActivityEvent>();
        while (batch.Count < 5_000 && _queue.TryDequeue(out var e))
        {
            batch.Add(e);
        }
        Volatile.Write(ref _scheduled, 0);
        if (batch.Count > 0)
        {
            _apply(batch);
        }
    }

    public void Dispose() => _timer?.Dispose();
}
