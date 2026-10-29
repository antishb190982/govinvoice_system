/* Workflow step editor: add rows by cloning the Django formset empty_form; removal checks DELETE. */
(function () {
  "use strict";
  function init() {
    var root = document.getElementById("step-editor");
    if (!root) return;
    var prefix = root.getAttribute("data-prefix") || "steps";
    var total = document.getElementById("id_" + prefix + "-TOTAL_FORMS");
    var maxInput = document.getElementById("id_" + prefix + "-MAX_NUM_FORMS");
    var list = document.getElementById("step-list");
    var tpl = document.getElementById("step-template");
    var addBtn = document.getElementById("step-add");
    var counter = document.getElementById("step-live");
    if (!total || !list || !tpl || !addBtn) return;
    var max = Math.min(parseInt(maxInput && maxInput.value, 10) || 20, 20);

    function rows() { return Array.prototype.slice.call(list.querySelectorAll(".step-row")); }
    function live() {
      return rows().filter(function (r) {
        var d = r.querySelector("input[name$='-DELETE']");
        return !(d && d.checked);
      });
    }
    function refresh() {
      rows().forEach(function (r) {
        var d = r.querySelector("input[name$='-DELETE']");
        r.classList.toggle("is-deleted", !!(d && d.checked));
      });
      var n = live().length;
      addBtn.disabled = n >= max;
      if (counter) counter.textContent = n + " of " + max + " steps in use";
    }
    function nextOrder() {
      var m = 0;
      live().forEach(function (r) {
        var o = r.querySelector("input[name$='-order']");
        var v = o ? parseInt(o.value, 10) : 0;
        if (v > m) m = v;
      });
      return m + 1;
    }
    addBtn.addEventListener("click", function () {
      if (live().length >= max) return;
      var index = parseInt(total.value, 10) || 0;
      var html = tpl.innerHTML.replace(/__prefix__/g, String(index)).replace(/__num__/g, String(index + 1));
      var holder = document.createElement("div");
      holder.innerHTML = html.trim();
      var row = holder.firstElementChild;
      list.appendChild(row);
      total.value = String(index + 1);
      var order = row.querySelector("input[name$='-order']");
      if (order) order.value = String(nextOrder());
      var name = row.querySelector("input[name$='-name']");
      if (name) name.focus();
      refresh();
    });
    list.addEventListener("change", function (e) {
      if (e.target && /-DELETE$/.test(e.target.name || "")) refresh();
    });
    refresh();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
