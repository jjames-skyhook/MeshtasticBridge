from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from queue import Queue
from typing import Any

import meshtastic.protobuf.atak_pb2 as atak_pb2


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceivedChatMessage:
    source_port: str
    message: str
    sender: str | None
    recipient: str | None
    to_callsign: str | None
    from_id: str | None
    to_id: str | None
    received_at: str

    def to_json(self) -> dict[str, str | None]:
        return asdict(self)


received_chat_messages: Queue[ReceivedChatMessage] = Queue()


def on_receive(
    packet: dict[str, Any],
    interface: Any,
    *,
    callsign: str,
    message_queue: Queue[ReceivedChatMessage] = received_chat_messages,
    accept_broadcast: bool = True,
) -> None:
    port = packet['decoded'].get('portnum')
    message = None
    if port == "ATAK_PLUGIN":
        try:
            tak_packet = atak_pb2.TAKPacket()
            tak_packet.ParseFromString(packet['decoded']['payload'])
            recipient = tak_packet.chat.to or None
            to_callsign = tak_packet.chat.to_callsign or None
            sender = tak_packet.contact.callsign or tak_packet.contact.device_callsign or None
            text = tak_packet.chat.message
            if text:
                message = ReceivedChatMessage(
                    source_port="ATAK_PLUGIN",
                    message=text,
                    sender=sender,
                    recipient=recipient,
                    to_callsign=to_callsign,
                    from_id=_string_or_none(packet.get("fromId") or packet.get("from")),
                    to_id=_string_or_none(packet.get("toId") or packet.get("to")),
                    received_at=_now_iso(),
                )
        except BaseException as e:
            #Ignoring this exception, looking for only chat messages
            #logger.exception("failed to decode ATAK_PLUGIN protobuf: %s", e)
            None
    elif port == 'TEXT_MESSAGE_APP':
        try:
            message = ReceivedChatMessage(
                source_port="TEXT_MESSAGE_APP",
                message=packet['decoded']['payload'].decode('utf-8'),
                sender=_string_or_none(packet.get("fromId") or packet.get("from")),
                recipient=_string_or_none(packet.get("toId") or packet.get("to")),
                to_callsign=None,
                from_id=_string_or_none(packet.get("fromId") or packet.get("from")),
                to_id=_string_or_none(packet.get("toId") or packet.get("to")),
                received_at=_now_iso(),
            )
        except Exception as e:
            logger.exception("failed to decode TEXT_MESSAGE_APP payload: %s", e)

    if message is not None:
        logger.info(
            "queueing received chat source_port=%s from_id=%s to_id=%s sender=%s recipient=%s to_callsign=%s",
            message.source_port,
            message.from_id,
            message.to_id,
            message.sender,
            message.recipient,
            message.to_callsign,
        )
        message_queue.put(message)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
