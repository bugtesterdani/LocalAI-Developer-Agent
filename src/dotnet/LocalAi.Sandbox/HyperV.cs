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
            installed.Add($"{name} {Clip(entry["version"]?.ToString() ?? "", 20)}" + (selfTest == "ok" ? " (Hallo-Welt ok)" : ""));
        }
        var tools = installed.Count == 0 ? "" : "; Toolchains: " + string.Join(", ", installed);
        return (true, $"Selbsttest bestanden ({probes.Count} Netzwerkprüfungen ohne Verbindung, Python {Clip(w["python"]?.ToString() ?? "?", 20)}{tools})");
    }

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

    /// <param name="prepareGuest">Legt die Gastdateien im übergebenen Ordner ab (Python, Worker, Schlüssel, Skripte).</param>
    public HyperVSandbox(HyperVSettings settings, IHyperVHost host, Func<string, Task> prepareGuest, IReadOnlyList<string>? expectedToolchains = null)
    {
        _expectedToolchains = expectedToolchains ?? [];
        settings.Validate();
        _settings = settings;
        _host = host;
        _prepareGuest = prepareGuest;
    }

    public event Action<SandboxStatus>? StatusChanged;

    public SandboxStatus Status
    {
        get { lock (_gate) { return _status; } }
    }

    public string TemplateDisk => Path.Combine(_settings.VmDirectory, "transfer-template.vhdx");

    public string WorkDirectory => Path.Combine(_settings.VmDirectory, "host");

    /// <summary>Startet den Aufbau im Hintergrund.</summary>
    public void Start() => _lifecycle = Task.Run(() => LifecycleAsync(_shutdown.Token));

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

    /// <summary>Sperrt die Sandbox endgültig für diese Sitzung (Neuaufbau nötig).</summary>
    public void Lock(string reason) => SetStatus(SandboxState.Locked, "gesperrt: " + reason);

    private async Task LifecycleAsync(CancellationToken ct)
    {
        try
        {
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

            SetStatus(SandboxState.Preparing, "Gastdateien werden zusammengestellt");
            Directory.CreateDirectory(_settings.VmDirectory);
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
        if (!status.IsReady)
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
            if (!Status.IsReady)
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
                // Zustand im Gast unklar: keine weiteren Jobs in dieser VM (Spezifikation: keine automatische Wiederholung).
                Lock($"keine Antwort auf Job {job.Sequence} innerhalb von {wait.TotalSeconds:F0} s – VM neu aufbauen");
                return new SandboxReply(null, "Keine Antwort der Sandbox (Ergebnis unklar) – Sandbox gesperrt");
            }
            var (message, swapNics) = await CollectAsync(cancellationToken).ConfigureAwait(false);
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
        if (!_settings.KeepVmOnExit)
        {
            try
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromMinutes(2));
                await _host.RemoveVmAsync(cts.Token).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is HyperVException or OperationCanceledException or IOException)
            {
            }
        }
        SetStatus(SandboxState.Removed, "VM entfernt");
        _shutdown.Dispose();
        _exchange.Dispose();
    }
}
