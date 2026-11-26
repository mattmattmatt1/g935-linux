import os
import select
import tempfile
import threading
import time
import unittest

from g935.daemon_ipc import DaemonIpcClient, DaemonIpcServer


class DaemonIpcTests(unittest.TestCase):
    def _start_server(self, path, handler, on_disconnect=None):
        srv = DaemonIpcServer(path=path, on_disconnect=on_disconnect)
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                fds = srv.filenos()
                if not fds:
                    break
                r, _, _ = select.select(fds, [], [], 0.02)
                if r:
                    srv.service(r, handler)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return srv, stop, t

    def test_request_response_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            sock = os.path.join(td, "dspd.sock")
            events = []
            ev_seen = threading.Event()

            def handler(_fd, method, params):
                if method == "echo":
                    return {"got": params.get("v")}
                raise ValueError("bad method")

            srv, stop, t = self._start_server(sock, handler)
            try:
                def on_ev(name, data):
                    events.append((name, data))
                    ev_seen.set()

                cli = DaemonIpcClient(path=sock, on_event=on_ev)
                self.assertTrue(cli.connect(timeout=1.0))
                try:
                    ok, res = cli.request("echo", {"v": 42})
                    self.assertTrue(ok)
                    self.assertEqual(res, {"got": 42})

                    ok2, err = cli.request("missing", {})
                    self.assertFalse(ok2)
                    self.assertIn("bad method", str(err))

                    srv.broadcast("battery", {"pct": 88})
                    self.assertTrue(ev_seen.wait(1.0))
                    self.assertEqual(events, [("battery", {"pct": 88})])
                finally:
                    cli.close()
            finally:
                stop.set()
                t.join(timeout=1.0)
                srv.close()

    def test_disconnect_callback_fires_for_wheel_resume(self):
        with tempfile.TemporaryDirectory() as td:
            sock = os.path.join(td, "dspd.sock")
            disconnected = threading.Event()
            seen_fds = []

            def on_disc(fd):
                seen_fds.append(fd)
                disconnected.set()

            srv, stop, t = self._start_server(
                sock, lambda _fd, _m, _p: {"ok": True}, on_disconnect=on_disc)
            try:
                cli = DaemonIpcClient(path=sock)
                self.assertTrue(cli.connect(timeout=1.0))
                ok, _res = cli.request("ping", {})
                self.assertTrue(ok)
                cli.close()
                self.assertTrue(disconnected.wait(1.0))
                self.assertEqual(len(seen_fds), 1)
            finally:
                stop.set()
                t.join(timeout=1.0)
                srv.close()


if __name__ == "__main__":
    unittest.main()
