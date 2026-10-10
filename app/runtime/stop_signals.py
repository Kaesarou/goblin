"""Convert process termination into an orderly runtime stop request."""

import signal
from contextlib import contextmanager


@contextmanager
def runtime_stop_signals(request_stop):
    """Keep handlers installed through cleanup; never do I/O in a handler."""
    previous = {}

    def request(_signum, _frame):
        request_stop()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, request)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
