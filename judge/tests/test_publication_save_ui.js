const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../resources/widgets/publication_save.js'), 'utf8');

function setup({readyState = 'loading', pending = '1', accept = true} = {}) {
    const events = {}, listeners = {}, children = [], confirmations = [];
    const form = {
        dataset: {pendingPublication: pending, publicationSaveWarning: 'Withdraw request?', publicationConfirmationToken: 'current-token'},
        fields: [['description', 'original']],
        addEventListener(name, fn) { listeners[name] = fn; },
        querySelector() { return children.find(child => child.name === 'confirm_withdraw_publication'); },
        appendChild(child) { children.push(child); },
    };
    const document = {
        readyState,
        addEventListener(name, fn) { events[name] = fn; },
        querySelectorAll(selector) {
            assert.ok(selector.includes('form[data-pending-publication]'));
            return [form];
        },
        createElement() { return {}; },
    };
    vm.runInNewContext(source, {
        document,
        window: {confirm(message) { confirmations.push(message); return accept; }},
        FormData: class {
            constructor(form) { this.form = form; }
            entries() { return this.form.fields; }
        },
        File: class {},
    });
    if (readyState === 'loading') events.DOMContentLoaded();
    const event = {prevented: false, stopped: false,
        preventDefault() { this.prevented = true; },
        stopImmediatePropagation() { this.stopped = true; }};
    return {form, confirmations, children, submit() { listeners.submit(event); return event; }};
}

test('unchanged save does not ask for withdrawal', () => {
    const ui = setup();
    assert.equal(ui.submit().prevented, false);
    assert.equal(ui.confirmations.length, 0);
    assert.equal(ui.children.length, 0);
});

test('changed save carries consent bound to the current request', () => {
    const ui = setup();
    ui.form.fields[0][1] = 'changed';
    assert.equal(ui.submit().prevented, false);
    assert.equal(ui.confirmations.length, 1);
    assert.equal(ui.children.find(child => child.name === 'publication_confirmation_token').value, 'current-token');
    assert.equal(ui.children.find(child => child.name === 'confirm_withdraw_publication').value, '1');
});

test('declining keeps the editor and does not add consent', () => {
    const ui = setup({accept: false});
    ui.form.fields[0][1] = 'changed';
    const event = ui.submit();
    assert.equal(event.prevented, true);
    assert.equal(event.stopped, true);
    assert.equal(ui.children.length, 0);
});

test('initializes even when loaded after DOMContentLoaded', () => {
    const ui = setup({readyState: 'complete'});
    ui.form.fields[0][1] = 'changed';
    ui.submit();
    assert.equal(ui.confirmations.length, 1);
});

test('no pending request means no empty confirmation', () => {
    const ui = setup({pending: '0'});
    ui.form.fields[0][1] = 'changed';
    ui.submit();
    assert.equal(ui.confirmations.length, 0);
    assert.equal(ui.children.length, 0);
});

test('a request that becomes pending is noticed at Save', () => {
    const ui = setup({pending: '0'});
    ui.form.dataset.pendingPublication = '1';
    ui.form.fields[0][1] = 'changed';
    ui.submit();
    assert.equal(ui.confirmations.length, 1);
});

test('published-contest warning is retained without a pending request', () => {
    const ui = setup({pending: '0'});
    ui.form.dataset.liveContestWarning = 'This changes a published contest.';
    ui.form.fields[0][1] = 'changed';
    ui.submit();
    assert.equal(ui.confirmations[0], 'This changes a published contest.');
});

test('active-review save opens a modal and keeps the form until confirmed', async () => {
    const listeners = {}, children = [], controls = {
        '[data-save-message]': {}, '[data-save-close]': {}, '[data-save-confirm]': {},
    };
    let submitted = 0;
    const dialog = {
        open: false,
        querySelector(selector) { return controls[selector]; },
        showModal() { this.open = true; },
        close() { this.open = false; if (this.onclose) this.onclose(); },
    };
    const form = {
        dataset: {reviewSaveProbe: '1', pendingPublication: '0'},
        action: '/problem/example/edit',
        addEventListener(name, fn) { listeners[name] = fn; },
        querySelector(selector) {
            if (selector.includes('csrfmiddlewaretoken')) return {value: 'csrf'};
            return children.find(child => child.name === 'confirm_withdraw_publication');
        },
        appendChild(child) { children.push(child); },
        requestSubmit() { submitted++; listeners.submit({preventDefault() {}, stopImmediatePropagation() {}}); },
    };
    const document = {
        readyState: 'complete', querySelectorAll() { return [form]; },
        getElementById() { return dialog; }, createElement() { return {}; },
    };
    vm.runInNewContext(source, {
        document, window: {location: {href: form.action}},
        FormData: class { entries() { return []; } }, File: class {},
        fetch: async () => ({json: async () => ({requires_review_confirmation: true, error: 'Running review will be cancelled.', publication_confirmation_token: 'bound-token'})}),
    });
    let prevented = false;
    listeners.submit({preventDefault() { prevented = true; }, stopImmediatePropagation() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(prevented, true);
    assert.equal(dialog.open, true);
    assert.equal(submitted, 0);
    assert.equal(controls['[data-save-message]'].textContent, 'Running review will be cancelled.');
    controls['[data-save-close]'].onclick();
    assert.equal(dialog.open, false);
    assert.equal(submitted, 0);
    assert.equal(children.length, 0);
    listeners.submit({preventDefault() {}, stopImmediatePropagation() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(dialog.open, true);
    controls['[data-save-confirm]'].onclick();
    assert.equal(dialog.open, false);
    assert.equal(submitted, 1);
    assert.equal(children.find(child => child.name === 'publication_confirmation_token').value, 'bound-token');
});
