"""Connection management without any GTK: a stoppable background `idle`
watcher, and a call wrapper that reconnects after a dropped connection."""
import threading

from .mpd_client import CommandError, MPDClient, MPDError


class IdleWatcher:
    """Runs MPD's `idle` on its own connection and reports changed subsystems.

    on_change(changed) is called from the background thread. The watcher
    reconnects after errors (waiting retry_seconds). stop() interrupts a
    blocked idle() and ends the thread, so the connection settings can change.
    A watcher is single-use: create a new one after stop().
    """

    def __init__(self, cfg, on_change, client_factory=MPDClient, retry_seconds=2.0, partition=None):
        self._cfg = cfg
        self._partition = partition  # idle must watch the same partition the UI shows
        self._on_change = on_change
        self._factory = client_factory
        self._retry = retry_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._client = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="mpd-idle")

    def start(self):
        self._thread.start()

    def stop(self, join_timeout=None):
        self._stop.set()
        with self._lock:
            client = self._client
        if client is not None:
            client.abort()  # wakes a blocked idle(); the thread closes the client
        if join_timeout is not None and self._thread.is_alive():
            self._thread.join(join_timeout)

    @property
    def running(self):
        return self._thread.is_alive()

    def _run(self):
        while not self._stop.is_set():
            extra = {"partition": self._partition} if self._partition else {}
            client = self._factory(*self._cfg, **extra)
            with self._lock:
                self._client = client
            try:
                client.connect()
                while not self._stop.is_set():
                    changed = client.idle()
                    if not self._stop.is_set():
                        self._on_change(changed)
            except (OSError, MPDError):
                pass  # dropped, refused or aborted: fall through to retry/stop
            finally:
                client.close()
            if self._stop.wait(self._retry):
                break


def call_with_reconnect(client, fn, *args, on_reconnect=None):
    """Run fn(*args). If the client had lost its connection, reconnect once
    and retry.

    Retrying is safe because a client that is not connected never sent the
    command. Errors from a live connection (including CommandError, i.e. the
    server saying no) propagate untouched; the caller should close() the
    client after a non-CommandError so it starts clean next time.
    """
    try:
        return fn(*args)
    except CommandError:
        raise
    except (OSError, MPDError):
        if client.connected:
            raise
        client.connect()
        if on_reconnect:
            on_reconnect()
        return fn(*args)


class ChangeBatcher:
    """Merges MPD change notifications that arrive while the UI thread is busy.

    The idle thread reports every change at once. Adding thousands of songs is
    done in several command lists while the UI thread waits, so one "Add to
    Queue" produced one "playlist" report per list, and each report then
    rebuilt the whole queue (six rebuilds for 3000 songs). add() is called on
    the idle thread and says whether a handler must be scheduled (only for the
    first report since the last take()); take() runs on the UI thread and returns
    every subsystem reported meanwhile, once each, in arrival order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = []

    def add(self, changed):
        with self._lock:
            first = not self._pending
            for name in changed:
                if name not in self._pending:
                    self._pending.append(name)
            return first and bool(self._pending)

    def take(self):
        with self._lock:
            out, self._pending = self._pending, []
            return out
