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
- A **Profile** switcher in the toolbar: pick a Hermes profile
  (e.g. `trading-research-assistant`, `default`) and the chat
  reconnects with that profile. Profiles are configured in the
  integration options as a comma-separated list.
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
- **Profiles** — comma-separated Hermes profiles for the switcher.
- **Default profile** — the profile the panel opens with.

## Version log

- **0.1.0** — first version: chat panel with profile switcher.
  Untested against a live Hermes dashboard until installed — the
  login, ticket, and PTY relay are written from the Hermes protocol
  (form login → `POST /api/auth/ws-ticket` → `ws:///api/pty`) and need
  a real install to confirm end to end.
