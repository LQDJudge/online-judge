// Deterministic unit tests; no browser or network required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const requests = [];
const timers = new Map();
const status = new Map();
let nextTimer = 0;
function $(selector) {
    return {
        text(value) { status.set(selector, value); return this; },
        removeClass() { return this; },
        addClass() { return this; }
    };
}
$.ajax = request => requests.push(request);
const context = vm.createContext({
    $, window: {}, document: {}, alert() {}, console,
    setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 1; }, clearInterval() {},
});
vm.runInContext(fs.readFileSync('resources/quiz.js', 'utf8'), context);
function tick() {
    const callbacks = [...timers.values()];
    timers.clear();
    callbacks.forEach(fn => fn());
}
const saver = context.window.initAutoSave({savedText: 'Saved', savingText: 'Saving', errorText: 'Failed'});
saver.saveAnswer(1, 'old');
tick();
assert.equal(requests.length, 1);
saver.saveAnswer(1, 'middle');
saver.saveAnswer(1, 'latest');
tick();
assert.equal(requests.length, 1, 'no overlapping save for the same question');
requests[0].success({});
assert.equal(status.get('#save-status-1'), 'Saving', 'old completion cannot mark new text saved');
requests[0].complete();
assert.equal(requests.length, 2);
assert.equal(JSON.parse(requests[1].data).answer, 'latest');
requests[1].success({});
requests[1].complete();
assert.equal(status.get('#save-status-1'), 'Saved');
saver.saveAnswer(2, 'unsent');
saver.stop();
tick();
assert.equal(requests.length, 2, 'manual submission cancels queued autosaves');
const another = context.window.initAutoSave({errorText: 'Failed'});
another.saveAnswer(3, 'offline');
tick();
requests[2].error({});
requests[2].complete();
assert.equal(status.get('#save-status-3'), 'Failed');
let expirations = 0;
const timer = new context.window.QuizTimer(0, () => expirations++, '#timer');
timer.expire();
timer.expire();
assert.equal(expirations, 1, 'expiry is idempotent');
const fractionalTimer = new context.window.QuizTimer(59.125, () => {}, '#fractional-timer');
fractionalTimer.tick();
assert.equal(status.get('#fractional-timer'), '1:00', 'status resync must display whole seconds');
const immediateTimer = new context.window.QuizTimer(0, () => {}, '#immediate-timer');
immediateTimer.start();
assert.equal(immediateTimer.interval, null, 'already expired timers must not leave an interval running');
console.log('Quiz deadline JavaScript unit checks passed.');
