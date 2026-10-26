const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

test('disabled fieldsets never initialize sortable editing controls', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/widgets/sortable_formset.js'), 'utf8');
    let initialized = 0;
    const window = {};
    const document = {readyState: 'loading', addEventListener() {}};
    vm.runInNewContext(source, {window, document});
    const widget = window.SortableFormset.prototype;
    for (const name of ['initSortable', 'initAddRow', 'initRemoveRow', 'updateRowNumbers']) {
        widget[name] = () => { initialized++; };
    }
    function container(locked) {
        return {dataset: {prefix: 'rows'}, querySelector() { return null; }, closest() { return locked ? {} : null; }};
    }
    new window.SortableFormset(container(true));
    assert.equal(initialized, 0);
    new window.SortableFormset(container(false));
    assert.equal(initialized, 4);
});
