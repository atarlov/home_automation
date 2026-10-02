# Raspberry Pi and a Wi-Fi display

Run the house agent on a Raspberry Pi that stays on the LAN. Power the LilyGO T-Display-S3 AMOLED from a charger and let it join the same Wi-Fi. The laptop is only for flashing the panel over USB.

The Pi does not run the model. It calls hosted NVIDIA NIM, the same way any other host does. UniFi and Shelly stay on the LAN.

Today the panel speaks only USB serial. The broker, the passwords, and the topics below already exist. The work later is a Wi-Fi build of `firmware/display` and an MQTT link in the agent.

## What you need

- Raspberry Pi 4 or 5, 2 GB or more, Raspberry Pi OS 64-bit, Ethernet to the UniFi network
- The Pi's LAN address reserved in UniFi, so the panel always finds the broker
- A USB charger for the T-Display-S3, not power from the Pi
- Wi-Fi name and password for a 2.4 GHz network the ESP32-S3 can join
- `.env` already filled in: UniFi keys, `NVIDIA_API_KEY`, and the Shelly rows in SQLite

## How the messages move

The panel is MQTT user `esp32`. The agent publishes what the screen should draw. A tap comes back as a signed decision. The gateway is the only process that acts on it.

| Topic | Direction | Payload |
|---|---|---|
| `netwatch/status` | agent → panel | `{"lines": ["..."]}` idle text. Empty lines mean the waiting face |
| `netwatch/notify` | agent → panel | greeting lines, no buttons |
| `netwatch/proposal` | agent → panel | `{"id", "lines", "button_a", "button_b"}` |
| `netwatch/resolved` | agent → panel | `{"id", "status"}` clears a question |
| `netwatch/decision` | panel → gateway | `{"id", "decision", "ts", "hmac"}` |
| `netwatch/heartbeat` | gateway → panel | `{"from": "gateway", "ts"}` |

`decision` is `approve` or `deny`. The HMAC is SHA-256 over `id|decision|ts` with `NETWATCH_HMAC_SECRET`, the same bytes `tools/fake_button.py` uses. Left half of the glass approves. Right half denies.

## 1. Pi

Clone the repo, install Docker and Python, copy `.env`, and create the database.

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-v2 python3-venv mosquitto-clients
git clone https://github.com/atarlov/home_automation.git
cd home_automation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

In `.env` on the Pi:

```bash
MQTT_HOST=127.0.0.1
NETWATCH_DB=/home/pi/home_automation/netwatch.db
```

Keep the UniFi URL, both UniFi keys, and `NVIDIA_API_KEY`. Create the broker users and the database:

```bash
./tools/bootstrap.sh
make init-db
docker compose up -d
```

Enroll the phone and the Shelly rows with the SQL in the README. Then check the broker from the Pi:

```bash
mosquitto_sub -h 127.0.0.1 -u esp32 -P "$MQTT_PASS_ESP32" -t 'netwatch/#' -v
```

Done when that command stays connected and a published `netwatch/status` shows up.

## 2. Panel firmware

Flash this once from the laptop, over USB, with the board plugged into the laptop. After it joins Wi-Fi, day-to-day power is the charger.

Add a Wi-Fi build of `firmware/display` that keeps the current landscape face and touch, and replaces the USB `SHOW` / `BTN` lines with the topics above.

- Wi-Fi SSID and password, and the Pi's address, live in a header that is not committed. `.gitignore` already ignores secrets. Do not put the password in `platformio.ini`.
- Connect as MQTT user `esp32` with `MQTT_PASS_ESP32`.
- Subscribe to `status`, `notify`, `proposal`, `resolved`, and `heartbeat`.
- Publish `netwatch/decision` with the HMAC.
- On boot, draw the waiting face, then replace it when a retained `netwatch/status` arrives.
- If `heartbeat` is older than 30 seconds, show that the agent is offline. The face returns when heartbeats resume.

Build and flash from the laptop while the board is on USB. Then move it to the charger. On the Pi, publish a greeting:

```bash
mosquitto_pub -h 127.0.0.1 -u api -P "$MQTT_PASS_API" \
  -t netwatch/notify -m '{"lines":["Welcome home","Hall light on"]}'
```

Done when the charger-powered panel shows those lines, and a tap prints a `netwatch/decision` line in the `mosquitto_sub` from step 1.

## 3. Agent on the Pi

`host/agent.py` still opens the USB port. Add an MQTT display link with the same `publish` and `poll` behavior as `host/display_serial.py`, selected with `DISPLAY_TRANSPORT=mqtt`.

- `publish` writes `netwatch/status`, `netwatch/notify`, `netwatch/proposal`, and `netwatch/resolved`.
- `poll` reads `netwatch/decision` and passes it to `handle_button`.
- Run `host/gateway.py` as well, so heartbeats and expiry reach the panel. The agent still calls the gateway in-process for proposals.

Three long-running processes, restarted by systemd:

```bash
.venv/bin/python host/collector.py
.venv/bin/python host/gateway.py
.venv/bin/python host/agent.py
```

`DISPLAY_TRANSPORT=mqtt` belongs in the agent's environment. `MQTT_HOST` stays `127.0.0.1` on the Pi.

Done when `make face-greet` is no longer required: an arrival event on the Pi changes the panel, and a tap changes the proposal row in SQLite.

## 4. Leave the laptop

Unplug the panel from the laptop. Confirm, on charger power only:

- The waiting face is up within a few seconds of power on.
- A retained status or a new greeting appears without USB.
- The hall Shelly marked `comfort_auto` switches on when the phone joins Wi-Fi after a real absence.
- A question can be approved and denied from the glass, and a late tap does nothing.

USB remains the way to flash a new sketch. It is not the way the house talks to the panel.
