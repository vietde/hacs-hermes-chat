# Hermes Chat

The Hermes agent chat, as a native panel inside Home Assistant. No
add-on container, no ingress proxy.

Home Assistant itself connects to the Hermes dashboard on your local
network (server side): it logs in, mints a single-use WebSocket ticket,
and relays the Hermes PTY channel (`/api/pty`) to the browser. The
browser only ever talks to Home Assistant, so there is no proxy, no
mixed content, and no subpath rewriting — the problems that kept the
Hermes Chat tab from loading through `hass_ingress` do not apply here.

## What it is

- A sidebar panel (**Hermes Chat**) with the Hermes terminal chat
  (xterm.js), the same chat you get from the Hermes dashboard.
- A **Profile** switcher in the toolbar: it syncs the profile list
  live from Hermes (`GET /api/profiles`), so every profile you have
  shows up with its description — pick one and the chat reconnects
  with that profile. If Hermes cannot be reached, the switcher falls
  back to the profiles listed in the integration options.
- Works on web, mobile browser, and the Home Assistant app.

## Requirements

- The Hermes dashboard running on your local network (default
  `192.168.2.10:9119`), with a username and password.
- Home Assistant 2026.11 or newer (uses `panel_custom` and the
  standard HTTP views).

## Install

Via HACS (custom repository):

1. HACS → Integrations → ⋮ → Custom repositories → add
   `https://github.com/vietde/hacs-hermes-chat` (category Integration).
2. Install **Hermes Chat**, then restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **Hermes Chat**.
4. Enter the Hermes host, port, username, and password. The setup
   tests the login before it creates the entry.
5. Open the **Hermes Chat** panel in the sidebar.

Direct install: download the
[latest release ZIP](https://github.com/vietde/hacs-hermes-chat/releases),
unzip it into `/config/custom_components/`, and restart.

## Options

- **Host / Port / Username / Password** — where Hermes runs and how
  to log in.
- **Profiles** — fallback list (comma-separated) used only when the
  live profile sync from Hermes fails. Normally the switcher shows
  the profiles Hermes reports, so you never need to type them in.
- **Default profile** — the profile the panel opens with. When the
  live sync works, Hermes's own default profile is used instead.
- **Hermes API host / port / key** (optional) — enables the
  OpenAI-compatible relay for other agents (see below). Leave the
  key empty to disable it. The API host defaults to the same host
  as the dashboard; the port is 8642 by default.

## Other agents (OpenClaw, Pi, Grok, Muse AI)

Hermes Chat exposes Hermes as an OpenAI-compatible API through Home
Assistant, so other agents can use it without reaching your LAN
directly. This needs the Hermes API server running (port 8642 by
default) and the API key set in the integration options.

- **Base URL:** `https://<your HA host>/api/hermes_chat/v1`
  (e.g. `https://homebui.duckdns.org:8123/api/hermes_chat/v1`)
- **API key:** a Home Assistant long-lived access token
  (your profile → Long-Lived Access Tokens → Create Token).
- **Model:** a Hermes profile name — `GET /v1/models` lists them
  (default, cybersecurity-expert, financial-advisor,
  trading-research-assistant, …). Set `model` to the profile you
  want the agent to use.
- **Endpoints:** `GET /v1/models`, `POST /v1/chat/completions`
  (streaming and non-streaming both work).

Example (any OpenAI-compatible client):

```json
{
  "base_url": "https://homebui.duckdns.org:8123/api/hermes_chat/v1",
  "api_key": "<HA long-lived access token>",
  "model": "trading-research-assistant"
}
```

Direct LAN access also works, without Home Assistant: point the
agent at `http://<Hermes host>:8642/v1` with the Hermes API key
(`API_SERVER_KEY`) as the Bearer token.

## Version log

- **0.1.2** — OpenAI-compatible API relay for other agents
  (`/v1/models`, `/v1/chat/completions` through HA) + profiles sync
  live from Hermes (`GET /api/profiles`): the switcher lists every
  profile with its description, the default comes from Hermes, and
  the configured list is only a fallback when Hermes cannot be
  reached.
- **0.1.0** — first version: chat panel with profile switcher.
  Untested against a live Hermes dashboard until installed — the
  login, ticket, and PTY relay are written from the Hermes protocol
  (form login → `POST /api/auth/ws-ticket` → `ws:///api/pty`) and need
  a real install to confirm end to end.
