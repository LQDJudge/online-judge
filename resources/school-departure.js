document.addEventListener('DOMContentLoaded', function () {
    const dialog = document.getElementById('school-departure-dialog');
    if (!dialog) return;
    const accept = document.getElementById('school-departure-confirm');
    let pendingForm = null;
    let trigger = null;

    function submitConfirmed(form) {
        const confirmation = document.createElement('input');
        confirmation.type = 'hidden';
        confirmation.name = 'confirm';
        confirmation.value = 'yes';
        form.appendChild(confirmation);
        HTMLFormElement.prototype.submit.call(form);
    }

    document.querySelectorAll('form.school-departure-form').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            if (dialog.open) return;
            if (typeof dialog.showModal !== 'function') {
                if (window.confirm(form.dataset.confirmMessage)) submitConfirmed(form);
                return;
            }
            pendingForm = form;
            trigger = document.activeElement;
            document.getElementById('school-departure-title').textContent = form.dataset.confirmTitle;
            document.getElementById('school-departure-description').textContent = form.dataset.confirmMessage;
            accept.textContent = form.dataset.confirmTitle;
            accept.disabled = false;
            dialog.showModal();
        });
    });
    document.getElementById('school-departure-cancel').addEventListener('click', function () {
        dialog.close();
    });
    dialog.addEventListener('close', function () {
        pendingForm = null;
        if (trigger && trigger.isConnected) trigger.focus();
    });
    dialog.addEventListener('keydown', function (event) {
        // Keep Escape local so it does not also close the mobile school sidebar.
        if (event.key === 'Escape') event.stopPropagation();
        if (event.key !== 'Tab') return;
        const cancel = document.getElementById('school-departure-cancel');
        if (event.shiftKey && document.activeElement === cancel) {
            event.preventDefault();
            accept.focus();
        } else if (!event.shiftKey && document.activeElement === accept) {
            event.preventDefault();
            cancel.focus();
        }
    });
    accept.addEventListener('click', function () {
        if (!pendingForm || accept.disabled) return;
        accept.disabled = true;
        submitConfirmed(pendingForm);
    });
});
