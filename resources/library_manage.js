(function () {
    "use strict";

    const root = document.querySelector(".library-manage");
    if (!root) return;

    const cur = root.dataset.currentPath || "";
    const URLS = {
        create: root.dataset.urlCreate,
        upload: root.dataset.urlUpload,
        rename: root.dataset.urlRename,
        move: root.dataset.urlMove,
        delete: root.dataset.urlDelete,
    };

    function csrf() {
        for (const c of document.cookie.split(";")) {
            const [n, v] = c.trim().split("=");
            if (n === "csrftoken") return v;
        }
        return "";
    }

    function toast(msg) {
        const el = document.createElement("div");
        el.className = "doc-reader-toast";
        el.textContent = msg;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 1500);
    }

    // Robust copy: Clipboard API -> execCommand -> visible prompt.
    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(
                () => toast(gettext("Link copied")),
                () => execCommandCopy(text)
            );
        } else {
            execCommandCopy(text);
        }
    }

    function execCommandCopy(text) {
        const tmp = document.createElement("textarea");
        tmp.value = text;
        tmp.style.position = "fixed";
        tmp.style.opacity = "0";
        document.body.appendChild(tmp);
        tmp.focus();
        tmp.select();
        let ok = false;
        try {
            ok = document.execCommand("copy");
        } catch (e) {
            ok = false;
        }
        tmp.remove();
        if (ok) {
            toast(gettext("Link copied"));
        } else {
            window.prompt(gettext("Copy this link:"), text);
        }
    }

    async function post(url, data, isForm) {
        const opts = { method: "POST", headers: { "X-CSRFToken": csrf() } };
        if (isForm) {
            opts.body = data;
        } else {
            opts.headers["Content-Type"] = "application/x-www-form-urlencoded";
            opts.body = new URLSearchParams(data).toString();
        }
        const r = await fetch(url, opts);
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
            alert(j.error || gettext("Operation failed"));
            return false;
        }
        return true;
    }

    document.getElementById("lib-new-folder").addEventListener("click", async () => {
        const name = prompt(gettext("New folder name:"));
        if (!name) return;
        if (await post(URLS.create, { path: cur, name: name })) location.reload();
    });

    const uploadInput = document.getElementById("lib-upload-input");
    uploadInput.addEventListener("change", async () => {
        const f = uploadInput.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append("path", cur);
        fd.append("file", f);
        if (await post(URLS.upload, fd, true)) location.reload();
    });

    root.querySelectorAll(".library-item").forEach((item) => {
        const path = item.dataset.path;
        const kind = item.dataset.kind;
        const on = (sel, fn) => {
            const el = item.querySelector(sel);
            if (el) el.addEventListener("click", fn);
        };
        on(".lib-rename", async () => {
            const name = prompt(gettext("New name:"));
            if (!name) return;
            if (await post(URLS.rename, { path: path, name: name, kind: kind })) location.reload();
        });
        on(".lib-delete", async () => {
            if (!confirm(gettext("Delete this item?"))) return;
            if (await post(URLS.delete, { path: path, kind: kind })) location.reload();
        });
        on(".lib-copylink", () => {
            const share = item.dataset.shareUrl;
            if (share) copyToClipboard(location.origin + share);
        });
    });
})();
