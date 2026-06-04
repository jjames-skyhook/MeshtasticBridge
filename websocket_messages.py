from __future__ import annotations

import asyncio
from contextlib import suppress
from queue import Empty, Queue

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.received_messages import ReceivedChatMessage, received_chat_messages


router = APIRouter()


class ChatWebSocketBroadcaster:
    def __init__(self, message_queue: Queue[ReceivedChatMessage]):
        self.message_queue = message_queue
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._broadcast_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()

        for websocket in clients:
            with suppress(Exception):
                await websocket.close()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def _broadcast_loop(self) -> None:
        while True:
            message = await asyncio.to_thread(self._get_next_message)
            if message is None:
                continue
            await self.broadcast(message)

    def _get_next_message(self) -> ReceivedChatMessage | None:
        try:
            return self.message_queue.get(timeout=0.5)
        except Empty:
            return None

    async def broadcast(self, message: ReceivedChatMessage) -> None:
        async with self._lock:
            clients = list(self._clients)

        disconnected: list[WebSocket] = []
        payload = message.to_json()
        for websocket in clients:
            try:
                await websocket.send_json(payload)
            except Exception:
                disconnected.append(websocket)

        if disconnected:
            async with self._lock:
                for websocket in disconnected:
                    self._clients.discard(websocket)


broadcaster = ChatWebSocketBroadcaster(received_chat_messages)


@router.websocket("/ws/chat")
async def chat_messages(websocket: WebSocket) -> None:
    await broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await broadcaster.disconnect(websocket)
