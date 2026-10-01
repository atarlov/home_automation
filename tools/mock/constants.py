"""LilyGO T-Display-S3 AMOLED facts from the product page (variant 43532279939253).

https://lilygo.cc/en-de/products/t-display-s3-amoled?variant=43532279939253

Resolution stated as 240 X RGB X 536(H) on a 1.91" RM67162 AMOLED.
MCU: ESP32-S3R8. Capacitive-touch variant is what Assen has.
"""

PRODUCT_NAME = "T-Display S3 AMOLED"
MCU = "ESP32-S3R8"
PANEL_W = 240
PANEL_H = 536
DRIVER_IC = "RM67162"
DIAGONAL_IN = 1.91

# Gateway publishes heartbeat every 10s; firmware treats a gap as offline.
HEARTBEAT_STALE_SEC = 30

MOCK_BIND = "127.0.0.1"
MOCK_PORT = 18765

PHONE_MAC = "aa:bb:cc:dd:ee:10"
UNKNOWN_MAC = "de:ad:be:ef:00:01"
AP_MAC = "00:11:22:33:44:55"
