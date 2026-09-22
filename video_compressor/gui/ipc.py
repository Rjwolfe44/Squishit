"""Single-instance IPC for forwarding open requests to the running UI."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional


HOST = "127.0.0.1"
PORT = 49627


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        callback = getattr(server, "callback", None)
        if callback is None:
            return

        payload = self.rfile.readline().decode("utf-8").strip()
        if not payload:
            return

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return

        paths = [Path(item) for item in data.get("paths", []) if item]
        callback(paths)
        self.wfile.write(b"ok\n")


class SingleInstanceServer:
    def __init__(self, callback: Callable[[list[Path]], None]):
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._callback = callback

    def start(self) -> bool:
        try:
            class _Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            server = _Server((HOST, PORT), _RequestHandler)
            server.callback = self._callback
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, daemon=True)
            self._thread.start()
            return True
        except OSError:
            return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def send_open_request(paths: Iterable[Path]) -> bool:
    payload = json.dumps({"paths": [str(Path(path)) for path in paths]}) + "\n"
    try:
        with socket.create_connection((HOST, PORT), timeout=0.35) as sock:
            sock.sendall(payload.encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            return True
    except OSError:
        return False