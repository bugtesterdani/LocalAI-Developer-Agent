import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("hello", pathlib.Path(__file__).with_name("hello.py"))
hello = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hello)


def test_greeting():
    assert hello.greeting() == "Hallo aus der Sandbox (Python)"
