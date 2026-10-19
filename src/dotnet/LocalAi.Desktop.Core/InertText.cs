using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace LocalAi.Desktop.Core;

/// <summary>
/// Inerte, begrenzte Darstellung untrusted Texte (Logs, Webinhalte, Agentenausgaben – SEC-14): ANSI-Sequenzen und
/// Steuerzeichen werden entfernt, Bidi- und unsichtbare Zeichen sichtbar markiert (⟦U+202E⟧), die Länge begrenzt.
/// HTML wird ohnehin nur als Text angezeigt (WPF-TextBlock, kein WebView).
/// </summary>
public static partial class InertText
{
    public const int DefaultMaxChars = 200_000;

    [GeneratedRegex(@"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")]
    private static partial Regex Ansi();

    public static string Clean(string? text, int maxChars = DefaultMaxChars)
    {
        if (string.IsNullOrEmpty(text))
        {
            return string.Empty;
        }
        var source = Ansi().Replace(text, string.Empty);
        var sb = new StringBuilder(Math.Min(source.Length, maxChars) + 32);
        foreach (var rune in source.EnumerateRunes())
        {
            if (sb.Length >= maxChars)
            {
                sb.Append($"\n… [gekürzt: Anzeige auf {maxChars} Zeichen begrenzt]");
                break;
            }
            var value = rune.Value;
            if (value is '\n' or '\t')
            {
                sb.Append((char)value);
                continue;
            }
            if (value == '\r')
            {
                continue;
            }
            var category = Rune.GetUnicodeCategory(rune);
            if (category == UnicodeCategory.Control)
            {
                continue;
            }
            if (category == UnicodeCategory.Format || value is 0x00AD or 0x034F or 0x115F or 0x1160 or 0x3164 or 0xFFA0
                || (value >= 0xFE00 && value <= 0xFE0F) || (value >= 0xE0000 && value <= 0xE007F))
            {
                sb.Append(CultureInfo.InvariantCulture, $"⟦U+{value:X4}⟧");
                continue;
            }
            sb.Append(rune.ToString());
        }
        return sb.ToString();
    }
}
