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
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    CONF_API_HOST,
    CONF_API_KEY,
    CONF_API_PORT,
    CONF_DEFAULT_PROFILE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_PROFILES,
    CONF_USERNAME,
    DEFAULT_API_PORT,
    DEFAULT_PROFILE,
    DEFAULT_PROFILES,
    WS_CLOSE_UNAUTHORIZED,
)
from .hermes import HermesSession, fetch_profiles

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


async def _profiles(
    hass: HomeAssistant, force_refresh: bool = False
) -> tuple[list[dict], str]:
    """The Hermes profiles, synced live from the dashboard.

    Logs in to Hermes and calls ``GET /api/profiles`` so the switcher
    always shows the profiles that actually exist (and their
    descriptions). The result is cached for 5 minutes: without the
    cache, every page load and every chat reconnect would log in to
    Hermes again, and Hermes rate-limits logins to 10/minute — the
    panel would 429 itself into a reconnect loop. Falls back to the
    profiles configured in the integration options when Hermes cannot
    be reached, so the panel still works offline. Returns (profiles,
    default_name) where each profile is {"name", "description",
    "is_default"}.
    """
    data = hass.data.get("hermes_chat") or {}
    now = time.monotonic()
    cached = data.get("_profiles_cache") or {}
    if (
        not force_refresh
        and cached.get("profiles")
        and now - cached.get("fetched_at", 0) < 300
    ):
        return cached["profiles"], cached["default"]
    host = str(data.get(CONF_HOST) or "")
    try:
        port = int(data.get(CONF_PORT) or 0)
    except (TypeError, ValueError):
        port = 0
    username = str(data.get(CONF_USERNAME) or "")
    password = str(data.get(CONF_PASSWORD) or "")
    if host and port and username and password:
        live = await fetch_profiles(host, port, username, password)
        if live:
            default = next(
                (p["name"] for p in live if p.get("is_default")), live[0]["name"]
            )
            data["_profiles_cache"] = {
                "profiles": live,
                "default": default,
                "fetched_at": now,
            }
            return live, default
    # Fallback: the configured comma-separated list. Cache it briefly
    # too, so a down Hermes does not make every request retry a login.
    raw = str(data.get(CONF_PROFILES) or DEFAULT_PROFILES)
    names = [p.strip() for p in raw.split(",") if p.strip()]
    if not names:
        names = [p.strip() for p in DEFAULT_PROFILES.split(",")]
    default = str(data.get(CONF_DEFAULT_PROFILE) or DEFAULT_PROFILE).strip()
    if default not in names:
        default = names[0]
    profiles = [{"name": n, "description": "", "is_default": n == default} for n in names]
    data["_profiles_cache"] = {
        "profiles": profiles,
        "default": default,
        "fetched_at": now,
    }
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
        force = request.query.get("refresh") == "1"
        profiles, default = await _profiles(self.hass, force_refresh=force)
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
        profiles, default_profile = await _profiles(self.hass)
        profile_names = [p["name"] for p in profiles]
        profile = (request.query.get("profile") or "").strip() or default_profile
        if profile not in profile_names:
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


class HermesChatModelsView(HomeAssistantView):
    """List the Hermes profiles as OpenAI-compatible models.

    Other agents (OpenClaw, Pi, Grok, Muse) point their
    OpenAI-compatible client at HA and call this to discover which
    profiles they can use as the `model` in a chat completion.
    Requires an HA token (long-lived access token as Bearer).
    """

    url = "/api/hermes_chat/v1/models"
    name = "hermes_chat:models"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        profiles, _default = await _profiles(self.hass)
        return self.json(
            {
                "object": "list",
                "data": [
                    {
                        "id": p["name"],
                        "object": "model",
                        "created": 0,
                        "owned_by": "hermes",
                    }
                    for p in profiles
                ],
            }
        )


class HermesChatCompletionsView(HomeAssistantView):
    """OpenAI-compatible chat completions, relayed to Hermes.

    Lets other agents (OpenClaw, Pi, Grok, Muse) talk to Hermes
    through Home Assistant instead of reaching the Hermes API server
    on the LAN directly. The agent points its OpenAI-compatible
    client at ``https://<HA host>/api/hermes_chat/v1`` with an HA
    long-lived access token as the Bearer token, and sets `model` to
    a Hermes profile name (see /v1/models). HA forwards the request
    to the Hermes API server (port 8642 by default) with the
    configured Hermes API key.

    Requires the Hermes API server to be running and the API key to
    be set in the integration options; otherwise returns a clear
    error instead of a silent failure. Streaming (SSE) and
    non-streaming requests are both passed through.
    """

    url = "/api/hermes_chat/v1/chat/completions"
    name = "hermes_chat:completions"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.StreamResponse:
        data = self.hass.data.get("hermes_chat") or {}
        api_host = str(data.get(CONF_API_HOST) or data.get(CONF_HOST) or "")
        try:
            api_port = int(data.get(CONF_API_PORT) or DEFAULT_API_PORT)
        except (TypeError, ValueError):
            api_port = DEFAULT_API_PORT
        api_key = str(data.get(CONF_API_KEY) or "")
        if not api_host or not api_key:
            return self.json(
                {
                    "error": {
                        "message": (
                            "Hermes API relay is not configured — set the Hermes "
                            "API host and API key in the Hermes Chat integration "
                            "options (Settings → Devices & Services → Hermes Chat → "
                            "Configure), and make sure the Hermes API server is "
                            "running (port 8642 by default)."
                        ),
                        "type": "configuration_error",
                    }
                },
                status_code=503,
            )
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return self.json(
                {"error": {"message": "Request body must be JSON.", "type": "invalid_request"}},
                status_code=400,
            )
        url = f"http://{api_host}:{api_port}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        session = aiohttp.ClientSession(trust_env=True)
        try:
            resp = await session.post(
                url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=600)
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            await session.close()
            _LOGGER.warning("Hermes API relay: cannot reach %s (%s)", url, err)
            return self.json(
                {
                    "error": {
                        "message": (
                            f"Cannot reach the Hermes API server at {api_host}:{api_port} "
                            f"({err}). Make sure the Hermes API server is running."
                        ),
                        "type": "upstream_unreachable",
                    }
                },
                status_code=502,
            )
        if body.get("stream"):
            # Server-sent events: pass the chunks through as they arrive.
            stream_resp = web.StreamResponse(
                status=resp.status,
                headers={
                    "Content-Type": resp.headers.get("Content-Type", "text/event-stream"),
                    "Cache-Control": "no-cache",
                },
            )
            await stream_resp.prepare(request)
            try:
                async for chunk in resp.content.iter_any():
                    if chunk:
                        await stream_resp.write(chunk)
            except (aiohttp.ClientError, asyncio.CancelledError, ConnectionResetError):
                pass
            finally:
                await resp.release()
                await session.close()
            await stream_resp.write_eof()
            return stream_resp
        # Non-streaming: read the whole response and return it.
        try:
            payload = await resp.json(content_type=None)
        except ValueError:
            text = await resp.text()
            await resp.release()
            await session.close()
            return web.Response(
                status=resp.status,
                text=text,
                content_type=resp.headers.get("Content-Type", "application/json"),
            )
        await resp.release()
        await session.close()
        return self.json(payload, status_code=resp.status)
