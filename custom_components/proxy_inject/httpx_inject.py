"""Route standard httpx transports for existing and new clients."""

import asyncio
from functools import wraps
from threading import RLock

import httpcore
import httpx
from yarl import URL


class HttpxInjector:
    """Keep a separate transport per original transport and route."""

    def __init__(self, policy):
        self.policy = policy
        self.active = False
        self.patches = []
        self.transports = {}
        self.lock = RLock()

    def select(self, client, url, original):
        transport = original(client, url)
        if not self.active or url.scheme not in ("http", "https"):
            return transport
        if type(transport) not in (httpx.HTTPTransport, httpx.AsyncHTTPTransport):
            return transport
        pool = transport._pool
        direct = type(pool) in (httpcore.ConnectionPool, httpcore.AsyncConnectionPool)
        proxied = type(pool) in (httpcore.HTTPProxy, httpcore.AsyncHTTPProxy)
        if not (direct or proxied) or pool._uds is not None:
            return transport
        bypass = self.policy.bypass.matches(URL(str(url)))
        if (bypass and direct) or (not bypass and proxied):
            return transport
        proxy = None if bypass else self.policy.config["proxy"]
        key = (transport, proxy)
        with self.lock:
            routes = self.transports.setdefault(client, {})
            if key not in routes:
                routes[key] = type(transport)(
                    proxy=proxy,
                    verify=pool._ssl_context,
                    trust_env=False,
                    http1=pool._http1,
                    http2=pool._http2,
                    limits=httpx.Limits(
                        max_connections=pool._max_connections,
                        max_keepalive_connections=pool._max_keepalive_connections,
                        keepalive_expiry=pool._keepalive_expiry,
                    ),
                    retries=pool._retries,
                    local_address=pool._local_address,
                    socket_options=pool._socket_options,
                )
            return routes[key]

    def pop(self, client):
        with self.lock:
            return list(self.transports.pop(client, {}).values())

    def close_client(self, client):
        for transport in self.pop(client):
            transport.close()

    async def aclose_client(self, client):
        for transport in self.pop(client):
            await transport.aclose()

    def patch(self, cls, name, wrapper):
        original = getattr(cls, name)
        wraps(original)(wrapper)
        wrapper._proxy_inject = True
        self.patches.append((cls, name, original, wrapper))
        setattr(cls, name, wrapper)

    def install(self):
        for cls in (httpx.Client, httpx.AsyncClient):
            if getattr(cls._transport_for_url, "_proxy_inject", False):
                raise RuntimeError("Proxy Inject httpx hook is already installed")
        for cls in (httpx.Client, httpx.AsyncClient):
            original = cls._transport_for_url

            def route(client, url, _original=original):
                return self.select(client, url, _original)

            self.patch(cls, "_transport_for_url", route)
        for name in ("close", "__exit__"):
            original = getattr(httpx.Client, name)

            def close(client, *args, _original=original):
                try:
                    return _original(client, *args)
                finally:
                    self.close_client(client)

            self.patch(httpx.Client, name, close)
        for name in ("aclose", "__aexit__"):
            original = getattr(httpx.AsyncClient, name)

            async def aclose(client, *args, _original=original):
                try:
                    return await _original(client, *args)
                finally:
                    await self.aclose_client(client)

            self.patch(httpx.AsyncClient, name, aclose)
        self.active = True

    async def uninstall(self):
        self.active = False
        for cls, name, original, wrapper in reversed(self.patches):
            if getattr(cls, name) is wrapper:
                setattr(cls, name, original)
        self.patches.clear()
        with self.lock:
            clients = list(self.transports)
        for client in clients:
            if isinstance(client, httpx.AsyncClient):
                await self.aclose_client(client)
            else:
                await asyncio.to_thread(self.close_client, client)
