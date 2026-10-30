using System.Globalization;
using System.Text;

namespace LocalAi.Contracts;

/// <summary>Ergebnis der Prüfung einer bearbeiteten Eingabe: Fehler sperren, Warnungen nur anzeigen.</summary>
public sealed record LimitCheckResult(IReadOnlyList<string> Errors, IReadOnlyList<string> Warnings)
{
    public bool Ok => Errors.Count == 0;
}

/// <summary>
/// Prüft, was der Nutzer bei der Rückfrage „Schrittlimit erreicht“ eingefügt hat – gemeinsam für Oberfläche (Anzeige
/// beim Tippen) und Broker (endgültige Prüfung). Grundsatz: fast alles ist erlaubt. Gesperrt ist nur, was man beim
/// Bearbeiten nie bewusst einfügt (unsichtbare Steuer- und Richtungszeichen); alles Auffällige ist eine Warnung.
/// Verglichen wird immer mit dem angezeigten Text – was dort schon stand (z. B. aus einer Datei), zählt nicht.
/// </summary>
public static class LimitInputCheck
{
    public const string EvidenceMarker = "<<<EVIDENCE";
    public const string EvidenceEndMarker = "<<<END EVIDENCE";

    /// <summary>Formulierungen, mit denen eingefügter Fremdtext Regeln außer Kraft setzen will (klein geschrieben).</summary>
    private static readonly string[] OverridePhrases =
    [
        "ignore previous", "ignore all previous", "ignore the above", "ignore your instructions", "disregard previous",
        "disregard all", "forget your instructions", "you are now", "new instructions:", "system prompt",
        "without approval", "skip approval", "do not ask for approval", "bypass",
        "ignoriere alle", "ignoriere die vorherigen", "vergiss deine anweisungen", "ohne freigabe", "du bist jetzt",
    ];

    public static LimitCheckResult Check(LimitQuestion question, string system, string task, string rest)
    {
        var errors = new List<string>();
        var warnings = new List<string>();
        var original = question.System + "\n" + question.Task + "\n" + question.Rest;
        var edited = system + "\n" + task + "\n" + rest;

        if (string.IsNullOrWhiteSpace(task))
        {
            errors.Add("Die Aufgabe darf nicht leer sein.");
        }

        var hidden = AddedHiddenCharacters(original, edited);
        if (hidden.Count > 0)
        {
            errors.Add("Unsichtbare Steuer- bzw. Richtungszeichen eingefügt (" + string.Join(", ", hidden) + "). Sie können "
                       + "Text vor dir verbergen und werden nie bewusst getippt – bitte entfernen (meist aus kopiertem Text).");
        }

        if (Count(edited, EvidenceMarker) > Count(original, EvidenceMarker) || Count(edited, EvidenceEndMarker) > Count(original, EvidenceEndMarker))
        {
            warnings.Add("Eingefügter Text sieht aus wie Evidenz („<<<EVIDENCE …“). Das Modell hält ihn dann für den Inhalt "
                         + "einer Datei bzw. ein Werkzeugergebnis, obwohl er von dir stammt.");
        }

        if (system != question.System)
        {
            warnings.Add("Die Regeln (Systemnachricht) sind geändert – nur für den nächsten Schritt.");
            if (question.System.Contains("\"action\"", StringComparison.Ordinal) && !system.Contains("\"action\"", StringComparison.Ordinal))
            {
                warnings.Add("Das Aktionsformat („\"action\": …“) fehlt in den Regeln – der Agent liefert dann womöglich keine "
                             + "gültige Aktion.");
            }
        }

        var lowerOriginal = original.ToLowerInvariant();
        var lowerEdited = edited.ToLowerInvariant();
        var phrases = OverridePhrases.Where(p => Count(lowerEdited, p) > Count(lowerOriginal, p)).ToList();
        if (phrases.Count > 0)
        {
            warnings.Add("Enthält Formulierungen, die Regeln außer Kraft setzen sollen („" + string.Join("“, „", phrases.Take(3))
                         + "“). Stammt das von dir? Bei eingefügtem Fremdtext (Webseite, Ticket, Datei) bitte prüfen.");
        }

        if (task.Trim() != question.Task.Trim() && question.Task.Length > 200 && task.Trim().Length < question.Task.Trim().Length / 4)
        {
            warnings.Add("Die Aufgabe ist viel kürzer als vorher – sie gilt ab jetzt für alle weiteren Schritte.");
        }
        return new LimitCheckResult(errors, warnings);
    }

    /// <summary>Unsichtbare Zeichen, die im bearbeiteten Text häufiger vorkommen als im angezeigten (als „U+202E“).</summary>
    private static List<string> AddedHiddenCharacters(string original, string edited)
    {
        var before = HiddenCounts(original);
        return HiddenCounts(edited)
            .Where(kv => kv.Value > before.GetValueOrDefault(kv.Key))
            .Select(kv => "U+" + kv.Key.ToString("X4", CultureInfo.InvariantCulture))
            .Order(StringComparer.Ordinal)
            .ToList();
    }

    private static Dictionary<int, int> HiddenCounts(string text)
    {
        var counts = new Dictionary<int, int>();
        foreach (var rune in text.EnumerateRunes())
        {
            if (IsHidden(rune))
            {
                counts[rune.Value] = counts.GetValueOrDefault(rune.Value) + 1;
            }
        }
        return counts;
    }

    public static bool IsHidden(Rune rune) => rune.Value switch
    {
        '\t' or '\n' or '\r' => false,
        < 0x20 or 0x7F => true, // Steuerzeichen
        >= 0x80 and <= 0x9F => true,
        >= 0x200B and <= 0x200F => true, // Nullbreite, Richtungsmarken
        >= 0x202A and <= 0x202E => true, // Richtungs-Einbettung/-Überschreibung
        >= 0x2060 and <= 0x2064 => true, // Wortverbinder, unsichtbare Operatoren
        >= 0x2066 and <= 0x2069 => true, // Richtungs-Isolation
        0xFEFF => true, // Byte Order Mark mitten im Text
        >= 0xE0000 and <= 0xE007F => true, // Tag-Zeichen (unsichtbarer ASCII-Text)
        _ => false,
    };

    private static int Count(string text, string part)
    {
        var n = 0;
        for (var i = text.IndexOf(part, StringComparison.Ordinal); i >= 0; i = text.IndexOf(part, i + part.Length, StringComparison.Ordinal))
        {
            n++;
        }
        return n;
    }
}
