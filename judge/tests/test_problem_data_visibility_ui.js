const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../../resources/widgets/problem_data_visibility.js'), 'utf8');

function setup({locked = false, checker = 'standard', outputOnly = false, communication = false, signature = false} = {}) {
    const nodes = {};
    const fields = ['checker', 'custom_checker', 'custom_checker_cpp', 'interactive_judge',
        'output_only', 'binary_data', 'output_zip_size_mb', 'is_communication',
        'communication_manager', 'communication_num_processes', 'use_ioi_signature'];
    for (const name of fields) {
        const row = {style: {}};
        nodes['id_problem-data-' + name] = {
            value: '', checked: false, disabled: locked,
            closest(selector) { assert.equal(selector, 'tr'); return row; },
        };
    }
    nodes['signature-grader-table'] = {style: {display: 'none'}};
    const root = {getElementById(id) { return nodes[id] || null; }};
    const field = name => nodes['id_problem-data-' + name];
    field('checker').value = checker;
    field('output_only').checked = outputOnly;
    field('is_communication').checked = communication;
    field('use_ioi_signature').checked = signature;
    const window = {jQuery(control) {
        return {on(event, callback) { assert.equal(event, 'change'); control.change = callback; }};
    }};
    vm.runInNewContext(source, {window});
    window.initProblemDataVisibility(root);
    return {
        field,
        visible(name) { return field(name).closest('tr').style.display !== 'none'; },
        signatureVisible() { return nodes['signature-grader-table'].style.display !== 'none'; },
    };
}

test('ordinary CP problems hide irrelevant fields even in disabled forms', () => {
    for (const locked of [false, true]) {
        const form = setup({locked});
        for (const name of ['custom_checker', 'custom_checker_cpp', 'interactive_judge',
            'binary_data', 'output_zip_size_mb', 'communication_manager', 'communication_num_processes']) {
            assert.equal(form.visible(name), false, name);
        }
        assert.equal(form.signatureVisible(), false);
        assert.equal(form.field('checker').disabled, locked);
        assert.equal(form.field('checker').value, 'standard');
    }
});

test('only applicable checker source fields appear', () => {
    for (const locked of [false, true]) {
        for (const checker of ['standard', 'custom', 'customcpp', 'testlib', 'testlibcms', 'interact', 'interacttl']) {
            const form = setup({locked, checker});
            assert.equal(form.visible('custom_checker'), checker === 'custom');
            assert.equal(form.visible('custom_checker_cpp'), ['customcpp', 'testlib', 'testlibcms'].includes(checker));
            assert.equal(form.visible('interactive_judge'), ['interact', 'interacttl'].includes(checker));
        }
    }
});

test('applicable output-only, communication and signature fields remain readable when locked', () => {
    const form = setup({locked: true, outputOnly: true, communication: true, signature: true});
    for (const name of ['binary_data', 'output_zip_size_mb', 'communication_manager', 'communication_num_processes']) {
        assert.equal(form.visible(name), true);
        assert.equal(form.field(name).disabled, true);
    }
    assert.equal(form.signatureVisible(), true);
});

test('editable forms respond to changes including Select2-style jQuery events', () => {
    const form = setup();
    form.field('checker').value = 'testlib';
    form.field('checker').change();
    assert.equal(form.visible('custom_checker_cpp'), true);
    for (const [name, dependent] of [['output_only', 'binary_data'], ['is_communication', 'communication_manager']]) {
        form.field(name).checked = true;
        form.field(name).change();
        assert.equal(form.visible(dependent), true);
        form.field(name).checked = false;
        form.field(name).change();
        assert.equal(form.visible(dependent), false);
    }
    form.field('use_ioi_signature').checked = true;
    form.field('use_ioi_signature').change();
    assert.equal(form.signatureVisible(), true);
});

test('missing controls do not break presentation setup', () => {
    const window = {};
    vm.runInNewContext(source, {window});
    assert.doesNotThrow(() => window.initProblemDataVisibility({getElementById() { return null; }}));
});

test('visibility setup runs before locked Test Data skips mutation handlers', () => {
    const template = fs.readFileSync(path.join(__dirname, '../../templates/problem/data.html'), 'utf8');
    const setupPosition = template.indexOf('window.initProblemDataVisibility(document)');
    const lockPosition = template.indexOf('{% if problem.is_locked');
    assert.ok(setupPosition >= 0 && setupPosition < lockPosition);
    assert.ok(template.indexOf('return;', lockPosition) < template.indexOf('.fineUploader('));
});
