(function () {
    'use strict';

    var active = null;

    function wrapper(target) {
        return target && target.closest ? target.closest('[data-field-help]') : null;
    }

    function hide() {
        if (!active) return;
        active.querySelector('.contest-field-help-panel').hidden = true;
        active.querySelector('[data-field-help-button]').setAttribute('aria-expanded', 'false');
        active = null;
    }

    function show(node) {
        if (!node) return;
        if (active !== node) hide();
        var panel = node.querySelector('.contest-field-help-panel');
        var button = node.querySelector('[data-field-help-button]');
        if (!panel || !button) return;
        active = node;
        panel.hidden = false;
        button.setAttribute('aria-expanded', 'true');
        var anchor = button.getBoundingClientRect();
        var bounds = panel.getBoundingClientRect();
        var margin = 8;
        var left = Math.max(margin, Math.min(anchor.left,
            window.innerWidth - bounds.width - margin));
        var top = anchor.bottom + margin;
        if (top + bounds.height > window.innerHeight - margin) {
            top = anchor.top - bounds.height - margin;
        }
        panel.style.left = left + 'px';
        panel.style.top = Math.max(margin, top) + 'px';
    }

    document.addEventListener('focusin', function (event) {
        show(wrapper(event.target));
    });
    document.addEventListener('focusout', function (event) {
        if (active && !active.contains(event.relatedTarget)) hide();
    });
    document.addEventListener('click', function (event) {
        var node = wrapper(event.target);
        if (!node) {
            hide();
        } else if (event.target.closest('[data-field-help-button]')) {
            event.preventDefault();
            show(node);
        }
        // Links and selectable text inside the explanation retain native behavior.
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') hide();
    });
    window.addEventListener('resize', hide);
    window.addEventListener('scroll', function (event) {
        if (active && active.contains(event.target)) return;
        hide();
    }, true);
}());
