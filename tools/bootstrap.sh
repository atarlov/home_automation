#!/bin/sh
# Create .env, the Mosquitto password file, and the SQLite schema.
# Re-run after changing MQTT_* passwords in .env.
set -eu
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "wrote .env from .env.example"
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

touch mosquitto/passwd
# The image entrypoint is the broker; call mosquitto_passwd explicitly.
# -c recreates the file, so the first user creates it and the rest append.
docker run --rm -v "$PWD/mosquitto:/mosquitto/config" --entrypoint mosquitto_passwd \
  eclipse-mosquitto:2 -b -c /mosquitto/config/passwd "$MQTT_USER_API" "$MQTT_PASS_API"
docker run --rm -v "$PWD/mosquitto:/mosquitto/config" --entrypoint mosquitto_passwd \
  eclipse-mosquitto:2 -b /mosquitto/config/passwd "$MQTT_USER_GATEWAY" "$MQTT_PASS_GATEWAY"
docker run --rm -v "$PWD/mosquitto:/mosquitto/config" --entrypoint mosquitto_passwd \
  eclipse-mosquitto:2 -b /mosquitto/config/passwd "$MQTT_USER_ESP32" "$MQTT_PASS_ESP32"

python3 host/db.py
echo "broker users written; database initialized at $NETWATCH_DB"
