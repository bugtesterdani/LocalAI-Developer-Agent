using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Sandbox;

/// <summary>Sitzungsschlüssel und Sequenzzähler für signierte Jobs.</summary>
public sealed class SandboxSession
{
    private long _sequence;

    public SandboxSession(string sessionId, byte[]? key = null)
    {
        SessionId = sessionId;
        Key = key ?? RandomNumberGenerator.GetBytes(32);
    }

    public string SessionId { get; }

    public byte[] Key { get; }

    public long NextSequence() => Interlocked.Increment(ref _sequence);
}

public static class JobSigner
{
    /// <summary>HMAC-SHA256 über das kanonische JSON des Jobs – identisch zu compute_mac() in Python.</summary>
    public static string ComputeMac(SandboxJob job, byte[] key) =>
        Convert.ToHexStringLower(HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(CanonicalJson.Canonicalize(job))));

    public static JobEnvelope Sign(SandboxJob job, byte[] key) => new(job, ComputeMac(job, key));
}

/// <summary>In dieser Sitzung importierte Snapshots (die VM wird je Sitzung neu aufgebaut).</summary>
public sealed class SnapshotRegistry
{
    private readonly ConcurrentDictionary<string, string> _snapshots = new(StringComparer.Ordinal);

    public void Add(string snapshotId, string grantId) => _snapshots[snapshotId] = grantId;

    public bool Contains(string snapshotId, string grantId) =>
        _snapshots.TryGetValue(snapshotId, out var g) && g == grantId;
}

/// <summary>Ordnerrechte aus der Projektkonfiguration plus Snapshots dieser Sitzung.</summary>
public sealed class ProjectPolicyContext(ProjectConfig project, SnapshotRegistry snapshots) : IPolicyContext
{
    public IReadOnlyList<FolderGrant> Grants { get; } = project.Grants;

    public FolderGrant? FindGrant(string grantId) => project.Grants.FirstOrDefault(g => g.GrantId == grantId);

    public bool HasSnapshot(string snapshotId, string grantId) => snapshots.Contains(snapshotId, grantId);
}

public static class ProjectLoader
{
    /// <summary>Lädt eine Projektdatei; relative Grant-Pfade beziehen sich auf <paramref name="baseDirectory"/>.</summary>
    public static ProjectConfig Load(string path, string baseDirectory)
    {
        var project = ContractJson.Deserialize<ProjectConfig>(File.ReadAllText(path, Encoding.UTF8));
        var grants = new List<FolderGrant>();
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var grant in project.Grants)
        {
            if (!ids.Add(grant.GrantId))
            {
                throw new InvalidDataException($"grantId doppelt: {grant.GrantId}");
            }
            if (grant.Rights.Any(r => r is FolderRight.Delete or FolderRight.Execute))
            {
                throw new InvalidDataException($"Grant {grant.GrantId}: Delete und Execute sind in M2 nicht vorgesehen");
            }
            var root = Path.GetFullPath(Path.Combine(baseDirectory, grant.Root));
            if (!Directory.Exists(root))
            {
                throw new DirectoryNotFoundException($"Grant {grant.GrantId}: Ordner existiert nicht: {root}");
            }
            grants.Add(grant with { Root = root });
        }
        var names = new HashSet<string>(StringComparer.Ordinal);
        foreach (var name in project.Toolchains ?? [])
        {
            if (!System.Text.RegularExpressions.Regex.IsMatch(name, "^[a-z][a-z0-9-]{0,31}$") || !names.Add(name))
            {
                throw new InvalidDataException($"toolchains: ungültiger oder doppelter Name '{name}'");
            }
        }
        return project with { Grants = grants };
    }
}

public sealed record SandboxReply(string? ResultJson, string? Rejection);

/// <summary>Überträgt einen signierten Job in die Sandbox und wartet auf die Antwort.</summary>
public interface ISandboxTransport
{
    Task<SandboxReply> ExchangeAsync(JobEnvelope envelope, string? payloadPath, TimeSpan timeout, CancellationToken cancellationToken);
}

/// <summary>
/// Entwicklungstransport über zwei Ordner (Eingang/Ausgang) auf dem Host.
/// In der VM-Variante (M2b) ersetzen Copy-VMFile und die Transfer-VHDX diese Ordner.
/// </summary>
public sealed class LocalDirectoryTransport(string inbox, string outbox) : ISandboxTransport
{
    public const long MaxResultBytes = 16L * 1024 * 1024;

    public string Inbox { get; } = Directory.CreateDirectory(inbox).FullName;

    public string Outbox { get; } = Directory.CreateDirectory(outbox).FullName;

    public async Task<SandboxReply> ExchangeAsync(JobEnvelope envelope, string? payloadPath, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var job = envelope.Job;
        var stem = $"{job.Sequence:D12}-{job.JobId}";
        if (payloadPath is not null)
        {
            File.Copy(payloadPath, Path.Combine(Inbox, stem + ".payload.zip"));
        }
        // Job zuletzt und atomar ablegen: der Worker sieht ihn erst, wenn alles da ist.
        var tmp = Path.Combine(Inbox, stem + ".job.tmp");
        await File.WriteAllTextAsync(tmp, ContractJson.Serialize(envelope), new UTF8Encoding(false), cancellationToken).ConfigureAwait(false);
        File.Move(tmp, Path.Combine(Inbox, stem + ".job.json"));

        var resultPath = Path.Combine(Outbox, job.JobId + ".result.json");
        var rejectedPath = Path.Combine(Outbox, stem + ".job.json.rejected.txt");
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (File.Exists(resultPath))
            {
                var info = new FileInfo(resultPath);
                if (info.Length > MaxResultBytes)
                {
                    File.Delete(resultPath);
                    return new SandboxReply(null, $"Ergebnis zu groß ({info.Length} Bytes)");
                }
                var text = await File.ReadAllTextAsync(resultPath, Encoding.UTF8, cancellationToken).ConfigureAwait(false);
                File.Delete(resultPath);
                return new SandboxReply(text, null);
            }
            if (File.Exists(rejectedPath))
            {
                var reason = await File.ReadAllTextAsync(rejectedPath, Encoding.UTF8, cancellationToken).ConfigureAwait(false);
                File.Delete(rejectedPath);
                return new SandboxReply(null, reason.Length > 500 ? reason[..500] : reason);
            }
            if (DateTime.UtcNow > deadline)
            {
                return new SandboxReply(null, $"Keine Antwort der Sandbox innerhalb von {timeout.TotalSeconds:F0} s");
            }
            await Task.Delay(50, cancellationToken).ConfigureAwait(false);
        }
    }
}

/// <summary>Prüft Antworten der Sandbox. Alles aus der Sandbox ist untrusted.</summary>
public static class ResultValidator
{
    public const int MaxTextChars = 8 * 1024 * 1024;

    public static ToolResult Validate(SandboxReply reply, SandboxJob job)
    {
        if (reply.Rejection is not null || reply.ResultJson is null)
        {
            return Failed(job, $"Sandbox hat den Job nicht ausgeführt: {reply.Rejection ?? "keine Antwort"}");
        }
        ResultEnvelope envelope;
        try
        {
            envelope = ContractJson.Deserialize<ResultEnvelope>(reply.ResultJson);
        }
        catch (JsonException ex)
        {
            return Failed(job, $"Antwort der Sandbox ist ungültig: {ex.Message}");
        }
        var result = envelope.Result;
        if (envelope.JobId != job.JobId || envelope.Sequence != job.Sequence
            || result.ProposalId != job.Proposal.ProposalId || result.InvocationId != job.InvocationId)
        {
            return Failed(job, "Antwort der Sandbox gehört nicht zu diesem Job");
        }
        var allowed = job.Proposal.Targets.Select(t => t.TargetId).ToHashSet(StringComparer.Ordinal);
        allowed.Add("*");
        allowed.Add("snapshot");
        var seen = new HashSet<string>(StringComparer.Ordinal);
        long chars = 0;
        foreach (var target in result.TargetResults)
        {
            if (!allowed.Contains(target.TargetId) || !seen.Add(target.TargetId))
            {
                return Failed(job, $"Antwort enthält unerwartetes Ziel: {target.TargetId}");
            }
            chars += (target.Text?.Length ?? 0) + (target.Message?.Length ?? 0);
        }
        if (chars > MaxTextChars)
        {
            return Failed(job, "Antwort der Sandbox ist zu groß");
        }
        return result with { Untrusted = true };
    }

    public static ToolResult Failed(SandboxJob job, string message) =>
        new(job.InvocationId, job.Proposal.ProposalId, ResultStatus.Failed, job.Proposal.SnapshotId,
            [new TargetResult("*", ResultStatus.Failed, Message: message)],
            CanonicalJson.Sha256Hex(message), Truncated: false, Untrusted: true);
}

/// <summary>Führt freigegebene Vorschläge über signierte Jobs in der Sandbox aus.</summary>
public sealed class SandboxToolAdapter(
    SandboxSession session,
    ISandboxTransport transport,
    IPolicyContext policy,
    SnapshotRegistry snapshots,
    string scratchDirectory,
    IClock clock,
    SnapshotBuilder? builder = null) : IToolAdapter
{
    private readonly SnapshotBuilder _builder = builder ?? new SnapshotBuilder();

    public TimeSpan JobTimeout { get; init; } = TimeSpan.FromMinutes(5);

    public TimeSpan SnapshotTimeout { get; init; } = TimeSpan.FromMinutes(30);

    public async Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken)
    {
        var isSnapshot = proposal.Tool == "Workspace.Snapshot";
        var timeout = isSnapshot ? SnapshotTimeout : JobTimeout;
        var now = clock.UtcNow;
        var job = new SandboxJob(
            SchemaVersion: ContractJson.SchemaVersion,
            JobId: Guid.NewGuid().ToString("D"),
            Sequence: session.NextSequence(),
            SessionId: session.SessionId,
            InvocationId: invocationId,
            Proposal: proposal,
            ProposalSha256: ProposalHashing.Sha256(proposal),
            IssuedAt: ContractTime.ToContract(now),
            ExpiresAt: ContractTime.ToContract(now + timeout));

        string? archive = null;
        try
        {
            if (isSnapshot)
            {
                var grantId = proposal.Arguments.GetProperty("grantId").GetString()!;
                var grant = policy.FindGrant(grantId);
                if (grant is null)
                {
                    return ResultValidator.Failed(job, $"Grant {grantId} existiert nicht mehr");
                }
                Directory.CreateDirectory(scratchDirectory);
                archive = Path.Combine(scratchDirectory, $"snapshot-{job.JobId}.zip");
                SnapshotManifest manifest;
                try
                {
                    manifest = await Task.Run(() => _builder.Build(grant, archive, clock, cancellationToken), cancellationToken).ConfigureAwait(false);
                }
                catch (SnapshotException ex)
                {
                    return ResultValidator.Failed(job, ex.Message);
                }
                job = job with { Snapshot = manifest, PayloadSha256 = Sha256File(archive) };
            }

            var reply = await transport.ExchangeAsync(JobSigner.Sign(job, session.Key), archive, timeout, cancellationToken).ConfigureAwait(false);
            var result = ResultValidator.Validate(reply, job);
            if (isSnapshot && result.Status == ResultStatus.Succeeded)
            {
                var id = result.TargetResults.FirstOrDefault(t => t.TargetId == "snapshot")?.Text;
                if (id != job.Snapshot!.SnapshotId)
                {
                    return ResultValidator.Failed(job, "Sandbox meldet einen anderen Snapshot als übertragen");
                }
                snapshots.Add(id, job.Snapshot.GrantId);
            }
            return result;
        }
        finally
        {
            if (archive is not null)
            {
                File.Delete(archive);
            }
        }
    }

    private static string Sha256File(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexStringLower(SHA256.HashData(stream));
    }
}

/// <summary>Ohne Sandbox wird nichts ausgeführt.</summary>
public sealed class NoSandboxAdapter : IToolAdapter
{
    public Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken) =>
        Task.FromResult(new ToolResult(invocationId, proposal.ProposalId, ResultStatus.Failed, proposal.SnapshotId,
            [new TargetResult("*", ResultStatus.Failed, Message: "Keine Sandbox verfügbar – Ausführung gesperrt")],
            CanonicalJson.Sha256Hex(string.Empty), Truncated: false, Untrusted: true));
}
