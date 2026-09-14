document.addEventListener('DOMContentLoaded', function () {
    const form = document.querySelector('.summary-filters');
    if (!form) return;
    const pending = document.getElementById('summary-pending');
    const root = form.closest('.contest-summary-page');
    root.querySelectorAll('.summary-school-logo').forEach(function (logo) {
        logo.addEventListener('error', function () { logo.hidden = true; });
        if (logo.complete && !logo.naturalWidth) logo.hidden = true;
    });
    const displayKey = 'summary-display-' + root.dataset.summaryKey;
    let display = {};
    try { display = JSON.parse(localStorage.getItem(displayKey)) || {}; } catch (error) {}
    if (typeof display !== 'object' || Array.isArray(display)) display = {};
    root.classList.add('summary-enhanced');
    const displayControls = form.querySelector('.summary-display-controls');
    displayControls.hidden = !displayControls.querySelector('input');
    ['summary-total-only'].forEach(function (id) {
        const checkbox = document.getElementById(id);
        if (!checkbox) return;
        checkbox.checked = display[id] === true;
        function updateDisplay() {
            root.classList.toggle(id, checkbox.checked);
            display[id] = checkbox.checked;
            try { localStorage.setItem(displayKey, JSON.stringify(display)); } catch (error) {}
        }
        updateDisplay();
        checkbox.addEventListener('change', updateDisplay);
    });
    const initial = new URLSearchParams(new FormData(form)).toString();
    function changed() {
        pending.hidden = new URLSearchParams(new FormData(form)).toString() === initial;
    }
    form.addEventListener('input', changed);
    form.addEventListener('change', changed);
    if (!window.jQuery || !jQuery.fn.select2) return;
    jQuery(form).find('select[multiple]').each(function () {
        const select = this;
        const field = jQuery(select);
        field.select2({
            width: '100%',
            placeholder: select.dataset.placeholder,
            closeOnSelect: false,
            dropdownParent: field.closest('.summary-filter-field'),
            language: {noResults: function () { return select.dataset.noResults; }},
        });
        field.next('.select2-container').find('[role="combobox"], .select2-search__field')
            .attr('aria-label', select.dataset.label);
        field.on('change', changed);
    });
});
