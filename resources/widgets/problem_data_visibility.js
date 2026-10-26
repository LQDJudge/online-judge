(function () {
    'use strict';

    // Keep type-specific field visibility independent of upload/edit handlers.
    // Reading checked/value also works for controls in a disabled fieldset.
    window.initProblemDataVisibility = function (root) {
        function field(name) {
            return root.getElementById('id_problem-data-' + name);
        }

        function showRow(name, visible) {
            var control = field(name);
            var row = control && control.closest('tr');
            if (row) row.style.display = visible ? '' : 'none';
        }

        function bind(name, update) {
            var control = field(name);
            if (!control) return;
            function refresh() { update(control); }
            // Select2 emits jQuery change events, not native DOM events.
            window.jQuery(control).on('change', refresh);
            refresh();
        }

        bind('checker', function (control) {
            showRow('custom_checker', control.value === 'custom');
            showRow('custom_checker_cpp', ['customcpp', 'testlib', 'testlibcms'].indexOf(control.value) !== -1);
            showRow('interactive_judge', ['interact', 'interacttl'].indexOf(control.value) !== -1);
        });
        bind('output_only', function (control) {
            showRow('binary_data', control.checked);
            showRow('output_zip_size_mb', control.checked);
        });
        bind('is_communication', function (control) {
            showRow('communication_manager', control.checked);
            showRow('communication_num_processes', control.checked);
        });
        bind('use_ioi_signature', function (control) {
            var table = root.getElementById('signature-grader-table');
            if (table) table.style.display = control.checked ? '' : 'none';
        });
    };
}());
