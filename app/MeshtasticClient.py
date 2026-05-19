"""Small Meshtastic ATAK client wrapper.

License note:
    This module depends on the Meshtastic Python library, which is licensed
    under GPL-3.0-only. Review GPL obligations before distributing software,
    appliances, containers, or servers that bundle this dependency.
"""

from __future__ import annotations

from typing import Optional

import meshtastic.serial_interface
import meshtastic.protobuf.atak_pb2 as atak_pb2
import meshtastic.protobuf.portnums_pb2 as portnums_pb2


MAX_MESHTASTIC_DATA_BYTES = 239


class MeshtasticClient:
    """Send compact ATAK plugin protobuf messages over a Meshtastic radio."""

    def __init__(self, serial_port: Optional[str] = None, timeout: int = 300):
        """Connect to a Meshtastic device.

        Args:
            serial_port: Serial device path, such as ``/dev/cu.usbserial-0001``.
                When omitted, the Meshtastic library auto-detects a device.
            timeout: Serial connection timeout in seconds.
        """
        self.serial_port = serial_port
        self.interface = meshtastic.serial_interface.SerialInterface(
            devPath=serial_port,
            timeout=timeout,
        )

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
        self.interface.sendData(
            payload,
            portNum=portnums_pb2.PortNum.ATAK_PLUGIN,
            wantAck=False,
            wantResponse=False,
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
    ) -> atak_pb2.TAKPacket:
        """Build a compact ATAK PLI/location protobuf packet."""
        packet = atak_pb2.TAKPacket()
        packet.is_compressed = False
        packet.contact.callsign = callsign
        packet.contact.device_callsign = callsign
        packet.group.team = atak_pb2.Cyan
        packet.group.role = atak_pb2.TeamMember
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
        )
        return self._send_packet(packet)

    def close(self) -> None:
        """Close the Meshtastic serial interface."""
        self.interface.close()

    # Compatibility aliases for the requested function names.
    SendChatMessage = send_chat_message
    SendLocationInfo = send_location_info
