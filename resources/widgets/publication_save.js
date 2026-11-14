/* Confirm changed Save without changing a pending request merely by opening an editor. */
(function () {
  "use strict";
  if (window.jQuery) {
    window.jQuery.ajaxPrefilter(function (options, originalOptions) {
      var originalError = options.error;
      options.error = function (xhr) {
        var response = xhr.responseJSON;
        if (response && response.requires_publication_confirmation &&
            !(options.headers && options.headers["X-Publication-Confirmation-Token"] === response.publication_confirmation_token) &&
            window.confirm(response.error)) {
          var retry = window.jQuery.extend({}, originalOptions);
          retry.headers = window.jQuery.extend({}, retry.headers, {"X-Confirm-Withdraw-Publication": "1", "X-Publication-Confirmation-Token": response.publication_confirmation_token});
          window.jQuery.ajax(retry);
          return;
        }
        if (originalError) originalError.apply(this, arguments);
      };
    });
  }
  function values(form) {
    return JSON.stringify(Array.from(new FormData(form).entries()).filter(function (item) {
      return item[0] !== "csrfmiddlewaretoken" && item[0] !== "confirm_withdraw_publication" && item[0] !== "publication_confirmation_token";
    }).map(function (item) {
      var value = item[1];
      return [item[0], value instanceof File ? [value.name, value.size, value.lastModified] : value];
    }));
  }
  function initializePublicationSave() {
    document.querySelectorAll("form[data-pending-publication], form[data-live-contest-warning]").forEach(function (form) {
      var initial = values(form);
      var probing = false;
      form.addEventListener("submit", function (event) {
        if (form.dataset.reviewSaveProbe === "1" && !form.dataset.reviewSaveProbePassed) {
          event.preventDefault();
          event.stopImmediatePropagation();
          if (probing) return;
          probing = true;
          var submitter = event.submitter;
          var csrf = form.querySelector("input[name='csrfmiddlewaretoken']");
          function resume() {
            probing = false;
            form.dataset.reviewSaveProbePassed = "1";
            if (form.requestSubmit) form.requestSubmit(submitter || undefined);
            else form.submit();
          }
          fetch(form.action || window.location.href, {
            method: "POST",
            credentials: "same-origin",
            headers: {"X-Requested-With": "XMLHttpRequest", "X-Review-Save-Probe": "1", "X-CSRFToken": csrf ? csrf.value : ""}
          }).then(function (response) {
            return response.json();
          }).then(function (result) {
            if (!result.requires_review_confirmation) return resume();
            if (form.dataset.liveContestWarning && values(form) !== initial) {
              result.error += "\n\n" + form.dataset.liveContestWarning;
            }
            var dialog = document.getElementById("review-save-dialog");
            if (!dialog || !dialog.showModal) {
              if (!window.confirm(result.error)) { probing = false; return; }
              return confirmSave();
            }
            function confirmSave() {
              var confirmInput = document.createElement("input");
              confirmInput.type = "hidden";
              confirmInput.name = "confirm_withdraw_publication";
              confirmInput.value = "1";
              form.appendChild(confirmInput);
              var tokenInput = document.createElement("input");
              tokenInput.type = "hidden";
              tokenInput.name = "publication_confirmation_token";
              tokenInput.value = result.publication_confirmation_token;
              form.appendChild(tokenInput);
              resume();
            }
            dialog.querySelector("[data-save-message]").textContent = result.error;
            dialog.querySelector("[data-save-close]").onclick = function () { dialog.close(); };
            dialog.querySelector("[data-save-confirm]").onclick = function () { dialog.close(); confirmSave(); };
            dialog.onclose = function () { probing = false; };
            dialog.onclick = function (click) { if (click.target === dialog) dialog.close(); };
            dialog.showModal();
          }).catch(resume);
          return;
        }
        delete form.dataset.reviewSaveProbePassed;
        if (values(form) === initial || form.querySelector("input[name='confirm_withdraw_publication']")) return;
        var warning = form.dataset.pendingPublication === "1" ? form.dataset.publicationSaveWarning : "";
        if (form.dataset.liveContestWarning) warning += (warning ? "\n\n" : "") + form.dataset.liveContestWarning;
        if (!warning) return;
        if (!window.confirm(warning)) {
          event.preventDefault();
          event.stopImmediatePropagation();
          return;
        }
        var confirmInput = document.createElement("input");
        confirmInput.type = "hidden";
        confirmInput.name = "confirm_withdraw_publication";
        confirmInput.value = "1";
        form.appendChild(confirmInput);
        var tokenInput = document.createElement("input");
        tokenInput.type = "hidden";
        tokenInput.name = "publication_confirmation_token";
        tokenInput.value = form.dataset.publicationConfirmationToken || "";
        form.appendChild(tokenInput);
      }, true);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializePublicationSave);
  } else {
    initializePublicationSave();
  }
}());
