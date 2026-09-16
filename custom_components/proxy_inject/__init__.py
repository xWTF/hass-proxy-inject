"""Proxy Inject: global outbound proxy settings for HA network clients."""

import logging

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.exceptions import ConfigEntryError

from .aiohttp_inject import AiohttpInjector
from .const import DOMAIN
from .httpx_inject import HttpxInjector
from .policy import SETTINGS_SCHEMA, Policy

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema(
    {vol.Optional(DOMAIN): SETTINGS_SCHEMA}, extra=vol.ALLOW_EXTRA
)


class Runtime:
    """Own installed hooks and their cleanup."""

    def __init__(self, settings):
        policy = Policy(settings)
        self.hooks = []
        if "aiohttp" in policy.config["clients"]:
            self.hooks.append(AiohttpInjector(policy))
        if "httpx" in policy.config["clients"]:
            self.hooks.append(HttpxInjector(policy))

    async def install(self):
        try:
            for hook in self.hooks:
                hook.install()
        except Exception:
            await self.close()
            raise

    async def close(self, _event=None):
        for hook in reversed(self.hooks):
            if isinstance(hook, HttpxInjector):
                await hook.uninstall()
            else:
                hook.uninstall()


async def _start(hass, settings):
    runtime = Runtime(settings)
    await runtime.install()
    runtime.unsubscribe = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_CLOSE, runtime.close
    )
    hass.data[DOMAIN] = runtime
    _LOGGER.info("Proxy Inject enabled for %s", ", ".join(settings["clients"]))


async def async_setup(hass, config) -> bool:
    """Support optional YAML configuration as an alternative to the UI."""
    if DOMAIN in config:
        if hass.config_entries.async_entries(DOMAIN):
            _LOGGER.error("Remove proxy_inject YAML before using its UI entry")
            return False
        await _start(hass, config[DOMAIN])
    return True


async def async_setup_entry(hass, entry) -> bool:
    """Load saved settings. Option changes take effect after HA restart."""
    if DOMAIN in hass.data:
        raise ConfigEntryError(
            "Proxy Inject is already active; remove duplicate YAML configuration"
        )
    await _start(hass, SETTINGS_SCHEMA(entry.options or entry.data))
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Restore original client methods when the integration is disabled."""
    if runtime := hass.data.pop(DOMAIN, None):
        runtime.unsubscribe()
        await runtime.close()
    return True
