(function () {
    'use strict';

    var active = null;
    var selector = '[data-publication-hint]';

    function control(target) {
        return target && target.closest ? target.closest(selector) : null;
    }

    function hide() {
        if (!active) return;
        active.querySelector('[role="tooltip"]').hidden = true;
        active = null;
    }

    function show(wrapper) {
        if (!wrapper) return;
        if (active !== wrapper) hide();
        var tooltip = wrapper.querySelector('[role="tooltip"]');
        if (!tooltip) return;
        active = wrapper;
        tooltip.hidden = false;
        var anchor = wrapper.getBoundingClientRect();
        var bounds = tooltip.getBoundingClientRect();
        var margin = 8;
        var left = Math.max(margin, Math.min(anchor.left,
            window.innerWidth - bounds.width - margin));
        var top = anchor.top - bounds.height - margin;
        if (top < margin) top = anchor.bottom + margin;
        top = Math.max(margin, Math.min(top,
            window.innerHeight - bounds.height - margin));
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
    }

    // Delegation also covers action bars replaced by live review updates.
    document.addEventListener('pointerover', function (event) {
        show(control(event.target));
    });
    document.addEventListener('pointerout', function (event) {
        if (active && !active.contains(event.relatedTarget) &&
                !active.contains(document.activeElement)) hide();
    });
    document.addEventListener('focusin', function (event) {
        show(control(event.target));
    });
    document.addEventListener('focusout', function (event) {
        if (active && !active.contains(event.relatedTarget)) hide();
    });
    document.addEventListener('click', function (event) {
        var wrapper = control(event.target);
        if (!wrapper) {
            hide();
            return;
        }
        // Tapping explains the disabled action; it must never submit the form.
        event.preventDefault();
        show(wrapper);
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') hide();
        if ((event.key === 'Enter' || event.key === ' ') && control(event.target)) {
            event.preventDefault();
            show(control(event.target));
        }
    });
    window.addEventListener('resize', hide);
    window.addEventListener('scroll', hide, true);
}());
