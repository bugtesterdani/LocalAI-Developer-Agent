cmake_minimum_required(VERSION 3.20)
project(Rechner LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(rechner src/rechner.cpp)
target_include_directories(rechner PUBLIC include)

enable_testing()
add_executable(rechner_tests tests/rechner_tests.cpp)
target_link_libraries(rechner_tests PRIVATE rechner)

# Jeder Testfall ist ein eigener CTest-Eintrag (eigene Zeile im JUnit-Bericht).
foreach(fall addieren multiplizieren)
    add_test(NAME ${fall} COMMAND rechner_tests ${fall})
endforeach()
