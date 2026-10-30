using System.Collections.Concurrent;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Broker.Host;

/// <summary>Einstellungen für Web.Fetch (config/localai.json → web).</summary>
public sealed record WebSettings
{
    public bool Enabled { get; init; } = true;
    public long MaxBytes { get; init; } = 2 * 1024 * 1024;
    public int TimeoutSeconds { get; init; } = 15;
    public int MaxChars { get; init; } = 20_000;
    public bool LowIntegrity { get; init; } = true;
    public required ProcessCommand Command { get; init; }

    /// <summary>Nur diese Hosts (<c>*.domain</c> = Unterdomänen); leer = alle öffentlichen.</summary>
    public IReadOnlyList<string> AllowedHosts { get; init; } = [];

    /// <summary>Zusätzliche Stammzertifikate (PEM), z. B. eines TLS-aufbrechenden Firmenproxys.</summary>
    public string? CaFile { get; init; }

    /// <summary>Ausdrücklich konfigurierter HTTP-Proxy (CONNECT); Umgebungsvariablen werden nie verwendet.</summary>
    public WebProxySettings? Proxy { get; init; }

    /// <summary>Für Status und Freigabekarte: „direkt“ oder „über Proxy host:port“.</summary>
    public string RouteText => Proxy is null
        ? "direkt (eigene DNS-Prüfung, Verbindung zur geprüften Adresse)"
        : $"über Proxy {Proxy.Host}:{Proxy.Port} (CONNECT {(Proxy.ConnectByAddress ? "auf die geprüfte Adresse" : "auf den Hostnamen")}"
          + $"{(Proxy.DnsViaProxy ? ", DNS ggf. nur durch den Proxy" : "")}; TLS weiter Ende zu Ende geprüft"
          + $"{(CaFile is null ? "" : ", zusätzliches Stammzertifikat")})";
}

/// <summary>HTTP-Proxy aus <c>web.proxy</c>. Passwort nur als Datei (nicht in der Konfiguration, nicht auf der Kommandozeile).</summary>
public sealed record WebProxySettings(string Url, string Host, int Port, string? Username, string? PasswordFile, bool DnsViaProxy,
    bool ConnectByAddress)
{
    public static WebProxySettings Parse(string url, string? username, string? passwordFile, bool dnsViaProxy, bool connectByAddress)
    {
        var text = url.Trim().TrimEnd('/');
        if (!Uri.TryCreate(text, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttp || uri.UserInfo.Length > 0
            || uri.AbsolutePath != "/" || uri.Query.Length > 0 || uri.Fragment.Length > 0 || uri.Host.Length == 0
            || !text.EndsWith($":{uri.Port}", StringComparison.Ordinal))
        {
            throw new JsonException("web.proxy.url muss die Form http://host:port haben, mit Port (Benutzer in web.proxy.username, Passwort in web.proxy.passwordFile)");
        }
        var host = uri.HostNameType == UriHostNameType.IPv6 ? $"[{uri.IdnHost}]" : uri.IdnHost;
        return new WebProxySettings($"http://{host}:{uri.Port}", uri.IdnHost, uri.Port, username, passwordFile, dnsViaProxy, connectByAddress);
    }
}

/// <summary>
/// Quarantäne und freigegebene Texte der Webabrufe (Spezifikation §7). Rohbytes liegen unter web\quarantine (für die
/// Prozesse niedriger Integrität beschreibbar), der bereinigte Text samt Metadaten unter web\captures (nur Broker).
/// Aufbewahrung 7 Tage.
/// </summary>
public sealed class WebCaptureStore(string directory, WebSettings settings, IClock clock) : IWebCaptures
{
    public static readonly TimeSpan Retention = TimeSpan.FromDays(7);
    private readonly Lock _gate = new();

    public long MaxBytes => settings.MaxBytes;

    public IReadOnlyList<string> AllowedHosts => settings.AllowedHosts;

    public string QuarantineDirectory => Path.Combine(directory, "quarantine");

    public string CapturesDirectory => Path.Combine(directory, "captures");

    public WebCapture? Find(string captureId)
    {
        if (captureId.Length != 32 || !captureId.All(Uri.IsHexDigit))
        {
            return null;
        }
        var dir = Path.Combine(CapturesDirectory, captureId);
        try
        {
            if (!File.Exists(Path.Combine(dir, "capture.json")))
            {
                return null;
            }
            var capture = ContractJson.Deserialize<WebCapture>(File.ReadAllText(Path.Combine(dir, "capture.json"), Encoding.UTF8));
            var text = File.ReadAllBytes(Path.Combine(dir, "text.txt"));
            return capture with { Text = Encoding.UTF8.GetString(text) };
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    /// <summary>SHA-256 der gespeicherten Textbytes – bei jeder Inhaltsfreigabe neu berechnet (SEC-10).</summary>
    public string? CurrentTextSha(string captureId)
    {
        var file = Path.Combine(CapturesDirectory, captureId, "text.txt");
        return File.Exists(file) ? Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(file))) : null;
    }

    public void Save(WebCapture capture, byte[] text)
    {
        lock (_gate)
        {
            var dir = Path.Combine(CapturesDirectory, capture.CaptureId);
            Directory.CreateDirectory(dir);
            File.WriteAllBytes(Path.Combine(dir, "text.txt"), text);
            File.WriteAllText(Path.Combine(dir, "capture.json"), ContractJson.Serialize(capture with { Text = null }), new UTF8Encoding(false));
        }
    }

    public void MarkReleased(string captureId, string recipient)
    {
        lock (_gate)
        {
            var file = Path.Combine(CapturesDirectory, captureId, "capture.json");
            var capture = ContractJson.Deserialize<WebCapture>(File.ReadAllText(file, Encoding.UTF8));
            var released = (capture.ReleasedTo ?? []).Append($"{recipient} {clock.UtcNow:yyyy-MM-ddTHH:mm:ssZ}").ToList();
            File.WriteAllText(file, ContractJson.Serialize(capture with { ReleasedTo = released, Text = null }), new UTF8Encoding(false));
        }
    }

    /// <summary>Rohquarantäne und Texte älter als 7 Tage löschen.</summary>
    public void Cleanup()
    {
        foreach (var root in new[] { QuarantineDirectory, CapturesDirectory })
        {
            if (!Directory.Exists(root))
            {
                continue;
            }
            foreach (var dir in Directory.GetDirectories(root))
            {
                try
                {
                    if (clock.UtcNow - Directory.GetLastWriteTimeUtc(dir) > Retention)
                    {
                        LocalAi.Sandbox.FileTools.DeleteTree(dir);
                    }
                }
                catch (IOException)
                {
                }
            }
        }
    }
}

/// <summary>
/// Führt Web.Fetch (Fetcher + Content Gate als getrennte Python-Prozesse) und Web.Content (Text an genau einen Agenten)
/// aus. Der Text eines Abrufs erscheint nie im Ergebnis von Web.Fetch (SEC-09).
/// </summary>
public sealed class WebToolAdapter(WebCaptureStore store, WebSettings settings, string logPath) : IToolAdapter
{
    private static readonly string[] DroppedEnvironment =
    [
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
        "LOCALAI_UI_TOKEN", "LOCALAI_ORCHESTRATOR_TOKEN",
    ];

    private readonly ConcurrentDictionary<int, Process> _running = new();

    public Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken) =>
        proposal.Tool switch
        {
            "Web.Fetch" => FetchAsync(proposal, invocationId, cancellationToken),
            "Web.Content" => Task.FromResult(Content(proposal, invocationId)),
            _ => Task.FromResult(Failed(proposal, invocationId, $"Unbekanntes Web-Tool {proposal.Tool}")),
        };

    /// <summary>Not-Aus: laufende Abrufe samt Kindprozessen beenden.</summary>
    public int KillAll()
    {
        var killed = 0;
        foreach (var (_, process) in _running)
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                    killed++;
                }
            }
            catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
            {
            }
        }
        return killed;
    }

    private async Task<ToolResult> FetchAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken)
    {
        string url;
        try
        {
            (url, var host) = UrlPolicy.Validate(proposal.Arguments.GetProperty("url").GetString());
            if (!UrlPolicy.HostAllowed(host, settings.AllowedHosts))
            {
                return Failed(proposal, invocationId, $"Host {host} steht nicht in web.allowedHosts");
            }
        }
        catch (BrokerException ex)
        {
            return Failed(proposal, invocationId, ex.Message);
        }
        var captureId = Guid.NewGuid().ToString("N");
        var work = Path.Combine(store.QuarantineDirectory, captureId);
        Directory.CreateDirectory(work);
        var arguments = new List<string>
        {
            "run", "--url", url, "--work", work, "--max-bytes", settings.MaxBytes.ToString(System.Globalization.CultureInfo.InvariantCulture),
            "--timeout", settings.TimeoutSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture),
            "--max-chars", settings.MaxChars.ToString(System.Globalization.CultureInfo.InvariantCulture),
        };
        if (!settings.LowIntegrity)
        {
            arguments.Add("--no-low-integrity");
        }
        if (settings.CaFile is not null)
        {
            arguments.AddRange(["--ca-file", settings.CaFile]);
        }
        if (settings.Proxy is { } proxy)
        {
            arguments.AddRange(["--proxy", proxy.Url]);
            if (proxy.Username is not null)
            {
                arguments.AddRange(["--proxy-user", proxy.Username]);
            }
            if (proxy.PasswordFile is not null)
            {
                arguments.AddRange(["--proxy-password-file", proxy.PasswordFile]);
            }
            if (proxy.DnsViaProxy)
            {
                arguments.Add("--dns-via-proxy");
            }
            if (proxy.ConnectByAddress)
            {
                arguments.Add("--connect-by-address");
            }
        }
        var exit = await RunAsync(arguments, TimeSpan.FromSeconds(settings.TimeoutSeconds * 2 + 120), cancellationToken).ConfigureAwait(false);
        if (exit is not null)
        {
            return Failed(proposal, invocationId, exit);
        }
        try
        {
            using var doc = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(work, "result.json"), cancellationToken).ConfigureAwait(false));
            var root = doc.RootElement;
            var fetch = root.TryGetProperty("fetch", out var f) ? f : default;
            string Str(JsonElement e, string name) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString()! : "";
            long Num(JsonElement e, string name) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt64() : 0;
            var error = Str(root, "error");
            if (error.Length == 0)
            {
                error = Str(fetch, "error");
            }
            if (error.Length > 0)
            {
                return Failed(proposal, invocationId, $"Abruf von {url} fehlgeschlagen: {error}");
            }
            var redirect = Str(fetch, "redirect");
            if (redirect.Length > 0)
            {
                var info = JsonSerializer.Serialize(new { redirect, status = Num(fetch, "status"), url });
                return Ok(proposal, invocationId, [new TargetResult("capture", ResultStatus.Succeeded, Text: info, ContentSha256: CanonicalJson.Sha256Hex(info),
                    Message: $"Weiterleitung angehalten (HTTP {Num(fetch, "status")}) nach {redirect} – für diese URL ist ein neuer, eigener Vorschlag nötig")]);
            }
            var gate = root.GetProperty("gate");
            var textBytes = await File.ReadAllBytesAsync(Path.Combine(work, "text.txt"), cancellationToken).ConfigureAwait(false);
            var textSha = Convert.ToHexStringLower(SHA256.HashData(textBytes));
            if (textSha != Str(gate, "text_sha256"))
            {
                return Failed(proposal, invocationId, "Content Gate: Text passt nicht zum gemeldeten Hash – verworfen");
            }
            var warnings = gate.TryGetProperty("warnings", out var w) && w.ValueKind == JsonValueKind.Array
                ? w.EnumerateArray().Take(50).Select(x => new WebWarning(Clip(Str(x, "kind"), 40), Clip(Str(x, "message"), 300), Clip(Str(x, "excerpt"), 300))).ToList()
                : [];
            var capture = new WebCapture(captureId, Str(fetch, "url"), Str(fetch, "host"), Str(fetch, "address"), Str(fetch, "fetched_at"),
                Num(fetch, "status"), Str(fetch, "content_type"), Num(fetch, "raw_bytes"), Str(fetch, "raw_sha256"), textSha,
                Str(gate, "sanitizer_version"), Num(gate, "chars"), gate.TryGetProperty("truncated", out var t) && t.ValueKind == JsonValueKind.True,
                Num(gate, "links"), warnings, Title: Clip(Str(gate, "title"), 300), Request: Clip(Str(fetch, "request"), 4000),
                Proxy: Str(fetch, "proxy") is { Length: > 0 } via ? via : null, TlsIssuer: Clip(Str(fetch, "tls_issuer"), 200),
                ResponseHeaders: fetch.TryGetProperty("headers", out var h) && h.ValueKind == JsonValueKind.Object
                    ? h.EnumerateObject().Take(40).Select(x => Clip($"{x.Name}: {(x.Value.ValueKind == JsonValueKind.String ? x.Value.GetString() : x.Value.ToString())}", 300)).ToList()
                    : null);
            store.Save(capture, textBytes);
            var summary = ContractJson.Serialize(capture);
            return Ok(proposal, invocationId, [new TargetResult("capture", ResultStatus.Succeeded, Text: summary, ContentSha256: CanonicalJson.Sha256Hex(summary),
                Message: $"Seite abgerufen: {capture.Chars} Zeichen, {warnings.Count} Hinweis(e) – der Text wartet auf deine eigene Freigabe")]);
        }
        catch (Exception ex) when (ex is IOException or JsonException or KeyNotFoundException or InvalidOperationException)
        {
            return Failed(proposal, invocationId, $"Ergebnis des Abrufs unlesbar: {ex.Message}");
        }
    }

    private ToolResult Content(ToolProposal proposal, string invocationId)
    {
        var captureId = proposal.Arguments.GetProperty("captureId").GetString()!;
        var expected = proposal.Arguments.GetProperty("textSha256").GetString()!;
        var capture = store.Find(captureId);
        if (capture?.Text is null)
        {
            return Failed(proposal, invocationId, "Webinhalt nicht mehr vorhanden – erneut abrufen");
        }
        var current = store.CurrentTextSha(captureId);
        if (current != expected || capture.TextSha256 != expected)
        {
            return Failed(proposal, invocationId, "Hashfehler: Text hat sich nach der Freigabe verändert – nicht übergeben, neue Prüfung nötig (SEC-10)");
        }
        store.MarkReleased(captureId, proposal.Agent.ToString());
        return Ok(proposal, invocationId, [new TargetResult("text", ResultStatus.Succeeded, Text: capture.Text, ContentSha256: current,
            Message: $"Webinhalt von {capture.Url} (untrusted, SHA-256 {current[..12]}) an {proposal.Agent} übergeben")]);
    }

    private static string BuildSafeArgumentPreview(IReadOnlyList<string> args, int take)
    {
        var redacted = args.ToList();
        static bool IsSensitiveSwitch(string arg) =>
            arg is "--proxy-password-file" or "--proxy-user" or "--ca-file";

        for (var i = 0; i < redacted.Count; i++)
        {
            var current = redacted[i];
            if (IsSensitiveSwitch(current))
            {
                if (i + 1 < redacted.Count)
                {
                    redacted[i + 1] = "***";
                    i++;
                }
                continue;
            }

            if (current.StartsWith("--proxy-password-file=", StringComparison.Ordinal)
                || current.StartsWith("--proxy-user=", StringComparison.Ordinal)
                || current.StartsWith("--ca-file=", StringComparison.Ordinal))
            {
                var key = current.Split('=', 2)[0];
                redacted[i] = $"{key}=***";
            }
        }

        return string.Join(' ', redacted.Take(take));
    }

    private async Task<string?> RunAsync(IReadOnlyList<string> extra, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var command = settings.Command;
        var start = new ProcessStartInfo(ChildProcesses.ResolveExecutable(command.Command) ?? command.Command)
        {
            WorkingDirectory = command.WorkingDirectory,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            CreateNoWindow = true,
        };
        foreach (var argument in command.Arguments.Concat(extra))
        {
            start.ArgumentList.Add(argument);
        }
        foreach (var name in DroppedEnvironment)
        {
            start.Environment.Remove(name);
        }
        start.Environment["PYTHONUTF8"] = "1";
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        Process process;
        try
        {
            process = Process.Start(start) ?? throw new InvalidOperationException("kein Prozess");
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            return $"Web-Fetcher konnte nicht gestartet werden ({command.Command}): {ex.Message}";
        }
        _running[process.Id] = process;
        try
        {
            process.StandardInput.Close();
            var stdout = process.StandardOutput.ReadToEndAsync(cancellationToken);
            var stderr = process.StandardError.ReadToEndAsync(cancellationToken);
            using var limit = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            limit.CancelAfter(timeout);
            try
            {
                await process.WaitForExitAsync(limit.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                process.Kill(entireProcessTree: true);
                ChildProcesses.AppendLog(logPath, "[Web] Abruf abgebrochen (Zeitgrenze oder Not-Aus)");
                return cancellationToken.IsCancellationRequested ? "Abruf abgebrochen (Not-Aus oder Abbruch)" : "Abruf abgebrochen (Zeitgrenze)";
            }
            var output = (await stdout.ConfigureAwait(false)) + (await stderr.ConfigureAwait(false));
            var safeArgsPreview = BuildSafeArgumentPreview(extra, 3);
            ChildProcesses.AppendLog(logPath, $"[Web] {safeArgsPreview} → Exitcode {process.ExitCode}: {Clip(output, 2000)}");
            return process.ExitCode == 0 ? null : $"Web-Fetcher endete mit Exitcode {process.ExitCode}: {Clip(output, 500)}";
        }
        finally
        {
            _running.TryRemove(process.Id, out _);
            process.Dispose();
        }
    }

    private static string Clip(string text, int max) => text.Length <= max ? text : text[..max] + " …";

    private static ToolResult Ok(ToolProposal proposal, string invocationId, IReadOnlyList<TargetResult> results) =>
        new(invocationId, proposal.ProposalId, ResultStatus.Succeeded, proposal.SnapshotId, results,
            CanonicalJson.Sha256Hex(results.Select(r => r.ContentSha256 ?? "").ToArray()), Truncated: false, Untrusted: true);

    private static ToolResult Failed(ToolProposal proposal, string invocationId, string message) =>
        new(invocationId, proposal.ProposalId, ResultStatus.Failed, proposal.SnapshotId,
            [new TargetResult("*", ResultStatus.Failed, Message: message)], CanonicalJson.Sha256Hex(message), Truncated: false, Untrusted: true);
}

/// <summary>Leitet Tools an den zuständigen Adapter: Web.* an den Web-Fetcher, Übernahme an das Staging, sonst Sandbox.</summary>
public sealed class RoutingToolAdapter(IToolAdapter sandbox, WebToolAdapter? web, LocalAi.Sandbox.ExportStaging? export) : IToolAdapter
{
    public async Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken)
    {
        if (proposal.Tool.StartsWith("Web.", StringComparison.Ordinal) && web is not null)
        {
            return await web.ExecuteAsync(proposal, invocationId, cancellationToken).ConfigureAwait(false);
        }
        if (proposal.Tool == "Workspace.ExportPatch" && export is not null)
        {
            return await export.ExecuteAsync(proposal, invocationId, cancellationToken).ConfigureAwait(false);
        }
        var result = await sandbox.ExecuteAsync(proposal, invocationId, cancellationToken).ConfigureAwait(false);
        return proposal.Tool == "Workspace.Diff" && export is not null ? export.Stage(proposal, result) : result;
    }
}
