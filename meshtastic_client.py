"""Small Meshtastic ATAK client wrapper.

License note:
    This module depends on the Meshtastic Python library, which is licensed
    under GPL-3.0-only. Review GPL obligations before distributing software,
    appliances, containers, or servers that bundle this dependency.
"""

from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Queue
from typing import Optional

import meshtastic.serial_interface
import meshtastic.protobuf.atak_pb2 as atak_pb2
import meshtastic.protobuf.portnums_pb2 as portnums_pb2
from pubsub import pub

from app.received_messages import on_receive


MAX_MESHTASTIC_DATA_BYTES = 239
logger = logging.getLogger(__name__)


def _enum_values(enum_descriptor) -> dict[str, int]:
    return {value.name: value.number for value in enum_descriptor.values}


def _normalize_enum_key(value: object) -> str:
    return "".join(char.lower() for char in str(value) if char.isalnum())


TEAM_VALUES = _enum_values(atak_pb2.Team.DESCRIPTOR)
ROLE_VALUES = _enum_values(atak_pb2.MemberRole.DESCRIPTOR)
_TEAM_LOOKUP = {_normalize_enum_key(name): number for name, number in TEAM_VALUES.items()}
_ROLE_LOOKUP = {_normalize_enum_key(name): number for name, number in ROLE_VALUES.items()}
_TEAM_ALIASES = {
    "unspecified": atak_pb2.Unspecifed_Color,
    "darkblue": atak_pb2.Dark_Blue,
    "darkgreen": atak_pb2.Dark_Green,
}
_ROLE_ALIASES = {
    "unspecified": atak_pb2.Unspecifed,
    "member": atak_pb2.TeamMember,
    "lead": atak_pb2.TeamLead,
    "teamlead": atak_pb2.TeamLead,
    "forwardobserver": atak_pb2.ForwardObserver,
    "observer": atak_pb2.ForwardObserver,
    "fo": atak_pb2.ForwardObserver,
    "radio": atak_pb2.RTO,
    "rto": atak_pb2.RTO,
}


def _enum_name(values: dict[str, int], number: int) -> str:
    for name, enum_number in values.items():
        if enum_number == number:
            return name
    return str(number)


def team_value(team: str | int = "Cyan") -> int:
    """Return the TAK team enum value for a REST-friendly color/team string."""
    if isinstance(team, int):
        if team in TEAM_VALUES.values():
            return team
        raise ValueError(f"unknown TAK team value: {team}")

    key = _normalize_enum_key(team)
    if key in _TEAM_ALIASES:
        return _TEAM_ALIASES[key]
    if key in _TEAM_LOOKUP:
        return _TEAM_LOOKUP[key]

    supported = ", ".join(TEAM_VALUES)
    raise ValueError(f"unknown TAK team/color '{team}'; supported values: {supported}")


def team_name(team: str | int = "Cyan") -> str:
    """Return the canonical TAK team enum name."""
    return _enum_name(TEAM_VALUES, team_value(team))


def role_value(role: str | int = "TeamMember") -> int:
    """Return the TAK member role enum value for a REST-friendly role string."""
    if isinstance(role, int):
        if role in ROLE_VALUES.values():
            return role
        raise ValueError(f"unknown TAK role value: {role}")

    key = _normalize_enum_key(role)
    if key in _ROLE_ALIASES:
        return _ROLE_ALIASES[key]
    if key in _ROLE_LOOKUP:
        return _ROLE_LOOKUP[key]

    supported = ", ".join(ROLE_VALUES)
    raise ValueError(f"unknown TAK role '{role}'; supported values: {supported}")


def role_name(role: str | int = "TeamMember") -> str:
    """Return the canonical TAK member role enum name."""
    return _enum_name(ROLE_VALUES, role_value(role))


class MeshtasticClient:
    """Send compact ATAK plugin protobuf messages over a Meshtastic radio."""

    def __init__(
        self,
        serial_port: Optional[str] = None,
        timeout: int = 300,
        callsign: str = "BRIDGE",
        accept_broadcast: bool = True,
    ):
        """Connect to a Meshtastic device.

        Args:
            serial_port: Serial device path, such as ``/dev/cu.usbserial-0001``.
                When omitted, the Meshtastic library auto-detects a device.
            timeout: Serial connection timeout in seconds.
            callsign: Callsign used to decide whether received messages are addressed here.
            accept_broadcast: Accept common broadcast/group targets in addition to this callsign.
        """
        self.serial_port = serial_port
        self.callsign = callsign
        self.accept_broadcast = accept_broadcast
        self._send_lock = threading.Lock()
        self._receive_packets = Queue()
        self._stop_receive_worker = threading.Event()
        self._receive_worker = threading.Thread(
            target=self._process_received_packets,
            name="meshtastic-receive-worker",
            daemon=True,
        )
        started_at = time.monotonic()
        logger.debug(
            "creating Meshtastic SerialInterface devPath=%s timeout=%ss",
            serial_port or "auto-detect",
            timeout,
        )
        try:
            self.interface = meshtastic.serial_interface.SerialInterface(
                devPath=serial_port,
                timeout=timeout,
            )
        except Exception as exc:
            logger.error(
                "failed to create Meshtastic SerialInterface devPath=%s timeout=%ss elapsed=%.2fs error=%s",
                serial_port or "auto-detect",
                timeout,
                time.monotonic() - started_at,
                exc,
                exc_info=True,
            )
            raise
        logger.debug(
            "created Meshtastic SerialInterface devPath=%s elapsed=%.2fs",
            serial_port or "auto-detect",
            time.monotonic() - started_at,
        )
        self._receive_handler = self._on_receive
        pub.subscribe(self._receive_handler, "meshtastic.receive")
        self._receive_worker.start()
        logger.debug("started Meshtastic receive worker callsign=%s", self.callsign)

    def _on_receive(self, packet, interface):
        self._receive_packets.put((packet, interface))

    def _process_received_packets(self) -> None:
        while not self._stop_receive_worker.is_set():
            try:
                packet, interface = self._receive_packets.get(timeout=0.5)
            except Empty:
                continue

            try:
                on_receive(
                    packet,
                    interface,
                    callsign=self.callsign,
                    accept_broadcast=self.accept_broadcast,
                )
            except Exception as exc:
                logger.exception("failed to process queued Meshtastic receive packet: %s", exc)

    @staticmethod
    def _degrees_to_int(value: float) -> int:
        return int(round(value * 1e7))

    @staticmethod
    def _serialize_packet(packet: atak_pb2.TAKPacket) -> bytes:
        payload = packet.SerializeToString()
        if len(payload) > MAX_MESHTASTIC_DATA_BYTES:
            raise ValueError(
                f"TAK protobuf is {len(payload)} bytes; "
                f"max is {MAX_MESHTASTIC_DATA_BYTES}"
            )

        parsed = atak_pb2.TAKPacket()
        parsed.ParseFromString(payload)
        return payload

    def _send_packet(self, packet: atak_pb2.TAKPacket) -> bytes:
        payload = self._serialize_packet(packet)
        wait_started_at = time.monotonic()
        if self._send_lock.locked():
            logger.info(
                "Meshtastic send waiting for in-flight send devPath=%s payload_bytes=%s",
                self.serial_port or "auto-detect",
                len(payload),
            )
        with self._send_lock:
            wait_elapsed = time.monotonic() - wait_started_at
            send_started_at = time.monotonic()
            logger.debug(
                "sending Meshtastic ATAK_PLUGIN payload bytes=%s wait_elapsed=%.2fs",
                len(payload),
                wait_elapsed,
            )
            try:
                self.interface.sendData(
                    payload,
                    portNum=portnums_pb2.PortNum.ATAK_PLUGIN,
                    wantAck=False,
                    wantResponse=False,
                )
            except Exception as exc:
                logger.error(
                    "Meshtastic ATAK_PLUGIN send failed devPath=%s payload_bytes=%s wait_elapsed=%.2fs send_elapsed=%.2fs error=%s",
                    self.serial_port or "auto-detect",
                    len(payload),
                    wait_elapsed,
                    time.monotonic() - send_started_at,
                    exc,
                    exc_info=True,
                )
                raise
            send_elapsed = time.monotonic() - send_started_at
            log = logger.warning if send_elapsed >= 5 else logger.debug
            log(
                "Meshtastic ATAK_PLUGIN send completed devPath=%s payload_bytes=%s wait_elapsed=%.2fs send_elapsed=%.2fs",
                self.serial_port or "auto-detect",
                len(payload),
                wait_elapsed,
                send_elapsed,
            )
        return payload

    def build_chat_packet(
        self,
        message: str,
        to: str = "All Chat Rooms",
        to_callsign: str = "All Chat Rooms",
        sender_callsign: str = "BRIDGE",
    ) -> atak_pb2.TAKPacket:
        """Build a compact ATAK GeoChat protobuf packet."""
        packet = atak_pb2.TAKPacket()
        packet.is_compressed = False
        packet.contact.callsign = sender_callsign
        packet.chat.message = message
        packet.chat.to = to
        packet.chat.to_callsign = to_callsign
        return packet

    def send_chat_message(
        self,
        message: str,
        to: str = "All Chat Rooms",
        to_callsign: str = "All Chat Rooms",
        sender_callsign: str = "BRIDGE",
    ) -> bytes:
        """Send a compact ATAK GeoChat message.

        Returns the serialized protobuf bytes sent to Meshtastic.
        """
        if not message:
            raise ValueError("message is required")

        packet = self.build_chat_packet(
            message=message,
            to=to,
            to_callsign=to_callsign,
            sender_callsign=sender_callsign,
        )
        return self._send_packet(packet)

    def build_track_packet(
        self,
        callsign: str,
        lat: float,
        lon: float,
        alt: float,
        speed_ms: float = 0.0,
        course_deg: float = 0.0,
        battery: int = 100,
        team: str | int = "Cyan",
        role: str | int = "TeamMember",
    ) -> atak_pb2.TAKPacket:
        """Build a compact ATAK PLI/location protobuf packet."""
        packet = atak_pb2.TAKPacket()
        packet.is_compressed = False
        packet.contact.callsign = callsign
        packet.contact.device_callsign = callsign
        packet.group.team = team_value(team)
        packet.group.role = role_value(role)
        packet.status.battery = battery
        packet.pli.latitude_i = self._degrees_to_int(lat)
        packet.pli.longitude_i = self._degrees_to_int(lon)
        packet.pli.altitude = int(round(alt))
        packet.pli.speed = max(0, int(round(speed_ms)))
        packet.pli.course = int(round(course_deg)) % 360
        return packet

    def send_location_info(
        self,
        callsign: str,
        lat: float,
        lon: float,
        alt: float,
        speed_ms: float = 0.0,
        course_deg: float = 0.0,
        battery: int = 100,
        team: str | int = "Cyan",
        role: str | int = "TeamMember",
    ) -> bytes:
        """Send compact ATAK PLI/location information.

        Returns the serialized protobuf bytes sent to Meshtastic.
        """
        if not callsign:
            raise ValueError("callsign is required")

        packet = self.build_track_packet(
            callsign=callsign,
            lat=lat,
            lon=lon,
            alt=alt,
            speed_ms=speed_ms,
            course_deg=course_deg,
            battery=battery,
            team=team,
            role=role,
        )
        return self._send_packet(packet)

    def close(self) -> None:
        """Close the Meshtastic serial interface."""
        wait_started_at = time.monotonic()
        if self._send_lock.locked():
            logger.info(
                "waiting for in-flight Meshtastic send before close devPath=%s",
                self.serial_port or "auto-detect",
            )
        logger.debug(
            "closing Meshtastic serial interface devPath=%s",
            self.serial_port or "auto-detect",
        )
        pub.unsubscribe(self._receive_handler, "meshtastic.receive")
        self._stop_receive_worker.set()
        self._receive_worker.join(timeout=2)
        with self._send_lock:
            wait_elapsed = time.monotonic() - wait_started_at
            if wait_elapsed >= 1:
                logger.warning(
                    "in-flight Meshtastic send completed before close devPath=%s wait_elapsed=%.2fs",
                    self.serial_port or "auto-detect",
                    wait_elapsed,
                )
            self.interface.close()
        logger.debug(
            "closed Meshtastic serial interface devPath=%s",
            self.serial_port or "auto-detect",
        )

    # Compatibility aliases for the requested function names.
    SendChatMessage = send_chat_message
    SendLocationInfo = send_location_info
