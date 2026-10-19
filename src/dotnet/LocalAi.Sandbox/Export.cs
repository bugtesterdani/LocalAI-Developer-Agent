using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Sandbox;

/// <summary>Zeilenbasierter Unified Diff (Myers) für die Anzeige der Übernahme – vom Broker selbst berechnet.</summary>
public static class LineDiff
{
    public const int MaxLines = 40_000;
    public const int MaxEditDistance = 4_000;

    public static string[] Lines(string text)
    {
        var normalized = text.Replace("\r\n", "\n", StringComparison.Ordinal);
        if (normalized.Length == 0)
        {
            return [];
        }
        var lines = normalized.Split('\n');
        return normalized.EndsWith('\n') ? lines[..^1] : lines;
    }

    /// <summary>Abschnitt einer Datei im Unified-Diff-Format (Kopfzeilen + Hunks mit 3 Kontextzeilen).</summary>
    public static string Unified(string path, string? before, string? after, int context = 3)
    {
        var sb = new StringBuilder();
        sb.Append(before is null ? "--- /dev/null\n" : $"--- a/{path}\n");
        sb.Append(after is null ? "+++ /dev/null\n" : $"+++ b/{path}\n");
        var a = Lines(before ?? string.Empty);
        var b = Lines(after ?? string.Empty);
        var script = Diff(a, b);
        if (script.Count == 0 || script.All(e => e.Op == ' '))
        {
            return sb.ToString();
        }
        // Hunks: Änderungen mit höchstens 2*context Gleichzeilen dazwischen zusammenfassen.
        var i = 0;
        while (i < script.Count)
        {
            while (i < script.Count && script[i].Op == ' ')
            {
                i++;
            }
            if (i >= script.Count)
            {
                break;
            }
            var start = Math.Max(0, i - context);
            var end = i;
            var lastChange = i;
            while (end < script.Count)
            {
                if (script[end].Op != ' ')
                {
                    lastChange = end;
                }
                else if (end - lastChange > 2 * context)
                {
                    break;
                }
                end++;
            }
            end = Math.Min(script.Count, lastChange + context + 1);
            var oldStart = script[start].OldLine;
            var newStart = script[start].NewLine;
            var oldCount = script.Skip(start).Take(end - start).Count(e => e.Op != '+');
            var newCount = script.Skip(start).Take(end - start).Count(e => e.Op != '-');
            sb.Append($"@@ -{(oldCount == 0 ? oldStart : oldStart + 1)},{oldCount} +{(newCount == 0 ? newStart : newStart + 1)},{newCount} @@\n");
            for (var j = start; j < end; j++)
            {
                sb.Append(script[j].Op).Append(script[j].Text).Append('\n');
            }
            i = end;
        }
        return sb.ToString();
    }

    /// <summary>Editierskript; Op ' ' (gleich), '-' (entfernt), '+' (neu). OldLine/NewLine: 0-basierte Position davor.</summary>
    public static List<(char Op, string Text, int OldLine, int NewLine)> Diff(string[] a, string[] b)
    {
        var prefix = 0;
        while (prefix < a.Length && prefix < b.Length && a[prefix] == b[prefix])
        {
            prefix++;
        }
        var suffix = 0;
        while (suffix < a.Length - prefix && suffix < b.Length - prefix && a[^(suffix + 1)] == b[^(suffix + 1)])
        {
            suffix++;
        }
        var midA = a[prefix..(a.Length - suffix)];
        var midB = b[prefix..(b.Length - suffix)];
        var middle = midA.Length + midB.Length > MaxLines ? null : Myers(midA, midB);
        middle ??= [.. midA.Select(l => ('-', l)), .. midB.Select(l => ('+', l))];

        var result = new List<(char, string, int, int)>(prefix + suffix + middle.Count);
        int oldLine = 0, newLine = 0;
        void Add(char op, string text)
        {
            result.Add((op, text, oldLine, newLine));
            if (op != '+')
            {
                oldLine++;
            }
            if (op != '-')
            {
                newLine++;
            }
        }
        for (var i = 0; i < prefix; i++)
        {
            Add(' ', a[i]);
        }
        foreach (var (op, text) in middle)
        {
            Add(op, text);
        }
        for (var i = a.Length - suffix; i < a.Length; i++)
        {
            Add(' ', a[i]);
        }
        return result;
    }

    private static List<(char, string)>? Myers(string[] a, string[] b)
    {
        int n = a.Length, m = b.Length, max = n + m, offset = max;
        if (max == 0)
        {
            return [];
        }
        var v = new int[2 * max + 2];
        var trace = new List<int[]>();
        for (var d = 0; d <= max; d++)
        {
            if (d > MaxEditDistance)
            {
                return null;
            }
            trace.Add((int[])v.Clone());
            for (var k = -d; k <= d; k += 2)
            {
                var x = k == -d || (k != d && v[k - 1 + offset] < v[k + 1 + offset]) ? v[k + 1 + offset] : v[k - 1 + offset] + 1;
                var y = x - k;
                while (x < n && y < m && a[x] == b[y])
                {
                    x++;
                    y++;
                }
                v[k + offset] = x;
                if (x >= n && y >= m)
                {
                    return Backtrack(a, b, trace, offset);
                }
            }
        }
        return null;
    }

    private static List<(char, string)> Backtrack(string[] a, string[] b, List<int[]> trace, int offset)
    {
        var edits = new List<(char, string)>();
        int x = a.Length, y = b.Length;
        for (var d = trace.Count - 1; d >= 0; d--)
        {
            var v = trace[d];
            var k = x - y;
            var prevK = k == -d || (k != d && v[k - 1 + offset] < v[k + 1 + offset]) ? k + 1 : k - 1;
            var prevX = v[prevK + offset];
            var prevY = prevX - prevK;
            while (x > prevX && y > prevY)
            {
                edits.Add((' ', a[x - 1]));
                x--;
                y--;
            }
            if (d > 0)
            {
                if (x == prevX)
                {
                    edits.Add(('+', b[y - 1]));
                }
                else
                {
                    edits.Add(('-', a[x - 1]));
                }
            }
            x = prevX;
            y = prevY;
        }
        edits.Reverse();
        return edits;
    }
}

/// <summary>
/// Übernahme in den Projektordner (B10): Workspace.Diff legt die geänderten Dateien aus der Sandbox im Staging ab
/// (inhaltsadressiert), der Broker berechnet den Diff gegen den aktuellen Ordner; Workspace.ExportPatch schreibt nach
/// eigener Freigabe genau diese Bytes – nur wenn die Datei noch den Vorher-Hash hat. Vorher wird gesichert; scheitert
/// eine Datei, werden alle bereits geschriebenen zurückgesetzt.
/// </summary>
public sealed class ExportStaging(string directory, IPolicyContext policy, IClock clock) : IExportStaging, IToolAdapter
{
    public static readonly TimeSpan Retention = TimeSpan.FromDays(30);
    private readonly Lock _gate = new();

    public string Directory => directory;

    public ExportPreview? Find(string stagingId)
    {
        if (stagingId.Length != 32 || !stagingId.All(Uri.IsHexDigit))
        {
            return null;
        }
        var file = Path.Combine(directory, stagingId, "preview.json");
        try
        {
            return File.Exists(file) ? ContractJson.Deserialize<ExportPreview>(File.ReadAllText(file, Encoding.UTF8)) : null;
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            return null;
        }
    }

    /// <summary>Wandelt das Ergebnis von Workspace.Diff (Dateien aus der Sandbox) in eine Übernahme-Vorschau.</summary>
    public ToolResult Stage(ToolProposal proposal, ToolResult result)
    {
        if (result.Status != ResultStatus.Succeeded)
        {
            return result;
        }
        try
        {
            var grantId = proposal.Arguments.GetProperty("grantId").GetString()!;
            var grant = policy.FindGrant(grantId) ?? throw new ExportException($"Grant {grantId} existiert nicht mehr");
            var changes = result.TargetResults.FirstOrDefault(t => t.TargetId == "changes")?.Text
                          ?? throw new ExportException("Sandbox lieferte keine Änderungen");
            using var doc = JsonDocument.Parse(changes);
            var stagingId = Guid.NewGuid().ToString("N");
            var root = Path.Combine(directory, stagingId);
            var filesDir = Path.Combine(root, "files");
            System.IO.Directory.CreateDirectory(filesDir);
            var files = new List<ExportFile>();
            var unchanged = new List<string>();
            var patch = new StringBuilder();
            var baseId = proposal.Arguments.GetProperty("baseSnapshotId").GetString()!;
            if (doc.RootElement.GetProperty("baseSnapshotId").GetString() != baseId
                || doc.RootElement.GetProperty("snapshotId").GetString() != proposal.SnapshotId)
            {
                throw new ExportException("Sandbox meldet andere Snapshots als angefragt");
            }
            foreach (var entry in doc.RootElement.GetProperty("files").EnumerateArray())
            {
                var path = PathRules.NormalizeRelative(entry.GetProperty("path").GetString());
                byte[]? after = null;
                if (entry.TryGetProperty("content", out var content))
                {
                    after = Convert.FromBase64String(content.GetString()!);
                    if (Sha256(after) != entry.GetProperty("sha256").GetString())
                    {
                        throw new ExportException($"{path}: Inhalt passt nicht zum gemeldeten Hash");
                    }
                }
                var before = SafeFiles.ReadIfExists(grant, path);
                if ((before is null && after is null) || (before is not null && after is not null && before.AsSpan().SequenceEqual(after)))
                {
                    unchanged.Add(path);
                    continue;
                }
                var kind = after is null ? ExportFileKind.Delete : before is null ? ExportFileKind.Create : ExportFileKind.Modify;
                string? afterSha = null;
                if (after is not null)
                {
                    afterSha = Sha256(after);
                    File.WriteAllBytes(Path.Combine(filesDir, afterSha), after);
                }
                files.Add(new ExportFile(path, kind, after?.Length ?? 0, before is null ? null : Sha256(before), afterSha));
                patch.Append(LineDiff.Unified(path, before is null ? null : AsText(before), after is null ? null : AsText(after)));
            }
            if (files.Count == 0)
            {
                throw new ExportException("Keine Unterschiede zum Projektordner – nichts zu übernehmen");
            }
            var preview = new ExportPreview(stagingId, grantId, baseId, proposal.SnapshotId, files, patch.ToString(),
                unchanged.Count > 0 ? unchanged : null);
            File.WriteAllText(Path.Combine(root, "preview.json"), ContractJson.Serialize(preview), new UTF8Encoding(false));
            var text = ContractJson.Serialize(preview);
            return result with
            {
                TargetResults = [new TargetResult("export", ResultStatus.Succeeded, Text: text, ContentSha256: CanonicalJson.Sha256Hex(text),
                    Message: $"{files.Count} Datei(en) zur Übernahme vorbereitet" + (unchanged.Count > 0 ? $", {unchanged.Count} schon gleich" : ""))],
            };
        }
        catch (Exception ex) when (ex is ExportException or JsonException or FormatException or IOException or UnauthorizedAccessException
                                       or BrokerException or KeyNotFoundException or InvalidOperationException)
        {
            return Failed(proposal, result.InvocationId, ex.Message);
        }
    }

    public Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken) =>
        Task.Run(() => Export(proposal, invocationId), cancellationToken);

    private ToolResult Export(ToolProposal proposal, string invocationId)
    {
        lock (_gate)
        {
            try
            {
                var stagingId = proposal.Arguments.GetProperty("stagingId").GetString()!;
                var preview = Find(stagingId) ?? throw new ExportException("Übernahme unbekannt oder abgelaufen");
                var grant = policy.FindGrant(preview.GrantId) ?? throw new ExportException("Grant existiert nicht mehr");
                var byPath = preview.Files.ToDictionary(f => f.Path, StringComparer.Ordinal);
                var selected = proposal.Targets.Select(t => (t, byPath.TryGetValue(t.Path, out var f) ? f : throw new ExportException($"{t.Path} gehört nicht zur Übernahme"))).ToList();
                // 1. Alles prüfen, bevor etwas geschrieben wird (Vorher-Hash, Staging-Inhalt, Pfade).
                var plan = new List<(ToolTarget Target, ExportFile File, byte[]? After)>();
                foreach (var (target, file) in selected)
                {
                    var current = SafeFiles.ReadIfExists(grant, file.Path);
                    var currentSha = current is null ? null : Sha256(current);
                    if (currentSha != file.BeforeSha256)
                    {
                        throw new ExportException($"{file.Path} wurde seit der Vorschau verändert – Übernahme neu vorbereiten (nichts geschrieben)");
                    }
                    byte[]? after = null;
                    if (file.AfterSha256 is { } sha)
                    {
                        after = File.ReadAllBytes(Path.Combine(directory, stagingId, "files", sha));
                        if (Sha256(after) != sha)
                        {
                            throw new ExportException($"{file.Path}: Staging-Inhalt verändert (Hashfehler) – nichts geschrieben");
                        }
                    }
                    plan.Add((target, file, after));
                }
                // 2. Sichern und schreiben; bei einem Fehler alles Geschriebene zurücksetzen.
                var backup = Path.Combine(directory, stagingId, $"sicherung-{clock.UtcNow:yyyyMMdd-HHmmss}");
                var done = new List<(string Path, bool Existed)>();
                try
                {
                    foreach (var (_, file, after) in plan)
                    {
                        var existed = SafeFiles.Backup(grant, file.Path, backup);
                        SafeFiles.Write(grant, file.Path, after);
                        done.Add((file.Path, existed));
                    }
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or BrokerException or ExportException)
                {
                    foreach (var (path, existed) in done)
                    {
                        try
                        {
                            SafeFiles.Restore(grant, path, backup, existed);
                        }
                        catch (Exception restoreError) when (restoreError is IOException or UnauthorizedAccessException or BrokerException)
                        {
                        }
                    }
                    throw new ExportException($"Schreiben fehlgeschlagen ({ex.Message}) – {done.Count} Datei(en) zurückgesetzt");
                }
                var results = plan.Select(p => new TargetResult(p.Target.TargetId, ResultStatus.Succeeded,
                    Message: p.File.Kind switch
                    {
                        ExportFileKind.Create => "neu angelegt",
                        ExportFileKind.Delete => "gelöscht",
                        _ => "geändert",
                    })).ToList();
                var summary = $"{plan.Count} Datei(en) in {grant.Root} übernommen; Sicherung: {backup}";
                results.Add(new TargetResult("export", ResultStatus.Succeeded, Text: summary, ContentSha256: CanonicalJson.Sha256Hex(summary), Message: summary));
                return new ToolResult(invocationId, proposal.ProposalId, ResultStatus.Succeeded, proposal.SnapshotId, results,
                    CanonicalJson.Sha256Hex(summary), Truncated: false, Untrusted: false);
            }
            catch (Exception ex) when (ex is ExportException or IOException or UnauthorizedAccessException or BrokerException or JsonException
                                           or KeyNotFoundException or InvalidOperationException)
            {
                return Failed(proposal, invocationId, ex.Message);
            }
        }
    }

    /// <summary>Alte Staging-Ordner (älter als 30 Tage) entfernen.</summary>
    public void Cleanup()
    {
        if (!System.IO.Directory.Exists(directory))
        {
            return;
        }
        foreach (var dir in System.IO.Directory.GetDirectories(directory))
        {
            try
            {
                if (clock.UtcNow - System.IO.Directory.GetLastWriteTimeUtc(dir) > Retention)
                {
                    FileTools.DeleteTree(dir);
                }
            }
            catch (IOException)
            {
            }
        }
    }

    private static ToolResult Failed(ToolProposal proposal, string invocationId, string message) =>
        new(invocationId, proposal.ProposalId, ResultStatus.Failed, proposal.SnapshotId,
            [new TargetResult("*", ResultStatus.Failed, Message: message)], CanonicalJson.Sha256Hex(message), Truncated: false, Untrusted: false);

    private static string AsText(byte[] data)
    {
        if (data.AsSpan(0, Math.Min(data.Length, 8192)).Contains((byte)0))
        {
            return $"(Binärdatei, {data.Length} Bytes, SHA-256 {Sha256(data)})\n";
        }
        var span = data.AsSpan();
        if (span.StartsWith((ReadOnlySpan<byte>)[0xEF, 0xBB, 0xBF]))
        {
            span = span[3..];
        }
        return Encoding.UTF8.GetString(span);
    }

    public static string Sha256(byte[] data) => Convert.ToHexStringLower(SHA256.HashData(data));
}

public sealed class ExportException(string message) : Exception(message);

/// <summary>Dateizugriffe im Projektordner für die Übernahme: nur unterhalb des Grants, keine Verknüpfungen.</summary>
public static class SafeFiles
{
    public static string Resolve(FolderGrant grant, string relative)
    {
        var rel = PathRules.NormalizeRelative(relative);
        var root = Path.GetFullPath(grant.Root);
        var full = Path.GetFullPath(Path.Combine(root, rel.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            throw new ExportException($"{relative}: außerhalb des freigegebenen Ordners");
        }
        // Jede vorhandene Stufe prüfen: keine Junctions, Symlinks oder anderen Reparse Points (SEC-05).
        var current = root;
        foreach (var segment in rel.Split('/'))
        {
            current = Path.Combine(current, segment);
            if ((File.Exists(current) || System.IO.Directory.Exists(current))
                && File.GetAttributes(current).HasFlag(FileAttributes.ReparsePoint))
            {
                throw new ExportException($"{relative}: Verknüpfung im Pfad – Übernahme verweigert");
            }
        }
        if (System.IO.Directory.Exists(full))
        {
            throw new ExportException($"{relative}: ist ein Ordner");
        }
        return full;
    }

    public static byte[]? ReadIfExists(FolderGrant grant, string relative)
    {
        var full = Resolve(grant, relative);
        return File.Exists(full) ? File.ReadAllBytes(full) : null;
    }

    public static bool Backup(FolderGrant grant, string relative, string backupRoot)
    {
        var full = Resolve(grant, relative);
        if (!File.Exists(full))
        {
            return false;
        }
        var target = Path.Combine(backupRoot, relative.Replace('/', Path.DirectorySeparatorChar));
        System.IO.Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        File.Copy(full, target, overwrite: true);
        return true;
    }

    public static void Write(FolderGrant grant, string relative, byte[]? content)
    {
        var full = Resolve(grant, relative);
        if (content is null)
        {
            if (File.Exists(full))
            {
                File.Delete(full);
            }
            return;
        }
        var dir = Path.GetDirectoryName(full)!;
        System.IO.Directory.CreateDirectory(dir);
        Resolve(grant, relative); // nach dem Anlegen von Ordnern erneut prüfen
        var temp = Path.Combine(dir, $".{Path.GetFileName(full)}.localai-{Guid.NewGuid():N}.tmp");
        try
        {
            File.WriteAllBytes(temp, content);
            File.Move(temp, full, overwrite: true);
        }
        finally
        {
            if (File.Exists(temp))
            {
                File.Delete(temp);
            }
        }
    }

    public static void Restore(FolderGrant grant, string relative, string backupRoot, bool existed)
    {
        var full = Resolve(grant, relative);
        if (existed)
        {
            File.Copy(Path.Combine(backupRoot, relative.Replace('/', Path.DirectorySeparatorChar)), full, overwrite: true);
        }
        else if (File.Exists(full))
        {
            File.Delete(full);
        }
    }
}
