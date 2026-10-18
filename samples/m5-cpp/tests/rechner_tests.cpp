// Kleine Testsammlung ohne Fremdbibliothek: "rechner_tests <fall>" führt einen Fall aus,
// Exitcode 0 = bestanden. Neue Fälle: Funktion schreiben, in FAELLE eintragen und in CMakeLists.txt ergänzen.
#include <cstring>
#include <iostream>

#include "rechner.hpp"

namespace
{
int fehler = 0;

void pruefe(bool ok, const char* ausdruck, int zeile)
{
    if (!ok)
    {
        std::cerr << "FEHLER Zeile " << zeile << ": " << ausdruck << std::endl;
        ++fehler;
    }
}

#define PRUEFE(x) pruefe((x), #x, __LINE__)

void addieren()
{
    PRUEFE(rechner::addieren(2, 3) == 5);
    PRUEFE(rechner::addieren(-2, 2) == 0);
}

void multiplizieren()
{
    PRUEFE(rechner::multiplizieren(4, 5) == 20);
}

struct Fall
{
    const char* name;
    void (*funktion)();
};

const Fall FAELLE[] = {
    {"addieren", addieren},
    {"multiplizieren", multiplizieren},
};
}

int main(int argc, char** argv)
{
    if (argc != 2)
    {
        std::cerr << "Aufruf: rechner_tests <fall>" << std::endl;
        return 2;
    }
    for (const auto& fall : FAELLE)
    {
        if (std::strcmp(fall.name, argv[1]) == 0)
        {
            fall.funktion();
            std::cout << fall.name << (fehler == 0 ? ": ok" : ": fehlgeschlagen") << std::endl;
            return fehler == 0 ? 0 : 1;
        }
    }
    std::cerr << "Unbekannter Fall: " << argv[1] << std::endl;
    return 2;
}
