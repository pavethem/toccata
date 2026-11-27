import socket
import threading
import time
import unittest

from toccata.connection import IdleWatcher, call_with_reconnect
from toccata.mpd_client import CommandError, MPDClient, ProtocolError
from tests.test_mpd_client import Handler, Server


class TimeoutHandler(Handler):
    """Like MPD with connection_timeout: the first connection is closed by the
    server without any command; later connections are served normally."""
    closed = threading.Event()

    def handle(self):
        if not TimeoutHandler.closed.is_set():
            self.w("OK MPD 0.23.5\n")
            self.wfile.flush()
            self.request.close()
            TimeoutHandler.closed.set()
            return
        super().handle()


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @property
    def cfg(self):
        return ("127.0.0.1", self.port, None)


class TestIdleWatcher(ServerCase):
    def watcher(self, **kw):
        got, evt = [], threading.Event()
        w = IdleWatcher(kw.pop("cfg", self.cfg), lambda ch: (got.append(ch), evt.set()),
                        retry_seconds=kw.pop("retry_seconds", 0.05), **kw)
        self.addCleanup(w.stop, 3)
        return w, got, evt

    def test_delivers_changes(self):
        w, got, evt = self.watcher()
        w.start()
        self.assertTrue(evt.wait(3))
        self.assertEqual(got[0], ["player", "mixer"])

    def test_stop_interrupts_blocked_idle_and_ends_thread(self):
        w, got, evt = self.watcher()
        w.start()
        self.assertTrue(evt.wait(3))   # first change delivered, now blocked in idle
        w.stop(join_timeout=3)
        self.assertFalse(w.running, "thread still alive after stop()")

    def test_no_callbacks_after_stop(self):
        w, got, evt = self.watcher()
        w.start()
        evt.wait(3)
        w.stop(join_timeout=3)
        n = len(got)
        threading.Event().wait(0.2)
        self.assertEqual(len(got), n)

    def test_stop_immediately_after_start_does_not_hang(self):
        for _ in range(20):  # shake out start/stop races
            w, _, _ = self.watcher()
            w.start()
            w.stop(join_timeout=3)
            self.assertFalse(w.running)

    def test_stop_before_start_is_harmless(self):
        w, _, _ = self.watcher()
        w.stop()

    def test_retries_until_the_server_is_reachable(self):
        attempts = []

        def flaky(*cfg):
            c = MPDClient(*cfg)
            if len(attempts) < 3:
                attempts.append(1)
                c.connect = lambda: (_ for _ in ()).throw(ConnectionRefusedError())
            return c
        w, got, evt = self.watcher(client_factory=flaky)
        w.start()
        self.assertTrue(evt.wait(3))
        self.assertEqual(len(attempts), 3)

    def test_stops_while_waiting_to_retry(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()  # nothing listens: every attempt is refused
        w, _, _ = self.watcher(cfg=("127.0.0.1", port, None), retry_seconds=30)
        w.start()
        threading.Event().wait(0.2)
        w.stop(join_timeout=3)  # must not wait out the 30 s retry delay
        self.assertFalse(w.running)


class TestCallWithReconnect(ServerCase):
    def client(self):
        c = MPDClient(*self.cfg)
        self.addCleanup(c.close)
        return c

    def test_plain_success_does_not_reconnect(self):
        c = self.client()
        c.connect()
        calls = []
        self.assertEqual(call_with_reconnect(c, c.status, on_reconnect=lambda: calls.append(1))["state"], "play")
        self.assertEqual(calls, [])

    def test_reconnects_once_when_connection_was_lost(self):
        c = self.client()
        c.connect()
        c.close()  # simulate a dropped connection
        reconnects = []
        st = call_with_reconnect(c, c.status, on_reconnect=lambda: reconnects.append(1))
        self.assertEqual(st["state"], "play")
        self.assertEqual(reconnects, [1])
        self.assertTrue(c.connected)

    def test_fn_runs_exactly_twice_on_retry(self):
        c = self.client()
        calls = []

        def fn():
            calls.append(1)
            return c.status()
        self.assertEqual(call_with_reconnect(c, fn)["state"], "play")
        self.assertEqual(len(calls), 2)  # first attempt: not connected; second: after reconnect

    def test_command_error_propagates_without_reconnect(self):
        c = self.client()
        c.connect()
        reconnects = []
        with self.assertRaises(CommandError):
            call_with_reconnect(c, c.command, "bogus", on_reconnect=lambda: reconnects.append(1))
        self.assertEqual(reconnects, [])
        self.assertTrue(c.connected)

    def test_error_on_live_connection_is_not_retried(self):
        c = self.client()
        c.connect()
        calls = []

        def fn():
            calls.append(1)
            raise socket.timeout()
        with self.assertRaises(socket.timeout):
            call_with_reconnect(c, fn)
        self.assertEqual(len(calls), 1)  # command may have been sent: never repeat it

    def test_reconnect_failure_raises(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        c = MPDClient("127.0.0.1", port)
        with self.assertRaises(ConnectionRefusedError):
            call_with_reconnect(c, c.status)

    def test_on_reconnect_not_called_if_reconnect_fails(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        c = MPDClient("127.0.0.1", port)
        reconnects = []
        with self.assertRaises(OSError):
            call_with_reconnect(c, c.status, on_reconnect=lambda: reconnects.append(1))
        self.assertEqual(reconnects, [])

    def test_server_closed_idle_connection_is_reconnected(self):
        # MPD closes a client after connection_timeout (60 s) without a command,
        # which happens to the UI connection while a track plays. The next status()
        # (track change) must reconnect, not fail forever (user report 2026-11-12).
        srv = Server(("127.0.0.1", 0), TimeoutHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        TimeoutHandler.closed.clear()
        c = MPDClient("127.0.0.1", srv.server_address[1])
        self.addCleanup(c.close)
        c.connect()
        self.assertTrue(TimeoutHandler.closed.wait(2))
        reconnects = []
        st = call_with_reconnect(c, c.status, on_reconnect=lambda: reconnects.append(1))
        self.assertEqual(st["state"], "play")
        self.assertEqual(reconnects, [1])

    def test_send_on_reset_connection_is_reconnected(self):
        # A second command on a connection the server closed fails in sendall
        # (broken pipe / reset), not in readline. The client must count as not
        # connected then, or call_with_reconnect would never retry.
        srv = Server(("127.0.0.1", 0), TimeoutHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        TimeoutHandler.closed.clear()
        c = MPDClient("127.0.0.1", srv.server_address[1])
        self.addCleanup(c.close)
        c.connect()
        self.assertTrue(TimeoutHandler.closed.wait(2))
        c._sock.sendall(b"status\n")  # reaches the closed peer, which answers with a reset
        time.sleep(0.2)
        with self.assertRaises((BrokenPipeError, ConnectionResetError)):
            c.status()
        self.assertFalse(c.connected)
        st = call_with_reconnect(c, c.status)
        self.assertEqual(st["state"], "play")

    def test_disconnected_status_error_is_protocol_error(self):
        c = MPDClient("127.0.0.1", 1)
        with self.assertRaises(ProtocolError):
            c.status()


if __name__ == "__main__":
    unittest.main()


class TestChangeBatcher(unittest.TestCase):
    def test_reports_while_busy_become_one_call(self):
        from toccata.connection import ChangeBatcher
        b = ChangeBatcher()
        scheduled = [b.add(["playlist"]) for _ in range(6)]
        self.assertEqual(scheduled, [True] + [False] * 5)
        self.assertEqual(b.take(), ["playlist"])
        self.assertEqual(b.take(), [])

    def test_subsystems_merged_in_order(self):
        from toccata.connection import ChangeBatcher
        b = ChangeBatcher()
        b.add(["playlist", "player"]); b.add(["mixer", "playlist"])
        self.assertEqual(b.take(), ["playlist", "player", "mixer"])

    def test_after_take_the_next_report_schedules_again(self):
        from toccata.connection import ChangeBatcher
        b = ChangeBatcher()
        self.assertTrue(b.add(["player"]))
        b.take()
        self.assertTrue(b.add(["player"]))

    def test_empty_report_schedules_nothing(self):
        from toccata.connection import ChangeBatcher
        b = ChangeBatcher()
        self.assertFalse(b.add([]))
        self.assertTrue(b.add(["options"]))
