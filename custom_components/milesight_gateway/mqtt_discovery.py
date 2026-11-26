"""
Devices found on MQTT, for a gateway that lists none (UG63 Mini).

The gateway publishes each device's uplinks under a topic the user sets up per
application. The entry's topic template says where the devEUI sits in that
topic, for example ``sensors/lora/uplink/{dev_eui}``. A device is created with
its first uplink; its model comes from the devEUI prefix in Milesight's codec
catalog, and where several models share a prefix, from the fields it reports.

The devices are the same models the LoRaWAN source uses, fed with the JSON the
gateway already decoded, so the entities are built the same way. Fields a
payload codec can add from the gateway's radio data (RSSI, SNR, receiving
gateway) get entities of their own. Nothing is sent to a device: the topic
carries no downlinks.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from lorawan_connection import DeviceDescriptor
from milesight_gateway_api import decoder
from milesight_gateway_api.lorawan_devices import MODELS, MilesightDevice

from .const import CONF_MQTT_TOPIC, DOMAIN
from .lorawan_coordinator import MilesightLoRaWANCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.components.mqtt import ReceiveMessage
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

DEV_EUI = "{dev_eui}"
EUI_DIGITS = 16
# Fields a payload codec can copy from the gateway's LoRaObject. They are no
# catalog field, so they are described here, in the catalog's format.
OVERLAY_FIELDS: dict[str, dict[str, Any]] = {
    key: {"access": "R", "diagnostic": True, **field}
    for key, field in {
        "rssi": {"name": "RSSI", "type": "NUMBER", "unit": "dBm"},
        "loRaSNR": {"name": "SNR", "type": "NUMBER", "unit": "dB"},
        "gw": {"name": "Receiving gateway", "type": "TEXT"},
        "mac": {"name": "Receiving gateway EUI", "type": "TEXT"},
    }.items()
}
# Payload keys that describe the uplink rather than the device's state.
_META = frozenset(
    {
        "devEUI",
        "deviceName",
        "applicationName",
        "applicationID",
        "timestamp",
        "time",
        "fPort",
        "fCnt",
        "data",
        "gatewayTime",
    }
)


def topic_error(template: str) -> str | None:
    """Return why a topic template is unusable, or None."""
    levels = template.strip().split("/")
    if not template.strip() or "#" in template:
        return "invalid_topic"
    for level in levels:
        placeholder = "{" in level or "}" in level
        if placeholder and not (level.startswith("{") and level.endswith("}")):
            # A placeholder must fill a whole level, or MQTT cannot match it.
            return "invalid_topic"
        if "+" in level and level != "+":
            return "invalid_topic"
    if levels.count(DEV_EUI) > 1:
        return "invalid_topic"
    return None


@dataclass(frozen=True)
class TopicTemplate:
    """Where the devEUI sits in the topic, and what to subscribe to."""

    subscription: str
    eui_level: int | None

    @classmethod
    def parse(cls, template: str) -> TopicTemplate:
        """Read a template; any placeholder other than {dev_eui} matches anything."""
        levels = template.strip().split("/")
        return cls(
            subscription="/".join(
                "+" if level.startswith("{") else level for level in levels
            ),
            eui_level=levels.index(DEV_EUI) if DEV_EUI in levels else None,
        )

    def dev_eui(self, topic: str) -> str | None:
        """Return the devEUI the topic names, if the template places one."""
        levels = topic.split("/")
        if self.eui_level is None or self.eui_level >= len(levels):
            return None
        return levels[self.eui_level]


def _eui(value: Any) -> str | None:
    """Return a devEUI as 16 lowercase hex digits, or None."""
    if not isinstance(value, str):
        return None
    eui = value.replace(":", "").replace("-", "").strip().lower()
    try:
        int(eui, 16)
    except ValueError:
        return None
    return eui if len(eui) == EUI_DIGITS else None


def guess_model(dev_eui: str, keys: set[str]) -> str | None:
    """
    Return the catalog model for a devEUI, or None.

    The longest matching prefix decides. Where several models share it (the
    EM300 variants, for one), the model whose fields cover most of the reported
    keys wins; on a tie the first by ID, since those report the same fields.
    """
    models = decoder.models()
    length = 0
    candidates: list[str] = []
    for model_id, model in models.items():
        prefix = str(model.get("deveui") or "").lower()
        if not prefix or not dev_eui.startswith(prefix):
            continue
        if len(prefix) > length:
            candidates, length = [model_id], len(prefix)
        elif len(prefix) == length:
            candidates.append(model_id)
    if not candidates:
        return None
    return max(
        sorted(candidates),
        key=lambda model_id: len(keys & models[model_id]["fields"].keys()),
    )


_CLASSES: dict[str, type[MilesightDevice]] = {}


def _model_class(model_id: str) -> type[MilesightDevice]:
    """Return the model's device class, with the overlay fields added."""
    if model_id not in _CLASSES:
        base = next(model for model in MODELS if model.model_id == model_id)
        _CLASSES[model_id] = type(
            f"Mqtt{base.__name__}",
            (base,),
            {
                "fields": property(
                    lambda self: (
                        decoder.models()[self.model_id]["fields"] | OVERLAY_FIELDS
                    )
                )
            },
        )
    return _CLASSES[model_id]


@dataclass
class _Uplink:
    """The last uplink of a device, as the LoRaWAN entities read it."""

    received_at: datetime
    receptions: tuple = ()


@dataclass
class _Uplinks:
    """Stands in for the LoRaWAN connection: last uplinks, no downlinks."""

    last_uplinks: dict[str, _Uplink] = field(default_factory=dict)


class MqttDiscoveryRuntime:
    """One gateway entry's MQTT subscription, device models and coordinators."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Prepare; async_start() subscribes."""
        self.hass = hass
        self.entry = entry
        self.template = TopicTemplate.parse(entry.options[CONF_MQTT_TOPIC])
        self.connection = _Uplinks()
        self.coordinators: dict[str, MilesightLoRaWANCoordinator] = {}
        self._listeners: list[Callable[[MilesightLoRaWANCoordinator], None]] = []
        self._unsubscribe: Callable[[], None] | None = None
        # Unknown devEUIs are logged once, not with every uplink.
        self._unknown: set[str] = set()

    async def async_start(self) -> None:
        """Bring back the devices seen before, then subscribe."""
        # MQTT is only an after-dependency: entries without devices need no broker.
        from homeassistant.components import mqtt  # noqa: PLC0415

        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            msg = "MQTT is not set up"
            raise ConfigEntryNotReady(msg)
        await self.hass.async_add_executor_job(decoder.preload)
        self._restore()
        self._unsubscribe = await mqtt.async_subscribe(
            self.hass, self.template.subscription, self._message
        )

    def _restore(self) -> None:
        """Recreate the devices registered under this entry, by their model."""
        by_name = {
            model["name"]: model_id for model_id, model in decoder.models().items()
        }
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            model_id = by_name.get(device.model_id or "")
            for domain, identifier in device.identifiers:
                eui = _eui(identifier) if domain == DOMAIN else None
                if eui and model_id and not self._other_owner(eui):
                    self._add(eui, model_id, device.name or eui.upper())

    @callback
    def _message(self, msg: ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        eui = _eui(self.template.dev_eui(msg.topic)) or _eui(payload.get("devEUI"))
        if eui is None:
            return
        values = {
            key: value
            for key, value in decoder.flatten(payload).items()
            if key not in _META
        }
        coordinator = self.coordinators.get(eui) or self._discover(
            eui, values, payload.get("deviceName"), msg.topic
        )
        if coordinator is None:
            return
        device = coordinator.data
        if not values.keys() & device.fields.keys() and isinstance(
            payload.get("data"), str
        ):
            # No payload codec on the gateway: decode the raw bytes here.
            self.entry.async_create_background_task(
                self.hass,
                self._async_decode(device, payload["data"]),
                "Decode Milesight uplink",
            )
            return
        self._apply(device, values)

    @callback
    def _discover(
        self, eui: str, values: dict[str, Any], name: Any, topic: str
    ) -> MilesightLoRaWANCoordinator | None:
        """Create the device of a first uplink, or None when it is not ours."""
        if eui in self._unknown:
            return None
        if owner := self._other_owner(eui):
            # Entity IDs are "<devEUI>_<field>", the same in every entry, so
            # history survives a change of source. A second copy of the device
            # would get only the fields the first lacks.
            self._unknown.add(eui)
            _LOGGER.info(
                "%s is a device of the Milesight entry %s already; its uplinks on "
                "%s are left to that entry",
                eui.upper(),
                owner,
                topic,
            )
            return None
        model_id = guess_model(eui, set(values))
        if model_id is None:
            self._unknown.add(eui)
            _LOGGER.warning(
                "No Milesight model has the devEUI prefix of %s; its uplinks on %s "
                "are ignored",
                eui.upper(),
                topic,
            )
            return None
        return self._add(
            eui, model_id, name if isinstance(name, str) and name else eui.upper()
        )

    def _other_owner(self, eui: str) -> str | None:
        """Return the title of another entry that has this device, if any."""
        registry = dr.async_get(self.hass)
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id != self.entry.entry_id and (
                registry.async_get_device_by_identifier(
                    (DOMAIN, eui.upper()), entry.entry_id
                )
            ):
                return entry.title
        return None

    async def _async_decode(self, device: MilesightDevice, data: str) -> None:
        try:
            raw = base64.b64decode(data, validate=True)
        except binascii.Error, ValueError:
            return
        await self.hass.async_add_executor_job(decoder.load, [device.model_id])
        try:
            decoded = decoder.decode(device.model_id, raw)
        except decoder.DecodeError as err:
            _LOGGER.debug(
                "Undecodable uplink from %s: %s", device.descriptor.dev_eui, err
            )
            return
        self._apply(device, decoder.flatten(decoded))

    @callback
    def _apply(self, device: MilesightDevice, values: dict[str, Any]) -> None:
        self.connection.last_uplinks[device.descriptor.dev_eui] = _Uplink(
            dt_util.utcnow()
        )
        device.last_decoded = values
        device.uplinks += 1
        device.values.update(values)
        device.notify()

    @callback
    def _add(self, eui: str, model_id: str, name: str) -> MilesightLoRaWANCoordinator:
        if coordinator := self.coordinators.get(eui):
            return coordinator
        device = _model_class(model_id)(
            DeviceDescriptor(
                network_id=self.entry.entry_id,
                stack="mqtt",
                dev_eui=eui,
                name=name,
                application_id="",
                profile_id="",
                model_id=model_id,
            )
        )
        coordinator = self.coordinators[eui] = MilesightLoRaWANCoordinator(
            self.hass,
            self.entry,
            device,
            self.connection,  # type: ignore[arg-type]
            self.entry.runtime_data.gateway_device_id,
        )
        for listener in tuple(self._listeners):
            listener(coordinator)
        return coordinator

    @callback
    def subscribe_coordinator_added(
        self, listener: Callable[[MilesightLoRaWANCoordinator], None]
    ) -> Callable[[], None]:
        """Call listener for every device now and for every one added later."""
        self._listeners.append(listener)
        for coordinator in tuple(self.coordinators.values()):
            listener(coordinator)
        return lambda: self._listeners.remove(listener)

    async def async_stop(self) -> None:
        """Unsubscribe and retire the devices."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for coordinator in self.coordinators.values():
            coordinator.async_shutdown_listener()
            coordinator.data.close()
        self.coordinators.clear()
