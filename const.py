"""Constants for the ESP32 Bulb Relay integration."""

DOMAIN = "esp32_bulb_relay"

# Configuration keys
CONF_ESP32_HOST = "esp32_host"
CONF_BULBS = "bulbs"
CONF_ESP32_HOSTS = "esp32_hosts"

# Default values
DEFAULT_PORT = 80
DEFAULT_TIMEOUT = 10

# API Endpoints
API_BULBS = "/bulbs"
API_BULB_ON = "/bulb/{name}/on"
API_BULB_OFF = "/bulb/{name}/off"
API_BULB_BRIGHTNESS = "/bulb/{name}/brightness/{value}"
API_BULB_RGB = "/bulb/{name}/rgb/r={r}&g={g}&b={b}"
API_BULB_TEMPERATURE = "/bulb/{name}/temperature/{value}"
API_BULB_CONNECT = "/bulb/{name}/connect"
API_BULB_DISCONNECT = "/bulb/{name}/disconnect"

# Color temperature range (in Kelvin)
MIN_COLOR_TEMP_KELVIN = 2000
MAX_COLOR_TEMP_KELVIN = 9000

# Brightness range
MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 100

# Update interval in seconds
UPDATE_INTERVAL = 30

# Minimum interval between commands to ESP32 (in seconds)
MIN_COMMAND_INTERVAL = 0.5  # 500ms

# Services
SERVICE_CONNECT_BULB = "connect_bulb"
SERVICE_DISCONNECT_BULB = "disconnect_bulb"
SERVICE_REFRESH_BULBS = "refresh_bulbs"

# Attributes
ATTR_ESP32_HOST = "esp32_host"
ATTR_BULB_NAME = "bulb_name"
