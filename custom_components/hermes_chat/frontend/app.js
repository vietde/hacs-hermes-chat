/* Hermes Chat frontend: xterm.js chat + profile switcher.
 * Same origin as Home Assistant. The access token comes from, in order:
 *   1. a ?token= query parameter,
 *   2. a postMessage from the sidebar panel element (panel.js), which gets
 *      the token from the `hass` object — this is the path that works in
 *      the HA mobile app, where localStorage holds no token,
 *   3. localStorage ("hassTokens") — the desktop-browser path.
 * The chat websocket takes the token as ?token= plus ?profile=; the
 * server validates the token and requires an admin user, then relays
 * the Hermes PTY channel. Switching profile closes the socket and
 * reconnects with the new profile (a fresh Hermes ticket is minted
 * server side for every connection). */
(function () {
  "use strict";

  var WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;

  var haToken = null; // token posted by the sidebar panel element
  var tokenWaitedMs = 0; // ms spent waiting for a token at boot
  var tokenRetryTimer = null;

  var profiles = [];
  var currentProfile = "";
  var ws = null;
  var wantOpen = true;
  var reconnectAttempts = 0;
  var reconnectTimer = null;
  var keepaliveTimer = null;
  var tokenRefreshTried = false;

  var term = null;
  var fitAddon = null;

  function requestTokenFromParent() {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: "hermes-chat-need-token" }, location.origin);
      }
    } catch (e) { /* ignore: no parent to ask */ }
  }

  function getToken() {
    try {
      var m = /[?&]token=([^&]+)/.exec(location.search);
      if (m) return decodeURIComponent(m[1]);
    } catch (e) { /* ignore */ }
    if (haToken) return haToken;
    try {
      var raw = localStorage.getItem("hassTokens");
      if (raw) return JSON.parse(raw).access_token || null;
    } catch (e) {
      return null;
    }
    return null;
  }

  // Tell the server where the page got to (loaded, token received,
  // websocket open/closed). Used to diagnose a blank screen on a phone,
  // where there is no dev console to look at. Sends only a short stage
  // string — never the token. Failures are silent on purpose.
  var reportedStages = {};
  function report(stage) {
    if (reportedStages[stage]) return;
    reportedStages[stage] = true;
    try {
      fetch("/api/hermes_chat/client-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage: stage }),
        keepalive: true,
      }).catch(function () {});
    } catch (e) { /* ignore */ }
  }

  window.addEventListener("message", function (ev) {
    if (ev.origin !== location.origin) return;
    var d = ev.data || {};
    if (d.type === "hermes-chat-token" && d.token) {
      haToken = d.token;
      report("token-parent");
      connect();
    }
  });

  function showOverlay(text) {
    document.getElementById("overlay-text").textContent = text;
    document.getElementById("term-overlay").classList.remove("hidden");
  }
  function hideOverlay() {
    document.getElementById("term-overlay").classList.add("hidden");
  }

  function setStatus(state, text) {
    var el = document.getElementById("status");
    el.textContent = text;
    el.className = "status " + state;
  }

  function sendResize() {
    try { fitAddon.fit(); } catch (e) { /* ignore */ }
    if (ws && ws.readyState === WebSocket.OPEN && term) {
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
  }

  function stopKeepalive() {
    if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
  }

  function startKeepalive() {
    stopKeepalive();
    // Application-level ping every 5 s. The server answers {"type":"pong"}.
    // This keeps data flowing through middleboxes that kill idle
    // websockets after ~10 s, and it keeps the server's 120 s receive
    // timeout from ever firing on a live connection.
    keepaliveTimer = setInterval(function () {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) { /* ignore */ }
      }
    }, 5000);
  }

  function scheduleReconnect() {
    if (!wantOpen) return;
    if (reconnectAttempts >= 5) {
      setStatus("disconnected", "Disconnected");
      showOverlay("Hermes chat disconnected. Reconnect to try again.");
      return;
    }
    reconnectAttempts += 1;
    setStatus("disconnected", "Reconnecting…");
    reconnectTimer = setTimeout(function () {
      if (term) term.reset();
      connect();
    }, 1000);
  }

  function connect() {
    var token = getToken();
    if (!token) {
      // No token yet — the sidebar panel posts one (the HA mobile app
      // keeps tokens out of localStorage). Ask for it and wait a little
      // before giving up, instead of failing on the first paint.
      requestTokenFromParent();
      if (tokenWaitedMs < 10000) {
        tokenWaitedMs += 500;
        setStatus("disconnected", "Connecting…");
        if (!tokenRetryTimer) {
          tokenRetryTimer = setTimeout(function () {
            tokenRetryTimer = null;
            if (!ws || ws.readyState !== WebSocket.OPEN) connect();
          }, 500);
        }
        return;
      }
      setStatus("disconnected", "No session");
      report("token-timeout");
      showOverlay("No Home Assistant session found. Open this page from inside Home Assistant (sidebar → Hermes Chat) while logged in.");
      return;
    }
    tokenWaitedMs = 0;
    if (!haToken) report("token-storage");
    setStatus("disconnected", "Connecting…");
    hideOverlay();
    var url = WS_BASE + "/api/hermes_chat/ws?token=" + encodeURIComponent(token);
    if (currentProfile) url += "&profile=" + encodeURIComponent(currentProfile);
    var socket = new WebSocket(url);
    ws = socket;
    socket.binaryType = "arraybuffer";
    socket.onopen = function () {
      if (socket !== ws) return; // stale socket (reconnected already)
      tokenRefreshTried = false;
      reconnectAttempts = 0;
      report("ws-open");
      setStatus("connected", "Connected" + (currentProfile ? " · " + currentProfile : ""));
      startKeepalive();
      sendResize();
      if (term) term.focus();
    };
    socket.onmessage = function (ev) {
      if (socket !== ws) return;
      if (typeof ev.data === "string") {
        try {
          var msg = JSON.parse(ev.data);
          if (msg.type === "error" && term) term.writeln("\r\n" + msg.message);
        } catch (e) { /* ignore */ }
        return;
      }
      if (term) term.write(new Uint8Array(ev.data));
    };
    socket.onclose = function (ev) {
      if (socket !== ws) return; // stale socket
      stopKeepalive();
      report(ev.code === 4401 ? "ws-close-4401" : "ws-close-" + ev.code);
      if (!wantOpen) return;
      setStatus("disconnected", "Disconnected");
      if (ev.code === 4401 && !tokenRefreshTried && window.parent && window.parent !== window) {
        // Token expired — ask the panel for a fresh one and retry once
        // before telling the user the session is gone.
        tokenRefreshTried = true;
        setStatus("disconnected", "Session expired — refreshing…");
        requestTokenFromParent();
        setTimeout(function () { connect(); }, 500);
        return;
      }
      if (ev.code === 4401) {
        showOverlay("Home Assistant session expired. Reload the page and log in again.");
        return;
      }
      // Any other close (usually 1006, the network dropping the socket):
      // reconnect automatically a few times instead of leaving a dead
      // chat. A reconnect starts a fresh Hermes session — the old one is
      // gone with the socket — so the terminal is reset first.
      scheduleReconnect();
    };
    socket.onerror = function () {
      if (socket !== ws) return;
      report("ws-error");
      setStatus("disconnected", "Connection error");
    };
  }

  function reconnect() {
    wantOpen = true;
    reconnectAttempts = 0;
    tokenRefreshTried = false;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { try { ws.close(); } catch (e) { /* ignore */ } ws = null; }
    hideOverlay();
    if (term) term.reset();
    connect();
  }

  /* ---------------- Profiles ---------------- */
  function loadProfiles() {
    var token = getToken();
    var select = document.getElementById("profile-select");
    function fill(list, def) {
      profiles = list && list.length ? list : [];
      select.innerHTML = "";
      profiles.forEach(function (p) {
        var opt = document.createElement("option");
        opt.value = p;
        opt.textContent = p;
        select.appendChild(opt);
      });
      currentProfile = def || profiles[0] || "";
      if (currentProfile) select.value = currentProfile;
      select.disabled = profiles.length <= 1;
    }
    if (!token) {
      // No token yet: the panel will post one, and connect() runs then;
      // profiles load on the next tick once the token exists.
      setTimeout(loadProfiles, 500);
      return;
    }
    fetch("/api/hermes_chat/profiles", {
      headers: { Authorization: "Bearer " + token },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("profiles failed (" + r.status + ")");
        return r.json();
      })
      .then(function (data) { fill(data.profiles || [], data.default || ""); })
      .catch(function () {
        // Profiles endpoint unavailable (older install?): fall back to
        // the default profile and hide the switcher.
        fill(["trading-research-assistant", "default"], "trading-research-assistant");
      });
  }

  document.getElementById("profile-select").addEventListener("change", function (ev) {
    var next = ev.target.value;
    if (!next || next === currentProfile) return;
    currentProfile = next;
    // A new profile means a new Hermes session: close and reconnect.
    reconnect();
  });

  /* ---------------- Buttons ---------------- */
  document.getElementById("btn-reconnect").addEventListener("click", reconnect);
  document.getElementById("btn-overlay-reconnect").addEventListener("click", function () {
    tokenWaitedMs = 0;
    reconnect();
  });
  document.getElementById("btn-clear").addEventListener("click", function () {
    if (term) term.clear();
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(sendResize, 100);
  });

  /* ---------------- Boot ---------------- */
  report("page-loaded");
  term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, monospace',
    theme: { background: "#0b0e14", foreground: "#d6deeb" },
    allowProposedApi: true,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById("term-body"));
  term.onData(function (data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(data));
  });
  loadProfiles();
  connect();
})();
