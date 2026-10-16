using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using LocalAi.Contracts;

namespace LocalAi.Desktop.Core;

/// <summary>Broker-Funktionen, die die Oberfläche nutzt (austauschbar für Tests).</summary>
public interface IBrokerUiApi
{
    Task<IReadOnlyList<ProposalView>> PendingAsync(CancellationToken ct);
    Task<ProposalView> DecideAsync(UserDecision decision, CancellationToken ct);
    Task<ProposalView> NarrowAsync(NarrowRequest request, CancellationToken ct);
    Task<IReadOnlyList<BrokerEvent>> WaitEventsAsync(long afterSeq, int timeoutMs, CancellationToken ct);
    Task EmergencyStopAsync(CancellationToken ct);
    Task ReleaseAsync(CancellationToken ct);
    Task<JsonElement> StartFakeAgentAsync(CancellationToken ct);
    Task<IReadOnlyList<FolderGrant>> GrantsAsync(CancellationToken ct);
}

public sealed record AgentItem(string Name, string StateText);

public sealed record EventLine(string Time, string Text);

public sealed class TargetItemViewModel(ToolTarget target, Action selectionChanged) : ObservableObject
{
    private bool _isSelected = true;

    public ToolTarget Target { get; } = target;
    public string TargetId => Target.TargetId;
    public string Path => Target.Path;

    public string LinesText => Target.LineStart is null && Target.LineEnd is null
        ? UiText.Get("LinesAll")
        : UiText.Format("Lines", Target.LineStart ?? 1, Target.LineEnd?.ToString(CultureInfo.InvariantCulture) ?? "Ende");

    public string HashText => Target.ExpectedSha256 is { } h ? h[..12] + "…" : UiText.Get("NoExpectedHash");

    public bool IsSelected
    {
        get => _isSelected;
        set
        {
            if (Set(ref _isSelected, value))
            {
                selectionChanged();
            }
        }
    }
}

/// <summary>Freigabekarte für genau einen unveränderlichen Vorschlag.</summary>
public sealed class ApprovalCardViewModel : ObservableObject
{
    private static readonly HashSet<string> NarrowableTools = new(StringComparer.Ordinal) { "File.ReadMany", "Patch.Apply" };

    private readonly MainViewModel _owner;
    private string _filterText = string.Empty;
    private bool _busy;

    public ApprovalCardViewModel(ProposalView view, MainViewModel owner)
    {
        View = view;
        _owner = owner;
        Targets = view.Proposal.Targets.Select(t => new TargetItemViewModel(t, OnSelectionChanged)).ToList();
        VisibleTargets = new ObservableCollection<TargetItemViewModel>(Targets);
        ApproveCommand = new RelayCommand(() => RunBusy(() => _owner.DecideAsync(this, approve: true)), () => CanApprove);
        DenyCommand = new RelayCommand(() => RunBusy(() => _owner.DecideAsync(this, approve: false)), () => !_busy);
        ApplySelectionCommand = new RelayCommand(() => RunBusy(() => _owner.NarrowAsync(this)), () => CanApplySelection);
        SelectAllVisibleCommand = new RelayCommand(() => SetVisible(true), () => IsBatch);
        DeselectAllVisibleCommand = new RelayCommand(() => SetVisible(false), () => IsBatch);
    }

    public ProposalView View { get; }
    public ToolProposal Proposal => View.Proposal;
    public string ProposalId => Proposal.ProposalId;
    public string Sha256 => View.ProposalSha256;
    public IReadOnlyList<TargetItemViewModel> Targets { get; }
    public ObservableCollection<TargetItemViewModel> VisibleTargets { get; }

    public string Title => $"{Proposal.Agent} · {Proposal.Tool}";
    public string Summary => Targets.Count == 1 ? Targets[0].Path : UiText.Format("SelectionCount", Targets.Count, Targets.Count);
    public string RoleText => Proposal.Agent.ToString();
    public string ToolText => $"{Proposal.Tool} (Version {Proposal.ToolVersion})";
    public string PurposeText => Proposal.Purpose;
    public string SnapshotText => Proposal.SnapshotId;
    public string SandboxText => Proposal.SandboxId;
    public string HashText => Sha256;
    public string ParentText => Proposal.ParentProposalId ?? "–";
    public bool HasParent => Proposal.ParentProposalId is not null;
    public string SideEffectsText => _owner.SandboxMode == "test"
        ? UiText.Get("SideEffectsTest")
        : UiText.Get(Proposal.Tool == "Workspace.Snapshot" ? "SideEffectsSnapshot" : "SideEffectsRead");

    public string ValidUntilText =>
        ContractTime.Parse(Proposal.ExpiresAt).ToLocalTime().ToString("HH:mm:ss", UiText.Culture);

    public string ArgumentsText => JsonSerializer.Serialize(Proposal.Arguments, new JsonSerializerOptions
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    });

    public bool IsBatch => NarrowableTools.Contains(Proposal.Tool) && Targets.Count > 1;

    public int TotalCount => Targets.Count;
    public int SelectedCount => Targets.Count(t => t.IsSelected);
    public string CountText => UiText.Format("SelectionCount", SelectedCount, TotalCount);

    /// <summary>Auswahl weicht vom angezeigten Vorschlag ab: Erlauben ist gesperrt, bis sie übernommen wurde.</summary>
    public bool SelectionChanged => SelectedCount != TotalCount;

    public bool CanApprove => !_busy && !SelectionChanged && !_owner.IsStopped;
    public bool CanApplySelection => !_busy && IsBatch && SelectionChanged && SelectedCount > 0 && !_owner.IsStopped;

    public string ApproveText => UiText.Get(IsBatch ? "ApproveSelectionOnce" : "ApproveOnce");

    public string FilterText
    {
        get => _filterText;
        set
        {
            if (Set(ref _filterText, value))
            {
                ApplyFilter();
            }
        }
    }

    public RelayCommand ApproveCommand { get; }
    public RelayCommand DenyCommand { get; }
    public RelayCommand ApplySelectionCommand { get; }
    public RelayCommand SelectAllVisibleCommand { get; }
    public RelayCommand DeselectAllVisibleCommand { get; }

    public NarrowRequest BuildNarrowRequest() =>
        new(ProposalId, Sha256, Targets.Where(t => t.IsSelected).Select(t => t.TargetId).ToList());

    public void RefreshCommands()
    {
        OnPropertyChanged(nameof(CanApprove));
        OnPropertyChanged(nameof(CanApplySelection));
        ApproveCommand.Refresh();
        DenyCommand.Refresh();
        ApplySelectionCommand.Refresh();
    }

    private void OnSelectionChanged()
    {
        OnPropertyChanged(nameof(SelectedCount));
        OnPropertyChanged(nameof(CountText));
        OnPropertyChanged(nameof(SelectionChanged));
        RefreshCommands();
    }

    private Task SetVisible(bool selected)
    {
        foreach (var target in VisibleTargets)
        {
            target.IsSelected = selected;
        }
        return Task.CompletedTask;
    }

    private void ApplyFilter()
    {
        var filter = _filterText.Trim();
        VisibleTargets.Clear();
        foreach (var target in Targets.Where(t => filter.Length == 0 || t.Path.Contains(filter, StringComparison.OrdinalIgnoreCase)))
        {
            VisibleTargets.Add(target);
        }
    }

    private async Task RunBusy(Func<Task> action)
    {
        _busy = true;
        RefreshCommands();
        try
        {
            await action();
        }
        finally
        {
            _busy = false;
            RefreshCommands();
        }
    }
}

/// <summary>Hauptansicht: Agenten, Ereignisse, Freigabe-Queue, Not-Aus.</summary>
public sealed class MainViewModel : ObservableObject, IAsyncDisposable
{
    private readonly IBrokerUiApi _api;
    private readonly IBrokerUiApi _eventsApi;
    private readonly Action<Action> _post;
    private readonly CancellationTokenSource _shutdown = new();
    private ApprovalCardViewModel? _selected;
    private string _statusText = UiText.Get("Starting");
    private string? _errorText;
    private string? _infoText;
    private bool _isStopped;
    private long _lastSeq;
    private string? _selectAfterRefresh;
    private string _projectText = UiText.Get("ProjectNone");
    private Task? _eventLoop;

    public MainViewModel(IBrokerUiApi api, IBrokerUiApi eventsApi, Action<Action> post, string sessionId, string sandboxMode = "test")
    {
        SandboxMode = sandboxMode;
        _api = api;
        _eventsApi = eventsApi;
        _post = post;
        StatusText = UiText.Format("Connected", sessionId[..8]);
        Agents = new ObservableCollection<AgentItem>(Enum.GetValues<AgentRole>().Select(r => new AgentItem(r.ToString(), UiText.Get("AgentReady"))));
        StartFakeRunCommand = new RelayCommand(StartFakeRunAsync, () => !IsStopped);
        EmergencyStopCommand = new RelayCommand(EmergencyStopAsync);
        ReleaseCommand = new RelayCommand(ReleaseAsync, () => IsStopped);
    }

    public ObservableCollection<AgentItem> Agents { get; }
    public ObservableCollection<ApprovalCardViewModel> Approvals { get; } = [];
    public ObservableCollection<EventLine> EventLog { get; } = [];

    public RelayCommand StartFakeRunCommand { get; }
    public RelayCommand EmergencyStopCommand { get; }
    public RelayCommand ReleaseCommand { get; }

    public string SandboxMode { get; }
    public string SandboxText => UiText.Exists($"Sandbox_{SandboxMode}") ? UiText.Get($"Sandbox_{SandboxMode}") : SandboxMode;

    /// <summary>Entwicklungsmodus ohne VM: in der Oberfläche deutlich als unsicher markiert.</summary>
    public bool IsUnsafeSandbox => SandboxMode == "dev-local-unsafe";

    public string ProjectText
    {
        get => _projectText;
        private set => Set(ref _projectText, value);
    }
    public bool HasApprovals => Approvals.Count > 0;
    public string ApprovalsHeader => $"{UiText.Get("Approvals")} ({Approvals.Count})";

    public ApprovalCardViewModel? SelectedApproval
    {
        get => _selected;
        set => Set(ref _selected, value);
    }

    public string StatusText
    {
        get => _statusText;
        private set => Set(ref _statusText, value);
    }

    public string? ErrorText
    {
        get => _errorText;
        private set => Set(ref _errorText, value);
    }

    public string? InfoText
    {
        get => _infoText;
        private set => Set(ref _infoText, value);
    }

    public bool IsStopped
    {
        get => _isStopped;
        private set
        {
            if (Set(ref _isStopped, value))
            {
                StartFakeRunCommand.Refresh();
                ReleaseCommand.Refresh();
                foreach (var card in Approvals)
                {
                    card.RefreshCommands();
                }
            }
        }
    }

    public async Task InitializeAsync()
    {
        await GuardAsync(async () =>
        {
            var grants = await _api.GrantsAsync(_shutdown.Token).ConfigureAwait(false);
            var text = grants.Count == 0
                ? UiText.Get("ProjectNone")
                : string.Join(", ", grants.Select(g => $"{g.GrantId} ({string.Join("/", g.Rights)}): {g.Root}"));
            Post(() => ProjectText = text);
        }).ConfigureAwait(false);
        await RefreshPendingAsync().ConfigureAwait(false);
        _eventLoop = Task.Run(EventLoopAsync);
    }

    internal async Task DecideAsync(ApprovalCardViewModel card, bool approve)
    {
        await GuardAsync(async () =>
        {
            await _api.DecideAsync(new UserDecision(card.ProposalId, card.Sha256, approve ? DecisionKind.Approve : DecisionKind.Deny), _shutdown.Token).ConfigureAwait(false);
            Post(() => InfoText = UiText.Format(approve ? "ApprovedInfo" : "DeniedInfo", card.Title));
        }).ConfigureAwait(false);
        await RefreshPendingAsync().ConfigureAwait(false);
    }

    internal async Task NarrowAsync(ApprovalCardViewModel card)
    {
        await GuardAsync(async () =>
        {
            var request = card.BuildNarrowRequest();
            var view = await _api.NarrowAsync(request, _shutdown.Token).ConfigureAwait(false);
            _selectAfterRefresh = view.Proposal.ProposalId;
            Post(() => InfoText = UiText.Format("NarrowedInfo", request.KeepTargetIds.Count, card.TotalCount));
        }).ConfigureAwait(false);
        await RefreshPendingAsync().ConfigureAwait(false);
    }

    public async Task RefreshPendingAsync()
    {
        IReadOnlyList<ProposalView> pending = [];
        await GuardAsync(async () => pending = await _api.PendingAsync(_shutdown.Token).ConfigureAwait(false)).ConfigureAwait(false);
        Post(() => SyncApprovals(pending));
    }

    /// <summary>Gleicht die Kartenliste ab, ohne die Auswahl bestehender Karten zu verlieren.</summary>
    internal void SyncApprovals(IReadOnlyList<ProposalView> pending)
    {
        var ids = pending.Select(p => p.Proposal.ProposalId).ToHashSet(StringComparer.Ordinal);
        foreach (var stale in Approvals.Where(c => !ids.Contains(c.ProposalId)).ToList())
        {
            Approvals.Remove(stale);
        }
        var known = Approvals.Select(c => c.ProposalId).ToHashSet(StringComparer.Ordinal);
        foreach (var view in pending.Where(p => !known.Contains(p.Proposal.ProposalId)))
        {
            Approvals.Add(new ApprovalCardViewModel(view, this));
        }
        var wanted = _selectAfterRefresh is not null ? Approvals.FirstOrDefault(c => c.ProposalId == _selectAfterRefresh) : null;
        if (wanted is not null)
        {
            _selectAfterRefresh = null;
            SelectedApproval = wanted;
        }
        else if (SelectedApproval is null || !Approvals.Contains(SelectedApproval))
        {
            SelectedApproval = Approvals.FirstOrDefault();
        }
        OnPropertyChanged(nameof(HasApprovals));
        OnPropertyChanged(nameof(ApprovalsHeader));
    }

    private async Task EventLoopAsync()
    {
        while (!_shutdown.IsCancellationRequested)
        {
            IReadOnlyList<BrokerEvent> events;
            try
            {
                events = await _eventsApi.WaitEventsAsync(_lastSeq, 15_000, _shutdown.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception ex)
            {
                Post(() => ErrorText = UiText.Format("ConnectFailed", ex.Message));
                await Task.Delay(2000, _shutdown.Token).ConfigureAwait(false);
                continue;
            }
            if (events.Count == 0)
            {
                continue;
            }
            _lastSeq = events[^1].Seq;
            Post(() => ApplyEvents(events));
            if (events.Any(e => e.Type.StartsWith("proposal.", StringComparison.Ordinal) || e.Type.StartsWith("emergency.", StringComparison.Ordinal)))
            {
                await RefreshPendingAsync().ConfigureAwait(false);
            }
        }
    }

    internal void ApplyEvents(IReadOnlyList<BrokerEvent> events)
    {
        foreach (var e in events)
        {
            var time = ContractTime.Parse(e.At).ToLocalTime().ToString("HH:mm:ss", UiText.Culture);
            var parts = new List<string> { UiText.Event(e.Type) };
            if (e.ProposalId is not null)
            {
                parts.Add(e.ProposalId[..8]);
            }
            if (e.State is { } state)
            {
                parts.Add(UiText.State(state));
            }
            if (!string.IsNullOrEmpty(e.Message))
            {
                parts.Add(e.Message);
            }
            EventLog.Insert(0, new EventLine(time, string.Join(" · ", parts)));
            if (e.Type == "emergency.stop")
            {
                IsStopped = true;
            }
            else if (e.Type == "emergency.release")
            {
                IsStopped = false;
            }
            else if (e.Type == "broker.started" && e.Message is not null)
            {
                IsStopped = true;
            }
        }
        while (EventLog.Count > 500)
        {
            EventLog.RemoveAt(EventLog.Count - 1);
        }
    }

    private async Task StartFakeRunAsync()
    {
        await GuardAsync(async () =>
        {
            var result = await _api.StartFakeAgentAsync(_shutdown.Token).ConfigureAwait(false);
            var pid = result.GetProperty("pid").GetInt32();
            var log = result.GetProperty("log").GetString();
            Post(() => InfoText = UiText.Format("FakeAgentStarted", pid, log));
        }).ConfigureAwait(false);
    }

    private Task EmergencyStopAsync() => GuardAsync(async () =>
    {
        await _api.EmergencyStopAsync(_shutdown.Token).ConfigureAwait(false);
        Post(() => IsStopped = true);
    });

    private Task ReleaseAsync() => GuardAsync(async () =>
    {
        await _api.ReleaseAsync(_shutdown.Token).ConfigureAwait(false);
        Post(() => IsStopped = false);
    });

    private async Task GuardAsync(Func<Task> action)
    {
        try
        {
            Post(() => ErrorText = null);
            await action().ConfigureAwait(false);
        }
        catch (BrokerCallException ex)
        {
            Post(() => ErrorText = UiText.Format("ErrorFormat", ex.Code, ex.Message));
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException)
        {
            Post(() => ErrorText = UiText.Format("ConnectFailed", ex.Message));
        }
    }

    private void Post(Action action) => _post(action);

    public async ValueTask DisposeAsync()
    {
        await _shutdown.CancelAsync().ConfigureAwait(false);
        if (_eventLoop is not null)
        {
            try
            {
                // Begrenzt warten: eine hängende Pipe-Leseoperation darf das Beenden nicht blockieren.
                await _eventLoop.WaitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is OperationCanceledException or TimeoutException)
            {
            }
        }
    }
}
