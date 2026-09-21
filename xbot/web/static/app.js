/* =========================================================================
   Bedienlogik der Oberfläche.

   Bewusst ohne Framework und ohne Build-Schritt: die Seite wird serverseitig
   gerendert, dieses Skript haelt nur die Werte aktuell und wickelt die
   Aufträge ab, die im Hintergrund laufen (Beitrag, Reaktion, Prüfung).
   ========================================================================= */
(function () {
  "use strict";

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var CSRF = csrfMeta ? csrfMeta.getAttribute("content") : "";

  /* ------------------------------------------------------------ Netzwerk */
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify(body || {}),
      credentials: "same-origin"
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) { throw new Error(data.error || "Anfrage fehlgeschlagen"); }
        return data;
      });
    });
  }

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (response.status === 401) { window.location.href = "/login"; throw new Error("abgemeldet"); }
      if (!response.ok) { throw new Error("Abfrage fehlgeschlagen"); }
      return response.json();
    });
  }

  /* -------------------------------------------------------- Ergebnisbox */
  function showResult(box, state, title, lines) {
    if (!box) { return; }
    box.hidden = false;
    var mark = state === "laeuft" ? '<span class="spinner"></span>'
             : state === "ok" ? '<span style="color:var(--good-text)">+</span>'
             : '<span style="color:var(--critical)">x</span>';
    var html = '<div class="title">' + mark + "<span>" + escapeHtml(title) + "</span></div>";
    if (lines && lines.length) {
      html += "<ul>";
      for (var i = 0; i < lines.length; i++) { html += "<li>" + escapeHtml(lines[i]) + "</li>"; }
      html += "</ul>";
    }
    box.innerHTML = html;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
    });
  }

  /* ------------------------------------------------------------ Aufträge */
  function pollTask(taskId, box, button, label) {
    getJSON("/api/task/" + encodeURIComponent(taskId)).then(function (task) {
      if (!task.done) {
        showResult(box, "laeuft", label + " laeuft ...");
        window.setTimeout(function () { pollTask(taskId, box, button, label); }, 900);
        return;
      }
      showResult(box, task.ok ? "ok" : "fehler", task.summary, task.detail);
      if (button) { button.disabled = false; }
      refreshStatus();
    }).catch(function (error) {
      showResult(box, "fehler", error.message);
      if (button) { button.disabled = false; }
    });
  }

  function wireTaskButtons() {
    var box = document.getElementById("task-result");
    var buttons = document.querySelectorAll("[data-run]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var kind = button.getAttribute("data-run");
        var label = button.getAttribute("data-label") || "Auftrag";
        var confirmText = button.getAttribute("data-confirm");
        if (confirmText && !window.confirm(confirmText)) { return; }
        button.disabled = true;
        showResult(box, "laeuft", label + " wurde gestartet ...");
        postJSON("/api/run/" + kind, {}).then(function (data) {
          pollTask(data.task.id, box, button, label);
        }).catch(function (error) {
          showResult(box, "fehler", error.message);
          button.disabled = false;
        });
      });
    });
  }

  /* -------------------------------------------------------- Steuerbefehle */
  function wireControls() {
    var box = document.getElementById("task-result");
    Array.prototype.forEach.call(document.querySelectorAll("[data-command]"), function (button) {
      button.addEventListener("click", function () {
        var command = button.getAttribute("data-command");
        var payload = { command: command };
        if (command === "mode") {
          payload.dry_run = button.getAttribute("data-dry-run") === "1";
          if (!payload.dry_run) {
            var warning = "Echtbetrieb einschalten?\n\n" +
              "Der Bot sendet danach wirklich auf X: Beiträge, Likes, Reposts und Antworten.";
            if (!window.confirm(warning)) { return; }
          }
        }
        button.disabled = true;
        postJSON("/api/control", payload).then(function (data) {
          showResult(box, "ok", data.message || "Übernommen.");
          window.setTimeout(function () { window.location.reload(); }, 600);
        }).catch(function (error) {
          showResult(box, "fehler", error.message);
          button.disabled = false;
        });
      });
    });
  }

  /* ------------------------------------------------------ Laufende Anzeige */
  function setText(id, value) {
    var node = document.getElementById(id);
    if (node) { node.textContent = value; }
  }

  /* Zahlen und Balken eines Plattformblocks auffrischen. "prefix" ist leer
     fuer X und "discord-" fuer Discord - die IDs im Markup heissen genauso. */
  function applyCounters(prefix, block) {
    if (!block) { return; }
    setText("stat-" + prefix + "total", block.today_total != null ? block.today_total : "-");

    var today = block.today || {};
    Object.keys(today).forEach(function (action) {
      setText("stat-" + prefix + action, today[action]);
    });

    var quota = block.quota || {};
    Object.keys(quota).forEach(function (action) {
      var usage = quota[action];
      var fill = document.getElementById("meter-" + prefix + action);
      var value = document.getElementById("meterval-" + prefix + action);
      if (fill) {
        var share = usage.limit_day ? Math.min(1, usage.used_day / usage.limit_day) : 0;
        fill.style.width = (share * 100).toFixed(1) + "%";
        fill.className = "meter-fill" + (share >= 0.9 ? " critical" : share >= 0.7 ? " warn" : "");
      }
      if (value) { value.textContent = usage.used_day + " / " + usage.limit_day; }
    });
  }

  function refreshStatus() {
    if (!document.getElementById("live-root")) { return; }
    getJSON("/api/status").then(function (data) {
      var snapshot = data.snapshot || {};
      applyCounters("", snapshot);
      applyCounters("discord-", snapshot.discord);

      (data.jobs || []).forEach(function (job, index) {
        var node = document.getElementById("job-next-" + index);
        if (node && job.next_run) { node.textContent = relativeTime(job.next_run); }
      });
    }).catch(function () { /* kurze Aussetzer still ignorieren */ });
  }

  function relativeTime(isoString) {
    var seconds = Math.round((new Date(isoString).getTime() - Date.now()) / 1000);
    if (seconds <= 0) { return "jetzt"; }
    if (seconds < 60) { return "in " + seconds + " Sek."; }
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) { return "in " + minutes + " Min."; }
    var hours = Math.floor(minutes / 60);
    var restMinutes = minutes % 60;
    if (hours < 24) { return restMinutes ? "in " + hours + " Std. " + restMinutes + " Min." : "in " + hours + " Std."; }
    var days = Math.floor(hours / 24);
    return "in " + days + " Tg. " + (hours % 24) + " Std.";
  }

  /* --------------------------------------------------------- Protokoll */
  function wireLogs() {
    var box = document.getElementById("logbox");
    if (!box) { return; }
    var toggle = document.getElementById("log-auto");
    var timer = null;

    function load() {
      getJSON("/api/logs?lines=300").then(function (data) {
        box.innerHTML = (data.lines || []).map(function (line) {
          var level = (line.match(/\b(ERROR|CRITICAL|WARNING)\b/) || [])[1];
          var text = escapeHtml(line);
          return level ? '<span class="lvl-' + level + '">' + text + "</span>" : text;
        }).join("\n");
        box.scrollTop = box.scrollHeight;
      }).catch(function () { /* still */ });
    }

    var reloadButton = document.getElementById("log-reload");
    if (reloadButton) { reloadButton.addEventListener("click", load); }
    if (toggle) {
      toggle.addEventListener("change", function () {
        if (toggle.checked) { timer = window.setInterval(load, 5000); load(); }
        else if (timer) { window.clearInterval(timer); timer = null; }
      });
      if (toggle.checked) { timer = window.setInterval(load, 5000); }
    }
    box.scrollTop = box.scrollHeight;
  }

  /* ----------------------------------------------------- Themenumschalter */
  function wireTheme() {
    var button = document.getElementById("theme-toggle");
    if (!button) { return; }
    var stored = null;
    try { stored = window.localStorage.getItem("xbot-theme"); } catch (error) { stored = null; }
    if (stored) { document.documentElement.setAttribute("data-theme", stored); }
    button.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var isDark = current ? current === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
      var next = isDark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { window.localStorage.setItem("xbot-theme", next); } catch (error) { /* egal */ }
    });
  }

  /* ---------------------------------------------------------------- Start */
  document.addEventListener("DOMContentLoaded", function () {
    wireTaskButtons();
    wireControls();
    wireLogs();
    wireTheme();
    if (document.getElementById("live-root")) {
      window.setInterval(refreshStatus, 10000);
    }
  });
})();
