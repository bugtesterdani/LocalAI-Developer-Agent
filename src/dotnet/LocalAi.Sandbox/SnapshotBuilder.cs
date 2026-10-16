using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Sandbox;

public sealed record SnapshotLimits
{
    public int MaxFiles { get; init; } = 20_000;
    public long MaxTotalBytes { get; init; } = 512L * 1024 * 1024;
    public long MaxFileBytes { get; init; } = 32L * 1024 * 1024;

    /// <summary>Ordner- und Dateinamen, die nie in einen Snapshot gelangen (zusätzlich zu Grant.Exclude).</summary>
    public IReadOnlySet<string> DefaultExcludes { get; init; } = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        ".git", ".vs", ".idea", "bin", "obj", "node_modules", ".venv", "__pycache__", ".pytest_cache",
    };
}

public sealed class SnapshotException(string message) : Exception(message);

/// <summary>
/// Erzeugt einen Snapshot eines freigegebenen Ordners als ZIP + Manifest.
/// Sicherheitsregeln (Spezifikation §6/§8):
/// <list type="bullet">
/// <item>Verknüpfungen, Junctions und andere Reparse Points werden nie verfolgt, sondern übersprungen.</item>
/// <item>Dateien mit mehreren Hardlinks werden übersprungen (sie könnten auf Inhalte außerhalb zeigen).</item>
/// <item>Unter Windows wird der endgültige Pfad jedes geöffneten Datei-Handles geprüft (Schutz vor Junction-Races).</item>
/// <item>Namen müssen den Pfadregeln entsprechen; Alternate Data Streams werden nicht gelesen.</item>
/// </list>
/// </summary>
public sealed class SnapshotBuilder(SnapshotLimits? limits = null)
{
    private readonly SnapshotLimits _limits = limits ?? new SnapshotLimits();

    public SnapshotManifest Build(FolderGrant grant, string archivePath, IClock clock, CancellationToken cancellationToken = default)
    {
        var root = Path.GetFullPath(grant.Root);
        if (!Directory.Exists(root))
        {
            throw new SnapshotException($"Freigegebener Ordner existiert nicht: {root}");
        }
        var rootInfo = new DirectoryInfo(root);
        if (!OperatingSystem.IsWindows() && rootInfo.LinkTarget is not null)
        {
            throw new SnapshotException("Der freigegebene Ordner selbst ist eine Verknüpfung");
        }
        var excludes = new HashSet<string>(_limits.DefaultExcludes, StringComparer.OrdinalIgnoreCase);
        foreach (var name in grant.Exclude ?? [])
        {
            excludes.Add(name);
        }

        var candidates = new List<(string Rel, string Full)>();
        var skipped = new List<string>();
        Walk(rootInfo, string.Empty, grant.Recursive, excludes, candidates, skipped, cancellationToken);
        candidates.Sort((a, b) => string.CompareOrdinal(a.Rel, b.Rel));

        var finalRoot = OperatingSystem.IsWindows() ? WindowsFileChecks.FinalDirectoryPath(root) : root;
        var files = new List<SnapshotFile>();
        long total = 0;
        using (var zipStream = new FileStream(archivePath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        using (var zip = new ZipArchive(zipStream, ZipArchiveMode.Create))
        {
            foreach (var (rel, full) in candidates)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (files.Count >= _limits.MaxFiles)
                {
                    throw new SnapshotException($"Mehr als {_limits.MaxFiles} Dateien – Ordner eingrenzen oder Ausschlüsse ergänzen");
                }
                using var source = new FileStream(full, FileMode.Open, FileAccess.Read, FileShare.Read, 81920, FileOptions.SequentialScan);
                if (OperatingSystem.IsWindows())
                {
                    var reason = WindowsFileChecks.Reject(source, finalRoot);
                    if (reason is not null)
                    {
                        skipped.Add($"{rel}: {reason}");
                        continue;
                    }
                }
                if (source.Length > _limits.MaxFileBytes)
                {
                    skipped.Add($"{rel}: größer als {_limits.MaxFileBytes} Bytes");
                    continue;
                }
                var entry = zip.CreateEntry(rel, CompressionLevel.Fastest);
                using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
                long size = 0;
                using (var target = entry.Open())
                {
                    var buffer = new byte[81920];
                    int read;
                    while ((read = source.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        size += read;
                        if (size > _limits.MaxFileBytes)
                        {
                            throw new SnapshotException($"{rel} ist während des Lesens gewachsen");
                        }
                        hash.AppendData(buffer, 0, read);
                        target.Write(buffer, 0, read);
                    }
                }
                total += size;
                if (total > _limits.MaxTotalBytes)
                {
                    throw new SnapshotException($"Snapshot größer als {_limits.MaxTotalBytes} Bytes – Ordner eingrenzen");
                }
                files.Add(new SnapshotFile(rel, size, Convert.ToHexStringLower(hash.GetHashAndReset())));
            }
        }

        return new SnapshotManifest(
            SchemaVersion: ContractJson.SchemaVersion,
            SnapshotId: ComputeSnapshotId(grant.GrantId, files),
            GrantId: grant.GrantId,
            CreatedAt: ContractTime.ToContract(clock.UtcNow),
            Files: files,
            Skipped: skipped.Count > 0 ? skipped : null);
    }

    /// <summary>SHA-256 über das kanonische JSON von {grantId, files} – identisch zu snapshot_id() in Python.</summary>
    public static string ComputeSnapshotId(string grantId, IReadOnlyList<SnapshotFile> files)
    {
        var obj = new JsonObject
        {
            ["grantId"] = grantId,
            ["files"] = System.Text.Json.JsonSerializer.SerializeToNode(files, ContractJson.Options),
        };
        return CanonicalJson.Sha256Hex(CanonicalJson.Canonicalize(System.Text.Json.JsonSerializer.SerializeToElement(obj)));
    }

    private void Walk(DirectoryInfo dir, string prefix, bool recursive, HashSet<string> excludes,
        List<(string Rel, string Full)> files, List<string> skipped, CancellationToken cancellationToken)
    {
        var options = new EnumerationOptions
        {
            RecurseSubdirectories = false,
            IgnoreInaccessible = true,
            AttributesToSkip = 0,
            ReturnSpecialDirectories = false,
        };
        foreach (var entry in dir.EnumerateFileSystemInfos("*", options))
        {
            cancellationToken.ThrowIfCancellationRequested();
            var rel = prefix + entry.Name;
            if (excludes.Contains(entry.Name))
            {
                continue;
            }
            if (entry.Attributes.HasFlag(FileAttributes.ReparsePoint) || entry.LinkTarget is not null)
            {
                skipped.Add($"{rel}: Verknüpfung/Junction wird nicht verfolgt");
                continue;
            }
            if (entry.Attributes.HasFlag(FileAttributes.Device))
            {
                skipped.Add($"{rel}: Gerät");
                continue;
            }
            try
            {
                PathRules.NormalizeRelative(rel);
            }
            catch (BrokerException ex)
            {
                skipped.Add($"{rel}: {ex.Message}");
                continue;
            }
            if (entry is DirectoryInfo sub)
            {
                if (recursive)
                {
                    Walk(sub, rel + "/", recursive, excludes, files, skipped, cancellationToken);
                }
            }
            else
            {
                files.Add((rel, entry.FullName));
            }
        }
    }
}

/// <summary>Windows-spezifische Prüfungen über das geöffnete Datei-Handle.</summary>
[System.Runtime.Versioning.SupportedOSPlatform("windows")]
internal static class WindowsFileChecks
{
    private const uint FileFlagBackupSemantics = 0x02000000;
    private const uint OpenExisting = 3;
    private const uint FileShareAll = 0x7;

    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public uint CreationTimeLow, CreationTimeHigh;
        public uint LastAccessTimeLow, LastAccessTimeHigh;
        public uint LastWriteTimeLow, LastWriteTimeHigh;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh, FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh, FileIndexLow;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(Microsoft.Win32.SafeHandles.SafeFileHandle handle, out ByHandleFileInformation info);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern uint GetFinalPathNameByHandleW(Microsoft.Win32.SafeHandles.SafeFileHandle handle, char[] buffer, uint length, uint flags);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr template);

    /// <summary>Grund für die Ablehnung oder null, wenn die Datei verwendet werden darf.</summary>
    public static string? Reject(FileStream stream, string finalRoot)
    {
        if (!GetFileInformationByHandle(stream.SafeFileHandle, out var info))
        {
            return $"Dateiinformationen nicht lesbar (Fehler {Marshal.GetLastWin32Error()})";
        }
        if (info.NumberOfLinks > 1)
        {
            return $"Datei hat {info.NumberOfLinks} Hardlinks";
        }
        if ((info.FileAttributes & (uint)FileAttributes.ReparsePoint) != 0)
        {
            return "Reparse Point";
        }
        var final = FinalPath(stream.SafeFileHandle);
        if (!final.StartsWith(finalRoot.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
        {
            return $"liegt tatsächlich außerhalb des freigegebenen Ordners ({final})";
        }
        return null;
    }

    public static string FinalDirectoryPath(string directory)
    {
        using var handle = CreateFileW(directory, 0, FileShareAll, IntPtr.Zero, OpenExisting, FileFlagBackupSemantics, IntPtr.Zero);
        if (handle.IsInvalid)
        {
            throw new SnapshotException($"Ordner nicht lesbar: {directory} (Fehler {Marshal.GetLastWin32Error()})");
        }
        return FinalPath(handle);
    }

    private static string FinalPath(Microsoft.Win32.SafeHandles.SafeFileHandle handle)
    {
        var buffer = new char[32768];
        var length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Length, 0);
        if (length == 0 || length >= buffer.Length)
        {
            throw new SnapshotException($"Endgültiger Pfad nicht ermittelbar (Fehler {Marshal.GetLastWin32Error()})");
        }
        var path = new string(buffer, 0, (int)length);
        if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase))
        {
            throw new SnapshotException("Netzwerkpfade sind nicht erlaubt");
        }
        return path.StartsWith(@"\\?\", StringComparison.Ordinal) ? path[4..] : path;
    }
}
