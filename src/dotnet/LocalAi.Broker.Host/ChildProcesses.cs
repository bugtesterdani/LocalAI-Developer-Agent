using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;

namespace LocalAi.Broker.Host;

public sealed class ChildProcessException(string message) : Exception(message);

/// <summary>
/// Startet Kindprozesse (Fake-Agent, Entwicklungs-Worker) mit eigener, sofort geschlossener
/// Standardeingabe, UTF-8-Ausgaben und Protokolldatei, und beendet sie samt Kindprozessen
/// beim Herunterfahren des Brokers.
/// </summary>
public sealed class ChildProcesses
{
    private static readonly SemaphoreSlim LogGate = new(1, 1);
    private readonly ConcurrentDictionary<int, (Process Process, string Log)> _running = new();

    public Process Start(ProcessCommand command, IEnumerable<string> extraArguments, IReadOnlyDictionary<string, string> environment, string logPath)
    {
        var resolved = ResolveExecutable(command.Command);
        var arguments = command.Arguments.Concat(extraArguments).ToList();
        AppendLog(logPath, $"[Broker] Starte: {command.Command} {string.Join(' ', arguments)}");
        AppendLog(logPath, $"[Broker] Arbeitsverzeichnis: {command.WorkingDirectory}");
        AppendLog(logPath, $"[Broker] Programm: {resolved ?? "NICHT im PATH gefunden"}");

        var start = new ProcessStartInfo(resolved ?? command.Command)
        {
            WorkingDirectory = command.WorkingDirectory,
            UseShellExecute = false,
            // Eigene stdin: der Broker nutzt seine stdin als Lebenszeichen und blockiert darauf lesend.
            // Unter Windows würde ein geerbtes Handle den Start des Kindprozesses blockieren.
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            CreateNoWindow = true,
        };
        foreach (var argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }
        start.Environment.Remove("LOCALAI_UI_TOKEN");
        start.Environment["PYTHONUTF8"] = "1";
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        foreach (var (key, value) in environment)
        {
            start.Environment[key] = value;
        }

        Process process;
        try
        {
            process = Process.Start(start) ?? throw new InvalidOperationException("Process.Start lieferte keinen Prozess");
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or IOException)
        {
            AppendLog(logPath, $"[Broker] Start fehlgeschlagen: {ex.Message}");
            throw new ChildProcessException($"'{command.Command}' konnte nicht gestartet werden ({command.WorkingDirectory}): {ex.Message}");
        }
        AppendLog(logPath, $"[Broker] Prozess gestartet, PID {process.Id}");
        process.StandardInput.Close();
        _running[process.Id] = (process, logPath);
        var stdout = PumpAsync(process.StandardOutput, logPath);
        var stderr = PumpAsync(process.StandardError, logPath);
        _ = Task.Run(async () =>
        {
            await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
            await process.WaitForExitAsync().ConfigureAwait(false);
            AppendLog(logPath, $"[Broker] Prozess {process.Id} beendet, Exitcode {process.ExitCode}");
            _running.TryRemove(process.Id, out _);
        });
        return process;
    }

    /// <summary>Beendet alle noch laufenden Kindprozesse samt deren Kindprozessen.</summary>
    public void StopAll()
    {
        foreach (var (pid, (process, log)) in _running)
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                    AppendLog(log, $"[Broker] Prozess {pid} beim Beenden des Brokers gestoppt");
                }
            }
            catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
            {
            }
        }
    }

    /// <summary>Sucht ein Programm im PATH (unter Windows mit PATHEXT, z. B. uv.exe oder uv.cmd).</summary>
    public static string? ResolveExecutable(string command)
    {
        if (Path.IsPathRooted(command))
        {
            return File.Exists(command) ? command : null;
        }
        var extensions = OperatingSystem.IsWindows()
            ? (Environment.GetEnvironmentVariable("PATHEXT") ?? ".EXE;.CMD;.BAT").Split(';', StringSplitOptions.RemoveEmptyEntries)
            : [string.Empty];
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? string.Empty).Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            foreach (var extension in Path.HasExtension(command) ? [string.Empty] : extensions)
            {
                var candidate = Path.Combine(dir.Trim('"'), command + extension);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }
        return null;
    }

    public static void AppendLog(string logPath, string line)
    {
        LogGate.Wait();
        try
        {
            using var stream = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
            using var writer = new StreamWriter(stream, new UTF8Encoding(false));
            writer.WriteLine($"{DateTime.Now:HH:mm:ss.fff} {RedactSensitiveLogContent(line)}");
        }
        catch (IOException)
        {
        }
        finally
        {
            LogGate.Release();
        }
    }

    private static string RedactSensitiveLogContent(string line)
    {
        if (string.IsNullOrEmpty(line))
        {
            return line;
        }

        return System.Text.RegularExpressions.Regex.Replace(
            line,
            @"(?<=\s|^)(--proxy-password-file|--proxy-user|--ca-file)\s+\S+",
            "$1 [REDACTED]",
            System.Text.RegularExpressions.RegexOptions.CultureInvariant);
    }

    private static async Task PumpAsync(StreamReader reader, string logPath)
    {
        try
        {
            while (await reader.ReadLineAsync().ConfigureAwait(false) is { } line)
            {
                await LogGate.WaitAsync().ConfigureAwait(false);
                try
                {
                    await using var stream = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
                    await using var writer = new StreamWriter(stream, new UTF8Encoding(false));
                    await writer.WriteLineAsync(RedactSensitiveLogContent(line)).ConfigureAwait(false);
                }
                finally
                {
                    LogGate.Release();
                }
            }
        }
        catch (IOException)
        {
        }
    }
}
