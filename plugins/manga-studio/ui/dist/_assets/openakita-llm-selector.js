(function () {
  "use strict";
  var script = document.currentScript;
  var pluginId = script && script.dataset ? script.dataset.pluginId : "";
  if (!pluginId || document.getElementById("openakita-llm-selector")) return;

  var root = document.createElement("section");
  root.id = "openakita-llm-selector";
  root.setAttribute("aria-label", "OpenAkita text model settings");
  root.innerHTML =
    '<button class="oa-llm-toggle" type="button" aria-expanded="false">AI 模型</button>' +
    '<div class="oa-llm-panel" hidden>' +
    '<label for="oa-llm-select">OpenAkita 文本模型</label>' +
    '<select id="oa-llm-select" disabled><option>正在读取模型…</option></select>' +
    '<p class="oa-llm-help">“跟随当前模型”使用 OpenAkita 当前选择；指定模型后将严格锁定，不会静默回退。费用由所选 OpenAkita 端点决定。</p>' +
    '<div class="oa-llm-actions"><span class="oa-llm-status" role="status"></span>' +
    '<button class="oa-llm-save" type="button" disabled>保存</button></div></div>';
  var style = document.createElement("style");
  style.textContent =
    "#openakita-llm-selector{position:fixed;right:18px;bottom:18px;z-index:2147483000;font:14px/1.4 system-ui,sans-serif;color:#172033}" +
    ".oa-llm-toggle,.oa-llm-save{border:0;border-radius:9px;background:#2563eb;color:#fff;padding:9px 14px;cursor:pointer;box-shadow:0 4px 18px #0002}" +
    ".oa-llm-panel{position:absolute;right:0;bottom:46px;width:min(360px,calc(100vw - 36px));box-sizing:border-box;padding:16px;border:1px solid #d9dfeb;border-radius:12px;background:#fff;box-shadow:0 14px 40px #0003}" +
    ".oa-llm-panel label{display:block;font-weight:650;margin-bottom:8px}.oa-llm-panel select{width:100%;box-sizing:border-box;padding:9px;border:1px solid #b8c1d1;border-radius:8px;background:#fff;color:#172033}" +
    ".oa-llm-help{margin:8px 0;color:#667085;font-size:12px}.oa-llm-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.oa-llm-status{font-size:12px;color:#667085}.oa-llm-save:disabled{opacity:.55;cursor:not-allowed}" +
    "@media(prefers-color-scheme:dark){.oa-llm-panel{background:#151a24;color:#eef2ff;border-color:#364152}.oa-llm-panel select{background:#202735;color:#eef2ff;border-color:#536078}.oa-llm-help,.oa-llm-status{color:#aeb8ca}}";
  document.head.appendChild(style);
  document.body.appendChild(root);

  var toggle = root.querySelector(".oa-llm-toggle");
  var panel = root.querySelector(".oa-llm-panel");
  var select = root.querySelector("select");
  var save = root.querySelector(".oa-llm-save");
  var status = root.querySelector(".oa-llm-status");
  var base = "/api/plugins/" + encodeURIComponent(pluginId) + "/_admin";

  toggle.addEventListener("click", function () {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
  });

  function show(message, error) {
    status.textContent = message || "";
    status.style.color = error ? "#dc2626" : "";
  }

  async function load() {
    try {
      var response = await fetch(base + "/llm-models", { credentials: "same-origin" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      var payload = (await response.json()).data || {};
      select.innerHTML = "";
      var inherited = document.createElement("option");
      inherited.value = "";
      inherited.textContent = "跟随 OpenAkita 当前模型";
      select.appendChild(inherited);
      (payload.models || []).forEach(function (model) {
        var option = document.createElement("option");
        option.value = model.endpoint;
        option.textContent = model.endpoint + " · " + model.model + (model.provider ? " · " + model.provider : "") + (model.local ? " · 本地" : "");
        option.disabled = !model.healthy;
        select.appendChild(option);
      });
      var selected = payload.selected_endpoint || "";
      if (selected && !Array.prototype.some.call(select.options, function (option) { return option.value === selected; })) {
        var stale = document.createElement("option");
        stale.value = selected;
        stale.textContent = selected + " · 已失效，请重新选择";
        select.appendChild(stale);
      }
      select.value = selected;
      select.disabled = false;
      save.disabled = false;
      show(payload.available ? "" : "OpenAkita 文本模型暂不可用", !payload.available);
    } catch (error) {
      select.innerHTML = "<option>模型目录读取失败</option>";
      show(String(error), true);
    }
  }

  save.addEventListener("click", async function () {
    save.disabled = true;
    show("正在保存…");
    try {
      var response = await fetch(base + "/config", {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ llm_endpoint: select.value }),
      });
      if (!response.ok) {
        var body = await response.text();
        throw new Error(body || "HTTP " + response.status);
      }
      show("已保存");
    } catch (error) {
      show(String(error), true);
    } finally {
      save.disabled = false;
    }
  });

  load();
})();
