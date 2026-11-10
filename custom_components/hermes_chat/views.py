"""HTTP views: Hermes Chat frontend and websocket relay.

The chat runs in the browser (xterm.js) and talks to Home Assistant
over a websocket. Browsers cannot set an Authorization header on a
websocket, so the token travels as a ``?token=`` query parameter and is
validated here against HA's own auth — admin users only.

Home Assistant (server side) then talks to the Hermes dashboard on the
LAN: it logs in, mints a single-use WebSocket ticket, and relays the
Hermes PTY channel (`/api/pty`) to the browser. The browser never
touches Hermes directly, so there is no proxy, no mixed content, and no
subpath rewriting to fight.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEFAULT_PROFILE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_PROFILES,
    CONF_USERNAME,
    DEFAULT_PROFILE,
    DEFAULT_PROFILES,
    WS_CLOSE_UNAUTHORIZED,
)
from .hermes import HermesSession

_LOGGER = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"

_ASSET_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".map": "application/json",
}


def _validate_ws_user(hass: HomeAssistant, request: web.Request):
    """Return the admin user for a valid ``?token=``, else None."""
    token = request.query.get("token")
    if not token:
        return None
    try:
        refresh_token = hass.auth.async_validate_access_token(token)
    except Exception:  # noqa: BLE001 - any auth failure denies access
        return None
    if refresh_token is None:
        return None
    user = refresh_token.user
    if user is None or not user.is_admin:
        return None
    return user


def _profiles(hass: HomeAssistant) -> tuple[list[str], str]:
    """Configured Hermes profiles and the default, from the entry."""
    data = hass.data.get("hermes_chat") or {}
    raw = str(data.get(CONF_PROFILES) or DEFAULT_PROFILES)
    profiles = [p.strip() for p in raw.split(",") if p.strip()]
    if not profiles:
        profiles = [p.strip() for p in DEFAULT_PROFILES.split(",")]
    default = str(data.get(CONF_DEFAULT_PROFILE) or DEFAULT_PROFILE).strip()
    if default not in profiles:
        default = profiles[0]
    return profiles, default


class HermesChatFrontendView(HomeAssistantView):
    """Serve the chat page. The page itself holds no secrets; the
    websocket behind it validates the HA token, so this needs no auth
    (same as HA's own login page)."""

    url = "/api/hermes_chat/"
    name = "hermes_chat:frontend"
    requires_auth = False

    async def get(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(FRONTEND_DIR / "index.html")


class HermesChatAssetView(HomeAssistantView):
    """Serve the frontend's static files (xterm.js, app.js, styles)."""

    url = "/api/hermes_chat/assets/{filename}"
    name = "hermes_chat:assets"
    requires_auth = False

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        path = FRONTEND_DIR / filename
        if (
            path.suffix not in _ASSET_TYPES
            or path.parent != FRONTEND_DIR
            or not path.is_file()
        ):
            return web.Response(status=404, text="not found")
        # Never cache the frontend: after a HACS update the browser must
        # fetch the new app.js/panel.js, not keep running the old ones.
        return web.FileResponse(
            path,
            headers={
                "Content-Type": _ASSET_TYPES[path.suffix],
                "Cache-Control": "no-cache, max-age=0",
            },
        )


class ClientLogView(HomeAssistantView):
    """Receive a short stage report from the chat page.

    The page posts where it got to (loaded, token received, websocket
    open/closed) so a blank screen on a phone can be diagnosed from the
    HA log instead of guessed at. No token or user data is sent — just a
    short stage string. Unauthenticated on purpose: the page cannot
    authenticate before it has a token, which is exactly the state this
    diagnoses.
    """

    url = "/api/hermes_chat/client-log"
    name = "hermes_chat:client_log"
    requires_auth = False

    async def post(self, request: web.Request) -> web.StreamResponse:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - bad body is not worth an error
            data = {}
        stage = str(data.get("stage") or "")[:64]
        if stage:
            _LOGGER.warning("Hermes Chat client: %s", stage)
        return self.json_message("ok")


class HermesChatProfilesView(HomeAssistantView):
    """List the configured Hermes profiles for the panel switcher."""

    url = "/api/hermes_chat/profiles"
    name = "hermes_chat:profiles"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        profiles, default = _profiles(self.hass)
        return self.json({"profiles": profiles, "default": default})


class HermesChatWebSocketView(HomeAssistantView):
    """WebSocket bridge between the browser chat and the Hermes PTY."""

    url = "/api/hermes_chat/ws"
    name = "hermes_chat:chat_ws"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        # No protocol heartbeat: this network drops websocket PONGs (HA's
        # own log shows "No PONG received after 27.5 seconds" on the main
        # HA connections too), so a server ping whose pong never comes
        # back would kill a live chat. Instead the browser sends an
        # application-level {"type":"ping"} every 5 s (answered with
        # {"type":"pong"}). The 120 s receive timeout only cleans up
        # truly dead peers: a live client pings every 5 s and never trips
        # it.
        ws = web.WebSocketResponse(heartbeat=None, receive_timeout=120)
        await ws.prepare(request)
        user = _validate_ws_user(self.hass, request)
        if user is None:
            await ws.close(code=WS_CLOSE_UNAUTHORIZED, message=b"unauthorized")
            return ws

        data = self.hass.data.get("hermes_chat") or {}
        host = str(data.get(CONF_HOST) or "")
        try:
            port = int(data.get(CONF_PORT) or 0)
        except (TypeError, ValueError):
            port = 0
        username = str(data.get(CONF_USERNAME) or "")
        password = str(data.get(CONF_PASSWORD) or "")
        profiles, default_profile = _profiles(self.hass)
        profile = (request.query.get("profile") or "").strip() or default_profile
        if profile not in profiles:
            profile = default_profile
        if not host or port < 1 or not username or not password:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "error",
                        "message": "Hermes Chat is not configured — open Settings → Devices & Services → Hermes Chat and enter the Hermes host and login.",
                    }
                )
            )
            await ws.close()
            return ws

        loop = asyncio.get_running_loop()
        session: HermesSession | None = None

        def _on_output(payload: bytes) -> None:
            if not ws.closed:
                asyncio.ensure_future(_safe_send(payload))

        async def _safe_send(payload: bytes) -> None:
            try:
                await ws.send_bytes(payload)
            except Exception:  # noqa: BLE001 - peer gone
                pass

        def _on_exit(_code) -> None:
            if not ws.closed:
                asyncio.ensure_future(_safe_send(b"\r\n[Hermes chat closed]\r\n"))

        try:
            session = HermesSession(
                loop, host, port, username, password, profile, _on_output, on_exit=_on_exit
            )
            await session.start()
        except RuntimeError as err:
            _LOGGER.warning("Hermes Chat: could not start session: %s", err)
            await ws.send_str(json.dumps({"type": "error", "message": str(err)}))
            await ws.close()
            return ws
        except Exception as err:  # noqa: BLE001 - report to the browser
            _LOGGER.warning("Hermes Chat: could not start session: %s", err)
            await ws.send_str(
                json.dumps({"type": "error", "message": f"Could not start Hermes chat: {err}"})
            )
            await ws.close()
            return ws

        _LOGGER.debug("Hermes Chat: websocket opened for %s (profile=%s)", user.name, profile)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    session.write_input(msg.data)
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    if payload.get("type") == "resize":
                        try:
                            session.resize(int(payload["cols"]), int(payload["rows"]))
                        except (KeyError, TypeError, ValueError):
                            pass
                    elif payload.get("type") == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        finally:
            if session is not None:
                await session.close()
            _LOGGER.debug("Hermes Chat: websocket closed for %s", user.name)
        return ws
