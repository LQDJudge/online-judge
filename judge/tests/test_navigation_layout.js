const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../resources/common.js'), 'utf8');
const navigateSource = source.slice(source.indexOf('function navigateTo('),
    source.indexOf('function isEligibleLinkToRegister('));

function setup({initial = 'column-layout soft-listing-page', next = 'column-layout',
    missingFrame = false, missingContent = false} = {}) {
    const currentPage = {className: initial};
    const html = '<html><body>destination page</body></html>';
    const phases = [];
    const window = {location: {href: '/old'}, history: {pushState() {}}};
    const document = {getElementById: id => id === 'page-container' ? currentPage : null};
    function collection(selector, incoming = false) {
        return {
            0: {},
            length: incoming && missingContent && selector === '.middle-right-content' ? 0 : 1,
            first() { return this; },
            find(child) { return collection(child, true); },
            filter() { return this; },
            text() { return 'Destination'; },
            replaceWith() {
                if (selector === '#js_media') phases.push(currentPage.className);
                return this;
            },
            off() { return this; },
            scrollTop() { return this; },
            stop() { return this; },
            hide() { return this; },
            css() { return this; },
            prop() { return this; },
        };
    }
    const $ = value => collection(value);
    $.ajax = options => options.success(html);
    class DOMParser {
        parseFromString(value, type) {
            assert.equal(value, html);
            assert.equal(type, 'text/html');
            return {getElementById: id => id === 'page-container' && !missingFrame
                ? {className: next} : null};
        }
    }
    const context = {window, document, $, DOMParser, AbortController,
        replaceLoadingPage() {}, renderKatex() {}, initPagedown() {}, registerNavList() {},
        onWindowReady() { phases.push(currentPage.className); }};
    vm.runInNewContext(navigateSource, context);
    return {currentPage, phases, window,
        navigate: (force = false) => context.navigateTo('/course/test/edit/lessons', '.middle-right-content', force)};
}

test('Grades/Members to Lessons removes listing styles before page initialization', () => {
    const ui = setup();
    ui.navigate();
    assert.equal(ui.currentPage.className, 'column-layout');
    assert.deepEqual(ui.phases, ['column-layout', 'column-layout']);
});

test('returning to a listing restores its classes and clears old page-specific classes', () => {
    const ui = setup({initial: 'column-layout lesson-editor', next: 'column-layout soft-listing-page'});
    ui.navigate();
    assert.equal(ui.currentPage.className, 'column-layout soft-listing-page');
});

test('destinations can clear every frame class', () => {
    const ui = setup({next: ''});
    ui.navigate();
    assert.equal(ui.currentPage.className, '');
});

test('content fallback still synchronizes layout classes', () => {
    const ui = setup({missingContent: true});
    ui.navigate();
    assert.equal(ui.currentPage.className, 'column-layout');
});

test('responses without a page frame leave the existing frame alone', () => {
    const ui = setup({missingFrame: true});
    ui.navigate();
    assert.equal(ui.currentPage.className, 'column-layout soft-listing-page');
});

test('full navigation keeps its normal browser-loading path', () => {
    const ui = setup();
    ui.navigate(true);
    assert.equal(ui.window.location.href, '/course/test/edit/lessons');
    assert.deepEqual(ui.phases, []);
});
