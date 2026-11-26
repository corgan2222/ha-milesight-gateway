"""
Create devices on the gateway from a list: devEUI, name, optionally AppKey.

The network server's API takes them one by one, into one application, with
one device profile. The payload codec is either one for all, or the built-in
codec of each device's model ("auto"), found by its name in the gateway's
codec list. The API is reached by REST, or by MQTT for a gateway Home
Assistant cannot reach.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from milesight_gateway_api import decoder
from milesight_gateway_api.lorawan import identify

from .api import APIAuthError, APIConnectionError
from .const import gateway_only
from .nsapi import NsApiError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# Milesight's factory AppKey, which new devices ship with.
DEFAULT_APP_KEY = "5572404c696e6b4c6f52613230313823"
AUTO = "auto"
PAUSE = 0.2
_EUI = re.compile(r"^[0-9a-fA-F]{16}$")
_KEY = re.compile(r"^[0-9a-fA-F]{32}$")

type Call = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]


def network_server(hass: HomeAssistant, entry: ConfigEntry) -> Call:
    """Return how this entry calls the network server's API."""
    if not gateway_only(entry.data):
        api = entry.runtime_data.coordinator.api

        async def rest(method: str, path: str, body: dict | None) -> dict:
            return await api.async_ns_call(
                async_get_clientsession(hass), method, path, body
            )

        return rest
    mqtt = entry.runtime_data.mqtt
    if mqtt is None or mqtt.ns_api is None:
        msg = "This gateway's API is reachable neither by REST nor by MQTT"
        raise HomeAssistantError(msg)

    async def by_mqtt(method: str, path: str, body: dict | None) -> dict:
        return await mqtt.ns_api.async_request(method, path, body)

    return by_mqtt


def _named(items: Any, *keys: str) -> list[dict[str, str]]:
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        inner = item.get("profile") if isinstance(item.get("profile"), dict) else item
        ident = next(
            (inner.get(k) for k in keys if inner.get(k) not in (None, "")), None
        )
        if ident is not None:
            name = inner.get("name") or inner.get("profileName") or ident
            out.append({"id": str(ident), "name": str(name)})
    return out


def _profiles(items: Any) -> list[dict[str, str]]:
    """
    Name each device profile with what it sets: class, join mode, region.

    The one in use comes first, so it is the one picked unless changed.
    """
    out = []
    for item in items if isinstance(items, list) else []:
        profile = item.get("profile") if isinstance(item, dict) else None
        if not isinstance(profile, dict) or not profile.get("profileID"):
            continue
        details = ", ".join(
            part
            for part in (
                "class C" if profile.get("supportsClassC") else "class A",
                "OTAA" if profile.get("supportsJoin") else "ABP",
                profile.get("rfRegion"),
            )
            if part
        )
        name = item.get("name") or profile["profileID"]
        out.append(
            {
                "id": str(profile["profileID"]),
                "name": f"{name} ({details})",
                "using": bool(item.get("using")),
            }
        )
    out.sort(key=lambda p: not p["using"])
    return [{"id": p["id"], "name": p["name"]} for p in out]


async def async_choices(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the applications, device profiles and payload codecs to pick from."""
    call = network_server(hass, entry)
    try:
        apps = await call(
            "GET", "/api/urapplications?limit=100&offset=0&organizationID=0", None
        )
        profiles = await call(
            "GET", "/api/urprofiles?organizationID=0&offset=0&limit=100", None
        )
        codecs = await call("GET", "/api/payloadcodecs-short", None)
    except (APIAuthError, APIConnectionError, NsApiError) as err:
        raise HomeAssistantError(str(err)) from err
    return {
        "applications": _named(apps.get("result"), "id", "applicationID"),
        "profiles": _profiles(profiles.get("result")),
        "codecs": _named(codecs.get("result"), "id"),
    }


# A Milesight devEUI whose prefix belongs to no model.
NO_MODEL_EUI = "24e124" + "f" * 10


def _auto_codec(dev_eui: str, codecs: list[dict[str, str]]) -> str:
    """Return the built-in codec of the device's model, or none ("0")."""
    model_id = decoder.model_for_eui(dev_eui.lower())
    if not model_id:
        return "0"
    for codec in codecs:
        # A Milesight devEUI no model has: identify() then reads the codec's
        # name alone, and a name the catalog does not know matches nothing.
        _brand, named = identify({"devEUI": NO_MODEL_EUI, "payloadName": codec["name"]})
        if named == model_id:
            return codec["id"]
    return "0"


async def async_import(  # noqa: PLR0913
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    application_id: str,
    profile_id: str,
    codec_id: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create each row's device; one failure does not stop the others."""
    call = network_server(hass, entry)
    codecs = (await async_choices(hass, entry))["codecs"] if codec_id == AUTO else []
    results = []
    for index, row in enumerate(rows):
        if index:
            await asyncio.sleep(PAUSE)
        dev_eui = str(row.get("dev_eui") or "").strip()
        app_key = str(row.get("app_key") or DEFAULT_APP_KEY).strip()
        result: dict[str, Any] = {"dev_eui": dev_eui.upper()}
        if not _EUI.match(dev_eui) or not _KEY.match(app_key):
            result["error"] = "devEUI needs 16 hex digits, an AppKey 32"
            results.append(result)
            continue
        codec = _auto_codec(dev_eui, codecs) if codec_id == AUTO else codec_id
        body = {
            "name": str(row.get("name") or dev_eui.upper())[:64],
            "description": str(row.get("description") or "")[:128],
            "devEUI": dev_eui.lower(),
            "applicationID": str(application_id),
            "profileID": str(profile_id),
            "appKey": app_key.lower(),
            "fPort": 85,
            "skipFCntCheck": False,
            "payloadCodecID": str(codec),
        }
        try:
            await call("POST", "/api/urdevices", body)
            result["ok"] = True
            result["codec"] = codec
        except (APIAuthError, APIConnectionError, NsApiError) as err:
            result["error"] = str(err)
        results.append(result)
    if any(r.get("ok") for r in results) and not gateway_only(entry.data):
        await entry.runtime_data.coordinator.async_request_refresh()
    return results
