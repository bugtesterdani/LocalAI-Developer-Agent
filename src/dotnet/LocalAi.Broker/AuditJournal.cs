using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>Ein Eintrag im Audit-Journal.</summary>
public sealed record JournalEntry(long Seq, string At, string Type, JsonObject Data, string PrevHash, string Hash);

/// <summary>
/// Append-only-Journal mit Hashverkettung (JSON Lines, kanonisch).
/// Hash = SHA-256(kanonisch {seq, at, type, data, prevHash}).
/// Erkennt nachträgliche Änderungen; Schutz gegen einen vollständig kompromittierten Host
/// bietet erst ein getrennt geschützter Checkpoint (Spezifikation §10).
/// </summary>
public sealed class AuditJournal : IDisposable
{
    public const string GenesisHash = "0000000000000000000000000000000000000000000000000000000000000000";

    private readonly string _path;
    private readonly IClock _clock;
    private readonly Lock _gate = new();
    private FileStream? _stream;
    private long _seq;
    private string _lastHash = GenesisHash;

    public AuditJournal(string path, IClock clock)
    {
        _path = path;
        _clock = clock;
    }

    public string FilePath => _path;

    /// <summary>
    /// Liest und prüft das bestehende Journal. Eine unvollständige letzte Zeile (Absturz beim Schreiben)
    /// wird ignoriert und abgeschnitten; jede andere Abweichung ist ein Manipulationsverdacht.
    /// </summary>
    public IReadOnlyList<JournalEntry> OpenAndReplay()
    {
        lock (_gate)
        {
            var directory = Path.GetDirectoryName(Path.GetFullPath(_path));
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            var entries = new List<JournalEntry>();
            long validLength = 0;
            if (File.Exists(_path))
            {
                var bytes = File.ReadAllBytes(_path);
                var offset = 0;
                while (offset < bytes.Length)
                {
                    var newline = Array.IndexOf(bytes, (byte)'\n', offset);
                    if (newline < 0)
                    {
                        break; // unvollständige letzte Zeile
                    }
                    var line = Encoding.UTF8.GetString(bytes, offset, newline - offset);
                    var entry = ParseAndVerify(line, entries.Count == 0 ? GenesisHash : entries[^1].Hash, entries.Count + 1);
                    entries.Add(entry);
                    offset = newline + 1;
                    validLength = offset;
                }
            }

            _stream = new FileStream(_path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.Read);
            _stream.SetLength(validLength);
            _stream.Seek(validLength, SeekOrigin.Begin);
            _seq = entries.Count;
            _lastHash = entries.Count == 0 ? GenesisHash : entries[^1].Hash;
            return entries;
        }
    }

    public JournalEntry Append(string type, JsonObject data)
    {
        lock (_gate)
        {
            if (_stream is null)
            {
                throw new InvalidOperationException("Journal ist nicht geöffnet");
            }
            var seq = _seq + 1;
            var at = ContractTime.ToContract(_clock.UtcNow);
            var body = new JsonObject
            {
                ["seq"] = seq,
                ["at"] = at,
                ["type"] = type,
                ["data"] = data.DeepClone(),
                ["prevHash"] = _lastHash,
            };
            var hash = CanonicalJson.Sha256Hex(CanonicalJson.Canonicalize(ToElement(body)));
            body["hash"] = hash;
            var line = CanonicalJson.Canonicalize(ToElement(body)) + "\n";
            var bytes = Encoding.UTF8.GetBytes(line);
            _stream.Write(bytes);
            _stream.Flush(flushToDisk: true);
            _seq = seq;
            _lastHash = hash;
            return new JournalEntry(seq, at, type, (JsonObject)data.DeepClone(), body["prevHash"]!.GetValue<string>(), hash);
        }
    }

    /// <summary>
    /// Prüft eine Journaldatei vollständig (z. B. für Diagnose und Tests).
    /// Darf auch laufen, während der Broker das Journal zum Schreiben geöffnet hat:
    /// unter Windows muss der Lesezugriff dafür Schreiben durch andere zulassen.
    /// </summary>
    public static int Verify(string path)
    {
        string text;
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
        using (var reader = new StreamReader(stream, Encoding.UTF8))
        {
            text = reader.ReadToEnd();
        }
        var lines = text.Split('\n');
        var prev = GenesisHash;
        var count = 0;
        foreach (var line in lines)
        {
            if (line.Length == 0)
            {
                continue;
            }
            var entry = ParseAndVerify(line, prev, count + 1);
            prev = entry.Hash;
            count++;
        }
        return count;
    }

    private static JournalEntry ParseAndVerify(string line, string expectedPrev, long expectedSeq)
    {
        JsonObject node;
        try
        {
            node = JsonNode.Parse(line)?.AsObject() ?? throw new JsonException("leer");
        }
        catch (JsonException ex)
        {
            throw new AuditTamperException($"Journalzeile {expectedSeq} ist kein gültiges JSON: {ex.Message}");
        }
        var hash = node["hash"]?.GetValue<string>() ?? throw new AuditTamperException($"Journalzeile {expectedSeq} ohne Hash");
        node.Remove("hash");
        var computed = CanonicalJson.Sha256Hex(CanonicalJson.Canonicalize(ToElement(node)));
        var seq = node["seq"]?.GetValue<long>() ?? -1;
        var prev = node["prevHash"]?.GetValue<string>();
        if (computed != hash || seq != expectedSeq || prev != expectedPrev)
        {
            throw new AuditTamperException($"Hashkette in Journalzeile {expectedSeq} verletzt");
        }
        return new JournalEntry(seq, node["at"]!.GetValue<string>(), node["type"]!.GetValue<string>(),
            node["data"]!.AsObject(), prev!, hash);
    }

    private static JsonElement ToElement(JsonNode node) => JsonSerializer.SerializeToElement(node);

    public void Dispose()
    {
        lock (_gate)
        {
            _stream?.Dispose();
            _stream = null;
        }
    }
}

public sealed class AuditTamperException(string message) : Exception(message);
