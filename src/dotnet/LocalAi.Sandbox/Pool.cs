using System.Collections.Concurrent;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Sandbox;

/// <summary>
/// Mehrere VMs einer Sitzung (M8): Toolchain-Aktionen laufen in der VM ihres Gastsystems, alles andere (Snapshot,
/// Lesen, Patch, Index, Diff) in der Heimat-VM. Braucht eine Toolchain-Aktion einen Snapshot, den ihre VM noch nicht
/// hat, wird er abgeglichen: das schon übertragene Archiv des Basis-Snapshots und die bereits ausgeführten Patch.Apply-
/// Vorschläge der Kette werden als signierte Jobs wiederholt; die Snapshot-IDs (Inhaltshashes) müssen übereinstimmen.
/// Der Abgleich führt keinen Projektcode aus und bringt nichts Neues in die VM – nur Stände, die der Nutzer schon
/// freigegeben hat.
/// </summary>
public sealed class MultiGuestToolAdapter : IToolAdapter, IDisposable
{
    private abstract record Recipe(string GrantId);

    private sealed record BaseRecipe(string GrantId, ToolProposal Proposal, SnapshotManifest Manifest, string Archive) : Recipe(GrantId);

    private sealed record DerivedRecipe(string GrantId, string Parent, ToolProposal Proposal) : Recipe(GrantId);

    private readonly GuestPlan _plan;
    private readonly IReadOnlyDictionary<GuestOs, SandboxToolAdapter> _adapters;
    private readonly string _retainDirectory;
    private readonly ConcurrentDictionary<string, Recipe> _recipes = new(StringComparer.Ordinal);
    private readonly Dictionary<GuestOs, HashSet<string>> _present = [];
    private readonly Dictionary<GuestOs, SemaphoreSlim> _sync = [];
    private readonly Lock _gate = new();

    /// <summary>Meldung für das Aktivitätsprotokoll (z. B. „Snapshot … in die Linux-VM übertragen“).</summary>
    public Action<string>? Info { get; set; }

    /// <param name="adapters">Ein Adapter je VM; der der Heimat-VM muss <see cref="RetainSnapshot"/> als RetainSnapshot verwenden.</param>
    public MultiGuestToolAdapter(GuestPlan plan, IReadOnlyDictionary<GuestOs, SandboxToolAdapter> adapters, string retainDirectory)
    {
        if (!adapters.ContainsKey(plan.Home))
        {
            throw new ArgumentException("Adapter der Heimat-VM fehlt", nameof(adapters));
        }
        _plan = plan;
        _adapters = adapters;
        _retainDirectory = retainDirectory;
        foreach (var guest in adapters.Keys)
        {
            _present[guest] = new HashSet<string>(StringComparer.Ordinal);
            _sync[guest] = new SemaphoreSlim(1, 1);
        }
        if (Directory.Exists(retainDirectory))
        {
            FileTools.DeleteTree(retainDirectory);
        }
    }

    /// <summary>Für den Adapter der Heimat-VM: Archiv importierter Snapshots aufbewahren (nur solange die Sitzung läuft).</summary>
    public void RetainSnapshot(ToolProposal proposal, SnapshotManifest manifest, string archive)
    {
        if (_adapters.Count < 2 || _recipes.ContainsKey(manifest.SnapshotId))
        {
            return;
        }
        Directory.CreateDirectory(_retainDirectory);
        var target = Path.Combine(_retainDirectory, manifest.SnapshotId + ".zip");
        File.Copy(archive, target, overwrite: true);
        _recipes[manifest.SnapshotId] = new BaseRecipe(manifest.GrantId, proposal, manifest, target);
    }

    /// <summary>VM, in der ein Vorschlag läuft.</summary>
    public GuestOs GuestFor(ToolProposal proposal)
    {
        if (ToolchainTools.Actions.ContainsKey(proposal.Tool) && proposal.Arguments.ValueKind == JsonValueKind.Object
            && proposal.Arguments.TryGetProperty("toolchain", out var name) && name.ValueKind == JsonValueKind.String)
        {
            var guest = _plan.GuestOf(name.GetString()!);
            return _adapters.ContainsKey(guest) ? guest : _plan.Home;
        }
        return _plan.Home;
    }

    public bool IsPresent(GuestOs guest, string snapshotId)
    {
        lock (_gate)
        {
            return _present.TryGetValue(guest, out var set) && set.Contains(snapshotId);
        }
    }

    private void MarkPresent(GuestOs guest, string snapshotId)
    {
        lock (_gate)
        {
            _present[guest].Add(snapshotId);
        }
    }

    public async Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken)
    {
        var guest = GuestFor(proposal);
        if (guest != _plan.Home && ProposalHashing.IsSha256Hex(proposal.SnapshotId) && !IsPresent(guest, proposal.SnapshotId))
        {
            var problem = await ReplicateAsync(guest, proposal.SnapshotId, cancellationToken).ConfigureAwait(false);
            if (problem is not null)
            {
                var message = $"Snapshot konnte nicht in die {GuestSelection.Name(guest)}-VM übertragen werden: {problem}";
                return new ToolResult(invocationId, proposal.ProposalId, ResultStatus.Failed, proposal.SnapshotId,
                    [new TargetResult("*", ResultStatus.Failed, Message: message)], CanonicalJson.Sha256Hex(message), Truncated: false, Untrusted: true);
            }
        }
        var result = await _adapters[guest].ExecuteAsync(proposal, invocationId, cancellationToken).ConfigureAwait(false);
        if (result.Status == ResultStatus.Succeeded)
        {
            Record(guest, proposal, result);
        }
        return result;
    }

    private void Record(GuestOs guest, ToolProposal proposal, ToolResult result)
    {
        var id = result.TargetResults.FirstOrDefault(t => t.TargetId == "snapshot")?.Text;
        if (id is null || !ProposalHashing.IsSha256Hex(id))
        {
            return;
        }
        if (proposal.Tool == "Workspace.Snapshot")
        {
            MarkPresent(guest, id);
        }
        else if (proposal.Tool == "Patch.Apply")
        {
            MarkPresent(guest, id);
            if (id != proposal.SnapshotId && proposal.Arguments.TryGetProperty("grantId", out var g) && g.GetString() is { } grantId)
            {
                _recipes.TryAdd(id, new DerivedRecipe(grantId, proposal.SnapshotId, proposal));
            }
        }
    }

    /// <summary>Bringt <paramref name="snapshotId"/> samt Kette in die VM <paramref name="guest"/>. Rückgabe: null = erledigt.</summary>
    private async Task<string?> ReplicateAsync(GuestOs guest, string snapshotId, CancellationToken ct)
    {
        await _sync[guest].WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var chain = new List<(string Id, Recipe Recipe)>();
            var current = snapshotId;
            for (var i = 0; i < 10_000 && !IsPresent(guest, current); i++)
            {
                if (!_recipes.TryGetValue(current, out var recipe))
                {
                    return $"Stand {current[..12]}… ist in dieser Sitzung nicht über die Heimat-VM entstanden (neuen Snapshot anlegen)";
                }
                chain.Add((current, recipe));
                if (recipe is BaseRecipe)
                {
                    break;
                }
                current = ((DerivedRecipe)recipe).Parent;
            }
            chain.Reverse();
            var adapter = _adapters[guest];
            foreach (var (id, recipe) in chain)
            {
                ToolResult result;
                if (recipe is BaseRecipe b)
                {
                    if (!File.Exists(b.Archive))
                    {
                        return "aufbewahrtes Snapshot-Archiv fehlt";
                    }
                    result = await adapter.ImportSnapshotAsync(b.Proposal, b.Manifest, b.Archive, ct).ConfigureAwait(false);
                }
                else
                {
                    result = await adapter.ExecuteAsync(((DerivedRecipe)recipe).Proposal, "abgleich-" + Guid.NewGuid().ToString("D"), ct).ConfigureAwait(false);
                }
                var reported = result.TargetResults.FirstOrDefault(t => t.TargetId == "snapshot")?.Text;
                if (result.Status != ResultStatus.Succeeded)
                {
                    return GuestReport.Clip(result.TargetResults.FirstOrDefault()?.Message ?? "kein Ergebnis", 300);
                }
                if (reported != id)
                {
                    return $"VM meldet Stand {GuestReport.Clip(reported ?? "-", 16)} statt {id[..12]}…";
                }
                MarkPresent(guest, id);
            }
            Info?.Invoke($"Snapshot {snapshotId[..12]}… in die {GuestSelection.Name(guest)}-VM übertragen ({chain.Count} Schritt(e))");
            return null;
        }
        finally
        {
            _sync[guest].Release();
        }
    }

    public void Dispose()
    {
        foreach (var s in _sync.Values)
        {
            s.Dispose();
        }
        try
        {
            FileTools.DeleteTree(_retainDirectory);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}

/// <summary>Die VMs einer Sitzung für Oberfläche und Programmende: Zustand, Neuaufbau, Weiterverwenden, Not-Aus.</summary>
public sealed class SandboxPool : IAsyncDisposable
{
    private readonly IReadOnlyDictionary<GuestOs, HyperVSandbox> _vms;

    public SandboxPool(IReadOnlyDictionary<GuestOs, HyperVSandbox> vms)
    {
        if (vms.Count == 0)
        {
            throw new ArgumentException("Keine VM", nameof(vms));
        }
        _vms = vms;
        foreach (var vm in vms.Values)
        {
            vm.StatusChanged += _ => StatusChanged?.Invoke(StatusText);
        }
    }

    public IReadOnlyDictionary<GuestOs, HyperVSandbox> Vms => _vms;

    /// <summary>Hinweise der Gastauswahl (z. B. Toolchain läuft laut Manifest in der anderen VM).</summary>
    public IReadOnlyList<string> Notes { get; init; } = [];

    /// <summary>Gesamttext: bei einer VM deren Text, sonst „Windows: … | Linux: …“.</summary>
    public event Action<string>? StatusChanged;

    public const string Separator = " | ";

    public string StatusText => _vms.Count == 1
        ? _vms.Values.First().Status.Text
        : string.Join(Separator, _vms.Select(kv => $"{GuestSelection.Name(kv.Key)}: {kv.Value.Status.Text}"));

    public void Start()
    {
        foreach (var vm in _vms.Values)
        {
            vm.Start();
        }
    }

    /// <summary>„VM neu aufbauen“: gesperrte oder angehaltene VMs; ist keine davon betroffen, alle.</summary>
    public async Task RebuildAsync()
    {
        var troubled = _vms.Values.Where(v => v.Status.State is SandboxState.Locked or SandboxState.Paused).ToList();
        foreach (var vm in troubled.Count > 0 ? troubled : _vms.Values.ToList())
        {
            await vm.RebuildAsync().ConfigureAwait(false);
        }
    }

    public async Task<string> ContinueAsync()
    {
        var texts = new List<string>();
        foreach (var (guest, vm) in _vms)
        {
            if (vm.Status.State == SandboxState.Paused)
            {
                texts.Add($"{GuestSelection.Name(guest)}: {await vm.ContinueAsync().ConfigureAwait(false)}");
            }
        }
        return texts.Count == 0 ? StatusText : string.Join(Separator, texts);
    }

    public async Task<string> EmergencyStopAsync()
    {
        var results = await Task.WhenAll(_vms.Select(async kv => (kv.Key, Text: await kv.Value.EmergencyStopAsync().ConfigureAwait(false))))
            .ConfigureAwait(false);
        return _vms.Count == 1 ? results[0].Text : string.Join(Separator, results.Select(r => $"{GuestSelection.Name(r.Key)}: {r.Text}"));
    }

    public async ValueTask DisposeAsync()
    {
        // Parallel: beide VMs gleichzeitig herunterfahren bzw. entfernen.
        await Task.WhenAll(_vms.Values.Select(v => v.DisposeAsync().AsTask())).ConfigureAwait(false);
    }
}
