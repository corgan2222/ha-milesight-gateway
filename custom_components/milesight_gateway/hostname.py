"""
The gateway's hostname, where the gateway itself does not report one.

Firmware 60.0.0.50 and later name it in the web interface's status. A UG63
does not, but it sends its hostname with every DHCP request, and a router
integration (pfSense, FRITZ!Box, UniFi ...) reports that on its device tracker
as ``host_name``, next to the MAC and the IP address.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import format_mac

from .const import CONF_GATEWAY_URL, CONF_HOSTNAME, CONF_MAC, entry_title, gateway_name

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import State


# A DHCP hostname label. Some trackers put the vendor there instead
# ("Xiamen Milesight IoT Co., Ltd."), which is no name of this gateway.
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def short_hostname(name: str | None) -> str | None:
    """Drop the domain a DHCP server appends: "ms_gw_mini.localdomain"."""
    if not isinstance(name, str):
        return None
    short = name.strip().split(".")[0]
    return short if _LABEL.match(short) else None


def _matches(state: State, mac: str | None, ip: str | None) -> bool:
    attrs = state.attributes
    tracked = attrs.get("mac")
    if mac and isinstance(tracked, str) and format_mac(tracked) == format_mac(mac):
        return True
    return bool(ip) and ip in (attrs.get("ip"), attrs.get("last_known_ip"))


@callback
def learn_hostname(
    hass: HomeAssistant,
    entry: ConfigEntry,
    gateway_device_id: str,
    reported: str | None = None,
) -> None:
    """
    Name an entry set up before its hostname was known after it.

    The gateway's own hostname first, else the router's. The device name
    follows unless the user renamed the device, and the title unless it is no
    longer the one the setup gave. Entity IDs stay.
    """
    stored = entry.data.get(CONF_HOSTNAME)
    without = {key: value for key, value in entry.data.items() if key != CONF_HOSTNAME}
    url = urlsplit(entry.data.get(CONF_GATEWAY_URL, ""))
    # Titles the setup gave, this version's and older ones (the 1.5.0 beta
    # named an entry without its address), so a chosen title stays.
    given = {
        entry_title(entry.data),
        entry_title(without),
        url.geturl(),
        gateway_name(entry.data),
    }
    if stored and short_hostname(stored) == stored:
        if entry.title in given and entry.title != entry_title(entry.data):
            hass.config_entries.async_update_entry(entry, title=entry_title(entry.data))
        return
    # A stored name that is no hostname (1.5.0 beta took a tracker's vendor
    # text) is replaced, or dropped when nothing better is known.
    hostname = short_hostname(reported) or tracker_hostname(
        hass, entry.data.get(CONF_MAC), url.hostname
    )
    if not hostname and not stored:
        return
    data = {**without, CONF_HOSTNAME: hostname} if hostname else without
    title = entry_title(data) if entry.title in given else entry.title
    hass.config_entries.async_update_entry(entry, data=data, title=title)
    registry = dr.async_get(hass)
    if device := registry.async_get(gateway_device_id):
        registry.async_update_device(
            device.id, name=gateway_name(data) or "Milesight Gateway"
        )


@callback
def tracker_hostname(
    hass: HomeAssistant, mac: str | None, ip: str | None
) -> str | None:
    """Return the hostname a router's device tracker knows for the gateway."""
    if not mac and not ip:
        return None
    for state in hass.states.async_all("device_tracker"):
        if _matches(state, mac, ip) and (
            name := short_hostname(
                state.attributes.get("host_name")
                or state.attributes.get("last_known_hostname")
            )
        ):
            return name
    return None
