using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using LocalAi.Contracts;

namespace LocalAi.Sandbox;

/// <summary>Einstellungen für die Offline-VM (config/localai.json → sandbox.hyperv).</summary>
public sealed record HyperVSettings
{
    /// <summary>Windows-ISO oder direkt eine install.wim/install.esd.</summary>
    public required string ImagePath { get; init; }

    /// <summary>Name der Edition im Abbild, z. B. „Windows 11 Pro“; alternativ <see cref="ImageIndex"/>.</summary>
    public string? ImageName { get; init; }

    public int? ImageIndex { get; init; }

    public string VmName { get; init; } = "LocalAi-Sandbox";

    /// <summary>Ordner für VM-Konfiguration und virtuelle Festplatten (wird bei jedem Aufbau geleert).</summary>
    public required string VmDirectory { get; init; }

    public int MemoryMB { get; init; } = 8192;

    public int Processors { get; init; } = 4;

    public int OsDiskGB { get; init; } = 128;

    public int TransferDiskMB { get; init; } = 64;

    public TimeSpan BuildTimeout { get; init; } = TimeSpan.FromMinutes(90);

    /// <summary>Optionaler Produktschlüssel für das Gast-Windows (Evaluierungsversionen brauchen keinen).</summary>
    public string? ProductKey { get; init; }

    /// <summary>Beibehalten statt löschen, wenn die Sitzung endet (nur zur Fehlersuche).</summary>
    public bool KeepVmOnExit { get; init; }

    /// <summary>
    /// VM beim Programmende speichern statt löschen und beim nächsten Start fortsetzen, wenn sie zum aktuellen Stand passt
    /// (gleicher Fingerabdruck aus Abbild, Einstellungen, Worker und Toolchains) und die letzte Sitzung sauber endete.
    /// Abweichung von R17 (Neuaufbau je Sitzung) auf ausdrücklichen Wunsch; „VM neu aufbauen“ erzwingt den Neuaufbau.
    /// </summary>
    public bool ReuseVm { get; init; }

    /// <summary>
    /// Werkzeugprozesse im Gast mit eingeschränktem Token auf niedriger Integritätsstufe starten (SEC-21, Standard).
    /// Nur zur Fehlersuche abschaltbar – dann kann bösartiger Build-/Testcode den Worker in der VM verändern.
    /// </summary>
    public bool LowIntegrityTools { get; init; } = true;

    /// <summary>Wartezeit auf die Antwort eines normalen Jobs (Lesen, Patch, Index).</summary>
    public int JobTimeoutMinutes { get; init; } = 5;

    /// <summary>Wartezeit auf die Übernahme eines Snapshots (große Projekte).</summary>
    public int SnapshotTimeoutMinutes { get; init; } = 30;

    /// <summary>Zuschlag auf timeoutSeconds einer Toolchain-Aktion (Kopie, Übertragung).</summary>
    public int ToolchainExtraMinutes { get; init; } = 5;

    /// <summary>
    /// Bleibt eine Antwort aus: VM anhalten statt sperren. Die Oberfläche bietet dann „VM weiter verwenden“ oder
    /// „VM neu aufbauen“ an; die VM bleibt für reuseVm erhalten. false = sofort sperren (Neuaufbau nötig).
    /// Netzwerkkarten oder ungültige Rückgaben sperren immer.
    /// </summary>
    public bool PauseOnNoAnswer { get; init; } = true;

    public long TransferDiskBytes => TransferDiskMB * 1024L * 1024L;

    private static readonly Regex VmNamePattern = new("^[A-Za-z0-9-]{1,40}$");

    public void Validate()
    {
        if (!VmNamePattern.IsMatch(VmName))
        {
            throw new InvalidDataException("sandbox.hyperv.vmName: nur Buchstaben, Ziffern und '-' (höchstens 40 Zeichen)");
        }
        if (ImageName is null == ImageIndex is null)
        {
            throw new InvalidDataException("sandbox.hyperv: genau eines von imageName oder imageIndex angeben");
        }
        if (MemoryMB is < 2048 or > 262144 || Processors is < 1 or > 64 || OsDiskGB is < 40 or > 2048
            || TransferDiskMB is < 32 or > 4096)
        {
            throw new InvalidDataException("sandbox.hyperv: memoryMB, processors, osDiskGB oder transferDiskMB außerhalb der Grenzen");
        }
        if (ProductKey is not null && !Regex.IsMatch(ProductKey, "^[A-Z0-9]{5}(-[A-Z0-9]{5}){4}$"))
        {
            throw new InvalidDataException("sandbox.hyperv.productKey hat nicht das Format XXXXX-XXXXX-XXXXX-XXXXX-XXXXX");
        }
    }
}

public enum SandboxState
{
    Preparing,
    Building,
    WaitingForGuest,
    SelfTest,
    Ready,
    Locked,
    Removed,

    /// <summary>Angehalten nach ausgebliebener Antwort; der Nutzer entscheidet: weiter verwenden oder neu aufbauen.</summary>
    Paused,
}

public sealed record SandboxStatus(SandboxState State, string Text)
{
    public bool IsReady => State == SandboxState.Ready;
}

/// <summary>Letztes Signal des Gasts (KVP) mit Warten auf einen erwarteten Wert.</summary>
public sealed class SignalBoard
{
    private static readonly Regex Valid = new(@"^(result|report):\d{1,18}$", RegexOptions.CultureInvariant);
    private static readonly Regex ProgressToken = new("^[a-z0-9-]{1,32}$", RegexOptions.CultureInvariant);
    private static readonly Regex HeartbeatToken = new("^[A-Za-z]{1,40}$", RegexOptions.CultureInvariant);
    private readonly Lock _gate = new();
    private string? _latest;
    private string? _progress;
    private string? _heartbeat;

    /// <summary>Letzter Fortschritt der Provisionierung im Gast (festes Wortformat).</summary>
    public string? Progress
    {
        get { lock (_gate) { return _progress; } }
    }

    /// <summary>Hyper-V-Heartbeat des Gasts (vom Host gelesen).</summary>
    public string? Heartbeat
    {
        get { lock (_gate) { return _heartbeat; } }
    }

    public bool PostProgress(string value)
    {
        if (!ProgressToken.IsMatch(value))
        {
            return false;
        }
        lock (_gate)
        {
            _progress = value;
        }
        return true;
    }

    public bool PostHeartbeat(string value)
    {
        if (!HeartbeatToken.IsMatch(value))
        {
            return false;
        }
        lock (_gate)
        {
            _heartbeat = value;
        }
        return true;
    }

    /// <summary>Deutscher Text zum Stand im Gast, z. B. für die Kopfzeile.</summary>
    public string DescribeGuest()
    {
        var progress = Progress switch
        {
            null => "noch keine Meldung aus dem Gast (Windows-Einrichtung läuft)",
            "setupcomplete" => "Windows eingerichtet, Provisionierung startet",
            "start" => "Provisionierung läuft",
            "network" => "Netzwerkprüfungen",
            "toolchains" => "Toolchains werden installiert",
            "worker" => "Worker wird eingerichtet",
            "report" => "Bericht wird übergeben",
            "report-sent" or "report-sent-ps" => "Bericht übergeben",
            "report-failed" => "Bericht konnte nicht übergeben werden",
            var other => other,
        };
        var heartbeat = Heartbeat switch
        {
            null => "unbekannt",
            "NoContact" => "kein Kontakt",
            "LostCommunication" => "Verbindung verloren",
            var h when h.StartsWith("Ok", StringComparison.Ordinal) => "Windows läuft",
            var h => h,
        };
        return $"{progress}; Heartbeat: {heartbeat}";
    }
    private TaskCompletionSource _changed = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public string? Latest
    {
        get { lock (_gate) { return _latest; } }
    }

    /// <summary>Nimmt einen Wert aus dem Gast an; alles außerhalb des festen Formats wird verworfen.</summary>
    public bool Post(string value)
    {
        if (!Valid.IsMatch(value))
        {
            return false;
        }
        TaskCompletionSource previous;
        lock (_gate)
        {
            _latest = value;
            previous = _changed;
            _changed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        }
        previous.TrySetResult();
        return true;
    }

    public async Task<bool> WaitForAsync(string expected, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            Task changed;
            lock (_gate)
            {
                if (_latest == expected)
                {
                    return true;
                }
                changed = _changed.Task;
            }
            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                return false;
            }
            await Task.WhenAny(changed, Task.Delay(remaining, cancellationToken)).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
        }
    }
}

public sealed record HostCheck(bool Ok, string Message);

public sealed record VmInfo(bool Exists, string State, int NetworkAdapters);

public sealed class HyperVException(string message) : Exception(message);

/// <summary>Alles, was auf dem Host mit Hyper-V geschieht. Echt: PowerShell-Skripte; in Tests: Simulation.</summary>
public interface IHyperVHost
{
    Task<HostCheck> CheckAsync(CancellationToken cancellationToken);

    /// <summary>Baut die VM neu auf (mit Administratorrechten, UAC). Meldet Fortschrittszeilen.</summary>
    Task BuildAsync(string requestFile, string statusFile, Action<string> progress, CancellationToken cancellationToken);

    /// <summary>Startet die Beobachtung der Gastsignale (KVP) und meldet jeden Wert an <paramref name="board"/>.</summary>
    Task WatchSignalsAsync(SignalBoard board, Action<string> problem, CancellationToken cancellationToken);

    /// <summary>Copy-VMFile: Dateien in dieser Reihenfolge in den Gastordner kopieren. Liefert die Zahl der Netzwerkkarten.</summary>
    Task<int> CopyToGuestAsync(IReadOnlyList<string> files, string guestDirectory, CancellationToken cancellationToken);

    /// <summary>Transfer-Laufwerk abhängen, nach <paramref name="detachedPath"/> verschieben, Ersatz aus der Vorlage anhängen.</summary>
    Task<int> SwapTransferDiskAsync(string currentDisk, string templateDisk, string newDisk, string detachedPath, CancellationToken cancellationToken);

    Task<VmInfo> GetVmInfoAsync(CancellationToken cancellationToken);

    /// <summary>Vorhandene VM starten/fortsetzen ("Start", nie mit Netzwerkkarte) oder Zustand speichern ("Save").</summary>
    Task<VmInfo> SetVmStateAsync(string action, CancellationToken cancellationToken);

    Task RemoveVmAsync(CancellationToken cancellationToken);
}

/// <summary>Bericht der Provisionierung im Gast (untrusted) und seine Prüfung.</summary>
public static class GuestReport
{
    public static (bool Ok, string Message) Evaluate(byte[] payload, int hostNetworkAdapters, IReadOnlyList<string>? expectedToolchains = null)
    {
        if (hostNetworkAdapters != 0)
        {
            return (false, $"VM hat {hostNetworkAdapters} Netzwerkkarte(n) – Ausführung gesperrt");
        }
        JsonNode? report;
        try
        {
            report = JsonNode.Parse(Encoding.UTF8.GetString(payload));
        }
        catch (JsonException)
        {
            return (false, "Bericht der VM ist kein gültiges JSON");
        }
        if (report is not JsonObject r || r["schemaVersion"]?.GetValueKind() != JsonValueKind.Number || (int)r["schemaVersion"]! != 1)
        {
            return (false, "Bericht der VM hat ein unbekanntes Format");
        }
        if (r["errors"] is JsonArray { Count: > 0 } errors)
        {
            return (false, "Provisionierung meldet Fehler: " + Clip(string.Join("; ", errors.Select(e => e?.ToString() ?? "")), 900));
        }
        if (r["networkAdapters"] is not JsonValue adapters || adapters.GetValueKind() != JsonValueKind.Number || (int)adapters != 0)
        {
            return (false, "Gast meldet Netzwerkkarten – Ausführung gesperrt");
        }
        if (r["probes"] is not JsonArray probes || probes.Count < 3)
        {
            return (false, "Netzwerkprüfungen fehlen im Bericht");
        }
        foreach (var probe in probes)
        {
            if (probe is not JsonObject p || p["reachable"]?.GetValueKind() != JsonValueKind.False)
            {
                return (false, "Netzwerkprüfung erfolgreich verbunden: " + Clip(probe?["name"]?.ToString() ?? "?") + " – Ausführung gesperrt");
            }
        }
        if (r["worker"] is not JsonObject w || w["running"]?.GetValueKind() != JsonValueKind.True)
        {
            return (false, "Worker läuft im Gast nicht");
        }
        var installed = new List<string>();
        var toolchains = r["toolchains"] as JsonArray ?? [];
        foreach (var name in expectedToolchains ?? [])
        {
            var entry = toolchains.OfType<JsonObject>().FirstOrDefault(t => t["name"]?.ToString() == name);
            if (entry is null)
            {
                return (false, $"Toolchain {name} fehlt im Bericht der VM");
            }
            if (entry["installed"]?.GetValueKind() != JsonValueKind.True || entry["error"] is JsonValue { } err && err.GetValueKind() == JsonValueKind.String)
            {
                return (false, $"Toolchain {name} nicht installiert: {Clip(entry["error"]?.ToString() ?? "unbekannt")}");
            }
            var selfTest = entry["selfTest"]?.ToString();
            var seconds = entry["seconds"] is JsonValue sv && sv.GetValueKind() == JsonValueKind.Number ? (int)sv : -1;
            installed.Add($"{name} {Clip(entry["version"]?.ToString() ?? "", 20)}" + (selfTest == "ok" ? " (Hallo-Welt ok)" : "") + InstallTime(seconds));
        }
        var tools = installed.Count == 0 ? "" : "; Toolchains: " + string.Join(", ", installed);
        return (true, $"Selbsttest bestanden ({probes.Count} Netzwerkprüfungen ohne Verbindung, Python {Clip(w["python"]?.ToString() ?? "?", 20)}{tools})");
    }

    /// <summary>Installationsdauer einer Toolchain im VM-Aufbau (M5: wird dokumentiert), z. B. „ 3 min“.</summary>
    public static string InstallTime(int seconds) => seconds switch
    {
        < 0 => "",
        < 120 => $" {seconds} s",
        _ => $" {(seconds + 30) / 60} min",
    };

    /// <summary>Gasttext nur gekürzt und ohne Steuerzeichen anzeigen.</summary>
    public static string Clip(string text, int max = 300)
    {
        var clean = new string(text.Select(c => char.IsControl(c) ? ' ' : c).ToArray());
        return clean.Length <= max ? clean : clean[..max] + " …";
    }
}

/// <summary>
/// Steuert die Offline-VM einer Sitzung: Neuaufbau, Selbsttest, Jobs per Copy-VMFile hin und per Transfer-VHDX zurück.
/// </summary>
public sealed class HyperVSandbox : ISandboxTransport, IAsyncDisposable
{
    public const string GuestInbox = @"C:\LocalAi\inbox";
    private readonly HyperVSettings _settings;
    private readonly IHyperVHost _host;
    private readonly Func<string, Task> _prepareGuest;
    private readonly IReadOnlyList<string> _expectedToolchains;
    private readonly SignalBoard _signals = new();
    private readonly SemaphoreSlim _exchange = new(1, 1);
    private readonly CancellationTokenSource _shutdown = new();
    private readonly Lock _gate = new();
    private SandboxStatus _status = new(SandboxState.Preparing, "VM-Aufbau wird vorbereitet");
    private Task? _lifecycle;
    private Task? _watcher;
    private string? _currentDisk;
    private int _diskCounter;
    private readonly SandboxSession? _session;
    private readonly Func<string>? _fingerprint;
    private readonly Func<string>? _codeFingerprint;
    private readonly Func<string, Task>? _buildUpdate;
    private readonly SandboxReuseStore _reuse;
    private string? _currentFingerprint;
    private string? _currentCode;
    private int _guestUpdate;
    private volatile bool _updating;
    private FileStream? _instanceLock;

    /// <summary>Wie lange eine zweite Instanz (z. B. Abnahme-Skript nach der Oberfläche) auf die Freigabe der VM wartet.</summary>
    public TimeSpan InstanceLockWait { get; init; } = TimeSpan.FromMinutes(3);

    /// <summary>Version des Gast-Updates, die neu aufgebaute VMs beherrschen (worker.cmd mit Neustart, update.ps1).</summary>
    public const int GuestUpdateVersion = 1;

    /// <summary>Internes Werkzeug des Hosts für das Gast-Update; nie über den Broker erreichbar.</summary>
    public const string UpdateTool = "Sandbox.Update";
    private DateTimeOffset _builtAt;
    private CancellationTokenSource? _lifecycleCts;
    private bool _forceRebuild;

    /// <param name="prepareGuest">Legt die Gastdateien im übergebenen Ordner ab (Python, Worker, Schlüssel, Skripte).</param>
    /// <param name="session">Sitzung (Schlüssel, Sequenz) – nötig für die Wiederverwendung.</param>
    /// <param name="fingerprint">Fingerabdruck der Grundlage (Abbild, Einstellungen, Laufzeit, Toolchains) – nötig für die Wiederverwendung.</param>
    /// <param name="codeFingerprint">Fingerabdruck von Worker-Code und Gastskripten; weicht nur er ab, wird die VM per Gast-Update aktualisiert.</param>
    /// <param name="buildUpdate">Schreibt die Nutzlast eines Gast-Updates (ZIP) an den übergebenen Pfad.</param>
    public HyperVSandbox(HyperVSettings settings, IHyperVHost host, Func<string, Task> prepareGuest, IReadOnlyList<string>? expectedToolchains = null,
        SandboxSession? session = null, Func<string>? fingerprint = null, Func<string>? codeFingerprint = null,
        Func<string, Task>? buildUpdate = null)
    {
        _codeFingerprint = codeFingerprint;
        _buildUpdate = buildUpdate;
        _expectedToolchains = expectedToolchains ?? [];
        settings.Validate();
        _settings = settings;
        _host = host;
        _prepareGuest = prepareGuest;
        _session = session;
        _fingerprint = fingerprint;
        _reuse = new SandboxReuseStore(settings.VmDirectory);
    }

    private bool ReuseEnabled => _settings.ReuseVm && _session is not null && _fingerprint is not null;

    /// <summary>
    /// Lädt eine gespeicherte Sitzung für die Wiederverwendung (vor dem Erzeugen des Adapters aufrufen).
    /// Nur wenn die letzte Sitzung sauber endete; sonst null → neue Sitzung, Neuaufbau.
    /// </summary>
    public static SandboxSession? LoadReusableSession(HyperVSettings settings)
    {
        if (!settings.ReuseVm)
        {
            return null;
        }
        var loaded = new SandboxReuseStore(settings.VmDirectory).Load();
        return loaded is { State.Clean: true } l ? new SandboxSession(l.State.SessionId, l.Key, l.State.LastSequence) : null;
    }

    public event Action<SandboxStatus>? StatusChanged;

    public SandboxStatus Status
    {
        get { lock (_gate) { return _status; } }
    }

    public string TemplateDisk => Path.Combine(_settings.VmDirectory, "transfer-template.vhdx");

    public string WorkDirectory => Path.Combine(_settings.VmDirectory, "host");

    /// <summary>Startet den Aufbau (bzw. die Wiederverwendung) im Hintergrund.</summary>
    public void Start()
    {
        _lifecycleCts = CancellationTokenSource.CreateLinkedTokenSource(_shutdown.Token);
        var token = _lifecycleCts.Token;
        _lifecycle = Task.Run(() => LifecycleAsync(token));
    }

    /// <summary>„VM neu aufbauen“: verwirft die laufende VM (auch eine gesperrte) und baut sie neu auf.</summary>
    public async Task RebuildAsync()
    {
        await _exchange.WaitAsync(_shutdown.Token).ConfigureAwait(false);
        try
        {
            if (_lifecycleCts is not null)
            {
                await _lifecycleCts.CancelAsync().ConfigureAwait(false);
            }
            foreach (var task in new[] { _lifecycle, _watcher })
            {
                if (task is null)
                {
                    continue;
                }
                try
                {
                    await task.WaitAsync(TimeSpan.FromSeconds(30)).ConfigureAwait(false);
                }
                catch (Exception ex) when (ex is OperationCanceledException or TimeoutException or HyperVException)
                {
                }
            }
            _reuse.Delete();
            _forceRebuild = true;
            lock (_gate)
            {
                _status = new SandboxStatus(SandboxState.Preparing, "VM wird neu aufgebaut");
            }
            StatusChanged?.Invoke(Status);
            Start();
        }
        finally
        {
            _exchange.Release();
        }
    }

    public Task WhenSettled => _lifecycle ?? Task.CompletedTask;

    private void SetStatus(SandboxState state, string text)
    {
        lock (_gate)
        {
            if (_status.State is SandboxState.Locked or SandboxState.Removed && state != SandboxState.Removed)
            {
                return;
            }
            _status = new SandboxStatus(state, text);
        }
        StatusChanged?.Invoke(new SandboxStatus(state, text));
    }

    /// <summary>Sperrt die Sandbox für diese Sitzung (Neuaufbau nötig). Eine gesperrte VM wird nie wiederverwendet.</summary>
    public void Lock(string reason)
    {
        if (ReuseEnabled)
        {
            _reuse.Delete();
        }
        SetStatus(SandboxState.Locked, "gesperrt: " + reason);
    }

    /// <summary>Hält Sitzung, Laufwerk und Fingerabdruck für die nächste Sitzung fest (nur mit reuseVm).</summary>
    private void SaveReuseState(bool clean)
    {
        if (!ReuseEnabled || _currentFingerprint is null || _currentDisk is null)
        {
            return;
        }
        try
        {
            _reuse.Save(new SandboxReuseState(1, _currentFingerprint, _session!.SessionId, "", "", _session.LastSequence, _currentDisk,
                Volatile.Read(ref _diskCounter), _builtAt, clean, _currentCode, _guestUpdate), _session.Key);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.Cryptography.CryptographicException)
        {
        }
    }

    /// <summary>Setzt eine passende, gespeicherte VM fort. false → neu aufbauen.</summary>
    private async Task<bool> TryResumeAsync(CancellationToken ct)
    {
        var loaded = _reuse.Load();
        if (loaded is not { } l || !l.State.Clean || l.State.Fingerprint != _currentFingerprint)
        {
            return false;
        }
        if (l.State.SessionId != _session!.SessionId)
        {
            // Die VM wurde erst nach dem Start dieser Instanz gespeichert (andere Instanz gerade beendet): Sitzung übernehmen.
            _session.Adopt(l.State.SessionId, l.Key, l.State.LastSequence);
        }
        var info = await _host.GetVmInfoAsync(ct).ConfigureAwait(false);
        if (!info.Exists || info.NetworkAdapters != 0)
        {
            return false;
        }
        SetStatus(SandboxState.WaitingForGuest, "vorhandene VM wird fortgesetzt (Stand passt)");
        // Ab jetzt gilt die VM als in Benutzung: stürzt das Programm ab, wird beim nächsten Start neu aufgebaut.
        _reuse.Save(l.State with { Clean = false }, l.Key);
        var codeMatches = _currentCode is null || l.State.CodeFingerprint == _currentCode;
        if (!codeMatches && (l.State.GuestUpdate < 1 || _buildUpdate is null))
        {
            // Älterer Gast ohne Update-Fähigkeit: einmal neu aufbauen.
            return false;
        }
        _currentDisk = l.State.CurrentDisk;
        Volatile.Write(ref _diskCounter, l.State.DiskCounter);
        _builtAt = l.State.BuiltAt;
        _guestUpdate = l.State.GuestUpdate;
        Directory.CreateDirectory(WorkDirectory);
        var started = await _host.SetVmStateAsync("Start", ct).ConfigureAwait(false);
        if (started.NetworkAdapters != 0)
        {
            Lock($"VM hat {started.NetworkAdapters} Netzwerkkarte(n)");
            return true;
        }
        var watch = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var resumed = false;
        _watcher = Task.Run(() => _host.WatchSignalsAsync(_signals, p =>
        {
            if (resumed)
            {
                Lock("Beobachtung der VM: " + GuestReport.Clip(p));
            }
        }, watch.Token), watch.Token);
        async Task<bool> GiveUp(string text)
        {
            SetStatus(SandboxState.Preparing, text);
            await watch.CancelAsync().ConfigureAwait(false);
            try
            {
                await _watcher!.WaitAsync(TimeSpan.FromSeconds(10), CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is OperationCanceledException or TimeoutException or HyperVException)
            {
            }
            _watcher = null;
            return false;
        }
        var deadline = DateTime.UtcNow + TimeSpan.FromMinutes(5);
        while (_signals.Heartbeat?.StartsWith("Ok", StringComparison.Ordinal) != true)
        {
            if (DateTime.UtcNow > deadline)
            {
                return await GiveUp($"vorhandene VM antwortet nicht ({_signals.DescribeGuest()}) – wird neu aufgebaut").ConfigureAwait(false);
            }
            await Task.Delay(1000, ct).ConfigureAwait(false);
        }
        // Frisches, leeres Transfer-Laufwerk; dabei Netzwerkkarten erneut prüfen.
        var (leftover, nics) = await CollectAsync(ct).ConfigureAwait(false);
        if (nics != 0)
        {
            Lock($"VM hat {nics} Netzwerkkarte(n)");
            return true;
        }
        var note = leftover is null ? "" : " (alte Rückgabe auf dem Transfer-Laufwerk verworfen)";
        if (!codeMatches)
        {
            SetStatus(SandboxState.WaitingForGuest, "vorhandene VM wird aktualisiert (Worker-Code und Gastskripte, ohne Neuaufbau)");
            var problem = await UpdateGuestAsync(ct).ConfigureAwait(false);
            if (Status.State == SandboxState.Locked)
            {
                return true;
            }
            if (problem is not null)
            {
                return await GiveUp($"Gast-Update fehlgeschlagen ({problem}) – wird neu aufgebaut").ConfigureAwait(false);
            }
            note += " und aktualisiert";
        }
        resumed = true;
        SaveReuseState(clean: false);
        SetStatus(SandboxState.Ready, $"Offline-VM bereit – wiederverwendet{note} (aufgebaut {_builtAt.ToLocalTime():dd.MM. HH:mm})");
        return true;
    }

    /// <summary>
    /// Gast-Update: signierter Job <see cref="UpdateTool"/> mit neuem Worker-Code und Gastskripten. Der Worker tauscht
    /// sie aus, richtet sie ein (update.ps1) und startet neu. Rückgabe: null = erfolgreich, sonst der Grund.
    /// </summary>
    private async Task<string?> UpdateGuestAsync(CancellationToken ct)
    {
        var zip = Path.Combine(WorkDirectory, "gast-update.zip");
        try
        {
            File.Delete(zip);
            await _buildUpdate!(zip).ConfigureAwait(false);
            var now = DateTimeOffset.UtcNow;
            var proposal = new ToolProposal(ContractJson.SchemaVersion, Guid.NewGuid().ToString("D"), "sandbox", "gast-update", AgentRole.Planner,
                UpdateTool, "1", "Gast-Update durch den Host (Worker-Code und Gastskripte)",
                JsonSerializer.SerializeToElement(new { codeFingerprint = _currentCode }), [], "-", 0, "sbx-hyperv",
                ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(15)));
            byte[] payloadHash;
            await using (var stream = File.OpenRead(zip))
            {
                payloadHash = await SHA256.HashDataAsync(stream, ct).ConfigureAwait(false);
            }
            var job = new SandboxJob(ContractJson.SchemaVersion, Guid.NewGuid().ToString("D"), _session!.NextSequence(), _session.SessionId,
                Guid.NewGuid().ToString("D"), proposal, ProposalHashing.Sha256(proposal), ContractTime.ToContract(now),
                ContractTime.ToContract(now.AddMinutes(15)), PayloadSha256: Convert.ToHexStringLower(payloadHash));
            _updating = true;
            SandboxReply reply;
            try
            {
                reply = await ExchangeAsync(JobSigner.Sign(job, _session.Key), zip, TimeSpan.FromMinutes(10), ct).ConfigureAwait(false);
            }
            finally
            {
                _updating = false;
            }
            var result = ResultValidator.Validate(reply, job);
            if (result.Status != ResultStatus.Succeeded)
            {
                return GuestReport.Clip(result.TargetResults.FirstOrDefault()?.Message ?? "kein Ergebnis", 300);
            }
            return null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidDataException)
        {
            return GuestReport.Clip(ex.Message, 300);
        }
        finally
        {
            try
            {
                File.Delete(zip);
            }
            catch (IOException)
            {
            }
        }
    }

    /// <summary>
    /// Nur eine Instanz (Oberfläche oder Abnahme-Skript) darf die VM gleichzeitig benutzen. Die Sperrdatei wird erst
    /// freigegeben, nachdem die VM gespeichert und ihr Zustand geschrieben ist – eine direkt danach gestartete Instanz
    /// wartet darauf und setzt die VM dann fort, statt sie neu aufzubauen.
    /// </summary>
    private async Task<bool> AcquireInstanceLockAsync(CancellationToken ct)
    {
        if (_instanceLock is not null)
        {
            return true;
        }
        Directory.CreateDirectory(_settings.VmDirectory);
        var path = Path.Combine(_settings.VmDirectory, "sandbox.lock");
        var deadline = DateTime.UtcNow + InstanceLockWait;
        var announced = false;
        while (true)
        {
            try
            {
                _instanceLock = new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None, 1, FileOptions.DeleteOnClose);
                return true;
            }
            catch (IOException) when (DateTime.UtcNow < deadline)
            {
                if (!announced)
                {
                    SetStatus(SandboxState.Preparing, "VM wird noch von einer anderen Instanz benutzt bzw. gespeichert – warte auf die Freigabe");
                    announced = true;
                }
                await Task.Delay(2000, ct).ConfigureAwait(false);
            }
            catch (IOException)
            {
                // Nicht Lock(): der gespeicherte Zustand gehört der anderen Instanz und bleibt erhalten.
                SetStatus(SandboxState.Locked, "gesperrt: VM wird von einer anderen Instanz benutzt (Oberfläche oder Abnahme-Skript) – diese zuerst schließen");
                return false;
            }
        }
    }

    /// <summary>
    /// „VM weiter verwenden“ nach ausgebliebener Antwort: verwirft eine verspätete Rückgabe (neues, leeres
    /// Transfer-Laufwerk), prüft erneut auf Netzwerkkarten und gibt die VM wieder frei. Der unbeantwortete Job wird
    /// nicht wiederholt.
    /// </summary>
    public async Task<string> ContinueAsync()
    {
        await _exchange.WaitAsync(_shutdown.Token).ConfigureAwait(false);
        try
        {
            if (Status.State != SandboxState.Paused)
            {
                return Status.Text;
            }
            var (leftover, nics) = await CollectAsync(_shutdown.Token).ConfigureAwait(false);
            if (nics != 0)
            {
                Lock($"VM hat {nics} Netzwerkkarte(n)");
                return Status.Text;
            }
            var note = leftover is null ? "" : $" (verspätete Rückgabe zu Sequenz {leftover.Sequence} verworfen)";
            SetStatus(SandboxState.Ready, $"Offline-VM bereit – nach Unterbrechung weiter verwendet{note}");
            return Status.Text;
        }
        catch (Exception ex) when (ex is HyperVException or IOException or VhdxFormatException)
        {
            Lock(GuestReport.Clip(ex.Message));
            return Status.Text;
        }
        finally
        {
            _exchange.Release();
        }
    }

    /// <summary>
    /// Not-Aus (SEC-15): Läuft gerade ein Job (oder ist die VM angehalten), wird die VM sofort ausgeschaltet – damit
    /// endet jeder Prozessbaum im Gast sicher. Danach ist die Sandbox gesperrt (Neuaufbau); ohne laufenden Job bleibt
    /// sie bereit, es wird ohnehin nichts mehr versandt (Dispatch gesperrt).
    /// </summary>
    public async Task<string> EmergencyStopAsync()
    {
        var busy = _exchange.CurrentCount == 0 || Status.State == SandboxState.Paused;
        if (!busy)
        {
            return "keine laufende Ausführung in der VM";
        }
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromMinutes(2));
            await _host.SetVmStateAsync("TurnOff", cts.Token).ConfigureAwait(false);
            Lock("Not-Aus: VM während einer Ausführung ausgeschaltet – „VM neu aufbauen“");
        }
        catch (Exception ex) when (ex is HyperVException or OperationCanceledException or IOException)
        {
            Lock("Not-Aus: VM konnte nicht ausgeschaltet werden (" + GuestReport.Clip(ex.Message) + ") – in Hyper-V-Manager ausschalten");
        }
        return Status.Text;
    }

    private async Task LifecycleAsync(CancellationToken ct)
    {
        try
        {
            if (!await AcquireInstanceLockAsync(ct).ConfigureAwait(false))
            {
                return;
            }
            SetStatus(SandboxState.Preparing, "Voraussetzungen werden geprüft");
            if (!File.Exists(_settings.ImagePath))
            {
                Lock($"Windows-Abbild nicht gefunden: {_settings.ImagePath}");
                return;
            }
            var check = await _host.CheckAsync(ct).ConfigureAwait(false);
            if (!check.Ok)
            {
                Lock(check.Message);
                return;
            }

            Directory.CreateDirectory(_settings.VmDirectory);
            if (ReuseEnabled)
            {
                _currentFingerprint = _fingerprint!();
                _currentCode = _codeFingerprint?.Invoke();
                if (!_forceRebuild && await TryResumeAsync(ct).ConfigureAwait(false))
                {
                    return;
                }
                _forceRebuild = false;
                _reuse.Delete();
            }
            SetStatus(SandboxState.Preparing, "Gastdateien werden zusammengestellt");
            var guestDir = Path.Combine(WorkDirectory, "guest");
            if (Directory.Exists(WorkDirectory))
            {
                FileTools.DeleteTree(WorkDirectory);
            }
            Directory.CreateDirectory(guestDir);
            await _prepareGuest(guestDir).ConfigureAwait(false);

            var requestFile = Path.Combine(WorkDirectory, "build-request.json");
            var statusFile = Path.Combine(WorkDirectory, "build-status.log");
            _currentDisk = NextDiskPath();
            await File.WriteAllTextAsync(requestFile, BuildRequestJson(guestDir, _currentDisk), new UTF8Encoding(false), ct).ConfigureAwait(false);

            SetStatus(SandboxState.Building, "VM wird aufgebaut (Administratorrechte werden angefragt)");
            var started = DateTime.UtcNow;
            using (var buildTimeout = CancellationTokenSource.CreateLinkedTokenSource(ct))
            {
                buildTimeout.CancelAfter(_settings.BuildTimeout);
                try
                {
                    await _host.BuildAsync(requestFile, statusFile,
                        line => SetStatus(SandboxState.Building, "VM wird aufgebaut – " + GuestReport.Clip(line, 120)), buildTimeout.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                {
                    Lock($"VM-Aufbau dauerte länger als {_settings.BuildTimeout.TotalMinutes:F0} Minuten");
                    return;
                }
            }

            _watcher = Task.Run(() => _host.WatchSignalsAsync(_signals, p => Lock("Beobachtung der VM: " + GuestReport.Clip(p)), ct), ct);
            SetStatus(SandboxState.WaitingForGuest, "Windows richtet sich in der VM ein (erster Start, dauert einige Minuten)");
            var reported = false;
            string? shown = null;
            while (!reported)
            {
                var remaining = _settings.BuildTimeout - (DateTime.UtcNow - started);
                if (remaining <= TimeSpan.Zero)
                {
                    break;
                }
                reported = await _signals.WaitForAsync("report:0", remaining < TimeSpan.FromSeconds(5) ? remaining : TimeSpan.FromSeconds(5), ct)
                    .ConfigureAwait(false);
                var guest = _signals.DescribeGuest();
                if (!reported && guest != shown)
                {
                    shown = guest;
                    SetStatus(SandboxState.WaitingForGuest, "VM startet – " + guest);
                }
            }
            if (!reported)
            {
                Lock($"VM hat sich nicht innerhalb von {_settings.BuildTimeout.TotalMinutes:F0} Minuten gemeldet (letzter Stand: {_signals.DescribeGuest()}) – " +
                     "Konsole der VM im Hyper-V-Manager prüfen, Gastprotokolle mit scripts\\sandbox\\Get-LocalAiGuestLogs.ps1");
                return;
            }

            SetStatus(SandboxState.SelfTest, "Selbsttest");
            var (message, nics) = await CollectAsync(ct).ConfigureAwait(false);
            if (message is null || message.Kind != TransferKind.Report)
            {
                Lock("Bericht der VM fehlt oder ist ungültig");
                return;
            }
            var (ok, text) = GuestReport.Evaluate(message.Payload, nics, _expectedToolchains);
            if (!ok)
            {
                Lock(text);
                return;
            }
            var minutes = (DateTime.UtcNow - started).TotalMinutes;
            _builtAt = DateTimeOffset.UtcNow;
            _guestUpdate = GuestUpdateVersion;
            SaveReuseState(clean: false);
            SetStatus(SandboxState.Ready, $"Offline-VM bereit – {text}; Aufbau {minutes:F0} min");
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
        }
        catch (Exception ex) when (ex is HyperVException or IOException or UnauthorizedAccessException or VhdxFormatException or InvalidDataException
                                       or ToolchainException)
        {
            Lock(GuestReport.Clip(ex.Message));
        }
    }

    private string NextDiskPath() =>
        Path.Combine(_settings.VmDirectory, $"transfer-{Interlocked.Increment(ref _diskCounter):D6}.vhdx");

    private string BuildRequestJson(string guestDir, string transferDisk)
    {
        var request = new JsonObject
        {
            ["schemaVersion"] = 1,
            ["imagePath"] = _settings.ImagePath,
            ["imageName"] = _settings.ImageName,
            ["imageIndex"] = _settings.ImageIndex,
            ["vmName"] = _settings.VmName,
            ["vmDirectory"] = _settings.VmDirectory,
            ["memoryMB"] = _settings.MemoryMB,
            ["processors"] = _settings.Processors,
            ["osDiskGB"] = _settings.OsDiskGB,
            ["transferDiskMB"] = _settings.TransferDiskMB,
            ["transferTemplate"] = TemplateDisk,
            ["transferDisk"] = transferDisk,
            ["guestDirectory"] = guestDir,
            ["productKey"] = _settings.ProductKey,
            ["keepVmOnExit"] = _settings.KeepVmOnExit,
        };
        return request.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
    }

    /// <summary>Holt den Container vom aktuellen Transfer-Laufwerk und hängt ein leeres an.</summary>
    private async Task<(TransferMessage? Message, int Nics)> CollectAsync(CancellationToken ct)
    {
        var current = _currentDisk ?? throw new InvalidOperationException("Kein Transfer-Laufwerk");
        var next = NextDiskPath();
        var detached = Path.Combine(WorkDirectory, "detached-" + Path.GetFileName(current));
        var nics = await _host.SwapTransferDiskAsync(current, TemplateDisk, next, detached, ct).ConfigureAwait(false);
        _currentDisk = next;
        try
        {
            return (TransferContainer.Read(detached, _settings.TransferDiskBytes), nics);
        }
        finally
        {
            File.Delete(detached);
        }
    }

    public async Task<SandboxReply> ExchangeAsync(JobEnvelope envelope, string? payloadPath, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var status = Status;
        if (!status.IsReady && !_updating)
        {
            return new SandboxReply(null, $"Sandbox nicht bereit ({status.Text})");
        }
        var job = envelope.Job;
        await _exchange.WaitAsync(cancellationToken).ConfigureAwait(false);
        var outgoing = Path.Combine(WorkDirectory, "outgoing");
        var stem = $"{job.Sequence:D12}-{job.JobId}";
        var files = new List<string>();
        try
        {
            if (!Status.IsReady && !_updating)
            {
                return new SandboxReply(null, $"Sandbox nicht bereit ({Status.Text})");
            }
            Directory.CreateDirectory(outgoing);
            if (payloadPath is not null)
            {
                var payload = Path.Combine(outgoing, stem + ".payload.zip");
                File.Copy(payloadPath, payload, overwrite: true);
                files.Add(payload);
            }
            var jobFile = Path.Combine(outgoing, stem + ".job.json");
            await File.WriteAllTextAsync(jobFile, ContractJson.Serialize(envelope), new UTF8Encoding(false), cancellationToken).ConfigureAwait(false);
            files.Add(jobFile);
            var ready = Path.Combine(outgoing, stem + ".ready");
            // Nicht leer: Copy-VMFile soll keine Sonderfälle mit 0-Byte-Dateien treffen.
            await File.WriteAllTextAsync(ready, stem, Encoding.ASCII, cancellationToken).ConfigureAwait(false);
            files.Add(ready);

            var nics = await _host.CopyToGuestAsync(files, GuestInbox, cancellationToken).ConfigureAwait(false);
            if (nics != 0)
            {
                Lock($"VM hat {nics} Netzwerkkarte(n)");
                return new SandboxReply(null, "VM hat eine Netzwerkkarte – Ausführung gesperrt");
            }
            var wait = timeout;
            if (!await _signals.WaitForAsync($"result:{job.Sequence}", wait, cancellationToken).ConfigureAwait(false))
            {
                // Zustand im Gast unklar: keine automatische Wiederholung. Je nach Einstellung anhalten (Nutzer entscheidet)
                // oder sperren.
                var reason = $"keine Antwort auf Job {job.Sequence} ({job.Proposal.Tool}) innerhalb von {wait.TotalMinutes:F1} min";
                if (_settings.PauseOnNoAnswer && !_updating)
                {
                    SetStatus(SandboxState.Paused, $"angehalten: {reason} – „VM weiter verwenden“ oder „VM neu aufbauen“ " +
                                                   "(Gastprotokolle: scripts\\sandbox\\Get-LocalAiGuestLogs.ps1)");
                    return new SandboxReply(null, $"Keine Antwort der Sandbox ({reason}) – Ergebnis unklar, Sandbox angehalten");
                }
                Lock($"{reason} – VM neu aufbauen");
                return new SandboxReply(null, "Keine Antwort der Sandbox (Ergebnis unklar) – Sandbox gesperrt");
            }
            var (message, swapNics) = await CollectAsync(cancellationToken).ConfigureAwait(false);
            SaveReuseState(clean: false);
            if (swapNics != 0)
            {
                Lock($"VM hat {swapNics} Netzwerkkarte(n)");
                return new SandboxReply(null, "VM hat eine Netzwerkkarte – Ausführung gesperrt");
            }
            if (message is null)
            {
                return new SandboxReply(null, "Transfer-Laufwerk ist leer");
            }
            if (message.Sequence != job.Sequence)
            {
                return new SandboxReply(null, $"Antwort gehört zu Sequenz {message.Sequence}, erwartet {job.Sequence}");
            }
            var text = new UTF8Encoding(false, throwOnInvalidBytes: true);
            try
            {
                return message.Kind switch
                {
                    TransferKind.Result => new SandboxReply(text.GetString(message.Payload), null),
                    TransferKind.Rejected => new SandboxReply(null, GuestReport.Clip(text.GetString(message.Payload), 500)),
                    _ => new SandboxReply(null, "Unerwartete Containerart"),
                };
            }
            catch (DecoderFallbackException)
            {
                return new SandboxReply(null, "Antwort ist kein gültiges UTF-8");
            }
        }
        catch (VhdxFormatException ex)
        {
            return new SandboxReply(null, "Rückgabe verworfen: " + ex.Message);
        }
        catch (HyperVException ex)
        {
            Lock(GuestReport.Clip(ex.Message));
            return new SandboxReply(null, "Hyper-V-Fehler: " + GuestReport.Clip(ex.Message));
        }
        finally
        {
            foreach (var f in files)
            {
                File.Delete(f);
            }
            _exchange.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _shutdown.CancelAsync().ConfigureAwait(false);
        foreach (var task in new[] { _lifecycle, _watcher })
        {
            if (task is not null)
            {
                try
                {
                    await task.WaitAsync(TimeSpan.FromSeconds(10)).ConfigureAwait(false);
                }
                catch (Exception ex) when (ex is OperationCanceledException or TimeoutException or HyperVException)
                {
                }
            }
        }
        if (_instanceLock is null)
        {
            // VM gehört einer anderen Instanz (oder wurde nie gestartet): nichts speichern, nichts löschen.
            SetStatus(SandboxState.Removed, "VM nicht übernommen");
            _shutdown.Dispose();
            _exchange.Dispose();
            return;
        }
        var kept = false;
        if (ReuseEnabled && (Status.IsReady || Status.State == SandboxState.Paused))
        {
            try
            {
                // Zustand speichern (schnelles Fortsetzen beim nächsten Start) und als sauber markieren.
                using var cts = new CancellationTokenSource(TimeSpan.FromMinutes(2));
                var saved = await _host.SetVmStateAsync("Save", cts.Token).ConfigureAwait(false);
                if (saved.NetworkAdapters == 0)
                {
                    SaveReuseState(clean: true);
                    kept = true;
                }
            }
            catch (Exception ex) when (ex is HyperVException or OperationCanceledException or IOException)
            {
            }
        }
        if (!kept && !_settings.KeepVmOnExit)
        {
            if (ReuseEnabled)
            {
                _reuse.Delete();
            }
            try
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromMinutes(2));
                await _host.RemoveVmAsync(cts.Token).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is HyperVException or OperationCanceledException or IOException)
            {
            }
        }
        SetStatus(SandboxState.Removed, kept ? "VM gespeichert (wird beim nächsten Start fortgesetzt)" : "VM entfernt");
        _shutdown.Dispose();
        _exchange.Dispose();
        // Erst jetzt (VM gespeichert, Zustand geschrieben) darf eine andere Instanz die VM übernehmen.
        _instanceLock?.Dispose();
        _instanceLock = null;
    }
}
