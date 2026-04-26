import argparse
import atexit
import os
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
import socketio

from shared_protocol import (
    HttpRequestMessage,
    HttpResponseMessage,
    TunnelCloseMessage,
    TunnelDataMessage,
    TunnelOpenMessage,
    b64_encode,
    new_request_id,
)


REQUEST_TIMEOUT_SECONDS = 40
SOCKET_IO_PATH = "/socket.io"
IS_WINDOWS = os.name == "nt"
LISTEN_HOST = "127.0.0.1"

HOP_BY_HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
}


def parse_target(path: str, headers: Dict[str, str]) -> Tuple[str, int, str]:
    if path.startswith("http://") or path.startswith("https://"):
        parsed = urlsplit(path)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += f"?{parsed.query}"
        return host, port, request_path

    host_header = headers.get("Host", "")
    if ":" in host_header:
        host, port_s = host_header.rsplit(":", 1)
        port = int(port_s)
    else:
        host = host_header
        port = 80
    return host, port, path


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    clean = {}
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        clean[key] = value
    return clean


class SocketIoBridgeClient:
    def __init__(self, remote_url: str):
        self.remote_url = remote_url
        # Important: do not use system proxy for A->B control channel.
        # Otherwise, after enabling system proxy, this client can recurse
        # back into local proxy and create CONNECT loops.
        self.http_session = requests.Session()
        self.http_session.trust_env = False
        self.sio = socketio.Client(
            reconnection=True,
            logger=False,
            engineio_logger=False,
            http_session=self.http_session,
        )
        self._pending: Dict[str, Tuple[threading.Event, Optional[HttpResponseMessage]]] = {}
        self._tunnel_open_events: Dict[str, threading.Event] = {}
        self._tunnel_close_events: Dict[str, threading.Event] = {}
        self._tunnel_close_reasons: Dict[str, str] = {}
        self._tunnel_chunks: Dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._connect_lock = threading.Lock()

        @self.sio.event
        def connect():
            print("[local] connected to remote proxy")

        @self.sio.event
        def disconnect():
            print("[local] disconnected from remote proxy")

        @self.sio.on("proxy:http_response")
        def on_response(payload: str):
            message = HttpResponseMessage.from_json(payload)
            with self._lock:
                state = self._pending.get(message.request_id)
            if state is None:
                return
            event, _ = state
            with self._lock:
                self._pending[message.request_id] = (event, message)
            event.set()

        @self.sio.on("proxy:tunnel_opened")
        def on_tunnel_opened(payload: str):
            msg = TunnelOpenMessage.from_json(payload)
            with self._lock:
                event = self._tunnel_open_events.get(msg.tunnel_id)
            if event:
                event.set()

        @self.sio.on("proxy:tunnel_data")
        def on_tunnel_data(payload: str):
            msg = TunnelDataMessage.from_json(payload)
            with self._lock:
                self._tunnel_chunks[msg.tunnel_id] = self._tunnel_chunks.get(msg.tunnel_id, b"") + msg.data

        @self.sio.on("proxy:tunnel_close")
        def on_tunnel_close(payload: str):
            msg = TunnelCloseMessage.from_json(payload)
            with self._lock:
                self._tunnel_close_reasons[msg.tunnel_id] = msg.reason or "remote closed"
                event = self._tunnel_close_events.get(msg.tunnel_id)
            if event:
                event.set()

    def connect(self):
        with self._connect_lock:
            if self.sio.connected:
                return
            try:
                self.sio.connect(self.remote_url, socketio_path=SOCKET_IO_PATH, transports=["websocket", "polling"])
            except Exception as exc:
                # During concurrent reconnect attempts socketio may report
                # "Client is not in a disconnected state". Treat this as
                # non-fatal and let ensure_connected() re-check.
                if "not in a disconnected state" not in str(exc):
                    raise

    def ensure_connected(self) -> bool:
        if self.sio.connected:
            return True
        try:
            self.connect()
            return self.sio.connected
        except Exception as exc:
            print(f"[local] failed to reconnect to remote proxy: {exc}")
            return False

    def disconnect(self):
        with self._connect_lock:
            if self.sio.connected:
                self.sio.disconnect()
        self.http_session.close()

    def forward_http_request(self, message: HttpRequestMessage) -> HttpResponseMessage:
        if not self.ensure_connected():
            return HttpResponseMessage(
                request_id=message.request_id,
                status_code=502,
                reason="Bad Gateway",
                headers={"Content-Type": "text/plain; charset=utf-8"},
                body_b64=b64_encode(b"Remote proxy is disconnected"),
                error="remote disconnected",
            )

        event = threading.Event()
        with self._lock:
            self._pending[message.request_id] = (event, None)

        try:
            self.sio.emit("proxy:http_request", message.to_json())
        except Exception:
            if not self.ensure_connected():
                return HttpResponseMessage(
                    request_id=message.request_id,
                    status_code=502,
                    reason="Bad Gateway",
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                    body_b64=b64_encode(b"Remote proxy emit failed"),
                    error="emit failed",
                )
            self.sio.emit("proxy:http_request", message.to_json())
        arrived = event.wait(timeout=REQUEST_TIMEOUT_SECONDS)

        with self._lock:
            _, response = self._pending.pop(message.request_id, (None, None))

        if not arrived or response is None:
            return HttpResponseMessage(
                request_id=message.request_id,
                status_code=504,
                reason="Gateway Timeout",
                headers={"Content-Type": "text/plain; charset=utf-8"},
                body_b64=b64_encode(b"Timeout waiting for remote proxy response"),
                error="timeout",
            )
        return response

    def tunnel_open(self, tunnel_id: str, target_host: str, target_port: int) -> Tuple[bool, Optional[str]]:
        if not self.ensure_connected():
            return False, "socket.io disconnected"
        event = threading.Event()
        with self._lock:
            self._tunnel_open_events[tunnel_id] = event
            self._tunnel_close_events[tunnel_id] = threading.Event()
            self._tunnel_close_reasons.pop(tunnel_id, None)
            self._tunnel_chunks[tunnel_id] = b""
        msg = TunnelOpenMessage(tunnel_id=tunnel_id, target_host=target_host, target_port=target_port)
        try:
            self.sio.emit("proxy:tunnel_open", msg.to_json())
        except Exception:
            if not self.ensure_connected():
                return False, "socket.io reconnect failed"
            self.sio.emit("proxy:tunnel_open", msg.to_json())
        ok = event.wait(timeout=10)
        reason = None
        if not ok:
            with self._lock:
                close_event = self._tunnel_close_events.get(tunnel_id)
                if close_event and close_event.is_set():
                    reason = self._tunnel_close_reasons.get(tunnel_id, "remote refused tunnel")
                else:
                    reason = "timeout waiting tunnel_opened"
        with self._lock:
            self._tunnel_open_events.pop(tunnel_id, None)
        return ok, reason

    def tunnel_send(self, tunnel_id: str, data: bytes):
        if not self.ensure_connected():
            return
        try:
            self.sio.emit("proxy:tunnel_data", TunnelDataMessage(tunnel_id=tunnel_id, data_b64=b64_encode(data)).to_json())
        except Exception:
            # Tunnel is best-effort; caller loop handles close/error paths.
            return

    def tunnel_take_chunks(self, tunnel_id: str) -> bytes:
        with self._lock:
            data = self._tunnel_chunks.get(tunnel_id, b"")
            self._tunnel_chunks[tunnel_id] = b""
        return data

    def tunnel_wait_close(self, tunnel_id: str, timeout_seconds: float = 0.01) -> bool:
        with self._lock:
            event = self._tunnel_close_events.get(tunnel_id)
        if not event:
            return True
        return event.wait(timeout=timeout_seconds)

    def tunnel_close(self, tunnel_id: str):
        if self.sio.connected:
            self.sio.emit("proxy:tunnel_close", TunnelCloseMessage(tunnel_id=tunnel_id, reason="local close").to_json())
        with self._lock:
            self._tunnel_open_events.pop(tunnel_id, None)
            self._tunnel_close_events.pop(tunnel_id, None)
            self._tunnel_close_reasons.pop(tunnel_id, None)
            self._tunnel_chunks.pop(tunnel_id, None)


class RotatingBridgeManager:
    def __init__(self, remote_urls: List[str], rotate_every_seconds: int):
        if not remote_urls:
            raise ValueError("remote_urls must not be empty")
        self.remote_urls = remote_urls
        self.rotate_every_seconds = max(1, rotate_every_seconds)
        self._idx = 0
        self._lock = threading.Lock()
        self._bridge = SocketIoBridgeClient(self.remote_urls[self._idx])
        self._bridge.connect()
        self._stop_event = threading.Event()
        self._rotation_thread = threading.Thread(target=self._rotation_loop, daemon=True)
        self._rotation_thread.start()

    def _rotation_loop(self):
        while not self._stop_event.wait(self.rotate_every_seconds):
            self.rotate_now()

    def rotate_now(self):
        with self._lock:
            old = self._bridge
            self._idx = (self._idx + 1) % len(self.remote_urls)
            next_url = self.remote_urls[self._idx]
            print(f"[local] rotating remote route to: {next_url}")
            new_bridge = SocketIoBridgeClient(next_url)
            new_bridge.connect()
            self._bridge = new_bridge
        old.disconnect()

    def get_bridge(self) -> SocketIoBridgeClient:
        with self._lock:
            return self._bridge

    def shutdown(self):
        self._stop_event.set()
        with self._lock:
            bridge = self._bridge
        bridge.disconnect()


class WindowsSystemProxyManager:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.enabled = False

    def _run(self, command: str):
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=True,
        )

    def enable(self):
        if not IS_WINDOWS:
            print("[local] system proxy automation is implemented for Windows only.")
            return
        cmd = (
            "Set-ItemProperty -Path "
            "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
            f"-Name ProxyServer -Value '{self.host}:{self.port}'; "
            "Set-ItemProperty -Path "
            "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
            "-Name ProxyEnable -Value 1"
        )
        self._run(cmd)
        self.enabled = True
        print(f"[local] system proxy enabled: {self.host}:{self.port}")

    def disable(self):
        if not IS_WINDOWS:
            return
        cmd = (
            "Set-ItemProperty -Path "
            "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
            "-Name ProxyEnable -Value 0"
        )
        try:
            self._run(cmd)
            if self.enabled:
                print("[local] system proxy disabled")
        except Exception as exc:
            print(f"[local] failed to disable system proxy: {exc}")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bridge_manager: RotatingBridgeManager = None  # type: ignore

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_remote_response(self, remote_response: HttpResponseMessage):
        status_code = remote_response.status_code or 502
        reason = remote_response.reason or "Bad Gateway"
        self.send_response(status_code, reason)
        for key, value in remote_response.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            if key.lower() == "content-length":
                continue
            self.send_header(key, value)
        body = remote_response.body
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle_forward(self):
        bridge = self.bridge_manager.get_bridge()
        request_id = new_request_id()
        headers = {k: v for k, v in self.headers.items()}
        target_host, target_port, target_path = parse_target(self.path, headers)

        message = HttpRequestMessage(
            request_id=request_id,
            method=self.command,
            target_host=target_host,
            target_port=target_port,
            path=target_path,
            headers=sanitize_headers(headers),
            body_b64=b64_encode(self._read_body()),
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        remote_response = bridge.forward_http_request(message)
        self._send_remote_response(remote_response)

    def do_GET(self):
        self._handle_forward()

    def do_POST(self):
        self._handle_forward()

    def do_PUT(self):
        self._handle_forward()

    def do_DELETE(self):
        self._handle_forward()

    def do_PATCH(self):
        self._handle_forward()

    def do_OPTIONS(self):
        self._handle_forward()

    def do_HEAD(self):
        self._handle_forward()

    def do_CONNECT(self):
        bridge = self.bridge_manager.get_bridge()
        if ":" not in self.path:
            self.send_error(400, "CONNECT target must be host:port")
            return
        target_host, target_port_s = self.path.rsplit(":", 1)
        target_port = int(target_port_s)
        tunnel_id = new_request_id()
        opened, open_error = bridge.tunnel_open(tunnel_id=tunnel_id, target_host=target_host, target_port=target_port)
        if not opened:
            print(f"[local] tunnel open failed for {target_host}:{target_port} -> {open_error}")
            try:
                self.send_error(502, f"Cannot open tunnel on remote proxy ({open_error})")
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # Browser already dropped socket while we were opening tunnel.
                pass
            return

        self.send_response(200, "Connection Established")
        self.end_headers()
        self.connection.settimeout(0.1)

        try:
            while True:
                try:
                    local_data = self.connection.recv(32768)
                    if local_data:
                        bridge.tunnel_send(tunnel_id, local_data)
                    elif local_data == b"":
                        break
                except Exception:
                    pass

                remote_data = bridge.tunnel_take_chunks(tunnel_id)
                if remote_data:
                    try:
                        self.connection.sendall(remote_data)
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                        break

                if bridge.tunnel_wait_close(tunnel_id, timeout_seconds=0.001):
                    break
        finally:
            bridge.tunnel_close(tunnel_id)
            # CONNECT request should terminate this handler session.
            # Without explicit close, leftover TLS bytes may be parsed as a
            # new HTTP request and produce "Bad request version" noise.
            self.close_connection = True

    def log_message(self, format: str, *args):
        print(f"[local] {self.client_address[0]} - {format % args}")


def run_local_proxy(local_host: str, local_port: int, remote_urls: List[str], rotate_every_seconds: int):
    bridge_manager = RotatingBridgeManager(remote_urls=remote_urls, rotate_every_seconds=rotate_every_seconds)
    LocalProxyHandler.bridge_manager = bridge_manager
    server = ThreadedTCPServer((local_host, local_port), LocalProxyHandler)

    # System proxy should point to loopback on this machine even when
    # server listens on 0.0.0.0 for LAN clients.
    system_proxy = WindowsSystemProxyManager("127.0.0.1", local_port)
    system_proxy.enable()

    def shutdown():
        print("[local] shutdown started")
        system_proxy.disable()
        bridge_manager.shutdown()
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    atexit.register(shutdown)

    try:
        print(f"[local] listening on {local_host}:{local_port}")
        print(f"[local] forwarding to remote socket.io nodes: {', '.join(remote_urls)}")
        print(f"[local] route rotation interval: {rotate_every_seconds} seconds")
        server.serve_forever()
    except KeyboardInterrupt:
        print("[local] interrupted by user")
    finally:
        shutdown()


def main():
    parser = argparse.ArgumentParser(description="Local HTTP proxy that forwards via Socket.IO")
    parser.add_argument(
        "--listen-host",
        default=LISTEN_HOST,
        help="Local bind host. Use 127.0.0.1 for local only, 0.0.0.0 for LAN clients.",
    )
    parser.add_argument("--local-port", type=int, default=6767, help="Local HTTP proxy port")
    parser.add_argument(
        "--remote-urls",
        default="http://127.0.0.1:9000",
        help="Comma-separated remote Socket.IO endpoint URLs",
    )
    parser.add_argument(
        "--rotate-every-seconds",
        type=int,
        default=3600,
        help="How often to rotate active remote route",
    )
    args = parser.parse_args()
    remote_urls = [x.strip() for x in args.remote_urls.split(",") if x.strip()]
    run_local_proxy(
        local_host=args.listen_host,
        local_port=args.local_port,
        remote_urls=remote_urls,
        rotate_every_seconds=args.rotate_every_seconds,
    )


if __name__ == "__main__":
    main()
