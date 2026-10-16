using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace LocalAi.Contracts;

/// <summary>Wert lässt sich nicht kanonisch darstellen.</summary>
public sealed class CanonicalJsonException(string message) : Exception(message);

/// <summary>
/// Kanonisches JSON (Teilmenge von RFC 8785 / JCS) und SHA-256.
/// Muss byte-identisch zu <c>localai/canonical.py</c> sein; gemeinsame Testvektoren in
/// <c>contracts/testvectors/canonical.json</c>.
/// </summary>
public static class CanonicalJson
{
    public const long MaxSafeInteger = 9_007_199_254_740_991;

    private static readonly JsonDocumentOptions StrictDocumentOptions = new()
    {
        AllowTrailingCommas = false,
        CommentHandling = JsonCommentHandling.Disallow,
        MaxDepth = 64,
    };

    /// <summary>Parst JSON-Text strikt und gibt die kanonische Form zurück.</summary>
    public static string CanonicalizeText(string json)
    {
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(json, StrictDocumentOptions);
        }
        catch (JsonException ex)
        {
            throw new CanonicalJsonException($"Ungültiges JSON: {ex.Message}");
        }
        using (document)
        {
            return Canonicalize(document.RootElement);
        }
    }

    public static string Canonicalize(JsonElement element)
    {
        var sb = new StringBuilder();
        Write(element, sb);
        return sb.ToString();
    }

    /// <summary>Serialisiert einen Vertrag mit den Vertragsoptionen und kanonisiert ihn.</summary>
    public static string Canonicalize<T>(T value)
    {
        var element = JsonSerializer.SerializeToElement(value, ContractJson.Options);
        return Canonicalize(element);
    }

    public static string Sha256Hex(string canonical)
    {
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(canonical));
        return Convert.ToHexStringLower(hash);
    }

    public static string Sha256Hex<T>(T value) => Sha256Hex(Canonicalize(value));

    private static void Write(JsonElement element, StringBuilder sb)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Null:
                sb.Append("null");
                break;
            case JsonValueKind.True:
                sb.Append("true");
                break;
            case JsonValueKind.False:
                sb.Append("false");
                break;
            case JsonValueKind.Number:
                WriteNumber(element, sb);
                break;
            case JsonValueKind.String:
                WriteString(ReadString(element), sb);
                break;
            case JsonValueKind.Array:
                sb.Append('[');
                var first = true;
                foreach (var item in element.EnumerateArray())
                {
                    if (!first)
                    {
                        sb.Append(',');
                    }
                    first = false;
                    Write(item, sb);
                }
                sb.Append(']');
                break;
            case JsonValueKind.Object:
                WriteObject(element, sb);
                break;
            default:
                throw new CanonicalJsonException($"Wertart nicht unterstützt: {element.ValueKind}");
        }
    }

    private static void WriteObject(JsonElement element, StringBuilder sb)
    {
        var properties = new List<(string Name, JsonElement Value)>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject())
        {
            string name;
            try
            {
                name = property.Name;
            }
            catch (InvalidOperationException)
            {
                throw new CanonicalJsonException("Ungültiger Objektschlüssel");
            }
            EnsureValidUtf16(name);
            if (!seen.Add(name))
            {
                throw new CanonicalJsonException($"Doppelter Schlüssel: {name}");
            }
            properties.Add((name, property.Value));
        }

        // Ordinal = Sortierung nach UTF-16-Codeeinheiten (wie JCS).
        properties.Sort((a, b) => string.CompareOrdinal(a.Name, b.Name));

        sb.Append('{');
        for (var i = 0; i < properties.Count; i++)
        {
            if (i > 0)
            {
                sb.Append(',');
            }
            WriteString(properties[i].Name, sb);
            sb.Append(':');
            Write(properties[i].Value, sb);
        }
        sb.Append('}');
    }

    private static void WriteNumber(JsonElement element, StringBuilder sb)
    {
        var raw = element.GetRawText();
        var digits = raw.StartsWith('-') ? raw.AsSpan(1) : raw.AsSpan();
        if (digits.IsEmpty || digits.ContainsAnyExceptInRange('0', '9'))
        {
            throw new CanonicalJsonException($"Gleitkommazahl nicht erlaubt: {raw}");
        }
        if (!element.TryGetInt64(out var value) || Math.Abs(value) > MaxSafeInteger)
        {
            throw new CanonicalJsonException($"Ganzzahl außerhalb des sicheren Bereichs: {raw}");
        }
        sb.Append(value.ToString(CultureInfo.InvariantCulture));
    }

    private static string ReadString(JsonElement element)
    {
        string value;
        try
        {
            value = element.GetString() ?? string.Empty;
        }
        catch (InvalidOperationException)
        {
            throw new CanonicalJsonException("Einzelnes Surrogat im String");
        }
        EnsureValidUtf16(value);
        return value;
    }

    private static void EnsureValidUtf16(string value)
    {
        for (var i = 0; i < value.Length; i++)
        {
            var c = value[i];
            if (char.IsHighSurrogate(c))
            {
                if (i + 1 >= value.Length || !char.IsLowSurrogate(value[i + 1]))
                {
                    throw new CanonicalJsonException("Einzelnes Surrogat im String");
                }
                i++;
            }
            else if (char.IsLowSurrogate(c))
            {
                throw new CanonicalJsonException("Einzelnes Surrogat im String");
            }
        }
    }

    private static void WriteString(string value, StringBuilder sb)
    {
        sb.Append('"');
        foreach (var c in value)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20)
                    {
                        sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        sb.Append(c);
                    }
                    break;
            }
        }
        sb.Append('"');
    }
}
