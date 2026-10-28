/* GovBills UI behaviour. Loaded as an external file to satisfy the strict CSP. */
(function () {
  "use strict";

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // Validated categorical order: distinguishable for common colour-vision deficiencies.
  var PALETTE = ["#0b6e99", "#d97706", "#0f9d74", "#9b4dca", "#d1435b", "#5b6b85", "#2f9fd8", "#a47c1b", "#3d8b3d"];
  var UTILITY_COLOURS = {
    electricity: "#d97706", water: "#0b6e99", gas: "#d1435b", telecom: "#9b4dca", internet: "#2f9fd8",
    waste: "#5b6b85", fuel: "#a47c1b", cooling: "#0f9d74", other: "#8c96a8",
    in_approval: "#2f6fd8", approved: "#0f9d74", scheduled: "#9b4dca", other_status: "#8c96a8",
    amount: "#0b6e99", consumption: "#d97706", total: "#0b6e99"
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
    var btn = document.querySelector(".menu-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
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

  document.addEventListener("DOMContentLoaded", function () {
    renderCharts();
    wireConfirm();
    wireSelectAll();
    wireDecision();
    wireMenu();
    wireIdleWarning();
    wireAutoSubmit();
  });
})();
