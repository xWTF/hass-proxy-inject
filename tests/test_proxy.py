"""Real socket tests for global routing, redirects, credentials and cleanup."""

import asyncio

import aiohttp
import httpx
import pytest
from yarl import URL

from custom_components.proxy_inject import Runtime
from custom_components.proxy_inject.policy import SETTINGS_SCHEMA, NoProxy


@pytest.mark.parametrize(
    "rule,url,match",
    [
        ("example.com", "https://sub.example.com", True),
        (".example.com", "https://EXAMPLE.COM./", True),
        ("*.example.com", "https://notexample.com", False),
        ("example.com:443", "https://example.com", True),
        ("example.com:443", "http://example.com", False),
        ("192.168.0.0/16", "http://192.168.4.5", True),
        ("192.168.0.0/16", "http://device.example.com", False),
        ("fc00::/7", "http://[fd01::5]", True),
        ("[::1]:8123", "http://[::1]:8123", True),
        ("[::1]:8123", "http://[::1]:80", False),
        ("<local>", "http://supervisor", True),
        ("<local>", "http://1.2.3.4", False),
        ("*", "https://any.invalid", True),
    ],
)
def test_no_proxy(rule, url, match):
    assert NoProxy([rule]).matches(URL(url)) is match


def settings(network, clients):
    return SETTINGS_SCHEMA({"proxy": network["proxy"], "clients": clients})


async def test_aiohttp_old_new_redirect_auth_ws_tls(network):
    original = aiohttp.ClientSession._request
    old = aiohttp.ClientSession()
    config = settings(network, ["aiohttp"])
    config["proxy"] = network["proxy"].replace("http://", "http://user:secret@")
    runtime = Runtime(config)
    await runtime.install()
    try:
        async with old, aiohttp.ClientSession() as new:
            for client in (old, new):
                async with client.get("http://remote.invalid/proxied") as response:
                    assert await response.text() == "proxy"
                async with client.get(
                    network["direct"] + "/direct", proxy=network["proxy"]
                ) as response:
                    assert await response.text() == "direct"
            async with old.get("http://remote.invalid/to-direct") as response:
                assert await response.text() == "direct"
            async with old.get(network["direct"] + "/to-proxy") as response:
                assert await response.text() == "proxy"
            async with old.get(network["https"], ssl=network["ssl"]) as response:
                assert await response.text() == "direct"
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await old.get(network["https"])
            async with old.ws_connect("ws://remote.invalid/ws") as ws:
                await ws.send_str("echo")
                assert (await ws.receive()).data == "echo"
            assert any(
                "Proxy-Authorization" in headers
                for kind, _, headers in network["seen"]
                if kind == "proxy"
            )
            assert all(
                "Proxy-Authorization" not in headers
                for kind, path, headers in network["seen"]
                if kind == "direct" and path != "/ws"
            )
    finally:
        await runtime.close()
        await old.close()
    assert aiohttp.ClientSession._request is original


async def test_aiohttp_middlewares_and_explicit_proxy(network):
    called = []

    async def session_middleware(req, handler):
        called.append("session")
        return await handler(req)

    async def per_request(req, handler):
        called.append("request")
        return await handler(req)

    runtime = Runtime(
        {"proxy": "http://127.0.0.1:1", "clients": ["aiohttp"], "no_proxy": []}
    )
    await runtime.install()
    try:
        async with aiohttp.ClientSession(middlewares=(session_middleware,)) as client:
            async with client.get(
                "http://remote.invalid", proxy=network["proxy"]
            ) as resp:
                assert await resp.text() == "proxy"
            async with client.get(
                "http://remote.invalid",
                proxy=network["proxy"],
                middlewares=(per_request,),
            ) as resp:
                assert await resp.text() == "proxy"
        assert called == ["session", "request"]
    finally:
        await runtime.close()


async def test_httpx_async_old_new_redirect_tls_close(network):
    original = httpx.AsyncClient._transport_for_url
    old = httpx.AsyncClient(trust_env=False, verify=network["ssl"])
    runtime = Runtime(settings(network, ["httpx"]))
    await runtime.install()
    hook = runtime.hooks[0]
    try:
        async with old, httpx.AsyncClient(trust_env=False) as new:
            for client in (old, new):
                assert (await client.get("http://remote.invalid")).text == "proxy"
                assert (await client.get(network["direct"])).text == "direct"
            assert (
                await old.get("http://remote.invalid/to-direct", follow_redirects=True)
            ).text == "direct"
            assert (
                await old.get(network["direct"] + "/to-proxy", follow_redirects=True)
            ).text == "proxy"
            assert (await old.get(network["https"])).text == "direct"
            with pytest.raises(httpx.ConnectError):
                await new.get(network["https"])
            assert hook.transports
        assert not hook.transports
    finally:
        await old.aclose()
        await runtime.close()
    assert httpx.AsyncClient._transport_for_url is original


async def test_httpx_sync_and_explicit_proxy_bypass(network):
    runtime = Runtime(settings(network, ["httpx"]))
    await runtime.install()
    try:

        def run():
            with httpx.Client(trust_env=False, proxy=network["proxy"]) as client:
                assert client.get("http://remote.invalid").text == "proxy"
                assert client.get(network["direct"]).text == "direct"
            with httpx.Client(trust_env=False) as client:
                assert client.get("http://remote.invalid").text == "proxy"

        await asyncio.to_thread(run)
        assert not runtime.hooks[0].transports
    finally:
        await runtime.close()


async def test_httpx_custom_transport_and_pool_settings(network):
    runtime = Runtime(settings(network, ["httpx"]))
    await runtime.install()
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="mock"))
        ) as client:
            assert (await client.get("http://remote.invalid")).text == "mock"
        transport = httpx.AsyncHTTPTransport(
            verify=network["ssl"],
            limits=httpx.Limits(
                max_connections=7, max_keepalive_connections=3, keepalive_expiry=8
            ),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get("http://remote.invalid")).text == "proxy"
            cloned = client._transport_for_url(httpx.URL("http://remote.invalid"))
            assert cloned._pool._ssl_context is transport._pool._ssl_context
            assert cloned._pool._max_connections == 7
            assert cloned._pool._max_keepalive_connections == 3
            assert cloned._pool._keepalive_expiry == 8
    finally:
        await runtime.close()


@pytest.mark.parametrize("clients", [["aiohttp"], ["httpx"], ["aiohttp", "httpx"]])
async def test_selection_and_uninstall(network, clients):
    aio_original = aiohttp.ClientSession._request
    httpx_original = httpx.AsyncClient._transport_for_url
    runtime = Runtime(settings(network, clients))
    await runtime.install()
    assert (aiohttp.ClientSession._request is not aio_original) == (
        "aiohttp" in clients
    )
    assert (httpx.AsyncClient._transport_for_url is not httpx_original) == (
        "httpx" in clients
    )
    await runtime.close()
    assert aiohttp.ClientSession._request is aio_original
    assert httpx.AsyncClient._transport_for_url is httpx_original


@pytest.mark.parametrize("library", ["aiohttp", "httpx"])
async def test_proxy_failure_does_not_fall_back(network, library):
    reserve = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = reserve.sockets[0].getsockname()[1]
    reserve.close()
    await reserve.wait_closed()
    runtime = Runtime(
        {
            "proxy": f"http://127.0.0.1:{port}",
            "clients": [library],
            "no_proxy": [],
        }
    )
    await runtime.install()
    try:
        if library == "aiohttp":
            async with aiohttp.ClientSession() as client:
                with pytest.raises(aiohttp.ClientProxyConnectionError):
                    await client.get(network["direct"])
        else:
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get(network["direct"])
        assert not network["seen"]
    finally:
        await runtime.close()
