(function () {
    "use strict";

    function slugify(text) {
        return (text || "")
            .toLowerCase().trim()
            .replace(/[^\w\s-]/g, "")
            .replace(/[\s_-]+/g, "-")
            .replace(/^-+|-+$/g, "");
    }

    function toast(msg) {
        const el = document.createElement("div");
        el.className = "doc-reader-toast";
        el.textContent = msg;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 1500);
    }

    // Robust copy: Clipboard API (secure contexts / localhost) -> execCommand ->
    // visible prompt. Never silently does nothing.
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

    async function destToPage(pdfDocument, dest) {
        try {
            let explicit = dest;
            if (typeof dest === "string") {
                explicit = await pdfDocument.getDestination(dest);
            }
            if (!Array.isArray(explicit) || !explicit[0]) return null;
            const index = await pdfDocument.getPageIndex(explicit[0]);
            return index + 1;
        } catch (e) {
            return null;
        }
    }

    function waitForViewer(iframe) {
        return new Promise(function (resolve, reject) {
            const start = Date.now();
            (function poll() {
                let app;
                try {
                    app = iframe.contentWindow && iframe.contentWindow.PDFViewerApplication;
                } catch (e) {
                    return reject(e);
                }
                if (app && app.initializedPromise) {
                    app.initializedPromise.then(function () { resolve(app); });
                    return;
                }
                if (Date.now() - start > 20000) return reject(new Error("viewer init timeout"));
                setTimeout(poll, 100);
            })();
        });
    }

    class ReaderOutline {
        constructor(root, app) {
            this.root = root;
            this.app = app;
            this.treeEl = root.querySelector("#doc-reader-tree");
            this.emptyEl = root.querySelector("#doc-reader-outline-empty");
            this.nodes = [];
        }

        async build() {
            const pdfDocument = this.app.pdfDocument;
            const outline = await pdfDocument.getOutline();
            if (!outline || !outline.length) {
                this.emptyEl.hidden = false;
                return;
            }
            const usedSlugs = {};
            const buildLevel = async (items, container) => {
                for (const item of items) {
                    const li = document.createElement("li");
                    const node = document.createElement("div");
                    node.className = "doc-reader-node";

                    const toggle = document.createElement("span");
                    toggle.className = "doc-reader-toggle";
                    const hasChildren = item.items && item.items.length;
                    node.appendChild(toggle);

                    const label = document.createElement("span");
                    label.className = "doc-reader-label";
                    label.textContent = item.title;
                    node.appendChild(label);

                    let slug = slugify(item.title) || "section";
                    while (usedSlugs[slug]) slug = slug + "-x";
                    usedSlugs[slug] = true;

                    const copy = document.createElement("span");
                    copy.className = "doc-reader-node-copy";
                    copy.innerHTML = '<i class="fa fa-link"></i>';
                    copy.title = gettext("Copy link to this section");
                    copy.addEventListener("click", (e) => {
                        e.stopPropagation();
                        copyToClipboard(
                            location.origin + location.pathname + "#section=" + encodeURIComponent(slug)
                        );
                    });
                    node.appendChild(copy);

                    li.appendChild(node);

                    const page = await destToPage(pdfDocument, item.dest);
                    const record = { li, page, dest: item.dest, slug, title: item.title };
                    this.nodes.push(record);

                    let childUl = null;
                    if (hasChildren) {
                        childUl = document.createElement("ul");
                        childUl.hidden = false;
                        li.appendChild(childUl);
                        toggle.textContent = "▾";
                        toggle.addEventListener("click", (e) => {
                            e.stopPropagation();
                            childUl.hidden = !childUl.hidden;
                            toggle.textContent = childUl.hidden ? "▸" : "▾";
                        });
                    }

                    node.addEventListener("click", () => this.goTo(record));

                    container.appendChild(li);
                    if (childUl) await buildLevel(item.items, childUl);
                }
            };
            await buildLevel(outline, this.treeEl);
        }

        goTo(record) {
            if (record.dest) {
                this.app.pdfLinkService.goToDestination(record.dest);
            } else if (record.page) {
                this.app.page = record.page;
            }
        }

        highlightByPage(pageNumber) {
            let best = null;
            for (const n of this.nodes) {
                if (n.page && n.page <= pageNumber) {
                    if (!best || n.page >= best.page) best = n;
                }
            }
            this.nodes.forEach((n) => n.li.classList.toggle("active", n === best));
            if (best) {
                let el = best.li.parentElement;
                while (el && el !== this.treeEl) {
                    if (el.tagName === "UL") el.hidden = false;
                    el = el.parentElement;
                }
                best.li.querySelector(".doc-reader-node").scrollIntoView({ block: "nearest" });
            }
        }

        findBySlug(slug) {
            return this.nodes.find((n) => n.slug === slug) || null;
        }
    }

    function applyHash(outline, app) {
        const hash = (location.hash || "").replace(/^#/, "");
        if (!hash) return;
        const params = new URLSearchParams(hash);
        if (params.has("page")) {
            const p = parseInt(params.get("page"), 10);
            if (p > 0) app.page = p;
        } else if (params.has("section")) {
            const rec = outline.findBySlug(decodeURIComponent(params.get("section")));
            if (rec) outline.goTo(rec);
        }
    }

    document.addEventListener("DOMContentLoaded", async function () {
        const root = document.getElementById("doc-reader");
        if (!root) return;
        const iframe = document.getElementById("doc-reader-frame");
        const viewerUrl = root.dataset.viewerUrl;
        const rawUrl = root.dataset.rawUrl;

        let app = null;  // set once the viewer initializes

        // --- Bind UI immediately (independent of the PDF.js bridge) ---
        const toggleBtn = document.getElementById("doc-reader-toggle-outline");
        const reopenBtn = document.getElementById("doc-reader-reopen");
        if (toggleBtn) {
            toggleBtn.addEventListener("click", () => root.classList.add("outline-collapsed"));
        }
        if (reopenBtn) {
            reopenBtn.addEventListener("click", () => root.classList.remove("outline-collapsed"));
        }
        const copyBtn = document.getElementById("doc-reader-copylink");
        if (copyBtn) {
            copyBtn.addEventListener("click", () => {
                const page = app ? app.page : 1;
                copyToClipboard(location.origin + location.pathname + "#page=" + page);
            });
        }

        // --- Load the viewer and wire the outline ---
        iframe.src = viewerUrl + "?file=" + encodeURIComponent(rawUrl) + "#pagemode=none";
        try {
            app = await waitForViewer(iframe);
        } catch (e) {
            console.error("reader: viewer bridge failed", e);
            return;
        }

        const outline = new ReaderOutline(root, app);
        app.eventBus.on("documentloaded", async function onDoc() {
            app.eventBus.off("documentloaded", onDoc);
            await outline.build();
            applyHash(outline, app);
        });
        app.eventBus.on("pagechanging", function (e) {
            if (e && e.pageNumber) outline.highlightByPage(e.pageNumber);
        });
        window.addEventListener("hashchange", function () {
            applyHash(outline, app);
        });
    });
})();
