import argparse
import asyncio
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
import socketio

from shared_protocol import HttpRequestMessage, HttpResponseMessage, TunnelCloseMessage, TunnelDataMessage, TunnelOpenMessage, b64_encode


sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
app = web.Application()
sio.attach(app)
tunnel_streams = {}
tunnel_lock = asyncio.Lock()


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
async def connect(sid, environ):
    print(f"[remote] local proxy connected: {sid}")


@sio.event
async def disconnect(sid):
    print(f"[remote] local proxy disconnected: {sid}")
    to_close = []
    async with tunnel_lock:
        for tunnel_id, state in list(tunnel_streams.items()):
            owner_sid = state["sid"]
            if owner_sid == sid:
                to_close.append((tunnel_id, state))
                tunnel_streams.pop(tunnel_id, None)
    for _, state in to_close:
        writer = state["writer"]
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


@sio.on("proxy:http_request")
async def on_http_request(sid, payload):
    req = HttpRequestMessage.from_json(payload)
    print(f"[remote] request {req.request_id} -> {req.method} {req.target_host}:{req.target_port}{req.path}")

    try:
        method = req.method.upper()
        url = normalize_url(req)
        parsed = urlsplit(url)
        outgoing_headers = sanitize_outgoing_headers(req.headers)
        outgoing_headers["Host"] = parsed.netloc

        timeout = aiohttp.ClientTimeout(total=req.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method=method,
                url=url,
                headers=outgoing_headers,
                data=req.body,
                allow_redirects=False,
            ) as response:
                body = await response.read()
                message = HttpResponseMessage(
                    request_id=req.request_id,
                    status_code=response.status,
                    reason=response.reason or "",
                    headers=dict(response.headers),
                    body_b64=b64_encode(body),
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

    await sio.emit("proxy:http_response", message.to_json(), to=sid)


@sio.on("proxy:tunnel_open")
async def on_tunnel_open(sid, payload):
    msg = TunnelOpenMessage.from_json(payload)
    print(f"[remote] tunnel open {msg.tunnel_id} -> {msg.target_host}:{msg.target_port}")
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(msg.target_host, msg.target_port), timeout=20)
        async with tunnel_lock:
            tunnel_streams[msg.tunnel_id] = {"sid": sid, "reader": reader, "writer": writer}
        await sio.emit("proxy:tunnel_opened", msg.to_json(), to=sid)

        async def pump_remote_to_local():
            try:
                while True:
                    chunk = await reader.read(32768)
                    if not chunk:
                        break
                    data_msg = TunnelDataMessage(tunnel_id=msg.tunnel_id, data_b64=b64_encode(chunk))
                    await sio.emit("proxy:tunnel_data", data_msg.to_json(), to=sid)
            except Exception:
                pass
            finally:
                close_msg = TunnelCloseMessage(tunnel_id=msg.tunnel_id, reason="remote closed")
                await sio.emit("proxy:tunnel_close", close_msg.to_json(), to=sid)
                async with tunnel_lock:
                    state = tunnel_streams.pop(msg.tunnel_id, None)
                if state:
                    out_writer = state["writer"]
                    out_writer.close()
                    try:
                        await out_writer.wait_closed()
                    except Exception:
                        pass

        asyncio.create_task(pump_remote_to_local())
    except Exception as exc:
        close_msg = TunnelCloseMessage(tunnel_id=msg.tunnel_id, reason=str(exc))
        await sio.emit("proxy:tunnel_close", close_msg.to_json(), to=sid)


@sio.on("proxy:tunnel_data")
async def on_tunnel_data(sid, payload):
    msg = TunnelDataMessage.from_json(payload)
    async with tunnel_lock:
        state = tunnel_streams.get(msg.tunnel_id)
    if not state:
        return
    upstream = state["writer"]
    try:
        upstream.write(msg.data)
        await upstream.drain()
    except Exception:
        close_msg = TunnelCloseMessage(tunnel_id=msg.tunnel_id, reason="send failed")
        await sio.emit("proxy:tunnel_close", close_msg.to_json(), to=sid)


@sio.on("proxy:tunnel_close")
async def on_tunnel_close(sid, payload):
    msg = TunnelCloseMessage.from_json(payload)
    async with tunnel_lock:
        state = tunnel_streams.pop(msg.tunnel_id, None)
    if state:
        upstream = state["writer"]
        upstream.close()
        try:
            await upstream.wait_closed()
        except Exception:
            pass


async def health(request):
    return web.json_response({"status": "ok"})


def main():
    parser = argparse.ArgumentParser(description="Remote proxy server over Socket.IO")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=9000, help="Bind port")
    args = parser.parse_args()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    print(f"[remote] listening on {args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
