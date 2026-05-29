import logging
import os
import sys
import threading
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.meshtastic_client import MeshtasticClient, MAX_MESHTASTIC_DATA_BYTES
from app.websocket_messages import broadcaster, router as websocket_router


DEFAULT_CALLSIGN = os.getenv("MESHTASTIC_CALLSIGN", "BRIDGE")
MESHTASTIC_DEVICE = os.getenv("MESHTASTIC_DEVICE") or None
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ACCEPT_BROADCAST_MESSAGES = os.getenv("MESHTASTIC_ACCEPT_BROADCAST", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CONNECT_ON_START = os.getenv("MESHTASTIC_CONNECT_ON_START", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="MeshtasticBridge", version="0.1.0")
app.include_router(websocket_router)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=180)
    to: str = Field(default="All Chat Rooms", min_length=1, max_length=80)
    to_callsign: str = Field(default="All Chat Rooms", min_length=1, max_length=80)
    sender_callsign: str = Field(default=DEFAULT_CALLSIGN, min_length=1, max_length=40)


class TrackRequest(BaseModel):
    callsign: str = Field(default="DRONE-1", min_length=1, max_length=40)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    altitude: float = Field(default=0)
    speed: float = Field(default=0, ge=0, description="Speed in meters per second")
    course: float = Field(default=0, ge=0, lt=360, description="Course in degrees")
    battery: int = Field(default=100, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def accept_short_coordinate_names(cls, data: Any) -> Any:
        """Allow REST callers to send lat/lon/alt aliases for track coordinates."""
        if isinstance(data, dict):
            normalized = dict(data)
            if "latitude" not in normalized and "lat" in normalized:
                normalized["latitude"] = normalized["lat"]
            if "longitude" not in normalized and "lon" in normalized:
                normalized["longitude"] = normalized["lon"]
            if "altitude" not in normalized and "alt" in normalized:
                normalized["altitude"] = normalized["alt"]
            return normalized
        return data

    @model_validator(mode="after")
    def require_coordinates(self) -> "TrackRequest":
        """Reject track requests that do not include a complete coordinate pair."""
        if self.latitude is None or self.longitude is None:
            raise ValueError("latitude/longitude are required; lat/lon aliases are also accepted")
        return self


class BridgeConfig(BaseModel):
    callsign: str = Field(default=DEFAULT_CALLSIGN, min_length=1, max_length=40)
    serialport: str | None = Field(default=MESHTASTIC_DEVICE, max_length=255)
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_config_aliases(cls, data: Any) -> Any:
        """Accept common serial/enabled aliases while returning one stable shape."""
        if isinstance(data, dict):
            normalized = dict(data)
            if "serialport" not in normalized:
                if "serial_port" in normalized:
                    normalized["serialport"] = normalized["serial_port"]
                elif "device" in normalized:
                    normalized["serialport"] = normalized["device"]
            if "enabled" not in normalized and "enabeled" in normalized:
                normalized["enabled"] = normalized["enabeled"]
            if normalized.get("serialport") == "":
                normalized["serialport"] = None
            return normalized
        return data


class SendResponse(BaseModel):
    ok: bool
    port: str
    payload_bytes: int
    message: str


class BridgeConfigResponse(BridgeConfig):
    radio_connected: bool
    radio_error: str | None
    changed: bool = False


class ExitResponse(BaseModel):
    ok: bool
    exit_code: int
    message: str


class MeshtasticTakClient:
    def __init__(
        self,
        device: str | None = None,
        callsign: str = DEFAULT_CALLSIGN,
        accept_broadcast: bool = ACCEPT_BROADCAST_MESSAGES,
        enabled: bool = True,
    ):
        """Create a Meshtastic client wrapper for an optional serial device path."""
        self.device = device
        self.callsign = callsign
        self.accept_broadcast = accept_broadcast
        self.enabled = enabled
        self._client = None
        self._last_error = None
        self._lock = threading.Lock()

    def meshtastic_client(self) -> MeshtasticClient:
        """Open the MeshtasticClient wrapper on first use and reuse it for later sends."""
        with self._lock:
            if self._client is None:
                try:
                    self._client = MeshtasticClient(
                        serial_port=self.device,
                        callsign=self.callsign,
                        accept_broadcast=self.accept_broadcast,
                    )
                except Exception as exc:
                    self._last_error = str(exc)
                    raise
                self._last_error = None
            return self._client

    def connect(self) -> None:
        """Open the Meshtastic interface so receive subscriptions are active."""
        if not self.enabled:
            return
        self.meshtastic_client()

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def close(self) -> None:
        """Close the MeshtasticClient wrapper if it has been opened."""
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def config(self) -> BridgeConfig:
        """Return the active bridge configuration."""
        with self._lock:
            return BridgeConfig(
                callsign=self.callsign,
                serialport=self.device,
                enabled=self.enabled,
            )

    def configure(self, config: BridgeConfig, connect_when_enabled: bool = True) -> bool:
        """Apply a runtime config and reconnect when serial/callsign settings change."""
        with self._lock:
            changed = (
                self.callsign != config.callsign
                or self.device != config.serialport
                or self.enabled != config.enabled
            )
            if not changed:
                should_connect = config.enabled and connect_when_enabled and self._client is None
            else:
                if self._client is not None:
                    self._client.close()
                    self._client = None
                self.callsign = config.callsign
                self.device = config.serialport
                self.enabled = config.enabled
                self._last_error = None
                should_connect = config.enabled and connect_when_enabled

        if should_connect:
            self.connect()
        return changed

    def send_chat(self, request: ChatRequest) -> SendResponse:
        """Send a REST chat request through the MeshtasticClient wrapper."""
        if not self.enabled:
            raise HTTPException(status_code=503, detail="bridge is disabled")
        try:
            payload = self.meshtastic_client().send_chat_message(
                message=request.message,
                to=request.to,
                to_callsign=request.to_callsign,
                sender_callsign=request.sender_callsign,
            )
        except Exception as exc:
            raise _send_error("chat", exc) from exc

        return SendResponse(
            ok=True,
            port="ATAK_PLUGIN",
            payload_bytes=len(payload),
            message=request.message,
        )

    def send_track(self, request: TrackRequest) -> SendResponse:
        """Send a REST track request through the MeshtasticClient wrapper."""
        if not self.enabled:
            raise HTTPException(status_code=503, detail="bridge is disabled")
        try:
            payload = self.meshtastic_client().send_location_info(
                callsign=request.callsign,
                lat=request.latitude,
                lon=request.longitude,
                alt=request.altitude,
                speed_ms=request.speed,
                course_deg=request.course,
                battery=request.battery,
            )
        except Exception as exc:
            raise _send_error(f"track update for {request.callsign}", exc) from exc

        return SendResponse(
            ok=True,
            port="ATAK_PLUGIN",
            payload_bytes=len(payload),
            message=f"sent track update for {request.callsign}",
        )


client = MeshtasticTakClient(MESHTASTIC_DEVICE, DEFAULT_CALLSIGN, enabled=True)


def _send_error(description: str, exc: Exception) -> HTTPException:
    """Map wrapper send failures to REST errors with payload-size failures as 400s."""
    if isinstance(exc, ValueError) and f"max is {MAX_MESHTASTIC_DATA_BYTES}" in str(exc):
        return HTTPException(status_code=400, detail=f"{description} payload is too large: {exc}")
    return HTTPException(status_code=503, detail=f"failed to send {description}: {exc}")


def _config_response(changed: bool = False) -> BridgeConfigResponse:
    config = client.config()
    return BridgeConfigResponse(
        callsign=config.callsign,
        serialport=config.serialport,
        enabled=config.enabled,
        radio_connected=client.connected,
        radio_error=client.last_error,
        changed=changed,
    )


def _exit_process(exit_code: int) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


@app.get("/health")
def health() -> dict[str, str | bool | None]:
    """Return service health and the configured Meshtastic device path."""
    config = client.config()
    return {
        "status": "ok",
        "device": config.serialport,
        "callsign": config.callsign,
        "enabled": config.enabled,
        "accept_broadcast": ACCEPT_BROADCAST_MESSAGES,
        "connect_on_start": CONNECT_ON_START,
        "radio_connected": client.connected,
        "radio_error": client.last_error,
    }


@app.get("/config", response_model=BridgeConfigResponse)
def get_config() -> BridgeConfigResponse:
    """Return the active bridge config."""
    return _config_response()


@app.post("/config", response_model=BridgeConfigResponse)
def config(request: BridgeConfig) -> BridgeConfigResponse:
    """Apply bridge config and reinitialize Meshtastic when settings differ."""
    try:
        changed = client.configure(request)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"failed to apply config: {exc}") from exc
    return _config_response(changed=changed)


@app.post("/restart", response_model=ExitResponse)
def restart(background_tasks: BackgroundTasks) -> ExitResponse:
    """Exit with a non-zero code so Docker on-failure policies restart the app."""
    background_tasks.add_task(_exit_process, 1)
    return ExitResponse(ok=True, exit_code=1, message="restarting")


@app.post("/shutdown", response_model=ExitResponse)
def shutdown_app(background_tasks: BackgroundTasks) -> ExitResponse:
    """Exit with code 0 so Docker on-failure policies do not restart the app."""
    background_tasks.add_task(_exit_process, 0)
    return ExitResponse(ok=True, exit_code=0, message="shutting down")


@app.post("/chat", response_model=SendResponse)
def chat(request: ChatRequest) -> SendResponse:
    """Handle REST chat input and transmit it as a Meshtastic TAK GeoChat packet."""
    return client.send_chat(request)


@app.post("/track", response_model=SendResponse)
def track(request: TrackRequest) -> SendResponse:
    """Handle REST track input and transmit it as a Meshtastic TAK PLI packet."""
    return client.send_track(request)


@app.on_event("startup")
async def startup() -> None:
    """Start websocket fanout for received chat messages."""
    broadcaster.start()
    if CONNECT_ON_START:
        try:
            client.connect()
        except Exception:
            pass


@app.on_event("shutdown")
async def shutdown() -> None:
    """Release the Meshtastic serial interface during application shutdown."""
    await broadcaster.stop()
    client.close()
