const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

// Run after make_style.sh: assert the actual emitted shared rules, including
// the exceptions that protect scoreboards and intentionally borderless tables.
const css = fs.readFileSync(path.join(__dirname, '../../resources/table.css'), 'utf8');
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(match => ({
    selectors: match[1].replace(/\/\*[\s\S]*?\*\//g, '').trim().split(/,\s*/),
    body: match[2],
}));
function rule(selector) {
    const matching = rules.filter(item => item.selectors.includes(selector));
    assert.ok(matching.length, 'Missing CSS rule: ' + selector);
    return matching.map(item => item.body).join('\n');
}

test('table surface does not clip shadows, menus or sticky descendants', () => {
    const table = rule('.table');
    assert.doesNotMatch(table, /(?:clip-path|overflow)\s*:/);
    assert.match(table, /border-radius\s*:/);
    assert.match(table, /box-shadow\s*:/);
});

test('standard striping and hover paint cells rather than square rows', () => {
    for (const parity of ['even', 'odd']) {
        assert.match(rule('.table.striped:not(:where(#users-table)) > tbody > tr:nth-child(' + parity + ') > td'), /background\s*:/);
        assert.equal(rules.some(item => item.selectors.includes('.table.striped tr:nth-child(' + parity + ')')), false);
    }
    assert.match(rule('.table.striped:not(:where(#users-table)) > tbody > tr:hover > td'), /background\s*:/);
});

test('closing border and individual corner radii remain explicit', () => {
    const bottom = rule('.table tr:last-child td');
    assert.match(bottom, /border-bottom:\s*1px solid/);
    assert.doesNotMatch(bottom, /border-bottom:\s*(?:0|none)\s*;/);
    assert.match(rule('.table tr:last-child td:first-child'), /border-bottom-left-radius/);
    assert.match(rule('.table tr:last-child td:last-child'), /border-bottom-right-radius/);
    assert.match(rule('.table thead tr:first-child th:first-child'), /border-top-left-radius/);
    assert.match(rule('.table thead tr:first-child th:last-child'), /border-top-right-radius/);
});

test('explicitly borderless tables also suppress the closing border', () => {
    assert.match(rule('.table.no-border tr:last-child td'), /border:\s*none\s*;/);
});

test('scoreboards keep row-painted stripes and hover without increasing ID specificity', () => {
    assert.match(rule('.table.striped:where(#users-table) > tbody > tr:nth-child(even)'), /background\s*:/);
    assert.match(rule('.table:where(#users-table) > tbody > tr:hover'), /background\s*:/);
    assert.equal(rules.some(item => item.selectors.includes('.table.striped#users-table > tbody > tr:nth-child(even)')), false);
});
