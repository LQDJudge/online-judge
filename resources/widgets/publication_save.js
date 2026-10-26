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
      form.addEventListener("submit", function (event) {
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
