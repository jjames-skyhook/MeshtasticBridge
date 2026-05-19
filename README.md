# MeshtasticBridge

Dockerized REST client for sending Meshtastic ATAK Plugin packets.

Meshtastic radio access is handled through the local `MeshtasticClient.py` wrapper class, which wraps the Meshtastic Python library.

The service exposes two REST endpoints:

- `POST /chat` sends a TAK GeoChat message.
- `POST /track` sends a TAK PLI position update.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export APP_PORT=8080
uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT"
```

## Run with Docker

Set the serial device for your Meshtastic radio, then build and start:

```bash
export MESHTASTIC_DEVICE=/dev/ttyUSB0
export MESHTASTIC_CALLSIGN=BRIDGE
export APP_PORT=8080
docker compose up --build
```

On macOS Docker Desktop cannot directly pass most host serial devices into Linux containers.
Run the service directly on macOS, or expose the radio over TCP/USB from a Linux host.

## REST examples

Send a chat message:

```bash
curl -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "hello from REST",
    "sender_callsign": "BRIDGE",
    "to": "All Chat Rooms",
    "to_callsign": "All Chat Rooms"
  }'
```

Send a track update:

```bash
curl -X POST http://localhost:8080/track \
  -H 'Content-Type: application/json' \
  -d '{
    "callsign": "DRONE-1",
    "lat": 40.1941,
    "lon": -75.4051,
    "alt": 50,
    "speed": 5,
    "course": 90,
    "battery": 100
  }'
```

`/track` accepts either `latitude`/`longitude`/`altitude` or `lat`/`lon`/`alt`.
