"""Constants for the ESP32 Bulb Relay integration."""

DOMAIN = "esp32_bulb_relay"

# Configuration keys
CONF_SERIAL_PORT = "serial_port"
CONF_BULBS = "bulbs"
CONF_SERIAL_PORTS = "serial_ports"

# Default values
DEFAULT_BAUD_RATE = 115200
DEFAULT_TIMEOUT = 30  # Seconds - ESP32 may take several seconds to respond

# API Commands (same as HTTP endpoints, without the leading slash for serial)
CMD_BULBS = "/bulbs"
CMD_BULB_ON = "/bulb/{name}/on"
CMD_BULB_OFF = "/bulb/{name}/off"
CMD_BULB_BRIGHTNESS = "/bulb/{name}/brightness/{value}"
CMD_BULB_RGB = "/bulb/{name}/rgb/r={r}&g={g}&b={b}"
CMD_BULB_TEMPERATURE = "/bulb/{name}/temperature/{value}"
CMD_BULB_CONNECT = "/bulb/{name}/connect"
CMD_BULB_DISCONNECT = "/bulb/{name}/disconnect"

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
ATTR_SERIAL_PORT = "serial_port"
ATTR_BULB_NAME = "bulb_name"
