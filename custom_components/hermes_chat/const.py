"""Constants for the Hermes Chat integration."""

DOMAIN = "hermes_chat"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PROFILES = "profiles"
CONF_DEFAULT_PROFILE = "default_profile"

DEFAULT_HOST = "192.168.2.10"
DEFAULT_PORT = 9119
DEFAULT_USERNAME = "admin"
# Comma-separated Hermes profiles offered in the panel switcher.
DEFAULT_PROFILES = "trading-research-assistant,default"
DEFAULT_PROFILE = "trading-research-assistant"

# WebSocket close code sent when the browser token is missing/invalid.
WS_CLOSE_UNAUTHORIZED = 4401

# Integration version (mirrors manifest.json). Used to bust the browser
# cache on the panel module URL after an update.
VERSION = "0.1.0"
