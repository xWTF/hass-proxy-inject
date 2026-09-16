"""Proxy configuration validation and host bypass rules."""

from ipaddress import ip_address, ip_network

import voluptuous as vol
from yarl import URL

from .const import DEFAULT_NO_PROXY


def proxy_url(value: str) -> str:
    """Validate an HTTP forward proxy, including optional URL credentials."""
    if not isinstance(value, str):
        raise vol.Invalid("proxy must be an HTTP proxy URL")
    try:
        url = URL(value)
        if (
            url.scheme == "http"
            and url.host
            and url.port
            and url.path == "/"
            and not url.query
            and not url.fragment
        ):
            return value
    except ValueError:
        pass
    # Do not echo a URL that may contain a password.
    raise vol.Invalid("Use http://[user:password@]host:port without a path")


class NoProxy:
    """Match URL hosts without doing DNS or blocking the event loop."""

    def __init__(self, entries):
        self.rules = []
        for entry in entries:
            if entry in ("*", "<local>"):
                self.rules.append((entry, None))
                continue
            try:
                if "/" in entry:
                    self.rules.append((ip_network(entry, strict=False), None))
                    continue
                # Bare IPv6 is an address; brackets are required to add a port.
                self.rules.append((ip_address(entry), None))
                continue
            except ValueError:
                if "/" in entry:
                    raise vol.Invalid("Invalid no_proxy CIDR") from None
            try:
                host_entry = entry.removeprefix("*.").lstrip(".")
                parsed = URL("http://" + host_entry)
                if (
                    not parsed.host
                    or parsed.user is not None
                    or parsed.path != "/"
                    or parsed.query
                    or parsed.fragment
                    or "*" in parsed.host
                    or any(c.isspace() for c in parsed.host)
                ):
                    raise ValueError
                host = parsed.raw_host.lower().rstrip(".")
                port = parsed.explicit_port
                if port is not None and not 0 < port <= 65535:
                    raise ValueError
                try:
                    host = ip_address(host)
                except ValueError:
                    pass
                self.rules.append((host, port))
            except ValueError:
                raise vol.Invalid("Invalid no_proxy host or port") from None

    def matches(self, url: URL) -> bool:
        host = (url.raw_host or "").lower().rstrip(".")
        try:
            address = ip_address(host)
        except ValueError:
            address = None
        for rule, port in self.rules:
            if port is not None and port != url.port:
                continue
            if rule == "*":
                return True
            if rule == "<local>":
                if address is None and host and "." not in host:
                    return True
            elif isinstance(rule, str):
                if host == rule or host.endswith("." + rule):
                    return True
            elif hasattr(rule, "prefixlen"):
                if address is not None and address in rule:
                    return True
            elif address == rule:
                return True
        return False


def no_proxy_entries(value):
    """Accept a YAML list or a comma-separated string."""
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise vol.Invalid("no_proxy must be a string or a list of strings")
    entries = [item.strip() for item in value if item.strip()]
    NoProxy(entries)
    return entries


SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Required("proxy"): proxy_url,
        vol.Optional("clients", default=lambda: ["aiohttp"]): vol.All(
            [vol.In(("aiohttp", "httpx"))], vol.Length(min=1)
        ),
        vol.Optional(
            "no_proxy", default=lambda: list(DEFAULT_NO_PROXY)
        ): no_proxy_entries,
    }
)


class Policy:
    """Shared proxy and host bypass settings."""

    def __init__(self, config):
        self.config = SETTINGS_SCHEMA(config)
        self.bypass = NoProxy(self.config["no_proxy"])
