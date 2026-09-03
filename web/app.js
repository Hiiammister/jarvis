/* Bella web client — one shared brain, this browser is just a device.
   Protocol mirrors protocol/events.py. */
(function () {
  "use strict";

  var PROTOCOL_VERSION = 1;
  var TOKEN_KEY = "bella_token";
  var DEVICE_KEY = "bella_web_device_id";

  var log = document.getElementById("log");
  var form = document.getElementById("form");
  var input = document.getElementById("text");
  var sendBtn = document.getElementById("send");
  var dot = document.getElementById("dot");
  var connEl = document.getElementById("conn");
  var deviceEl = document.getElementById("device");
  var modelEl = document.getElementById("model");
  var devicesEl = document.getElementById("devices");
  var srvStatEl = document.getElementById("srvstat");

  // ---- device identity (stable per browser) ----
  function deviceId() {
    var id = localStorage.getItem(DEVICE_KEY);
    if (!id) {
      id = "web-" + Math.random().toString(36).slice(2, 10);
      localStorage.setItem(DEVICE_KEY, id);
    }
    return id;
  }
  function deviceName() {
    var ua = navigator.userAgent;
    if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "iPad";
    if (/iPhone/.test(ua)) return "iPhone";
    if (/Android/.test(ua)) return "Android";
    if (/Windows/.test(ua)) return "Windows browser";
    if (/Macintosh/.test(ua)) return "Mac browser";
    return "Web";
  }
  var DEVICE = {
    device_id: deviceId(),
    device_name: deviceName(),
    platform: "web",
    client_type: "web",
    hostname: location.host,
    capabilities: ["browser", "notifications", "clipboard"],
    aliases: [deviceName().toLowerCase(), "web", "browser", "this", "ipad", "phone"],
    protocol_version: PROTOCOL_VERSION
  };

  function getToken() {
    var t = localStorage.getItem(TOKEN_KEY);
    if (!t) {
      t = window.prompt("Enter the Bella server token (JARVIS_TOKEN from .env):");
      if (t) { t = t.trim(); localStorage.setItem(TOKEN_KEY, t); }
    }
    return t || "";
  }

  // ---- rendering ----
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function addMsg(role, whoLabel) {
    var m = el("div", "msg " + role);
    var head = el("div");
    head.appendChild(el("span", "who", whoLabel));
    head.appendChild(document.createTextNode(" "));
    head.appendChild(el("span", "caret", "›"));
    m.appendChild(head);
    var body = el("div", "body");
    m.appendChild(body);
    log.appendChild(m);
    scroll();
    return m;
  }
  function scroll() { log.scrollTop = log.scrollHeight; }

  var current = null; // { el, body, tools }

  function beginAssistant() {
    var m = addMsg("bella", "Bella");
    m.querySelector(".body").classList.add("cursor");
    current = { el: m, body: m.querySelector(".body"), text: "" };
  }
  function appendChunk(txt) {
    if (!current) beginAssistant();
    current.text += txt;
    current.body.textContent = current.text;
    scroll();
  }
  function endAssistant() {
    if (current) current.body.classList.remove("cursor");
    current = null;
  }
  function toolLine(txt, cls) {
    var m = current ? current.el : addMsg("bella", "Bella");
    var t = m.querySelector(".tool") || m.appendChild(el("div", "tool"));
    var span = el("span", cls || "");
    span.textContent = (t.textContent ? "  " : "") + txt;
    t.appendChild(span);
    scroll();
  }
  function errorMsg(txt) {
    endAssistant();
    var m = addMsg("err", "Bella");
    m.querySelector(".body").textContent = txt;
  }

  // ---- device panel ----
  function renderDevices(list) {
    devicesEl.innerHTML = "";
    (list || []).forEach(function (d) {
      var wrap = el("div", "dev");
      var n = el("div", "n");
      var dt = el("span", "dot" + (d.connected ? " on" : ""));
      n.appendChild(dt);
      n.appendChild(document.createTextNode(d.device_name + (d.is_local ? " (server)" : "")));
      wrap.appendChild(n);
      wrap.appendChild(el("div", "s", d.platform + " · " + (d.capabilities || []).length + " capabilities"));
      devicesEl.appendChild(wrap);
    });
  }

  // ---- device-tool execution (this browser) ----
  function runDeviceTool(tool, args) {
    try {
      if (tool === "open_browser") {
        var url = String(args.url || "");
        if (!/^https?:\/\//.test(url) && !/^file:/.test(url)) url = "https://" + url;
        window.open(url, "_blank", "noopener");
        return { success: true, url: url };
      }
      if (tool === "notification" || tool === "notify") {
        var title = args.title || "Bella";
        var body = args.body || args.message || args.text || "";
        if ("Notification" in window) {
          if (Notification.permission === "granted") new Notification(title, { body: body });
          else Notification.requestPermission();
        }
        return { success: true };
      }
      if (tool === "clipboard" && navigator.clipboard) {
        navigator.clipboard.writeText(String(args.text || ""));
        return { success: true };
      }
      return { error: "this browser can't do '" + tool + "'" };
    } catch (e) {
      return { error: String(e) };
    }
  }

  // ---- websocket ----
  var ws = null, backoff = 1, stopped = false;

  function wsURL() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws?token=" + encodeURIComponent(getToken());
  }
  function setConn(state) {
    dot.className = "statusdot " + (state === "online" ? "online" : state === "connecting" ? "connecting" : "offline");
    connEl.textContent = state;
  }
  function send(type, payload, requestId) {
    if (!ws || ws.readyState !== 1) return false;
    ws.send(JSON.stringify({
      type: type, v: PROTOCOL_VERSION, request_id: requestId || "",
      device_id: DEVICE.device_id, ts: Date.now() / 1000, payload: payload || {}
    }));
    return true;
  }

  function connect() {
    if (stopped) return;
    setConn("connecting");
    ws = new WebSocket(wsURL());

    ws.onopen = function () {
      send("hello", DEVICE);
    };
    ws.onmessage = function (ev) {
      var env;
      try { env = JSON.parse(ev.data); } catch (e) { return; }
      handle(env);
    };
    ws.onclose = function (ev) {
      setConn("offline");
      endAssistant();
      if (ev.code === 4401) {
        localStorage.removeItem(TOKEN_KEY);
        errorMsg("Unauthorized — reload and enter the token again.");
        stopped = true;
        return;
      }
      if (!stopped) {
        var d = Math.min(30, backoff);
        backoff = Math.min(30, backoff * 2);
        connEl.textContent = "reconnecting in " + d + "s";
        setTimeout(connect, d * 1000);
      }
    };
    ws.onerror = function () { /* onclose handles retry */ };

    // heartbeat
    var hb = setInterval(function () {
      if (!ws || ws.readyState !== 1) { clearInterval(hb); return; }
      send("heartbeat", {});
    }, 25000);
  }

  function handle(env) {
    var p = env.payload || {};
    switch (env.type) {
      case "connected":
        backoff = 1;
        setConn("online");
        deviceEl.textContent = DEVICE.device_name;
        modelEl.textContent = p.server || "";
        srvStatEl.textContent = "online · " + (p.server || "");
        sendBtn.disabled = false;
        break;
      case "devices":
        renderDevices(p.devices);
        break;
      case "thinking":
        break;
      case "assistant_start":
        beginAssistant();
        break;
      case "assistant_chunk":
        appendChunk(p.text || "");
        break;
      case "assistant_end":
        if (!current && p.reply) { beginAssistant(); appendChunk(p.reply); }
        endAssistant();
        sendBtn.disabled = false;
        input.focus();
        break;
      case "tool_start":
        toolLine("◈ " + (p.activity || p.title || p.tool) + "…");
        break;
      case "tool_end":
        toolLine((p.ok ? "✓ " : "✗ ") + p.tool + (p.ok ? "" : "  " + (p.error || "")),
                 p.ok ? "m-ok" : "m-err");
        break;
      case "device_tool_request":
        var res = runDeviceTool(p.tool, p.args || {});
        send("tool_result",
             { ok: !res.error, tool: p.tool, result: res.error ? {} : res, error: res.error || "" },
             env.request_id);
        break;
      case "notification":
        runDeviceTool("notification", p);
        break;
      case "error":
        errorMsg(p.message || "error");
        sendBtn.disabled = false;
        break;
      case "ping":
        break;
    }
  }

  // ---- submit ----
  function submit() {
    var text = input.value.trim();
    if (!text || !ws || ws.readyState !== 1) return;
    addMsg("user", "You").querySelector(".body").textContent = text;
    input.value = "";
    autosize();
    sendBtn.disabled = true;
    endAssistant();
    send("message", { text: text }, "r-" + Date.now().toString(36));
  }
  form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(140, input.scrollHeight) + "px";
  }
  input.addEventListener("input", autosize);

  sendBtn.disabled = true;
  connect();
})();
