(function () {
    "use strict";

    const tree = document.querySelector(".catalog-tree");
    if (!tree) return;
    const apiUrl = tree.dataset.apiUrl;

    function escapeHtml(s) {
        const d = document.createElement("div");
        d.textContent = s;
        return d.innerHTML;
    }

    function encPath(path) {
        return path.split("/").map(encodeURIComponent).join("/");
    }

    function folderNode(f) {
        return (
            '<li class="catalog-folder" data-path="' + escapeHtml(f.path) + '" data-loaded="0">' +
            '<div class="catalog-row catalog-toggle">' +
            '<i class="catalog-chevron fa fa-chevron-right"></i>' +
            '<i class="fa fa-folder"></i>' +
            '<span class="catalog-name">' + escapeHtml(f.name) + "</span></div>" +
            '<ul class="catalog-children" hidden></ul></li>'
        );
    }

    function fileNode(f) {
        const url = (f.is_pdf ? "/library/doc/" : "/library/download/") + encPath(f.path);
        return (
            '<li class="catalog-file"><div class="catalog-row">' +
            '<i class="catalog-chevron-spacer"></i>' +
            '<i class="fa fa-file-pdf"></i>' +
            '<a class="catalog-name" href="' + url + '">' + escapeHtml(f.name) + "</a>" +
            "</div></li>"
        );
    }

    function render(container, data) {
        const html =
            (data.folders || []).map(folderNode).join("") +
            (data.files || []).map(fileNode).join("");
        container.innerHTML = html;
    }

    tree.addEventListener("click", async (e) => {
        const toggle = e.target.closest(".catalog-toggle");
        if (!toggle || !tree.contains(toggle)) return;
        const li = toggle.closest(".catalog-folder");
        const children = li.querySelector(":scope > .catalog-children");
        const chevron = toggle.querySelector(".catalog-chevron");

        if (li.dataset.loaded === "0") {
            try {
                const r = await fetch(
                    apiUrl + "?path=" + encodeURIComponent(li.dataset.path)
                );
                if (r.ok) {
                    render(children, await r.json());
                    li.dataset.loaded = "1";
                }
            } catch (err) {
                return;
            }
        }

        const show = children.hidden;
        children.hidden = !show;
        chevron.classList.toggle("fa-chevron-down", show);
        chevron.classList.toggle("fa-chevron-right", !show);
    });
})();
