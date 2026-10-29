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
    Task<JsonElement> StatusAsync(CancellationToken ct);
    Task<IReadOnlyList<ToolchainInfo>> ScanToolchainsAsync(CancellationToken ct);
    Task RegisterToolchainAsync(string directory, string digest, CancellationToken ct);
    Task UnregisterToolchainAsync(string name, CancellationToken ct);
    Task<string> ChatSendAsync(string text, CancellationToken ct);
    Task AgentCancelAsync(string agent, CancellationToken ct);
    Task RunCancelAsync(CancellationToken ct);
    Task<IReadOnlyList<ActivityEvent>> WaitActivityAsync(long afterSeq, int timeoutMs, CancellationToken ct);
    Task OrchestratorStopAsync(CancellationToken ct);
    Task RebuildSandboxAsync(CancellationToken ct);

    /// <summary>„VM weiter verwenden“ nach ausgebliebener Antwort.</summary>
    Task ContinueSandboxAsync(CancellationToken ct) => Task.CompletedTask;

    /// <summary>Bereinigter Text eines Webabrufs für die Inhaltsfreigabe (nur die Oberfläche sieht ihn vorher).</summary>
    Task<WebCapture?> WebCaptureAsync(string captureId, CancellationToken ct) => Task.FromResult<WebCapture?>(null);

    /// <summary>Versions- und Offline-Manifest (M7).</summary>
    Task<JsonElement> ManifestAsync(CancellationToken ct) => Task.FromResult(default(JsonElement));

    /// <summary>Audit-Journal prüfen: Hashkette und Freigabeinvarianten (AC03).</summary>
    Task<JsonElement> AuditVerifyAsync(CancellationToken ct) => Task.FromResult(default(JsonElement));

    /// <summary>Diagnosepaket erstellen; Protokolle nur nach ausdrücklicher Auswahl.</summary>
    Task<JsonElement> DiagnosticsExportAsync(bool includeLogs, CancellationToken ct) => Task.FromResult(default(JsonElement));

    /// <summary>Sicherung (Journal, Registrierungen, Kalibrierung, Konfiguration) erstellen.</summary>
    Task<JsonElement> BackupCreateAsync(CancellationToken ct) => Task.FromResult(default(JsonElement));
}

public sealed record EventLine(string Time, string Text)
{
    public string Text { get; init; } = InertText.Clean(Text, 4_000);
}

public sealed class TargetItemViewModel(ToolTarget target, Action selectionChanged, PatchFileKind? patchKind = null) : ObservableObject
{
    private bool _isSelected = true;

    public ToolTarget Target { get; } = target;
    public string TargetId => Target.TargetId;
    public string Path => Target.Path;

    /// <summary>Bei Patch.Apply: neu/ändern/löschen statt Zeilenbereich.</summary>
    public string LinesText => patchKind is { } kind
        ? UiText.Get(kind switch
        {
            PatchFileKind.Create => "PatchCreate",
            PatchFileKind.Delete => "PatchDelete",
            _ => "PatchModify",
        })
        : Target.LineStart is null && Target.LineEnd is null
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
    private static readonly HashSet<string> NarrowableTools = new(StringComparer.Ordinal) { "File.ReadMany", "Patch.Apply", "Workspace.ExportPatch" };
    private static readonly HashSet<string> PatchTools = new(StringComparer.Ordinal) { "Patch.Apply", "Workspace.ExportPatch" };

    private readonly MainViewModel _owner;
    private readonly string? _patch;
    private string _filterText = string.Empty;
    private bool _busy;

    public ApprovalCardViewModel(ProposalView view, MainViewModel owner)
    {
        View = view;
        _owner = owner;
        _patch = PatchTools.Contains(Proposal.Tool) && Proposal.Arguments.ValueKind == JsonValueKind.Object
                 && Proposal.Arguments.TryGetProperty("patch", out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;
        var kinds = new Dictionary<string, PatchFileKind>(StringComparer.Ordinal);
        if (_patch is not null)
        {
            try
            {
                foreach (var file in PatchHeaders.Parse(_patch))
                {
                    kinds[file.Path] = file.Kind;
                }
            }
            catch (PatchFormatException)
            {
                // Anzeige bleibt ohne Art; der Broker hat den Patch bereits geprüft.
            }
        }
        Targets = view.Proposal.Targets
            .Select(t => new TargetItemViewModel(t, OnSelectionChanged, kinds.TryGetValue(t.Path, out var k) ? k : null)).ToList();
        VisibleTargets = new ObservableCollection<TargetItemViewModel>(Targets);
        ApproveCommand = new RelayCommand(() => RunBusy(() => _owner.DecideAsync(this, approve: true)), () => CanApprove);
        DenyCommand = new RelayCommand(() => RunBusy(() => _owner.DecideAsync(this, approve: false)), () => !_busy);
        ApplySelectionCommand = new RelayCommand(() => RunBusy(() => _owner.NarrowAsync(this)), () => CanApplySelection);
        SelectAllVisibleCommand = new RelayCommand(() => SetVisible(true), () => IsBatch);
        DeselectAllVisibleCommand = new RelayCommand(() => SetVisible(false), () => IsBatch);
        if (IsWebContent)
        {
            _ = LoadWebAsync();
        }
    }

    // ------------------------------------------------------------------ Web (M6)
    private WebCapture? _capture;
    private string _webText = UiText.Get("WebLoading");

    public bool IsWebFetch => Proposal.Tool == "Web.Fetch";

    public bool IsWebContent => Proposal.Tool == "Web.Content";

    private string Argument(string name) =>
        Proposal.Arguments.ValueKind == JsonValueKind.Object && Proposal.Arguments.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString()! : "";

    /// <summary>Erste Freigabe: genau diese URL mit festen Grenzen.</summary>
    public string WebFetchText
    {
        get
        {
            if (!IsWebFetch)
            {
                return string.Empty;
            }
            var url = Argument("url");
            var host = Uri.TryCreate(url, UriKind.Absolute, out var uri) ? uri.IdnHost : "?";
            return UiText.Format("WebFetchDetails", InertText.Clean(url, 2100), host);
        }
    }

    /// <summary>Zweite Freigabe: exakt dieser bereinigte Text (angezeigt wird, was übergeben wird).</summary>
    public string WebText
    {
        get => _webText;
        private set => Set(ref _webText, value);
    }

    /// <summary>Erste Freigabe: genau diese Anfrage (Anfragezeile, Kopfzeilen, Weg über Proxy oder direkt).</summary>
    public string WebRequestText => IsWebFetch ? WebRequestPreview.RequestText(Argument("url"), _owner.WebRoute) : string.Empty;

    /// <summary>Was die Anfrage an Daten überträgt: Pfadsegmente und Parameter, dekodiert.</summary>
    public string WebDataText => IsWebFetch ? WebRequestPreview.DataText(Argument("url")) : string.Empty;

    public bool HasWebDataWarnings => IsWebFetch && WebRequestPreview.DataWarnings(Argument("url")).Count > 0;

    public string WebDataWarningsText => IsWebFetch
        ? string.Join("\n", WebRequestPreview.DataWarnings(Argument("url")).Select(w => "• " + w))
        : string.Empty;

    /// <summary>Zweite Freigabe: was tatsächlich gesendet wurde und was zurückkam (Status, Weg, Zertifikat, Kopfzeilen).</summary>
    public string WebResponseText
    {
        get
        {
            if (_capture is null)
            {
                return string.Empty;
            }
            var lines = new List<string>
            {
                UiText.Format("WebResponseSummary", _capture.Status, _capture.ContentType, _capture.RawBytes, _capture.Chars),
                UiText.Format("WebResponseRoute", _capture.Proxy is null ? UiText.Get("WebRouteDirect") : UiText.Format("WebRouteProxy", _capture.Proxy)),
                UiText.Format("WebResponseIssuer", string.IsNullOrEmpty(_capture.TlsIssuer) ? "?" : _capture.TlsIssuer),
            };
            if (!string.IsNullOrEmpty(_capture.Request))
            {
                lines.Add("");
                lines.Add(UiText.Get("WebRequestSent"));
                lines.Add(_capture.Request);
            }
            if (_capture.ResponseHeaders is { Count: > 0 } headers)
            {
                lines.Add("");
                lines.Add(UiText.Get("WebResponseHeaders"));
                lines.AddRange(headers);
            }
            return InertText.Clean(string.Join("\n", lines), 12_000);
        }
    }

    public string WebSourceText => _capture is null
        ? string.Empty
        : UiText.Format("WebSourceDetails", InertText.Clean(_capture.Url, 2100), _capture.Address, _capture.FetchedAt, _capture.ContentType,
            _capture.Chars, _capture.Truncated ? UiText.Get("WebTruncated") : "", _capture.TextSha256, Proposal.Agent.ToString());

    public bool HasWebWarnings => _capture?.Warnings.Count > 0;

    public string WebWarningsText => _capture is null
        ? string.Empty
        : string.Join("\n", _capture.Warnings.Select(w => "• " + InertText.Clean(w.Message, 300)
                                                            + (string.IsNullOrEmpty(w.Excerpt) ? "" : $" – „{InertText.Clean(w.Excerpt, 240)}“")));

    /// <summary>Text liegt vor und sein Hash ist genau der des Vorschlags (sonst kein Erlauben möglich).</summary>
    public bool WebReady { get; private set; }

    private async Task LoadWebAsync()
    {
        try
        {
            var capture = await _owner.Api.WebCaptureAsync(Argument("captureId"), CancellationToken.None).ConfigureAwait(false);
            var text = capture?.Text;
            var sha = text is null ? null : Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(text)));
            _owner.Post(() =>
            {
                _capture = capture;
                if (capture is null || text is null)
                {
                    WebText = UiText.Get("WebMissing");
                }
                else if (sha != Argument("textSha256") || capture.TextSha256 != sha)
                {
                    WebText = UiText.Get("WebHashMismatch");
                }
                else
                {
                    WebText = InertText.Clean(text, 250_000);
                    WebReady = true;
                }
                OnPropertyChanged(nameof(WebSourceText));
                OnPropertyChanged(nameof(WebResponseText));
                OnPropertyChanged(nameof(HasWebWarnings));
                OnPropertyChanged(nameof(WebWarningsText));
                OnPropertyChanged(nameof(WebReady));
                RefreshCommands();
            });
        }
        catch (Exception ex) when (ex is not OutOfMemoryException)
        {
            _owner.Post(() => WebText = UiText.Format("WebLoadFailed", ex.Message));
        }
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
    public string PurposeText => InertText.Clean(Proposal.Purpose, 2000);
    public string SnapshotText => Proposal.SnapshotId;
    public string SandboxText => Proposal.SandboxId;
    public string HashText => Sha256;
    public string ParentText => Proposal.ParentProposalId ?? "–";
    public bool HasParent => Proposal.ParentProposalId is not null;
    public string SideEffectsText => _owner.SandboxMode == "test"
        ? UiText.Get("SideEffectsTest")
        : UiText.Get(Proposal.Tool switch
        {
            "Workspace.Snapshot" => "SideEffectsSnapshot",
            "Patch.Apply" => "SideEffectsPatch",
            "Web.Fetch" => "SideEffectsWebFetch",
            "Web.Content" => "SideEffectsWebContent",
            "Workspace.Diff" => "SideEffectsExportPrepare",
            "Workspace.ExportPatch" => "SideEffectsExport",
            "Repo.Index" => "SideEffectsIndex",
            _ when IsToolchain => "SideEffectsToolchain",
            "OsDebug.Start" => "SideEffectsOsDebug",
            "Run.AutoApprove" => "SideEffectsAutoApprove",
            _ => "SideEffectsRead",
        });

    /// <summary>Bestätigung der automatischen Freigabe für einen Run (autoApprove).</summary>
    public bool IsAutoApprove => Proposal.Tool == "Run.AutoApprove";

    /// <summary>Die Tools, die nach der Bestätigung in diesem Run ohne Einzelfreigabe laufen – je Zeile mit Erklärung.</summary>
    public string AutoApproveText
    {
        get
        {
            if (!IsAutoApprove || Proposal.Arguments.ValueKind != JsonValueKind.Object
                || !Proposal.Arguments.TryGetProperty("tools", out var tools) || tools.ValueKind != JsonValueKind.Array)
            {
                return string.Empty;
            }
            return string.Join("\n", tools.EnumerateArray().Select(t =>
            {
                var name = t.ValueKind == JsonValueKind.String ? t.GetString() ?? "?" : "?";
                var key = "AutoTool_" + name.Replace('.', '_');
                return "• " + (UiText.Exists(key) ? UiText.Get(key) : UiText.Format("AutoToolUnknown", InertText.Clean(name, 100)));
            }));
        }
    }

    public bool IsToolchain => Proposal.Tool.StartsWith("Toolchain.", StringComparison.Ordinal);

    /// <summary>Befehlsabschnitt sichtbar: Toolchain-Aufruf oder Debug-Sitzung (M9).</summary>
    public bool HasCommand => IsToolchain || Proposal.Tool == "OsDebug.Start";

    /// <summary>Debug-Sitzung (M9) in Klartext: Abbild, Maschine, Build und die freigegebenen Grenzen.</summary>
    private string OsDebugText()
    {
        var args = Proposal.Arguments;
        var c = args.TryGetProperty("config", out var cfg) && cfg.ValueKind == JsonValueKind.Object ? cfg : default;
        string Get(string name) => c.ValueKind == JsonValueKind.Object && c.TryGetProperty(name, out var v) ? v.ToString() : "?";
        var lines = new List<string>
        {
            UiText.Format("OsDebugImage", Get("image"), Get("media")),
            UiText.Format("OsDebugMachine", Get("machine"), Get("memoryMB"), Get("cpus"), Get("firmware"), Get("accel")),
        };
        if (c.ValueKind == JsonValueKind.Object && c.TryGetProperty("build", out var build))
        {
            lines.Add(UiText.Format("OsDebugBuild", build.TryGetProperty("toolchain", out var t) ? t.ToString() : "?",
                build.TryGetProperty("action", out var a) ? a.ToString() : "?"));
        }
        lines.Add(UiText.Format("OsDebugLimits", args.TryGetProperty("maxCommands", out var n) ? n.ToString() : "?",
            args.TryGetProperty("maxMinutes", out var m) ? m.ToString() : "?"));
        lines.Add(UiText.Get(args.TryGetProperty("allowInput", out var input) && input.ValueKind == JsonValueKind.True
            ? "OsDebugInputAllowed" : "OsDebugInputDenied"));
        return string.Join("\n", lines);
    }

    public bool HasPatch => _patch is not null;

    /// <summary>Vollständiger Patch je Datei, lesbar; abgewählte Dateien sind gekennzeichnet.</summary>
    public string PatchText
    {
        get
        {
            if (_patch is null)
            {
                return string.Empty;
            }
            var parts = new List<string>();
            foreach (var target in Targets)
            {
                var section = PatchHeaders.Section(_patch, target.Path);
                parts.Add(target.IsSelected ? section : $"{target.Path} {UiText.Get("PatchDeselected")}");
            }
            return InertText.Clean(string.Join("\n\n", parts));
        }
    }

    /// <summary>Toolchain-Aufruf in Klartext: Toolchain, Aktion und jeder typisierte Parameter einzeln.</summary>
    public string CommandText
    {
        get
        {
            if (Proposal.Tool == "OsDebug.Start" && Proposal.Arguments.ValueKind == JsonValueKind.Object)
            {
                return OsDebugText();
            }
            if (!IsToolchain || Proposal.Arguments.ValueKind != JsonValueKind.Object)
            {
                return string.Empty;
            }
            var args = Proposal.Arguments;
            var toolchain = args.TryGetProperty("toolchain", out var t) ? t.ToString() : "?";
            var lines = new List<string> { UiText.Format("CommandToolchain", toolchain, Proposal.Tool["Toolchain.".Length..].ToLowerInvariant()) };
            if (args.TryGetProperty("parameters", out var parameters) && parameters.ValueKind == JsonValueKind.Object
                && parameters.EnumerateObject().Any())
            {
                lines.AddRange(parameters.EnumerateObject().Select(p => $"  {p.Name} = {p.Value}"));
            }
            else
            {
                lines.Add("  " + UiText.Get("CommandNoParameters"));
            }
            return string.Join("\n", lines);
        }
    }

    public string ValidUntilText =>
        ContractTime.Parse(Proposal.ExpiresAt).ToLocalTime().ToString("HH:mm:ss", UiText.Culture);

    public string ArgumentsText => JsonSerializer.Serialize(ArgumentsWithoutPatch(), new JsonSerializerOptions
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    });

    /// <summary>Der Patch steht lesbar im eigenen Abschnitt; in den Parametern nur ein Verweis.</summary>
    private object ArgumentsWithoutPatch()
    {
        if (_patch is null)
        {
            return Proposal.Arguments;
        }
        var copy = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (var p in Proposal.Arguments.EnumerateObject())
        {
            copy[p.Name] = p.Name == "patch" ? $"({_patch.Length} Zeichen – siehe Änderungen)" : p.Value;
        }
        return copy;
    }

    public bool IsBatch => NarrowableTools.Contains(Proposal.Tool) && Targets.Count > 1;

    public int TotalCount => Targets.Count;
    public int SelectedCount => Targets.Count(t => t.IsSelected);
    public string CountText => UiText.Format("SelectionCount", SelectedCount, TotalCount);

    /// <summary>Auswahl weicht vom angezeigten Vorschlag ab: Erlauben ist gesperrt, bis sie übernommen wurde.</summary>
    public bool SelectionChanged => SelectedCount != TotalCount;

    public bool CanApprove => !_busy && !SelectionChanged && !_owner.IsStopped && (!IsWebContent || WebReady);
    public bool CanApplySelection => !_busy && IsBatch && SelectionChanged && SelectedCount > 0 && !_owner.IsStopped;

    public string ApproveText => Proposal.Tool switch
    {
        "Web.Content" => UiText.Format("ApproveWebContent", UiText.AgentName(Proposal.Agent.ToString())),
        "Workspace.ExportPatch" => UiText.Get(IsBatch ? "ApproveExportSelection" : "ApproveExport"),
        "Run.AutoApprove" => UiText.Get("ApproveAutoApprove"),
        _ => UiText.Get(IsBatch ? "ApproveSelectionOnce" : "ApproveOnce"),
    };

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
        OnPropertyChanged(nameof(PatchText));
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
    private string? _sandboxDetail;
    private string? _toolchainsEmptyText;
    private Task? _eventLoop;
    private Task? _activityLoop;
    private readonly IBrokerUiApi? _activityApi;
    private readonly ActivityBuffer _activityBuffer;
    private long _lastActivitySeq;
    private AgentViewModel? _selectedAgent;
    private string _chatInput = string.Empty;
    private int _selectedTab;
    private string _orchestratorText = UiText.Get("OrchestratorIdle");
    private bool _isOrchestratorProblem;

    public MainViewModel(IBrokerUiApi api, IBrokerUiApi eventsApi, Action<Action> post, string sessionId, string sandboxMode = "test",
        IBrokerUiApi? activityApi = null, bool throttle = true)
    {
        SandboxMode = sandboxMode;
        _api = api;
        _eventsApi = eventsApi;
        // Eigene Verbindung nötig: zwei lange Abfragen auf einer Verbindung würden sich gegenseitig aufhalten.
        _activityApi = activityApi;
        _post = post;
        StatusText = UiText.Format("Connected", sessionId[..8]);
        Agents = new ObservableCollection<AgentViewModel>(Enum.GetValues<AgentRole>().Select(r => new AgentViewModel(r.ToString(), CancelAgentAsync)));
        Orchestrator = new AgentViewModel("Orchestrator");
        _selectedAgent = Agents[0];
        StartFakeRunCommand = new RelayCommand(StartFakeRunAsync, () => !IsStopped);
        EmergencyStopCommand = new RelayCommand(EmergencyStopAsync);
        ReleaseCommand = new RelayCommand(ReleaseAsync, () => IsStopped);
        ScanToolchainsCommand = new RelayCommand(ScanToolchainsAsync);
        SendChatCommand = new RelayCommand(SendChatAsync, () => CanSendChat);
        CancelRunCommand = new RelayCommand(CancelRunAsync);
        StopOrchestratorCommand = new RelayCommand(StopOrchestratorAsync);
        RebuildSandboxCommand = new RelayCommand(RebuildSandboxAsync, () => IsHyperV);
        ContinueSandboxCommand = new RelayCommand(ContinueSandboxAsync, () => IsSandboxPaused);
        RefreshManifestCommand = new RelayCommand(RefreshManifestAsync);
        VerifyAuditCommand = new RelayCommand(VerifyAuditAsync);
        ExportDiagnosticsCommand = new RelayCommand(ExportDiagnosticsAsync);
        CreateBackupCommand = new RelayCommand(CreateBackupAsync);
        _activityBuffer = new ActivityBuffer(ApplyActivity, post, useTimer: throttle);
    }

    // ------------------------------------------------------------------ System (M7): Manifest, Audit, Diagnose, Sicherung

    private string _manifestText = UiText.Get("SystemNotLoaded");
    private string _manifestProblems = "";
    private string _systemResultText = "";
    private bool _includeLogs;

    public RelayCommand RefreshManifestCommand { get; }
    public RelayCommand VerifyAuditCommand { get; }
    public RelayCommand ExportDiagnosticsCommand { get; }
    public RelayCommand CreateBackupCommand { get; }

    public string ManifestText
    {
        get => _manifestText;
        private set => Set(ref _manifestText, value);
    }

    /// <summary>Fehlende Offline-Voraussetzungen (rot); leer = vollständig.</summary>
    public string ManifestProblemsText
    {
        get => _manifestProblems;
        private set => Set(ref _manifestProblems, value);
    }

    public string SystemResultText
    {
        get => _systemResultText;
        private set => Set(ref _systemResultText, value);
    }

    /// <summary>Protokolle ins Diagnosepaket aufnehmen (können Pfade und Codeausschnitte enthalten).</summary>
    public bool IncludeLogs
    {
        get => _includeLogs;
        set => Set(ref _includeLogs, value);
    }

    private Task RefreshManifestAsync() => GuardAsync(async () =>
    {
        var manifest = await _api.ManifestAsync(_shutdown.Token).ConfigureAwait(false);
        var (text, problems) = SystemReport.FormatManifest(manifest);
        Post(() =>
        {
            ManifestText = text;
            ManifestProblemsText = problems;
        });
    });

    private Task VerifyAuditAsync() => GuardAsync(async () =>
    {
        var report = await _api.AuditVerifyAsync(_shutdown.Token).ConfigureAwait(false);
        var text = SystemReport.FormatAudit(report);
        Post(() => SystemResultText = text);
    });

    private Task ExportDiagnosticsAsync() => GuardAsync(async () =>
    {
        var result = await _api.DiagnosticsExportAsync(IncludeLogs, _shutdown.Token).ConfigureAwait(false);
        var text = UiText.Format(IncludeLogs ? "DiagnosticsDoneWithLogs" : "DiagnosticsDone", SystemReport.PathOf(result));
        Post(() => SystemResultText = text);
    });

    private Task CreateBackupAsync() => GuardAsync(async () =>
    {
        var result = await _api.BackupCreateAsync(_shutdown.Token).ConfigureAwait(false);
        var text = UiText.Format("BackupDone", SystemReport.PathOf(result));
        Post(() => SystemResultText = text);
    });

    // ------------------------------------------------------------------ Chat und Live-Aktivität (M3)

    public ObservableCollection<ChatEntry> Chat { get; } = [];
    public bool HasChat => Chat.Count > 0;
    public AgentViewModel Orchestrator { get; }
    public RelayCommand SendChatCommand { get; }
    public RelayCommand CancelRunCommand { get; }
    public RelayCommand StopOrchestratorCommand { get; }
    public RelayCommand RebuildSandboxCommand { get; }

    public RelayCommand ContinueSandboxCommand { get; }

    /// <summary>VM nach ausgebliebener Antwort angehalten: „VM weiter verwenden“ sichtbar.</summary>
    public bool IsSandboxPaused => SandboxParts(SandboxDetail).Any(p => p.StartsWith("angehalten", StringComparison.Ordinal));

    /// <summary>
    /// Zustand je VM: bei einer VM der ganze Text, bei mehreren (M8) „Windows: … | Linux: …“ ohne das Präfix des Gasts.
    /// </summary>
    public static IReadOnlyList<string> SandboxParts(string? detail)
    {
        if (string.IsNullOrEmpty(detail))
        {
            return [];
        }
        return detail.Split(" | ").Select(part =>
        {
            foreach (var prefix in new[] { "Windows: ", "Linux: " })
            {
                if (part.StartsWith(prefix, StringComparison.Ordinal))
                {
                    return part[prefix.Length..];
                }
            }
            return part;
        }).ToList();
    }

    private async Task ContinueSandboxAsync()
    {
        await GuardAsync(async () =>
        {
            await _api.ContinueSandboxAsync(_shutdown.Token).ConfigureAwait(false);
            Post(() => InfoText = UiText.Get("SandboxContinueInfo"));
        }).ConfigureAwait(false);
    }

    /// <summary>Betriebsart mit Offline-VM: Knopf „VM neu aufbauen“ sichtbar.</summary>
    public bool IsHyperV => SandboxMode is "hyperv" or "qemu";

    /// <summary>Rückfrage vor dem Neuaufbau (dauert Minuten). Die Oberfläche setzt hier einen Dialog ein.</summary>
    public Func<bool> ConfirmRebuild { get; set; } = () => false;

    /// <summary>Asynchrone Rückfrage (Avalonia unter Linux, M10); hat Vorrang vor <see cref="ConfirmRebuild"/>.</summary>
    public Func<Task<bool>>? ConfirmRebuildAsync { get; set; }

    /// <summary>Weg der Webabrufe laut Broker (direkt oder über den konfigurierten Proxy).</summary>
    public string? WebRoute { get; private set; }

    private async Task RebuildSandboxAsync()
    {
        if (!(ConfirmRebuildAsync is { } ask ? await ask().ConfigureAwait(true) : ConfirmRebuild()))
        {
            return;
        }
        await GuardAsync(async () =>
        {
            await _api.RebuildSandboxAsync(_shutdown.Token).ConfigureAwait(false);
            Post(() => InfoText = UiText.Get("SandboxRebuildInfo"));
        }).ConfigureAwait(false);
    }

    /// <summary>Für Tests: gepufferte Aktivität sofort anwenden.</summary>
    public ActivityBuffer ActivityBuffer => _activityBuffer;

    public AgentViewModel? SelectedAgent
    {
        get => _selectedAgent;
        set
        {
            if (Set(ref _selectedAgent, value) && value is not null)
            {
                SelectedTab = 1;
            }
        }
    }

    /// <summary>0 Chat, 1 Aktivität, 2 Ereignisse, 3 Toolchains.</summary>
    public int SelectedTab
    {
        get => _selectedTab;
        set => Set(ref _selectedTab, value);
    }

    public string ChatInput
    {
        get => _chatInput;
        set
        {
            if (Set(ref _chatInput, value))
            {
                OnPropertyChanged(nameof(CanSendChat));
                SendChatCommand.Refresh();
            }
        }
    }

    public bool CanSendChat => !IsStopped && !string.IsNullOrWhiteSpace(_chatInput) && _chatInput.Length <= 16_000;

    public string OrchestratorText
    {
        get => _orchestratorText;
        private set => Set(ref _orchestratorText, value);
    }

    /// <summary>Orchestrator hängt oder ist abgestürzt: Hinweis mit Knopf „Orchestrator beenden“.</summary>
    public bool IsOrchestratorProblem
    {
        get => _isOrchestratorProblem;
        private set => Set(ref _isOrchestratorProblem, value);
    }

    /// <summary>
    /// Läuft auf dem UI-Thread (Befehl): Text sofort übernehmen, nicht erst über <see cref="Post"/> –
    /// in der echten Oberfläche ist Post asynchron (Dispatcher.BeginInvoke).
    /// </summary>
    private Task SendChatAsync()
    {
        var text = ChatInput.Trim();
        if (text.Length == 0)
        {
            return Task.CompletedTask;
        }
        ChatInput = string.Empty;
        return GuardAsync(async () =>
        {
            try
            {
                await _api.ChatSendAsync(text, _shutdown.Token).ConfigureAwait(false);
            }
            catch
            {
                // Nicht gesendet: Text zurück ins Eingabefeld, damit nichts verloren geht.
                Post(() =>
                {
                    if (ChatInput.Length == 0)
                    {
                        ChatInput = text;
                    }
                });
                throw;
            }
        });
    }

    private Task CancelAgentAsync(AgentViewModel agent) => GuardAsync(async () =>
    {
        await _api.AgentCancelAsync(agent.Name, _shutdown.Token).ConfigureAwait(false);
        Post(() => InfoText = UiText.Format("AgentCancelledInfo", agent.Name));
    });

    private Task CancelRunAsync() => GuardAsync(async () =>
    {
        await _api.RunCancelAsync(_shutdown.Token).ConfigureAwait(false);
        Post(() => InfoText = UiText.Get("RunCancelledInfo"));
    });

    private Task StopOrchestratorAsync() => GuardAsync(async () =>
    {
        await _api.OrchestratorStopAsync(_shutdown.Token).ConfigureAwait(false);
        Post(() => InfoText = UiText.Get("OrchestratorStoppedInfo"));
    });

    private async Task ActivityLoopAsync()
    {
        while (!_shutdown.IsCancellationRequested)
        {
            IReadOnlyList<ActivityEvent> events;
            try
            {
                events = await _activityApi!.WaitActivityAsync(_lastActivitySeq, 15_000, _shutdown.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception ex)
            {
                Post(() => ErrorText = UiText.Format("ConnectFailed", ex.Message));
                try
                {
                    await Task.Delay(2000, _shutdown.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                continue;
            }
            if (events.Count == 0)
            {
                continue;
            }
            _lastActivitySeq = events[^1].Seq;
            _activityBuffer.Enqueue(events);
        }
    }

    /// <summary>Wendet einen Stapel Aktivität an (UI-Thread). Texte werden nur einmal pro Stapel veröffentlicht.</summary>
    internal void ApplyActivity(IReadOnlyList<ActivityEvent> events)
    {
        var touched = new HashSet<AgentViewModel>();
        foreach (var e in events)
        {
            var time = ContractTime.Parse(e.At).ToLocalTime().ToString("HH:mm:ss", UiText.Culture);
            if (e.Kind == ActivityKind.Chat && e.Text is not null)
            {
                Chat.Add(new ChatEntry(time, UiText.AgentName(e.Agent), e.Text, e.Agent == "User"));
                while (Chat.Count > 1_000)
                {
                    Chat.RemoveAt(0);
                }
            }
            var agent = e.Agent == "Orchestrator" ? Orchestrator : Agents.FirstOrDefault(a => a.Name == e.Agent);
            if (agent is null)
            {
                continue;
            }
            agent.Apply(e, time);
            touched.Add(agent);
        }
        foreach (var agent in touched)
        {
            agent.Publish();
        }
        OnPropertyChanged(nameof(HasChat));
    }

    public ObservableCollection<ToolchainItemViewModel> Toolchains { get; } = [];

    public RelayCommand ScanToolchainsCommand { get; }

    /// <summary>Bestätigung für proprietäre Toolchains (Lizenz). Die Oberfläche setzt hier einen Dialog ein.</summary>
    public Func<ToolchainInfo, bool> ConfirmLicense { get; set; } = _ => false;

    /// <summary>Asynchrone Lizenzbestätigung (Avalonia unter Linux, M10); hat Vorrang vor <see cref="ConfirmLicense"/>.</summary>
    public Func<ToolchainInfo, Task<bool>>? ConfirmLicenseAsync { get; set; }

    public string? ToolchainsEmptyText
    {
        get => _toolchainsEmptyText;
        private set => Set(ref _toolchainsEmptyText, value);
    }

    public Task ScanToolchainsAsync() => GuardAsync(async () =>
    {
        var list = await _api.ScanToolchainsAsync(_shutdown.Token).ConfigureAwait(false);
        Post(() =>
        {
            Toolchains.Clear();
            foreach (var info in list)
            {
                Toolchains.Add(new ToolchainItemViewModel(info, this));
            }
            ToolchainsEmptyText = list.Count == 0 ? UiText.Get("NoToolchains") : null;
        });
    });

    internal async Task RegisterToolchainAsync(ToolchainItemViewModel item)
    {
        if (item.Info.Proprietary && !(ConfirmLicenseAsync is { } ask ? await ask(item.Info).ConfigureAwait(true) : ConfirmLicense(item.Info)))
        {
            return;
        }
        await GuardAsync(async () =>
        {
            await _api.RegisterToolchainAsync(item.Info.Directory, item.Info.Digest, _shutdown.Token).ConfigureAwait(false);
            Post(() => InfoText = UiText.Format("ToolchainRegisteredInfo", item.TitleText));
        }).ConfigureAwait(false);
        await ScanToolchainsAsync().ConfigureAwait(false);
    }

    internal async Task UnregisterToolchainAsync(ToolchainItemViewModel item)
    {
        await GuardAsync(() => _api.UnregisterToolchainAsync(item.Name, _shutdown.Token)).ConfigureAwait(false);
        await ScanToolchainsAsync().ConfigureAwait(false);
    }

    public ObservableCollection<AgentViewModel> Agents { get; }
    public ObservableCollection<ApprovalCardViewModel> Approvals { get; } = [];
    public ObservableCollection<EventLine> EventLog { get; } = [];

    public RelayCommand StartFakeRunCommand { get; }
    public RelayCommand EmergencyStopCommand { get; }
    public RelayCommand ReleaseCommand { get; }

    public string SandboxMode { get; }
    public string SandboxText
    {
        get
        {
            var text = UiText.Exists($"Sandbox_{SandboxMode}") ? UiText.Get($"Sandbox_{SandboxMode}") : SandboxMode;
            return SandboxDetail is null ? text : $"{text} – {SandboxDetail}";
        }
    }

    /// <summary>Zustand der Offline-VM (Aufbau, bereit, gesperrt) aus dem Broker.</summary>
    public string? SandboxDetail
    {
        get => _sandboxDetail;
        private set
        {
            if (Set(ref _sandboxDetail, value))
            {
                OnPropertyChanged(nameof(SandboxText));
                OnPropertyChanged(nameof(IsSandboxProblem));
                OnPropertyChanged(nameof(IsSandboxPaused));
                ContinueSandboxCommand?.Refresh();
            }
        }
    }

    /// <summary>VM gesperrt: Kopfzeile rot.</summary>
    public bool IsSandboxProblem => SandboxParts(SandboxDetail).Any(p => p.StartsWith("gesperrt", StringComparison.Ordinal)) || IsSandboxPaused;

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
                OnPropertyChanged(nameof(CanSendChat));
                SendChatCommand.Refresh();
                foreach (var toolchain in Toolchains)
                {
                    toolchain.RefreshCommands();
                }
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
            var status = await _api.StatusAsync(_shutdown.Token).ConfigureAwait(false);
            if (status.TryGetProperty("sandbox", out var sandbox) && sandbox.ValueKind == JsonValueKind.String)
            {
                var detail = sandbox.GetString();
                Post(() => SandboxDetail ??= detail);
            }
            if (status.TryGetProperty("web", out var web) && web.ValueKind == JsonValueKind.Object
                && web.TryGetProperty("route", out var route) && route.ValueKind == JsonValueKind.String)
            {
                var routeText = route.GetString();
                Post(() => WebRoute = routeText);
            }
        }).ConfigureAwait(false);
        await RefreshPendingAsync().ConfigureAwait(false);
        _eventLoop = Task.Run(EventLoopAsync);
        if (_activityApi is not null)
        {
            _activityLoop = Task.Run(ActivityLoopAsync);
        }
        // Hashes großer Toolchains zu berechnen dauert: im Hintergrund, die Oberfläche ist sofort bedienbar.
        _ = Task.Run(ScanToolchainsAsync);
        _ = Task.Run(RefreshManifestAsync);
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
            else if (e.Type == "sandbox.status" && e.Message is not null)
            {
                SandboxDetail = e.Message;
            }
            else if (e.Type == "broker.started" && e.Message is not null)
            {
                IsStopped = true;
            }
            else if (e.Type.StartsWith("orchestrator.", StringComparison.Ordinal) && e.Message is not null)
            {
                OrchestratorText = e.Message;
                IsOrchestratorProblem = e.Type is "orchestrator.unresponsive"
                    || (e.Type == "orchestrator.exited" && e.Message.Contains("unerwartet", StringComparison.Ordinal));
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

    internal void Post(Action action) => _post(action);

    internal IBrokerUiApi Api => _api;

    public async ValueTask DisposeAsync()
    {
        await _shutdown.CancelAsync().ConfigureAwait(false);
        _activityBuffer.Dispose();
        foreach (var loop in new[] { _eventLoop, _activityLoop })
        {
            if (loop is null)
            {
                continue;
            }
            try
            {
                // Begrenzt warten: eine hängende Pipe-Leseoperation darf das Beenden nicht blockieren.
                await loop.WaitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is OperationCanceledException or TimeoutException)
            {
            }
        }
    }
}
