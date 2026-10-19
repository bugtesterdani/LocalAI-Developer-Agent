using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text.RegularExpressions;
using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>Abgerufene, im Content Gate bereinigte Webinhalte (Quarantäne des Brokers, Spezifikation §7).</summary>
public interface IWebCaptures
{
    /// <summary>Größenlimit eines Abrufs (Standard 2 MiB).</summary>
    long MaxBytes { get; }

    /// <summary>Erlaubte Hosts (<c>web.allowedHosts</c>); leer = alle öffentlichen.</summary>
    IReadOnlyList<string> AllowedHosts => [];

    WebCapture? Find(string captureId);
}

/// <summary>Vorbereitete Übernahmen (Workspace.Diff → Staging), Grundlage für Workspace.ExportPatch.</summary>
public interface IExportStaging
{
    ExportPreview? Find(string stagingId);
}

/// <summary>
/// URL-Regeln für Web.Fetch – identisch zu localai/web/netcheck.py. Der Fetcher prüft zusätzlich alle DNS-Antworten
/// und verbindet sich nur mit der geprüften Adresse (SEC-11).
/// </summary>
public static partial class UrlPolicy
{
    public const int MaxUrlLength = 2048;

    private static readonly string[] BlockedSuffixes =
    [
        ".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".home.arpa", ".corp", ".localdomain", ".arpa",
        ".onion", ".test", ".invalid", ".example",
    ];

    [GeneratedRegex(@"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+))*\.?$", RegexOptions.IgnoreCase)]
    private static partial Regex NumericHost();

    [GeneratedRegex(@"^[a-z0-9.-]+$")]
    private static partial Regex HostChars();

    /// <summary>
    /// Host gegen <c>web.allowedHosts</c>: <c>docs.python.org</c> genau dieser Host, <c>*.microsoft.com</c> alle
    /// Unterdomänen (nicht die Domain selbst). Leere Liste = alle (öffentlichen) Hosts.
    /// </summary>
    public static bool HostAllowed(string host, IReadOnlyList<string> allowed)
    {
        if (allowed.Count == 0)
        {
            return true;
        }
        host = host.TrimEnd('.').ToLowerInvariant();
        return allowed.Any(a => a.StartsWith("*.", StringComparison.Ordinal)
            ? host.EndsWith(a[1..], StringComparison.Ordinal) && host.Length > a.Length - 1
            : host == a);
    }

    [GeneratedRegex(@"^(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+$")]
    private static partial Regex AllowedHostPattern();

    /// <summary>Eintrag für <c>web.allowedHosts</c> normalisieren (IDNA, klein) oder null, wenn ungültig.</summary>
    public static string? NormalizeAllowedHost(string entry)
    {
        var text = entry.Trim().TrimEnd('.');
        var wildcard = text.StartsWith("*.", StringComparison.Ordinal);
        try
        {
            var ascii = new IdnMapping().GetAscii(wildcard ? text[2..] : text).ToLowerInvariant();
            var normalized = (wildcard ? "*." : "") + ascii;
            return AllowedHostPattern().IsMatch(normalized) ? normalized : null;
        }
        catch (ArgumentException)
        {
            return null;
        }
    }

    /// <summary>Gibt die URL ohne Fragment zurück oder wirft 403 mit deutscher Begründung.</summary>
    public static (string Url, string Host) Validate(string? url)
    {
        if (string.IsNullOrEmpty(url) || url.Length > MaxUrlLength)
        {
            throw Denied($"URL fehlt oder ist länger als {MaxUrlLength} Zeichen");
        }
        if (url.Any(c => c <= ' ' || c == '\u007f'))
        {
            throw Denied("URL enthält Leer- oder Steuerzeichen");
        }
        if (!url.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
        {
            throw Denied("nur https:// ist erlaubt");
        }
        var authorityEnd = url.IndexOfAny(['/', '?', '#'], 8);
        var authority = authorityEnd < 0 ? url[8..] : url[8..authorityEnd];
        if (authority.Contains('@'))
        {
            throw Denied("Zugangsdaten in der URL sind nicht erlaubt");
        }
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps)
        {
            throw Denied("URL ist ungültig");
        }
        if (uri.Port != 443)
        {
            throw Denied("nur Port 443 ist erlaubt");
        }
        string host;
        if (uri.HostNameType is UriHostNameType.IPv4 or UriHostNameType.IPv6)
        {
            var rawHost = authority.StartsWith('[') ? authority[1..authority.IndexOf(']')] : authority.Split(':')[0];
            if (uri.HostNameType == UriHostNameType.IPv4 && !IsCanonicalIPv4(rawHost))
            {
                throw Denied("IP-Adresse in ungewöhnlicher Schreibweise – nur kanonisch (z. B. 93.184.215.14)");
            }
            var address = IPAddress.Parse(uri.HostNameType == UriHostNameType.IPv6 ? uri.Host.Trim('[', ']') : uri.Host);
            if (!IsPublic(address))
            {
                throw Denied($"Adresse {address} ist nicht öffentlich (privat, lokal oder reserviert)");
            }
            host = uri.Host;
        }
        else
        {
            var rawHost = authority.Split(':')[0];
            if (NumericHost().IsMatch(rawHost))
            {
                throw Denied("IP-Adresse in ungewöhnlicher Schreibweise – nur kanonisch (z. B. 93.184.215.14)");
            }
            try
            {
                host = new IdnMapping().GetAscii(uri.IdnHost).ToLowerInvariant();
            }
            catch (ArgumentException)
            {
                throw Denied("Hostname ist kein gültiger Domainname");
            }
            if (!host.TrimEnd('.').Contains('.') || host == "localhost" || BlockedSuffixes.Any(s => host.TrimEnd('.').EndsWith(s, StringComparison.Ordinal)))
            {
                throw Denied($"Hostname {host} ist kein öffentlicher Domainname");
            }
            if (!HostChars().IsMatch(host))
            {
                throw Denied($"Hostname {host} ist ungültig");
            }
        }
        var fragment = url.IndexOf('#', StringComparison.Ordinal);
        return (fragment < 0 ? url : url[..fragment], host);
    }

    private static bool IsCanonicalIPv4(string text) =>
        IPAddress.TryParse(text, out var a) && a.AddressFamily == AddressFamily.InterNetwork && a.ToString() == text;

    /// <summary>Nur global erreichbare Unicast-Adressen; IPv4 in IPv6 wird auf die eingebettete Adresse geprüft.</summary>
    public static bool IsPublic(IPAddress address)
    {
        if (address.AddressFamily == AddressFamily.InterNetworkV6)
        {
            if (address.IsIPv4MappedToIPv6)
            {
                return IsPublic(address.MapToIPv4());
            }
            var b = address.GetAddressBytes();
            if (b.All(x => x == 0) || (b.Take(15).All(x => x == 0) && b[15] == 1))
            {
                return false; // :: und ::1
            }
            if ((b[0] & 0xfe) == 0xfc || (b[0] == 0xfe && (b[1] & 0xc0) == 0x80) || b[0] == 0xff)
            {
                return false; // ULA, link-local, Multicast
            }
            if (b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x0d && b[3] == 0xb8)
            {
                return false; // Dokumentation
            }
            if (b[0] == 0x20 && b[1] == 0x02)
            {
                return IsPublic(new IPAddress(b[2..6])); // 6to4
            }
            if (b[0] == 0x20 && b[1] == 0x01 && b[2] == 0 && b[3] == 0)
            {
                return false; // Teredo – eingebettete Adressen nicht eindeutig prüfbar
            }
            if (b[0] == 0x00 && b[1] == 0x64 && b[2] == 0xff && b[3] == 0x9b)
            {
                return IsPublic(new IPAddress(b[12..16])); // NAT64
            }
            return (b[0] & 0xe0) == 0x20; // 2000::/3 global unicast
        }
        var v = address.GetAddressBytes();
        return !(v[0] == 0 || v[0] == 10 || v[0] == 127 || v[0] >= 224
                 || (v[0] == 100 && (v[1] & 0xc0) == 64)
                 || (v[0] == 169 && v[1] == 254)
                 || (v[0] == 172 && (v[1] & 0xf0) == 16)
                 || (v[0] == 192 && v[1] == 168)
                 || (v[0] == 192 && v[1] == 0 && v[2] is 0 or 2)
                 || (v[0] == 192 && v[1] == 88 && v[2] == 99)
                 || (v[0] == 198 && v[1] is 18 or 19)
                 || (v[0] == 198 && v[1] == 51 && v[2] == 100)
                 || (v[0] == 203 && v[1] == 0 && v[2] == 113));
    }

    private static BrokerException Denied(string message) => new(BrokerErrorCodes.Forbidden, message);
}
