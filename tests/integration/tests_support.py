import time


def wait_until(condition, timeout: float = 5.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("Bedingung nicht rechtzeitig erfüllt")
        time.sleep(interval)
