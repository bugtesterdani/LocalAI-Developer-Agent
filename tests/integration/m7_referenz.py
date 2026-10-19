"""Referenzänderungen für die M7-Abnahme ohne Modell: CSV-Parser (RFC 4180) mit Unicode-, Leerwert- und Fehlerfalltests
für samples/m7-demo-csharp und samples/m7-demo-python, dazu je ein absichtlich falscher Test."""

CS_PARSER = r'''using System.Text;

namespace Kontakte;

/// <summary>CSV nach RFC 4180: Anführungszeichen, verdoppelte Anführungszeichen, Zeilenumbrüche in Feldern; leere Felder = null.</summary>
public static class CsvParser
{
    public static IReadOnlyList<IReadOnlyList<string?>> Parse(string text)
    {
        var rows = new List<IReadOnlyList<string?>>();
        var row = new List<string?>();
        var field = new StringBuilder();
        var quoted = false;
        var wasQuoted = false;
        var line = 1;
        var quoteLine = 0;
        for (var i = 0; i < text.Length; i++)
        {
            var c = text[i];
            if (quoted)
            {
                if (c == '"' && i + 1 < text.Length && text[i + 1] == '"')
                {
                    field.Append('"');
                    i++;
                }
                else if (c == '"')
                {
                    quoted = false;
                }
                else
                {
                    if (c == '\n')
                    {
                        line++;
                    }
                    field.Append(c);
                }
                continue;
            }
            switch (c)
            {
                case '"' when field.Length == 0 && !wasQuoted:
                    quoted = wasQuoted = true;
                    quoteLine = line;
                    break;
                case '"':
                    throw new FormatException($"Zeile {line}: Anführungszeichen mitten im Feld");
                case ',':
                    row.Add(Value(field, wasQuoted));
                    wasQuoted = false;
                    break;
                case '\r':
                    break;
                case '\n':
                    row.Add(Value(field, wasQuoted));
                    rows.Add(row);
                    row = [];
                    wasQuoted = false;
                    line++;
                    break;
                default:
                    if (wasQuoted)
                    {
                        throw new FormatException($"Zeile {line}: Text nach schließendem Anführungszeichen");
                    }
                    field.Append(c);
                    break;
            }
        }
        if (quoted)
        {
            throw new FormatException($"Zeile {quoteLine}: Anführungszeichen nicht geschlossen");
        }
        if (field.Length > 0 || wasQuoted || row.Count > 0)
        {
            row.Add(Value(field, wasQuoted));
            rows.Add(row);
        }
        return rows;
    }

    public static IReadOnlyList<Kontakt> Kontakte(string text) =>
        Parse(text).Skip(1).Select((r, i) => r.Count == 3 && r[0] is { } name
            ? new Kontakt(name, r[1], r[2])
            : throw new FormatException($"Zeile {i + 2}: erwartet Name,Email,Ort")).ToList();

    private static string? Value(StringBuilder field, bool quoted)
    {
        var value = field.ToString();
        field.Clear();
        return value.Length == 0 && !quoted ? null : value;
    }
}
'''

CS_TESTS = r'''namespace Kontakte.Tests;

public sealed class CsvParserTests
{
    [Fact]
    public void Unicode_bleibt_erhalten() =>
        Assert.Equal("Jürgen Groß 😀", CsvParser.Parse("Name\n\"Jürgen Groß 😀\"\n")[1][0]);

    [Fact]
    public void Leere_Felder_sind_null()
    {
        var k = CsvParser.Kontakte("Name,Email,Ort\nAna,,Köln\n")[0];
        Assert.Null(k.Email);
        Assert.Equal("Köln", k.Ort);
    }

    [Fact]
    public void Anfuehrungszeichen_und_Kommas_im_Feld() =>
        Assert.Equal("a, \"b\"", CsvParser.Parse("\"a, \"\"b\"\"\"")[0][0]);

    [Fact]
    public void Offenes_Anfuehrungszeichen_meldet_Zeile() =>
        Assert.Contains("Zeile 2", Assert.Throws<FormatException>(() => CsvParser.Parse("a\n\"b,c\n")).Message);
}
'''

CS_FAILING = r'''namespace Kontakte.Tests;

public sealed class AbsichtlichFalschTests
{
    [Fact]
    public void Absichtlich_falsch() => Assert.Equal("Koeln", CsvParser.Kontakte("Name,Email,Ort\nAna,,Köln\n")[0].Ort);
}
'''

PY_PARSER = r'''"""CSV nach RFC 4180: Anführungszeichen, verdoppelte Anführungszeichen, Zeilenumbrüche in Feldern; leere Felder = None."""

from __future__ import annotations

from kontakte.model import Kontakt


def parse(text: str) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    row: list[str | None] = []
    field: list[str] = []
    quoted = was_quoted = False
    line = quote_line = 1
    i = 0
    while i < len(text):
        c = text[i]
        if quoted:
            if c == '"' and text[i + 1:i + 2] == '"':
                field.append('"')
                i += 1
            elif c == '"':
                quoted = False
            else:
                line += c == "\n"
                field.append(c)
        elif c == '"' and not field and not was_quoted:
            quoted = was_quoted = True
            quote_line = line
        elif c == '"':
            raise ValueError(f"Zeile {line}: Anführungszeichen mitten im Feld")
        elif c == ",":
            row.append(_value(field, was_quoted))
            field, was_quoted = [], False
        elif c == "\r":
            pass
        elif c == "\n":
            row.append(_value(field, was_quoted))
            rows.append(row)
            row, field, was_quoted = [], [], False
            line += 1
        else:
            if was_quoted:
                raise ValueError(f"Zeile {line}: Text nach schließendem Anführungszeichen")
            field.append(c)
        i += 1
    if quoted:
        raise ValueError(f"Zeile {quote_line}: Anführungszeichen nicht geschlossen")
    if field or was_quoted or row:
        row.append(_value(field, was_quoted))
        rows.append(row)
    return rows


def kontakte(text: str) -> list[Kontakt]:
    result = []
    for number, row in enumerate(parse(text)[1:], start=2):
        if len(row) != 3 or row[0] is None:
            raise ValueError(f"Zeile {number}: erwartet Name,Email,Ort")
        result.append(Kontakt(row[0], row[1], row[2]))
    return result


def _value(field: list[str], quoted: bool) -> str | None:
    value = "".join(field)
    return None if not value and not quoted else value
'''

PY_TESTS = r'''import pytest

from kontakte.csv_parser import kontakte, parse


def test_unicode_bleibt_erhalten():
    assert parse('Name\n"Jürgen Groß 😀"\n')[1][0] == "Jürgen Groß 😀"


def test_leere_felder_sind_none():
    k = kontakte("Name,Email,Ort\nAna,,Köln\n")[0]
    assert k.email is None and k.ort == "Köln"


def test_anfuehrungszeichen_und_kommas_im_feld():
    assert parse('"a, ""b"""')[0][0] == 'a, "b"'


def test_offenes_anfuehrungszeichen_meldet_zeile():
    with pytest.raises(ValueError, match="Zeile 2"):
        parse('a\n"b,c\n')
'''

PY_FAILING = r'''from kontakte.csv_parser import kontakte


def test_absichtlich_falsch():
    assert kontakte("Name,Email,Ort\nAna,,Köln\n")[0].ort == "Koeln"
'''
