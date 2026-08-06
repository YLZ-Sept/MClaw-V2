/**
 * OpenAkita Plugin UI Bootstrap (self-contained copy for word-maker).
 */
(function () {
  if (typeof window === "undefined") return;
  if (window.parent === window) return;
  if (window.OpenAkita && window.OpenAkita.__bootstrapped) return;

  var BRIDGE_VERSION = 1;
  var meta = { theme: "light", locale: "zh-CN", apiBase: "", pluginId: "word-maker" };
  var pending = Object.create(null);
  var renderReadySent = false;

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme || "light");
  }

  function send(type, payload, requestId) {
    var message = { __akita_bridge: true, version: BRIDGE_VERSION, type: type };
    if (payload !== undefined) message.payload = payload;
    if (requestId !== undefined) message.requestId = requestId;
    window.parent.postMessage(message, "*");
  }

  function dispatch(name, detail) {
    window.dispatchEvent(new CustomEvent(name, { detail: detail }));
  }

  function requestId() {
    return "wm" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  function bridgeRequest(type, payload) {
    return new Promise(function (resolve, reject) {
      var id = requestId();
      var timer = setTimeout(function () {
        if (!pending[id]) return;
        delete pending[id];
        reject(new Error("Bridge request timed out"));
      }, 30000);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      send(type, payload, id);
    });
  }

  function pluginPath(path) {
    if (/^\/api\/plugins\//.test(path)) return path;
    return "/api/plugins/" + encodeURIComponent(meta.pluginId) + "/" + path.replace(/^\//, "");
  }

  function request(path, options) {
    options = options || {};
    return bridgeRequest("bridge:api-request", {
      method: options.method || "GET",
      path: pluginPath(path),
      body: options.body,
    }).then(function (response) {
      if (!response || !response.ok) {
        var message = response && response.error;
        if (!message) message = "HTTP " + (response && response.status || 0);
        throw new Error(message);
      }
      return response.body;
    });
  }

  window.OpenAkita = {
    __bootstrapped: true,
    __ready: false,
    meta: meta,
    request: request,
    postMessage: send,
    ready: function () {
      if (renderReadySent) return;
      renderReadySent = true;
      send("bridge:render-ready");
    },
  };

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var message = event.data || {};
    if (message.__akita_bridge !== true) return;

    if (message.type === "bridge:init") {
      Object.assign(meta, message.payload || {});
      applyTheme(meta.theme);
      if (!window.OpenAkita.__ready) {
        window.OpenAkita.__ready = true;
        dispatch("openakita:ready", Object.assign({}, meta));
      }
      return;
    }
    if (message.type === "bridge:theme-change") {
      meta.theme = message.payload && message.payload.theme || "light";
      applyTheme(meta.theme);
      dispatch("openakita:theme-change", { theme: meta.theme });
      return;
    }
    if (message.type === "bridge:locale-change") {
      meta.locale = message.payload && message.payload.locale || "zh-CN";
      dispatch("openakita:locale-change", { locale: meta.locale });
      return;
    }
    if (message.type === "bridge:api-response" && message.requestId && pending[message.requestId]) {
      var resolver = pending[message.requestId];
      delete pending[message.requestId];
      clearTimeout(resolver.timer);
      resolver.resolve(message.payload || {});
    }
  });

  function handshake() {
    send("bridge:ready");
    send("bridge:handshake", { pluginId: meta.pluginId });
  }

  applyTheme(meta.theme);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", handshake, { once: true });
  } else {
    handshake();
  }
})();
