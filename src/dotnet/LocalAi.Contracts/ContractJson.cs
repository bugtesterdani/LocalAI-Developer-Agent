using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace LocalAi.Contracts;

/// <summary>Gemeinsame Serialisierungsregeln für alle Verträge.</summary>
public static class ContractJson
{
    public const long SchemaVersion = 1;

    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = null,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        RespectNullableAnnotations = true,
        RespectRequiredConstructorParameters = true,
        NumberHandling = JsonNumberHandling.Strict,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        WriteIndented = false,
    };

    public static T Deserialize<T>(JsonElement element) =>
        element.Deserialize<T>(Options) ?? throw new JsonException($"{typeof(T).Name} fehlt");

    public static T Deserialize<T>(string json) =>
        JsonSerializer.Deserialize<T>(json, Options) ?? throw new JsonException($"{typeof(T).Name} fehlt");

    public static string Serialize<T>(T value) => JsonSerializer.Serialize(value, Options);

    public static JsonElement ToElement<T>(T value) => JsonSerializer.SerializeToElement(value, Options);
}

/// <summary>Zeitstempel im Vertragsformat (UTC, Millisekunden, Z).</summary>
public static class ContractTime
{
    public const string Format = "yyyy-MM-dd'T'HH:mm:ss.fff'Z'";

    public static string ToContract(DateTimeOffset value) =>
        value.UtcDateTime.ToString(Format, CultureInfo.InvariantCulture);

    public static DateTimeOffset Parse(string value) =>
        DateTimeOffset.ParseExact(value, Format, CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
}

public static class ProposalHashing
{
    /// <summary>SHA-256 über das kanonische JSON des gesamten Vorschlags.</summary>
    public static string Sha256(ToolProposal proposal) => CanonicalJson.Sha256Hex(proposal);

    public static bool IsSha256Hex(string? value) =>
        value is { Length: 64 } && value.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');
}
