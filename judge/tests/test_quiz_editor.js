// Run with: node --test judge/tests/test_quiz_editor.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

test('TF authoring and submission preserve special and JSON-like statement IDs', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz.js'), 'utf8');
    const ids = ['__proto__', 'constructor', '[1]', '{"x":1}', 'A'];
    const rows = ids.map(id => ({id}));
    const context = vm.createContext({
        $: target => typeof target === 'string' ? {
            find: selector => ({each: callback => {
                if (selector === '.mtf-statement-row') rows.forEach(row => callback.call(row));
            }}),
        } : {
            find: selector => ({length: 0, val: () => {
                if (selector === '.mtf-statement-id') return target.id;
                if (selector === '.mtf-correct-answer:checked') return 'false';
                return 'Statement';
            }}),
        },
    });
    vm.runInContext(source.slice(source.indexOf('class MultipleTrueFalseEditor'),
        source.indexOf('// Navigation prevention')) + '\nthis.Editor = MultipleTrueFalseEditor;', context);
    const editor = Object.create(context.Editor.prototype);
    editor.container = '#editor';
    editor.updateHiddenFields = () => {};
    editor.updateFromUI();
    const expected = Object.fromEntries(ids.map(id => [id, false]));
    assert.deepEqual(JSON.parse(JSON.stringify(editor.correctAnswers)), expected);

    let handler, saved;
    const studentContext = vm.createContext({
        $: target => target === '.question-mtf' ? {on: (event, callback) => {handler = callback;}} :
            typeof target === 'string' && target.startsWith('input.question-mtf') ? {each: callback => rows.forEach(row => callback.call(row))} :
            typeof target === 'string' ? {find: () => ({val: () => {}})} :
            {data: () => 1, attr: () => target.id, val: () => 'false'},
        autoSaver: {saveAnswer: (id, answer) => {saved = answer;}},
    });
    const start = source.indexOf("    $('.question-mtf').on('change'");
    vm.runInContext(source.slice(start, source.indexOf('    // Prevent navigation', start)), studentContext);
    handler.call({});
    assert.deepEqual(JSON.parse(saved), expected);
});

test('import TF conversion preserves prototype-named IDs', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz_import.js'), 'utf8');
    const context = vm.createContext({window: {}});
    vm.runInContext(source.slice(0, source.indexOf('$(function ()')), context);
    const converted = context.convertQuizImportQuestionType({
        choices: [{id: '__proto__', text: 'Statement'}],
        correct_answers: JSON.parse('{"answers":{"__proto__":false}}'),
    }, 'TF');
    assert.deepEqual(JSON.parse(JSON.stringify(converted.correct_answers)),
        JSON.parse('{"answers":{"__proto__":false}}'));
});

test('import cards show escaped suggestions separately and omit essay answer warnings', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz_import.js'), 'utf8');
    const $ = () => ({val: () => ''});
    $.each = (items, fn) => Object.entries(items).forEach(([key, value]) => fn(key, value));
    const config = {
        questionTypes: {MC: 'Multiple Choice', MA: 'Multiple Answer', TF: 'True/False', SA: 'Short Answer', ES: 'Essay'},
        i18n: new Proxy({}, {get: (target, key) => key}),
        answerInstructions: {MC: 'One choice', MA: 'All choices', TF: 'Every statement', SA: 'Equivalent alternatives'},
    };
    const context = vm.createContext({$, CONFIG: config, questionsData: [], composeQuizTitle: () => ''});
    vm.runInContext(source.slice(source.indexOf('    function renderQuestionCard('), source.indexOf('    // Toggle card body')) +
        source.slice(source.indexOf('    function escapeHtml('), source.lastIndexOf('});')), context);
    for (const [question_type, answers] of [
        ['MC', 'A'], ['MA', ['A', 'B']], ['TF', {A: true, B: false}], ['SA', ['<script>bad</script>']],
    ]) {
        const q = {question_type, title: 'Example', content: 'Question', choices: [{id: 'A', text: 'First'}, {id: 'B', text: 'Second'}],
            correct_answers: null, suggested_answers: {answers}, suggestion_explanation: '<img src=x onerror=bad>'};
        const html = context.renderQuestionCard(0, q);
        assert.match(html, /import-apply-suggestion/);
        assert.match(html, /reviewSuggestion/);
        assert.match(html, /&lt;img/);
        assert.doesNotMatch(html, /<img|<script>/);
        if (question_type === 'TF') assert.match(html, /B: falseAnswer/);
        const essay = context.renderQuestionCard(0, {...q, question_type: 'ES', choices: []});
        assert.doesNotMatch(essay, /import-answer-suggestion|noAnswersHint/);
    }
});

test('import suggestions apply explicitly without losing edits or mutating the source', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz_import.js'), 'utf8');
    const context = vm.createContext({ window: {} });
    vm.runInContext(source.slice(0, source.indexOf('$(function ()')), context);
    for (const [question_type, answers] of [
        ['MC', 'A'], ['MA', ['A', 'B']], ['TF', { A: true, B: false }], ['SA', ['5']],
    ]) {
        const original = {
            question_type, content: 'Original', choices: [{id: 'A', text: 'First'}, {id: 'B', text: 'Second'}],
            correct_answers: null, suggested_answers: {answers}, suggestion_explanation: 'Reason',
        };
        const edited = {...original, title: 'Edited title', multiple_true_false_score_table: [0, 30, 100]};
        const applied = context.applyQuizImportSuggestion(original, edited);
        assert.deepEqual(JSON.parse(JSON.stringify(applied.correct_answers)), {answers});
        assert.equal(applied.title, 'Edited title');
        assert.deepEqual(applied.multiple_true_false_score_table, [0, 30, 100]);
        assert.equal(applied.suggested_answers, null);
        assert.equal(applied.answer_source, 'ai');
        assert.equal(original.correct_answers, null);
        assert.notEqual(applied.correct_answers, original.suggested_answers);
        for (const changed of [
            {...edited, content: 'Changed'}, {...edited, choices: []}, {...edited, question_type: 'ES'},
        ]) {
            assert.equal(context.applyQuizImportSuggestion(original, changed), null);
        }
        const converted = context.convertQuizImportQuestionType(original, 'ES');
        assert.equal(converted.suggested_answers, null);
        assert.equal(converted.suggestion_explanation, '');
        assert.equal(converted.answer_source, null);
    }
});

test('True/False radio groups are unique across editors and stable on rerender', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz.js'), 'utf8');
    const context = vm.createContext({ gettext: text => text });
    vm.runInContext(source.slice(
        source.indexOf('class MultipleTrueFalseEditor'),
        source.indexOf('// Navigation prevention')
    ) + '\nthis.Editor = MultipleTrueFalseEditor;', context);
    const Editor = context.Editor;
    // Exercise construction and HTML generation without a browser or jQuery.
    Editor.prototype.render = function() {};
    Editor.prototype.bindEvents = function() {};
    Editor.prototype.escapeHtml = text => text;
    const config = {
        choices: [{ id: 'A', text: 'First' }, { id: 'B', text: 'Second' }],
        correctAnswers: { A: true, B: false },
    };
    const first = new Editor(config);
    const second = new Editor(config);
    const names = (editor, index) => [...editor.renderStatement(config.choices[index], index)
        .matchAll(/name="([^"]+)"/g)].map(match => match[1]);
    assert.equal(names(first, 0).length, 2);
    assert.equal(names(first, 0)[0], names(first, 0)[1]);
    assert.notEqual(names(first, 0)[0], names(first, 1)[0]);
    assert.notEqual(names(first, 0)[0], names(second, 0)[0]);
    const originalNames = names(first, 0);
    first.render();
    assert.deepEqual(names(first, 0), originalNames);
    const statementHtml = first.renderStatement(config.choices[0], 0);
    assert.match(statementHtml, /mtf-statement-text choice-text-input/);
    assert.match(statementHtml, /expand-choice-btn/);
    assert.match(statementHtml, /choice-expanded-editor/);
});

test('TF score rows group the percentage input and suffix for consistent spacing', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz.js'), 'utf8');
    let html;
    const context = vm.createContext({
        gettext: text => text,
        interpolate: (text, values) => text.replace('%(count)s', values.count),
        $: () => ({
            html: value => { html = value; },
            find: () => ({ each: () => {} }),
        }),
    });
    vm.runInContext(source.slice(source.indexOf('class MultipleTrueFalseEditor'),
        source.indexOf('// Navigation prevention')) + '\nthis.Editor = MultipleTrueFalseEditor;', context);
    const editor = Object.create(context.Editor.prototype);
    editor.choices = [];
    editor.scoreTable = [0, 10, 25, 50, 100];
    editor.escapeHtml = text => text;
    editor.render();
    assert.equal([...html.matchAll(/class="mtf-score-input-group"/g)].length, 5);
    assert.match(html, /<span>0 correct<\/span>/);
    assert.equal([...html.matchAll(/><span>%<\/span><\/span><\/label>/g)].length, 5);
});

test('TF statements reuse the choice Markdown toolbar, preview, and image paste', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz.js'), 'utf8');
    const calls = [];
    class ChoiceEditor {}
    for (const method of ['expandEditor', 'collapseEditor', 'registerImagePaste']) {
        ChoiceEditor.prototype[method] = function(target) {
            calls.push({ method, editor: this, target });
        };
    }
    const context = vm.createContext({ ChoiceEditor });
    vm.runInContext(source.slice(
        source.indexOf('class MultipleTrueFalseEditor'),
        source.indexOf('// Navigation prevention')
    ) + '\nthis.Editor = MultipleTrueFalseEditor;', context);
    const editor = Object.create(context.Editor.prototype);
    const target = {};
    editor.expandEditor(target);
    editor.collapseEditor(target);
    editor.registerImagePaste(target);
    assert.deepEqual(calls.map(call => call.method),
        ['expandEditor', 'collapseEditor', 'registerImagePaste']);
    for (const call of calls) {
        assert.equal(call.editor, editor);
        assert.equal(call.target, target);
    }
});

test('TF editor defaults to one statement and updates default tiers when adding statements', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz.js'), 'utf8');
    const context = vm.createContext({});
    vm.runInContext(source.slice(
        source.indexOf('class MultipleTrueFalseEditor'),
        source.indexOf('// Navigation prevention')
    ) + '\nthis.Editor = MultipleTrueFalseEditor;', context);
    const Editor = context.Editor;
    Editor.prototype.render = function() {};
    Editor.prototype.bindEvents = function() {};
    const editor = new Editor({});
    assert.equal(editor.choices.length, 1);
    assert.deepEqual(Array.from(editor.scoreTable), [0, 100]);
    assert.deepEqual(Array.from(editor.defaultScoreTable(8)), [0, 13, 25, 38, 50, 63, 75, 88, 100]);
    for (let count = 2; count <= 4; count++) {
        editor.choices.push({ id: String(count), text: 'Statement' });
        editor.resizeScoreTable();
    }
    assert.deepEqual(Array.from(editor.scoreTable), [0, 10, 25, 50, 100]);
    editor.scoreTable = [0, 20, 40, 70, 100];
    editor.choices.pop();
    editor.resizeScoreTable();
    assert.deepEqual(Array.from(editor.scoreTable), [0, 20, 40, 100]);
});

test('TF generated IDs remain unique after removals and custom numbered IDs', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz.js'), 'utf8');
    const context = vm.createContext({});
    vm.runInContext(source.slice(source.indexOf('class MultipleTrueFalseEditor'),
        source.indexOf('// Navigation prevention')) + '\nthis.Editor = MultipleTrueFalseEditor;', context);
    const editor = Object.create(context.Editor.prototype);
    editor.choices = Array.from('ABCDEFGHIJKLMNOPQRSTUVWXYZ', id => ({ id }));
    editor.choices.push({ id: 'S27' }, { id: 'S28' });
    editor.choices = editor.choices.filter(choice => choice.id !== 'S27');
    assert.equal(editor.generateId(), 'S29');
    editor.choices.push({ id: 's29' }, { id: 'S30' });
    assert.equal(editor.generateId(), 'S31');
    editor.choices = editor.choices.filter(choice => choice.id !== 'B');
    assert.equal(editor.generateId(), 'B');
});

test('quiz import type conversion rebuilds complete TF answer data', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz_import.js'), 'utf8');
    const context = vm.createContext({ window: {} });
    vm.runInContext(source.slice(0, source.indexOf('$(function ()')),
        context);

    const converted = context.window.convertQuizImportQuestionType({
        question_type: 'MA',
        title: 'Statements',
        content: 'Evaluate each statement.',
        choices: [
            { id: 'A', text: 'First' },
            { id: 'B', text: 'Second' },
            { id: 'C', text: 'Third' },
            { id: 'D', text: 'Fourth' }
        ],
        correct_answers: { answers: ['A', 'C'] }
    }, 'TF');

    assert.equal(converted.question_type, 'TF');
    assert.deepEqual(
        JSON.parse(JSON.stringify(converted.correct_answers.answers)),
        { A: true, B: false, C: true, D: false }
    );
    assert.deepEqual(JSON.parse(JSON.stringify(converted.multiple_true_false_score_table)), []);
});

test('quiz import type conversion maps true TF statements back to MA choices', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz_import.js'), 'utf8');
    const context = vm.createContext({ window: {} });
    vm.runInContext(source.slice(0, source.indexOf('$(function ()')),
        context);

    const converted = context.window.convertQuizImportQuestionType({
        question_type: 'TF',
        choices: [
            { id: 'A', text: 'First' },
            { id: 'B', text: 'Second' }
        ],
        correct_answers: { answers: { A: true, B: false } },
        multiple_true_false_score_table: [0, 25, 100]
    }, 'MA');

    assert.equal(converted.question_type, 'MA');
    assert.deepEqual(JSON.parse(JSON.stringify(converted.correct_answers.answers)), ['A']);
    assert.deepEqual(JSON.parse(JSON.stringify(converted.multiple_true_false_score_table)), []);
});

test('switching an imported question without a key to TF does not invent false answers', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../resources/quiz_import.js'), 'utf8');
    const context = vm.createContext({ window: {} });
    vm.runInContext(source.slice(0, source.indexOf('$(function ()')), context);
    const converted = context.window.convertQuizImportQuestionType({
        question_type: 'MC', choices: [{ id: 'A', text: 'Unknown' }], correct_answers: null
    }, 'TF');
    assert.deepEqual(JSON.parse(JSON.stringify(converted.correct_answers.answers)), {});
});
