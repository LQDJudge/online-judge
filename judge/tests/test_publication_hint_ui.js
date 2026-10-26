const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../../resources/widgets/publication_hint.js'), 'utf8');

function setup({left = 20, top = 160} = {}) {
    const events = {}, windowEvents = {};
    const tooltip = {
        hidden: true, style: {},
        getBoundingClientRect: () => ({width: 280, height: 70}),
    };
    const wrapper = {
        querySelector: () => tooltip,
        closest: () => wrapper,
        contains: node => node === wrapper || node === tooltip,
        getBoundingClientRect: () => ({left, top, bottom: top + 40}),
    };
    const document = {
        activeElement: null,
        addEventListener: (name, callback) => { events[name] = callback; },
    };
    const window = {
        innerWidth: 360, innerHeight: 640,
        addEventListener: (name, callback) => { windowEvents[name] = callback; },
    };
    vm.runInNewContext(source, {document, window});
    function fire(name, fields = {}) {
        const event = {
            target: wrapper, relatedTarget: null,
            preventDefault() { this.prevented = true; }, ...fields,
        };
        events[name](event);
        return event;
    }
    return {tooltip, wrapper, document, fire, windowEvents};
}

test('hint is initially hidden and opens on hover', () => {
    const ui = setup();
    assert.equal(ui.tooltip.hidden, true);
    ui.fire('pointerover');
    assert.equal(ui.tooltip.hidden, false);
    ui.fire('pointerout');
    assert.equal(ui.tooltip.hidden, true);
});

test('keyboard focus opens the hint and Escape dismisses it', () => {
    const ui = setup();
    ui.fire('focusin');
    assert.equal(ui.tooltip.hidden, false);
    ui.fire('keydown', {key: 'Escape'});
    assert.equal(ui.tooltip.hidden, true);
    ui.fire('keydown', {key: 'Enter'});
    assert.equal(ui.tooltip.hidden, false);
    ui.fire('focusout');
    assert.equal(ui.tooltip.hidden, true);
});

test('tap explains the action without submitting, outside tap dismisses it', () => {
    const ui = setup();
    assert.equal(ui.fire('click').prevented, true);
    assert.equal(ui.tooltip.hidden, false);
    ui.fire('click', {target: {}});
    assert.equal(ui.tooltip.hidden, true);
});

test('space prevents scrolling and opens the explanation', () => {
    const ui = setup();
    assert.equal(ui.fire('keydown', {key: ' '}).prevented, true);
    assert.equal(ui.tooltip.hidden, false);
});

test('hint stays open when pointer moves onto its content', () => {
    const ui = setup();
    ui.fire('pointerover');
    ui.fire('pointerout', {relatedTarget: ui.tooltip});
    assert.equal(ui.tooltip.hidden, false);
});

test('pointer exit does not hide a keyboard-focused explanation', () => {
    const ui = setup();
    ui.document.activeElement = ui.wrapper;
    ui.fire('focusin');
    ui.fire('pointerout');
    assert.equal(ui.tooltip.hidden, false);
});

test('narrow-screen tooltip stays on screen and opens below top-edge buttons', () => {
    const ui = setup({left: 300, top: 4});
    ui.fire('pointerover');
    assert.equal(ui.tooltip.style.left, '72px');
    assert.equal(ui.tooltip.style.top, '52px');
});

test('scroll and resize dismiss the positioned tooltip', () => {
    const ui = setup();
    for (const event of ['scroll', 'resize']) {
        ui.fire('pointerover');
        ui.windowEvents[event]();
        assert.equal(ui.tooltip.hidden, true);
    }
});

test('ordinary enabled actions are not intercepted', () => {
    const ui = setup();
    assert.equal(ui.fire('click', {target: {}}).prevented, undefined);
    assert.equal(ui.fire('keydown', {target: {}, key: 'Enter'}).prevented, undefined);
    assert.equal(ui.tooltip.hidden, true);
});
