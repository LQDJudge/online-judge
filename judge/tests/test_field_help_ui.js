const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../../resources/widgets/field_help.js'), 'utf8');

function setup({left = 20, top = 120} = {}) {
    const events = {}, windowEvents = {};
    const panel = {hidden: true, style: {}, getBoundingClientRect: () => ({width: 280, height: 100})};
    const attributes = {'aria-expanded': 'false'};
    const button = {
        setAttribute: (name, value) => { attributes[name] = value; },
        getBoundingClientRect: () => ({left, top, bottom: top + 24}),
        closest: selector => selector === '[data-field-help]' ? wrapper : button,
    };
    const link = {closest: selector => selector === '[data-field-help]' ? wrapper : null};
    const wrapper = {
        querySelector: selector => selector === '[data-field-help-button]' ? button : panel,
        contains: node => [wrapper, panel, button, link].includes(node),
    };
    const document = {addEventListener: (name, fn) => { events[name] = fn; }};
    const window = {innerWidth: 360, innerHeight: 640, addEventListener: (name, fn) => { windowEvents[name] = fn; }};
    vm.runInNewContext(source, {document, window});
    function fire(name, fields = {}) {
        const event = {target: button, relatedTarget: null, preventDefault() { this.prevented = true; }, ...fields};
        events[name](event);
        return event;
    }
    return {panel, button, link, attributes, fire, windowEvents};
}

test('click opens help without submitting; outside click closes it', () => {
    const ui = setup();
    assert.equal(ui.panel.hidden, true);
    assert.equal(ui.fire('click').prevented, true);
    assert.equal(ui.panel.hidden, false);
    assert.equal(ui.attributes['aria-expanded'], 'true');
    ui.fire('click', {target: {}});
    assert.equal(ui.panel.hidden, true);
    assert.equal(ui.attributes['aria-expanded'], 'false');
});

test('focus opens help, Escape and leaving the help close it', () => {
    const ui = setup();
    ui.fire('focusin');
    assert.equal(ui.panel.hidden, false);
    ui.fire('keydown', {key: 'Escape'});
    assert.equal(ui.panel.hidden, true);
    ui.fire('focusin');
    ui.fire('focusout');
    assert.equal(ui.panel.hidden, true);
});

test('documentation links retain native behavior and remain reachable by keyboard', () => {
    const ui = setup();
    ui.fire('focusin');
    ui.fire('focusout', {relatedTarget: ui.link});
    assert.equal(ui.panel.hidden, false);
    assert.equal(ui.fire('click', {target: ui.link}).prevented, undefined);
    assert.equal(ui.panel.hidden, false);
});

test('help stays inside narrow viewports and opens above low buttons', () => {
    const ui = setup({left: 340, top: 610});
    ui.fire('click');
    assert.equal(ui.panel.style.left, '72px');
    assert.equal(ui.panel.style.top, '502px');
});

test('page scroll and resize close help; scrolling its content does not', () => {
    const ui = setup();
    ui.fire('focusin');
    ui.windowEvents.scroll({target: ui.panel});
    assert.equal(ui.panel.hidden, false);
    ui.windowEvents.scroll({target: {}});
    assert.equal(ui.panel.hidden, true);
    ui.fire('focusin');
    ui.windowEvents.resize();
    assert.equal(ui.panel.hidden, true);
});
