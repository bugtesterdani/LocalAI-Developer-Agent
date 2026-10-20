using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Sandbox;

public sealed class ToolchainException(string message) : Exception(message);

/// <summary>Prüft toolchain.json streng: feste Pfade, deklarierte Binaries, Aktionen ohne freie Kommandos.</summary>
public static class ToolchainValidator
{
    public const string ManifestFile = "toolchain.json";
    public const string GuestRoot = @"C:\Toolchains\";

    /// <summary>Installationsort im Linux-Gast (M8): /opt/toolchains/&lt;name&gt;, nur für root beschreibbar.</summary>
    public const string LinuxGuestRoot = "/opt/toolchains/";

    /// <summary>guestPath, den ein Manifest für sein Gastsystem haben muss.</summary>
    public static string ExpectedGuestPath(ToolchainManifest m) =>
        m.Guest == GuestOs.Linux ? LinuxGuestRoot + m.Name : GuestRoot + m.Name;

    /// <summary>Platzhalter, die der Host bzw. die Provisionierung selbst einsetzt.</summary>
    public static readonly IReadOnlySet<string> FixedPlaceholders =
        new HashSet<string>(StringComparer.Ordinal) { "guest", "source", "selftest", "work", "results", "packages" };

    public static readonly IReadOnlySet<string> ActionNames =
        new HashSet<string>(StringComparer.Ordinal) { "restore", "build", "test", "run", "lint", "format" };

    private static readonly Regex Name = new("^[a-z][a-z0-9-]{0,31}$");
    private static readonly Regex Version = new("^[0-9A-Za-z][0-9A-Za-z.+-]{0,39}$");
    private static readonly Regex Identifier = new("^[a-z][a-zA-Z0-9]{0,31}$");
    private static readonly Regex EnvName = new("^[A-Z_][A-Z0-9_]{0,63}$");
    private static readonly Regex Placeholder = new(@"\{([A-Za-z0-9_]*)\}");
    private static readonly Regex Hex64 = new("^[0-9a-f]{64}$");
    private static readonly HashSet<string> BlockedEnv = new(StringComparer.Ordinal)
    {
        "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "PSMODULEPATH", "LD_PRELOAD", "LD_AUDIT",
    };

    /// <summary>Präfix für Programme aus Windows selbst (C:\Windows\System32) – nur die hier erlaubten.</summary>
    public const string SystemPrefix = @"{system}\";

    /// <summary>Programme aus System32, die Toolchains nutzen dürfen: cmd (Batch) und Windows PowerShell 5.1.</summary>
    public static readonly IReadOnlySet<string> SystemBinaries =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "cmd.exe", @"WindowsPowerShell\v1.0\powershell.exe" };

    /// <summary>Präfix für Programme aus dem Linux-Gast selbst (/usr/bin) – nur die hier erlaubten.</summary>
    public const string LinuxSystemPrefix = "{system}/";

    /// <summary>Programme aus /usr/bin, die Linux-Toolchains nutzen dürfen: bash, sh und das Python des Debian-Abbilds.</summary>
    public static readonly IReadOnlySet<string> LinuxSystemBinaries =
        new HashSet<string>(StringComparer.Ordinal) { "bash", "sh", "python3" };

    /// <summary>Archivformate je Gast: Windows nur ZIP, Linux auch tar.gz/tar.xz (Rechte und Verknüpfungen bleiben erhalten).</summary>
    public static bool IsArchive(string file, GuestOs guest) =>
        file.EndsWith(".zip", StringComparison.OrdinalIgnoreCase)
        || (guest == GuestOs.Linux && (file.EndsWith(".tar.gz", StringComparison.Ordinal) || file.EndsWith(".tgz", StringComparison.Ordinal)
                                       || file.EndsWith(".tar.xz", StringComparison.Ordinal)));

    public const int MaxThenSteps = 5;
    public const int MaxDeploy = 20;

    public static ToolchainManifest Parse(string json) => ContractJson.Deserialize<ToolchainManifest>(json);

    /// <summary>Alle Probleme in deutscher Sprache; leer = gültig.</summary>
    public static List<string> Validate(ToolchainManifest m, Func<string, bool> fileExists) =>
        Validate(m, fileExists, _ => false);

    /// <summary>Wie oben; <paramref name="directoryExists"/> prüft Ordner im Toolchain-Ordner (für deploy).</summary>
    public static List<string> Validate(ToolchainManifest m, Func<string, bool> fileExists, Func<string, bool> directoryExists)
    {
        var problems = new List<string>();
        void Check(bool ok, string message)
        {
            if (!ok)
            {
                problems.Add(message);
            }
        }

        Check(m.SchemaVersion == 1, "schemaVersion muss 1 sein");
        Check(Name.IsMatch(m.Name), "name: nur Kleinbuchstaben, Ziffern und '-' (z. B. dotnet)");
        Check(Version.IsMatch(m.Version), "version ungültig");
        Check(!string.IsNullOrWhiteSpace(m.DisplayName) && m.DisplayName.Length <= 80, "displayName fehlt oder ist zu lang");
        var linux = m.Guest == GuestOs.Linux;
        Check(string.Equals(m.GuestPath, ExpectedGuestPath(m), StringComparison.Ordinal), $"guestPath muss {ExpectedGuestPath(m)} sein");
        Check(!m.License.Proprietary || !string.IsNullOrWhiteSpace(m.License.Note), "Proprietäre Toolchain braucht einen Lizenzhinweis (license.note)");
        foreach (var file in m.License.Files ?? [])
        {
            Check(RelativeFile(file, fileExists), $"Lizenzdatei fehlt: {file}");
        }

        var binaries = new HashSet<string>(StringComparer.Ordinal);
        Check(m.Binaries.Count is > 0 and <= 20, "binaries: 1 bis 20 Einträge");
        foreach (var b in m.Binaries)
        {
            Check(Identifier.IsMatch(b.Name), $"Binary-Name ungültig: {b.Name}");
            Check(binaries.Add(b.Name), $"Binary doppelt: {b.Name}");
            if (linux)
            {
                Check(IsLinuxSystemBinary(b.Path) || (IsRelative(b.Path) && !b.Path.Contains('{') && !b.Path.Contains('\\')),
                    $"Binary {b.Name}: path muss ein relativer Pfad mit '/' sein oder {LinuxSystemPrefix}bash, {LinuxSystemPrefix}sh bzw. {LinuxSystemPrefix}python3");
                Check(b.Sha256 is null || !IsLinuxSystemBinary(b.Path), $"Binary {b.Name}: sha256 nicht für Programme des Gastsystems");
            }
            else
            {
                Check(IsSystemBinary(b.Path) || (IsRelative(b.Path) && !b.Path.Contains('{') && b.Path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)),
                    $"Binary {b.Name}: path muss eine relative .exe sein oder {SystemPrefix}cmd.exe bzw. {SystemPrefix}WindowsPowerShell\\v1.0\\powershell.exe");
                Check(b.Sha256 is null || !IsSystemBinary(b.Path), $"Binary {b.Name}: sha256 nicht für Windows-Programme (ändern sich mit Updates)");
            }
            Check(b.Sha256 is null || Hex64.IsMatch(b.Sha256), $"Binary {b.Name}: sha256 ungültig");
        }

        switch (m.Install.Kind)
        {
            case ToolchainInstallKind.Archive:
                Check(m.Install.File is not null && IsArchive(m.Install.File, m.Guest ?? GuestOs.Windows) && RelativeFile(m.Install.File, fileExists),
                    linux ? $"install.file: Archiv (.zip, .tar.gz, .tgz, .tar.xz) im Toolchain-Ordner fehlt ({m.Install.File})"
                          : $"install.file: ZIP-Archiv im Toolchain-Ordner fehlt ({m.Install.File})");
                Check(m.Install.Arguments is null || m.Install.Arguments.Count == 0, "install.arguments gibt es nur bei Installern");
                break;
            case ToolchainInstallKind.Installer:
                if (linux)
                {
                    // Linux: Debian-Pakete (dpkg -i, offline, ohne Abhängigkeitsauflösung) – keine weiteren Argumente.
                    Check(m.Install.File is not null && m.Install.File.EndsWith(".deb", StringComparison.Ordinal) && RelativeFile(m.Install.File, fileExists),
                        $"install.file: Debian-Paket (.deb) im Toolchain-Ordner fehlt ({m.Install.File})");
                    Check(m.Install.Arguments is null || m.Install.Arguments.Count == 0, "install.arguments gibt es für .deb-Pakete nicht");
                    break;
                }
                Check(m.Install.File is not null && (m.Install.File.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) || m.Install.File.EndsWith(".msi", StringComparison.OrdinalIgnoreCase))
                      && RelativeFile(m.Install.File, fileExists), $"install.file: Installer (.exe/.msi) im Toolchain-Ordner fehlt ({m.Install.File})");
                foreach (var a in m.Install.Arguments ?? [])
                {
                    CheckFixedPlaceholders(a, "install.arguments", problems);
                }
                break;
            case ToolchainInstallKind.None:
                Check(m.Install.File is null, "install.file nur bei Archive/Installer");
                break;
        }
        Check(m.Install.ExpectedMinutes is > 0 and <= 240, "install.expectedMinutes: 1 bis 240");
        CheckCommand(new ToolchainCommand(m.Install.Check.Binary, m.Install.Check.Arguments), "install.check", binaries, problems);
        foreach (var (c, i) in (m.PostInstall ?? []).Select((c, i) => (c, i)))
        {
            CheckCommand(c, $"postInstall[{i}]", binaries, problems);
        }
        var deploy = m.Deploy ?? [];
        Check(deploy.Count <= MaxDeploy, $"deploy: höchstens {MaxDeploy} Einträge");
        var directories = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (d, i) in deploy.Select((d, i) => (d, i)))
        {
            var isFile = RelativeFile(d.Source, fileExists);
            var isDir = IsRelative(d.Source) && directoryExists(d.Source);
            Check(isFile || isDir, $"deploy[{i}]: Quelle fehlt im Toolchain-Ordner ({d.Source})");
            Check(IsRelative(d.Target) && d.Target != ".", $"deploy[{i}]: Ziel muss ein relativer Pfad unter guestPath sein ({d.Target})");
            Check(!string.Equals(d.Source.Replace('\\', '/'), ManifestFile, StringComparison.OrdinalIgnoreCase), $"deploy[{i}]: toolchain.json wird nicht kopiert");
        }
        if (m.SelfTest is { } selfTest)
        {
            Check(IsRelative(selfTest.Directory), "selfTest.directory muss ein relativer Ordner sein");
            Check(selfTest.Steps.Count is > 0 and <= 10, "selfTest.steps: 1 bis 10 Schritte");
            Check(!string.IsNullOrEmpty(selfTest.ExpectOutput), "selfTest.expectOutput fehlt");
            foreach (var (c, i) in selfTest.Steps.Select((c, i) => (c, i)))
            {
                CheckCommand(c, $"selfTest.steps[{i}]", binaries, problems);
            }
        }

        foreach (var (name, action) in m.Actions)
        {
            if (!ActionNames.Contains(name))
            {
                problems.Add($"Unbekannte Aktion: {name} (erlaubt: {string.Join(", ", ActionNames)})");
                continue;
            }
            Check(binaries.Contains(action.Binary), $"Aktion {name}: Binary {action.Binary} nicht deklariert");
            Check(action.TimeoutSeconds is null or (> 0 and <= 3600), $"Aktion {name}: timeoutSeconds 1 bis 3600");
            var steps = new List<ToolchainCommand> { new(action.Binary, action.Arguments) };
            Check((action.Then?.Count ?? 0) <= MaxThenSteps, $"Aktion {name}: then höchstens {MaxThenSteps} Schritte");
            foreach (var (c, i) in (action.Then ?? []).Select((c, i) => (c, i)))
            {
                Check(binaries.Contains(c.Binary), $"Aktion {name}: then[{i}]: Binary {c.Binary} nicht deklariert");
                Check(c.Arguments.Count <= 40, $"Aktion {name}: then[{i}]: zu viele Argumente");
                steps.Add(c);
            }
            // Jeder Job arbeitet auf einer frischen Kopie ohne obj/-Ordner: ohne Restore im selben Aufruf fehlt project.assets.json.
            Check(!steps.Any(c => c.Arguments.Contains("--no-restore", StringComparer.OrdinalIgnoreCase)),
                $"Aktion {name}: --no-restore entfernen – jeder Job beginnt mit einer frischen Kopie (Pakete über -p:RestoreSources={{packages}})");
            var declared = action.Parameters ?? new Dictionary<string, ToolchainParameter>();
            foreach (var (pname, p) in declared)
            {
                Check(Identifier.IsMatch(pname) && !FixedPlaceholders.Contains(pname), $"Aktion {name}: Parametername ungültig: {pname}");
                Check(p.Type != ToolchainParameterType.Choice || p.Values is { Count: > 0 }, $"Aktion {name}: Parameter {pname} braucht values");
                if (p.Pattern is not null)
                {
                    try
                    {
                        _ = new Regex(p.Pattern, RegexOptions.None, TimeSpan.FromMilliseconds(100));
                    }
                    catch (ArgumentException)
                    {
                        problems.Add($"Aktion {name}: pattern von {pname} ist kein gültiger regulärer Ausdruck");
                    }
                }
            }
            var used = new HashSet<string>(StringComparer.Ordinal);
            foreach (var arg in steps.SelectMany(c => c.Arguments))
            {
                foreach (Match match in Placeholder.Matches(arg))
                {
                    var ph = match.Groups[1].Value;
                    if (FixedPlaceholders.Contains(ph))
                    {
                        continue;
                    }
                    if (!declared.ContainsKey(ph))
                    {
                        problems.Add($"Aktion {name}: Platzhalter {{{ph}}} nicht deklariert");
                    }
                    else if (arg != match.Value)
                    {
                        problems.Add($"Aktion {name}: Parameter {{{ph}}} muss ein ganzes Argument sein");
                    }
                    used.Add(ph);
                }
                CheckShellLike(arg, $"Aktion {name}", problems);
            }
            foreach (var pname in declared.Keys.Where(k => !used.Contains(k)))
            {
                problems.Add($"Aktion {name}: Parameter {pname} wird nicht verwendet");
            }
        }

        foreach (var (key, value) in m.Environment ?? new Dictionary<string, string>())
        {
            Check(EnvName.IsMatch(key) && !BlockedEnv.Contains(key), $"Umgebungsvariable nicht erlaubt: {key}");
            CheckFixedPlaceholders(value, $"environment.{key}", problems);
        }
        if (m.PackageSource is { Kind: not PackageSourceKind.None } source)
        {
            Check(source.Path is not null && IsRelative(source.Path), "packageSource.path muss ein relativer Ordner sein");
        }
        foreach (var (path, hash) in m.Files ?? new Dictionary<string, string>())
        {
            Check(IsRelative(path) && Hex64.IsMatch(hash), $"files: Eintrag ungültig: {path}");
        }
        return problems;
    }

    private static void CheckCommand(ToolchainCommand c, string where, HashSet<string> binaries, List<string> problems)
    {
        if (!binaries.Contains(c.Binary))
        {
            problems.Add($"{where}: Binary {c.Binary} nicht deklariert");
        }
        if (c.Arguments.Count > 40)
        {
            problems.Add($"{where}: zu viele Argumente");
        }
        foreach (var a in c.Arguments)
        {
            CheckFixedPlaceholders(a, where, problems);
            CheckShellLike(a, where, problems);
        }
    }

    private static void CheckFixedPlaceholders(string value, string where, List<string> problems)
    {
        foreach (Match match in Placeholder.Matches(value))
        {
            if (!FixedPlaceholders.Contains(match.Groups[1].Value))
            {
                problems.Add($"{where}: unbekannter Platzhalter {match.Value}");
            }
        }
    }

    /// <summary>Keine Zeilenumbrüche und keine Steuerzeichen in Argumenten (Schutz vor eingeschmuggelten Kommandos).</summary>
    private static void CheckShellLike(string value, string where, List<string> problems)
    {
        if (value.Any(char.IsControl) || value.Length > 1000)
        {
            problems.Add($"{where}: Argument enthält Steuerzeichen oder ist zu lang");
        }
    }

    private static bool IsRelative(string path)
    {
        try
        {
            return PathRules.NormalizeRelative(path) == path.Replace('\\', '/').TrimEnd('/');
        }
        catch (BrokerException)
        {
            return false;
        }
    }

    private static bool RelativeFile(string path, Func<string, bool> fileExists) => IsRelative(path) && fileExists(path);

    /// <summary>{system}\cmd.exe oder {system}\WindowsPowerShell\v1.0\powershell.exe (Groß-/Kleinschreibung egal).</summary>
    public static bool IsSystemBinary(string path) =>
        path.StartsWith(SystemPrefix, StringComparison.OrdinalIgnoreCase) && SystemBinaries.Contains(path[SystemPrefix.Length..].Replace('/', '\\'));

    /// <summary>{system}/bash, {system}/sh oder {system}/python3 (Linux-Gast, genau so geschrieben).</summary>
    public static bool IsLinuxSystemBinary(string path) =>
        path.StartsWith(LinuxSystemPrefix, StringComparison.Ordinal) && LinuxSystemBinaries.Contains(path[LinuxSystemPrefix.Length..]);
}

/// <summary>
/// Registrierte Toolchains (Spezifikation §8, R12): Registrierung ist eine sichtbare, bestätigte Aktion mit
/// Hashprüfung; vor jedem VM-Aufbau werden die Dateien erneut gegen die festgehaltenen Hashes geprüft.
/// </summary>
public sealed class ToolchainRegistry(string registryPath, string? toolchainsDirectory, IReadOnlyList<string> projectToolchains, IClock clock)
{
    public const int MaxFiles = 50_000;
    public const long MaxBytes = 32L * 1024 * 1024 * 1024;
    private readonly Lock _gate = new();

    public string? ToolchainsDirectory => toolchainsDirectory;

    public IReadOnlyList<string> ProjectToolchains => projectToolchains;

    public IReadOnlyList<ToolchainRegistration> Registrations
    {
        get
        {
            lock (_gate)
            {
                return Load();
            }
        }
    }

    /// <summary>Sucht Toolchain-Ordner (mit toolchain.json), berechnet Hashes und zeigt Registrierungen samt Abweichungen.</summary>
    public IReadOnlyList<ToolchainInfo> Scan(CancellationToken cancellationToken)
    {
        var registrations = Registrations;
        var result = new List<ToolchainInfo>();
        if (toolchainsDirectory is not null && Directory.Exists(toolchainsDirectory))
        {
            foreach (var dir in Directory.GetDirectories(toolchainsDirectory).Order(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(Path.Combine(dir, ToolchainValidator.ManifestFile)))
                {
                    continue;
                }
                cancellationToken.ThrowIfCancellationRequested();
                result.Add(Inspect(dir, registrations, cancellationToken).Info);
            }
        }
        foreach (var r in registrations.Where(r => result.All(i => !SamePath(i.Directory, r.Directory))))
        {
            result.Add(new ToolchainInfo(r.Name, r.Version, r.Name, r.Directory, "", 0, 0, true, false,
                ["Ordner der registrierten Toolchain nicht gefunden"], projectToolchains.Contains(r.Name), RegisteredDigest: r.Digest));
        }
        return result;
    }

    public sealed record Inspection(ToolchainInfo Info, ToolchainManifest? Manifest, string? ManifestSha256, IReadOnlyDictionary<string, string> Files);

    public Inspection Inspect(string directory, IReadOnlyList<ToolchainRegistration>? registrations, CancellationToken cancellationToken)
    {
        directory = Path.GetFullPath(directory);
        var problems = new List<string>();
        ToolchainManifest? manifest = null;
        string? manifestSha = null;
        var files = new SortedDictionary<string, string>(StringComparer.Ordinal);
        long total = 0;
        try
        {
            var manifestBytes = File.ReadAllBytes(Path.Combine(directory, ToolchainValidator.ManifestFile));
            manifestSha = Convert.ToHexStringLower(SHA256.HashData(manifestBytes));
            var text = Encoding.UTF8.GetString(manifestBytes).TrimStart('\uFEFF');
            manifest = ToolchainValidator.Parse(text);
            // Der Worker im Gast liest das Manifest streng (Pydantic): Gastsystem genau so schreiben.
            using var doc = JsonDocument.Parse(text);
            if (doc.RootElement.TryGetProperty("guest", out var guest) && guest.GetString() is not ("Windows" or "Linux"))
            {
                problems.Add("guest muss \"Windows\" oder \"Linux\" sein (genau so geschrieben)");
            }
        }
        catch (Exception ex) when (ex is JsonException or IOException or UnauthorizedAccessException)
        {
            problems.Add($"toolchain.json ungültig: {GuestReport.Clip(ex.Message, 200)}");
        }
        try
        {
            foreach (var entry in new DirectoryInfo(directory).EnumerateFileSystemInfos("*", new EnumerationOptions
                     {
                         RecurseSubdirectories = true,
                         AttributesToSkip = 0,
                         IgnoreInaccessible = false,
                     }))
            {
                cancellationToken.ThrowIfCancellationRequested();
                var rel = Path.GetRelativePath(directory, entry.FullName).Replace('\\', '/');
                if (entry.LinkTarget is not null || entry.Attributes.HasFlag(FileAttributes.ReparsePoint))
                {
                    problems.Add($"Verknüpfung nicht erlaubt: {rel}");
                    continue;
                }
                if (entry is not FileInfo file || rel == ToolchainValidator.ManifestFile)
                {
                    continue;
                }
                if (files.Count >= MaxFiles || (total += file.Length) > MaxBytes)
                {
                    problems.Add($"Toolchain zu groß (höchstens {MaxFiles} Dateien bzw. {MaxBytes / (1024 * 1024 * 1024)} GiB)");
                    break;
                }
                files[rel] = HashFile(file.FullName);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            problems.Add($"Ordner nicht lesbar: {GuestReport.Clip(ex.Message, 200)}");
        }

        if (manifest is not null)
        {
            problems.AddRange(ToolchainValidator.Validate(manifest, rel => files.ContainsKey(rel.Replace('\\', '/')),
                rel =>
                {
                    var prefix = rel.Replace('\\', '/').TrimEnd('/') + "/";
                    return files.Keys.Any(k => k.StartsWith(prefix, StringComparison.Ordinal));
                }));
            foreach (var (rel, expected) in manifest.Files ?? new Dictionary<string, string>())
            {
                var key = rel.Replace('\\', '/');
                if (!files.TryGetValue(key, out var actual))
                {
                    problems.Add($"Datei aus toolchain.json fehlt: {rel}");
                }
                else if (!string.Equals(actual, expected, StringComparison.OrdinalIgnoreCase))
                {
                    problems.Add($"Hash weicht ab: {rel}");
                }
            }
        }

        var name = manifest?.Name ?? Path.GetFileName(directory);
        var version = manifest?.Version ?? "?";
        var digest = Digest(name, version, manifestSha ?? "", files);
        var registration = registrations?.FirstOrDefault(r => SamePath(r.Directory, directory) || (manifest is not null && r.Name == manifest.Name));
        var registered = registration is not null && SamePath(registration.Directory, directory);
        if (registered && registration!.Digest != digest)
        {
            problems.Add("Dateien haben sich seit der Registrierung verändert – erneut prüfen und registrieren");
        }
        var info = new ToolchainInfo(name, version, manifest?.DisplayName ?? name, directory, digest, files.Count, total, registered,
            manifest?.License.Proprietary ?? false, problems, projectToolchains.Contains(name),
            RegisteredDigest: registered ? registration!.Digest : null, LicenseNote: manifest?.License.Note, Guest: manifest?.Guest ?? GuestOs.Windows);
        return new Inspection(info, manifest, manifestSha, files);
    }

    /// <summary>Registriert den Ordner, wenn er unverändert dem angezeigten Stand (<paramref name="expectedDigest"/>) entspricht.</summary>
    public ToolchainRegistration Register(string directory, string expectedDigest, CancellationToken cancellationToken)
    {
        var full = Path.GetFullPath(directory);
        if (toolchainsDirectory is null || !SamePath(Path.GetDirectoryName(full) ?? "", Path.GetFullPath(toolchainsDirectory)))
        {
            throw new ToolchainException("Nur Ordner direkt im konfigurierten Toolchain-Ordner können registriert werden");
        }
        var inspection = Inspect(full, null, cancellationToken);
        if (inspection.Info.Problems.Count > 0 || inspection.Manifest is null)
        {
            throw new ToolchainException("Toolchain nicht registrierbar: " + string.Join("; ", inspection.Info.Problems));
        }
        if (inspection.Info.Digest != expectedDigest)
        {
            throw new ToolchainException("Der Ordner hat sich seit der Anzeige verändert – bitte erneut prüfen");
        }
        var registration = new ToolchainRegistration(inspection.Manifest.Name, inspection.Manifest.Version, full, inspection.ManifestSha256!,
            inspection.Info.Digest, new Dictionary<string, string>(inspection.Files), ContractTime.ToContract(clock.UtcNow));
        lock (_gate)
        {
            var list = Load().Where(r => r.Name != registration.Name).Append(registration).OrderBy(r => r.Name, StringComparer.Ordinal).ToList();
            Save(list);
        }
        return registration;
    }

    /// <summary>Gastsystem laut Manifest einer registrierten Toolchain; null = nicht registriert oder nicht lesbar.</summary>
    public GuestOs? GuestOf(string name)
    {
        var r = Registrations.FirstOrDefault(x => x.Name == name);
        if (r is null)
        {
            return null;
        }
        try
        {
            var manifest = ToolchainValidator.Parse(File.ReadAllText(Path.Combine(r.Directory, ToolchainValidator.ManifestFile)).TrimStart('\uFEFF'));
            return manifest.Guest ?? GuestOs.Windows;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return null;
        }
    }

    public bool Unregister(string name)
    {
        lock (_gate)
        {
            var list = Load();
            var remaining = list.Where(r => r.Name != name).ToList();
            Save(remaining);
            return remaining.Count != list.Count;
        }
    }

    /// <summary>
    /// Für den VM-Aufbau: kopiert die vom Projekt gewählten Toolchains nach <paramref name="stagingDirectory"/> und prüft
    /// die Kopie gegen die bei der Registrierung festgehaltenen Hashes (SEC-19/SEC-24). Liefert die Namen in Projektreihenfolge.
    /// </summary>
    public IReadOnlyList<string> Stage(string stagingDirectory, CancellationToken cancellationToken, IReadOnlyList<string>? only = null)
    {
        var registrations = Registrations;
        var staged = new List<string>();
        foreach (var name in projectToolchains.Where(n => only is null || only.Contains(n)))
        {
            var r = registrations.FirstOrDefault(x => x.Name == name)
                    ?? throw new ToolchainException($"Toolchain '{name}' ist für das Projekt gewählt, aber nicht registriert " +
                        $"(registriert: {(registrations.Count == 0 ? "keine" : string.Join(", ", registrations.Select(x => x.Name)))}; " +
                        $"Registrierungsdatei: {registryPath}) – in der Oberfläche unter TOOLCHAINS prüfen und registrieren");
            var target = Path.Combine(stagingDirectory, name);
            Directory.CreateDirectory(target);
            var source = Path.GetFullPath(r.Directory);
            var expected = new HashSet<string>(r.Files.Keys, StringComparer.Ordinal) { ToolchainValidator.ManifestFile };
            foreach (var rel in expected)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var from = Path.Combine(source, rel.Replace('/', Path.DirectorySeparatorChar));
                var to = Path.Combine(target, rel.Replace('/', Path.DirectorySeparatorChar));
                Directory.CreateDirectory(Path.GetDirectoryName(to)!);
                if (!File.Exists(from) || new FileInfo(from).LinkTarget is not null)
                {
                    throw new ToolchainException($"Toolchain '{name}': Datei fehlt: {rel}");
                }
                File.Copy(from, to, overwrite: true);
            }
            // Geprüft wird die Kopie – sie ist es, die in die VM geht.
            var manifestSha = HashFile(Path.Combine(target, ToolchainValidator.ManifestFile));
            if (manifestSha != r.ManifestSha256)
            {
                throw new ToolchainException($"Toolchain '{name}': toolchain.json wurde seit der Registrierung verändert");
            }
            foreach (var (rel, hash) in r.Files)
            {
                if (HashFile(Path.Combine(target, rel.Replace('/', Path.DirectorySeparatorChar))) != hash)
                {
                    throw new ToolchainException($"Toolchain '{name}': Hash weicht ab: {rel} – Aufbau abgebrochen");
                }
            }
            var extra = Directory.EnumerateFiles(source, "*", SearchOption.AllDirectories)
                .Select(f => Path.GetRelativePath(source, f).Replace('\\', '/'))
                .FirstOrDefault(f => !expected.Contains(f));
            if (extra is not null)
            {
                throw new ToolchainException($"Toolchain '{name}': neue Datei seit der Registrierung: {extra}");
            }
            File.WriteAllText(Path.Combine(target, "pinned.json"),
                JsonSerializer.Serialize(new { name = r.Name, version = r.Version, files = r.Files }), new UTF8Encoding(false));
            staged.Add(name);
        }
        return staged;
    }

    public static string Digest(string name, string version, string manifestSha256, IReadOnlyDictionary<string, string> files)
    {
        var node = new System.Text.Json.Nodes.JsonObject
        {
            ["name"] = name,
            ["version"] = version,
            ["manifestSha256"] = manifestSha256,
            ["files"] = new System.Text.Json.Nodes.JsonObject(files.Select(kv => KeyValuePair.Create(kv.Key, (System.Text.Json.Nodes.JsonNode?)kv.Value))),
        };
        return CanonicalJson.Sha256Hex(CanonicalJson.CanonicalizeText(node.ToJsonString()));
    }

    private static string HashFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20, FileOptions.SequentialScan);
        return Convert.ToHexStringLower(SHA256.HashData(stream));
    }

    private static bool SamePath(string a, string b) =>
        string.Equals(Path.GetFullPath(a).TrimEnd(Path.DirectorySeparatorChar), Path.GetFullPath(b).TrimEnd(Path.DirectorySeparatorChar),
            OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);

    private List<ToolchainRegistration> Load()
    {
        if (!File.Exists(registryPath))
        {
            return [];
        }
        return ContractJson.Deserialize<List<ToolchainRegistration>>(File.ReadAllText(registryPath, Encoding.UTF8));
    }

    private void Save(List<ToolchainRegistration> list)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(registryPath))!);
        var tmp = registryPath + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(list, new JsonSerializerOptions(ContractJson.Options) { WriteIndented = true }), new UTF8Encoding(false));
        File.Move(tmp, registryPath, overwrite: true);
    }
}

/// <summary>
/// Toolchains, die für das Projekt registriert sind und beim VM-Aufbau installiert wurden (nur hyperv).
/// Das Manifest wird aus dem registrierten Ordner gelesen und gegen den bei der Registrierung festgehaltenen
/// Hash geprüft; abweichende Manifeste sind nicht verfügbar (die VM enthält dann ohnehin einen anderen Stand).
/// </summary>
public sealed class RegisteredToolchainCatalog(ToolchainRegistry registry) : IToolchainCatalog
{
    private readonly Lock _gate = new();
    private readonly Dictionary<string, ToolchainManifest> _cache = new(StringComparer.Ordinal);

    public IReadOnlyList<ToolchainManifest> Available =>
        registry.ProjectToolchains.Select(Find).OfType<ToolchainManifest>().ToList();

    public ToolchainManifest? Find(string name)
    {
        if (!registry.ProjectToolchains.Contains(name))
        {
            return null;
        }
        var registration = registry.Registrations.FirstOrDefault(r => r.Name == name);
        if (registration is null)
        {
            return null;
        }
        lock (_gate)
        {
            if (_cache.TryGetValue(registration.ManifestSha256, out var cached))
            {
                return cached;
            }
            try
            {
                var bytes = File.ReadAllBytes(Path.Combine(registration.Directory, ToolchainValidator.ManifestFile));
                if (Convert.ToHexStringLower(SHA256.HashData(bytes)) != registration.ManifestSha256)
                {
                    return null;
                }
                var manifest = ToolchainValidator.Parse(Encoding.UTF8.GetString(bytes).TrimStart('\uFEFF'));
                if (manifest.Name != name)
                {
                    return null;
                }
                _cache[registration.ManifestSha256] = manifest;
                return manifest;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
            {
                return null;
            }
        }
    }
}

/// <summary>Mindestdauer für den VM-Aufbau aus den erwarteten Installationszeiten der Projekt-Toolchains (M5).</summary>
public static class ToolchainBuildTime
{
    public static readonly TimeSpan WindowsSetup = TimeSpan.FromMinutes(60);

    /// <summary>Debian-Cloud-Abbild: Start, cloud-init, Python entpacken – deutlich schneller als Windows.</summary>
    public static readonly TimeSpan LinuxSetup = TimeSpan.FromMinutes(20);

    public static TimeSpan Minimum(IEnumerable<ToolchainRegistration> registrations, IEnumerable<string> projectToolchains, GuestOs guest = GuestOs.Windows)
    {
        var wanted = new HashSet<string>(projectToolchains, StringComparer.OrdinalIgnoreCase);
        var minutes = 0;
        foreach (var r in registrations.Where(r => wanted.Contains(r.Name)))
        {
            try
            {
                var manifest = ToolchainValidator.Parse(File.ReadAllText(Path.Combine(r.Directory, ToolchainValidator.ManifestFile)).TrimStart('\uFEFF'));
                minutes += (int)Math.Clamp(manifest.Install.ExpectedMinutes, 1, 240) * 3 + 5;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
            {
                minutes += 15;
            }
        }
        return (guest == GuestOs.Linux ? LinuxSetup : WindowsSetup) + TimeSpan.FromMinutes(minutes);
    }
}
