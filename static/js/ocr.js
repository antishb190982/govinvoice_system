/* OCR review highlighting and zone template editor (CSP-safe external script). */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";

  function highlight(field, on) {
    document.querySelectorAll('rect.zone[data-field="' + field + '"]').forEach(function (r) {
      r.classList.toggle("hl", on);
      if (on && r.scrollIntoView) r.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  function wireReview() {
    var list = document.getElementById("field-list");
    if (!list) return;
    var current = null;
    list.querySelectorAll("li").forEach(function (li) {
      function pick() {
        if (current) { highlight(current.dataset.field, false); current.classList.remove("active"); }
        current = li; li.classList.add("active"); highlight(li.dataset.field, true);
      }
      li.addEventListener("click", pick);
      li.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    });
    document.querySelectorAll("rect.zone[data-field]").forEach(function (r) {
      r.addEventListener("click", function () {
        var li = list.querySelector('li[data-field="' + r.dataset.field + '"]');
        if (li) li.click();
      });
    });
  }

  function wireEditor() {
    var form = document.getElementById("zone-form");
    if (!form) return;
    var zones = JSON.parse(document.getElementById("zones-data").textContent || "{}");
    var input = document.getElementById("zones-input");
    var status = document.getElementById("editor-status");
    var buttons = document.querySelectorAll("#editor-fields button");
    var active = null;

    function sync() {
      input.value = JSON.stringify(zones);
      buttons.forEach(function (b) { b.textContent = b.textContent.replace(/ ✓$/, "") + (zones[b.dataset.field] ? " ✓" : ""); });
    }
    function draw() {
      document.querySelectorAll("svg.ocr-canvas").forEach(function (svg) {
        svg.querySelectorAll("rect.zone, text.zone-tag").forEach(function (n) { n.remove(); });
        var w = Number(svg.dataset.width), h = Number(svg.dataset.height), page = Number(svg.dataset.page);
        Object.keys(zones).forEach(function (field) {
          var z = zones[field];
          if (Number(z.page || 0) !== page) return;
          var r = document.createElementNS(SVGNS, "rect");
          r.setAttribute("class", "zone" + (field === active ? " hl" : ""));
          r.setAttribute("x", z.bbox[0] * w); r.setAttribute("y", z.bbox[1] * h);
          r.setAttribute("width", (z.bbox[2] - z.bbox[0]) * w); r.setAttribute("height", (z.bbox[3] - z.bbox[1]) * h);
          r.setAttribute("rx", 4);
          svg.appendChild(r);
          var t = document.createElementNS(SVGNS, "text");
          t.setAttribute("class", "zone-tag"); t.setAttribute("x", z.bbox[0] * w); t.setAttribute("y", z.bbox[1] * h - 8);
          var b = document.querySelector('#editor-fields button[data-field="' + field + '"]');
          t.textContent = b ? b.textContent.replace(/ ✓$/, "") : field;
          svg.appendChild(t);
        });
      });
    }
    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        active = b.dataset.field;
        buttons.forEach(function (o) { o.classList.toggle("active", o === b); });
        status.textContent = "Drag a box around the " + b.textContent.replace(/ ✓$/, "").toLowerCase() + " on the page.";
        draw();
      });
    });
    document.getElementById("zone-clear").addEventListener("click", function () {
      if (active && zones[active]) { delete zones[active]; sync(); draw(); }
    });

    document.querySelectorAll("svg.ocr-canvas").forEach(function (svg) {
      var start = null, rect = null;
      function point(e) {
        var pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
        return pt.matrixTransform(svg.getScreenCTM().inverse());
      }
      svg.addEventListener("pointerdown", function (e) {
        if (!active) { status.textContent = "Select a field first."; return; }
        start = point(e); svg.setPointerCapture(e.pointerId);
        rect = document.createElementNS(SVGNS, "rect"); rect.setAttribute("class", "zone drawing"); svg.appendChild(rect);
      });
      svg.addEventListener("pointermove", function (e) {
        if (!start) return;
        var p = point(e);
        rect.setAttribute("x", Math.min(start.x, p.x)); rect.setAttribute("y", Math.min(start.y, p.y));
        rect.setAttribute("width", Math.abs(p.x - start.x)); rect.setAttribute("height", Math.abs(p.y - start.y));
      });
      svg.addEventListener("pointerup", function (e) {
        if (!start) return;
        var p = point(e), w = Number(svg.dataset.width), h = Number(svg.dataset.height);
        var x0 = Math.max(0, Math.min(start.x, p.x) / w), y0 = Math.max(0, Math.min(start.y, p.y) / h);
        var x1 = Math.min(1, Math.max(start.x, p.x) / w), y1 = Math.min(1, Math.max(start.y, p.y) / h);
        start = null; rect.remove();
        if (x1 - x0 < 0.01 || y1 - y0 < 0.005) return;
        zones[active] = { page: Number(svg.dataset.page), bbox: [x0, y0, x1, y1].map(function (v) { return Math.round(v * 1e5) / 1e5; }) };
        status.textContent = "Zone set. Pick another field or save.";
        sync(); draw();
      });
    });
    sync(); draw();
  }

  document.addEventListener("DOMContentLoaded", function () { wireReview(); wireEditor(); });
})();
