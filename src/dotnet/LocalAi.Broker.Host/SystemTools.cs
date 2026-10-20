using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Broker.Host;

/// <summary>
/// Versions- und Offline-Manifest (AC01, Runbook „Normaler Start“): Versionen aller Teile, Hashes der Konfiguration und
/// eine Prüfliste der Offline-Voraussetzungen (Python-Umgebung, Windows-Abbild, Toolchains, Zertifikate). Wird bei jedem
/// Start nach <c>manifest.json</c> im Datenordner geschrieben und in der Oberfläche angezeigt.
/// </summary>
public static partial class VersionManifest
{
    [GeneratedRegex(@"<!--\s*prompt:\s*(\w+)\s*·\s*version:\s*(\d+)\s*-->")]
    private static partial Regex PromptHeader();

    public static string ProductVersion =>
        typeof(VersionManifest).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion.Split('+')[0]
        ?? typeof(VersionManifest).Assembly.GetName().Version?.ToString() ?? "?";

    public static JsonObject Build(HostOptions options, ToolchainRegistry? toolchains, IReadOnlyList<string> projectToolchains)
    {
        var checks = new JsonArray();
        void Check(string item, bool ok, string detail, bool required = true) =>
            checks.Add(new JsonObject { ["item"] = item, ["ok"] = ok, ["required"] = required, ["detail"] = detail });

        var manifest = new JsonObject
        {
            ["schemaVersion"] = 1,
            ["createdAt"] = ContractTime.ToContract(DateTimeOffset.UtcNow),
            ["product"] = "Local AI Developer Agent",
            ["version"] = ProductVersion,
            ["commit"] = GitCommit(options.RepoRoot),
            ["os"] = RuntimeInformation.OSDescription,
            ["architecture"] = RuntimeInformation.OSArchitecture.ToString(),
            ["dotnet"] = Environment.Version.ToString(),
            ["mode"] = HostOptions.ModeName(options.Mode),
            ["repoRoot"] = options.RepoRoot,
            ["dataDirectory"] = options.DataDirectory,
            ["config"] = FileInfoNode(options.ConfigPath),
            ["project"] = FileInfoNode(options.ProjectPath),
            ["contractsSchemaSha256"] = FileSha(Path.Combine(options.RepoRoot, "contracts", "schemas", "contracts.schema.json")),
            ["prompts"] = Prompts(options.RepoRoot),
        };

        // Python (Orchestrator, Tools; Laufzeit des Gasts)
        var python = Python(options.RepoRoot);
        manifest["python"] = python;
        var needsPython = options.Mode is SandboxMode.DevLocalUnsafe or SandboxMode.HyperV or SandboxMode.Qemu;
        Check("Python-Umgebung (uv sync --project src/python)", python["ok"]!.GetValue<bool>(),
            python["detail"]?.GetValue<string>() ?? "", needsPython);

        // Toolchains
        var tcArray = new JsonArray();
        var registrations = toolchains?.Registrations ?? [];
        foreach (var r in registrations)
        {
            tcArray.Add(new JsonObject
            {
                ["name"] = r.Name, ["version"] = r.Version, ["digest"] = r.Digest, ["directory"] = r.Directory,
                ["present"] = Directory.Exists(r.Directory), ["files"] = r.Files.Count, ["registeredAt"] = r.RegisteredAt,
            });
        }
        manifest["toolchains"] = tcArray;
        manifest["projectToolchains"] = new JsonArray(projectToolchains.Select(t => (JsonNode?)t).ToArray());
        foreach (var name in projectToolchains)
        {
            var reg = registrations.FirstOrDefault(r => r.Name == name);
            Check($"Toolchain {name}", reg is not null && Directory.Exists(reg.Directory),
                reg is null ? "nicht registriert (Oberfläche → TOOLCHAINS)" : Directory.Exists(reg.Directory)
                    ? $"{reg.Version}, {reg.Files.Count} Dateien, Digest {reg.Digest[..12]}" : $"Ordner fehlt: {reg.Directory}",
                options.UsesVm);
        }

        // Offline-VM
        if (options.HyperV is { } hv)
        {
            var image = new FileInfo(hv.ImagePath);
            manifest["sandbox"] = new JsonObject
            {
                ["vmName"] = hv.VmName,
                ["image"] = new JsonObject
                {
                    ["path"] = hv.ImagePath, ["exists"] = image.Exists, ["bytes"] = image.Exists ? image.Length : 0,
                    ["modified"] = image.Exists ? ContractTime.ToContract(image.LastWriteTimeUtc) : null,
                },
                ["memoryMB"] = hv.MemoryMB, ["processors"] = hv.Processors, ["reuseVm"] = hv.ReuseVm, ["keepVmOnExit"] = hv.KeepVmOnExit,
                ["lowIntegrityTools"] = hv.LowIntegrityTools,
            };
            Check("Windows-Abbild (sandbox.hyperv.imagePath)", image.Exists,
                image.Exists ? $"{image.Length / (1024 * 1024)} MB" : $"nicht gefunden: {hv.ImagePath}", options.Mode == SandboxMode.HyperV);
            if (!hv.LowIntegrityTools)
            {
                Check("Werkzeuge mit niedriger Integrität (lowIntegrityTools)", false, "ausgeschaltet – nur zur Fehlersuche", required: false);
            }
        }

        // Linux-VM (M8): nur Hinweis – Pflicht erst, wenn ein Projekt sie braucht (dann scheitert sonst der Start).
        if (options.HyperVLinux is { } lx)
        {
            var image = new FileInfo(lx.ImagePath);
            var prereqProblem = "";
            try
            {
                LinuxPrerequisites.Load(lx.LinuxPrerequisites ?? "");
            }
            catch (Exception ex) when (ex is InvalidDataException or IOException or JsonException or UnauthorizedAccessException)
            {
                prereqProblem = ex.Message;
            }
            manifest["sandboxLinux"] = new JsonObject
            {
                ["vmName"] = lx.VmName, ["image"] = lx.ImagePath, ["imageExists"] = image.Exists, ["prerequisites"] = lx.LinuxPrerequisites,
                ["memoryMB"] = lx.MemoryMB, ["processors"] = lx.Processors, ["idleMinutes"] = lx.IdleMinutes, ["stopAction"] = lx.StopAction,
                ["defaultGuestOs"] = options.DefaultGuestOs.ToString(),
            };
            Check("Debian-Abbild (sandbox.hypervLinux.imagePath)", image.Exists,
                image.Exists ? $"{image.Length / (1024 * 1024)} MB" : $"nicht gefunden: {lx.ImagePath}", required: false);
            Check("Linux-Bausteine (sandbox.hypervLinux.prerequisites)", prereqProblem.Length == 0,
                prereqProblem.Length == 0 ? "prerequisites.json geprüft" : GuestReport.Clip(prereqProblem, 300), required: false);
        }

        // QEMU auf dem Linux-Host (M10)
        if (options.Qemu is { } qm)
        {
            var image = new FileInfo(qm.ImagePath);
            var prereqProblem = "";
            try
            {
                LinuxPrerequisites.Load(qm.LinuxPrerequisites ?? "", Hypervisor.Qemu);
            }
            catch (Exception ex) when (ex is InvalidDataException or IOException or JsonException or UnauthorizedAccessException)
            {
                prereqProblem = ex.Message;
            }
            var required = options.Mode == SandboxMode.Qemu;
            var kvm = OperatingSystem.IsLinux() && QemuVmHost.KvmUsable();
            manifest["sandboxQemu"] = new JsonObject
            {
                ["vmName"] = qm.VmName, ["image"] = qm.ImagePath, ["imageExists"] = image.Exists, ["prerequisites"] = qm.LinuxPrerequisites,
                ["memoryMB"] = qm.MemoryMB, ["processors"] = qm.Processors, ["idleMinutes"] = qm.IdleMinutes, ["accel"] = qm.QemuAccel,
                ["kvm"] = kvm,
            };
            Check("Debian-Abbild (sandbox.qemu.imagePath)", image.Exists,
                image.Exists ? $"{image.Length / (1024 * 1024)} MB" : $"nicht gefunden: {qm.ImagePath}", required);
            Check("Linux-Bausteine (sandbox.qemu.prerequisites)", prereqProblem.Length == 0,
                prereqProblem.Length == 0 ? "prerequisites.json geprüft" : GuestReport.Clip(prereqProblem, 300), required);
            foreach (var (program, package) in new[] { (qm.QemuBinary, "qemu-system-x86"), ("qemu-img", "qemu-utils"), ("mkfs.fat", "dosfstools"), ("mcopy", "mtools") })
            {
                var found = QemuVmHost.FindProgram(program);
                Check($"{program} (Paket {package})", found is not null, found ?? "nicht im PATH – scripts/linux/install-host.sh", required);
            }
            Check("KVM (/dev/kvm)", kvm, kvm ? "nutzbar" : "nicht nutzbar – QEMU emuliert (sehr langsam); Benutzer in Gruppe kvm aufnehmen",
                required && qm.QemuAccel == "Kvm");
        }

        // Web
        if (options.Web is { } web)
        {
            manifest["web"] = new JsonObject
            {
                ["enabled"] = true, ["route"] = web.RouteText, ["proxy"] = web.Proxy?.Url,
                ["allowedHosts"] = new JsonArray(web.AllowedHosts.Select(h => (JsonNode?)h).ToArray()),
                ["caFile"] = FileInfoNode(web.CaFile), ["lowIntegrity"] = web.LowIntegrity,
            };
            if (!web.LowIntegrity)
            {
                Check("Webprozesse mit niedriger Integrität (web.lowIntegrity)", false, "ausgeschaltet – nur zur Fehlersuche", required: false);
            }
        }
        else
        {
            manifest["web"] = new JsonObject { ["enabled"] = false };
        }

        var modelsFile = Path.Combine(options.RepoRoot, "config", "models.json");
        Check("Modellprofile (config/models.json)", File.Exists(modelsFile), File.Exists(modelsFile) ? "vorhanden" : "fehlt", required: false);
        Check("Audit-Journal", true, File.Exists(Path.Combine(options.DataDirectory, "audit.db")) ? "vorhanden" : "wird angelegt");
        manifest["offline"] = checks;
        manifest["complete"] = checks.All(c => c!["ok"]!.GetValue<bool>() || !c["required"]!.GetValue<bool>());
        return manifest;
    }

    /// <summary>Schreibt das Manifest atomar nach manifest.json im Datenordner.</summary>
    public static void Write(JsonObject manifest, string dataDirectory)
    {
        var file = Path.Combine(dataDirectory, "manifest.json");
        var temp = file + ".tmp";
        File.WriteAllText(temp, manifest.ToJsonString(new JsonSerializerOptions { WriteIndented = true }), new UTF8Encoding(false));
        File.Move(temp, file, overwrite: true);
    }

    private static JsonNode? FileInfoNode(string? path) =>
        path is null ? null : new JsonObject { ["path"] = path, ["exists"] = File.Exists(path), ["sha256"] = FileSha(path) };

    internal static string? FileSha(string? path)
    {
        try
        {
            return path is not null && File.Exists(path) ? Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(path))) : null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static JsonArray Prompts(string repoRoot)
    {
        var result = new JsonArray();
        var dir = Path.Combine(repoRoot, "src", "python", "localai", "orchestrator", "prompts");
        if (!Directory.Exists(dir))
        {
            return result;
        }
        foreach (var file in Directory.GetFiles(dir, "*.md").Order(StringComparer.Ordinal))
        {
            var text = File.ReadAllText(file);
            var m = PromptHeader().Match(text);
            result.Add(new JsonObject
            {
                ["name"] = m.Success ? m.Groups[1].Value : Path.GetFileNameWithoutExtension(file),
                ["version"] = m.Success ? int.Parse(m.Groups[2].Value, System.Globalization.CultureInfo.InvariantCulture) : 0,
                ["sha256"] = FileSha(file)![..12],
            });
        }
        return result;
    }

    private static JsonObject Python(string repoRoot)
    {
        var venv = Path.Combine(repoRoot, "src", "python", ".venv");
        var cfg = Path.Combine(venv, "pyvenv.cfg");
        if (!File.Exists(cfg))
        {
            return new JsonObject { ["ok"] = false, ["detail"] = $"fehlt: {cfg}" };
        }
        var values = File.ReadAllLines(cfg).Select(l => l.Split('=', 2)).Where(p => p.Length == 2)
            .ToDictionary(p => p[0].Trim(), p => p[1].Trim(), StringComparer.OrdinalIgnoreCase);
        var version = values.GetValueOrDefault("version_info") ?? values.GetValueOrDefault("version") ?? "?";
        var packages = new JsonObject();
        var site = new[] { Path.Combine(venv, "Lib", "site-packages") }
            .Concat(Directory.Exists(Path.Combine(venv, "lib")) ? Directory.GetDirectories(Path.Combine(venv, "lib"), "python*").Select(d => Path.Combine(d, "site-packages")) : [])
            .FirstOrDefault(Directory.Exists);
        if (site is not null)
        {
            foreach (var dist in Directory.GetDirectories(site, "*.dist-info").Order(StringComparer.Ordinal))
            {
                var name = Path.GetFileName(dist)[..^".dist-info".Length];
                var dash = name.LastIndexOf('-');
                if (dash > 0)
                {
                    packages[name[..dash]] = name[(dash + 1)..];
                }
            }
        }
        var home = values.GetValueOrDefault("home") ?? "";
        var ok = home.Length > 0 && Directory.Exists(home);
        return new JsonObject
        {
            ["ok"] = ok, ["version"] = version, ["home"] = home, ["packages"] = packages,
            ["detail"] = ok ? $"Python {version}, {packages.Count} Pakete" : $"Python-Laufzeit fehlt: {home}",
        };
    }

    private static string GitCommit(string repoRoot)
    {
        try
        {
            var head = Path.Combine(repoRoot, ".git", "HEAD");
            if (!File.Exists(head))
            {
                return "unbekannt";
            }
            var text = File.ReadAllText(head).Trim();
            if (!text.StartsWith("ref: ", StringComparison.Ordinal))
            {
                return text[..Math.Min(12, text.Length)];
            }
            var refName = text[5..];
            var refFile = Path.Combine(repoRoot, ".git", refName.Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(refFile))
            {
                return File.ReadAllText(refFile).Trim()[..12];
            }
            var packed = Path.Combine(repoRoot, ".git", "packed-refs");
            var line = File.Exists(packed) ? File.ReadLines(packed).FirstOrDefault(l => l.EndsWith(" " + refName, StringComparison.Ordinal)) : null;
            return line is null ? "unbekannt" : line[..12];
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException)
        {
            return "unbekannt";
        }
    }
}

/// <summary>
/// Diagnosepaket (Runbook): Versionen, anonymisierte Zustände, Ereignis-IDs, Speicherstatistik und Hashes. Code,
/// Webinhalte und Argumente von Vorschlägen sind nie enthalten; Protokolle (die Pfade oder Code enthalten können) nur
/// nach ausdrücklicher Auswahl in der Oberfläche.
/// </summary>
public static class DiagnosticsExport
{
    public const long MaxLogBytes = 2 * 1024 * 1024;

    public static string Create(string dataDirectory, JsonObject manifest, AuditReport? audit, string? auditError,
        IReadOnlyDictionary<string, IReadOnlyDictionary<string, int>> states, IReadOnlyList<BrokerEvent> events, bool includeLogs,
        DateTimeOffset now)
    {
        var dir = Directory.CreateDirectory(Path.Combine(dataDirectory, "diagnose")).FullName;
        var file = Path.Combine(dir, $"diagnose-{now:yyyyMMdd-HHmmss}.zip");
        using var zip = ZipFile.Open(file, ZipArchiveMode.Create);
        var json = new JsonSerializerOptions { WriteIndented = true };
        void Add(string name, JsonNode node)
        {
            using var w = new StreamWriter(zip.CreateEntry(name).Open(), new UTF8Encoding(false));
            w.Write(node.ToJsonString(json));
        }
        Add("manifest.json", manifest);
        Add("audit.json", audit?.ToJson() ?? new JsonObject { ["ok"] = false, ["error"] = auditError });
        var stateNode = new JsonObject();
        foreach (var (tool, counts) in states)
        {
            stateNode[tool] = new JsonObject(counts.Select(c => KeyValuePair.Create(c.Key, (JsonNode?)c.Value)));
        }
        Add("zustaende.json", stateNode);
        // Ereignisse ohne Text (Meldungen können Pfade oder Inhalte enthalten).
        Add("ereignisse.json", new JsonArray(events.Select(e => (JsonNode?)new JsonObject
        {
            ["seq"] = e.Seq, ["at"] = e.At, ["type"] = e.Type, ["proposalId"] = e.ProposalId, ["runId"] = e.RunId, ["state"] = e.State?.ToString(),
        }).ToArray()));
        using var process = Process.GetCurrentProcess();
        var drive = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(dataDirectory))!);
        Add("system.json", new JsonObject
        {
            ["workingSetMB"] = process.WorkingSet64 / (1024 * 1024),
            ["gcHeapMB"] = GC.GetTotalMemory(false) / (1024 * 1024),
            ["uptimeMinutes"] = (int)(DateTime.Now - process.StartTime).TotalMinutes,
            ["processors"] = Environment.ProcessorCount,
            ["diskFreeGB"] = drive.IsReady ? drive.AvailableFreeSpace / (1024L * 1024 * 1024) : -1,
            ["machineMemoryMB"] = GC.GetGCMemoryInfo().TotalAvailableMemoryBytes / (1024 * 1024),
        });
        var hashes = new JsonObject();
        foreach (var name in new[] { "audit.db", "toolchains.json", "calibration.json", "run-journal.json", "manifest.json" })
        {
            hashes[name] = VersionManifest.FileSha(Path.Combine(dataDirectory, name));
        }
        hashes["auditLastHash"] = audit?.LastHash;
        Add("hashes.json", hashes);
        if (includeLogs)
        {
            foreach (var log in Directory.GetFiles(dataDirectory, "*.log").Order(StringComparer.Ordinal))
            {
                using var source = new FileStream(log, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                if (source.Length > MaxLogBytes)
                {
                    source.Seek(-MaxLogBytes, SeekOrigin.End);
                }
                using var target = zip.CreateEntry("protokolle/" + Path.GetFileName(log)).Open();
                source.CopyTo(target);
            }
        }
        Add("inhalt.json", new JsonObject
        {
            ["hinweis"] = includeLogs
                ? "Enthält Protokolle (können Pfade, Befehle und Ausschnitte aus Code enthalten). Keine Webinhalte, keine Vorschlagsargumente."
                : "Keine Protokolle, kein Code, keine Webinhalte, keine Vorschlagsargumente.",
            ["protokolle"] = includeLogs,
        });
        return file;
    }
}

/// <summary>
/// Sicherung und Wiederherstellung (Runbook „Backup / Restore“): Audit-Journal konsistent (SQLite-Online-Backup),
/// Toolchain-Registrierungen, Kalibrierung, Runjournal, Konfiguration und Manifest – mit SHA-256 je Datei.
/// Freigaben (Capabilities) werden nie gesichert; nach der Wiederherstellung sind alle offenen Vorschläge ungültig.
/// </summary>
public static class BackupArchive
{
    private static readonly string[] DataFiles = ["toolchains.json", "calibration.json", "run-journal.json", "manifest.json"];

    public static string Create(string dataDirectory, string? configPath, string? projectPath, string repoRoot, DateTimeOffset now)
    {
        var dir = Directory.CreateDirectory(Path.Combine(dataDirectory, "sicherungen")).FullName;
        var file = Path.Combine(dir, $"sicherung-{now:yyyyMMdd-HHmmss}.zip");
        var temp = Path.Combine(dir, $"audit-{Guid.NewGuid():N}.db");
        try
        {
            var files = new JsonObject();
            using (var zip = ZipFile.Open(file, ZipArchiveMode.Create))
            {
                void AddFile(string entry, string source)
                {
                    zip.CreateEntryFromFile(source, entry);
                    files[entry] = VersionManifest.FileSha(source);
                }
                var audit = Path.Combine(dataDirectory, "audit.db");
                if (File.Exists(audit))
                {
                    AuditJournal.BackupTo(audit, temp);
                    Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
                    AddFile("daten/audit.db", temp);
                }
                foreach (var name in DataFiles.Where(n => File.Exists(Path.Combine(dataDirectory, n))))
                {
                    AddFile("daten/" + name, Path.Combine(dataDirectory, name));
                }
                if (configPath is not null && File.Exists(configPath))
                {
                    AddFile("config/" + Path.GetFileName(configPath), configPath);
                }
                if (projectPath is not null && File.Exists(projectPath))
                {
                    AddFile("config/" + Path.GetFileName(projectPath), projectPath);
                }
                var models = Path.Combine(repoRoot, "config", "models.local.json");
                if (File.Exists(models))
                {
                    AddFile("config/models.local.json", models);
                }
                var meta = new JsonObject
                {
                    ["schemaVersion"] = 1, ["createdAt"] = ContractTime.ToContract(now), ["version"] = VersionManifest.ProductVersion,
                    ["files"] = files,
                    ["hinweis"] = "Freigaben werden nicht gesichert; nach einer Wiederherstellung sind offene Vorschläge ungültig.",
                };
                using var w = new StreamWriter(zip.CreateEntry("sicherung.json").Open(), new UTF8Encoding(false));
                w.Write(meta.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            }
            return file;
        }
        finally
        {
            if (File.Exists(temp))
            {
                File.Delete(temp);
            }
        }
    }

    /// <summary>
    /// Stellt eine Sicherung im (nicht laufenden) Datenordner wieder her: prüft alle Hashes und die Hashkette des
    /// Journals, verschiebt die bisherigen Dateien nach <c>vor-wiederherstellung-…</c>. Konfigurationsdateien werden nur
    /// daneben abgelegt (<c>wiederhergestellt-config-…</c>), nie über deine Konfiguration geschrieben.
    /// </summary>
    public static string Restore(string zipPath, string dataDirectory, DateTimeOffset now)
    {
        var work = Directory.CreateDirectory(Path.Combine(dataDirectory, $"wiederherstellung-{now:yyyyMMdd-HHmmss}-tmp")).FullName;
        try
        {
            using (var zip = ZipFile.OpenRead(zipPath))
            {
                var metaEntry = zip.GetEntry("sicherung.json") ?? throw new InvalidDataException("sicherung.json fehlt – keine Sicherung dieses Programms");
                JsonObject meta;
                using (var r = new StreamReader(metaEntry.Open()))
                {
                    meta = JsonNode.Parse(r.ReadToEnd())?.AsObject() ?? throw new InvalidDataException("sicherung.json ungültig");
                }
                var files = meta["files"]?.AsObject() ?? throw new InvalidDataException("Dateiliste fehlt");
                foreach (var entry in zip.Entries.Where(e => e.FullName != "sicherung.json" && e.Length > 0))
                {
                    if (!files.ContainsKey(entry.FullName))
                    {
                        throw new InvalidDataException($"Unerwartete Datei in der Sicherung: {entry.FullName}");
                    }
                }
                foreach (var (name, sha) in files)
                {
                    if (name.Contains("..", StringComparison.Ordinal) || Path.IsPathRooted(name) || name.Contains('\\')
                        || !(name.StartsWith("daten/", StringComparison.Ordinal) || name.StartsWith("config/", StringComparison.Ordinal))
                        || name.Count(c => c == '/') != 1)
                    {
                        throw new InvalidDataException($"Unzulässiger Name in der Sicherung: {name}");
                    }
                    var entry = zip.GetEntry(name) ?? throw new InvalidDataException($"Datei fehlt in der Sicherung: {name}");
                    var target = Path.Combine(work, name.Replace('/', Path.DirectorySeparatorChar));
                    Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                    entry.ExtractToFile(target);
                    if (VersionManifest.FileSha(target) != sha?.GetValue<string>())
                    {
                        throw new InvalidDataException($"Hash stimmt nicht: {name} – Sicherung beschädigt oder verändert");
                    }
                }
            }
            var restoredAudit = Path.Combine(work, "daten", "audit.db");
            if (File.Exists(restoredAudit))
            {
                AuditJournal.Verify(restoredAudit); // Hashkette (wirft AuditTamperException)
                Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
            }
            var previous = Path.Combine(dataDirectory, $"vor-wiederherstellung-{now:yyyyMMdd-HHmmss}");
            Directory.CreateDirectory(previous);
            foreach (var name in new[] { "audit.db", "audit.db-wal", "audit.db-shm" }.Concat(DataFiles))
            {
                var current = Path.Combine(dataDirectory, name);
                if (File.Exists(current))
                {
                    File.Move(current, Path.Combine(previous, name));
                }
            }
            var daten = Path.Combine(work, "daten");
            if (Directory.Exists(daten))
            {
                foreach (var f in Directory.GetFiles(daten))
                {
                    File.Move(f, Path.Combine(dataDirectory, Path.GetFileName(f)));
                }
            }
            var report = $"Wiederhergestellt nach {dataDirectory}; bisherige Dateien in {previous}.";
            var config = Path.Combine(work, "config");
            if (Directory.Exists(config))
            {
                var configTarget = Path.Combine(dataDirectory, $"wiederhergestellt-config-{now:yyyyMMdd-HHmmss}");
                Directory.Move(config, configTarget);
                report += $" Konfiguration der Sicherung liegt in {configTarget} (deine aktuelle Konfiguration bleibt unverändert).";
            }
            return report + " Offene Vorschläge und Freigaben sind beim nächsten Start ungültig.";
        }
        finally
        {
            if (Directory.Exists(work))
            {
                Directory.Delete(work, recursive: true);
            }
        }
    }
}
