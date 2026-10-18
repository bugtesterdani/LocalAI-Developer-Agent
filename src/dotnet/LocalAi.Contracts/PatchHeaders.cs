namespace LocalAi.Contracts;

public sealed class PatchFormatException(string message, bool tooLarge = false) : Exception(message)
{
    public bool TooLarge { get; } = tooLarge;
}

/// <summary>Art der Änderung einer Datei im Patch.</summary>
public enum PatchFileKind
{
    Create,
    Modify,
    Delete,
}

public sealed record PatchFile(string Path, PatchFileKind Kind);

/// <summary>
/// Liest nur die Dateiköpfe eines Unified Diffs (Patch.Apply). Die Hunks prüft und wendet der Worker in der
/// Sandbox an (localai/worker/patching.py); Broker (Zielmenge, Ordnerrechte) und Oberfläche (Anzeige je Datei)
/// nutzen diese Klasse. Pfade sind hier nur bereinigt, die Pfadregeln prüft der Broker.
/// </summary>
public static class PatchHeaders
{
    public const int MaxPatchChars = 1024 * 1024;

    public static IReadOnlyList<PatchFile> Parse(string patch)
    {
        if (patch.Length > MaxPatchChars)
        {
            throw new PatchFormatException($"Patch zu groß (höchstens {MaxPatchChars} Zeichen)", tooLarge: true);
        }
        var lines = patch.Replace("\r\n", "\n").Split('\n');
        var files = new List<PatchFile>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var i = 0; i < lines.Length - 1; i++)
        {
            var line = lines[i];
            if (line.StartsWith("rename from", StringComparison.Ordinal) || line.StartsWith("copy from", StringComparison.Ordinal))
            {
                throw Invalid("Umbenennen/Kopieren wird nicht unterstützt – als Löschen + Neuanlegen schreiben");
            }
            if (line.StartsWith("GIT binary patch", StringComparison.Ordinal) || line.StartsWith("Binary files", StringComparison.Ordinal))
            {
                throw Invalid("Binärpatches werden nicht unterstützt");
            }
            if (!line.StartsWith("--- ", StringComparison.Ordinal) || !lines[i + 1].StartsWith("+++ ", StringComparison.Ordinal))
            {
                continue;
            }
            var oldPath = HeaderPath(line[4..], "a/");
            var newPath = HeaderPath(lines[i + 1][4..], "b/");
            i++;
            PatchFile file = (oldPath, newPath) switch
            {
                (null, null) => throw Invalid("Kopfzeilen --- /dev/null und +++ /dev/null zugleich"),
                (null, { } created) => new PatchFile(created, PatchFileKind.Create),
                ({ } deleted, null) => new PatchFile(deleted, PatchFileKind.Delete),
                ({ } a, { } b) when a != b => throw Invalid($"Unterschiedliche Pfade in den Kopfzeilen ({a} → {b}) – Umbenennen wird nicht unterstützt"),
                ({ } a, _) => new PatchFile(a, PatchFileKind.Modify),
            };
            if (!seen.Add(file.Path))
            {
                throw Invalid($"Datei kommt mehrfach im Patch vor: {file.Path}");
            }
            files.Add(file);
        }
        if (files.Count == 0)
        {
            throw Invalid("Kein Unified Diff (Kopfzeilen '--- a/<pfad>' und '+++ b/<pfad>' fehlen)");
        }
        return files;
    }

    /// <summary>Abschnitt einer Datei aus dem Patch – für die Anzeige pro Ziel in der Freigabekarte.</summary>
    public static string Section(string patch, string path)
    {
        var lines = patch.Replace("\r\n", "\n").Split('\n');
        var output = new List<string>();
        var take = false;
        for (var i = 0; i < lines.Length; i++)
        {
            var line = lines[i];
            if (line.StartsWith("--- ", StringComparison.Ordinal) && i + 1 < lines.Length && lines[i + 1].StartsWith("+++ ", StringComparison.Ordinal))
            {
                string? name;
                try
                {
                    name = HeaderPath(lines[i + 1][4..], "b/") ?? HeaderPath(line[4..], "a/");
                }
                catch (PatchFormatException)
                {
                    name = null;
                }
                take = name == path;
            }
            else if (line.StartsWith("diff --git", StringComparison.Ordinal))
            {
                take = false;
            }
            if (take)
            {
                output.Add(line);
            }
        }
        return string.Join('\n', output).TrimEnd();
    }

    private static string? HeaderPath(string raw, string prefix)
    {
        var value = raw.Split('\t')[0].Trim();
        if (value == "/dev/null")
        {
            return null;
        }
        if (value.Length >= 2 && value[0] == '"' && value[^1] == '"')
        {
            value = value[1..^1];
        }
        if (value.StartsWith(prefix, StringComparison.Ordinal))
        {
            value = value[prefix.Length..];
        }
        value = value.Replace('\\', '/');
        while (value.StartsWith("./", StringComparison.Ordinal))
        {
            value = value[2..];
        }
        return value.Length == 0 ? throw Invalid("Leerer Pfad in einer Kopfzeile") : value;
    }

    private static PatchFormatException Invalid(string message) => new(message);
}

