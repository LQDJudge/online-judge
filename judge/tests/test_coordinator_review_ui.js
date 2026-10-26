const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../../resources/widgets/coordinator_review.js'), 'utf8');

function setup({query = '', status = 'D', history = false, modern = true, readyState = 'loading'} = {}) {
    const nodes = {}, events = {}, intervals = [], requests = [], navigations = [], mathRenders = [];
    let reloads = 0, parsed = null, response = async () => ({ok: true, json: async () => ({success: true})});
    const removedListeners = [], clearedIntervals = [];
    function node(id) {
        const item = {
            id, dataset: {}, attrs: {}, listeners: {}, children: [], disabled: false,
            setAttribute(key, value) { this.attrs[key] = value; },
            getAttribute(key) { return this.attrs[key] || null; },
            hasAttribute(key) { return key in this.attrs; },
            addEventListener(key, callback) { this.listeners[key] = callback; },
            removeEventListener(key, callback) { if (this.listeners[key] === callback) delete this.listeners[key]; },
            focus() { this.focused = true; },
            scrollIntoView() { this.scrolled = true; },
            appendChild(child) { this.children.push(child); },
            replaceChildren(...children) { this.children = children; },
            replaceWith(other) { nodes[this.id] = other; this.replacement = other; },
            remove() { delete nodes[this.id]; this.removed = true; },
            showModal() { this.open = true; }, close() { this.open = false; },
            querySelector(selector) { return this.children.find(child => (child.className || '').split(' ').includes(selector.slice(1))) || null; },
            querySelectorAll() { return []; },
            matches() { return false; }, closest() { return null; },
        };
        if (id) nodes[id] = item;
        return item;
    }
    const tabs = ['overview', 'sections', 'discussion'].map(name => {
        const tab = node('review-tab-' + name);
        tab.dataset.reviewTab = name;
        tab.attrs['aria-controls'] = node('review-panel-' + name).id;
        return tab;
    });
    const workspace = node('workspace');
    workspace.dataset.statusUrl = '/status';
    workspace.dataset.themeToggleUrl = '/toggle_darkmode/';
    node('nav-darkmode-icon');
    const jump = node(); jump.dataset.reviewSection = 'H3';
    jump.closest = selector => selector === '[data-review-show-sections], [data-review-section]' ? jump : null;
    const stage = node('review-stage-H3');
    stage.summary = node(); stage.querySelector = () => stage.summary;
    workspace.querySelectorAll = selector => selector === '[data-review-tab]' ? tabs : [jump];
    const latest = node(); latest.href = '/problem/example/review';
    latest.closest = selector => selector === '[data-review-latest]' ? latest : null;
    workspace.querySelector = selector => selector === '[data-review-latest]' ? latest : nodes[selector.slice(1)] || null;
    const run = node('run-status');
    run.dataset = {status, runId: '7', viewingHistory: history ? '1' : '0', stageProgress: '["D","X"]'};
    const select = node('review-history-select');
    const location = {
        href: 'http://localhost:8000/problem/example/review' + query,
        origin: 'http://localhost:8000', pathname: '/problem/example/review',
        hash: query.includes('#') ? query.slice(query.indexOf('#')) : '',
        assign: url => navigations.push(url), reload: () => { reloads++; },
    };
    const document = {
        hidden: false,
        readyState,
        addEventListener: (key, callback) => { events[key] = callback; },
        removeEventListener: (key, callback) => {
            removedListeners.push(key);
            if (events[key] === callback) delete events[key];
        },
        getElementById: id => nodes[id], querySelector: () => modern ? workspace : null,
        createElement: () => node(),
        importNode: item => item,
    };
    const context = {
        document, URL, Event, gettext: text => text,
        DOMParser: class { parseFromString() { return {querySelector: () => parsed}; } },
        FormData: class { constructor(form) { this.form = form; } },
        window: {location, history: {replaceState: (state, title, url) => { location.href = String(url); }},
            katex: {}, renderKatex: element => mathRenders.push(element),
            dispatchEvent() {}, setInterval: callback => intervals.push(callback),
            clearInterval: id => clearedIntervals.push(id)},
        fetch: (url, options) => { requests.push({url, options}); return response(url, options); },
    };
    vm.runInNewContext(source, context);
    if (readyState === 'loading') events.DOMContentLoaded();
    return {nodes, tabs, workspace, jump, stage, select, latest, location, document, events,
        intervals, requests, navigations, mathRenders, node, reloads: () => reloads,
        removedListeners, clearedIntervals,
        reinitialize() { document.readyState = 'complete'; vm.runInNewContext(source, context); },
        respond: callback => { response = callback; },
        parse: page => { parsed = page; },
        settle: () => new Promise(resolve => setImmediate(resolve)),
    };
}

function liveReport(app, {status = 'D', progress = ['D', 'D']} = {}) {
    const incoming = app.node(), header = app.node();
    header.id = 'run-status';
    header.dataset = {runId: '7', status, viewingHistory: '0', stageProgress: JSON.stringify(progress)};
    header.outerHTML = JSON.stringify(header.dataset);
    const regions = {'#run-status': header};
    incoming.querySelector = selector => regions[selector] || null;
    app.parse(incoming);
    return {incoming, header, regions};
}

function serveLiveReport(app, report) {
    app.respond(async (url, options) => options.headers && options.headers['X-Review-Live'] ?
        {ok: true, text: async () => 'report fragment'} :
        {ok: true, json: async () => ({run: report})});
}

test('sidebar navigation initializes tabs after DOMContentLoaded has already fired', () => {
    for (const readyState of ['interactive', 'complete']) {
        const app = setup({readyState});
        app.tabs[1].listeners.click({});
        assert.equal(app.nodes['review-panel-sections'].hidden, false);
        assert.equal(app.nodes['review-panel-overview'].hidden, true);
        assert.equal(new URL(app.location.href).searchParams.get('tab'), 'sections');
    }
});

test('returning to Review replaces old document handlers and polling', () => {
    const app = setup({status: 'R', readyState: 'complete'});
    const oldClick = app.events.click;
    app.reinitialize();
    assert.ok(app.removedListeners.includes('click'));
    assert.ok(app.removedListeners.includes('submit'));
    assert.deepEqual(app.clearedIntervals, [1]);
    assert.equal(app.intervals.length, 2);
    oldClick({target: {closest: () => app.jump}});
    assert.equal(new URL(app.location.href).searchParams.get('tab'), null);
    app.tabs[1].listeners.click({});
    assert.equal(app.nodes['review-panel-sections'].hidden, false);
});

test('leaving Review stops polling and ignores old page clicks', () => {
    const app = setup({status: 'R', readyState: 'complete'});
    const oldClick = app.tabs[1].listeners.click;
    app.workspace.isConnected = false;
    app.intervals[0]();
    assert.equal(app.requests.length, 0);
    assert.deepEqual(app.clearedIntervals, [1]);
    oldClick({});
    assert.equal(new URL(app.location.href).searchParams.get('tab'), null);
});

test('a late status response cannot reload the page after sidebar navigation away', async () => {
    const app = setup({status: 'R', readyState: 'complete'});
    let finish;
    app.respond(() => new Promise(resolve => { finish = resolve; }));
    app.intervals[0]();
    app.workspace.isConnected = false;
    finish({ok: true, json: async () => ({run: {id: 8, status: 'D', coordinator_stages: []}})});
    await app.settle();
    assert.equal(app.reloads(), 0);
    assert.equal(app.requests.length, 1);
});

test('shareable tabs hide other panels and support keyboard navigation', () => {
    const app = setup({query: '?run=4&tab=sections'});
    assert.equal(app.nodes['review-panel-sections'].hidden, false);
    assert.equal(app.nodes['review-panel-overview'].hidden, true);
    assert.equal(app.tabs[1].attrs['aria-selected'], 'true');
    let prevented = false;
    app.tabs[1].listeners.keydown({key: 'ArrowRight', preventDefault() { prevented = true; }});
    assert.ok(prevented);
    assert.ok(app.tabs[2].focused);
    assert.equal(new URL(app.location.href).searchParams.get('run'), '4');
    assert.equal(new URL(app.location.href).searchParams.get('tab'), 'discussion');
});

test('unknown tabs fall back safely and comment deep links open Discussion', () => {
    assert.equal(setup({query: '?tab=unknown'}).nodes['review-panel-overview'].hidden, false);
    for (const query of ['?target_comment=42', '?tab=overview#comment-42']) {
        assert.equal(setup({query}).nodes['review-panel-discussion'].hidden, false);
    }
});

test('Discussion badge updates from confirmed comment and reply counts without refreshing', () => {
    const app = setup({query: '?tab=discussion'});
    const badge = app.node('review-discussion-count');
    badge.dataset = {contentTypeId: '12', objectId: '7'};
    badge.textContent = '0';
    const posted = detail => app.events['lqdoj:comment-posted']({detail});
    posted({contentTypeId: 12, objectId: 7, count: 1});
    assert.equal(badge.textContent, '1');
    posted({contentTypeId: 12, objectId: 7, count: 2});
    assert.equal(badge.textContent, '2');
    for (const detail of [
        {contentTypeId: 12, objectId: 8, count: 100},
        {contentTypeId: 13, objectId: 7, count: 100},
        {contentTypeId: 12, objectId: 7, count: -1},
        {contentTypeId: 12, objectId: 7, count: 'bad'},
    ]) posted(detail);
    assert.equal(badge.textContent, '2');
    assert.equal(app.reloads(), 0);
});

test('shared comment widget announces server counts for comments and replies only', () => {
    const template = fs.readFileSync(path.join(__dirname, '../../templates/comments/media-js.html'), 'utf8');
    const handler = template.slice(template.indexOf('function commentPosted(xhr)'), template.indexOf('function getCommentPostError'));
    const announcements = [], badge = {length: 1, text: value => { badge.value = value; }};
    const context = {
        $: () => badge, commentConfig: {contentTypeId: 12, objectId: 7},
        document: {dispatchEvent: event => announcements.push(event)},
        CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    };
    vm.runInNewContext(handler, context);
    for (const value of ['1', '2']) context.commentPosted({getResponseHeader: () => value});
    assert.equal(badge.value, '(2)');
    assert.equal(announcements.length, 2);
    assert.equal(announcements[1].detail.count, 2);
    for (const value of [null, '-1', 'bad', 'Infinity']) context.commentPosted({getResponseHeader: () => value});
    assert.equal(announcements.length, 2);
    // Both POST success callbacks use this shared path; editing does not.
    assert.equal((template.match(/commentPosted\(xhr\);/g) || []).length, 2);
    const reply = template.slice(template.indexOf('function submitReply()'), template.indexOf('function submitReply()') + 2500);
    assert.match(reply, /success: function\(response, textStatus, xhr\)\s*\{\s*commentPosted\(xhr\);/);
});

test('theme switch retains the selected historical run and tab', () => {
    const app = setup({query: '?run=4&tab=sections'});
    assert.equal(new URL(app.nodes['nav-darkmode-icon'].href).searchParams.get('next'), '/problem/example/review?run=4&tab=sections');
    app.tabs[2].listeners.click();
    assert.equal(new URL(app.nodes['nav-darkmode-icon'].href).searchParams.get('next'), '/problem/example/review?run=4&tab=discussion');
});

test('finding links open the associated section without another review request', () => {
    const app = setup();
    app.events.click({target: app.jump});
    assert.equal(app.nodes['review-panel-sections'].hidden, false);
    assert.ok(app.stage.open && app.stage.scrolled && app.stage.summary.focused);
    assert.equal(app.requests.length, 0);
});

test('links to supporting steps open their collapsed section ancestors', () => {
    const app = setup();
    const group = app.node(); group.tagName = 'DETAILS'; group.parentElement = app.workspace;
    const evidence = app.node(); evidence.tagName = 'DETAILS'; evidence.parentElement = group;
    app.stage.parentElement = evidence;
    app.events.click({target: app.jump});
    assert.ok(group.open && evidence.open && app.stage.open);
    assert.ok(app.stage.summary.focused);
    assert.equal(app.requests.length, 0);
});

test('View run and return-to-latest preserve the selected tab', () => {
    const app = setup({query: '?run=4&tab=discussion'});
    app.select.value = '/problem/example/review?run=2';
    app.events.change({target: app.select});
    assert.equal(new URL(app.navigations[0]).searchParams.get('run'), '2');
    assert.equal(new URL(app.navigations[0]).searchParams.get('tab'), 'discussion');
    app.events.click({target: app.latest, preventDefault() {}});
    assert.equal(new URL(app.navigations[1]).searchParams.get('run'), null);
    assert.equal(new URL(app.navigations[1]).searchParams.get('tab'), 'discussion');
});

test('historical and completed reports never poll; transient failure preserves evidence', async () => {
    assert.equal(setup({status: 'R', history: true}).intervals.length, 0);
    assert.equal(setup().intervals.length, 0);
    const app = setup({status: 'B'});
    app.respond(async () => { throw Error('offline'); });
    app.intervals[0](); await app.settle();
    assert.equal(app.reloads(), 0);
    app.document.hidden = true;
    app.intervals[0](); await app.settle();
    assert.equal(app.requests.length, 1);
    app.document.hidden = false;
    liveReport(app);
    serveLiveReport(app, {id: 7, status: 'D', coordinator_stages: [{status: 'D'}, {status: 'D'}]});
    app.intervals[0](); await app.settle();
    assert.equal(app.reloads(), 0);
    assert.equal(app.nodes['run-status'].dataset.status, 'D');
    const count = app.requests.length;
    app.intervals[0](); await app.settle();
    assert.equal(app.requests.length, count);
});

test('unchanged progress does not fetch HTML; only a different latest run reloads', async () => {
    const app = setup({status: 'B'});
    const report = {id: 7, status: 'B', coordinator_stages: [{status: 'D'}, {status: 'X'}]};
    app.respond(async () => ({ok: true, json: async () => ({run: report})}));
    for (let poll = 0; poll < 3; poll++) {
        app.intervals[0](); await app.settle();
    }
    assert.equal(app.reloads(), 0);
    report.id = 8;
    app.intervals[0](); await app.settle();
    assert.equal(app.reloads(), 1);
    report.id = 7;
    report.coordinator_stages[1].status = 'D';
    liveReport(app, {status: 'B'});
    serveLiveReport(app, report);
    app.intervals[0](); await app.settle();
    assert.equal(app.reloads(), 1);
    assert.equal(app.nodes['run-status'].dataset.stageProgress, '["D","D"]');
    assert.equal(app.requests.at(-1).options.headers['X-Review-Live'], '1');
});

test('live results preserve open sections, active tab, comment draft and admin input', async () => {
    const app = setup({status: 'R', query: '?tab=sections'});
    const current = app.nodes['review-panel-sections'];
    current.attrs.role = 'tabpanel';
    const previousGroup = app.node(); previousGroup.id = 'review-stage-H3'; previousGroup.open = true;
    current.querySelectorAll = () => [previousGroup];
    const draft = app.node('comment-draft'); draft.value = 'Unfinished comment';
    const admin = app.node('review-decision-feedback'); admin.value = 'Unfinished decision';
    const discussion = app.nodes['review-panel-discussion'];
    const live = liveReport(app);
    const section = app.node(); section.id = current.id; section.outerHTML = 'updated sections';
    const group = app.node(); group.id = previousGroup.id; group.open = false;
    const focusedSummary = app.node();
    group.querySelector = () => focusedSummary;
    app.nodes[group.id] = group;
    app.document.activeElement = {id: '', matches: selector => selector === 'summary', parentElement: {id: group.id}};
    section.querySelectorAll = () => [group];
    live.regions['#review-panel-sections'] = section;
    serveLiveReport(app, {id: 7, status: 'D', coordinator_stages: [{status: 'D'}, {status: 'D'}]});
    app.intervals[0](); await app.settle();
    assert.equal(current.replacement, section);
    assert.ok(app.mathRenders.includes(section));
    assert.equal(section.hidden, false);
    assert.equal(group.open, true);
    assert.equal(focusedSummary.focused, true);
    assert.equal(app.nodes['review-panel-discussion'], discussion);
    assert.equal(draft.value, 'Unfinished comment');
    assert.equal(admin.value, 'Unfinished decision');
    assert.equal(new URL(app.location.href).searchParams.get('tab'), 'sections');
    assert.equal(app.reloads(), 0);
});

test('a late live fragment cannot overwrite a newly opened modal or a different run', async () => {
    const app = setup({status: 'R'});
    const original = app.nodes['run-status'];
    const live = liveReport(app);
    let finish;
    app.respond(async (url, options) => options.headers && options.headers['X-Review-Live'] ?
        new Promise(resolve => { finish = resolve; }) :
        {ok: true, json: async () => ({run: {id: 7, status: 'D'}})});
    app.intervals[0](); await app.settle();
    const dialog = app.node('review-start-dialog'); dialog.showModal();
    finish({ok: true, text: async () => 'late fragment'}); await app.settle();
    assert.equal(app.nodes['run-status'], original);
    dialog.close();
    live.header.dataset.runId = '8';
    serveLiveReport(app, {id: 7, status: 'D'});
    app.intervals[0](); await app.settle();
    assert.equal(app.nodes['run-status'], original);
    assert.equal(app.reloads(), 0);
});

test('failed live fetch retains progress and retries; slow requests never overlap', async () => {
    const app = setup({status: 'R'});
    let finish;
    app.respond(() => new Promise(resolve => { finish = resolve; }));
    app.intervals[0](); app.intervals[0]();
    assert.equal(app.requests.length, 1);
    finish({ok: true, json: async () => ({run: {id: 7, status: 'D'}})});
    await app.settle(); // Fragment is now pending, so polling must remain paused.
    app.intervals[0]();
    assert.equal(app.requests.length, 2);
    finish({ok: false}); await app.settle();
    assert.equal(app.nodes['run-status'].dataset.status, 'R');
    liveReport(app);
    serveLiveReport(app, {id: 7, status: 'D'});
    app.intervals[0](); await app.settle();
    assert.equal(app.nodes['run-status'].dataset.status, 'D');
    assert.equal(app.reloads(), 0);
});

function actionForm(app) {
    const form = app.node();
    form.action = '/publish';
    form.matches = selector => selector === '.coordinator-action-form';
    const publish = app.node(), reject = app.node();
    reject.attrs.formaction = '/reject';
    publish.disabled = true;
    form.querySelectorAll = () => [publish, reject];
    return {form, publish, reject, event: {target: form, submitter: reject, preventDefault() {}}};
}

test('Reject uses its own endpoint, includes the form, and blocks double submission', async () => {
    const app = setup();
    const action = actionForm(app);
    let finish;
    app.respond(() => new Promise(resolve => { finish = resolve; }));
    app.events.submit(action.event);
    app.events.submit(action.event);
    assert.equal(action.form.attrs['aria-busy'], 'true');
    assert.equal(action.reject.textContent, 'Working…');
    assert.equal(app.requests.length, 1);
    assert.equal(app.requests[0].url, '/reject');
    assert.equal(app.requests[0].options.credentials, 'same-origin');
    assert.equal(app.requests[0].options.body.form, action.form);
    finish({redirected: true, url: 'http://localhost:8000/problem/example/review'});
    await app.settle();
    assert.equal(action.form.dataset.reviewSubmitting, undefined);
    assert.equal(action.form.attrs['aria-busy'], 'false');
    assert.equal(action.publish.disabled, true);
    assert.equal(action.reject.disabled, false);
    assert.equal(app.navigations.length, 1);
});

test('request decision switches dialogs without stacking modals or submitting', () => {
    for (const kind of ['contest', 'problem']) {
        const app = setup();
        const decision = app.node(kind + '-request-decision-dialog');
        app.nodes[decision.id] = decision;
        decision.showModal();
        for (const id of [kind + '-request-accept-dialog', kind + '-request-reject-dialog']) {
            const destination = app.node(id);
            app.nodes[id] = destination;
            const opener = app.node();
            opener.dataset.reviewDialog = id;
            opener.closest = selector => selector === 'dialog' ? decision : null;
            const target = app.node();
            target.closest = selector => selector === '[data-review-dialog]' ? opener : null;
            decision.showModal();
            app.events.click({target});
            assert.equal(decision.open, false);
            assert.equal(destination.open, true);
            assert.equal(app.requests.length, 0);
            destination.close();
        }
    }
});

test('review dialogs dismiss on outside clicks, not padding, child clicks, or drags from inside', () => {
    const app = setup();
    for (const id of ['review-start-dialog', 'review-cancel-dialog', 'review-withdraw-dialog', 'contest-request-decision-dialog', 'contest-request-accept-dialog', 'contest-request-reject-dialog', 'problem-request-decision-dialog', 'problem-request-accept-dialog', 'problem-request-reject-dialog']) {
        const dialog = app.node(id);
        dialog.matches = selector => selector === 'dialog.review-dialog';
        dialog.getBoundingClientRect = () => ({left: 100, right: 500, top: 100, bottom: 400});
        const click = (x, y) => ({target: dialog, clientX: x, clientY: y});
        dialog.showModal();
        app.events.pointerdown(click(120, 120));
        app.events.click(click(120, 120));
        assert.equal(dialog.open, true);
        app.events.pointerdown(click(120, 120));
        app.events.click(click(20, 20));
        assert.equal(dialog.open, true);
        const child = app.node();
        app.events.pointerdown({target: child});
        app.events.click({target: child});
        assert.equal(dialog.open, true);
        app.events.pointerdown(click(20, 20));
        app.events.click(click(20, 20));
        assert.equal(dialog.open, false);
    }
    assert.equal(app.requests.length, 0);
});

test('polling cannot interrupt a confirmation dialog or a submitted action', async () => {
    const app = setup({status: 'R'});
    const dialog = app.node('review-cancel-dialog');
    dialog.showModal();
    app.intervals[0]();
    assert.equal(app.requests.length, 0);
    dialog.close();
    let finish;
    app.respond(() => new Promise(resolve => { finish = resolve; }));
    app.intervals[0]();
    dialog.showModal();
    finish({ok: true, json: async () => ({run: {id: 7, status: 'D'}})});
    await app.settle();
    assert.equal(app.reloads(), 0);
    dialog.close();
    const action = actionForm(app);
    app.events.submit(action.event);
    app.intervals[0]();
    assert.equal(app.requests.length, 2);
    finish({success: false, json: async () => ({error: 'Try later'})});
    await app.settle();
    liveReport(app);
    serveLiveReport(app, {id: 7, status: 'D'});
    app.intervals[0](); await app.settle();
    assert.equal(app.reloads(), 0);
    assert.equal(app.nodes['run-status'].dataset.status, 'D');
});

test('network errors are shown as alerts without altering readiness or reloading', async () => {
    const app = setup();
    const action = actionForm(app);
    app.respond(async () => { throw Error('offline'); });
    app.events.submit(action.event); await app.settle();
    const alert = action.form.children[0];
    assert.equal(alert.attrs.role, 'alert');
    assert.match(alert.textContent, /check the current status/);
    assert.equal(app.reloads(), 0);
    assert.equal(action.reject.disabled, false);
});

test('legacy pages retain action handlers but do not initialize tabs or polling', () => {
    const app = setup({modern: false, status: 'R'});
    assert.equal(app.intervals.length, 0);
    assert.equal(typeof app.events.submit, 'function');
    assert.equal(app.tabs[0].listeners.click, undefined);
});

test('Run review loads preparation in the modal with a standalone fallback', async () => {
    const app = setup();
    const dialog = app.node('review-start-dialog'), content = app.node();
    dialog.querySelector = () => content;
    const preparation = app.node();
    app.parse(preparation);
    app.respond(async () => ({ok: true, text: async () => '<html>preparation</html>'}));
    const start = app.node(); start.href = '/reviews/problem/example/run/';
    start.closest = selector => selector === '[data-review-start]' ? start : null;
    let prevented = false;
    const event = {target: start, preventDefault() { prevented = true; }};
    app.events.click(event);
    assert.ok(dialog.open && prevented);
    assert.equal(content.children[0].textContent, 'Loading…');
    assert.equal(content.children[0].attrs.role, 'status');
    await app.settle();
    assert.ok(dialog.open && prevented);
    assert.equal(content.children[0], preparation);
    assert.equal(app.requests[0].url, start.href);
    assert.equal(app.requests[0].options.credentials, 'same-origin');
    assert.equal(app.requests[0].options.headers['X-Review-Fragment'], '1');
    assert.equal(app.reloads(), 0);
    assert.equal(app.navigations.length, 0);
    app.respond(async () => ({ok: false}));
    app.events.click(event); await app.settle();
    assert.equal(app.navigations[0], start.href);
});

test('modal preparation displays server validation errors without starting another request', async () => {
    const app = setup();
    const content = app.node(), dialog = app.node('review-start-dialog'), form = app.node();
    form.action = '/reviews/problem/example/run/';
    form.matches = selector => selector === '.review-start-form';
    form.closest = selector => selector === 'dialog' ? dialog : content;
    const replacement = app.node(); app.parse(replacement);
    app.respond(async () => ({headers: {get: () => 'text/html'}, text: async () => 'validation error'}));
    app.events.submit({target: form, preventDefault() {}}); await app.settle();
    assert.equal(content.children[0], replacement);
    assert.equal(app.requests.length, 1);
    assert.equal(app.requests[0].options.headers['X-Review-Fragment'], '1');
    assert.equal(app.reloads(), 0);
    assert.equal(app.navigations.length, 0);
});

test('closing a loading modal prevents a late failed request from navigating away', async () => {
    const app = setup();
    const dialog = app.node('review-start-dialog'), content = app.node();
    dialog.querySelector = () => content;
    const start = app.node(); start.href = '/reviews/problem/example/run/';
    start.closest = selector => selector === '[data-review-start]' ? start : null;
    let fail;
    app.respond(() => new Promise((resolve, reject) => { fail = reject; }));
    app.events.click({target: start, preventDefault() {}});
    dialog.close();
    fail(Error('offline'));
    await app.settle();
    assert.equal(app.navigations.length, 0);
    assert.equal(dialog.open, false);
});

test('standalone preparation uses its native form submission', () => {
    const app = setup({modern: false}), form = app.node();
    form.matches = selector => selector === '.review-start-form';
    let prevented = false;
    app.events.submit({target: form, preventDefault() { prevented = true; }});
    assert.equal(prevented, false);
    assert.equal(app.requests.length, 0);
});

test('global navigation bar ignores review modal links but keeps normal navigation', () => {
    const common = fs.readFileSync(path.join(__dirname, '../../resources/common.js'), 'utf8');
    const start = common.indexOf("$('a').click(function() {");
    const end = common.indexOf("    $('.errorlist')", start);
    let handler, loads = 0, supported = true;
    vm.runInNewContext(common.slice(start, end), {
        document: {getElementById: () => supported ? {showModal() {}} : null},
        $: value => {
            if (value === 'a') return {click: callback => { handler = callback; }};
            if (value === '#loading-bar') return {show() { loads++; }, animate() {}};
            return {attr: name => name === 'href' ? '/reviews/problem/example/run/' : null};
        },
    });
    const link = (attribute, inDialog = false) => ({
        hasAttribute: name => name === attribute,
        closest: () => inDialog ? {} : null,
    });
    handler.call(link('data-review-start'));
    handler.call(link('data-review-close-link', true));
    assert.equal(loads, 0);
    handler.call(link('normal'));
    assert.equal(loads, 1);
    supported = false;
    handler.call(link('data-review-start'));
    assert.equal(loads, 2);
});
