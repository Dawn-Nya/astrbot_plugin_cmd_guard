/* 指令守卫 · 分机器人配置页面
 *
 * 运行在 AstrBot 的插件页面 iframe 中，通过注入的 window.AstrBotPluginPage
 * 桥接对象访问本插件注册的 Web API：
 *   GET  bots       -> { bots, fields, global, overrides, platforms, kv_ready }
 *   POST bots/save  -> { overrides, bots, kv_ready }
 */
(function () {
  "use strict";

  var state = {
    bots: [],
    fields: [],
    global: {},
    overrides: {},
    platforms: [],
    kvReady: true,
  };
  var selectedBotId = "";
  var draft = {}; // 仅保存“已覆盖”的配置项
  var saving = false;

  // ---------------------------------------------------------------- 工具

  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function waitForBridge() {
    return new Promise(function (resolve, reject) {
      var tries = 0;
      (function poll() {
        if (window.AstrBotPluginPage) {
          resolve(window.AstrBotPluginPage);
          return;
        }
        tries += 1;
        if (tries > 100) {
          reject(new Error("无法连接到 AstrBot 插件页面桥接（请刷新页面重试）。"));
          return;
        }
        setTimeout(poll, 50);
      })();
    });
  }

  var bridge = null;

  function toast(message, isError) {
    var el = $("toast");
    el.textContent = message;
    el.className = isError ? "toast error" : "toast";
    el.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () {
      el.hidden = true;
    }, isError ? 5000 : 2200);
  }

  function formatValue(field, value) {
    if (field.type === "bool") {
      return value ? "开启" : "关闭";
    }
    if (field.type === "list") {
      var list = Array.isArray(value) ? value : [];
      return list.length ? list.join("、") : "（空）";
    }
    var text = value === null || value === undefined ? "" : String(value);
    return text ? text : "（空）";
  }

  // ---------------------------------------------------------------- 数据

  function load() {
    return bridge
      .apiGet("bots")
      .then(function (data) {
        state.bots = (data && data.bots) || [];
        state.fields = (data && data.fields) || [];
        state.global = (data && data.global) || {};
        state.overrides = (data && data.overrides) || {};
        state.platforms = (data && data.platforms) || [];
        state.kvReady = !data || data.kv_ready !== false;
        if (!selectedBotId || !findBot(selectedBotId)) {
          selectedBotId = state.bots.length ? state.bots[0].id : "";
        }
        draft = cloneOverrides(selectedBotId);
        renderAll();
        if (!state.kvReady) {
          toast("警告：插件存储当前不可用，保存的配置在重启后可能丢失。", true);
        }
      })
      .catch(function (error) {
        toast("读取配置失败：" + (error && error.message ? error.message : error), true);
      });
  }

  function findBot(id) {
    for (var i = 0; i < state.bots.length; i += 1) {
      if (state.bots[i].id === id) {
        return state.bots[i];
      }
    }
    return null;
  }

  function cloneOverrides(botId) {
    var source = state.overrides[botId];
    var result = {};
    if (source && typeof source === "object") {
      Object.keys(source).forEach(function (key) {
        var value = source[key];
        result[key] = Array.isArray(value) ? value.slice() : value;
      });
    }
    return result;
  }

  function overrideCount() {
    return Object.keys(draft).length;
  }

  // ---------------------------------------------------------------- 渲染

  function renderAll() {
    renderBotSelect();
    renderBotMeta();
    renderFields();
    renderGlobal();
  }

  function renderBotSelect() {
    var select = $("bot-select");
    if (!state.bots.length) {
      select.innerHTML = '<option value="">未发现机器人</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.innerHTML = state.bots
      .map(function (bot) {
        var badge = bot.has_override ? " · 已独立配置" : "";
        var offline = bot.offline ? " · 离线" : "";
        var label =
          (bot.display_name || bot.id) +
          "（" + (bot.name || bot.id) + "）" +
          badge +
          offline;
        return (
          '<option value="' +
          esc(bot.id) +
          '"' +
          (bot.id === selectedBotId ? " selected" : "") +
          ">" +
          esc(label) +
          "</option>"
        );
      })
      .join("");
  }

  function renderBotMeta() {
    var el = $("bot-meta");
    var bot = findBot(selectedBotId);
    if (!bot) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    var parts = [];
    parts.push("平台类型：<b>" + esc(bot.name || "未知") + "</b>");
    parts.push("机器人 ID：<b>" + esc(bot.id) + "</b>");
    if (bot.description) {
      parts.push("说明：" + esc(bot.description));
    }
    var warnings = [];
    if (state.platforms && state.platforms.length) {
      var supported = state.platforms.indexOf(bot.name) >= 0;
      if (!supported) {
        warnings.push(
          "该机器人所属平台类型不在全局「生效平台」中，插件不会处理它的消息；" +
            "请到插件配置页把 " +
            esc(bot.name) +
            " 加入生效平台。"
        );
      }
    }
    if (bot.offline) {
      warnings.push("该机器人当前不在线，其配置会被保留，重新上线后自动生效。");
    }
    el.innerHTML =
      "<div>" + parts.join("　|　") + "</div>" +
      warnings
        .map(function (text) {
          return '<div class="warn">⚠ ' + text + "</div>";
        })
        .join("");
    el.hidden = false;
  }

  function renderFields() {
    var card = $("config-card");
    var container = $("fields");
    if (!selectedBotId) {
      card.hidden = true;
      container.innerHTML = "";
      return;
    }
    card.hidden = false;

    var chip = $("override-count");
    var count = overrideCount();
    chip.textContent = count ? "已覆盖 " + count + " 项" : "全部继承全局";
    chip.className = count ? "chip on" : "chip";

    container.innerHTML = state.fields
      .map(function (field) {
        var overridden = Object.prototype.hasOwnProperty.call(draft, field.key);
        var globalValue = state.global[field.key];
        var currentValue = overridden ? draft[field.key] : globalValue;
        var control = "";

        if (field.type === "bool") {
          control =
            '<label class="switch">' +
            '<input type="checkbox" data-key="' +
            esc(field.key) +
            '"' +
            (currentValue ? " checked" : "") +
            " />" +
            '<span class="track"></span>' +
            "</label>";
        } else if (field.type === "list") {
          var text = Array.isArray(currentValue) ? currentValue.join("\n") : "";
          control =
            '<textarea class="textarea" data-key="' +
            esc(field.key) +
            '" placeholder="每行一项，留空表示（空）">' +
            esc(text) +
            "</textarea>";
        } else {
          control =
            '<input class="input" type="text" data-key="' +
            esc(field.key) +
            '" value="' +
            esc(currentValue === null || currentValue === undefined ? "" : currentValue) +
            '" />';
        }

        return (
          '<div class="field">' +
          '<div class="field-text">' +
          '<div class="field-label">' +
          esc(field.label || field.key) +
          '<span class="chip' +
          (overridden ? " on" : "") +
          '">' +
          (overridden ? "已覆盖" : "继承全局") +
          "</span>" +
          "</div>" +
          (field.hint ? '<div class="field-hint">' + esc(field.hint) + "</div>" : "") +
          '<div class="field-global">全局值：' +
          esc(formatValue(field, globalValue)) +
          "</div>" +
          "</div>" +
          '<div class="field-control">' +
          control +
          '<button type="button" class="reset-link" data-reset="' +
          esc(field.key) +
          '"' +
          (overridden ? "" : " disabled") +
          ">恢复继承全局</button>" +
          "</div>" +
          "</div>"
        );
      })
      .join("");

    bindFieldEvents(container);
  }

  function renderGlobal() {
    var el = $("global-list");
    el.innerHTML = state.fields
      .map(function (field) {
        return (
          '<div class="global-item"><b>' +
          esc(field.label || field.key) +
          "</b>：" +
          esc(formatValue(field, state.global[field.key])) +
          "</div>"
        );
      })
      .join("");
  }

  function fieldByKey(key) {
    for (var i = 0; i < state.fields.length; i += 1) {
      if (state.fields[i].key === key) {
        return state.fields[i];
      }
    }
    return null;
  }

  function parseListValue(raw) {
    return String(raw || "")
      .split(/[\n,，、]/)
      .map(function (item) {
        return item.trim();
      })
      .filter(function (item) {
        return item.length > 0;
      });
  }

  function bindFieldEvents(container) {
    Array.prototype.forEach.call(
      container.querySelectorAll("input[data-key], textarea[data-key]"),
      function (element) {
        var key = element.getAttribute("data-key");
        var field = fieldByKey(key);
        if (!field) {
          return;
        }
        var handler = function () {
          if (field.type === "bool") {
            draft[key] = !!element.checked;
          } else if (field.type === "list") {
            draft[key] = parseListValue(element.value);
          } else {
            draft[key] = element.value;
          }
          renderFields();
        };
        element.addEventListener(field.type === "bool" ? "change" : "change", handler);
      }
    );
    Array.prototype.forEach.call(
      container.querySelectorAll("button[data-reset]"),
      function (button) {
        button.addEventListener("click", function () {
          delete draft[button.getAttribute("data-reset")];
          renderFields();
        });
      }
    );
  }

  // ---------------------------------------------------------------- 交互

  function bindStaticEvents() {
    $("bot-select").addEventListener("change", function (event) {
      selectedBotId = event.target.value;
      draft = cloneOverrides(selectedBotId);
      renderAll();
    });

    $("save").addEventListener("click", function () {
      if (saving || !selectedBotId) {
        return;
      }
      saving = true;
      $("save").disabled = true;
      bridge
        .apiPost("bots/save", { platform_id: selectedBotId, overrides: draft })
        .then(function (data) {
          state.overrides = (data && data.overrides) || {};
          state.bots = (data && data.bots) || state.bots;
          state.kvReady = !data || data.kv_ready !== false;
          draft = cloneOverrides(selectedBotId);
          renderAll();
          toast(overrideCount() ? "已保存该机器人的独立配置。" : "已恢复为继承全局配置。");
        })
        .catch(function (error) {
          toast("保存失败：" + (error && error.message ? error.message : error), true);
        })
        .then(function () {
          saving = false;
          $("save").disabled = false;
        });
    });

    $("reset").addEventListener("click", function () {
      draft = {};
      renderFields();
      toast("已清空该机器人的覆盖项，点击「保存」后生效。");
    });

    $("reload").addEventListener("click", function () {
      load();
    });
  }

  // ---------------------------------------------------------------- 启动

  waitForBridge()
    .then(function (api) {
      bridge = api;
      bindStaticEvents();
      return bridge.ready();
    })
    .then(function () {
      return load();
    })
    .catch(function (error) {
      toast(error && error.message ? error.message : String(error), true);
    });
})();
