using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace LocalAi.Desktop.Core;

/// <summary>
/// Ausgehende Webanfrage für die erste Freigabe (Web.Fetch): genau die Bytes, die gesendet werden (fest im Fetcher,
/// localai/web/fetch.py), die übertragenen Daten (Pfad, Query) einzeln dekodiert und Hinweise auf mögliche
/// Datenabflüsse (z. B. Projektinhalte, die ein Agent in die URL kodiert hat).
/// </summary>
public static partial class WebRequestPreview
{
    // Muss zu localai/web/fetch.py passen (geprüft in M6Tests).
    public const string UserAgent = "LocalAI-WebFetch/1 (offline developer assistant; single approved request)";

    public static readonly string[] TextTypes =
    [
        "text/html", "application/xhtml+xml", "text/plain", "text/markdown", "text/x-markdown", "text/csv",
        "application/json", "application/xml", "text/xml", "text/x-rst", "application/ld+json",
    ];

    public static string Accept => string.Join(", ", TextTypes) + ";q=0.9, */*;q=0.1";

    [GeneratedRegex("^[A-Za-z0-9+/=_-]{24,}$")]
    private static partial Regex Base64Like();

    [GeneratedRegex("^[0-9a-fA-F]{24,}$")]
    private static partial Regex HexLike();

    [GeneratedRegex("%[0-9a-fA-F]{2}")]
    private static partial Regex Escaped();

    /// <summary>Anfragezeile und Kopfzeilen; <paramref name="route"/> aus dem Broker-Status (direkt oder Proxy).</summary>
    public static string RequestText(string url, string? route)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return InertText.Clean(url, 2100);
        }
        var host = uri.IdnHost;
        var lines = new List<string>
        {
            $"Weg: {route ?? "laut Broker-Einstellung"}",
            "",
            $"GET {uri.PathAndQuery} HTTP/1.1",
            $"Host: {host}",
            $"User-Agent: {UserAgent}",
            $"Accept: {Accept}",
            "Accept-Encoding: identity",
            "Connection: close",
            "",
            "(kein Inhalt, keine Cookies, keine Anmeldung beim Zielserver)",
        };
        return InertText.Clean(string.Join("\n", lines), 6000);
    }

    /// <summary>Pfadsegmente und Query-Parameter einzeln dekodiert – das sind die Daten, die die Anfrage überträgt.</summary>
    public static IReadOnlyList<(string Part, string Value)> DataParts(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return [];
        }
        var parts = new List<(string, string)>();
        foreach (var segment in uri.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries))
        {
            parts.Add(("Pfad", Uri.UnescapeDataString(segment)));
        }
        foreach (var pair in uri.Query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var kv = pair.Split('=', 2);
            var name = Uri.UnescapeDataString(kv[0].Replace('+', ' '));
            parts.Add(($"Parameter {name}", kv.Length > 1 ? Uri.UnescapeDataString(kv[1].Replace('+', ' ')) : ""));
        }
        return parts;
    }

    public static string DataText(string url)
    {
        var parts = DataParts(url);
        if (parts.Count == 0)
        {
            return "(keine – nur der Host)";
        }
        var sb = new StringBuilder();
        foreach (var (part, value) in parts)
        {
            sb.Append(CultureInfo.InvariantCulture, $"{part}: {value}\n");
        }
        return InertText.Clean(sb.ToString().TrimEnd(), 6000);
    }

    /// <summary>Hinweise auf Daten, die über die URL abfließen könnten (keine Garantie – du entscheidest).</summary>
    public static IReadOnlyList<string> DataWarnings(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return [];
        }
        var warnings = new List<string>();
        if (uri.Query.Length > 120)
        {
            warnings.Add($"Lange Query ({uri.Query.Length} Zeichen) – enthält sie Inhalte aus deinem Projekt?");
        }
        if (Escaped().Matches(uri.PathAndQuery).Count >= 10)
        {
            warnings.Add("Viele kodierte Zeichen in der URL – dekodierte Werte unten prüfen.");
        }
        foreach (var (part, value) in DataParts(url))
        {
            var compact = value.Trim();
            if (compact.Contains('\n') || compact.Contains('\r'))
            {
                warnings.Add($"{part} enthält Zeilenumbrüche (wie Code oder Text aus Dateien).");
            }
            else if (Base64Like().IsMatch(compact) || HexLike().IsMatch(compact))
            {
                warnings.Add($"{part} sieht wie kodierte Daten aus ({compact.Length} Zeichen).");
            }
            else if (compact.Length >= 60)
            {
                warnings.Add($"{part} ist ungewöhnlich lang ({compact.Length} Zeichen).");
            }
        }
        return warnings.Distinct().Take(10).ToList();
    }
}
