"""P15 W4: the background interpretation worker (coalesced, bounded, never blocks the incident)."""
import threading
import time


class SlowInterpreter:
    def __init__(self):
        self.calls: list[int] = []
        self.gate = threading.Event()

    def run(self, iid: int) -> int:
        self.calls.append(iid)
        self.gate.wait(2)
        return 1


def test_requests_for_the_same_incident_coalesce_while_one_is_running():
    from app.interpret import InterpretWorker
    fake = SlowInterpreter()
    w = InterpretWorker(fake, retry_after_s=0, min_interval_s=0)
    w.start()
    try:
        w.seen(7)                 # pipeline progress only refreshes incidents someone is watching
        w.seen(8)
        w.request(7)
        time.sleep(0.1)
        for _ in range(5):
            w.request(7)          # arrives while the first run is in progress -> one follow-up run only
        w.request(8)
        fake.gate.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(fake.calls) < 3:
            time.sleep(0.05)
        time.sleep(0.2)
        assert fake.calls.count(7) == 2 and fake.calls.count(8) == 1
    finally:
        w.stop()


def test_request_never_raises_and_returns_immediately():
    from app.interpret import InterpretWorker

    class Boom:
        def run(self, iid):
            raise RuntimeError("x")

    w = InterpretWorker(Boom(), retry_after_s=0)
    w.start()
    try:
        t = time.monotonic()
        w.request(1)
        assert time.monotonic() - t < 0.05
        time.sleep(0.2)
        assert w.alive()
    finally:
        w.stop()


def test_failed_attempts_are_not_retried_on_every_poll():
    from app.interpret import InterpretWorker

    class Zero:
        def __init__(self):
            self.n = 0

        def run(self, iid):
            self.n += 1
            return 0

    z = Zero()
    w = InterpretWorker(z, retry_after_s=60, min_interval_s=0)
    w.start()
    try:
        for _ in range(4):
            w.request(3, idle=True)      # a page poll: only when nothing was tried recently
            time.sleep(0.1)
        assert z.n == 1
        w.request(3)                     # pipeline progress on a watched incident still gets a run
        time.sleep(0.2)
        assert z.n == 2
    finally:
        w.stop()
