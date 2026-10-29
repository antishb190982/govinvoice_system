document.querySelectorAll("[data-folder-files]").forEach((input) => {
  input.addEventListener("change", () => {
    input.form.elements.folder_paths.value = JSON.stringify(
      Array.from(input.files).map((file) => file.webkitRelativePath || file.name)
    );
  });
});
