"""
Device values from MQTT, as the gateway's applications publish them.

The gateway publishes each device's uplinks under a topic the user sets up per
application. The entry's topic template says where the devEUI sits in that
topic, for example ``sensors/lora/uplink/{dev_eui}``.

Two kinds of entry use this. A gateway with a network server login (UG65,
UG67) lists its devices and their payload codecs: those are the devices, and
they follow the list's online state. A gateway without one (UG63 Mini) lists
nothing: a device is created with its first uplink, its model comes from the
devEUI prefix in Milesight's codec catalog, and where several models share a
prefix, from the fields it reports.

Settings go out as downlinks on the application's downlink topic, and their
ACK comes back on its ACK topic. An entry with a login and no downlink topic
queues them at the network server instead; one with neither sends nothing.

The devices are the same models the LoRaWAN source uses, fed with the JSON the
gateway already decoded, so the entities are built the same way. Fields a
payload codec can add from the gateway's radio data (RSSI, SNR, receiving
gateway) get entities of their own.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from lorawan_connection import DeviceDescriptor, DownlinkError, GatewayReception
from milesight_gateway_api import decoder
from milesight_gateway_api.lorawan import identify
from milesight_gateway_api.lorawan_devices import MODELS, MilesightDevice

from .api import APIAuthError, APIConnectionError, MqttTopics
from .const import DOMAIN
from .link import EU868_SF
from .lorawan_coordinator import MilesightLoRaWANCoordinator
from .nsapi import MqttNsApi, NsApiError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from homeassistant.components.mqtt import ReceiveMessage
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import MilesightGatewayCoordinator

_LOGGER = logging.getLogger(__name__)

DEV_EUI = "{dev_eui}"
EVENT_JOIN = "milesight_gateway_join"
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
} | {
    # Reported by codecs built on Milesight's decoders 1.5.31, which the
    # current catalog no longer has.
    "pir_trigger": {"access": "R", "name": "Motion", "type": "BOOL"},
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


def _template(topic: str | None) -> TopicTemplate | None:
    return TopicTemplate.parse(topic) if topic else None


def from_gateway(topics: MqttTopics) -> MqttTopics:
    """
    Write the gateway's topics as templates: "$deveui" becomes "{dev_eui}".

    The request and response topics name no device; the gateway takes them
    word for word, "$deveui" included.
    """

    def device(topic: str | None) -> str | None:
        if not topic:
            return None
        return re.sub(r"\$deveui", DEV_EUI, topic, flags=re.IGNORECASE)

    return replace(
        topics,
        uplink=device(topics.uplink) or topics.uplink,
        downlink=device(topics.downlink),
        ack=device(topics.ack),
        error=device(topics.error),
        join=device(topics.join),
    )


def request_error(topic: str) -> str | None:
    """Return why a request or response topic is unusable: it is used as is."""
    if not topic.strip() or "+" in topic or "#" in topic:
        return "invalid_downlink_topic"
    return None


def downlink_error(template: str) -> str | None:
    """Return why a downlink topic is unusable: it must name one device."""
    levels = template.strip().split("/")
    if topic_error(template) or levels.count(DEV_EUI) != 1:
        return "invalid_downlink_topic"
    if any(level == "+" or ("{" in level and level != DEV_EUI) for level in levels):
        return "invalid_downlink_topic"
    return None


def sibling_topic(template: str, kind: str) -> str | None:
    """Suggest the downlink or ACK topic next to a Milesight uplink topic."""
    levels = template.strip().split("/")
    if "uplink" not in levels:
        return None
    levels[levels.index("uplink")] = kind
    return "/".join(levels)


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


type Sender = Callable[[str, bytes, int], Awaitable[None]]


class _MqttDevice:
    """What an MQTT device adds to its catalog model."""

    # False while the gateway's device list leaves the device out.
    active = True
    # Sends encoded bytes to the device; None where the gateway takes none.
    sender: Sender | None = None
    # Whether the gateway reports an ACK for the downlinks; see commands.py.
    confirms = False

    async def async_set(
        self,
        changes: dict[str, Any],
        *,
        confirmed: bool = False,  # noqa: ARG002 - the runtime decides
    ) -> None:
        """Send by the runtime's way; confirmed when it hears ACKs."""
        await super().async_set(changes)  # type: ignore[misc]

    async def async_send_downlink(
        self,
        *,
        data: bytes,
        f_port: int,
        wait_for_ack: bool = True,  # noqa: ARG002 - the tracker follows the ACK
        expires_at: datetime | None = None,  # noqa: ARG002
    ) -> None:
        """Hand the encoded settings to the entry's sender: MQTT or the queue."""
        if self.sender is None:
            msg = "This gateway takes no downlinks"
            raise DownlinkError(msg)
        await self.sender(self.descriptor.dev_eui, data, f_port)  # type: ignore[attr-defined]


_CLASSES: dict[str, type[MilesightDevice]] = {}


def _model_class(model_id: str) -> type[MilesightDevice]:
    """Return the model's device class, with the overlay fields added."""
    if model_id not in _CLASSES:
        base = next(model for model in MODELS if model.model_id == model_id)
        _CLASSES[model_id] = type(
            f"Mqtt{base.__name__}",
            (_MqttDevice, base),
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
    receptions: tuple[GatewayReception, ...] = ()
    f_cnt: int | None = None
    spreading_factor: int | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


_SF = re.compile(r"SF(\d+)", re.IGNORECASE)


def _uplink(payload: dict[str, Any]) -> _Uplink:
    """
    Read the radio side of an uplink, as far as the payload tells it.

    The gateway's own format lists every receiving gateway (rxInfo) and the
    spreading factor (txInfo); an enriched codec names the best gateway, its
    RSSI and SNR, the frame counter and the EU868 data rate.
    """
    receptions = tuple(
        GatewayReception(
            str(rx.get("mac") or rx.get("gatewayID") or ""),
            _number(rx.get("rssi")),
            _number(rx.get("loRaSNR")),
        )
        for rx in payload.get("rxInfo") or ()
        if isinstance(rx, dict)
    )
    rssi = _number(payload.get("rssi"))
    if not receptions and rssi is not None:
        snr = payload.get("loRaSNR", payload.get("snr"))
        gateway = payload.get("mac") or payload.get("gw") or ""
        receptions = (GatewayReception(str(gateway), rssi, _number(snr)),)
    f_cnt = _number(payload.get("fCnt", payload.get("frame_count")))
    tx = payload.get("txInfo") if isinstance(payload.get("txInfo"), dict) else {}
    rate = tx.get("dataRate") if isinstance(tx.get("dataRate"), dict) else {}
    sf = _number(rate.get("spreadFactor"))
    datarate = payload.get("datarate")
    if sf is None and (named := _SF.match(str(datarate))):
        sf = int(named.group(1))  # "SF7BW125", as the UG65 sends it
    elif sf is None and (dr := _number(datarate)) is not None:
        sf = EU868_SF.get(int(dr))
    return _Uplink(
        dt_util.utcnow(),
        receptions,
        int(f_cnt) if f_cnt is not None else None,
        int(sf) if sf is not None else None,
    )


@dataclass
class _Uplinks:
    """Stands in for the LoRaWAN connection: last uplinks, no downlinks."""

    last_uplinks: dict[str, _Uplink] = field(default_factory=dict)


class MqttDiscoveryRuntime:
    """One gateway entry's MQTT subscription, device models and coordinators."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        topics: MqttTopics,
        network: MilesightGatewayCoordinator | None = None,
    ) -> None:
        """
        Prepare; async_start() subscribes.

        With the gateway's device list (``network``) the devices are the listed
        ones; without it they are found on the uplink topic. Downlinks go out
        on the downlink topic; without one, through the network server's
        queue, if there is one. ACKs, errors and joins arrive on their topics,
        the network server's API answers on ``response``.
        """
        self.hass = hass
        self.entry = entry
        self.template = TopicTemplate.parse(topics.uplink)
        self.connection = _Uplinks()
        self.coordinators: dict[str, MilesightLoRaWANCoordinator] = {}
        self._network = network
        self._started = False
        # The subscriptions async_apply_topics() replaces.
        self._topic_unsubscribe: list[Callable[[], None]] = []
        self._configure(topics)
        # The network server's API by MQTT, for an entry that has no login to
        # it; one with a login calls it directly.
        self.ns_api = (
            MqttNsApi(hass, topics.request, topics.response)
            if network is None and topics.request and topics.response
            else None
        )
        # The reference of each device's newest downlink; the ACK repeats it.
        self._references: dict[str, str] = {}
        self._sequence = 0
        self._run = secrets.token_hex(3)
        # What the gateway last said about each device being online.
        self._verdicts: dict[str, bool] = {}
        self._listeners: list[Callable[[MilesightLoRaWANCoordinator], None]] = []
        self._unsubscribe: list[Callable[[], None]] = []
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
        if self._network is not None:
            self._sync()
            self._unsubscribe.append(self._network.async_add_listener(self._sync))
        self._unsubscribe.append(
            await mqtt.async_subscribe(
                self.hass, self.template.subscription, self._message
            )
        )
        self._started = True
        await self._async_subscribe_topics()
        if self.ns_api is not None:
            self._unsubscribe.append(await self.ns_api.async_start())
            self.entry.async_create_background_task(
                self.hass, self._async_inventory(), "Read the Milesight devices"
            )

    def _configure(self, topics: MqttTopics) -> None:
        """Take the topics settings go out on and their outcome comes back on."""
        self.topics = topics
        self._downlink = topics.downlink or None
        self.sends_downlinks = self._network is not None or bool(self._downlink)
        # How values come and settings go, for the device page.
        self.path = (
            "mqtt+rest" if self._network is not None and not self._downlink else "mqtt"
        )
        # Both ways send confirmed downlinks when the application reports ACKs.
        self._ack = _template(topics.ack) if self.sends_downlinks else None
        self._error = _template(topics.error)
        self._join = _template(topics.join)

    async def _async_subscribe_topics(self) -> None:
        from homeassistant.components import mqtt  # noqa: PLC0415

        while self._topic_unsubscribe:
            self._topic_unsubscribe.pop()()
        for template, handler in (
            (self._ack, self._message_ack),
            (self._error, self._message_error),
            (self._join, self._message_join),
        ):
            if template is not None:
                self._topic_unsubscribe.append(
                    await mqtt.async_subscribe(
                        self.hass, template.subscription, handler
                    )
                )

    async def async_apply_topics(self, topics: MqttTopics) -> None:
        """
        Switch to the topics the gateway named, once it answered.

        At Home Assistant's start the gateway may not answer in time; until
        it does, the stored topics or the network server's queue serve.
        """
        self._configure(topics)
        for coordinator in self.coordinators.values():
            self._wire(coordinator.data)
        if self._started:
            await self._async_subscribe_topics()

    def _wire(self, device: MilesightDevice) -> None:
        """Tell a device how its settings go out."""
        if self._downlink:
            device.sender = self._async_publish
        elif self._network is not None:
            device.sender = self._async_send
        else:
            device.sender = None
        device.confirms = device.sender is not None and self._ack is not None

    async def _async_inventory(self) -> None:
        """Name the gateway's devices and their models through its MQTT API."""
        try:
            devices = await self.ns_api.async_devices()
        except NsApiError as err:
            _LOGGER.info("The gateway's device list is not readable by MQTT: %s", err)
            return
        for raw in devices:
            eui = _eui(raw.get("devEUI"))
            if eui is None or eui in self.coordinators or self._other_owner(eui):
                continue
            _brand, model_id = identify(raw)
            if model_id in decoder.models():
                self._add(eui, model_id, raw.get("name") or eui.upper())

    def _reference(self, dev_eui: str) -> str:
        """Name a device's newest downlink; its ACK repeats the name."""
        self._sequence += 1
        # The run's own token: the counter starts anew with every start, and
        # an ACK of the run before must not match.
        reference = self._references[dev_eui] = f"ha-{self._run}-{self._sequence}"
        if coordinator := self.coordinators.get(dev_eui):
            coordinator.commands.note_reference(reference)
        return reference

    async def _async_publish(self, dev_eui: str, data: bytes, f_port: int) -> None:
        """Put encoded bytes on the downlink topic, confirmed if ACKs come back."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        reference = self._reference(dev_eui)
        try:
            await mqtt.async_publish(
                self.hass,
                self._downlink.replace(DEV_EUI, dev_eui),
                json.dumps(
                    {
                        "confirmed": self._ack is not None,
                        "fport": f_port,
                        "data": base64.b64encode(data).decode("ascii"),
                        "reference": reference,
                    }
                ),
            )
        except HomeAssistantError as err:
            raise DownlinkError(str(err)) from err

    def _device_message(
        self, template: TopicTemplate, msg: ReceiveMessage
    ) -> tuple[MilesightLoRaWANCoordinator, dict[str, Any]] | None:
        """Return the device a notification is about, with its payload."""
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        eui = _eui(template.dev_eui(msg.topic)) or _eui(payload.get("devEUI"))
        coordinator = self.coordinators.get(eui) if eui else None
        return (coordinator, payload) if coordinator is not None else None

    @callback
    def _message_ack(self, msg: ReceiveMessage) -> None:
        """Hand the gateway's ACK to the command it belongs to."""
        if (found := self._device_message(self._ack, msg)) is None:
            return
        coordinator, payload = found
        eui = coordinator.data.descriptor.dev_eui
        reference = payload.get("reference")
        # A command taken up after a restart still has the name it went out by.
        expected = self._references.get(eui) or coordinator.commands.reference
        if reference and reference != expected:
            return  # an older downlink's
        coordinator.commands.ack(acknowledged=payload.get("acknowledged") is True)

    @callback
    def _message_error(self, msg: ReceiveMessage) -> None:
        """Fail the pending command with the gateway's reason."""
        if (found := self._device_message(self._error, msg)) is None:
            return
        coordinator, payload = found
        reason = payload.get("error") or payload.get("type") or "error"
        _LOGGER.warning(
            "The gateway reports an error for %s: %s",
            coordinator.data.descriptor.dev_eui.upper(),
            reason,
        )
        coordinator.commands.error(str(reason))

    @callback
    def _message_join(self, msg: ReceiveMessage) -> None:
        """Restart the frame counter of a device that joined again."""
        if (found := self._device_message(self._join, msg)) is None:
            return
        coordinator, _payload = found
        coordinator.link.joined()
        if (journal := self.entry.runtime_data.journal) is not None:
            journal.add(coordinator.data.descriptor.dev_eui, "join")
        self.hass.bus.async_fire(
            EVENT_JOIN, {"dev_eui": coordinator.data.descriptor.dev_eui.upper()}
        )

    @callback
    def _sync(self) -> None:
        """Follow the gateway's device list: add new devices, mark dropped ones."""
        network = self._network
        listed = {
            eui: device
            for device in network.data.devices
            if (eui := _eui(device.dev_eui))
        }
        for eui, device in listed.items():
            if (coordinator := self.coordinators.get(eui)) is not None:
                coordinator.rename(device.name)
            elif not self._other_owner(eui):
                self._add(eui, device.model_id, device.name)
        # A gateway that does not answer lists nothing for sure.
        reachable = network.last_update_success
        for eui, coordinator in self.coordinators.items():
            verdict = reachable and eui in listed and listed[eui].online
            # Only a change counts. An uplink that came in since proves more
            # than a gateway that calls the device offline: another gateway
            # may publish for it on the same topic.
            # ponytail: such a device stays available when it falls silent;
            # tell foreign uplinks by the gateway's "mac" to end that.
            if self._verdicts.get(eui) == verdict:
                continue
            self._verdicts[eui] = verdict
            if coordinator.data.active != verdict:
                coordinator.data.active = verdict
                coordinator.async_update_listeners()

    async def _async_send(self, dev_eui: str, data: bytes, f_port: int) -> None:
        """Queue encoded bytes at the network server, which names devices in caps."""
        try:
            await self._network.api.async_enqueue_downlink(
                async_get_clientsession(self.hass),
                dev_eui.upper(),
                data,
                fport=f_port,
                reference=self._reference(dev_eui) if self._ack else None,
            )
        except (APIAuthError, APIConnectionError) as err:
            raise DownlinkError(str(err)) from err

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
        # Some codecs name the SNR "snr", some send both: one entity.
        if "snr" in values:
            values.setdefault("loRaSNR", values.pop("snr"))
        coordinator = self.coordinators.get(eui) or self._discover(
            eui, values, payload.get("deviceName"), msg.topic
        )
        if coordinator is None:
            return
        device = coordinator.data
        # A device that reports is there, whatever the gateway's list says.
        device.active = True
        if not values.keys() & device.fields.keys() and isinstance(
            payload.get("data"), str
        ):
            # No payload codec on the gateway: decode the raw bytes here.
            self.entry.async_create_background_task(
                self.hass,
                self._async_decode(device, payload["data"], _uplink(payload)),
                "Decode Milesight uplink",
            )
            return
        self._apply(device, values, _uplink(payload))

    @callback
    def _discover(
        self, eui: str, values: dict[str, Any], name: Any, topic: str
    ) -> MilesightLoRaWANCoordinator | None:
        """Create the device of a first uplink, or None when it is not ours."""
        # A gateway that lists its devices names all of ours.
        if eui in self._unknown or self._network is not None:
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

    async def _async_decode(
        self, device: MilesightDevice, data: str, uplink: _Uplink
    ) -> None:
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
        self._apply(device, decoder.flatten(decoded), uplink)

    @callback
    def _apply(
        self, device: MilesightDevice, values: dict[str, Any], uplink: _Uplink
    ) -> None:
        self.connection.last_uplinks[device.descriptor.dev_eui] = uplink
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
        self._wire(device)
        coordinator = self.coordinators[eui] = MilesightLoRaWANCoordinator(
            self.hass,
            self.entry,
            device,
            self.connection,  # type: ignore[arg-type]
            self.entry.runtime_data.gateway_device_id,
        )
        if not decoder.is_loaded(model_id):
            # The encoder and the example uplink need it; unpacking reads a file.
            self.entry.async_create_background_task(
                self.hass, self._async_load_codec(device), "Unpack Milesight codec"
            )
        for listener in tuple(self._listeners):
            listener(coordinator)
        return coordinator

    async def _async_load_codec(self, device: MilesightDevice) -> None:
        await self.hass.async_add_executor_job(decoder.load, [device.model_id])
        device.codec_loaded()

    @callback
    def subscribe_coordinator_added(
        self, listener: Callable[[MilesightLoRaWANCoordinator], None]
    ) -> Callable[[], None]:
        """Call listener for every device now and for every one added later."""
        self._listeners.append(listener)
        for coordinator in tuple(self.coordinators.values()):
            listener(coordinator)
        return lambda: self._listeners.remove(listener)

    @callback
    def forget(self, dev_eui: str) -> None:
        """Retire a device the gateway no longer has."""
        self._verdicts.pop(dev_eui.lower(), None)
        if coordinator := self.coordinators.pop(dev_eui.lower(), None):
            coordinator.async_shutdown_listener()
            coordinator.data.close()

    async def async_stop(self) -> None:
        """Unsubscribe and retire the devices."""
        self._started = False
        while self._topic_unsubscribe:
            self._topic_unsubscribe.pop()()
        while self._unsubscribe:
            self._unsubscribe.pop()()
        for coordinator in self.coordinators.values():
            coordinator.async_shutdown_listener()
            coordinator.data.close()
        self.coordinators.clear()
