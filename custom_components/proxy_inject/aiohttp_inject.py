"""Process-wide aiohttp routing middleware."""

import inspect
from functools import wraps

import aiohttp
from aiohttp.helpers import strip_auth_from_url
from homeassistant.core import callback
from yarl import URL


class AiohttpInjector:
    """Add a routing middleware to both existing and future aiohttp sessions."""

    def __init__(self, policy):
        self.proxy, self.auth = strip_auth_from_url(URL(policy.config["proxy"]))
        self.policy = policy
        self.active = False
        self.original = None
        self.wrapper = None

    async def route(self, request, handler):
        if self.active and request.url.scheme in ("http", "https", "ws", "wss"):
            if self.policy.bypass.matches(request.url):
                request.update_proxy(None, None, None)
                # A previous HTTP proxy hop can have added this to shared headers.
                request.headers.popall(aiohttp.hdrs.PROXY_AUTHORIZATION, None)
            elif request.proxy is None:
                request.update_proxy(self.proxy, self.auth, None)
        return await handler(request)

    def install(self):
        original = aiohttp.ClientSession._request
        if "middlewares" not in inspect.signature(original).parameters:
            raise RuntimeError(
                "Proxy Inject requires aiohttp client middleware support"
            )
        if getattr(original, "_proxy_inject", False):
            raise RuntimeError("Proxy Inject is already installed in this process")

        @wraps(original)
        async def request(session, method, url, **kwargs):
            # Unix sockets/named pipes have no TCP forward-proxy semantics.
            if self.active and isinstance(session.connector, aiohttp.TCPConnector):
                middlewares = kwargs.get("middlewares")
                if middlewares is None:
                    middlewares = session._middlewares
                kwargs["middlewares"] = (*middlewares, self.route)
            return await original(session, method, url, **kwargs)

        request._proxy_inject = True
        self.original, self.wrapper = original, request
        self.active = True
        aiohttp.ClientSession._request = request

    @callback
    def uninstall(self, _event=None):
        self.active = False
        if aiohttp.ClientSession._request is self.wrapper:
            aiohttp.ClientSession._request = self.original
