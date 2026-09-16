"""Local servers; no requests to public services."""

import asyncio
import ssl
from datetime import datetime, timedelta, timezone

import homeassistant  # noqa: F401 -- initialize HA's schema implementation first
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture
async def network(tmp_path):
    seen = []
    handlers = set()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "secure.invalid")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("secure.invalid")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)

    async def direct(request):
        seen.append(("direct", str(request.rel_url), dict(request.headers)))
        if request.path == "/to-proxy":
            raise web.HTTPFound("http://remote.invalid/proxied")
        if request.path == "/ws":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                await ws.send_str(msg.data)
            return ws
        return web.Response(text="direct")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", direct)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    direct_port = site._server.sockets[0].getsockname()[1]
    tls_site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
    await tls_site.start()
    tls_port = tls_site._server.sockets[0].getsockname()[1]

    async def relay(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def proxy(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        upstream = None
        pumps = []
        try:
            data = await reader.readuntil(b"\r\n\r\n")
            lines = data.decode().split("\r\n")
            method, target, _ = lines[0].split()
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            seen.append(("proxy", target, headers))
            if method == "CONNECT" or target.endswith("/ws"):
                port = tls_port if method == "CONNECT" else direct_port
                upstream_reader, upstream = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    upstream.write(data.replace(target.encode(), b"/ws", 1))
                    await upstream.drain()
                pumps = [
                    asyncio.create_task(relay(reader, upstream)),
                    asyncio.create_task(relay(upstream_reader, writer)),
                ]
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            elif target.endswith("/to-direct"):
                writer.write(
                    f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{direct_port}/direct\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
                await writer.drain()
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nproxy"
                )
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            if upstream:
                upstream.close()
            writer.close()
            handlers.discard(task)

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    proxy_port = server.sockets[0].getsockname()[1]
    yield {
        "proxy": f"http://127.0.0.1:{proxy_port}",
        "direct": f"http://127.0.0.1:{direct_port}",
        "https": f"https://secure.invalid:{tls_port}/secure",
        "ssl": ssl.create_default_context(cafile=cert_path),
        "seen": seen,
    }
    server.close()
    await server.wait_closed()
    for task in list(handlers):
        task.cancel()
    await asyncio.gather(*list(handlers), return_exceptions=True)
    await runner.cleanup()
