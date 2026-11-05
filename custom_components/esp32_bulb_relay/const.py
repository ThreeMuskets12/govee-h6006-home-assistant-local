"""Constants for the ESP32 Bulb Relay integration."""

DOMAIN = "esp32_bulb_relay"

# Configuration keys
CONF_SERIAL_PORT = "serial_port"
CONF_SERIAL_PORTS = "serial_ports"  # List of ports to scan
CONF_BULBS = "bulbs"  # Flat list of bulb names (not grouped by port)

# Device registry identifier for a bulb: BULB_DEVICE_PREFIX + bulb name
BULB_DEVICE_PREFIX = "bulb_"

# Default values
DEFAULT_BAUD_RATE = 115200

# Timeouts (in seconds)
# Command timeout - for regular commands to ESP32
# The /bulbs query should be fast, but control commands may need longer
# if the ESP32 is reconnecting to a bulb
DEFAULT_TIMEOUT = 10  # Reduced from 30 - /bulbs should respond quickly

# Longer timeout for commands that involve BLE operations
COMMAND_TIMEOUT = 15  # For on/off/brightness/color commands

# Status poll timeout - shorter since we poll frequently
POLL_TIMEOUT = 5  # For /bulbs status queries

# Consecutive failed polls a port's bulbs stay routed through. A missed poll usually
# means the board is busy; three spans the original firmware's 91.5 s worst-case stall.
MAX_MISSED_SCANS = 3

# Serial stream resync: discard input until it pauses this long, within these limits
RESYNC_QUIET_GAP = 0.2
RESYNC_MAX_TIME = 3.0
RESYNC_MAX_BYTES = 262144
JUNK_SAMPLE_BYTES = 64  # How much of the discarded data to log
# Lines of this much that aren't a reply make a flood even with pauses in it, as a reset
# loop's banners have. It is several times what one ordinary boot prints.
JUNK_LINES_MAX_BYTES = 4096

# Self-recovery. A failed scan takes about 35 s, so three misses fit inside the original
# firmware's 91.5 s stall: a board that is only busy must not reach this count.
RESET_AFTER_MISSED_SCANS = MAX_MISSED_SCANS + 1
# Silence required before a reset, measured from the last reply or port open. Longer than
# that stall and than setup(), which connects up to three bulbs at about 31 s each.
RESET_MIN_SILENCE = 100
RESET_BACKOFF_INITIAL = 90  # Doubles per failed recovery of a port until it answers a scan
RESET_BACKOFF_MAX = 1800

# Coordinated recovery. The boards share a marginal 5 V supply, and boots that overlap can
# keep each other in a reset loop, so they are held in reset and brought up one at a time.
RECOVERY_HOLD = 2  # How long every troubled board stays in reset before the first release
RECOVERY_BOOT_TIMEOUT = 20  # From release to a quiet line and an answer to /bulbs
# Nothing is sent to a board for this long after a release. Past the 1 s delay and the 5 s
# prompt in setup() of older and debug builds, where the 's' in /bulbs would enter setup.
RECOVERY_BOOT_WAIT = 7
RECOVERY_RETRY_GAP = 1  # Between failed tries at reaching a released board

# API Commands
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
SERVICE_RESCAN_PORTS = "rescan_ports"

# Attributes
ATTR_SERIAL_PORT = "serial_port"
ATTR_BULB_NAME = "bulb_name"
