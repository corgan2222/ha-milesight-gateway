"""
The state of the last settings or command sent to a device.

A Class A device takes a downlink only after its next uplink, and it says
little about it. Three things tell what happened: the gateway's ACK for a
confirmed downlink (MQTT ack topic), an uplink that reports a sent field back
(most settings), and the uplinks that pass without either.

Only a downlink with an ACK channel is sent again: without one, silence does
not mean loss, and a command such as reboot must not run twice.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from milesight_gateway_api import decoder

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from milesight_gateway_api.lorawan_devices import MilesightDevice

    from .journal import Journal

_LOGGER = logging.getLogger(__name__)

EVENT_COMMAND = "milesight_gateway_command"
# Sources of a kept setting; settings.py.
SENT_SOURCE, ACK_SOURCE = "sent", "ack"

PENDING = "pending"
RETRYING = "retrying"
# The downlink left the gateway; a confirmed one's ACK comes with the uplink
# after that. Only the network server's packet log tells.
TRANSMITTED = "transmitted"
ACKNOWLEDGED = "acknowledged"
CONFIRMED = "confirmed"
DIFFERS = "differs"
SENT = "sent"
FAILED = "failed"
STATES = [
    PENDING,
    RETRYING,
    TRANSMITTED,
    ACKNOWLEDGED,
    CONFIRMED,
    DIFFERS,
    SENT,
    FAILED,
]
# States of a command still on its way.
OPEN = (PENDING, RETRYING, TRANSMITTED, ACKNOWLEDGED)
# States that end a command; the event fires once on them.
FINAL = frozenset({CONFIRMED, DIFFERS, SENT, FAILED})

# Uplinks without an ACK before a confirmed downlink counts as lost. The ACK
# rides on the uplink after the one that fetched the downlink, and may come
# in a moment after it.
ACK_UPLINKS = 3
# Uplinks without a report back before the device is taken to keep quiet.
ECHO_UPLINKS = 3
# Uplinks after which a device that reports another value has had the
# downlink: the first one only fetches it.
SETTLED = 2
RETRIES = 2


def _same(sent: Any, reported: Any) -> bool:
    try:
        return abs(float(sent) - float(reported)) < 0.01  # noqa: PLR2004
    except TypeError, ValueError:
        return str(sent) == str(reported)


class CommandTracker:
    """Follows the last changes sent to one device until they settle."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: MilesightDevice,
        remember: Callable[[dict[str, Any], str], None] | None = None,
        journal: Journal | None = None,
    ) -> None:
        """Start without a command; ``remember`` keeps sent settings."""
        self.hass = hass
        self.device = device
        self._remember = remember
        self._journal = journal
        self._record: dict[str, Any] | None = None
        self.state: str | None = None
        self.changes: dict[str, Any] = {}
        self.attempts = 0
        self.changed_at: datetime | None = None
        self.reported: dict[str, Any] = {}
        self._uplinks = 0
        self._sent_uplinks = 0
        self._acked = False
        self._acks = 0
        self._transmissions = 0
        # Set once the command is over, whatever its state says.
        self._closed = False
        # What the gateway's ACK names this command's downlink by, if anything.
        self.reference: str | None = None
        # A command taken up after a restart is followed, not sent again: what
        # went out before the restart may be hours old.
        self._resumed = False
        self._resume()

    def _resume(self) -> None:
        """
        Take up the command that was on its way before a restart or a reload.

        Its downlink is still at the gateway or the device, and the answer
        still comes. Older ones the device had open are over: a newer command
        took their place.
        """
        if self._journal is None:
            return
        eui = self.device.descriptor.dev_eui
        waiting = self._journal.open_commands(eui, OPEN)
        if not waiting:
            return
        newest = self._journal.open_commands(eui, tuple(STATES))[-1]
        for record in waiting:
            if record is not newest:
                record["state"] = SENT
        self._journal.saved()
        if newest not in waiting:
            return  # the device's newest command is over already
        record = self._record = newest
        self._resumed = True
        self.state = record["state"]
        self.changes = dict(record.get("command") or {})
        self.attempts = int(record.get("attempts") or 1)
        self.reported = dict(record.get("reported") or {})
        self.reference = record.get("reference")
        self._acked = self.state == ACKNOWLEDGED
        self.changed_at = dt_util.parse_datetime(str(record.get("changed") or ""))
        self._uplinks = self._sent_uplinks = self.device.uplinks
        self._acks = getattr(self.device, "acks", 0)
        self._transmissions = getattr(self.device, "transmissions", 0)
        self._journal.saved()

    def note_reference(self, reference: str) -> None:
        """Keep the name of the downlink just sent, over a restart too."""
        self.reference = reference
        if self._record is not None:
            self._record["reference"] = reference

    @property
    def confirms(self) -> bool:
        """Whether the gateway reports an ACK for this device's downlinks."""
        return bool(getattr(self.device, "confirms", False))

    async def async_send(self, changes: dict[str, Any]) -> None:
        """Send and start following; a failed send raises and marks it failed."""
        if self.state in OPEN and not self._closed:
            # The new command takes this one's place. What became of it will
            # never be known, so it ends as sent instead of staying open.
            self._closed = True
            self._set(SENT)
        self._resumed = False
        self.changes = dict(changes)
        self.attempts = 0
        self.reported = {}
        if self._journal is not None:
            self._record = self._journal.add(
                self.device.descriptor.dev_eui,
                "command",
                command=self.changes,
                confirms=self.confirms,
            )
        await self._async_attempt()

    async def _async_attempt(self) -> None:
        self.attempts += 1
        self._acked = False
        self._closed = False
        self._sent_uplinks = self.device.uplinks
        self._acks = getattr(self.device, "acks", 0)
        self._transmissions = getattr(self.device, "transmissions", 0)
        try:
            await self.device.async_set(self.changes, confirmed=self.confirms)
        except Exception:
            self._set(FAILED)
            raise
        self._keep(SENT_SOURCE)
        self._set(PENDING if self.attempts == 1 else RETRYING)

    @callback
    def ack(self, *, acknowledged: bool) -> None:
        """Take the gateway's ACK for the newest confirmed downlink."""
        if self.state not in (PENDING, RETRYING, TRANSMITTED):
            return
        if acknowledged:
            self._acked = True
            self._keep(ACK_SOURCE)
            self._set(ACKNOWLEDGED)
        else:
            self._retry_or_fail()

    @callback
    def error(self, reason: str) -> None:
        """Take the gateway's error report for the newest downlink."""
        if self.state in OPEN and not self._closed:
            self.reported = {"error": reason}
            self._set(FAILED)

    @callback
    def check(self) -> None:
        """Take an ACK from the packet log, look at a new uplink, or count it."""
        sent = getattr(self.device, "transmissions", 0)
        if sent != self._transmissions:
            self._transmissions = sent
            if self.state in (PENDING, RETRYING):
                self._set(TRANSMITTED)
        acks = getattr(self.device, "acks", 0)
        if acks != self._acks:
            self._acks = acks
            # The packet log's ACK names no command. One that comes before
            # this command's downlink left, with the first uplink after the
            # send, answers the command before it.
            if self.state == TRANSMITTED or self.device.uplinks > self._sent_uplinks:
                self.ack(acknowledged=bool(getattr(self.device, "acknowledged", True)))
        if self.device.uplinks == self._uplinks:
            return  # a local change, no uplink
        self._uplinks = self.device.uplinks
        if self.state not in OPEN or self._closed:
            return
        decoded = decoder.flatten(self.device.last_decoded)
        passed = self.device.uplinks - self._sent_uplinks
        if hit := {key: decoded[key] for key in self.changes if key in decoded}:
            same = all(_same(self.changes[key], value) for key, value in hit.items())
            # The uplink that fetches the downlink still carries the old
            # value: a difference counts once the downlink is known to have
            # arrived, or with the uplink after.
            arrived = self._acked or self.state == TRANSMITTED or passed >= SETTLED
            if same or arrived:
                self.reported = hit
                self._set(CONFIRMED if same else DIFFERS)
                return
        if self.confirms and not self._acked and passed >= ACK_UPLINKS:
            self._retry_or_fail()
        elif passed >= ECHO_UPLINKS:
            # The device does not report this field. An ACK says the frame
            # arrived, not that the device took the value (one outside its
            # range is acknowledged and dropped), so it stays "acknowledged".
            self._closed = True
            self._set(ACKNOWLEDGED if self._acked else SENT)

    def _keep(self, source: str) -> None:
        """Hand the sent settings (not the commands) to the settings log."""
        if self._remember is None:
            return
        fields = self.device.fields
        settings = {
            key: value
            for key, value in self.changes.items()
            if fields.get(key, {}).get("access") == "RW"
        }
        self._remember(settings, source)

    def _retry_or_fail(self) -> None:
        if self._resumed:
            # The answer may have come while Home Assistant was down: what
            # became of the command is not known, which is not a failure.
            self._closed = True
            self._set(SENT)
            return
        if self.attempts > RETRIES:
            self._set(FAILED)
            return
        self.hass.async_create_task(self._async_retry(), eager_start=False)

    async def _async_retry(self) -> None:
        try:
            await self._async_attempt()
        except Exception as err:  # noqa: BLE001 - reported as the state
            _LOGGER.warning(
                "Resending to %s failed: %s", self.device.descriptor.dev_eui, err
            )

    def _set(self, state: str) -> None:
        self.state = state
        self.changed_at = dt_util.utcnow()
        if self._record is not None and self._journal is not None:
            self._record |= {
                "state": state,
                "attempts": self.attempts,
                "reported": self.reported,
                "changed": self.changed_at.isoformat(),
            }
            self._journal.saved()
        self.device.notify()
        if state in FINAL or self._closed:
            self.hass.bus.async_fire(
                EVENT_COMMAND,
                {
                    "dev_eui": self.device.descriptor.dev_eui.upper(),
                    "status": state,
                    "command": self.changes,
                    "attempts": self.attempts,
                    "reported": self.reported,
                },
            )
