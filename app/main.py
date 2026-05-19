import os
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.MeshtasticClient import MeshtasticClient, MAX_MESHTASTIC_DATA_BYTES


DEFAULT_CALLSIGN = os.getenv("MESHTASTIC_CALLSIGN", "BRIDGE")
MESHTASTIC_DEVICE = os.getenv("MESHTASTIC_DEVICE") or None

app = FastAPI(title="MeshtasticBridge", version="0.1.0")


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


class SendResponse(BaseModel):
    ok: bool
    port: str
    payload_bytes: int
    message: str


class MeshtasticTakClient:
    def __init__(self, device: str | None = None):
        """Create a Meshtastic client wrapper for an optional serial device path."""
        self.device = device
        self._client = None
        self._lock = threading.Lock()

    def meshtastic_client(self) -> MeshtasticClient:
        """Open the MeshtasticClient wrapper on first use and reuse it for later sends."""
        with self._lock:
            if self._client is None:
                self._client = MeshtasticClient(serial_port=self.device)
            return self._client

    def close(self) -> None:
        """Close the MeshtasticClient wrapper if it has been opened."""
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def send_chat(self, request: ChatRequest) -> SendResponse:
        """Send a REST chat request through the MeshtasticClient wrapper."""
        try:
            payload = self.meshtastic_client().send_chat_message(
                message=request.message,
                to=request.to,
                to_callsign=request.to_callsign,
                sender_callsign=request.sender_callsign,
            )
        except Exception as exc:
            raise _send_error("CoT chat", exc) from exc

        return SendResponse(
            ok=True,
            port="ATAK_PLUGIN",
            payload_bytes=len(payload),
            message="sent CoT chat",
        )

    def send_track(self, request: TrackRequest) -> SendResponse:
        """Send a REST track request through the MeshtasticClient wrapper."""
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


client = MeshtasticTakClient(MESHTASTIC_DEVICE)


def _send_error(description: str, exc: Exception) -> HTTPException:
    """Map wrapper send failures to REST errors with payload-size failures as 400s."""
    if isinstance(exc, ValueError) and f"max is {MAX_MESHTASTIC_DATA_BYTES}" in str(exc):
        return HTTPException(status_code=400, detail=f"{description} payload is too large: {exc}")
    return HTTPException(status_code=503, detail=f"failed to send {description}: {exc}")


@app.get("/health")
def health() -> dict[str, str | None]:
    """Return service health and the configured Meshtastic device path."""
    return {"status": "ok", "device": MESHTASTIC_DEVICE}


@app.post("/chat", response_model=SendResponse)
def chat(request: ChatRequest) -> SendResponse:
    """Handle REST chat input and transmit it as a Meshtastic TAK GeoChat packet."""
    return client.send_chat(request)


@app.post("/track", response_model=SendResponse)
def track(request: TrackRequest) -> SendResponse:
    """Handle REST track input and transmit it as a Meshtastic TAK PLI packet."""
    return client.send_track(request)


@app.on_event("shutdown")
def shutdown() -> None:
    """Release the Meshtastic serial interface during application shutdown."""
    client.close()
