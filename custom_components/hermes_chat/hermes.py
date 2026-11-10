"""Hermes chat session: a relay to the Hermes dashboard's PTY channel.

The Hermes dashboard (Nous Research) serves its chat as a terminal UI
over a WebSocket at ``/api/pty``. Authentication is a cookie session
after a form login, plus a single-use, short-lived ticket minted for
each WebSocket connection:

  1. ``POST /auth/password-login`` (JSON: provider "basic", username,
     password) -> session cookie.
  2. ``POST /api/auth/ws-ticket`` (with the cookie) -> ``{"ticket": ...}``.
  3. Connect ``ws://<host>:<port>/api/pty?ticket=<ticket>`` (plus
     ``&profile=<name>`` to pick a Hermes profile).

This module owns one such connection: input bytes go to the Hermes PTY,
output bytes arrive via the ``on_output`` callback, and the session
closes (firing ``on_exit``) when Hermes drops the socket. Tickets are
single-use, so a reconnect must mint a fresh one — this class does not
auto-reconnect; the caller creates a new session instead.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class HermesSession:
    """One chat session on the Hermes dashboard's PTY channel."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        host: str,
        port: int,
        username: str,
        password: str,
        profile: str,
        on_output,
        on_exit=None,
    ) -> None:
        self._loop = loop
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._profile = profile or "default"
        self._on_output = on_output
        self._on_exit = on_exit
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._read_task: asyncio.Task | None = None
        self._closed = False
        self._exit_reported = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def _login(self, session: aiohttp.ClientSession) -> bool:
        """Log in to Hermes and keep the session cookie in the jar.

        The endpoint takes a JSON body — ``{"provider": "basic",
        "username": ..., "password": ..., "next": "/"}`` — not form
        data, and ``provider`` is a required field. A good login
        returns 200 ``{"ok": true}`` and a session cookie; a bad one
        returns 401 ``Invalid credentials``. Too many attempts from
        one IP returns 429 (10/minute).
        """
        url = f"{self._base_url()}/auth/password-login"
        try:
            async with session.post(
                url,
                json={
                    "provider": "basic",
                    "username": self._username,
                    "password": self._password,
                    "next": "/",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                # A successful login returns 200 {"ok": true} (or a
                # redirect on some versions) and sets a session cookie.
                _LOGGER.debug("Hermes login: %s -> %s", url, resp.status)
                if resp.status in (200, 302, 303):
                    return True
                body = await resp.text()
                _LOGGER.warning(
                    "Hermes login failed: status=%s body=%s",
                    resp.status,
                    body[:200],
                )
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Hermes login error: %s", err)
            return False

    async def _mint_ticket(self, session: aiohttp.ClientSession) -> str | None:
        """Mint a single-use WebSocket ticket (30 s TTL)."""
        url = f"{self._base_url()}/api/auth/ws-ticket"
        try:
            async with session.post(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    _LOGGER.warning(
                        "Hermes ws-ticket failed: status=%s body=%s",
                        resp.status,
                        body[:200],
                    )
                    return None
                data = await resp.json(content_type=None)
                ticket = (data or {}).get("ticket")
                if not ticket:
                    _LOGGER.warning("Hermes ws-ticket: no ticket in %s", str(data)[:200])
                    return None
                return ticket
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.warning("Hermes ws-ticket error: %s", err)
            return None

    async def start(self) -> None:
        """Log in, mint a ticket, and open the Hermes PTY channel.

        Raises RuntimeError with a human-readable message when any step
        fails, so the caller can report it to the browser.
        """
        session = aiohttp.ClientSession(
            trust_env=True,
            # Hermes runs on a LAN IP (192.168.2.10), and aiohttp's
            # default cookie jar REFUSES cookies set by bare IP
            # addresses. Without unsafe=True the login's session cookie
            # is silently dropped, so the ws-ticket request goes out
            # with no cookie and Hermes answers 401 "no_cookie".
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self._session = session
        try:
            if not await self._login(session):
                raise RuntimeError("Hermes login failed — check host, username and password")
            ticket = await self._mint_ticket(session)
            if not ticket:
                raise RuntimeError("Hermes would not issue a chat ticket — check the Hermes login")
            ws_url = (
                f"ws://{self._host}:{self._port}/api/pty"
                f"?ticket={ticket}&profile={self._profile}"
            )
            _LOGGER.debug("Hermes PTY: connecting (profile=%s)", self._profile)
            try:
                self._ws = await session.ws_connect(
                    ws_url, timeout=aiohttp.ClientTimeout(total=15)
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise RuntimeError(f"Could not open the Hermes chat channel: {err}") from err
            _LOGGER.info(
                "Hermes chat connected: %s:%s profile=%s",
                self._host,
                self._port,
                self._profile,
            )
            self._read_task = self._loop.create_task(self._read_loop())
        except Exception:
            await self._close_resources()
            raise

    async def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for msg in ws:
                if self._closed:
                    break
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._emit(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    # Hermes may send text frames; the terminal wants
                    # bytes, so encode them.
                    try:
                        self._emit(msg.data.encode("utf-8"))
                    except Exception:  # noqa: BLE001 - a bad frame must not kill the loop
                        pass
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug("Hermes PTY error: %s", ws.exception())
                    break
        except (aiohttp.ClientError, asyncio.CancelledError):
            pass
        finally:
            self._loop.call_soon(self._report_exit)

    def _emit(self, data: bytes) -> None:
        if not data:
            return
        try:
            self._on_output(data)
        except Exception:  # noqa: BLE001 - a dead transport must not kill the reader
            _LOGGER.debug("Hermes output callback failed", exc_info=True)

    def _report_exit(self) -> None:
        if self._exit_reported:
            return
        self._exit_reported = True
        if self._on_exit is not None:
            try:
                self._on_exit(None)
            except Exception:  # noqa: BLE001 - never raise out of the reader
                _LOGGER.debug("Hermes exit callback failed", exc_info=True)

    def write_input(self, data: bytes) -> None:
        """Send keystrokes to the Hermes PTY."""
        if self._closed or self._ws is None or self._ws.closed or not data:
            return
        try:
            asyncio.ensure_future(self._ws.send_bytes(data))
        except Exception:  # noqa: BLE001 - peer gone
            pass

    def resize(self, cols: int, rows: int) -> None:
        """Tell the Hermes PTY the terminal size changed.

        Best effort: Hermes may ignore an unknown message, so a failure
        here never raises.
        """
        if self._closed or self._ws is None or self._ws.closed:
            return
        if cols < 1 or rows < 1:
            return
        try:
            asyncio.ensure_future(
                self._ws.send_str(
                    json.dumps({"type": "resize", "cols": int(cols), "rows": int(rows)})
                )
            )
        except Exception:  # noqa: BLE001 - resize is best effort
            pass

    async def _close_resources(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - already closing
                pass
            self._read_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 - already closing
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001 - already closing
                pass
            self._session = None

    async def close(self) -> None:
        """Close the Hermes channel and release the HTTP session."""
        if self._closed:
            return
        self._closed = True
        await self._close_resources()
        _LOGGER.debug("Hermes chat session closed")


async def fetch_profiles(
    host: str, port: int, username: str, password: str
) -> list[dict] | None:
    """List the Hermes profiles from the dashboard itself.

    Logs in and calls ``GET /api/profiles``, which returns
    ``{"profiles": [{"name": ..., "description": ...,
    "is_default": ...}, ...]}``. Returns the profile list, or None
    when Hermes cannot be reached or the call fails — the caller then
    falls back to the profiles configured in the integration options.
    """
    session = aiohttp.ClientSession(
        trust_env=True,
        # See HermesSession.start: Hermes is on a LAN IP, so the
        # default cookie jar would drop its session cookie.
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    try:
        # Log in first: /api/profiles needs the session cookie. JSON
        # body with provider "basic" (form data is rejected).
        try:
            async with session.post(
                f"http://{host}:{int(port)}/auth/password-login",
                json={
                    "provider": "basic",
                    "username": username,
                    "password": password,
                    "next": "/",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 302, 303):
                    _LOGGER.debug("Hermes profile sync: login failed (%s)", resp.status)
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Hermes profile sync: login error: %s", err)
            return None
        try:
            async with session.get(
                f"http://{host}:{int(port)}/api/profiles",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Hermes profile sync: /api/profiles -> %s", resp.status)
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.debug("Hermes profile sync: /api/profiles error: %s", err)
            return None
        profiles = []
        for item in (data or {}).get("profiles") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            profiles.append(
                {
                    "name": name,
                    "description": str(item.get("description") or ""),
                    "is_default": bool(item.get("is_default")),
                }
            )
        return profiles or None
    finally:
        await session.close()


async def test_hermes_login(
    host: str, port: int, username: str, password: str
) -> tuple[bool, str]:
    """Check a Hermes host/username/password without opening a chat.

    Returns (ok, message). Used by the config flow so a typo is caught
    before the entry is created. Every failure is logged with its
    status code so a rejected login shows up in the HA log instead of
    failing silently.
    """
    session = aiohttp.ClientSession(
        trust_env=True,
        # See HermesSession.start: Hermes is on a LAN IP, so the
        # default cookie jar would drop its session cookie.
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    try:
        # Reachability first: the dashboard answers /api/status.
        try:
            async with session.get(
                f"http://{host}:{int(port)}/api/status",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Hermes Chat setup: /api/status at %s:%s returned %s",
                        host,
                        port,
                        resp.status,
                    )
                    return False, f"Hermes answered /api/status with {resp.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Hermes Chat setup: cannot reach Hermes at %s:%s (%s)", host, port, err
            )
            return False, f"Cannot reach Hermes at {host}:{port} ({err})"
        # Then the login itself. JSON body with provider "basic"
        # (form data is rejected with 401).
        try:
            async with session.post(
                f"http://{host}:{int(port)}/auth/password-login",
                json={
                    "provider": "basic",
                    "username": username,
                    "password": password,
                    "next": "/",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 302, 303):
                    return True, "ok"
                body = await resp.text()
                _LOGGER.warning(
                    "Hermes Chat setup: login rejected for %s at %s:%s "
                    "(status=%s body=%s)",
                    username,
                    host,
                    port,
                    resp.status,
                    body[:200],
                )
                if resp.status == 429:
                    return False, "Too many login attempts — Hermes rate-limits to 10/minute; wait a minute and try again"
                if resp.status == 401:
                    return False, "Hermes rejected the username or password"
                return False, f"Hermes login failed (status {resp.status})"
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Hermes Chat setup: login request to %s:%s failed (%s)", host, port, err
            )
            return False, f"Hermes login failed ({err})"
    finally:
        await session.close()
