from kontakte.model import Kontakt


def test_anzeige_mit_ort():
    assert Kontakt("Jürgen Groß", None, "Köln").anzeige == "Jürgen Groß (Köln)"


def test_anzeige_ohne_ort():
    assert Kontakt("Ana", "ana@example.org").anzeige == "Ana"
