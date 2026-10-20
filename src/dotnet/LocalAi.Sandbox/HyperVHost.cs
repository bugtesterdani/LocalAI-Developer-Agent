using System.ComponentModel;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using LocalAi.Contracts;

namespace LocalAi.Sandbox;

/// <summary>
/// Hyper-V über die PowerShell-Skripte in <c>scripts/sandbox</c>. Laufzeitbefehle brauchen die Gruppe
/// „Hyper-V-Administratoren“; nur der Neuaufbau (DISM, Einbinden der VHDX) läuft mit Administratorrechten (UAC).
/// </summary>
public sealed class PowerShellHyperVHost(HyperVSettings settings, string scriptsDirectory, string logPath) : IHyperVHost
{
    private static readonly SemaphoreSlim LogGate = new(1, 1);

    public string PowerShell { get; init; } = "powershell.exe";

    private string Script(string name) => Path.Combine(scriptsDirectory, name);

    public async Task<HostCheck> CheckAsync(CancellationToken cancellationToken)
    {
        if (!OperatingSystem.IsWindows())
        {
            return new HostCheck(false, "Hyper-V gibt es nur unter Windows");
        }
        var result = await RunJsonAsync("Test-LocalAiHost.ps1", ["-VmDirectory", settings.VmDirectory], cancellationToken).ConfigureAwait(false);
        return new HostCheck(result.GetProperty("ok").GetBoolean(), result.GetProperty("message").GetString() ?? "");
    }

    public async Task BuildAsync(string requestFile, string statusFile, Action<string> progress, CancellationToken cancellationToken)
    {
        File.WriteAllText(statusFile, string.Empty);
        var start = new ProcessStartInfo(PowerShell)
        {
            UseShellExecute = true,
            Verb = "runas",
            WindowStyle = ProcessWindowStyle.Minimized,
        };
        foreach (var a in new[] { "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Script(settings.IsLinux ? "New-LocalAiLinuxSandbox.ps1" : "New-LocalAiSandbox.ps1"),
                     "-RequestFile", requestFile, "-StatusFile", statusFile })
        {
            start.ArgumentList.Add(a);
        }
        await LogAsync($"Aufbau startet (UAC): {requestFile}").ConfigureAwait(false);
        Process process;
        try
        {
            process = Process.Start(start) ?? throw new HyperVException("Aufbau-Skript konnte nicht gestartet werden");
        }
        catch (Win32Exception ex) when (ex.NativeErrorCode == 1223)
        {
            throw new HyperVException("Administratorrechte für den VM-Aufbau wurden abgelehnt");
        }
        catch (Win32Exception ex)
        {
            throw new HyperVException($"Aufbau-Skript konnte nicht gestartet werden: {ex.Message}");
        }

        using (process)
        {
            long position = 0;
            string? error = null;
            var done = false;
            void Drain()
            {
                try
                {
                    DrainOnce();
                }
                catch (IOException)
                {
                    // Datei gerade vom Aufbau-Skript gesperrt: beim nächsten Durchlauf weiterlesen.
                }
            }
            void DrainOnce()
            {
                using var stream = new FileStream(statusFile, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                if (stream.Length <= position)
                {
                    return;
                }
                stream.Position = position;
                using var reader = new StreamReader(stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: true, leaveOpen: true);
                var text = reader.ReadToEnd();
                var complete = text.LastIndexOf('\n');
                if (complete < 0)
                {
                    return;
                }
                position += Encoding.UTF8.GetByteCount(text[..(complete + 1)]);
                foreach (var raw in text[..complete].Split('\n'))
                {
                    var line = raw.TrimEnd('\r').TrimStart('﻿');
                    if (line.StartsWith("STEP ", StringComparison.Ordinal))
                    {
                        var parts = line[5..].Split(' ', 2);
                        progress(parts.Length == 2 ? $"Schritt {parts[0]}: {parts[1]}" : line[5..]);
                    }
                    else if (line.StartsWith("ERROR ", StringComparison.Ordinal))
                    {
                        error = line[6..];
                    }
                    else if (line == "DONE")
                    {
                        done = true;
                    }
                }
            }
            try
            {
                while (!done && error is null && !HasExited(process))
                {
                    Drain();
                    await Task.Delay(500, cancellationToken).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException)
            {
                TryKill(process);
                throw;
            }
            Drain();
            await LogAsync($"Aufbau beendet: done={done} error={error}").ConfigureAwait(false);
            if (error is not null)
            {
                throw new HyperVException($"VM-Aufbau fehlgeschlagen: {error} (Protokoll: {Path.Combine(settings.VmDirectory, "build.log")})");
            }
            if (!done)
            {
                throw new HyperVException($"VM-Aufbau ohne Abschluss beendet (Protokoll: {Path.Combine(settings.VmDirectory, "build.log")})");
            }
        }
    }

    public async Task WatchSignalsAsync(SignalBoard board, Action<string> problem, CancellationToken cancellationToken)
    {
        using var process = StartScript("Watch-LocalAiSignal.ps1", ["-VmName", settings.VmName]);
        await using var registration = cancellationToken.Register(() => TryKill(process));
        _ = PumpErrorsAsync(process);
        while (await process.StandardOutput.ReadLineAsync(cancellationToken).ConfigureAwait(false) is { } line)
        {
            if (line.StartsWith("SIGNAL ", StringComparison.Ordinal))
            {
                var value = line[7..].Trim();
                if (!board.Post(value))
                {
                    await LogAsync("Ungültiges Signal verworfen: " + GuestReport.Clip(value, 80)).ConfigureAwait(false);
                }
            }
            else if (line.StartsWith("PROGRESS ", StringComparison.Ordinal))
            {
                board.PostProgress(line[9..].Trim());
                await LogAsync(line).ConfigureAwait(false);
            }
            else if (line.StartsWith("HEARTBEAT ", StringComparison.Ordinal))
            {
                board.PostHeartbeat(line[10..].Trim());
                await LogAsync(line).ConfigureAwait(false);
            }
            else if (line.StartsWith("NICS ", StringComparison.Ordinal) || line.StartsWith("ERROR ", StringComparison.Ordinal))
            {
                await LogAsync(line).ConfigureAwait(false);
                problem(line);
            }
        }
        if (!cancellationToken.IsCancellationRequested)
        {
            problem("Beobachtung der Gastsignale wurde beendet");
        }
    }

    public async Task<int> CopyToGuestAsync(IReadOnlyList<string> files, string guestDirectory, CancellationToken cancellationToken)
    {
        if (files.Any(f => f.Contains('|', StringComparison.Ordinal)))
        {
            throw new HyperVException("Dateiname mit '|' ist nicht erlaubt");
        }
        var result = await RunJsonAsync("Send-LocalAiJob.ps1",
            ["-VmName", settings.VmName, "-GuestDirectory", guestDirectory, "-FileList", string.Join('|', files)], cancellationToken).ConfigureAwait(false);
        return result.GetProperty("nics").GetInt32();
    }

    public async Task<int> SwapTransferDiskAsync(string currentDisk, string templateDisk, string newDisk, string detachedPath, CancellationToken cancellationToken)
    {
        var result = await RunJsonAsync("Switch-LocalAiTransferDisk.ps1",
            ["-VmName", settings.VmName, "-Current", currentDisk, "-Template", templateDisk, "-New", newDisk, "-Detached", detachedPath],
            cancellationToken).ConfigureAwait(false);
        return result.GetProperty("nics").GetInt32();
    }

    public async Task<VmInfo> GetVmInfoAsync(CancellationToken cancellationToken)
    {
        var result = await RunJsonAsync("Get-LocalAiVm.ps1", ["-VmName", settings.VmName], cancellationToken).ConfigureAwait(false);
        return new VmInfo(result.GetProperty("exists").GetBoolean(), result.GetProperty("state").GetString() ?? "", result.GetProperty("nics").GetInt32());
    }

    public async Task<VmInfo> SetVmStateAsync(string action, CancellationToken cancellationToken)
    {
        if (action is not ("Start" or "Stop" or "Save" or "TurnOff"))
        {
            throw new ArgumentException("Start, Stop, Save oder TurnOff", nameof(action));
        }
        var result = await RunJsonAsync("Set-LocalAiVmState.ps1", ["-VmName", settings.VmName, "-Action", action], cancellationToken).ConfigureAwait(false);
        return new VmInfo(true, result.GetProperty("state").GetString() ?? "", result.GetProperty("nics").GetInt32());
    }

    public Task RemoveVmAsync(CancellationToken cancellationToken) =>
        RunJsonAsync("Remove-LocalAiSandbox.ps1", ["-VmName", settings.VmName, "-VmDirectory", settings.VmDirectory], cancellationToken);

    private Process StartScript(string script, IEnumerable<string> arguments)
    {
        var start = new ProcessStartInfo(PowerShell)
        {
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            CreateNoWindow = true,
        };
        foreach (var a in new[] { "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", Script(script) }.Concat(arguments))
        {
            start.ArgumentList.Add(a);
        }
        Process process;
        try
        {
            process = Process.Start(start) ?? throw new HyperVException($"{script} konnte nicht gestartet werden");
        }
        catch (Win32Exception ex)
        {
            throw new HyperVException($"{script} konnte nicht gestartet werden: {ex.Message}");
        }
        process.StandardInput.Close();
        return process;
    }

    private async Task<JsonElement> RunJsonAsync(string script, IEnumerable<string> arguments, CancellationToken cancellationToken)
    {
        var args = arguments.ToList();
        await LogAsync($"{script} {string.Join(' ', args)}").ConfigureAwait(false);
        using var process = StartScript(script, args);
        var stdout = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var stderr = process.StandardError.ReadToEndAsync(cancellationToken);
        try
        {
            await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            TryKill(process);
            throw;
        }
        var output = await stdout.ConfigureAwait(false);
        var errors = await stderr.ConfigureAwait(false);
        if (errors.Length > 0)
        {
            await LogAsync($"{script} stderr: {errors}").ConfigureAwait(false);
        }
        var last = output.Split('\n').Select(l => l.Trim()).LastOrDefault(l => l.StartsWith('{'));
        if (process.ExitCode != 0 || last is null)
        {
            var reason = output.Split('\n').Select(l => l.Trim()).LastOrDefault(l => l.StartsWith("ERROR ", StringComparison.Ordinal))?[6..]
                ?? GuestReport.Clip(errors.Trim().Split('\n').LastOrDefault() ?? "", 300);
            throw new HyperVException($"{script} fehlgeschlagen (Exitcode {process.ExitCode}): {reason}");
        }
        await LogAsync($"{script} -> {last}").ConfigureAwait(false);
        using var doc = JsonDocument.Parse(last);
        return doc.RootElement.Clone();
    }

    private async Task PumpErrorsAsync(Process process)
    {
        while (await process.StandardError.ReadLineAsync().ConfigureAwait(false) is { } line)
        {
            await LogAsync("Watch stderr: " + line).ConfigureAwait(false);
        }
    }

    /// <summary>Bei erhöhten Prozessen kann die Abfrage scheitern; dann entscheidet die Statusdatei.</summary>
    private static bool HasExited(Process process)
    {
        try
        {
            return process.HasExited;
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException or NotSupportedException)
        {
            return false;
        }
    }

    private static void TryKill(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception ex) when (ex is InvalidOperationException or Win32Exception or NotSupportedException)
        {
        }
    }

    private async Task LogAsync(string line)
    {
        await LogGate.WaitAsync().ConfigureAwait(false);
        try
        {
            await File.AppendAllTextAsync(logPath, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {line}{Environment.NewLine}", new UTF8Encoding(false)).ConfigureAwait(false);
        }
        catch (IOException)
        {
        }
        finally
        {
            LogGate.Release();
        }
    }
}

/// <summary>Quellen für die Gastdateien: Python-Laufzeit, Pakete, Worker, Skripte.</summary>
/// <param name="LinuxPrerequisites">Nur Linux-Gast: Ordner mit prerequisites.json (Python für Linux, Wheels, hyperv-daemons).</param>
public sealed record GuestPayloadSources(
    string PythonHome,
    string SitePackages,
    string LocalAiPackage,
    string GuestScripts,
    IReadOnlyList<string> Packages,
    string? LinuxPrerequisites = null)
{
    /// <summary>
    /// Linux-Gast (M8): Worker und Gastskripte aus dem Repository, Python-Laufzeit und Pakete aus dem Offline-Ordner
    /// (scripts/sandbox/Get-LocalAiLinuxPrerequisites.ps1) – die Windows-Python des Hosts passt nicht in den Gast.
    /// </summary>
    public static GuestPayloadSources ForLinux(string repoRoot, string prerequisites)
    {
        var python = Path.Combine(repoRoot, "src", "python");
        return new GuestPayloadSources("", "", Path.Combine(python, "localai"), Path.Combine(repoRoot, "scripts", "sandbox", "guest-linux"),
            DefaultPackages, Path.GetFullPath(prerequisites));
    }

    /// <summary>Laufzeitpakete des Workers (pydantic und seine Abhängigkeiten); Testwerkzeuge kommen nicht in die VM.</summary>
    public static readonly IReadOnlyList<string> DefaultPackages =
        ["pydantic", "pydantic_core", "annotated_types", "typing_extensions", "typing_inspection"];

    /// <summary>Ermittelt Python-Laufzeit und site-packages aus der uv-Umgebung <c>src/python/.venv</c>.</summary>
    public static GuestPayloadSources FromRepository(string repoRoot)
    {
        var python = Path.Combine(repoRoot, "src", "python");
        var venv = Path.Combine(python, ".venv");
        var cfg = Path.Combine(venv, "pyvenv.cfg");
        if (!File.Exists(cfg))
        {
            throw new InvalidDataException($"Python-Umgebung fehlt ({cfg}) – 'uv sync --project src/python' ausführen");
        }
        var home = File.ReadAllLines(cfg)
            .Select(l => l.Split('=', 2))
            .Where(p => p.Length == 2 && p[0].Trim() == "home")
            .Select(p => p[1].Trim())
            .FirstOrDefault() ?? throw new InvalidDataException("pyvenv.cfg enthält keinen 'home'-Eintrag");
        var site = Path.Combine(venv, "Lib", "site-packages");
        if (!Directory.Exists(site))
        {
            site = Directory.GetDirectories(Path.Combine(venv, "lib"), "python*").Select(d => Path.Combine(d, "site-packages")).FirstOrDefault(Directory.Exists)
                ?? throw new InvalidDataException("site-packages der Python-Umgebung nicht gefunden");
        }
        return new GuestPayloadSources(home, site, Path.Combine(python, "localai"), Path.Combine(repoRoot, "scripts", "sandbox", "guest"), DefaultPackages);
    }
}

/// <summary>
/// Fingerabdruck eines VM-Aufbaus für die Wiederverwendung: Abbild (Pfad, Größe, Zeitstempel), VM-Einstellungen,
/// Python-Laufzeit, Worker-Code, Gastskripte und die Registrierungen der Projekt-Toolchains. Ändert sich etwas
/// davon, wird neu aufgebaut. Schnell: große Dateien (Abbild, Toolchains) gehen über Metadaten bzw. ihre
/// bei der Registrierung festgehaltenen Hashes ein.
/// </summary>
public static class SandboxFingerprint
{
    private static readonly HashSet<string> WorkerFiles = new(StringComparer.Ordinal) { "__init__.py", "canonical.py", "pathrules.py" };

    /// <summary>Gesamter Stand (Grundlage + Code) – ändert sich irgendetwas davon, passt eine gespeicherte VM nicht mehr unverändert.</summary>
    public static string Compute(HyperVSettings settings, GuestPayloadSources sources, IEnumerable<ToolchainRegistration> toolchains,
        IEnumerable<string> projectToolchains) =>
        Hash([ComputeBase(settings, sources, toolchains, projectToolchains), ComputeCode(settings, sources)]);

    /// <summary>
    /// Grundlage der VM: Abbild, VM-Einstellungen, Python-Laufzeit und -Pakete, Toolchains. Ändert sie sich, wird neu
    /// aufgebaut.
    /// </summary>
    public static string ComputeBase(HyperVSettings settings, GuestPayloadSources sources, IEnumerable<ToolchainRegistration> toolchains,
        IEnumerable<string> projectToolchains)
    {
        if (settings.IsLinux)
        {
            var linuxLines = new List<string>
            {
                "v1-base-linux",
                $"image|{settings.ImagePath}|{Facts(settings.ImagePath)}",
                $"vm|{settings.VmName}|{settings.MemoryMB}|{settings.Processors}|{settings.OsDiskGB}|{settings.TransferDiskMB}|{settings.KeepVmOnExit}"
                + (settings.NestedVirtualization ? "|nested" : "")
                + (settings.IsQemu ? $"|qemu|{settings.QemuAccel}" : ""),
                $"prereq|{LinuxPrerequisites.ManifestHash(sources.LinuxPrerequisites)}",
            };
            var known = toolchains.ToDictionary(r => r.Name, StringComparer.OrdinalIgnoreCase);
            foreach (var name in projectToolchains.Order(StringComparer.OrdinalIgnoreCase))
            {
                linuxLines.Add($"toolchain|{name}|{(known.TryGetValue(name, out var r) ? r.Digest : "nicht-registriert")}");
            }
            return Hash(linuxLines);
        }
        var lines = new List<string>
        {
            "v2-base",
            $"image|{settings.ImagePath}|{Facts(settings.ImagePath)}|{settings.ImageName}|{settings.ImageIndex}",
            $"vm|{settings.VmName}|{settings.MemoryMB}|{settings.Processors}|{settings.OsDiskGB}|{settings.TransferDiskMB}|{settings.ProductKey is not null}",
            $"python|{sources.PythonHome}|{Facts(Path.Combine(sources.PythonHome, "python.exe"))}",
        };
        IEnumerable<string?> entries = Directory.Exists(sources.SitePackages)
            ? Directory.EnumerateFileSystemEntries(sources.SitePackages).Select(Path.GetFileName).Order(StringComparer.Ordinal)
            : [];
        foreach (var entry in entries)
        {
            if (GuestPayloadBuilder.PackageName(entry!) is { } package && sources.Packages.Contains(package, StringComparer.OrdinalIgnoreCase))
            {
                lines.Add("pkg|" + entry);
            }
        }
        var registered = toolchains.ToDictionary(r => r.Name, StringComparer.OrdinalIgnoreCase);
        foreach (var name in projectToolchains.Order(StringComparer.OrdinalIgnoreCase))
        {
            lines.Add($"toolchain|{name}|{(registered.TryGetValue(name, out var r) ? r.Digest : "nicht-registriert")}");
        }
        return Hash(lines);
    }

    /// <summary>
    /// Code im Gast: Worker (Paket localai ohne Orchestrator), Gastskripte, Ausführungsoption. Ändert sich nur er, kann
    /// eine laufende VM per Gast-Update aktualisiert werden.
    /// </summary>
    public static string ComputeCode(HyperVSettings settings, GuestPayloadSources sources)
    {
        var lines = new List<string> { "v3-code", $"low={settings.LowIntegrityTools}", $"guest={settings.Guest}" };
        foreach (var (dir, include) in new (string, Func<string, bool>)[]
                 {
                     (sources.LocalAiPackage, rel => WorkerFiles.Contains(rel) || rel.StartsWith("worker/", StringComparison.Ordinal)
                                                     || rel.StartsWith("contracts/", StringComparison.Ordinal)
                                                     || rel.StartsWith("guest/", StringComparison.Ordinal)),
                     (sources.GuestScripts, _ => true),
                 })
        {
            if (!Directory.Exists(dir))
            {
                continue;
            }
            foreach (var file in Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories)
                         .Where(f => !f.Contains("__pycache__", StringComparison.Ordinal) && !f.EndsWith(".pyc", StringComparison.Ordinal))
                         .Order(StringComparer.Ordinal))
            {
                var rel = Path.GetRelativePath(dir, file).Replace('\\', '/');
                if (include(rel))
                {
                    lines.Add($"file|{rel}|{Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(file)))}");
                }
            }
        }
        return Hash(lines);
    }

    private static string Hash(IEnumerable<string> lines) =>
        Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(string.Join('\n', lines))));

    private static string Facts(string path)
    {
        var info = new FileInfo(path);
        return info.Exists ? $"{info.Length}|{info.LastWriteTimeUtc.Ticks}" : "fehlt";
    }
}

/// <summary>Stellt die Dateien zusammen, die beim Aufbau offline in das Gastabbild geschrieben werden.</summary>
public static class GuestPayloadBuilder
{
    private static readonly string[] SkipDirectories = ["__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip"];

    /// <summary>Wohin die Toolchains für den Aufbau kopiert werden (Windows: ins Abbild, Linux: aufs Seed-Laufwerk).</summary>
    public static string ToolchainStaging(string guestDirectory, GuestOs guest) => guest == GuestOs.Linux
        ? Path.Combine(guestDirectory, "seed", "localai", "toolchain-src")
        : Path.Combine(guestDirectory, "LocalAi", "toolchain-src");

    /// <summary>Gastdateien für die VM nach <paramref name="settings"/> (Windows-Abbild oder Linux-Seed).</summary>
    public static void Build(string guestDirectory, GuestPayloadSources sources, byte[] sessionKey, HyperVSettings settings, IReadOnlyList<string> toolchains)
    {
        if (settings.IsLinux)
        {
            BuildLinux(guestDirectory, sources, sessionKey, settings, toolchains);
        }
        else
        {
            Build(guestDirectory, sources, sessionKey, settings.TransferDiskBytes, toolchains, settings.LowIntegrityTools);
        }
    }

    public static readonly string[] LinuxScriptFiles = ["provision.sh"];

    /// <summary>
    /// Linux-Gast (M8): Inhalt des Seed-Laufwerks (FAT32, Bezeichnung CIDATA) für cloud-init NoCloud –
    /// <c>meta-data</c>, <c>user-data</c> und <c>localai/</c> mit Python für Linux, Wheels, hyperv-daemons, Worker,
    /// Sitzungsschlüssel, Konfiguration, Toolchains und provision.sh. Der Gast löscht das Laufwerk nach der Provisionierung.
    /// </summary>
    public static void BuildLinux(string guestDirectory, GuestPayloadSources sources, byte[] sessionKey, HyperVSettings settings, IReadOnlyList<string> toolchains)
    {
        var prerequisites = LinuxPrerequisites.Load(sources.LinuxPrerequisites
            ?? throw new InvalidDataException($"{settings.Section}.prerequisites fehlt"), settings.Hypervisor);
        var seed = Path.Combine(guestDirectory, "seed");
        var root = Path.Combine(seed, "localai");
        Directory.CreateDirectory(root);
        prerequisites.CopyVerified(Path.Combine(root, "prereq"));
        CopyTree(sources.LocalAiPackage, Path.Combine(root, "pkg", "localai"),
            rel => !rel.Split(Path.DirectorySeparatorChar).Contains("__pycache__") && !rel.EndsWith(".pyc", StringComparison.Ordinal)
                   && !rel.Split(Path.DirectorySeparatorChar)[0].Equals("orchestrator", StringComparison.Ordinal));
        foreach (var file in LinuxScriptFiles)
        {
            // Gastskripte immer mit LF (bash), auch wenn git sie unter Windows mit CRLF auscheckt.
            var text = File.ReadAllText(Path.Combine(sources.GuestScripts, file), Encoding.UTF8).Replace("\r\n", "\n");
            File.WriteAllText(Path.Combine(root, file), text, new UTF8Encoding(false));
        }
        File.WriteAllText(Path.Combine(root, "session.key"), Convert.ToHexStringLower(sessionKey), Encoding.ASCII);
        File.WriteAllText(Path.Combine(root, "guest-config.json"),
            GuestConfigJson(settings.TransferDiskBytes, toolchains, lowIntegrityTools: true, GuestOs.Linux, prerequisites, settings.Hypervisor),
            new UTF8Encoding(false));
        File.WriteAllText(Path.Combine(seed, "meta-data"),
            $"instance-id: localai-{Guid.NewGuid():N}\nlocal-hostname: localai-sbx\n", new UTF8Encoding(false));
        string? password = null;
        if (settings.KeepVmOnExit)
        {
            // Nur zur Fehlersuche (keepVmOnExit): Anmeldung an der VM-Konsole als root.
            password = "Lai-" + Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(12));
            Directory.CreateDirectory(settings.VmDirectory);
            File.WriteAllText(Path.Combine(settings.VmDirectory, "gast-kennwort.txt"), password, Encoding.ASCII);
        }
        File.WriteAllText(Path.Combine(seed, "user-data"), CloudConfig(password, settings.IsQemu), new UTF8Encoding(false));
    }

    /// <summary>cloud-config: keine Benutzer, keine Paketquellen, Root-Partition vergrößern, provision.sh vom Seed starten.</summary>
    public static string CloudConfig(string? rootPassword, bool serialConsole = false)
    {
        var sb = new StringBuilder();
        sb.Append("#cloud-config\n");
        sb.Append("# LocalAi-Sandbox (Linux): ohne Netzwerkkarte, ohne Benutzerkonten; Provisionierung vom Seed-Laufwerk.\n");
        sb.Append("users: []\n");
        sb.Append("disable_root: true\n");
        sb.Append("ssh_pwauth: false\n");
        sb.Append("package_update: false\n");
        sb.Append("package_upgrade: false\n");
        sb.Append("package_reboot_if_required: false\n");
        sb.Append("manage_etc_hosts: false\n");
        sb.Append("growpart:\n  mode: auto\n  devices: ['/']\n");
        if (rootPassword is not null)
        {
            sb.Append("chpasswd:\n  expire: false\n  users:\n");
            sb.Append($"    - {{name: root, password: \"{rootPassword}\", type: text}}\n");
        }
        sb.Append("runcmd:\n");
        // QEMU (M10): Protokoll zusätzlich auf die serielle Konsole – der Host schreibt sie nach console.log.
        var output = serialConsole
            ? "2>&1 | tee -a /var/log/localai-provision.log > /dev/ttyS0"
            : ">> /var/log/localai-provision.log 2>&1";
        sb.Append("  - [/bin/bash, -c, \"mkdir -p /mnt/localai-seed && mount -o ro LABEL=CIDATA /mnt/localai-seed && " +
                  $"/bin/bash /mnt/localai-seed/localai/provision.sh {output}\"]\n");
        return sb.ToString();
    }

    public static void Build(string guestDirectory, GuestPayloadSources sources, byte[] sessionKey, long transferDiskBytes,
        IReadOnlyList<string>? toolchains = null, bool lowIntegrityTools = true)
    {
        if (!File.Exists(Path.Combine(sources.PythonHome, "python.exe")))
        {
            throw new InvalidDataException($"python.exe nicht gefunden in {sources.PythonHome} – die Gast-Laufzeit muss eine Windows-Python sein");
        }
        var root = Path.Combine(guestDirectory, "LocalAi");
        var python = Path.Combine(root, "python");
        CopyTree(sources.PythonHome, python, rel =>
            !rel.StartsWith(Path.Combine("Lib", "site-packages"), StringComparison.OrdinalIgnoreCase)
            && !rel.Split(Path.DirectorySeparatorChar).Any(p => SkipDirectories.Contains(p, StringComparer.OrdinalIgnoreCase)));

        var site = Path.Combine(python, "Lib", "site-packages");
        Directory.CreateDirectory(site);
        var copied = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in Directory.EnumerateFileSystemEntries(sources.SitePackages))
        {
            var name = Path.GetFileName(entry);
            var package = PackageName(name);
            if (package is null || !sources.Packages.Contains(package, StringComparer.OrdinalIgnoreCase))
            {
                continue;
            }
            copied.Add(package);
            if (Directory.Exists(entry))
            {
                CopyTree(entry, Path.Combine(site, name), rel => !rel.Split(Path.DirectorySeparatorChar).Contains("__pycache__"));
            }
            else
            {
                File.Copy(entry, Path.Combine(site, name));
            }
        }
        var missing = sources.Packages.Where(p => !copied.Contains(p)).ToList();
        if (missing.Count > 0)
        {
            throw new InvalidDataException($"Pakete fehlen in {sources.SitePackages}: {string.Join(", ", missing)}");
        }
        CopyTree(sources.LocalAiPackage, Path.Combine(site, "localai"),
            rel => !rel.Split(Path.DirectorySeparatorChar).Contains("__pycache__") && !rel.EndsWith(".pyc", StringComparison.Ordinal));

        File.WriteAllText(Path.Combine(root, "session.key"), Convert.ToHexStringLower(sessionKey), Encoding.ASCII);
        File.WriteAllText(Path.Combine(root, "guest-config.json"), GuestConfigJson(transferDiskBytes, toolchains, lowIntegrityTools), new UTF8Encoding(false));
        foreach (var file in GuestScriptFiles)
        {
            File.Copy(Path.Combine(sources.GuestScripts, file), Path.Combine(root, file));
        }
        File.Copy(Path.Combine(sources.GuestScripts, "SetupComplete.cmd"), Path.Combine(guestDirectory, "SetupComplete.cmd"));
    }

    public static readonly string[] GuestScriptFiles = ["provision.ps1", "LocalAiGuest.psm1", "update.ps1"];

    public static string GuestConfigJson(long transferDiskBytes, IReadOnlyList<string>? toolchains, bool lowIntegrityTools) =>
        JsonSerializer.Serialize(new { schemaVersion = 1, transferDiskBytes, toolchains = toolchains ?? [], lowIntegrityTools });

    /// <summary>guest-config.json des Linux-Gasts: zusätzlich Gastsystem und die Dateien aus prerequisites.json.</summary>
    public static string GuestConfigJson(long transferDiskBytes, IReadOnlyList<string>? toolchains, bool lowIntegrityTools, GuestOs guest,
        LinuxPrerequisites? prerequisites, Hypervisor hypervisor = Hypervisor.HyperV) =>
        guest != GuestOs.Linux
            ? GuestConfigJson(transferDiskBytes, toolchains, lowIntegrityTools)
            : JsonSerializer.Serialize(new
            {
                schemaVersion = 1,
                guest = "Linux",
                hypervisor = hypervisor.ToString(),
                transferDiskBytes,
                toolchains = toolchains ?? [],
                python = prerequisites?.Python,
                wheels = prerequisites?.Wheels ?? [],
                debs = prerequisites?.Debs ?? [],
            });

    /// <summary>
    /// Nutzlast eines Gast-Updates: Paket <c>localai</c> (ohne Caches), Gastskripte und <c>guest-config.json</c>
    /// (siehe localai/worker/update.py). Python-Laufzeit, Pakete und Toolchains sind nicht enthalten.
    /// </summary>
    public static void BuildUpdate(string zipPath, GuestPayloadSources sources, long transferDiskBytes, IReadOnlyList<string>? toolchains,
        bool lowIntegrityTools, GuestOs guest, Hypervisor hypervisor = Hypervisor.HyperV)
    {
        if (guest != GuestOs.Linux)
        {
            BuildUpdate(zipPath, sources, transferDiskBytes, toolchains, lowIntegrityTools);
            return;
        }
        using var zip = System.IO.Compression.ZipFile.Open(zipPath, System.IO.Compression.ZipArchiveMode.Create);
        AddPackage(zip, sources);
        foreach (var file in LinuxScriptFiles)
        {
            var entry = zip.CreateEntry("guest/" + file);
            using var script = new StreamWriter(entry.Open(), new UTF8Encoding(false));
            script.Write(File.ReadAllText(Path.Combine(sources.GuestScripts, file), Encoding.UTF8).Replace("\r\n", "\n"));
        }
        var prerequisites = sources.LinuxPrerequisites is null ? null : LinuxPrerequisites.Load(sources.LinuxPrerequisites, hypervisor);
        var config = zip.CreateEntry("guest-config.json");
        using var writer = new StreamWriter(config.Open(), new UTF8Encoding(false));
        writer.Write(GuestConfigJson(transferDiskBytes, toolchains, lowIntegrityTools, GuestOs.Linux, prerequisites, hypervisor));
    }

    private static void AddPackage(System.IO.Compression.ZipArchive zip, GuestPayloadSources sources)
    {
        foreach (var file in Directory.EnumerateFiles(sources.LocalAiPackage, "*", SearchOption.AllDirectories).Order(StringComparer.Ordinal))
        {
            var rel = Path.GetRelativePath(sources.LocalAiPackage, file).Replace('\\', '/');
            if (rel.Split('/').Contains("__pycache__") || rel.EndsWith(".pyc", StringComparison.Ordinal) || new FileInfo(file).LinkTarget is not null)
            {
                continue;
            }
            System.IO.Compression.ZipFileExtensions.CreateEntryFromFile(zip, file, "localai/" + rel);
        }
    }

    public static void BuildUpdate(string zipPath, GuestPayloadSources sources, long transferDiskBytes, IReadOnlyList<string>? toolchains,
        bool lowIntegrityTools)
    {
        using var zip = System.IO.Compression.ZipFile.Open(zipPath, System.IO.Compression.ZipArchiveMode.Create);
        foreach (var file in Directory.EnumerateFiles(sources.LocalAiPackage, "*", SearchOption.AllDirectories).Order(StringComparer.Ordinal))
        {
            var rel = Path.GetRelativePath(sources.LocalAiPackage, file).Replace('\\', '/');
            if (rel.Split('/').Contains("__pycache__") || rel.EndsWith(".pyc", StringComparison.Ordinal) || new FileInfo(file).LinkTarget is not null)
            {
                continue;
            }
            System.IO.Compression.ZipFileExtensions.CreateEntryFromFile(zip, file, "localai/" + rel);
        }
        foreach (var file in GuestScriptFiles)
        {
            System.IO.Compression.ZipFileExtensions.CreateEntryFromFile(zip, Path.Combine(sources.GuestScripts, file), "guest/" + file);
        }
        var config = zip.CreateEntry("guest-config.json");
        using var writer = new StreamWriter(config.Open(), new UTF8Encoding(false));
        writer.Write(GuestConfigJson(transferDiskBytes, toolchains, lowIntegrityTools));
    }

    /// <summary>„pydantic“, „pydantic-2.9.dist-info“, „typing_extensions.py“ → Paketname; sonst null (.pth, Editables …).</summary>
    public static string? PackageName(string entry)
    {
        if (entry.EndsWith(".pth", StringComparison.OrdinalIgnoreCase) || entry.StartsWith('_'))
        {
            return null;
        }
        if (entry.EndsWith(".dist-info", StringComparison.OrdinalIgnoreCase))
        {
            return entry[..entry.IndexOf('-')];
        }
        if (entry.EndsWith(".py", StringComparison.OrdinalIgnoreCase))
        {
            return entry[..^3];
        }
        return entry.Contains('.', StringComparison.Ordinal) ? null : entry;
    }

    private static void CopyTree(string source, string target, Func<string, bool> include)
    {
        foreach (var dir in Directory.EnumerateDirectories(source, "*", SearchOption.AllDirectories))
        {
            var rel = Path.GetRelativePath(source, dir);
            if (include(rel) && new FileInfo(dir).LinkTarget is null)
            {
                Directory.CreateDirectory(Path.Combine(target, rel));
            }
        }
        Directory.CreateDirectory(target);
        foreach (var file in Directory.EnumerateFiles(source, "*", SearchOption.AllDirectories))
        {
            var rel = Path.GetRelativePath(source, file);
            var dir = Path.GetDirectoryName(rel) ?? "";
            if (include(rel) && (dir.Length == 0 || Directory.Exists(Path.Combine(target, dir))) && new FileInfo(file).LinkTarget is null)
            {
                File.Copy(file, Path.Combine(target, rel));
            }
        }
    }
}
