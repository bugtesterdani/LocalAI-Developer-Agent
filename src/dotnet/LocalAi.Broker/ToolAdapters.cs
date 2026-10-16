using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>Führt einen bereits freigegebenen und konsumierten Aufruf aus.</summary>
public interface IToolAdapter
{
    Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken);
}

/// <summary>
/// Testadapter für M1: führt nichts aus, sondern zählt Aufrufe und protokolliert, welche Ziele
/// "gelesen" worden wären. Echte Dateizugriffe gibt es erst über die Offline-Sandbox (M2).
/// </summary>
public sealed class CountingTestAdapter : IToolAdapter
{
    private int _invocations;
    private readonly ConcurrentQueue<(string ProposalId, string TargetId)> _executedTargets = new();

    public int Invocations => Volatile.Read(ref _invocations);

    public IReadOnlyList<(string ProposalId, string TargetId)> ExecutedTargets => _executedTargets.ToArray();

    /// <summary>Künstliche Laufzeit pro Aufruf (für UI- und Abbruchtests).</summary>
    public TimeSpan Delay { get; set; } = TimeSpan.Zero;

    public async Task<ToolResult> ExecuteAsync(ToolProposal proposal, string invocationId, CancellationToken cancellationToken)
    {
        Interlocked.Increment(ref _invocations);
        if (Delay > TimeSpan.Zero)
        {
            await Task.Delay(Delay, cancellationToken).ConfigureAwait(false);
        }
        var results = new List<TargetResult>();
        foreach (var target in proposal.Targets)
        {
            cancellationToken.ThrowIfCancellationRequested();
            _executedTargets.Enqueue((proposal.ProposalId, target.TargetId));
            var text = $"[Testadapter] Inhalt von {target.Path} wurde nicht gelesen (M1 ohne Sandbox).";
            results.Add(new TargetResult(target.TargetId, ResultStatus.Succeeded,
                ContentSha256: Sha(text), Text: text));
        }
        var output = string.Join("\n", results.Select(r => r.ContentSha256));
        return new ToolResult(invocationId, proposal.ProposalId, ResultStatus.Succeeded, proposal.SnapshotId,
            results, Sha(output), Truncated: false, Untrusted: true, ExitCode: 0);
    }

    private static string Sha(string text) => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
}
