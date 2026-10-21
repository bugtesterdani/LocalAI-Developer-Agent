using System.Diagnostics;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace LocalAi.Sandbox;

/// <summary>
/// Liest Zeilen (LF) aus einem Datenstrom mit Längengrenze. Gegenstelle ist nicht vertrauenswürdig (Gast bzw. ein
/// vom Gast womöglich übernommener QEMU-Prozess): überlange Zeilen beenden die Verbindung.
/// </summary>
internal sealed class LineReader(Stream stream, int maxLine)
{
    private readonly byte[] _buffer = new byte[8192];
    private readonly MemoryStream _line = new();
    private int _start;
    private int _end;

    /// <summary>Nächste Zeile ohne Zeilenende; null am Ende des Datenstroms.</summary>
    public async Task<string?> ReadLineAsync(CancellationToken ct)
    {
        _line.SetLength(0);
        while (true)
        {
            if (_start == _end)
            {
                _start = 0;
                _end = await stream.ReadAsync(_buffer, ct).ConfigureAwait(false);
                if (_end <= 0)
                {
                    _end = 0;
                    return null;
                }
            }
            var newline = Array.IndexOf(_buffer, (byte)'\n', _start, _end - _start);
            var take = (newline < 0 ? _end : newline) - _start;
            if (_line.Length + take > maxLine)
            {
                throw new HyperVException($"Zeile länger als {maxLine} Bytes");
            }
            _line.Write(_buffer, _start, take);
            if (newline < 0)
            {
                _start = _end;
                continue;
            }
            _start = newline + 1;
            var text = Encoding.UTF8.GetString(_line.GetBuffer(), 0, (int)_line.Length);
            return text.EndsWith('\r') ? text[..^1] : text;
        }
    }
}

/// <summary>QMP-Verbindung zum QEMU-Prozess (JSON je Zeile über einen Unix-Socket). Nur feste Befehle des Hosts.</summary>
internal sealed class QmpConnection : IAsyncDisposable
{
    private readonly Socket _socket;
    private readonly NetworkStream _stream;
    private readonly LineReader _reader;
    private readonly SemaphoreSlim _gate = new(1, 1);

    private QmpConnection(Socket socket)
    {
        _socket = socket;
        _stream = new NetworkStream(socket, ownsSocket: false);
        _reader = new LineReader(_stream, 1024 * 1024);
    }

    /// <summary>Zuletzt gesehene Ereignisse (z. B. SHUTDOWN), für das Protokoll.</summary>
    public Action<string>? Event { get; set; }

    public static async Task<QmpConnection> ConnectAsync(string path, TimeSpan timeout, Func<bool> alive, CancellationToken ct)
    {
        var socket = await QemuSockets.ConnectAsync(path, timeout, alive, ct).ConfigureAwait(false);
        var qmp = new QmpConnection(socket);
        try
        {
            using var greeting = CancellationTokenSource.CreateLinkedTokenSource(ct);
            greeting.CancelAfter(TimeSpan.FromSeconds(30));
            var hello = await qmp._reader.ReadLineAsync(greeting.Token).ConfigureAwait(false);
            if (hello is null || !hello.Contains("\"QMP\"", StringComparison.Ordinal))
            {
                throw new HyperVException("QEMU meldet sich nicht über QMP");
            }
            await qmp.ExecuteAsync("qmp_capabilities", null, ct).ConfigureAwait(false);
            return qmp;
        }
        catch
        {
            await qmp.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    public async Task<JsonNode?> ExecuteAsync(string command, JsonObject? arguments, CancellationToken ct, TimeSpan? timeout = null)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(timeout ?? TimeSpan.FromSeconds(30));
            var request = new JsonObject { ["execute"] = command };
            if (arguments is not null)
            {
                request["arguments"] = arguments;
            }
            await _stream.WriteAsync(Encoding.UTF8.GetBytes(request.ToJsonString() + "\n"), cts.Token).ConfigureAwait(false);
            while (true)
            {
                var line = await _reader.ReadLineAsync(cts.Token).ConfigureAwait(false)
                           ?? throw new HyperVException($"QMP-Verbindung während {command} beendet");
                JsonNode? reply;
                try
                {
                    reply = JsonNode.Parse(line);
                }
                catch (JsonException)
                {
                    throw new HyperVException($"QMP: ungültige Antwort auf {command}");
                }
                if (reply is not JsonObject r)
                {
                    throw new HyperVException($"QMP: ungültige Antwort auf {command}");
                }
                if (r["event"] is JsonValue ev)
                {
                    Event?.Invoke(ev.ToString());
                    continue;
                }
                if (r["error"] is JsonObject error)
                {
                    throw new HyperVException($"QMP {command}: {GuestReport.Clip(error["desc"]?.ToString() ?? "Fehler", 200)}");
                }
                if (r.ContainsKey("return"))
                {
                    return r["return"];
                }
            }
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new HyperVException($"QMP {command}: keine Antwort");
        }
        finally
        {
            _gate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _stream.DisposeAsync().ConfigureAwait(false);
        _socket.Dispose();
    }
}

internal static class QemuSockets
{
    /// <summary>Verbindet sich mit einem Unix-Socket, den QEMU gleich nach dem Start anlegt.</summary>
    public static async Task<Socket> ConnectAsync(string path, TimeSpan timeout, Func<bool> alive, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            var socket = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
            try
            {
                await socket.ConnectAsync(new UnixDomainSocketEndPoint(path), ct).ConfigureAwait(false);
                return socket;
            }
            catch (SocketException) when (DateTime.UtcNow < deadline && alive())
            {
                socket.Dispose();
                await Task.Delay(100, ct).ConfigureAwait(false);
            }
            catch (SocketException ex)
            {
                socket.Dispose();
                throw new HyperVException($"Verbindung zu {Path.GetFileName(path)} fehlgeschlagen: {ex.Message}");
            }
        }
    }
}

/// <summary>
/// Verbindungskanal zum Gast (virtio-serial-Port <c>org.localai.link</c>, M10) – Gegenstück zu KVP und Copy-VMFile
/// unter Hyper-V. Vom Gast kommen nur Zeilen fester Form (<c>hello</c>, <c>hb</c>, <c>kvp KEY\tWERT</c>,
/// <c>ack NAME ok|error TEXT</c>); der Host schickt Dateien als <c>LAIFILE1 NAME GRÖSSE SHA256</c> plus Inhalt.
/// Der Gastdienst schreibt sie nur in den Eingang des Workers.
/// </summary>
internal sealed class QemuLink : IAsyncDisposable
{
    public const string PortName = "org.localai.link";
    public const int MaxLine = 4096;
    private static readonly Regex Key = new("^[A-Za-z][A-Za-z0-9]{0,63}$", RegexOptions.CultureInvariant);
    private readonly Socket _socket;
    private readonly NetworkStream _stream;
    private readonly SemaphoreSlim _write = new(1, 1);
    private readonly Lock _gate = new();
    private readonly Dictionary<string, TaskCompletionSource<string?>> _acks = new(StringComparer.Ordinal);
    private readonly CancellationTokenSource _stop = new();
    private Task? _reader;
    private long _lastHeartbeat;
    private long _hellos;

    private QemuLink(Socket? socket)
    {
        _socket = socket!;
        _stream = socket is null ? null! : new NetworkStream(socket, ownsSocket: false);
    }

    /// <summary>Nur für Tests: Zeilenverarbeitung ohne Socket.</summary>
    internal static QemuLink Detached() => new(null);

    /// <summary>KVP-Wert aus dem Gast (Schlüssel geprüft, Wert ungeprüft – Abnehmer prüft).</summary>
    public Action<string, string>? Kvp { get; set; }

    public Action<string>? Log { get; set; }

    /// <summary>Verstoß gegen das Protokoll oder Verbindung beendet.</summary>
    public string? Failure { get; private set; }

    /// <summary>Letztes Lebenszeichen des Gastdienstes (UTC); null = noch keines.</summary>
    public DateTime? LastHeartbeat
    {
        get
        {
            var ticks = Interlocked.Read(ref _lastHeartbeat);
            return ticks == 0 ? null : new DateTime(ticks, DateTimeKind.Utc);
        }
    }

    /// <summary>Der Gastdienst hat sich auf dieser Verbindung gemeldet und lebt (Lebenszeichen der letzten 15 s).</summary>
    public bool GuestReady => Interlocked.Read(ref _hellos) > 0 && LastHeartbeat is { } hb && DateTime.UtcNow - hb < TimeSpan.FromSeconds(15);

    public static async Task<QemuLink> ConnectAsync(string path, TimeSpan timeout, Func<bool> alive, CancellationToken ct)
    {
        var link = new QemuLink(await QemuSockets.ConnectAsync(path, timeout, alive, ct).ConfigureAwait(false));
        link._reader = Task.Run(link.ReadLoopAsync);
        return link;
    }

    private async Task ReadLoopAsync()
    {
        var reader = new LineReader(_stream, MaxLine);
        try
        {
            while (await reader.ReadLineAsync(_stop.Token).ConfigureAwait(false) is { } line)
            {
                Handle(line);
            }
            Fail("Verbindung zum Gast beendet");
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex) when (ex is IOException or SocketException or HyperVException or ObjectDisposedException)
        {
            Fail("Verbindungskanal: " + GuestReport.Clip(ex.Message, 200));
        }
    }

    internal void Handle(string line)
    {
        if (line == "hb")
        {
            Interlocked.Exchange(ref _lastHeartbeat, DateTime.UtcNow.Ticks);
            return;
        }
        if (line == "hello")
        {
            // Gastdienst (neu) gestartet: halb übertragene Dateien sind verloren.
            Interlocked.Increment(ref _hellos);
            Interlocked.Exchange(ref _lastHeartbeat, DateTime.UtcNow.Ticks);
            AbortAcks("Gastdienst neu gestartet");
            Log?.Invoke("Gastdienst meldet sich (hello)");
            return;
        }
        if (line.StartsWith("kvp ", StringComparison.Ordinal))
        {
            var parts = line[4..].Split('\t', 2);
            if (parts.Length == 2 && Key.IsMatch(parts[0]))
            {
                Kvp?.Invoke(parts[0], parts[1]);
            }
            return;
        }
        if (line.StartsWith("ack ", StringComparison.Ordinal))
        {
            var parts = line[4..].Split(' ', 3);
            if (parts.Length >= 2)
            {
                TaskCompletionSource<string?>? pending;
                lock (_gate)
                {
                    _acks.Remove(parts[0], out pending);
                }
                pending?.TrySetResult(parts[1] == "ok" ? null : GuestReport.Clip(parts.Length == 3 ? parts[2] : parts[1], 200));
            }
            return;
        }
        Log?.Invoke("Unbekannte Zeile vom Gast verworfen: " + GuestReport.Clip(line, 80));
    }

    private void Fail(string reason)
    {
        Failure ??= reason;
        AbortAcks(reason);
    }

    private void AbortAcks(string reason)
    {
        List<TaskCompletionSource<string?>> pending;
        lock (_gate)
        {
            pending = [.. _acks.Values];
            _acks.Clear();
        }
        foreach (var p in pending)
        {
            p.TrySetException(new HyperVException(reason));
        }
    }

    /// <summary>Überträgt eine Datei in den Eingang des Workers und wartet auf die Bestätigung des Gastdienstes.</summary>
    public async Task SendFileAsync(string path, CancellationToken ct)
    {
        var name = Path.GetFileName(path);
        if (!QemuVmHost.SafeFileName.IsMatch(name))
        {
            throw new HyperVException($"Dateiname für den Gast nicht erlaubt: {name}");
        }
        if (Failure is not null)
        {
            throw new HyperVException(Failure);
        }
        if (!GuestReady)
        {
            throw new HyperVException("Gastdienst noch nicht bereit");
        }
        await using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 16, FileOptions.SequentialScan);
        var hash = Convert.ToHexStringLower(await SHA256.HashDataAsync(file, ct).ConfigureAwait(false));
        file.Position = 0;
        var ack = new TaskCompletionSource<string?>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_gate)
        {
            _acks[name] = ack;
        }
        // Ab ca. 5 MB/s (TCG) zuzüglich Grundzeit für Prüfung und Umbenennen im Gast.
        var timeout = TimeSpan.FromSeconds(30 + file.Length / (5 * 1024 * 1024));
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);
        try
        {
            await _write.WaitAsync(cts.Token).ConfigureAwait(false);
            try
            {
                await _stream.WriteAsync(Encoding.ASCII.GetBytes($"LAIFILE1 {name} {file.Length} {hash}\n"), cts.Token).ConfigureAwait(false);
                await file.CopyToAsync(_stream, 1 << 16, cts.Token).ConfigureAwait(false);
                await _stream.FlushAsync(cts.Token).ConfigureAwait(false);
            }
            finally
            {
                _write.Release();
            }
            var error = await ack.Task.WaitAsync(cts.Token).ConfigureAwait(false);
            if (error is not null)
            {
                throw new HyperVException($"Gast lehnt {name} ab: {error}");
            }
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new HyperVException($"Gast bestätigt {name} nicht innerhalb von {timeout.TotalSeconds:F0} s");
        }
        catch (Exception ex) when (ex is IOException or SocketException)
        {
            throw new HyperVException($"Übertragung von {name} fehlgeschlagen: {ex.Message}");
        }
        finally
        {
            lock (_gate)
            {
                _acks.Remove(name);
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _stop.CancelAsync().ConfigureAwait(false);
        try
        {
            _socket.Shutdown(SocketShutdown.Both);
        }
        catch (SocketException)
        {
        }
        if (_reader is not null)
        {
            await _reader.ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
        }
        await _stream.DisposeAsync().ConfigureAwait(false);
        _socket.Dispose();
        AbortAcks("Verbindung beendet");
    }
}

/// <summary>
/// Offline-Linux-VM mit QEMU direkt auf einem Linux-Host (M10) – Gegenstück zu <see cref="PowerShellHyperVHost"/>.
/// Braucht keine Administratorrechte: Systemfestplatte per <c>qemu-img</c>, Seed-Laufwerk (FAT32, CIDATA) per
/// <c>mkfs.fat</c>/<c>mcopy</c>; QEMU läuft als Benutzer des Brokers, ohne Netzwerkgerät (<c>-nodefaults</c>,
/// <c>-nic none</c>), ohne Anzeige und mit seccomp. Hin zum Gast: Dateien über den Verbindungskanal (virtio-serial);
/// zurück: Transfer-Laufwerk im Rohformat, das per <c>blockdev-reopen</c> gegen ein leeres getauscht wird – der Host
/// liest die abgehängte Datei mit demselben strengen Containerformat wie unter Hyper-V.
/// </summary>
public sealed class QemuVmHost : IHyperVHost, IAsyncDisposable
{
    internal static readonly Regex SafeFileName = new(@"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$", RegexOptions.CultureInvariant);
    private const string Marker = ".localai-vm";
    private const string RawNode = "xfer";
    private static readonly SemaphoreSlim LogGate = new(1, 1);
    private readonly HyperVSettings _settings;
    private readonly string _logPath;
    private readonly SemaphoreSlim _state = new(1, 1);
    private Process? _process;
    private QmpConnection? _qmp;
    private QemuLink? _link;
    private Task? _console;
    private CancellationTokenSource? _consoleStop;
    private string? _transferNode;
    private int _nodeCounter;
    private volatile bool _stopping;
    private volatile SignalBoard? _board;

    public QemuVmHost(HyperVSettings settings, string logPath)
    {
        if (!settings.IsQemu)
        {
            throw new ArgumentException("Einstellungen sind nicht für QEMU", nameof(settings));
        }
        _settings = settings;
        _logPath = logPath;
    }

    /// <summary>Mindestens freier Speicher im VM-Ordner vor dem Aufbau.</summary>
    public long MinFreeBytes { get; init; } = 10L * 1024 * 1024 * 1024;

    /// <summary>Wie lange ein Herunterfahren dauern darf, bevor QEMU beendet wird.</summary>
    public TimeSpan ShutdownTimeout { get; init; } = TimeSpan.FromMinutes(2);


    public string DiskExtension => ".raw";

    public string BuildingText => "VM wird mit QEMU aufgebaut";

    public string GuestLogHint => $"Konsolenprotokoll der VM: {ConsoleLog}";

    public string TurnOffHint => $"QEMU-Prozess beenden (PID in {PidFile})";

    private string VmDir => _settings.VmDirectory;

    public string OsDisk => Path.Combine(VmDir, _settings.VmName + "-os.qcow2");

    public string SeedDisk => Path.Combine(VmDir, _settings.VmName + "-seed.img");

    public string ConfigFile => Path.Combine(VmDir, "vm.json");

    public string PidFile => Path.Combine(VmDir, "qemu.pid");

    public string ConsoleLog => Path.Combine(VmDir, "console.log");

    private string QemuLog => Path.Combine(VmDir, "qemu.log");

    /// <summary>
    /// Ordner der Steuer-Sockets: kurz (Unix-Sockets höchstens 107 Zeichen), nur für diesen Benutzer, je VM-Ordner eindeutig.
    /// </summary>
    public string SocketDirectory
    {
        get
        {
            var runtime = Environment.GetEnvironmentVariable("XDG_RUNTIME_DIR");
            var root = !string.IsNullOrEmpty(runtime) && Directory.Exists(runtime) ? runtime : "/tmp";
            var id = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(VmDir))))[..12];
            return Path.Combine(root, $"localai-{id}");
        }
    }

    private bool Running => _process is { HasExited: false };

    public async Task<HostCheck> CheckAsync(CancellationToken cancellationToken)
    {
        await Task.CompletedTask.ConfigureAwait(false);
        if (!OperatingSystem.IsLinux())
        {
            return new HostCheck(false, "Die QEMU-Sandbox (sandbox.mode \"qemu\") läuft nur auf einem Linux-Host");
        }
        var missing = new List<string>();
        foreach (var (program, package) in new[] { (_settings.QemuBinary, "qemu-system-x86"), ("qemu-img", "qemu-utils"), ("mkfs.fat", "dosfstools"), ("mcopy", "mtools") })
        {
            if (FindProgram(program) is null)
            {
                missing.Add($"{program} (Paket {package})");
            }
        }
        if (missing.Count > 0)
        {
            return new HostCheck(false, "Auf dem Host fehlt: " + string.Join(", ", missing) + " – scripts/linux/install-host.sh");
        }
        if (_settings.QemuAccel == "Kvm" && !KvmUsable())
        {
            return new HostCheck(false, "accel \"Kvm\": /dev/kvm ist nicht nutzbar (Gruppe kvm? Virtualisierung im BIOS?) – oder \"Auto\"/\"Tcg\" eintragen");
        }
        return new HostCheck(true, KvmUsable() ? "QEMU mit KVM" : "QEMU ohne KVM (reine Emulation, langsam)");
    }

    /// <summary>/dev/kvm lässt sich zum Lesen und Schreiben öffnen.</summary>
    public static bool KvmUsable()
    {
        try
        {
            using var kvm = new FileStream("/dev/kvm", FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite, 1);
            return true;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or NotSupportedException or ArgumentException)
        {
            return false;
        }
    }

    public static string? FindProgram(string program)
    {
        if (Path.IsPathRooted(program))
        {
            return File.Exists(program) ? program : null;
        }
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var candidate = Path.Combine(dir, program);
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }
        return null;
    }

    // ------------------------------------------------------------------ Aufbau

    public async Task BuildAsync(string requestFile, string statusFile, Action<string> progress, CancellationToken cancellationToken)
    {
        await File.WriteAllTextAsync(statusFile, "", cancellationToken).ConfigureAwait(false);
        const int steps = 6;
        void Step(int n, string text)
        {
            File.AppendAllText(statusFile, $"STEP {n}/{steps} {text}\n");
            progress($"Schritt {n}/{steps}: {text}");
            _ = LogAsync($"Aufbau Schritt {n}/{steps}: {text}");
        }
        try
        {
            using var doc = JsonDocument.Parse(await File.ReadAllTextAsync(requestFile, cancellationToken).ConfigureAwait(false));
            var request = doc.RootElement;
            var transferDisk = request.GetProperty("transferDisk").GetString()!;
            var template = request.GetProperty("transferTemplate").GetString()!;
            var seedSource = Path.Combine(request.GetProperty("guestDirectory").GetString()!, "seed");

            Step(1, "Voraussetzungen prüfen");
            if (request.GetProperty("guest").GetString() != "Linux")
            {
                throw new HyperVException("Anfrage ist nicht für einen Linux-Gast");
            }
            var check = await CheckAsync(cancellationToken).ConfigureAwait(false);
            if (!check.Ok)
            {
                throw new HyperVException(check.Message);
            }
            if (!File.Exists(_settings.ImagePath))
            {
                throw new HyperVException($"Debian-Abbild nicht gefunden: {_settings.ImagePath}");
            }
            foreach (var f in new[] { "user-data", "meta-data", Path.Combine("localai", "provision.sh") })
            {
                if (!File.Exists(Path.Combine(seedSource, f)))
                {
                    throw new HyperVException($"Gastdatei fehlt: {f}");
                }
            }
            Directory.CreateDirectory(VmDir);
            var foreign = Directory.EnumerateFileSystemEntries(VmDir).Select(Path.GetFileName)
                .Where(n => n is not ("host" or Marker or "build.log" or "gast-kennwort.txt" or "sandbox.lock" or "reuse.json")).ToList();
            if (!File.Exists(Path.Combine(VmDir, Marker)) && foreign.Count > 0)
            {
                throw new HyperVException($"Der VM-Ordner {VmDir} enthält fremde Dateien und wird nicht verwendet. Leeren Ordner angeben.");
            }
            await File.WriteAllTextAsync(Path.Combine(VmDir, Marker), "Von LocalAi verwaltet. Inhalt wird beim Neuaufbau gelöscht.\n", cancellationToken)
                .ConfigureAwait(false);

            Step(2, "Alte VM entfernen, Speicherplatz prüfen");
            await TurnOffAsync(cancellationToken).ConfigureAwait(false);
            KillStale();
            DeleteVmFiles();
            var free = new DriveInfo(Path.GetFullPath(VmDir)).AvailableFreeSpace;
            if (free < MinFreeBytes)
            {
                throw new HyperVException($"Zu wenig freier Speicher für {VmDir}: {free / (1024 * 1024 * 1024)} GB frei, mindestens {MinFreeBytes / (1024 * 1024 * 1024)} GB nötig");
            }

            Step(3, "Systemfestplatte aus dem Debian-Abbild");
            var format = _settings.ImagePath.EndsWith(".qcow2", StringComparison.OrdinalIgnoreCase) ? "qcow2" : "raw";
            await RunToolAsync("qemu-img", ["convert", "-f", format, "-O", "qcow2", _settings.ImagePath, OsDisk], TimeSpan.FromMinutes(30), cancellationToken)
                .ConfigureAwait(false);
            var info = await RunToolAsync("qemu-img", ["info", "--output=json", "-f", "qcow2", OsDisk], TimeSpan.FromMinutes(1), cancellationToken).ConfigureAwait(false);
            var virtualSize = JsonNode.Parse(info)?["virtual-size"]?.GetValue<long>() ?? 0;
            var osBytes = _settings.OsDiskGB * 1024L * 1024 * 1024;
            if (virtualSize > osBytes)
            {
                throw new HyperVException($"osDiskGB ist kleiner als das Abbild ({(virtualSize + (1 << 30) - 1) >> 30} GB)");
            }
            if (virtualSize < osBytes)
            {
                await RunToolAsync("qemu-img", ["resize", "-f", "qcow2", OsDisk, osBytes.ToString(System.Globalization.CultureInfo.InvariantCulture)],
                    TimeSpan.FromMinutes(1), cancellationToken).ConfigureAwait(false);
            }

            Step(4, "Seed-Laufwerk für cloud-init (CIDATA) mit den Gastdateien");
            var payload = Directory.EnumerateFiles(seedSource, "*", SearchOption.AllDirectories).Sum(f => new FileInfo(f).Length);
            // FAT32: mindestens 512 MB; nie so groß wie das Transfer-Laufwerk (der Gast erkennt dieses an seiner Größe).
            var seedMB = Math.Max(512, (long)Math.Ceiling(payload * 1.3 / (1024 * 1024)) + 256);
            if (seedMB == _settings.TransferDiskMB)
            {
                seedMB += 64;
            }
            if (seedMB > 32000)
            {
                throw new HyperVException($"Gastdateien zu groß für das Seed-Laufwerk ({payload / (1024 * 1024 * 1024.0):F1} GB) – weniger bzw. kleinere Toolchains wählen");
            }
            await RunToolAsync("mkfs.fat", ["-C", "-F", "32", "-n", "CIDATA", SeedDisk, (seedMB * 1024).ToString(System.Globalization.CultureInfo.InvariantCulture)],
                TimeSpan.FromMinutes(2), cancellationToken).ConfigureAwait(false);
            var entries = Directory.EnumerateFileSystemEntries(seedSource).Order(StringComparer.Ordinal).ToList();
            await RunToolAsync("mcopy", ["-s", "-Q", "-i", SeedDisk, .. entries, "::/"], TimeSpan.FromMinutes(20), cancellationToken).ConfigureAwait(false);
            await LogAsync($"Seed-Laufwerk {seedMB} MB, Gastdateien {payload / (1024 * 1024)} MB").ConfigureAwait(false);

            Step(5, "Transfer-Laufwerk anlegen");
            CreateEmptyDisk(template);
            CreateEmptyDisk(transferDisk);
            await SaveConfigAsync(transferDisk, cancellationToken).ConfigureAwait(false);

            Step(6, "VM ohne Netzwerkgerät starten");
            await _state.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                await StartProcessAsync(attachSeed: true, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                _state.Release();
            }
            await File.AppendAllTextAsync(statusFile, "DONE\n", cancellationToken).ConfigureAwait(false);
        }
        catch (HyperVException ex)
        {
            await File.AppendAllTextAsync(statusFile, $"ERROR {ex.Message}\n", CancellationToken.None).ConfigureAwait(false);
            await LogAsync("Aufbau fehlgeschlagen: " + ex.Message).ConfigureAwait(false);
            throw new HyperVException($"VM-Aufbau fehlgeschlagen: {ex.Message} (Protokoll: {_logPath})");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or KeyNotFoundException)
        {
            await LogAsync("Aufbau fehlgeschlagen: " + ex.Message).ConfigureAwait(false);
            throw new HyperVException($"VM-Aufbau fehlgeschlagen: {ex.Message} (Protokoll: {_logPath})");
        }
    }

    /// <summary>Leeres Laufwerk fester Größe (sparse: belegt keinen Platz).</summary>
    private void CreateEmptyDisk(string path)
    {
        using var stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None);
        stream.SetLength(_settings.TransferDiskBytes);
    }

    /// <summary><paramref name="transferDisk"/> null: zwischen den Jobs hängt kein Transfer-Laufwerk an der VM.</summary>
    private async Task SaveConfigAsync(string? transferDisk, CancellationToken ct)
    {
        var config = new JsonObject
        {
            ["schemaVersion"] = 1,
            ["vmName"] = _settings.VmName,
            ["transferDisk"] = transferDisk is null ? null : Path.GetFullPath(transferDisk),
        };
        var tmp = ConfigFile + ".tmp";
        await File.WriteAllTextAsync(tmp, config.ToJsonString(), ct).ConfigureAwait(false);
        File.Move(tmp, ConfigFile, overwrite: true);
    }

    private string? CurrentTransferDisk()
    {
        try
        {
            var config = JsonNode.Parse(File.ReadAllText(ConfigFile)) ?? throw new InvalidOperationException("leer");
            var path = config["transferDisk"]?.GetValue<string>();
            if (path is null)
            {
                return null; // zwischen den Jobs abgehängt
            }
            if (!File.Exists(path))
            {
                throw new HyperVException("Transfer-Laufwerk der VM fehlt – „VM neu aufbauen“");
            }
            return path;
        }
        catch (Exception ex) when (ex is JsonException or IOException or InvalidOperationException)
        {
            throw new HyperVException($"vm.json ist ungültig ({ex.Message}) – „VM neu aufbauen“");
        }
    }

    private void DeleteVmFiles()
    {
        foreach (var file in Directory.EnumerateFiles(VmDir))
        {
            var name = Path.GetFileName(file);
            if (file == OsDisk || file == SeedDisk || name is "vm.json" or "vm.json.tmp" or "qemu.pid"
                || (name.StartsWith("transfer-", StringComparison.Ordinal) && name.EndsWith(".raw", StringComparison.Ordinal)))
            {
                File.Delete(file);
            }
        }
    }

    private async Task<string> RunToolAsync(string program, IReadOnlyList<string> arguments, TimeSpan timeout, CancellationToken ct)
    {
        var exe = FindProgram(program) ?? throw new HyperVException($"{program} nicht gefunden");
        var start = new ProcessStartInfo(exe)
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
        };
        foreach (var a in arguments)
        {
            start.ArgumentList.Add(a);
        }
        // mtools: keine Prüfung der Laufwerksgeometrie, keine Benutzerkonfiguration.
        start.Environment["MTOOLS_SKIP_CHECK"] = "1";
        start.Environment["MTOOLSRC"] = "/dev/null";
        start.Environment["LC_ALL"] = "C.UTF-8";
        await LogAsync($"> {program} {string.Join(' ', arguments)}").ConfigureAwait(false);
        using var process = Process.Start(start) ?? throw new HyperVException($"{program} startet nicht");
        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync(ct);
        var stderr = process.StandardError.ReadToEndAsync(ct);
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);
        try
        {
            await process.WaitForExitAsync(cts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            if (ct.IsCancellationRequested)
            {
                throw;
            }
            throw new HyperVException($"{program} dauerte länger als {timeout.TotalMinutes:F0} min");
        }
        var output = await stdout.ConfigureAwait(false);
        var errors = await stderr.ConfigureAwait(false);
        await LogAsync($"{output}{errors}(Exitcode {process.ExitCode})").ConfigureAwait(false);
        if (process.ExitCode != 0)
        {
            throw new HyperVException($"{program} endete mit Exitcode {process.ExitCode}: {GuestReport.Clip(errors.Trim(), 300)}");
        }
        return output;
    }

    // ------------------------------------------------------------------ QEMU-Prozess

    /// <summary>Feste Befehlszeile: kein Netzwerkgerät, keine Anzeige, Steuerung nur über Unix-Sockets, seccomp.</summary>
    internal IReadOnlyList<string> BuildArguments(bool attachSeed, string? transferDisk, string transferNode, bool kvm)
    {
        static string Esc(string path) => path.Replace(",", ",,", StringComparison.Ordinal);
        var sockets = SocketDirectory;
        var cpuFlags = _settings.NestedVirtualization ? "" : ",vmx=off,svm=off";
        var args = new List<string>
        {
            "-name", $"{_settings.VmName},debug-threads=on",
            "-nodefaults", "-no-user-config",
            "-machine", "q35",
            "-accel", kvm ? "kvm" : "tcg",
            "-cpu", (kvm ? "host" : "max") + cpuFlags,
            "-smp", _settings.Processors.ToString(System.Globalization.CultureInfo.InvariantCulture),
            "-m", _settings.MemoryMB.ToString(System.Globalization.CultureInfo.InvariantCulture),
            "-nic", "none",
            "-display", "none", "-vga", "none",
            "-rtc", "base=utc",
            "-object", "rng-random,id=rng0,filename=/dev/urandom", "-device", "virtio-rng-pci,rng=rng0",
        };
        if (_settings.QemuTestBoot is { } boot)
        {
            args.AddRange(["-kernel", boot.Kernel, "-initrd", boot.Initrd, "-append", boot.Append]);
        }
        args.AddRange([
            "-blockdev", $"driver=file,filename={Esc(OsDisk)},node-name=os-file,discard=unmap",
            "-blockdev", "driver=qcow2,file=os-file,node-name=os,discard=unmap",
            "-device", "virtio-blk-pci,drive=os,serial=localai-os" + (_settings.QemuTestBoot is null ? ",bootindex=0" : ""),
        ]);
        if (attachSeed)
        {
            args.AddRange([
                "-blockdev", $"driver=file,filename={Esc(SeedDisk)},node-name=seed-file",
                "-blockdev", "driver=raw,file=seed-file,node-name=seed",
                "-device", "virtio-blk-pci,drive=seed,serial=localai-seed",
            ]);
        }
        args.AddRange([
            "-blockdev", transferDisk is null
                ? $"driver=null-co,size={_settings.TransferDiskBytes.ToString(System.Globalization.CultureInfo.InvariantCulture)},read-zeroes=on,node-name={transferNode}"
                : $"driver=file,filename={Esc(transferDisk)},node-name={transferNode}",
            "-blockdev", $"driver=raw,file={transferNode},node-name={RawNode}",
            "-device", "virtio-blk-pci,drive=xfer,id=xferdev,serial=localai-transfer",
            "-device", "virtio-serial-pci,id=vser",
            "-chardev", $"socket,id=link,path={Esc(Path.Combine(sockets, "link.sock"))},server=on,wait=off",
            "-device", $"virtserialport,chardev=link,name={QemuLink.PortName}",
            "-chardev", $"socket,id=con,path={Esc(Path.Combine(sockets, "console.sock"))},server=on,wait=off",
            "-serial", "chardev:con",
            "-qmp", $"unix:{Esc(Path.Combine(sockets, "qmp.sock"))},server=on,wait=off",
            "-pidfile", PidFile,
            "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-msg", "timestamp=on",
        ]);
        return args;
    }

    private void PrepareSocketDirectory()
    {
        if (OperatingSystem.IsWindows())
        {
            throw new HyperVException("QEMU-Sandbox nur auf einem Linux-Host");
        }
        var dir = SocketDirectory;
        if (Directory.Exists(dir))
        {
            var info = new DirectoryInfo(dir);
            if (info.LinkTarget is not null)
            {
                throw new HyperVException($"Socket-Ordner {dir} ist ein Verweis – Abbruch");
            }
            Directory.Delete(dir, recursive: true);
        }
        Directory.CreateDirectory(dir, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        if (File.GetUnixFileMode(dir) != (UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute))
        {
            throw new HyperVException($"Socket-Ordner {dir} hat zu weite Rechte");
        }
    }

    /// <summary>Startet QEMU (Aufrufer hält _state), verbindet QMP, Verbindungskanal und Konsole, prüft auf Netzwerkgeräte.</summary>
    private async Task StartProcessAsync(bool attachSeed, CancellationToken ct)
    {
        if (Running)
        {
            return;
        }
        KillStale();
        await DisconnectAsync().ConfigureAwait(false);
        if (!attachSeed && File.Exists(SeedDisk))
        {
            // Seed (mit Sitzungsschlüssel) wird nur beim ersten Start gebraucht; der Gast hat ihn bereits gelöscht.
            File.Delete(SeedDisk);
        }
        var transferDisk = CurrentTransferDisk();
        var node = "xf" + Interlocked.Increment(ref _nodeCounter);
        var kvm = _settings.QemuAccel switch
        {
            "Kvm" => true,
            "Tcg" => false,
            _ => KvmUsable(),
        };
        PrepareSocketDirectory();
        var exe = FindProgram(_settings.QemuBinary) ?? throw new HyperVException($"{_settings.QemuBinary} nicht gefunden");
        var start = new ProcessStartInfo(exe)
        {
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = VmDir,
        };
        var arguments = BuildArguments(attachSeed, transferDisk, node, kvm);
        foreach (var a in arguments)
        {
            start.ArgumentList.Add(a);
        }
        await LogAsync($"QEMU startet ({(kvm ? "KVM" : "TCG")}): {string.Join(' ', arguments)}").ConfigureAwait(false);
        _stopping = false;
        var process = Process.Start(start) ?? throw new HyperVException("QEMU startet nicht");
        process.StandardInput.Close();
        _ = PumpAsync(process.StandardOutput);
        _ = PumpAsync(process.StandardError);
        _process = process;
        _transferNode = node;
        try
        {
            bool Alive() => !process.HasExited;
            _qmp = await QmpConnection.ConnectAsync(Path.Combine(SocketDirectory, "qmp.sock"), TimeSpan.FromSeconds(30), Alive, ct).ConfigureAwait(false);
            _qmp.Event = e => _ = LogAsync("QMP-Ereignis " + e);
            var nics = await CountNicsAsync(ct).ConfigureAwait(false);
            if (nics != 0)
            {
                await TurnOffAsync(ct).ConfigureAwait(false);
                throw new HyperVException($"VM hat {nics} Netzwerkgerät(e) – sofort beendet (SEC-24)");
            }
            _link = await QemuLink.ConnectAsync(Path.Combine(SocketDirectory, "link.sock"), TimeSpan.FromSeconds(10), Alive, ct).ConfigureAwait(false);
            _link.Kvp = OnKvp;
            _link.Log = text => _ = LogAsync(text);
            _consoleStop = new CancellationTokenSource();
            var consoleSocket = await QemuSockets.ConnectAsync(Path.Combine(SocketDirectory, "console.sock"), TimeSpan.FromSeconds(10), Alive, ct).ConfigureAwait(false);
            _console = Task.Run(() => ConsolePumpAsync(consoleSocket, _consoleStop.Token));
        }
        catch (Exception ex) when (ex is HyperVException or IOException or SocketException)
        {
            var detail = process.HasExited ? $" (QEMU beendet, Exitcode {process.ExitCode}; Protokoll {QemuLog})" : "";
            await TurnOffAsync(CancellationToken.None).ConfigureAwait(false);
            throw new HyperVException(ex.Message + detail);
        }
    }

    private void OnKvp(string key, string value)
    {
        var board = _board;
        if (board is null)
        {
            return;
        }
        switch (key)
        {
            case "LocalAiSignal":
                if (!board.Post(value))
                {
                    _ = LogAsync("Ungültiges Signal verworfen: " + GuestReport.Clip(value, 80));
                }
                break;
            case "LocalAiProgress":
                if (board.Progress != value && board.PostProgress(value))
                {
                    _ = LogAsync("PROGRESS " + value);
                }
                break;
        }
    }

    /// <summary>Netzwerkgeräte (PCI-Klasse 0x02xx) laut QEMU – erwartet: keine.</summary>
    private async Task<int> CountNicsAsync(CancellationToken ct)
    {
        var qmp = _qmp ?? throw new HyperVException("QMP-Verbindung fehlt");
        var buses = await qmp.ExecuteAsync("query-pci", null, ct).ConfigureAwait(false) as JsonArray ?? [];
        return CountNetworkDevices(buses);
    }

    internal static int CountNetworkDevices(JsonArray buses)
    {
        var count = 0;
        void Walk(JsonArray? devices)
        {
            foreach (var device in devices?.OfType<JsonObject>() ?? [])
            {
                if (device["class_info"]?["class"] is JsonValue c && c.TryGetValue<int>(out var cls) && cls >> 8 == 0x02)
                {
                    count++;
                }
                Walk(device["pci_bridge"]?["devices"] as JsonArray);
            }
        }
        foreach (var bus in buses.OfType<JsonObject>())
        {
            Walk(bus["devices"] as JsonArray);
        }
        return count;
    }

    private async Task ConsolePumpAsync(Socket socket, CancellationToken ct)
    {
        const long MaxLog = 4L * 1024 * 1024;
        var buffer = new byte[8192];
        try
        {
            using (socket)
            {
                await using var stream = new NetworkStream(socket, ownsSocket: false);
                var log = new FileStream(ConsoleLog, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                try
                {
                    int read;
                    while ((read = await stream.ReadAsync(buffer, ct).ConfigureAwait(false)) > 0)
                    {
                        if (log.Length + read > MaxLog)
                        {
                            // Begrenzt: der Gast bestimmt den Inhalt. Ältere Hälfte als console.1.log behalten.
                            await log.DisposeAsync().ConfigureAwait(false);
                            File.Move(ConsoleLog, Path.Combine(VmDir, "console.1.log"), overwrite: true);
                            log = new FileStream(ConsoleLog, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                        }
                        await log.WriteAsync(buffer.AsMemory(0, read), ct).ConfigureAwait(false);
                        await log.FlushAsync(ct).ConfigureAwait(false);
                    }
                }
                finally
                {
                    await log.DisposeAsync().ConfigureAwait(false);
                }
            }
        }
        catch (Exception ex) when (ex is OperationCanceledException or IOException or SocketException or ObjectDisposedException)
        {
        }
    }

    private async Task PumpAsync(StreamReader reader)
    {
        try
        {
            while (await reader.ReadLineAsync().ConfigureAwait(false) is { } line)
            {
                await AppendCappedAsync(QemuLog, $"{DateTime.Now:HH:mm:ss} {line}\n").ConfigureAwait(false);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException)
        {
        }
    }

    private static async Task AppendCappedAsync(string path, string text)
    {
        await LogGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (File.Exists(path) && new FileInfo(path).Length > 2 * 1024 * 1024)
            {
                File.Move(path, path + ".1", overwrite: true);
            }
            await File.AppendAllTextAsync(path, text).ConfigureAwait(false);
        }
        catch (IOException)
        {
        }
        finally
        {
            LogGate.Release();
        }
    }

    private Task LogAsync(string text) => AppendCappedAsync(_logPath, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {text}\n");

    private async Task DisconnectAsync()
    {
        if (_consoleStop is not null)
        {
            await _consoleStop.CancelAsync().ConfigureAwait(false);
        }
        if (_link is not null)
        {
            await _link.DisposeAsync().ConfigureAwait(false);
            _link = null;
        }
        if (_qmp is not null)
        {
            await _qmp.DisposeAsync().ConfigureAwait(false);
            _qmp = null;
        }
        if (_console is not null)
        {
            await _console.ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
            _console = null;
        }
        _consoleStop?.Dispose();
        _consoleStop = null;
    }

    /// <summary>QEMU-Prozess einer abgestürzten früheren Sitzung beenden (PID-Datei, nur wenn er zu dieser VM gehört).</summary>
    private void KillStale()
    {
        if (Running || !File.Exists(PidFile))
        {
            return;
        }
        try
        {
            if (int.TryParse(File.ReadAllText(PidFile).Trim(), out var pid))
            {
                var cmdline = File.Exists($"/proc/{pid}/cmdline") ? File.ReadAllText($"/proc/{pid}/cmdline") : "";
                if (cmdline.Contains(PidFile, StringComparison.Ordinal))
                {
                    using var stale = Process.GetProcessById(pid);
                    stale.Kill();
                    stale.WaitForExit(10_000);
                    _ = LogAsync($"Verwaister QEMU-Prozess {pid} beendet");
                }
            }
            File.Delete(PidFile);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException or InvalidOperationException
                                       or System.ComponentModel.Win32Exception)
        {
        }
    }

    private async Task TurnOffAsync(CancellationToken ct)
    {
        _stopping = true;
        var process = _process;
        if (process is not null && !process.HasExited)
        {
            try
            {
                if (_qmp is not null)
                {
                    await _qmp.ExecuteAsync("quit", null, ct, TimeSpan.FromSeconds(5)).ConfigureAwait(false);
                }
            }
            catch (HyperVException)
            {
            }
            using var wait = CancellationTokenSource.CreateLinkedTokenSource(ct);
            wait.CancelAfter(TimeSpan.FromSeconds(10));
            try
            {
                await process.WaitForExitAsync(wait.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                process.Kill();
                await process.WaitForExitAsync(CancellationToken.None).ConfigureAwait(false);
            }
        }
        await DisconnectAsync().ConfigureAwait(false);
        process?.Dispose();
        _process = null;
    }

    private async Task ShutDownAsync(CancellationToken ct)
    {
        _stopping = true;
        var process = _process;
        if (process is null || process.HasExited)
        {
            await TurnOffAsync(ct).ConfigureAwait(false);
            return;
        }
        try
        {
            await _qmp!.ExecuteAsync("system_powerdown", null, ct).ConfigureAwait(false);
            using var wait = CancellationTokenSource.CreateLinkedTokenSource(ct);
            wait.CancelAfter(ShutdownTimeout);
            await process.WaitForExitAsync(wait.Token).ConfigureAwait(false);
            await LogAsync("VM heruntergefahren").ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HyperVException or OperationCanceledException or NullReferenceException)
        {
            await LogAsync("Herunterfahren dauert zu lange – QEMU wird beendet").ConfigureAwait(false);
        }
        await TurnOffAsync(ct).ConfigureAwait(false);
    }

    // ------------------------------------------------------------------ IHyperVHost

    public async Task WatchSignalsAsync(SignalBoard board, Action<string> problem, CancellationToken cancellationToken)
    {
        _board = board;
        try
        {
            var lastNicCheck = DateTime.UtcNow;
            var hadHeartbeat = false;
            while (!cancellationToken.IsCancellationRequested)
            {
                await Task.Delay(1000, cancellationToken).ConfigureAwait(false);
                var process = _process;
                if (process is null || process.HasExited)
                {
                    if (!_stopping)
                    {
                        problem($"ERROR QEMU wurde beendet{(process is null ? "" : $" (Exitcode {process.ExitCode})")} – Protokoll {QemuLog}");
                    }
                    return;
                }
                if (_link is { Failure: { } failure } && !_stopping)
                {
                    problem("ERROR " + failure);
                    return;
                }
                if (_link?.LastHeartbeat is { } hb)
                {
                    var fresh = DateTime.UtcNow - hb < TimeSpan.FromSeconds(15);
                    hadHeartbeat |= fresh;
                    board.PostHeartbeat(fresh ? "Ok" : "LostCommunication");
                }
                else if (hadHeartbeat)
                {
                    board.PostHeartbeat("LostCommunication");
                }
                if (DateTime.UtcNow - lastNicCheck > TimeSpan.FromSeconds(10))
                {
                    lastNicCheck = DateTime.UtcNow;
                    try
                    {
                        var nics = await CountNicsAsync(cancellationToken).ConfigureAwait(false);
                        if (nics != 0)
                        {
                            problem($"NICS {nics}");
                            return;
                        }
                    }
                    catch (HyperVException ex) when (!_stopping && process.HasExited == false)
                    {
                        problem("ERROR " + ex.Message);
                        return;
                    }
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
        finally
        {
            _board = null;
        }
    }

    public async Task<int> CopyToGuestAsync(IReadOnlyList<string> files, string guestDirectory, CancellationToken cancellationToken)
    {
        if (guestDirectory != HyperVSandbox.LinuxGuestInbox)
        {
            throw new HyperVException($"Zielordner im Gast nicht erlaubt: {guestDirectory}");
        }
        var link = Running ? _link : null;
        if (link is null)
        {
            throw new HyperVException("VM läuft nicht bzw. Verbindungskanal fehlt");
        }
        foreach (var file in files)
        {
            await link.SendFileAsync(file, cancellationToken).ConfigureAwait(false);
        }
        return await CountNicsAsync(cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// Abhängen: das Gerät bleibt (virtio-blk ist nicht zuverlässig im Betrieb entfernbar), sein Inhalt wird aber auf einen
    /// leeren null-co-Knoten umgestellt – Schreibzugriffe des Gasts gehen ins Leere, die Datei ist von QEMU gelöst, bevor
    /// der Broker sie liest.
    /// </summary>
    public async Task<int> DetachTransferDiskAsync(string currentDisk, string detachedPath, CancellationToken cancellationToken)
    {
        await _state.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var nics = 0;
            if (Running && _qmp is not null)
            {
                await SwitchTransferNodeAsync(NullNodeOptions("xf" + Interlocked.Increment(ref _nodeCounter)), cancellationToken).ConfigureAwait(false);
                nics = await CountNicsAsync(cancellationToken).ConfigureAwait(false);
            }
            await SaveConfigAsync(null, cancellationToken).ConfigureAwait(false);
            if (File.Exists(currentDisk))
            {
                File.Move(currentDisk, detachedPath, overwrite: true); // fehlt nur, wenn das Anhängen nie so weit kam
            }
            return nics;
        }
        finally
        {
            _state.Release();
        }
    }

    public async Task<int> AttachTransferDiskAsync(string templateDisk, string newDisk, CancellationToken cancellationToken)
    {
        await _state.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            CreateEmptyDisk(newDisk);
            var nics = 0;
            if (Running && _qmp is not null)
            {
                await SwitchTransferNodeAsync(new JsonObject
                {
                    ["driver"] = "file", ["filename"] = Path.GetFullPath(newDisk), ["node-name"] = "xf" + Interlocked.Increment(ref _nodeCounter),
                }, cancellationToken).ConfigureAwait(false);
                nics = await CountNicsAsync(cancellationToken).ConfigureAwait(false);
            }
            await SaveConfigAsync(newDisk, cancellationToken).ConfigureAwait(false);
            return nics;
        }
        finally
        {
            _state.Release();
        }
    }

    private JsonObject NullNodeOptions(string node) => new()
    {
        ["driver"] = "null-co", ["size"] = _settings.TransferDiskBytes, ["read-zeroes"] = true, ["node-name"] = node,
    };

    /// <summary>Knoten unter dem Transfer-Gerät tauschen; der alte Knoten (und damit die Datei) wird freigegeben.</summary>
    private async Task SwitchTransferNodeAsync(JsonObject options, CancellationToken ct)
    {
        var node = options["node-name"]!.GetValue<string>();
        await _qmp!.ExecuteAsync("blockdev-add", options, ct).ConfigureAwait(false);
        await _qmp.ExecuteAsync("blockdev-reopen", new JsonObject
        {
            ["options"] = new JsonArray(new JsonObject { ["driver"] = "raw", ["node-name"] = RawNode, ["file"] = node }),
        }, ct).ConfigureAwait(false);
        if (_transferNode is not null)
        {
            await _qmp.ExecuteAsync("blockdev-del", new JsonObject { ["node-name"] = _transferNode }, ct).ConfigureAwait(false);
        }
        _transferNode = node;
    }

    public async Task<VmInfo> GetVmInfoAsync(CancellationToken cancellationToken)
    {
        var exists = File.Exists(ConfigFile) && File.Exists(OsDisk);
        if (!Running)
        {
            return new VmInfo(exists, exists ? "Off" : "", 0);
        }
        return new VmInfo(exists, "Running", await CountNicsAsync(cancellationToken).ConfigureAwait(false));
    }

    public async Task<VmInfo> SetVmStateAsync(string action, CancellationToken cancellationToken)
    {
        if (action is not ("Start" or "Stop" or "Save" or "TurnOff"))
        {
            throw new ArgumentException("Start, Stop, Save oder TurnOff", nameof(action));
        }
        await _state.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            switch (action)
            {
                case "Start":
                    if (!File.Exists(ConfigFile) || !File.Exists(OsDisk))
                    {
                        throw new HyperVException("VM ist nicht aufgebaut – „VM neu aufbauen“");
                    }
                    await StartProcessAsync(attachSeed: false, cancellationToken).ConfigureAwait(false);
                    return new VmInfo(true, "Running", await CountNicsAsync(cancellationToken).ConfigureAwait(false));
                case "TurnOff":
                    await TurnOffAsync(cancellationToken).ConfigureAwait(false);
                    return new VmInfo(true, "Off", 0);
                default:
                    // „Save“ gibt es unter QEMU nicht (Validierung) – beides fährt herunter.
                    await ShutDownAsync(cancellationToken).ConfigureAwait(false);
                    return new VmInfo(true, "Off", 0);
            }
        }
        finally
        {
            _state.Release();
        }
    }

    public async Task RemoveVmAsync(CancellationToken cancellationToken)
    {
        await _state.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await TurnOffAsync(cancellationToken).ConfigureAwait(false);
            KillStale();
            if (Directory.Exists(VmDir))
            {
                DeleteVmFiles();
                File.Delete(Path.Combine(VmDir, "transfer-template.raw"));
            }
            if (Directory.Exists(SocketDirectory))
            {
                Directory.Delete(SocketDirectory, recursive: true);
            }
        }
        finally
        {
            _state.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        // HyperVSandbox hat die VM bereits heruntergefahren bzw. entfernt; hier nur verwaiste Prozesse vermeiden.
        if (Running)
        {
            await TurnOffAsync(CancellationToken.None).ConfigureAwait(false);
        }
        _state.Dispose();
    }
}
