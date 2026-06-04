# MeshtasticBridge

Dockerized REST client for sending Meshtastic ATAK Plugin packets.

Meshtastic radio access is handled through the local `MeshtasticClient.py` wrapper class, which wraps the Meshtastic Python library.

The service exposes five REST endpoints and one websocket endpoint:

- `POST /chat` sends a TAK GeoChat message.
- `POST /track` sends a TAK PLI position update, matching the `run_track()` packet format in `simple_msg_chat_track_working.py`.
- `GET /config` returns the current bridge config.
- `POST /config` updates the bridge config and reconnects Meshtastic when the config changes.
- `POST /restart` exits with code `1` so Docker can restart the app.
- `POST /shutdown` exits with code `0` so Docker `on-failure` policies do not restart the app.
- `WS /ws/chat` streams received `ATAK_PLUGIN` and `TEXT_MESSAGE_APP` chat messages as JSON.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export APP_PORT=8080
uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT"
```

Or use the generated helper:

```bash
./scripts/run-local.sh
```

## Run with Docker

Set the serial device for your Meshtastic radio, then build and start:

```bash
export MESHTASTIC_DEVICE=/dev/ttyUSB0
export MESHTASTIC_CALLSIGN=BRIDGE
export MESHTASTIC_ACCEPT_BROADCAST=true
export MESHTASTIC_CONNECT_ON_START=true
export MESHTASTIC_CONNECT_TIMEOUT=300
export LOG_LEVEL=INFO
export APP_PORT=8080
docker compose up --build
```

Or use the generated helpers:

```bash
./scripts/docker-compose-up.sh
./scripts/docker-run.sh
./scripts/docker-stop.sh
./scripts/docker-logs.sh
```

`DockerAppStart.sh` is kept as a compatibility wrapper for `./scripts/docker-compose-up.sh`.
All helper defaults live in `./scripts/env.sh`; export values before running a script to override them.
For manual `docker run`, set `MESHTASTIC_DEVICE_IN_HOST` and `MESHTASTIC_DEVICE_IN_DOCKER` when the host and container serial paths differ.

`docker-compose.yml` uses `restart: on-failure`, so `/restart` triggers a container restart and `/shutdown` stops cleanly.

Equivalent `docker run` example:

```bash
docker build -t meshtasticbridge .
docker run --name meshtasticbridge \
  --restart on-failure \
  --device "$MESHTASTIC_DEVICE:$MESHTASTIC_DEVICE" \
  -p "${APP_PORT:-8080}:${APP_PORT:-8080}" \
  -e APP_PORT="${APP_PORT:-8080}" \
  -e MESHTASTIC_DEVICE="$MESHTASTIC_DEVICE" \
  -e MESHTASTIC_CALLSIGN="${MESHTASTIC_CALLSIGN:-BRIDGE}" \
  -e MESHTASTIC_ACCEPT_BROADCAST="${MESHTASTIC_ACCEPT_BROADCAST:-true}" \
  -e MESHTASTIC_CONNECT_ON_START="${MESHTASTIC_CONNECT_ON_START:-true}" \
  -e MESHTASTIC_CONNECT_TIMEOUT="${MESHTASTIC_CONNECT_TIMEOUT:-300}" \
  -e LOG_LEVEL="${LOG_LEVEL:-INFO}" \
  meshtasticbridge
```

On macOS Docker Desktop cannot directly pass most host serial devices into Linux containers.
Run the service directly on macOS, or expose the radio over TCP/USB from a Linux host.

## Configuration and logging

The bridge is configured with environment variables:

- `APP_PORT` sets the HTTP port. Default: `8080`.
- `MESHTASTIC_DEVICE` sets the serial device path. Default: `/dev/ttyUSB0` in Docker, auto-detect when unset locally.
- `MESHTASTIC_CALLSIGN` sets the local callsign used for sends and receive filtering. Default: `BRIDGE`.
- `MESHTASTIC_ACCEPT_BROADCAST` allows common broadcast/group receive targets. Default: `true`.
- `MESHTASTIC_CONNECT_ON_START` opens the Meshtastic radio during app startup. Default: `true`.
- `MESHTASTIC_CONNECT_TIMEOUT` controls the Meshtastic serial open timeout in seconds. Default: `300`.
- `LOG_LEVEL` controls Python logging. Default: `INFO`; use `DEBUG` for REST, serial connection, and radio send traces.

Connection behavior:

- When `MESHTASTIC_CONNECT_ON_START=true`, startup attempts to connect to the Meshtastic unit immediately.
- When `MESHTASTIC_CONNECT_ON_START=false`, the first `/chat`, `/track`, or enabled `/config` request opens the connection lazily.
- If the Meshtastic unit cannot be opened, the service logs an error with the serial device, callsign, elapsed time, timeout context, and traceback.
- REST requests that cannot connect or send return `503` with the Meshtastic error in the response detail.
- `/health` and `/config` include `radio_connected` and `radio_error` so clients can see the current radio state.

With `LOG_LEVEL=DEBUG`, logs include REST send requests, lazy connection attempts, serial open timing, successful payload sends, radio send wait/send timing, and receive filtering details. Slow REST sends or radio writes at or above five seconds are logged at `WARNING`; queued sends and close/reconfigure waits are logged at `INFO`.

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
    "battery": 100,
    "team": "Green",
    "role": "TeamMember"
  }'
```

`/track` accepts either `latitude`/`longitude`/`altitude` or `lat`/`lon`/`alt`.
It also accepts `color` as an alias for `team`; values such as `cyan`, `green`,
`Dark Blue`, and `TeamMember` are normalized to the TAK protobuf enum names.

Get the active bridge config:

```bash
curl http://localhost:8080/config
```

Update the bridge config:

```bash
curl -X POST http://localhost:8080/config \
  -H 'Content-Type: application/json' \
  -d '{
    "callsign": "SNAP",
    "serialport": "/dev/ttyUSB0",
    "enabled": true
  }'
```

If the new config differs from the active config, the service closes the current Meshtastic connection and reopens it with the new serial port and callsign. The default state is `enabled: true`, so the service starts with the default Meshtastic device behavior used by the previous implementation. Set `enabled: false` to close the radio connection and keep the bridge idle until a later `/config` request enables it again.

Restart through Docker:

```bash
curl -X POST http://localhost:8080/restart
```

The process exits with code `1`, which restarts the container when it was launched with `--restart on-failure` or `restart: on-failure`.

Shutdown without Docker restart:

```bash
curl -X POST http://localhost:8080/shutdown
```

The process exits with code `0`, so an `on-failure` restart policy leaves the container stopped.

## Websocket receive stream

Received `ATAK_PLUGIN` and `TEXT_MESSAGE_APP` chat packets are accepted only when addressed to `MESHTASTIC_CALLSIGN`.
When `MESHTASTIC_ACCEPT_BROADCAST=true`, common broadcast/group targets such as `All Chat Rooms` are also accepted.
The radio interface opens on application startup by default so receive subscriptions are active before the first REST send.
Set `MESHTASTIC_CONNECT_ON_START=false` to keep the older lazy-connect behavior.
Receive logs include Meshtastic packet `from`, `fromId`, `to`, `toId`, decoded destination, parsed ATAK chat recipient fields, and the local target ids used for filtering.

Connect one or more websocket clients:

```bash
websocat ws://localhost:8080/ws/chat
```

Each received chat message is sent to every connected websocket client:

```json
{
  "source_port": "ATAK_PLUGIN",
  "message": "hello from radio",
  "sender": "SENDER",
  "recipient": "All Chat Rooms",
  "to_callsign": "All Chat Rooms",
  "from_id": "!12345678",
  "to_id": "^all",
  "received_at": "2026-05-19T12:00:00+00:00"
}
```
