"""Line-delimited JSON-RPC + event stream between g935-dspd and g935-control."""
from __future__ import annotations

import errno
import json
import logging
import os
import socket
import threading

from g935.paths import runtime_dir

log = logging.getLogger("g935.daemon_ipc")

IPC_SOCKET_NAME = "g935-dspd.sock"
MAX_LINE_BYTES = 65536


def daemon_socket_path() -> str:
    return os.path.join(runtime_dir(), IPC_SOCKET_NAME)


def daemon_ipc_available(path: str | None = None) -> bool:
    sock_path = path or daemon_socket_path()
    return os.path.exists(sock_path)


class DaemonIpcServer:
    """Non-blocking UNIX stream server designed to sit in a select() loop."""

    def __init__(self, path: str | None = None, on_disconnect=None):
        self.path = path or daemon_socket_path()
        self.on_disconnect = on_disconnect
        self._server: socket.socket | None = None
        self._clients: dict[int, socket.socket] = {}
        self._buffers: dict[int, bytearray] = {}
        self._bind()

    def _bind(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            os.unlink(self.path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        srv.listen(8)
        srv.setblocking(False)
        self._server = srv

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def filenos(self) -> list[int]:
        fds: list[int] = []
        if self._server is not None:
            fds.append(self._server.fileno())
        fds.extend(self._clients.keys())
        return fds

    def _drop_client(self, fd: int) -> None:
        sock = self._clients.pop(fd, None)
        self._buffers.pop(fd, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            if self.on_disconnect is not None:
                try:
                    self.on_disconnect(fd)
                except Exception:
                    pass

    def _send_line(self, fd: int, payload: dict) -> bool:
        sock = self._clients.get(fd)
        if sock is None:
            return False
        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            sock.sendall(raw)
            return True
        except OSError:
            self._drop_client(fd)
            return False

    def broadcast(self, event: str, data: dict) -> None:
        if not self._clients:
            return
        msg = {"event": event, "data": data}
        for fd in list(self._clients):
            self._send_line(fd, msg)

    def service(self, readable_fds: list[int], handler) -> None:
        """Accept new connections and dispatch complete JSON-RPC lines."""
        srv_fd = self._server.fileno() if self._server is not None else None
        if srv_fd is not None and srv_fd in readable_fds:
            while True:
                try:
                    conn, _addr = self._server.accept()
                except BlockingIOError:
                    break
                except OSError:
                    break
                conn.setblocking(False)
                cfd = conn.fileno()
                self._clients[cfd] = conn
                self._buffers[cfd] = bytearray()

        for fd in list(readable_fds):
            if fd == srv_fd or fd not in self._clients:
                continue
            sock = self._clients[fd]
            try:
                chunk = sock.recv(8192)
            except BlockingIOError:
                continue
            except OSError:
                self._drop_client(fd)
                continue
            if not chunk:
                self._drop_client(fd)
                continue
            buf = self._buffers[fd]
            buf.extend(chunk)
            if len(buf) > MAX_LINE_BYTES * 4:
                self._drop_client(fd)
                continue
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                if not line.strip():
                    continue
                self._dispatch_line(fd, line, handler)
                if fd not in self._clients:
                    break

    def _dispatch_line(self, fd: int, raw: bytes, handler) -> None:
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_line(fd, {"id": None, "ok": False, "error": "invalid json"})
            return
        if not isinstance(req, dict):
            return
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        try:
            result = handler(fd, str(method or ""), params)
            if req_id is not None:
                self._send_line(fd, {"id": req_id, "ok": True, "result": result})
        except Exception as exc:
            if req_id is not None:
                self._send_line(fd, {"id": req_id, "ok": False, "error": str(exc)})

    def close(self) -> None:
        for fd in list(self._clients):
            self._drop_client(fd)
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            os.unlink(self.path)
        except OSError:
            pass


class DaemonIpcClient:
    """Thread-safe UNIX stream client with async callbacks and event stream."""

    def __init__(self, path: str | None = None, on_event=None,
                 on_disconnect=None):
        self.path = path or daemon_socket_path()
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self._sock: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list, object]] = {}
        self._next_id = 1
        self._closed = False
        self._thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed

    def connect(self, timeout: float = 1.0) -> bool:
        if self._closed:
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            return False
        sock.settimeout(None)
        self._sock = sock
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        return True

    def _send_raw(self, payload: dict) -> bool:
        sock = self._sock
        if sock is None or self._closed:
            return False
        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                sock.sendall(raw)
                return True
            except OSError:
                return False

    def request(self, method: str, params: dict | None = None,
                timeout: float = 2.5) -> tuple[bool, object]:
        if not self.connected:
            return False, "not connected"
        ev = threading.Event()
        box: list = []
        with self._pending_lock:
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = (ev, box, None)
        if not self._send_raw({"id": req_id, "method": method, "params": params or {}}):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            return False, "send failed"
        if not ev.wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            return False, "timeout"
        return box[0], box[1]

    def call_async(self, method: str, params: dict | None = None,
                   cb=None) -> bool:
        if not self.connected:
            if cb is not None:
                cb(False, "not connected")
            return False
        with self._pending_lock:
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = (None, None, cb)
        if not self._send_raw({"id": req_id, "method": method, "params": params or {}}):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            if cb is not None:
                cb(False, "send failed")
            return False
        return True

    def _fail_all_pending(self, reason: str) -> None:
        with self._pending_lock:
            items = list(self._pending.values())
            self._pending.clear()
        for ev, box, cb in items:
            if ev is not None and box is not None:
                box.extend([False, reason])
                ev.set()
            elif cb is not None:
                try:
                    cb(False, reason)
                except Exception:
                    pass

    def _reader_loop(self) -> None:
        buf = bytearray()
        sock = self._sock
        while not self._closed and sock is not None:
            try:
                chunk = sock.recv(8192)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                break
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                if not line.strip():
                    continue
                self._handle_message(line)
        self._sock = None
        self._fail_all_pending("disconnected")
        if not self._closed and self.on_disconnect is not None:
            try:
                self.on_disconnect()
            except Exception:
                pass

    def _handle_message(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        if "event" in msg:
            if self.on_event is not None:
                try:
                    self.on_event(msg["event"], msg.get("data") or {})
                except Exception:
                    pass
            return
        req_id = msg.get("id")
        if req_id is None:
            return
        with self._pending_lock:
            entry = self._pending.pop(req_id, None)
        if entry is None:
            return
        ev, box, cb = entry
        ok = bool(msg.get("ok"))
        payload = msg.get("result") if ok else msg.get("error", "error")
        if ev is not None and box is not None:
            box.extend([ok, payload])
            ev.set()
        elif cb is not None:
            try:
                cb(ok, payload)
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._fail_all_pending("closed")
