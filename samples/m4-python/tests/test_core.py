from rechner import addieren, multiplizieren, subtrahieren


def test_addieren():
    assert addieren(2, 3) == 5


def test_subtrahieren():
    assert subtrahieren(2, 3) == -1


def test_multiplizieren():
    assert multiplizieren(2, 3) == 6
