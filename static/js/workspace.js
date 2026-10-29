/* Presentation-only enhancements. No requests, financial writes or ownership changes. */
(function () {
  "use strict";
  function prepareWorkspace() {
    document.querySelectorAll("[data-attention-tab]").forEach(function (button) {
      button.addEventListener("click", function () {
        document.querySelectorAll("[data-attention-tab]").forEach(function (tab) {
          tab.setAttribute("aria-selected", String(tab === button));
        });
        document.querySelectorAll("[data-attention-feed]").forEach(function (panel) {
          panel.hidden = panel.dataset.attentionFeed !== button.dataset.attentionTab;
        });
      });
    });
    document.querySelectorAll(".mw-table").forEach(function (table) {
      var headings = Array.from(table.querySelectorAll("thead th"));
      if (!headings.length) return;
      table.classList.add("workspace-cards");
      table.setAttribute("role", "table");
      table.querySelectorAll("thead, tbody").forEach(function (group) {
        group.setAttribute("role", "rowgroup");
      });
      table.querySelectorAll("tr").forEach(function (row) { row.setAttribute("role", "row"); });
      headings.forEach(function (heading) { heading.setAttribute("role", "columnheader"); });
      table.querySelectorAll("tbody tr").forEach(function (row) {
        Array.from(row.cells).forEach(function (cell, index) {
          cell.setAttribute("role", "cell");
          if (cell.colSpan !== 1 || !headings[index]) return;
          var label = headings[index].textContent.trim();
          cell.dataset.label = label;
          cell.dataset.column = label.toLowerCase();
        });
      });
    });
    document.querySelectorAll(".table-wrap, .mw-table-wrap").forEach(function (region) {
      if (!region.hasAttribute("tabindex")) region.setAttribute("tabindex", "0");
      if (!region.hasAttribute("role")) region.setAttribute("role", "region");
      if (!region.hasAttribute("aria-label") && !region.hasAttribute("aria-labelledby")) {
        region.setAttribute("aria-label", "Records. Scroll horizontally if needed.");
      }
    });
  }
  document.addEventListener("govbills:attention", function (event) {
    var data = event.detail;
    document.querySelectorAll("[data-attention-count]").forEach(function (node) {
      node.textContent = String(data.total);
      if (node.classList.contains("dot")) node.hidden = data.total === 0;
    });
    document.querySelectorAll("[data-attention-overdue-count]").forEach(function (node) {
      node.textContent = String(data.overdue_count);
    });
    var toggle = document.querySelector(".attention-toggle");
    if (toggle) toggle.setAttribute("aria-label", "Notifications: " + data.unread_count + " unread alerts and " + data.overdue_count + " overdue work items");
    document.querySelectorAll("[data-attention-feed]").forEach(function (panel) {
      var items = panel.dataset.attentionFeed === "overdue" ? data.overdue : data.overdue.concat(data.alerts);
      panel.replaceChildren();
      if (!items.length) {
        var empty = document.createElement("p");
        empty.className = "attention-empty";
        empty.textContent = "No items in this notification view.";
        panel.appendChild(empty);
      }
      items.forEach(function (item) {
        var link = document.createElement("a"), symbol = document.createElement("span");
        var content = document.createElement("span"), title = document.createElement("strong");
        var body = document.createElement("small"), kind = document.createElement("small");
        link.className = "attention-item attention-" + item.level;
        link.href = item.url;
        symbol.className = "attention-symbol"; symbol.textContent = "!"; symbol.setAttribute("aria-hidden", "true");
        title.textContent = item.title; body.textContent = item.body; kind.textContent = item.kind;
        kind.className = "attention-kind";
        content.append(title, body, kind); link.append(symbol, content); panel.appendChild(link);
      });
    });
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", prepareWorkspace);
  } else {
    prepareWorkspace();
  }
})();
