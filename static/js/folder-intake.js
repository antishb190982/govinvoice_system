document.querySelectorAll("[data-folder-files]").forEach((input) => {
  input.addEventListener("change", () => {
    input.form.elements.folder_paths.value = JSON.stringify(
      Array.from(input.files).map((file) => file.webkitRelativePath || file.name)
    );
  });
});

document.querySelectorAll("form[data-intake-upload]").forEach((form) => {
  const inputs = Array.from(form.querySelectorAll('input[type="file"]'));
  const summary = form.querySelector("[data-intake-summary]");
  const list = form.querySelector("[data-intake-files]");
  const confirmation = form.elements.confirm_processing;
  const update = () => {
    const files = inputs.flatMap((input) => Array.from(input.files || []));
    if (confirmation) confirmation.checked = false;
    if (summary) {
      const size = files.reduce((total, file) => total + file.size, 0);
      summary.textContent = files.length
        ? `${files.length} selected file${files.length === 1 ? "" : "s"} · ${(size / 1024 / 1024).toFixed(2)} MB. Review this selection before confirming. ZIP contents are checked during processing.`
        : "No files selected. Choose files or a folder above.";
    }
    if (list) {
      list.replaceChildren();
      files.slice(0, 10).forEach((file) => {
        const item = document.createElement("li");
        item.textContent = file.webkitRelativePath || file.name;
        list.appendChild(item);
      });
      if (files.length > 10) {
        const item = document.createElement("li");
        item.textContent = `And ${files.length - 10} more selected files.`;
        list.appendChild(item);
      }
    }
  };
  inputs.forEach((input) => input.addEventListener("change", update));
  update();
});
