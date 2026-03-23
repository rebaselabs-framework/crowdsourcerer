/**
 * CrowdSorcerer Embeddable Widget v1.0
 * Embed a live task feed on any website.
 *
 * Usage:
 *   <div id="cs-widget"></div>
 *   <script src="https://crowdsourcerer.rebaselabs.online/widget.js"
 *     data-container="#cs-widget"
 *     data-limit="6"
 *     data-type=""
 *     data-theme="dark"
 *     data-cta-url="https://crowdsourcerer.rebaselabs.online/register">
 *   </script>
 */
(function () {
  "use strict";

  var WIDGET_URL = "https://crowdsourcerer.rebaselabs.online";

  // ─── Config from script tag ─────────────────────────────────────────────
  var scripts = document.querySelectorAll(
    'script[src*="widget.js"], script[data-container]'
  );
  var scriptEl = scripts[scripts.length - 1];

  function cfg(key, fallback) {
    return (scriptEl && scriptEl.getAttribute("data-" + key)) || fallback;
  }

  var containerSel = cfg("container", "#cs-widget");
  var limit = Math.min(parseInt(cfg("limit", "6"), 10) || 6, 12);
  var typeFilter = cfg("type", "");
  var theme = cfg("theme", "dark");
  var ctaUrl = cfg("cta-url", WIDGET_URL + "/register");
  var title = cfg("title", "Open Tasks — Earn Credits");
  var autoRefresh = cfg("auto-refresh", "60"); // seconds
  var refreshInterval = parseInt(autoRefresh, 10) || 60;

  // ─── Task type metadata ─────────────────────────────────────────────────
  var TASK_META = {
    label_image:          { label: "Label Image",        icon: "🖼️",  est: "2" },
    label_text:           { label: "Classify Text",      icon: "📝",  est: "2" },
    rate_quality:         { label: "Rate Quality",        icon: "⭐",  est: "1" },
    verify_fact:          { label: "Verify Fact",         icon: "✅",  est: "2" },
    moderate_content:     { label: "Moderate Content",    icon: "🛡️",  est: "3" },
    compare_rank:         { label: "A/B Compare",         icon: "⚖️",  est: "1" },
    answer_question:      { label: "Answer Question",     icon: "💬",  est: "3" },
    transcription_review: { label: "Review Transcript",  icon: "🎙️", est: "5" },
  };

  // ─── Styles ─────────────────────────────────────────────────────────────
  var DARK_STYLES = {
    bg: "#0d0d1a",
    border: "#2d2d4a",
    cardBg: "#13131f",
    cardBorder: "#2d2d4a",
    cardHover: "#1e1e35",
    text: "#f1f0ff",
    textMuted: "#8b8baa",
    accent: "#8b5cf6",
    accentText: "#c4b5fd",
    green: "#34d399",
    greenBg: "rgba(52,211,153,0.12)",
    radius: "12px",
    font: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  };

  var LIGHT_STYLES = {
    bg: "#fafafa",
    border: "#e5e7eb",
    cardBg: "#ffffff",
    cardBorder: "#e5e7eb",
    cardHover: "#f3f4f6",
    text: "#111827",
    textMuted: "#6b7280",
    accent: "#7c3aed",
    accentText: "#7c3aed",
    green: "#059669",
    greenBg: "rgba(5,150,105,0.1)",
    radius: "12px",
    font: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  };

  var s = theme === "light" ? LIGHT_STYLES : DARK_STYLES;

  function injectStyles() {
    if (document.getElementById("cs-widget-styles")) return;
    var style = document.createElement("style");
    style.id = "cs-widget-styles";
    style.textContent = [
      ".cs-widget { font-family: " + s.font + "; background: " + s.bg + "; border: 1px solid " + s.border + "; border-radius: " + s.radius + "; padding: 16px; box-sizing: border-box; color: " + s.text + "; }",
      ".cs-widget * { box-sizing: border-box; }",
      ".cs-widget-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }",
      ".cs-widget-title { font-size: 13px; font-weight: 600; color: " + s.textMuted + "; text-transform: uppercase; letter-spacing: 0.05em; }",
      ".cs-widget-badge { font-size: 11px; background: " + s.accent + "22; color: " + s.accentText + "; padding: 2px 8px; border-radius: 99px; font-weight: 600; }",
      ".cs-task-list { display: flex; flex-direction: column; gap: 8px; }",
      ".cs-task-card { display: flex; align-items: center; gap: 12px; background: " + s.cardBg + "; border: 1px solid " + s.cardBorder + "; border-radius: 8px; padding: 10px 12px; text-decoration: none; color: inherit; transition: background 0.15s, border-color 0.15s; cursor: pointer; }",
      ".cs-task-card:hover { background: " + s.cardHover + "; border-color: " + s.accent + "66; }",
      ".cs-task-icon { font-size: 20px; line-height: 1; flex-shrink: 0; }",
      ".cs-task-info { flex: 1; min-width: 0; }",
      ".cs-task-label { font-size: 13px; font-weight: 600; color: " + s.text + "; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
      ".cs-task-meta { font-size: 11px; color: " + s.textMuted + "; margin-top: 2px; }",
      ".cs-task-reward { flex-shrink: 0; text-align: right; }",
      ".cs-task-credits { font-size: 14px; font-weight: 700; color: " + s.green + "; background: " + s.greenBg + "; padding: 4px 8px; border-radius: 6px; white-space: nowrap; }",
      ".cs-widget-footer { margin-top: 14px; text-align: center; }",
      ".cs-widget-cta { display: inline-block; background: " + s.accent + "; color: #fff; font-size: 13px; font-weight: 600; padding: 9px 20px; border-radius: 8px; text-decoration: none; transition: opacity 0.15s; }",
      ".cs-widget-cta:hover { opacity: 0.88; }",
      ".cs-widget-powered { font-size: 10px; color: " + s.textMuted + "; margin-top: 8px; }",
      ".cs-widget-powered a { color: " + s.accentText + "; text-decoration: none; }",
      ".cs-widget-loading { text-align: center; padding: 24px; color: " + s.textMuted + "; font-size: 13px; }",
      ".cs-widget-error { text-align: center; padding: 16px; color: " + s.textMuted + "; font-size: 12px; }",
      ".cs-widget-empty { text-align: center; padding: 20px; font-size: 13px; color: " + s.textMuted + "; }",
    ].join("\n");
    document.head.appendChild(style);
  }

  // ─── Fetch tasks ─────────────────────────────────────────────────────────
  function fetchTasks(callback) {
    var apiUrl = WIDGET_URL + "/api/widget/tasks?limit=" + limit;
    if (typeFilter) apiUrl += "&type=" + encodeURIComponent(typeFilter);

    var xhr = new XMLHttpRequest();
    xhr.open("GET", apiUrl, true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          var data = JSON.parse(xhr.responseText);
          callback(null, data);
        } catch (e) {
          callback(new Error("Parse error"), null);
        }
      } else {
        callback(new Error("HTTP " + xhr.status), null);
      }
    };
    xhr.onerror = function () { callback(new Error("Network error"), null); };
    xhr.send();
  }

  // ─── Render ──────────────────────────────────────────────────────────────
  function render(container, tasks, total) {
    var items = tasks || [];

    var taskHtml = "";
    if (items.length === 0) {
      taskHtml = '<div class="cs-widget-empty">🔮 No open tasks right now.<br><small>Check back soon!</small></div>';
    } else {
      for (var i = 0; i < items.length; i++) {
        var task = items[i];
        var meta = TASK_META[task.type] || { label: task.type, icon: "🔮", est: "?" };
        var reward = task.reward || task.worker_reward_credits || "?";
        var taskUrl = WIDGET_URL + "/tasks";
        taskHtml += [
          '<a href="' + taskUrl + '" target="_blank" rel="noopener" class="cs-task-card">',
          '  <span class="cs-task-icon">' + meta.icon + '</span>',
          '  <div class="cs-task-info">',
          '    <div class="cs-task-label">' + escHtml(meta.label) + '</div>',
          '    <div class="cs-task-meta">~' + meta.est + ' min · ' + (task.slots || 1) + ' slot' + (task.slots !== 1 ? "s" : "") + ' open</div>',
          '  </div>',
          '  <div class="cs-task-reward">',
          '    <span class="cs-task-credits">+' + reward + ' cr</span>',
          '  </div>',
          '</a>',
        ].join("");
      }
    }

    var totalText = total > 0 ? (total + " task" + (total !== 1 ? "s" : "") + " open") : "";

    container.innerHTML = [
      '<div class="cs-widget">',
      '  <div class="cs-widget-header">',
      '    <span class="cs-widget-title">' + escHtml(title) + '</span>',
      totalText ? '    <span class="cs-widget-badge">' + totalText + '</span>' : "",
      '  </div>',
      '  <div class="cs-task-list">' + taskHtml + '</div>',
      '  <div class="cs-widget-footer">',
      '    <a href="' + escHtml(ctaUrl) + '" target="_blank" rel="noopener" class="cs-widget-cta">Start earning →</a>',
      '    <div class="cs-widget-powered">Powered by <a href="' + WIDGET_URL + '" target="_blank" rel="noopener">CrowdSorcerer</a></div>',
      '  </div>',
      '</div>',
    ].join("");
  }

  function renderLoading(container) {
    container.innerHTML = '<div class="cs-widget"><div class="cs-widget-loading">Loading tasks...</div></div>';
  }

  function renderError(container) {
    container.innerHTML = [
      '<div class="cs-widget">',
      '  <div class="cs-widget-error">Could not load tasks right now.</div>',
      '  <div class="cs-widget-footer">',
      '    <a href="' + escHtml(ctaUrl) + '" target="_blank" rel="noopener" class="cs-widget-cta">View tasks →</a>',
      '    <div class="cs-widget-powered">Powered by <a href="' + WIDGET_URL + '" target="_blank" rel="noopener">CrowdSorcerer</a></div>',
      '  </div>',
      '</div>',
    ].join("");
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ─── Init ────────────────────────────────────────────────────────────────
  function init() {
    var container = document.querySelector(containerSel);
    if (!container) {
      // Try to auto-create if we have an ID
      if (containerSel.charAt(0) === "#") {
        container = document.createElement("div");
        container.id = containerSel.slice(1);
        document.currentScript
          ? document.currentScript.parentNode.insertBefore(container, document.currentScript)
          : document.body.appendChild(container);
      } else {
        console.warn("[CrowdSorcerer Widget] Container not found:", containerSel);
        return;
      }
    }

    injectStyles();
    renderLoading(container);

    fetchTasks(function (err, data) {
      if (err || !data) {
        renderError(container);
      } else {
        render(container, data.items || [], data.total || 0);
      }
    });

    // Auto-refresh
    if (refreshInterval > 0) {
      setInterval(function () {
        fetchTasks(function (err, data) {
          if (!err && data) {
            render(container, data.items || [], data.total || 0);
          }
        });
      }, refreshInterval * 1000);
    }
  }

  // Wait for DOM
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // ─── Expose API for manual init ──────────────────────────────────────────
  window.CrowdSorcerer = window.CrowdSorcerer || {};
  window.CrowdSorcerer.initWidget = init;
})();
