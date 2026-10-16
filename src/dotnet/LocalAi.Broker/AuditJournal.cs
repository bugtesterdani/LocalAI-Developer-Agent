using System.Text.Json;
using System.Text.Json.Nodes;
using LocalAi.Contracts;
using Microsoft.Data.Sqlite;

namespace LocalAi.Broker;

/// <summary>Ein Eintrag im Audit-Journal.</summary>
public sealed record JournalEntry(long Seq, string At, string Type, JsonObject Data, string PrevHash, string Hash);

/// <summary>
/// Append-only-Journal in SQLite mit Hashverkettung (Spezifikation §10).
/// Hash = SHA-256(kanonisch {seq, at, type, data, prevHash}).
/// Jeder Eintrag wird in einer eigenen Transaktion mit synchronous=FULL geschrieben.
/// Erkennt nachträgliche Änderungen; Schutz gegen einen vollständig kompromittierten Host
/// bietet erst ein getrennt geschützter Checkpoint.
/// </summary>
public sealed class AuditJournal : IDisposable
{
    public const string GenesisHash = "0000000000000000000000000000000000000000000000000000000000000000";

    private readonly string _path;
    private readonly IClock _clock;
    private readonly Lock _gate = new();
    private SqliteConnection? _connection;
    private long _seq;
    private string _lastHash = GenesisHash;

    public AuditJournal(string path, IClock clock)
    {
        _path = path;
        _clock = clock;
    }

    public string FilePath => _path;

    /// <summary>Öffnet das Journal, prüft die gesamte Hashkette und liefert alle Einträge.</summary>
    public IReadOnlyList<JournalEntry> OpenAndReplay()
    {
        lock (_gate)
        {
            var directory = Path.GetDirectoryName(Path.GetFullPath(_path));
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }
            _connection = Open(_path, readOnly: false);
            Execute(_connection, """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS journal (
                    seq       INTEGER PRIMARY KEY,
                    at        TEXT NOT NULL,
                    type      TEXT NOT NULL,
                    data      TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    hash      TEXT NOT NULL
                );
                """);
            var entries = ReadAndVerify(_connection);
            _seq = entries.Count;
            _lastHash = entries.Count == 0 ? GenesisHash : entries[^1].Hash;
            return entries;
        }
    }

    public JournalEntry Append(string type, JsonObject data)
    {
        lock (_gate)
        {
            var connection = _connection ?? throw new InvalidOperationException("Journal ist nicht geöffnet");
            var seq = _seq + 1;
            var at = ContractTime.ToContract(_clock.UtcNow);
            var dataCopy = (JsonObject)data.DeepClone();
            var hash = ComputeHash(seq, at, type, dataCopy, _lastHash);
            using var transaction = connection.BeginTransaction();
            using (var insert = connection.CreateCommand())
            {
                insert.Transaction = transaction;
                insert.CommandText = "INSERT INTO journal (seq, at, type, data, prev_hash, hash) VALUES ($seq, $at, $type, $data, $prev, $hash)";
                insert.Parameters.AddWithValue("$seq", seq);
                insert.Parameters.AddWithValue("$at", at);
                insert.Parameters.AddWithValue("$type", type);
                insert.Parameters.AddWithValue("$data", CanonicalJson.Canonicalize(JsonSerializer.SerializeToElement(dataCopy)));
                insert.Parameters.AddWithValue("$prev", _lastHash);
                insert.Parameters.AddWithValue("$hash", hash);
                insert.ExecuteNonQuery();
            }
            transaction.Commit();
            var entry = new JournalEntry(seq, at, type, dataCopy, _lastHash, hash);
            _seq = seq;
            _lastHash = hash;
            return entry;
        }
    }

    /// <summary>Prüft ein Journal vollständig (auch während der Broker läuft). Gibt die Anzahl der Einträge zurück.</summary>
    public static int Verify(string path)
    {
        using var connection = Open(path, readOnly: true);
        return ReadAndVerify(connection).Count;
    }

    private static List<JournalEntry> ReadAndVerify(SqliteConnection connection)
    {
        var entries = new List<JournalEntry>();
        using var select = connection.CreateCommand();
        select.CommandText = "SELECT seq, at, type, data, prev_hash, hash FROM journal ORDER BY seq";
        using var reader = select.ExecuteReader();
        var prev = GenesisHash;
        while (reader.Read())
        {
            var expectedSeq = entries.Count + 1;
            var seq = reader.GetInt64(0);
            var at = reader.GetString(1);
            var type = reader.GetString(2);
            JsonObject data;
            try
            {
                data = JsonNode.Parse(reader.GetString(3))?.AsObject() ?? throw new JsonException("leer");
            }
            catch (JsonException ex)
            {
                throw new AuditTamperException($"Journaleintrag {expectedSeq}: Daten sind kein gültiges JSON: {ex.Message}");
            }
            var prevHash = reader.GetString(4);
            var hash = reader.GetString(5);
            if (seq != expectedSeq || prevHash != prev || ComputeHash(seq, at, type, data, prevHash) != hash)
            {
                throw new AuditTamperException($"Hashkette in Journaleintrag {expectedSeq} verletzt");
            }
            entries.Add(new JournalEntry(seq, at, type, data, prevHash, hash));
            prev = hash;
        }
        return entries;
    }

    private static string ComputeHash(long seq, string at, string type, JsonObject data, string prevHash)
    {
        var body = new JsonObject
        {
            ["seq"] = seq,
            ["at"] = at,
            ["type"] = type,
            ["data"] = data.DeepClone(),
            ["prevHash"] = prevHash,
        };
        return CanonicalJson.Sha256Hex(CanonicalJson.Canonicalize(JsonSerializer.SerializeToElement(body)));
    }

    private static SqliteConnection Open(string path, bool readOnly)
    {
        var builder = new SqliteConnectionStringBuilder
        {
            DataSource = path,
            Mode = readOnly ? SqliteOpenMode.ReadOnly : SqliteOpenMode.ReadWriteCreate,
            Pooling = false,
        };
        var connection = new SqliteConnection(builder.ToString());
        connection.Open();
        return connection;
    }

    private static void Execute(SqliteConnection connection, string sql)
    {
        using var command = connection.CreateCommand();
        command.CommandText = sql;
        command.ExecuteNonQuery();
    }

    public void Dispose()
    {
        lock (_gate)
        {
            _connection?.Dispose();
            _connection = null;
        }
    }
}

public sealed class AuditTamperException(string message) : Exception(message);
