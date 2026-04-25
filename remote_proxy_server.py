import argparse
import socket
import threading
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
)


sio = socketio.Server(async_mode="eventlet", cors_allowed_origins="*")
app = socketio.WSGIApp(sio)
tunnel_sockets = {}
tunnel_lock = threading.Lock()


HOP_BY_HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
}


def sanitize_outgoing_headers(headers: dict) -> dict:
    clean = {}
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        clean[key] = value
    return clean


def normalize_url(req: HttpRequestMessage) -> str:
    if req.path.startswith("http://") or req.path.startswith("https://"):
        return req.path
    scheme = "https" if req.target_port == 443 else "http"
    host = req.target_host
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{req.target_port}{req.path}"


@sio.event
def connect(sid, environ):
    print(f"[remote] local proxy connected: {sid}")


@sio.event
def disconnect(sid):
    print(f"[remote] local proxy disconnected: {sid}")
    to_close = []
    with tunnel_lock:
        for tunnel_id, (owner_sid, sock) in list(tunnel_sockets.items()):
            if owner_sid == sid:
                to_close.append((tunnel_id, sock))
                tunnel_sockets.pop(tunnel_id, None)
    for _, sock in to_close:
        try:
            sock.close()
        except Exception:
            pass


@sio.on("proxy:http_request")
def on_http_request(sid, payload):
    req = HttpRequestMessage.from_json(payload)
    print(f"[remote] request {req.request_id} -> {req.method} {req.target_host}:{req.target_port}{req.path}")

    try:
        method = req.method.upper()
        url = normalize_url(req)
        parsed = urlsplit(url)
        outgoing_headers = sanitize_outgoing_headers(req.headers)
        outgoing_headers["Host"] = parsed.netloc

        response = requests.request(
            method=method,
            url=url,
            headers=outgoing_headers,
            data=req.body,
            timeout=req.timeout_seconds,
            allow_redirects=False,
        )

        message = HttpResponseMessage(
            request_id=req.request_id,
            status_code=response.status_code,
            reason=response.reason or "",
            headers=dict(response.headers),
            body_b64=b64_encode(response.content),
            error=None,
        )
    except Exception as exc:
        message = HttpResponseMessage(
            request_id=req.request_id,
            status_code=502,
            reason="Bad Gateway",
            headers={"Content-Type": "text/plain; charset=utf-8"},
            body_b64=b64_encode(str(exc).encode("utf-8")),
            error=str(exc),
        )

    sio.emit("proxy:http_response", message.to_json(), to=sid)


@sio.on("proxy:tunnel_open")
def on_tunnel_open(sid, payload):
    msg = TunnelOpenMessage.from_json(payload)
    print(f"[remote] tunnel open {msg.tunnel_id} -> {msg.target_host}:{msg.target_port}")
    try:
        upstream = socket.create_connection((msg.target_host, msg.target_port), timeout=20)
        upstream.settimeout(1.0)
        with tunnel_lock:
            tunnel_sockets[msg.tunnel_id] = (sid, upstream)
        sio.emit("proxy:tunnel_opened", msg.to_json(), to=sid)

        def pump_remote_to_local():
            try:
                while True:
                    try:
                        chunk = upstream.recv(32768)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    data_msg = TunnelDataMessage(tunnel_id=msg.tunnel_id, data_b64=b64_encode(chunk))
                    sio.emit("proxy:tunnel_data", data_msg.to_json(), to=sid)
            except Exception:
                pass
            finally:
                close_msg = TunnelCloseMessage(tunnel_id=msg.tunnel_id, reason="remote closed")
                sio.emit("proxy:tunnel_close", close_msg.to_json(), to=sid)
                with tunnel_lock:
                    _, sock = tunnel_sockets.pop(msg.tunnel_id, (None, None))
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

        threading.Thread(target=pump_remote_to_local, daemon=True).start()
    except Exception as exc:
        close_msg = TunnelCloseMessage(tunnel_id=msg.tunnel_id, reason=str(exc))
        sio.emit("proxy:tunnel_close", close_msg.to_json(), to=sid)


@sio.on("proxy:tunnel_data")
def on_tunnel_data(sid, payload):
    msg = TunnelDataMessage.from_json(payload)
    with tunnel_lock:
        state = tunnel_sockets.get(msg.tunnel_id)
    if not state:
        return
    _, upstream = state
    try:
        upstream.sendall(msg.data)
    except Exception:
        close_msg = TunnelCloseMessage(tunnel_id=msg.tunnel_id, reason="send failed")
        sio.emit("proxy:tunnel_close", close_msg.to_json(), to=sid)


@sio.on("proxy:tunnel_close")
def on_tunnel_close(sid, payload):
    msg = TunnelCloseMessage.from_json(payload)
    with tunnel_lock:
        _, upstream = tunnel_sockets.pop(msg.tunnel_id, (None, None))
    if upstream:
        try:
            upstream.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Remote proxy server over Socket.IO")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=9000, help="Bind port")
    args = parser.parse_args()

    import eventlet
    import eventlet.wsgi

    print(f"[remote] listening on {args.host}:{args.port}")
    eventlet.wsgi.server(eventlet.listen((args.host, args.port)), app)


if __name__ == "__main__":
    main()
