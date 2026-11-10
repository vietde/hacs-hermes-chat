"""Hermes Chat — the Hermes agent chat, as a native panel inside Home
Assistant (no add-on container, no ingress proxy).

Home Assistant itself connects to the Hermes dashboard on the LAN
(server side): it logs in, mints a single-use WebSocket ticket, and
relays the Hermes PTY channel (`/api/pty`) to the browser. The browser
only ever talks to Home Assistant, so there is no proxy, no mixed
content, and no subpath rewriting to fight — the problems that kept the
Hermes Chat tab from loading through hass_ingress do not apply here.

The sidebar panel is a custom element (panel.js): HA hands it the `hass`
object, and it passes the access token to the chat page inside its
iframe via postMessage. The page cannot read the token from localStorage
on its own — the HA mobile app keeps tokens in native storage, so
localStorage is empty there.
"""

from __future__ import annotations

import logging

from homeassistant.components import frontend, panel_custom
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION
from .views import (
    ClientLogView,
    HermesChatAssetView,
    HermesChatCompletionsView,
    HermesChatFrontendView,
    HermesChatModelsView,
    HermesChatProfilesView,
    HermesChatWebSocketView,
)

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "hermes_chat"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hermes Chat from a config entry."""
    options = {**entry.data, **entry.options}
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update(options)

    hass.http.register_view(HermesChatFrontendView())
    hass.http.register_view(HermesChatAssetView())
    hass.http.register_view(ClientLogView())
    hass.http.register_view(HermesChatProfilesView(hass))
    hass.http.register_view(HermesChatWebSocketView(hass))
    hass.http.register_view(HermesChatModelsView(hass))
    hass.http.register_view(HermesChatCompletionsView(hass))

    # Sidebar panel as a custom element (not a plain iframe panel): the
    # element receives the `hass` object from the frontend and passes the
    # access token to the chat page via postMessage. A plain iframe panel
    # cannot do that, and the page cannot read the token from
    # localStorage in the HA mobile app (see panel.js).
    await panel_custom.async_register_panel(
        hass,
        PANEL_URL_PATH,
        "hermes-chat-panel",
        sidebar_title="Hermes Chat",
        sidebar_icon="mdi:robot",
        module_url=f"/api/hermes_chat/assets/panel.js?v={VERSION}",
        require_admin=True,
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    _LOGGER.info("Hermes Chat ready: panel at /%s", PANEL_URL_PATH)
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Hermes Chat."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.pop(DOMAIN, None)
    return True
