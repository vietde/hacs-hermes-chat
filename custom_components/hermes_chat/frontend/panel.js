/* Hermes Chat sidebar panel (custom element).

Home Assistant sets the `hass` object on this element. The chat page
runs inside an iframe below, and it needs the current access token to
open its websocket and call the profiles API. It cannot read the token
from localStorage ("hassTokens"): the HA mobile app keeps tokens in
native storage, so localStorage is empty there and the page showed "No
Home Assistant session found". Instead this element hands the token to
the iframe with postMessage — on load, and again whenever the token
changes (HA refreshes it about every 30 minutes).

The iframe page falls back to localStorage when there is no parent panel
(e.g. opening /api/hermes_chat/ directly in a desktop browser).
*/

class HermesChatPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._token = null;
    this._iframe = null;
    this._onMessage = this._onMessage.bind(this);
    // NOTE: a custom element constructor must not set attributes or add
    // children — setting this.style.* here throws NotSupportedError and
    // the element can never be created (that was the HA Terminal
    // v0.1.4/v0.1.5 black screen on every platform). Styles go in
    // connectedCallback.
  }

  set hass(hass) {
    this._hass = hass;
    const token =
      hass && hass.auth && hass.auth.data && hass.auth.data.access_token;
    if (token && token !== this._token) {
      this._token = token;
      this._postToken();
    }
  }

  connectedCallback() {
    this.style.display = "block";
    this.style.height = "100%";
    // The iframe is absolutely positioned inside this element, so it
    // fills the panel area even if the element's own height comes from
    // the fallback below instead of the layout.
    this.style.position = "relative";
    const iframe = document.createElement("iframe");
    iframe.src = "/api/hermes_chat/";
    iframe.title = "Hermes Chat";
    iframe.style.cssText =
      "border:0;position:absolute;inset:0;width:100%;height:100%;display:block;";
    this.appendChild(iframe);
    this._iframe = iframe;
    iframe.addEventListener("load", () => {
      this._postToken();
      this._fixHeight();
    });
    window.addEventListener("message", this._onMessage);
    requestAnimationFrame(() => this._fixHeight());
  }

  // If the percentage heights did not resolve (the element collapsed to
  // near zero — seen on the mobile app, where the panel showed only a
  // black screen), fall back to a viewport-based height so the chat is
  // actually visible. Only kicks in when the layout gave us nothing;
  // a calc() with dvh stays live across rotation.
  _fixHeight() {
    try {
      if (this.getBoundingClientRect().height < 50) {
        this.style.height = "calc(100dvh - 110px)";
      }
    } catch (e) { /* ignore */ }
  }

  disconnectedCallback() {
    window.removeEventListener("message", this._onMessage);
    this._iframe = null;
  }

  _postToken() {
    if (this._iframe && this._iframe.contentWindow && this._token) {
      this._iframe.contentWindow.postMessage(
        { type: "hermes-chat-token", token: this._token },
        window.location.origin
      );
    }
  }

  _onMessage(ev) {
    if (ev.origin !== window.location.origin) return;
    if (ev.data && ev.data.type === "hermes-chat-need-token") {
      this._postToken();
    }
  }
}

// Guarded: HA loads this module with a plain <script type="module">
// (no cache), so a panel re-created after a disconnect would inject it
// a second time — defining the same name twice throws.
if (!customElements.get("hermes-chat-panel")) {
  customElements.define("hermes-chat-panel", HermesChatPanel);
}
