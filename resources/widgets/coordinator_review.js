/* Review navigation. Authorization and readiness remain server-side. */
function initializeCoordinatorReview() {
    "use strict";
    if (window.lqdojReviewCleanup) window.lqdojReviewCleanup();
    var workspace = document.querySelector("[data-review-workspace]");
    var tabs = workspace ? Array.from(workspace.querySelectorAll("[data-review-tab]")) : [];
    var submittingActions = 0;
    var backdropPointer = null;
    var preparationRequest = 0;
    var disposed = false;
    var listeners = [];
    var pollTimer = null;

    function currentView() {
        return !disposed && (!workspace || workspace.isConnected !== false);
    }

    function listen(target, name, callback) {
        var handler = function (event) {
            if (!currentView()) { cleanupReview(); return; }
            callback(event);
        };
        target.addEventListener(name, handler);
        listeners.push(function () { target.removeEventListener(name, handler); });
    }

    function cleanupReview() {
        if (disposed) return;
        disposed = true;
        preparationRequest++;
        listeners.forEach(function (remove) { remove(); });
        if (pollTimer !== null) window.clearInterval(pollTimer);
    }
    window.lqdojReviewCleanup = cleanupReview;

    function actionInProgress() {
        return submittingActions || ["review-start-dialog", "review-cancel-dialog", "review-withdraw-dialog",
            "contest-request-decision-dialog", "contest-request-accept-dialog", "contest-request-reject-dialog",
            "problem-request-decision-dialog", "problem-request-accept-dialog", "problem-request-reject-dialog"].some(function (id) {
            var dialog = document.getElementById(id);
            return dialog && dialog.open;
        });
    }

    function isBackdrop(event) {
        var dialog = event.target;
        if (!dialog.matches("dialog.review-dialog") || !dialog.open) return false;
        var rect = dialog.getBoundingClientRect();
        return event.clientX < rect.left || event.clientX > rect.right ||
            event.clientY < rect.top || event.clientY > rect.bottom;
    }

    listen(document, "pointerdown", function (event) {
        backdropPointer = isBackdrop(event) ? event.target : null;
    });
    listen(document, "lqdoj:comment-posted", function (event) {
        var badge = document.getElementById("review-discussion-count");
        var detail = event.detail || {};
        if (!badge || String(detail.contentTypeId) !== badge.dataset.contentTypeId ||
                String(detail.objectId) !== badge.dataset.objectId ||
                !Number.isSafeInteger(detail.count) || detail.count < 0) return;
        badge.textContent = String(detail.count);
    });

    function activateTab(name, updateUrl, focus) {
        if (!tabs.some(function (tab) { return tab.dataset.reviewTab === name; })) name = "overview";
        tabs.forEach(function (tab) {
            var active = tab.dataset.reviewTab === name;
            tab.setAttribute("aria-selected", String(active));
            tab.tabIndex = active ? 0 : -1;
            document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
            if (active && focus) tab.focus();
        });
        if (updateUrl) {
            var url = new URL(window.location.href);
            url.searchParams.set("tab", name);
            window.history.replaceState(null, "", url);
        }
        var themeLink = document.getElementById("nav-darkmode-icon");
        if (themeLink && workspace.dataset.themeToggleUrl) {
            var current = new URL(window.location.href);
            var toggle = new URL(workspace.dataset.themeToggleUrl, current);
            toggle.searchParams.set("next", current.pathname + current.search + current.hash);
            themeLink.href = toggle.href;
        }
        if (name === "discussion") window.dispatchEvent(new Event("resize"));
    }

    function selectedTab() {
        return new URL(window.location.href).searchParams.get("tab") || "overview";
    }

    function navigate(url) {
        var target = new URL(url, window.location.href);
        if (workspace && target.origin === window.location.origin && target.pathname === window.location.pathname) {
            target.searchParams.set("tab", selectedTab());
        }
        window.location.assign(target.href);
    }

    if (workspace) {
        var query = new URL(window.location.href).searchParams;
        activateTab(query.has("target_comment") || window.location.hash.indexOf("comment") !== -1 ? "discussion" : selectedTab(), false);
        tabs.forEach(function (tab) {
            listen(tab, "click", function () { activateTab(tab.dataset.reviewTab, true); });
            listen(tab, "keydown", function (event) {
                var index = tabs.indexOf(tab), next;
                if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
                if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
                if (event.key === "Home") next = 0;
                if (event.key === "End") next = tabs.length - 1;
                if (next !== undefined) {
                    event.preventDefault();
                    activateTab(tabs[next].dataset.reviewTab, true, true);
                }
            });
        });
    }

    listen(document, "change", function (event) {
        if (workspace && event.target.id === "review-history-select") navigate(event.target.value);
    });

    listen(document, "click", function (event) {
        var latest = event.target.closest("[data-review-latest]");
        if (workspace && latest) {
            event.preventDefault();
            navigate(latest.href);
            return;
        }
        var section = event.target.closest("[data-review-show-sections], [data-review-section]");
        if (workspace && section) {
            activateTab("sections", true);
            var stage = document.getElementById("review-stage-" + section.dataset.reviewSection);
            if (stage) {
                stage.open = true;
                var ancestor = stage.parentElement;
                while (ancestor && ancestor !== workspace) {
                    if (ancestor.tagName === "DETAILS") ancestor.open = true;
                    ancestor = ancestor.parentElement;
                }
                stage.querySelector("summary").focus();
                stage.scrollIntoView({block: "nearest"});
            }
            return;
        }
        var dismiss = backdropPointer === event.target && isBackdrop(event);
        backdropPointer = null;
        if (dismiss) {
            event.target.close();
            return;
        }
        var close = event.target.closest("[data-review-close], [data-review-close-link]");
        if (close && close.closest("dialog")) {
            event.preventDefault();
            close.closest("dialog").close();
            return;
        }
        var opener = event.target.closest("[data-review-dialog]");
        if (opener) {
            var destination = document.getElementById(opener.dataset.reviewDialog);
            var previousDialog = opener.closest("dialog");
            if (previousDialog && previousDialog !== destination) previousDialog.close();
            if (destination) destination.showModal();
        }
        var start = event.target.closest("[data-review-start]");
        if (!start || !workspace) return;
        var dialog = document.getElementById("review-start-dialog");
        if (!dialog || typeof dialog.showModal !== "function") return;
        event.preventDefault();
        var content = dialog.querySelector("[data-review-start-content]");
        var requestNumber = ++preparationRequest;
        content.replaceChildren();
        var loading = document.createElement("p");
        loading.setAttribute("role", "status");
        loading.className = "review-muted";
        loading.textContent = gettext("Loading…");
        content.appendChild(loading);
        dialog.showModal();
        fetch(start.href, {credentials: "same-origin", headers: {"X-Review-Fragment": "1"}}).then(function (response) {
            if (!response.ok || response.redirected) throw new Error("Review preparation unavailable");
            return response.text();
        }).then(function (html) {
            if (!currentView() || requestNumber !== preparationRequest || !dialog.open) return;
            var page = new DOMParser().parseFromString(html, "text/html");
            var preparation = page.querySelector(".review-start-page");
            if (!preparation) throw new Error("Review preparation unavailable");
            content.replaceChildren(document.importNode(preparation, true));
        }).catch(function () {
            if (currentView() && requestNumber === preparationRequest && dialog.open) window.location.assign(start.href);
        });
    });

    function showActionError(form, text) {
        var message = form.querySelector(".coordinator-action-error");
        if (!message) {
            message = document.createElement("p");
            message.className = "red coordinator-action-error";
            message.setAttribute("role", "alert");
            form.appendChild(message);
        }
        message.textContent = text;
    }

    listen(document, "submit", function (event) {
        var form = event.target;
        var preparation = form.matches(".review-start-form") && form.closest("dialog");
        if (!preparation && !form.matches(".coordinator-action-form")) return;
        event.preventDefault();
        if (form.dataset.reviewSubmitting === "1") return;
        form.dataset.reviewSubmitting = "1";
        submittingActions++;
        form.setAttribute("aria-busy", "true");
        var button = event.submitter || form.querySelector("button[type=submit]");
        var action = button && button.getAttribute("formaction") || form.action;
        var buttonText = button && button.textContent;
        if (button) button.textContent = gettext("Working…");
        showActionError(form, "");
        var buttons = Array.from(form.querySelectorAll("button[type=submit]"));
        var previousDisabled = buttons.map(function (item) { return item.disabled; });
        buttons.forEach(function (item) { item.disabled = true; });
        fetch(action, {method: "POST", credentials: "same-origin", body: new FormData(form),
            headers: {"X-Review-Fragment": "1"}})
            .then(function (response) {
                if (!currentView()) return null;
                if (response.redirected) { navigate(response.url); return null; }
                if (preparation && (response.headers.get("content-type") || "").indexOf("text/html") !== -1) {
                    return response.text().then(function (html) {
                        if (!currentView()) return null;
                        var page = new DOMParser().parseFromString(html, "text/html");
                        var replacement = page.querySelector(".review-start-page");
                        if (!replacement) throw new Error("Review preparation unavailable");
                        form.closest("[data-review-start-content]").replaceChildren(document.importNode(replacement, true));
                        var error = document.querySelector("#review-start-dialog .red");
                        if (error) { error.setAttribute("role", "alert"); error.scrollIntoView({block: "nearest"}); }
                        return null;
                    });
                }
                return response.json();
            }).then(function (result) {
                if (!result || !currentView()) return;
                if (result.success) {
                    if (result.redirect) navigate(result.redirect);
                    else window.location.reload();
                } else showActionError(form, result.error || gettext("The review action could not be completed."));
            }).catch(function () {
                if (currentView()) showActionError(form, gettext("The review action could not be completed. Please check the current status before trying again."));
            }).finally(function () {
                delete form.dataset.reviewSubmitting;
                submittingActions--;
                form.setAttribute("aria-busy", "false");
                if (button) button.textContent = buttonText;
                buttons.forEach(function (item, index) { item.disabled = previousDisabled[index]; });
            });
    });

    if (workspace) {
        var status = document.getElementById("run-status");
        if (status && ["R", "B"].includes(status.dataset.status) && status.dataset.viewingHistory !== "1") {
            var progress = JSON.parse(status.dataset.stageProgress || "[]");
            var polling = false;
            var regionHtml = {};

            function updateReport() {
                return fetch(window.location.href, {credentials: "same-origin", headers: {"X-Review-Live": "1"}})
                    .then(function (response) {
                        if (!response.ok || response.redirected) throw new Error("Report unavailable");
                        return response.text();
                    }).then(function (html) {
                        if (!currentView() || actionInProgress()) return;
                        var page = new DOMParser().parseFromString(html, "text/html");
                        var incoming = page.querySelector("[data-review-workspace]");
                        var incomingStatus = incoming && incoming.querySelector("#run-status");
                        if (!incomingStatus || incomingStatus.dataset.runId !== status.dataset.runId) return;
                        var activeId = document.activeElement && document.activeElement.id;
                        var activeSummary = document.activeElement && document.activeElement.matches("summary") &&
                            document.activeElement.parentElement.id;
                        ["#run-status", "#review-live-notices", ".review-outcome", "#review-live-feedback",
                            "#review-panel-sections", "#review-cancel-dialog"].forEach(function (selector) {
                            var current = workspace.querySelector(selector);
                            var replacement = incoming.querySelector(selector);
                            if (!current || !replacement) {
                                if (current && selector === "#review-cancel-dialog") current.remove();
                                return;
                            }
                            if (regionHtml[selector] === replacement.outerHTML) return;
                            regionHtml[selector] = replacement.outerHTML;
                            var expanded = {};
                            current.querySelectorAll("details[id]").forEach(function (details) { expanded[details.id] = details.open; });
                            var copy = document.importNode(replacement, true);
                            copy.querySelectorAll("details[id]").forEach(function (details) {
                                if (Object.prototype.hasOwnProperty.call(expanded, details.id)) details.open = expanded[details.id];
                            });
                            if (current.hasAttribute("role") && current.getAttribute("role") === "tabpanel") copy.hidden = current.hidden;
                            current.replaceWith(copy);
                            if (typeof window.renderKatex === "function" && window.katex) window.renderKatex(copy);
                        });
                        status = document.getElementById("run-status");
                        progress = JSON.parse(status.dataset.stageProgress || "[]");
                        var active = activeId && document.getElementById(activeId);
                        if (!active && activeSummary) {
                            var group = document.getElementById(activeSummary);
                            active = group && group.querySelector("summary");
                        }
                        if (active && active !== document.activeElement) active.focus({preventScroll: true});
                    });
            }

            pollTimer = window.setInterval(function () {
                if (!currentView()) { cleanupReview(); return; }
                if (document.hidden || actionInProgress() || polling || !["R", "B"].includes(status.dataset.status)) return;
                polling = true;
                fetch(workspace.dataset.statusUrl, {credentials: "same-origin", headers: {"X-Review-Progress": "1"}})
                    .then(function (response) { if (!response.ok) throw new Error("Status unavailable"); return response.json(); })
                    .then(function (data) {
                        if (!currentView() || !data.run || actionInProgress()) return;
                        var updated = (data.run.coordinator_stages || []).map(function (stage) { return stage.status; });
                        var changedRun = data.run.id && String(data.run.id) !== status.dataset.runId;
                        if (changedRun) { window.location.reload(); return; }
                        if (data.run.status !== status.dataset.status || JSON.stringify(updated) !== JSON.stringify(progress)) return updateReport();
                    }).catch(function () { /* Keep the evidenced state on transient failure. */ })
                    .finally(function () { polling = false; });
            }, 3000);
        }
    }
}

// Sidebar navigation replaces #js_media after the initial page has loaded;
// DOMContentLoaded will not fire again for those dynamically loaded scripts.
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeCoordinatorReview, {once: true});
} else {
    initializeCoordinatorReview();
}
