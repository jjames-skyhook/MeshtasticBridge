import logging
import os
import sys
import threading
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.meshtastic_client import (
    MAX_MESHTASTIC_DATA_BYTES,
    MeshtasticClient,
    role_name,
    team_name,
)
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
MESHTASTIC_CONNECT_TIMEOUT = int(os.getenv("MESHTASTIC_CONNECT_TIMEOUT", "300"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="MeshtasticBridge", version="0.1.0")
app.include_router(websocket_router)
logger = logging.getLogger(__name__)


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
    team: str = Field(default="Cyan", description="ATAK team color, such as Cyan or Green")
    role: str = Field(default="TeamMember", description="ATAK member role")

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
            if "team" not in normalized and "color" in normalized:
                normalized["team"] = normalized["color"]
            return normalized
        return data

    @field_validator("team", mode="before")
    @classmethod
    def normalize_team(cls, value: Any) -> str:
        return team_name(value)

    @field_validator("role", mode="before")
    @classmethod
    def normalize_role(cls, value: Any) -> str:
        return role_name(value)

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
        connect_timeout: int = MESHTASTIC_CONNECT_TIMEOUT,
    ):
        """Create a Meshtastic client wrapper for an optional serial device path."""
        self.device = device
        self.callsign = callsign
        self.accept_broadcast = accept_broadcast
        self.enabled = enabled
        self.connect_timeout = connect_timeout
        self._client = None
        self._last_error = None
        self._lock = threading.Lock()
        self._operation_lock = threading.Lock()

    def meshtastic_client(self) -> MeshtasticClient:
        """Open the MeshtasticClient wrapper on first use and reuse it for later sends."""
        wait_started_at = time.monotonic()
        if self._lock.locked():
            logger.info(
                "waiting for Meshtastic client lock device=%s callsign=%s",
                self.device or "auto-detect",
                self.callsign,
            )
        with self._lock:
            wait_elapsed = time.monotonic() - wait_started_at
            if self._client is None:
                started_at = time.monotonic()
                logger.debug(
                    "opening Meshtastic connection device=%s callsign=%s timeout=%ss wait_elapsed=%.2fs",
                    self.device or "auto-detect",
                    self.callsign,
                    self.connect_timeout,
                    wait_elapsed,
                )
                try:
                    self._client = MeshtasticClient(
                        serial_port=self.device,
                        timeout=self.connect_timeout,
                        callsign=self.callsign,
                        accept_broadcast=self.accept_broadcast,
                    )
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.error(
                        "failed to connect to Meshtastic unit device=%s callsign=%s elapsed=%.2fs error=%s",
                        self.device or "auto-detect",
                        self.callsign,
                        time.monotonic() - started_at,
                        exc,
                        exc_info=True,
                    )
                    raise
                self._last_error = None
                logger.debug(
                    "Meshtastic connection opened device=%s callsign=%s elapsed=%.2fs",
                    self.device or "auto-detect",
                    self.callsign,
                    time.monotonic() - started_at,
                )
            elif wait_elapsed >= 1:
                logger.warning(
                    "Meshtastic client lock wait completed device=%s callsign=%s wait_elapsed=%.2fs",
                    self.device or "auto-detect",
                    self.callsign,
                    wait_elapsed,
                )
            return self._client

    def connect(self) -> None:
        """Open the Meshtastic interface so receive subscriptions are active."""
        if not self.enabled:
            logger.debug("skipping Meshtastic connection because bridge is disabled")
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
        wait_started_at = time.monotonic()
        if self._operation_lock.locked():
            logger.info("waiting for in-flight Meshtastic operation before close")
        with self._operation_lock:
            wait_elapsed = time.monotonic() - wait_started_at
            if wait_elapsed >= 1:
                logger.warning(
                    "in-flight Meshtastic operation completed before close wait_elapsed=%.2fs",
                    wait_elapsed,
                )
            self._close_locked()

    def _close_locked(self) -> None:
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
        wait_started_at = time.monotonic()
        if self._operation_lock.locked():
            logger.info("waiting for in-flight Meshtastic operation before reconfigure")
        with self._operation_lock:
            wait_elapsed = time.monotonic() - wait_started_at
            if wait_elapsed >= 1:
                logger.warning(
                    "in-flight Meshtastic operation completed before reconfigure wait_elapsed=%.2fs",
                    wait_elapsed,
                )
            with self._lock:
                changed = (
                    self.callsign != config.callsign
                    or self.device != config.serialport
                    or self.enabled != config.enabled
                )
                if not changed:
                    should_connect = (
                        config.enabled and connect_when_enabled and self._client is None
                    )
                else:
                    logger.debug(
                        "applying Meshtastic config callsign=%s serialport=%s enabled=%s",
                        config.callsign,
                        config.serialport or "auto-detect",
                        config.enabled,
                    )
                    if self._client is not None:
                        logger.debug("closing existing Meshtastic connection before reconfigure")
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
        started_at = time.monotonic()
        logger.debug(
            "REST chat request sender=%s to=%s to_callsign=%s connected=%s",
            request.sender_callsign,
            request.to,
            request.to_callsign,
            self.connected,
        )
        try:
            if self._operation_lock.locked():
                logger.info(
                    "REST chat request waiting for in-flight Meshtastic operation sender=%s",
                    request.sender_callsign,
                )
            with self._operation_lock:
                payload = self.meshtastic_client().send_chat_message(
                    message=request.message,
                    to=request.to,
                    to_callsign=request.to_callsign,
                    sender_callsign=request.sender_callsign,
                )
        except Exception as exc:
            self._last_error = str(exc)
            logger.error(
                "REST chat request failed sender=%s to=%s to_callsign=%s elapsed=%.2fs error=%s",
                request.sender_callsign,
                request.to,
                request.to_callsign,
                time.monotonic() - started_at,
                exc,
                exc_info=True,
            )
            raise _send_error("chat", exc) from exc

        self._last_error = None
        elapsed = time.monotonic() - started_at
        log = logger.warning if elapsed >= 5 else logger.debug
        log("REST chat request sent payload_bytes=%s elapsed=%.2fs", len(payload), elapsed)
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
        started_at = time.monotonic()
        logger.debug(
            "REST track request callsign=%s lat=%s lon=%s connected=%s",
            request.callsign,
            request.latitude,
            request.longitude,
            self.connected,
        )
        try:
            if self._operation_lock.locked():
                logger.info(
                    "REST track request waiting for in-flight Meshtastic operation callsign=%s",
                    request.callsign,
                )
            with self._operation_lock:
                payload = self.meshtastic_client().send_location_info(
                    callsign=request.callsign,
                    lat=request.latitude,
                    lon=request.longitude,
                    alt=request.altitude,
                    speed_ms=request.speed,
                    course_deg=request.course,
                    battery=request.battery,
                    team=request.team,
                    role=request.role,
                )
        except Exception as exc:
            self._last_error = str(exc)
            logger.error(
                "REST track request failed callsign=%s lat=%s lon=%s elapsed=%.2fs error=%s",
                request.callsign,
                request.latitude,
                request.longitude,
                time.monotonic() - started_at,
                exc,
                exc_info=True,
            )
            raise _send_error(f"track update for {request.callsign}", exc) from exc

        self._last_error = None
        elapsed = time.monotonic() - started_at
        log = logger.warning if elapsed >= 5 else logger.debug
        log(
            "REST track request sent callsign=%s payload_bytes=%s elapsed=%.2fs",
            request.callsign,
            len(payload),
            elapsed,
        )
        return SendResponse(
            ok=True,
            port="ATAK_PLUGIN",
            payload_bytes=len(payload),
            message=f"sent track update for {request.callsign}",
        )


client = MeshtasticTakClient(
    MESHTASTIC_DEVICE,
    DEFAULT_CALLSIGN,
    enabled=True,
    connect_timeout=MESHTASTIC_CONNECT_TIMEOUT,
)


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
def health() -> dict[str, str | bool | int | None]:
    """Return service health and the configured Meshtastic device path."""
    config = client.config()
    return {
        "status": "ok",
        "device": config.serialport,
        "callsign": config.callsign,
        "enabled": config.enabled,
        "accept_broadcast": ACCEPT_BROADCAST_MESSAGES,
        "connect_on_start": CONNECT_ON_START,
        "connect_timeout": MESHTASTIC_CONNECT_TIMEOUT,
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
            logger.debug("startup Meshtastic connection enabled")
            client.connect()
        except Exception as exc:
            logger.error("startup failed to connect to Meshtastic unit: %s", exc, exc_info=True)
    else:
        logger.debug("startup Meshtastic connection disabled by MESHTASTIC_CONNECT_ON_START")


@app.on_event("shutdown")
async def shutdown() -> None:
    """Release the Meshtastic serial interface during application shutdown."""
    await broadcaster.stop()
    client.close()
