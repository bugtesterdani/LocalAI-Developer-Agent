namespace LocalAi.Broker;

/// <summary>Fehlercodes in Anlehnung an HTTP (Spezifikation §10).</summary>
public static class BrokerErrorCodes
{
    public const int BadRequest = 400;
    public const int Forbidden = 403;
    public const int NotFound = 404;
    public const int Conflict = 409;
    public const int Gone = 410;
    public const int Unprocessable = 422;
    public const int TooMany = 429;
    public const int Internal = 500;
}

public sealed class BrokerException(int code, string message) : Exception(message)
{
    public int Code { get; } = code;
}

public interface IClock
{
    DateTimeOffset UtcNow { get; }
}

public sealed class SystemClock : IClock
{
    public static readonly SystemClock Instance = new();

    public DateTimeOffset UtcNow => DateTimeOffset.UtcNow;
}

/// <summary>Einstellbare Uhr für Tests.</summary>
public sealed class ManualClock(DateTimeOffset start) : IClock
{
    private DateTimeOffset _now = start;

    public DateTimeOffset UtcNow => _now;

    public void Advance(TimeSpan delta) => _now += delta;
}

public sealed record BrokerOptions
{
    /// <summary>Gültigkeit eines Vorschlags bis zum Start (Standard 5 Minuten).</summary>
    public TimeSpan ProposalTtl { get; init; } = TimeSpan.FromMinutes(5);

    /// <summary>Höchstzahl an Zielen in einer Sammelfreigabe.</summary>
    public int MaxTargetsPerProposal { get; init; } = 200;

    /// <summary>Höchstgröße einer Sammelfreigabe in Bytes.</summary>
    public long MaxBatchBytes { get; init; } = 4L * 1024 * 1024;

    /// <summary>Höchstgröße eines Einzel-Reads in Bytes.</summary>
    public long MaxSingleReadBytes { get; init; } = 256L * 1024;

    public long PolicyRevision { get; init; } = 1;

    public string SandboxId { get; init; } = "sbx-none";

    /// <summary>Höchstzahl gleichzeitig offener Vorschläge (Schutz vor Überflutung).</summary>
    public int MaxOpenProposals { get; init; } = 500;
}
