"""Validate HA's real config/options flow and hook lifecycle."""

from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import httpx
import pytest
import voluptuous as vol
from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.proxy_inject import async_setup_entry, async_unload_entry
from custom_components.proxy_inject.const import DOMAIN
from custom_components.proxy_inject.policy import SETTINGS_SCHEMA


@pytest.fixture
async def hass(tmp_path):
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).parents[1] / "custom_components"
    )
    instance = HomeAssistant(str(tmp_path))
    instance.config.skip_pip = True
    instance.config.language = "en"
    frame.async_setup(instance)
    loader.async_setup(instance)
    instance.config_entries = config_entries.ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()
    yield instance
    if DOMAIN in instance.data:
        await async_unload_entry(instance, None)
    await instance.async_stop(force=True)


@pytest.mark.parametrize(
    "value",
    ["socks5://localhost:1080", "http://", "http://localhost:bad", "http://host/path"],
)
def test_invalid_proxy(value):
    with pytest.raises(vol.Invalid):
        SETTINGS_SCHEMA({"proxy": value})


def test_rule_validation():
    with pytest.raises(vol.Invalid):
        SETTINGS_SCHEMA(
            {"proxy": "http://localhost:7890", "no_proxy": "192.168.0.0/99"}
        )
    with pytest.raises(vol.Invalid):
        SETTINGS_SCHEMA({"proxy": "http://localhost:7890", "clients": []})
    assert SETTINGS_SCHEMA(
        {"proxy": "http://localhost:7890", "no_proxy": ".local\n10.0.0.0/8,::1"}
    )["no_proxy"] == [".local", "10.0.0.0/8", "::1"]


async def test_ui_options_and_lifecycle(hass, monkeypatch):
    # Exercise real HA flow loading/storage; start the hooks explicitly below.
    monkeypatch.setattr(
        hass.config_entries, "async_setup", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    initial_schema = result["data_schema"]
    initial_defaults = initial_schema(
        {"proxy": "http://localhost:7890", "clients": ["aiohttp"]}
    )
    assert "192.168.0.0/16" in initial_defaults["no_proxy"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"proxy": "socks5://localhost:1080", "clients": ["aiohttp"], "no_proxy": ""},
    )
    assert result["errors"] == {"proxy": "invalid_config"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"proxy": "http://localhost:7890", "clients": ["aiohttp"], "no_proxy": ""},
    )
    assert result["type"] == "create_entry"
    entry = result["result"]
    assert entry.data["no_proxy"] == []
    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert duplicate["reason"] == "single_instance_allowed"
    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] == "form"
    options = await hass.config_entries.options.async_configure(
        options["flow_id"],
        {
            "proxy": "http://localhost:7891",
            "clients": ["httpx"],
            "no_proxy": ".local\n10.0.0.0/8",
        },
    )
    assert options["type"] == "create_entry"
    assert entry.options["clients"] == ["httpx"]
    aio_original, httpx_original = (
        aiohttp.ClientSession._request,
        httpx.AsyncClient._transport_for_url,
    )
    assert await async_setup_entry(hass, entry)
    assert aiohttp.ClientSession._request is aio_original
    assert httpx.AsyncClient._transport_for_url is not httpx_original
    assert await async_unload_entry(hass, entry)
    assert httpx.AsyncClient._transport_for_url is httpx_original


async def test_ui_rejects_active_yaml(hass):
    hass.data[DOMAIN] = object()
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["reason"] == "yaml_active"
    finally:
        hass.data.pop(DOMAIN)


async def test_visible_in_add_integration_catalog(hass):
    """The picker catalog must list it, not just allow a direct flow call."""
    descriptions = await loader.async_get_integration_descriptions(hass)
    metadata = descriptions["custom"]["integration"][DOMAIN]
    assert metadata["name"] == "Proxy Inject"
    assert metadata["config_flow"] is True
    assert metadata["integration_type"] == "service"
