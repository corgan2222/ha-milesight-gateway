"""
The network server's API over MQTT, for a gateway Home Assistant cannot reach.

The gateway's application takes requests on its request topic and answers on
its response topic, word for word as configured ("$deveui" included, it
names no device). A request carries an ID the answer repeats:

    {"type": "ns-api", "id": "ha-1", "method": "GET", "url": "/api/urdevices"}

The answer's body is what the REST API answers to the same call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.components.mqtt import ReceiveMessage

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 20


class NsApiError(HomeAssistantError):
    """The gateway answered with an error, or not at all."""


def _body(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {"raw": body}
    return body if isinstance(body, dict) else {}


class MqttNsApi:
    """Requests on one topic, answers on another, matched by ID."""

    def __init__(self, hass: HomeAssistant, request: str, response: str) -> None:
        """Prepare; async_start() subscribes to the answers."""
        self.hass = hass
        self.request_topic = request
        self.response_topic = response
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sequence = 0

    async def async_start(self) -> Callable[[], None]:
        """Listen for answers; return the unsubscribe."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        return await mqtt.async_subscribe(self.hass, self.response_topic, self._answer)

    @callback
    def _answer(self, msg: ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        waiting = self._waiting.pop(str(payload.get("id")), None)
        if waiting is not None and not waiting.done():
            waiting.set_result(payload)

    async def async_request(
        self, method: str, url: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one call and return the body of its answer."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        self._sequence += 1
        request_id = f"ha-{self._sequence}"
        message: dict[str, Any] = {
            "type": "ns-api",
            "id": request_id,
            "method": method.upper(),
            "url": url,
        }
        if body is not None:
            message["body"] = body
        answer = self._waiting[request_id] = self.hass.loop.create_future()
        try:
            await mqtt.async_publish(self.hass, self.request_topic, json.dumps(message))
            async with asyncio.timeout(TIMEOUT):
                payload = await answer
        except TimeoutError as err:
            msg = f"No answer on {self.response_topic} within {TIMEOUT} s"
            raise NsApiError(msg) from err
        finally:
            self._waiting.pop(request_id, None)
        result = _body(payload)
        code = result.get("code", payload.get("code"))
        if code not in (None, 200, "200") or result.get("error"):
            msg = f"{method} {url}: {result.get('error') or code}"
            raise NsApiError(msg)
        return result

    async def async_devices(self) -> list[dict[str, Any]]:
        """Return the network server's whole device list."""
        result = await self.async_request(
            "GET", "/api/urdevices?applicationID=0&offset=0&limit=10000"
        )
        devices = result.get("deviceResult")
        return (
            [d for d in devices if isinstance(d, dict)]
            if isinstance(devices, list)
            else []
        )
