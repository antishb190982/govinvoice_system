/* GovBills UI behaviour. Loaded as an external file to satisfy the strict CSP. */
(function () {
  "use strict";

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // Validated categorical order: distinguishable for common colour-vision deficiencies.
  var PALETTE = ["#2b3fd6", "#46bfc9", "#f08c2e", "#8b5cf6", "#e5484d", "#64709b", "#5b8cff", "#c9a227", "#1f9d6b"];
  var UTILITY_COLOURS = {
    electricity: "#f08c2e", water: "#2b8fd6", gas: "#e5484d", telecom: "#8b5cf6", internet: "#46bfc9",
    waste: "#64709b", fuel: "#c9a227", cooling: "#1f9d6b", other: "#9aa1c4",
    in_approval: "#2b3fd6", approved: "#1f9d6b", scheduled: "#8b5cf6", other_status: "#9aa1c4",
    amount: "#2b3fd6", consumption: "#46bfc9", total: "#2b3fd6"
  };

  function colourFor(key, i) {
    return UTILITY_COLOURS[key] || PALETTE[i % PALETTE.length];
  }

  function formatNumber(v) {
    var n = Number(v);
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function renderCharts() {
    if (typeof Chart === "undefined") return;
    Chart.defaults.font.family = cssVar("--font") || "system-ui";
    Chart.defaults.color = cssVar("--muted");
    Chart.defaults.borderColor = cssVar("--line");
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var source = document.getElementById(canvas.dataset.chart);
      if (!source) return;
      var data = JSON.parse(source.textContent);
      var type = canvas.dataset.type || "bar";
      var stacked = canvas.dataset.stacked === "true";
      var horizontal = canvas.dataset.horizontal === "true";
      var dual = canvas.dataset.dual === "true";
      var currency = canvas.dataset.currency || "";
      if (!data.series || !data.series.length) {
        var p = document.createElement("p");
        p.className = "empty";
        p.textContent = "No data for this selection yet.";
        canvas.replaceWith(p);
        return;
      }
      var datasets = data.series.map(function (s, i) {
        var colour = colourFor(s.key, i);
        var dsType = dual ? (i === 0 ? "bar" : "line") : type;
        var ds = {
          type: dsType, label: s.label, data: s.data, backgroundColor: colour, borderColor: colour,
          borderWidth: dsType === "line" ? 2 : 0, borderRadius: dsType === "bar" ? 3 : 0,
          tension: 0.25, pointRadius: dsType === "line" ? 2 : 0, fill: false, maxBarThickness: 42
        };
        if (dual) ds.yAxisID = i === 0 ? "y" : "y1";
        return ds;
      });
      var scales = {
        x: { stacked: stacked, grid: { display: horizontal } },
        y: { stacked: stacked, beginAtZero: true, grid: { display: !horizontal },
             ticks: { callback: function (v) { return horizontal ? this.getLabelForValue(v) : formatNumber(v); } } }
      };
      if (horizontal) {
        scales.x.ticks = { callback: function (v) { return formatNumber(v); } };
        scales.y.ticks = {};
      }
      if (dual) scales.y1 = { position: "right", beginAtZero: true, grid: { display: false },
                              ticks: { callback: function (v) { return formatNumber(v); } } };
      new Chart(canvas, {
        type: type,
        data: { labels: data.labels, datasets: datasets },
        options: {
          responsive: true, maintainAspectRatio: false, indexAxis: horizontal ? "y" : "x",
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: datasets.length > 1, position: "bottom", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
            tooltip: { callbacks: { label: function (ctx) {
              var v = horizontal ? ctx.parsed.x : ctx.parsed.y;
              return ctx.dataset.label + ": " + (currency ? currency + " " : "") +
                Number(v).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 3 });
            } } }
          },
          scales: scales
        }
      });
    });
  }

  function wireConfirm() {
    document.addEventListener("submit", function (e) {
      var form = e.target;
      var msg = form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) e.preventDefault();
    }, true);
  }

  function wireSelectAll() {
    document.querySelectorAll("input[data-select-all]").forEach(function (master) {
      var group = master.getAttribute("data-select-all");
      var boxes = document.querySelectorAll('input[data-group="' + group + '"]');
      function recalc() {
        var total = 0, count = 0;
        boxes.forEach(function (b) { if (b.checked) { count++; total += Number(b.dataset.amount || 0); } });
        var out = document.getElementById(group + "-summary");
        if (out) out.textContent = count + " selected · " + total.toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 });
      }
      master.addEventListener("change", function () { boxes.forEach(function (b) { b.checked = master.checked; }); recalc(); });
      boxes.forEach(function (b) { b.addEventListener("change", recalc); });
      recalc();
    });
  }

  function wireDecision() {
    var form = document.getElementById("decision-form");
    if (!form) return;
    var pinRow = document.getElementById("pin-row");
    var needsSig = form.dataset.signature === "true";
    function update() {
      var choice = form.querySelector('input[name="decision"]:checked');
      if (pinRow) pinRow.classList.toggle("hidden", !(needsSig && choice && choice.value === "approve"));
    }
    form.addEventListener("change", update);
    update();
  }

  function wireMenu() {
    var btn = document.querySelector(".nav-toggle");
    var side = document.getElementById("app-sidebar");
    function setNav(open) {
      var wasOpen = document.body.classList.contains("nav-open");
      document.body.classList.toggle("nav-open", open);
      if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open && side) side.focus();
      else if (!open && wasOpen && btn) btn.focus();
    }
    if (btn) btn.addEventListener("click", function () {
      setNav(!document.body.classList.contains("nav-open"));
    });
    document.querySelectorAll("[data-nav-close]").forEach(function (el) {
      el.addEventListener("click", function () { setNav(false); });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && document.body.classList.contains("nav-open")) setNav(false);
    });
    var menus = document.querySelectorAll("details.menu");
    menus.forEach(function (m) {
      m.addEventListener("toggle", function () {
        if (m.open) menus.forEach(function (o) { if (o !== m) o.open = false; });
      });
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest("details.menu")) menus.forEach(function (m) { m.open = false; });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") menus.forEach(function (m) { m.open = false; });
    });
  }

  function wireIdleWarning() {
    var minutes = Number(document.body.dataset.idleMinutes || 0);
    if (!minutes) return;
    var warnAt = Math.max(minutes - 2, 1) * 60 * 1000;
    var box = document.getElementById("idle-warning");
    var timer;
    function reset() {
      clearTimeout(timer);
      if (box) box.classList.add("hidden");
      timer = setTimeout(function () { if (box) box.classList.remove("hidden"); }, warnAt);
    }
    ["click", "keydown"].forEach(function (ev) { document.addEventListener(ev, reset, { passive: true }); });
    var stay = document.getElementById("idle-stay");
    if (stay) stay.addEventListener("click", function () { window.location.reload(); });
    reset();
  }

  function wireAutoSubmit() {
    document.querySelectorAll("select[data-autosubmit]").forEach(function (s) {
      s.addEventListener("change", function () { s.form.submit(); });
    });
  }


  function wireActivity() {
    var holder = document.querySelector("[data-activity-url]");
    var url = holder && holder.getAttribute("data-activity-url");
    if (!url || /\/login/.test(window.location.pathname)) return;
    var notice = document.getElementById("activity-notice");
    var lastId = null, stopped = false, busy = false, timer = null;
    function setCount(nodes, n) {
      n = Number(n) || 0;
      nodes.forEach(function (el) { el.textContent = String(n); el.hidden = n <= 0; });
    }
    function stop() { stopped = true; if (timer) clearInterval(timer); timer = null; }
    function poll() {
      if (stopped || busy || document.visibilityState !== "visible") return;
      busy = true;
      fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" }, redirect: "follow", cache: "no-store" })
        .then(function (r) {
          if (r.redirected || r.status === 401 || r.status === 403) { stop(); return null; }
          if (!r.ok) return null;
          return r.json();
        })
        .then(function (d) {
          if (!d) return;
          setCount(document.querySelectorAll("[data-notification-count]"), d.unread_notifications);
          if (d.pending_approvals !== undefined) setCount(document.querySelectorAll("[data-approval-count]"), d.pending_approvals);
          if (d.batch_folders) document.querySelectorAll("[data-batch-folder]").forEach(function (el) {
            var k = el.getAttribute("data-batch-folder");
            if (Object.prototype.hasOwnProperty.call(d.batch_folders, k)) el.textContent = String(Number(d.batch_folders[k]) || 0);
          });
          var id = d.latest_notification_id;
          if (lastId !== null && id && id !== lastId && notice) {
            var t = notice.querySelector("[data-activity-text]");
            if (t) t.textContent = "New activity: " + (Number(d.unread_notifications) || 0) + " unread";
            notice.hidden = false;
          }
          if (id !== undefined) lastId = id;
        })
        .catch(function () {})
        .then(function () { busy = false; });
    }
    poll();
    timer = setInterval(poll, 30000);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") poll(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderCharts();
    wireConfirm();
    wireSelectAll();
    wireDecision();
    wireMenu();
    wireIdleWarning();
    wireAutoSubmit();
    wireActivity();
  });
})();
