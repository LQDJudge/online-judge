$(function () {
    var CONFIG = window.QUIZ_IMPORT_CONFIG;
    if (!CONFIG) return;

    // State
    var questionsData = [];
    var createdQuestions = {}; // index -> {question_id, question_url}
    var choiceEditors = {}; // index -> ChoiceEditor instance

    // DOM elements
    var $fileInput = $('#import-file-input');
    var $uploadBtn = $('#import-upload-btn');
    var $status = $('#import-status');
    var $results = $('#import-results');
    var $summary = $('#import-summary');
    var $questions = $('#import-questions');
    var $createAllBtn = $('#import-create-all-btn');

    // Enable upload button when file is selected
    $fileInput.on('change', function () {
        $uploadBtn.prop('disabled', !this.files.length);
    });

    // Upload handler
    $uploadBtn.on('click', function () {
        var file = $fileInput[0].files[0];
        if (!file) return;

        if (file.size > CONFIG.maxUploadSize) {
            showStatus('error', CONFIG.i18n.fileTooLarge);
            return;
        }

        var formData = new FormData();
        formData.append('file', file);

        $uploadBtn.prop('disabled', true);
        showStatus('analyzing', CONFIG.i18n.uploading);

        $.ajax({
            url: CONFIG.uploadUrl,
            type: 'POST',
            data: formData,
            processData: false,
            contentType: false,
            headers: { 'X-CSRFToken': CONFIG.csrfToken },
            success: function (resp) {
                if (resp.success) {
                    showStatus('analyzing', CONFIG.i18n.analyzing);
                    pollTask(resp.task_id);
                } else {
                    showStatus('error', resp.error || CONFIG.i18n.uploadFailed);
                    $uploadBtn.prop('disabled', false);
                }
            },
            error: function (xhr) {
                var msg = CONFIG.i18n.uploadFailed;
                try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                showStatus('error', msg);
                $uploadBtn.prop('disabled', false);
            }
        });
    });

    // Poll task status
    function pollTask(taskId) {
        $.ajax({
            url: CONFIG.taskStatusUrl,
            data: { id: taskId },
            success: function (resp) {
                if (resp.code === 'SUCCESS') {
                    if (resp.success) {
                        showStatus('done', CONFIG.i18n.complete);
                        renderResults(resp);
                    } else {
                        showStatus('error', resp.error || CONFIG.i18n.failed);
                    }
                    $uploadBtn.prop('disabled', false);
                } else if (resp.code === 'FAILURE') {
                    showStatus('error', resp.error || CONFIG.i18n.taskFailed);
                    $uploadBtn.prop('disabled', false);
                } else {
                    // WORKING or PROGRESS
                    setTimeout(function () { pollTask(taskId); }, 4000);
                }
            },
            error: function () {
                showStatus('error', CONFIG.i18n.lostConnection);
                $uploadBtn.prop('disabled', false);
            }
        });
    }

    // Recover last task on page load
    if (CONFIG.lastTaskId) {
        showStatus('analyzing', CONFIG.i18n.loading);
        pollTask(CONFIG.lastTaskId);
    }

    // Status display
    function showStatus(state, message) {
        $status.removeClass('analyzing done error').addClass('show ' + state);
        var icon = state === 'analyzing' ? 'fa-spinner fa-spin' :
                   state === 'done' ? 'fa-check-circle' : 'fa-exclamation-circle';
        $status.html('<i class="fa ' + icon + '"></i> ' + escapeHtml(message));
    }

    // Render extracted questions
    function renderResults(data) {
        questionsData = data.questions || [];
        createdQuestions = {};

        if (!questionsData.length) {
            $summary.html('<div class="import-summary-box">' + escapeHtml(CONFIG.i18n.noQuestions) + '</div>');
            $results.addClass('show');
            return;
        }

        var s = data.summary || {};
        var summaryHtml = '<div class="import-summary-box">';
        summaryHtml += '<div class="import-summary-row"><strong>' + s.total_questions + '</strong> ' + CONFIG.i18n.questions;
        if (s.has_answers > 0) {
            summaryHtml += ' &mdash; <strong>' + s.has_answers + '</strong> ' + CONFIG.i18n.withAnswers;
        }
        summaryHtml += '</div>';

        // Type breakdown
        if (s.type_counts) {
            var types = [];
            $.each(s.type_counts, function (t, c) {
                types.push('<span class="question-type-badge badge-' + t + '">' + (CONFIG.questionTypes[t] || t) + ': ' + c + '</span>');
            });
            summaryHtml += '<div class="import-summary-row">' + types.join(' ') + '</div>';
        }
        summaryHtml += '</div>';
        $summary.html(summaryHtml);

        // Render question cards
        $questions.empty();
        $.each(questionsData, function (i, q) {
            $questions.append(renderQuestionCard(i, q));
        });

        initContentEditors();
        initChoiceEditors();
        $results.addClass('show');
    }

    // Render a single question card
    function renderQuestionCard(index, q) {
        var typeName = CONFIG.questionTypes[q.question_type] || q.question_type;

        var html = '<div class="import-question-card" data-index="' + index + '">';

        // Header (always visible)
        html += '<div class="import-question-header">';
        html += '<i class="fa fa-chevron-right import-chevron"></i>';
        html += '<span class="import-question-num">#' + (index + 1) + '</span>';
        html += '<input type="text" class="import-question-title" value="' + escapeAttr(q.title) + '" data-field="title">';
        html += '<span class="question-type-badge badge-' + q.question_type + '">' + escapeHtml(typeName) + '</span>';
        html += '<button type="button" class="action-btn small import-create-btn" data-index="' + index + '">';
        html += '<i class="fa fa-plus"></i> ' + escapeHtml(CONFIG.i18n.create);
        html += '</button>';
        html += '</div>';

        // Body (collapsed) — includes question-create-container for ChoiceEditor styles
        html += '<div class="import-question-body question-create-container">';

        // Type selector
        html += '<div class="import-field-row"><label>' + escapeHtml(CONFIG.i18n.questionType) + ':</label>';
        html += '<select class="import-question-type-select" data-field="question_type">';
        $.each(CONFIG.questionTypes, function (k, v) {
            html += '<option value="' + k + '"' + (k === q.question_type ? ' selected' : '') + '>' + escapeHtml(v) + '</option>';
        });
        html += '</select></div>';

        // Content with PageDown editor
        var editorId = 'import-content-' + index;
        html += '<div class="import-field-row import-content-editor">';
        html += '<label>' + escapeHtml(CONFIG.i18n.questionContent) + ':</label>';
        html += '<div class="wmd-wrapper">';
        html += '<div id="wmd-button-bar-' + editorId + '" class="wmd-button-bar"></div>';
        html += '<textarea id="wmd-input-' + editorId + '" class="wmd-input import-question-content" data-field="content" rows="6">' + escapeHtml(q.content) + '</textarea>';
        html += '</div>';
        html += '<div id="' + editorId + '-preview" class="dmmd-preview" data-preview-url="' + CONFIG.previewUrl + '" data-textarea-id="wmd-input-' + editorId + '">';
        html += '<div class="dmmd-preview-update"><i class="fa fa-refresh"></i> ' + escapeHtml(CONFIG.i18n.updatePreview) + '</div>';
        html += '<div class="dmmd-preview-content content-description"></div>';
        html += '</div>';
        html += '</div>';

        // Choices (MC/MA/TF) — ChoiceEditor container
        if (q.choices && q.choices.length) {
            html += '<div class="import-field-row">';
            html += '<label>' + escapeHtml(CONFIG.i18n.answerChoices) + ':</label>';
            html += '<div class="import-choice-editor-container" id="choice-editor-' + index + '"></div>';
            html += '</div>';
        }

        // Correct answers for SA — always show editable inputs + add button
        if (q.question_type === 'SA') {
            var answers = [];
            if (q.correct_answers && q.correct_answers.answers) {
                answers = Array.isArray(q.correct_answers.answers) ? q.correct_answers.answers : [q.correct_answers.answers];
            }
            html += '<div class="import-field-row"><label>' + escapeHtml(CONFIG.i18n.acceptedAnswers) + ':</label>';
            if (!answers.length) {
                html += '<div class="import-no-answers-hint"><i class="fa fa-info-circle"></i> ' + escapeHtml(CONFIG.i18n.noAnswersHint) + '</div>';
            }
            html += '<div class="import-sa-answers">';
            $.each(answers, function (ai, ans) {
                html += '<input type="text" class="import-sa-answer-input" data-answer-index="' + ai + '" value="' + escapeAttr(ans) + '">';
            });
            html += '<button type="button" class="import-sa-add-btn" title="' + escapeAttr(CONFIG.i18n.addAnswerHint) + '"><i class="fa fa-plus"></i></button>';
            html += '</div></div>';
        }

        // No answers indicator for non-SA types
        if (!q.correct_answers && q.question_type !== 'SA') {
            html += '<div class="import-field-row import-no-answers"><i class="fa fa-exclamation-triangle"></i> ' + escapeHtml(CONFIG.i18n.noAnswersHint) + '</div>';
        }

        html += '</div>'; // end body
        html += '</div>'; // end card
        return html;
    }

    // Toggle card body on header click
    $questions.on('click', '.import-question-header', function (e) {
        if ($(e.target).is('input, button') || $(e.target).closest('button').length) return;
        var $body = $(this).siblings('.import-question-body');
        $body.toggleClass('show');
        $(this).find('.import-chevron').toggleClass('fa-chevron-right fa-chevron-down');

        // Auto-resize textareas now that they're visible (scrollHeight is 0 when hidden)
        if ($body.hasClass('show')) {
            $body.find('.auto-resize-textarea, .choice-text-input').each(function () {
                if (typeof autoResizeTextarea === 'function') {
                    autoResizeTextarea(this);
                }
            });
        }
    });

    // Initialize PageDown editors for content fields
    function initContentEditors() {
        if (typeof Markdown === 'undefined') return;
        $('.import-question-card').each(function () {
            var index = $(this).data('index');
            var editorId = 'import-content-' + index;
            var converter = Markdown.getSanitizingConverter();
            if (typeof Markdown.Extra !== 'undefined') {
                Markdown.Extra.init(converter, { extensions: 'all' });
            }
            var editor = new Markdown.Editor(converter, '-' + editorId, {});
            editor.run();

            // Register dmmd-preview for this editor
            var $preview = $('#' + editorId + '-preview');
            if ($preview.length && typeof register_dmmd_preview === 'function') {
                register_dmmd_preview($preview);
            }

            // Enable image paste on the textarea
            var textarea = document.getElementById('wmd-input-' + editorId);
            if (textarea) {
                registerClipboardImageUpload(textarea);
            }
        });
    }

    // Clipboard image paste handler for PageDown textareas
    function registerClipboardImageUpload(element) {
        element.addEventListener('paste', function (event) {
            var clipboardData = event.clipboardData || window.clipboardData;
            if (!clipboardData || !clipboardData.items) return;

            for (var i = 0; i < clipboardData.items.length; i++) {
                var item = clipboardData.items[i];
                if (item.kind === 'file' && item.type.indexOf('image/') === 0) {
                    event.preventDefault();
                    var blob = item.getAsFile();
                    var formData = new FormData();
                    formData.append('image', blob);

                    element.disabled = true;

                    var xhr = new XMLHttpRequest();
                    xhr.open('POST', '/pagedown/image-upload/', true);
                    xhr.onload = function () {
                        element.disabled = false;
                        element.focus();
                        if (xhr.status === 200) {
                            try {
                                var response = JSON.parse(xhr.responseText);
                                var markdownImg = '![](' + response.url + ')';
                                var start = element.selectionStart;
                                var end = element.selectionEnd;
                                var before = element.value.slice(0, start);
                                var after = element.value.slice(end);
                                if (before) before += '\n';
                                if (after) markdownImg += '\n';
                                element.value = before + markdownImg + after;
                                var pos = before.length + markdownImg.length;
                                element.setSelectionRange(pos, pos);
                            } catch (e) {}
                        }
                    };
                    xhr.onerror = function () {
                        element.disabled = false;
                        element.focus();
                    };
                    xhr.send(formData);
                    break;
                }
            }
        });
    }

    // Initialize ChoiceEditor instances for MC/MA/TF questions
    function initChoiceEditors() {
        choiceEditors = {};
        $('.import-choice-editor-container').each(function () {
            var index = $(this).closest('.import-question-card').data('index');
            var q = questionsData[index];
            var correctAnswers = [];
            if (q.correct_answers && q.correct_answers.answers) {
                var ans = q.correct_answers.answers;
                correctAnswers = Array.isArray(ans) ? ans.map(String) : [String(ans)];
            }
            var editor = new ChoiceEditor({
                container: '#choice-editor-' + index,
                inputField: '#import-hidden-' + index,
                questionType: q.question_type,
                choices: (q.choices || []).map(function (c) { return { id: String(c.id), text: c.text }; }),
                correctAnswers: correctAnswers
            });
            choiceEditors[index] = editor;
        });
    }

    // Add new SA answer input
    $questions.on('click', '.import-sa-add-btn', function () {
        var $btn = $(this);
        var $container = $btn.closest('.import-sa-answers');
        var newIndex = $container.find('.import-sa-answer-input').length;
        $('<input type="text" class="import-sa-answer-input" data-answer-index="' + newIndex + '" value="" placeholder="New answer">').insertBefore($btn).focus();
    });

    // Create single question
    $questions.on('click', '.import-create-btn', function (e) {
        e.stopPropagation();
        var $btn = $(this);
        var index = $btn.data('index');
        if (createdQuestions[index]) return;

        createQuestion(index, $btn);
    });

    function getQuestionData(index) {
        var $card = $('.import-question-card[data-index="' + index + '"]');
        var q = questionsData[index];

        // Read edited values from inputs
        var title = $card.find('.import-question-title').val() || q.title;
        var qtype = $card.find('.import-question-type-select').val() || q.question_type;
        var content = $card.find('.import-question-content').val() || q.content;

        // Read choices and correct answers from ChoiceEditor
        var choices = q.choices;
        var correctAnswers = q.correct_answers;

        var editor = choiceEditors[index];
        if (editor) {
            editor.updateFromUI();
            choices = editor.choices;
            if (qtype === 'MA') {
                correctAnswers = { answers: editor.correctAnswers };
            } else {
                correctAnswers = { answers: editor.correctAnswers[0] || '' };
            }
        }

        // Read edited SA answers
        var $saInputs = $card.find('.import-sa-answer-input');
        if ($saInputs.length) {
            var saAnswers = [];
            $saInputs.each(function () {
                var val = $(this).val().trim();
                if (val) saAnswers.push(val);
            });
            if (saAnswers.length) {
                correctAnswers = { answers: saAnswers };
            }
        }

        return {
            title: title,
            question_type: qtype,
            content: content,
            choices: choices,
            correct_answers: correctAnswers,
            shuffle_choices: $('#import-shuffle-choices').is(':checked'),
            is_public: $('#import-is-public').is(':checked')
        };
    }

    function createQuestion(index, $btn) {
        var data = getQuestionData(index);

        $btn.prop('disabled', true).html('<i class="fa fa-spinner fa-spin"></i> ' + CONFIG.i18n.creating);

        $.ajax({
            url: CONFIG.createQuestionUrl,
            type: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(data),
            headers: { 'X-CSRFToken': CONFIG.csrfToken },
            success: function (resp) {
                if (resp.success) {
                    createdQuestions[index] = resp;
                    $btn.replaceWith(
                        '<a href="' + resp.question_url + '" class="import-created-link" target="_blank">' +
                        '<i class="fa fa-check"></i> ' + CONFIG.i18n.created +
                        '</a>'
                    );
                } else {
                    $btn.prop('disabled', false).html('<i class="fa fa-plus"></i> ' + CONFIG.i18n.createFailed);
                }
            },
            error: function (xhr) {
                var msg = CONFIG.i18n.createFailed;
                try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                $btn.prop('disabled', false).html('<i class="fa fa-exclamation-triangle"></i> ' + msg);
                setTimeout(function () {
                    $btn.html('<i class="fa fa-plus"></i> Create');
                }, 3000);
            }
        });
    }

    // Create all questions
    $createAllBtn.on('click', function () {
        var $btn = $(this);
        var $span = $btn.find('span');
        var uncreated = [];

        for (var i = 0; i < questionsData.length; i++) {
            if (!createdQuestions[i]) uncreated.push(i);
        }
        if (!uncreated.length) return;

        $btn.prop('disabled', true);
        $span.text(CONFIG.i18n.creatingAll);

        var completed = 0;
        var total = uncreated.length;

        function createNext() {
            if (!uncreated.length) {
                $btn.prop('disabled', false);
                $span.text(CONFIG.i18n.createAll);
                return;
            }
            var idx = uncreated.shift();
            var data = getQuestionData(idx);

            $.ajax({
                url: CONFIG.createQuestionUrl,
                type: 'POST',
                contentType: 'application/json',
                data: JSON.stringify(data),
                headers: { 'X-CSRFToken': CONFIG.csrfToken },
                success: function (resp) {
                    completed++;
                    $span.text(CONFIG.i18n.creatingAll + ' (' + completed + '/' + total + ')');
                    if (resp.success) {
                        createdQuestions[idx] = resp;
                        var $card = $('.import-question-card[data-index="' + idx + '"]');
                        $card.find('.import-create-btn').replaceWith(
                            '<a href="' + resp.question_url + '" class="import-created-link" target="_blank">' +
                            '<i class="fa fa-check"></i> ' + CONFIG.i18n.created +
                            '</a>'
                        );
                    }
                    createNext();
                },
                error: function () {
                    completed++;
                    createNext();
                }
            });
        }
        createNext();
    });

    // Quiz section toggle
    $('#import-quiz-toggle').on('click', function () {
        var $body = $('#import-quiz-body');
        var $icon = $(this).find('i');
        $body.toggleClass('show');
        $icon.toggleClass('fa-chevron-right fa-chevron-down');
    });

    // Create quiz
    $('#import-create-quiz-btn').on('click', function () {
        var $btn = $(this);
        var $statusDiv = $('#quiz-create-status');

        var code = $('#quiz-code').val().trim();
        var title = $('#quiz-title').val().trim();

        // Validate
        var errors = [];
        if (!code) errors.push({ field: '#quiz-code', msg: CONFIG.i18n.required });
        if (!title) errors.push({ field: '#quiz-title', msg: CONFIG.i18n.required });
        if (code && !/^[a-z0-9]+$/.test(code)) errors.push({ field: '#quiz-code', msg: 'a-z, 0-9 only' });

        $('.import-form-row input, .import-form-row textarea').removeClass('import-field-error');
        if (errors.length) {
            $.each(errors, function (_, e) { $(e.field).addClass('import-field-error'); });
            return;
        }

        $btn.prop('disabled', true);
        $statusDiv.html('<i class="fa fa-spinner fa-spin"></i> ' + CONFIG.i18n.creating);

        // First, create any uncreated questions
        var uncreated = [];
        for (var i = 0; i < questionsData.length; i++) {
            if (!createdQuestions[i]) uncreated.push(i);
        }

        function doCreateQuiz() {
            var questionIds = [];
            for (var i = 0; i < questionsData.length; i++) {
                if (createdQuestions[i]) {
                    questionIds.push(createdQuestions[i].question_id);
                }
            }

            if (!questionIds.length) {
                $statusDiv.html('<i class="fa fa-exclamation-triangle"></i> No questions created');
                $btn.prop('disabled', false);
                return;
            }

            $.ajax({
                url: CONFIG.createQuizUrl,
                type: 'POST',
                contentType: 'application/json',
                data: JSON.stringify({
                    code: code,
                    title: title,
                    time_limit: parseInt($('#quiz-time-limit').val()) || 0,
                    shuffle_questions: $('#quiz-shuffle-questions').is(':checked'),
                    is_shown_answer: $('#quiz-show-answers').is(':checked'),
                    is_public: $('#quiz-is-public').is(':checked'),
                    question_ids: questionIds
                }),
                headers: { 'X-CSRFToken': CONFIG.csrfToken },
                success: function (resp) {
                    if (resp.success) {
                        $statusDiv.html('<i class="fa fa-check-circle"></i> ' + CONFIG.i18n.quizCreated);
                        window.location.href = resp.quiz_url;
                    } else {
                        $statusDiv.html('<i class="fa fa-exclamation-triangle"></i> ' + escapeHtml(resp.error));
                        $btn.prop('disabled', false);
                    }
                },
                error: function (xhr) {
                    var msg = CONFIG.i18n.createFailed;
                    try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                    $statusDiv.html('<i class="fa fa-exclamation-triangle"></i> ' + escapeHtml(msg));
                    $btn.prop('disabled', false);
                }
            });
        }

        if (uncreated.length) {
            // Auto-create uncreated questions first
            var completed = 0;
            function autoCreateNext() {
                if (!uncreated.length) {
                    doCreateQuiz();
                    return;
                }
                var idx = uncreated.shift();
                var data = getQuestionData(idx);
                $.ajax({
                    url: CONFIG.createQuestionUrl,
                    type: 'POST',
                    contentType: 'application/json',
                    data: JSON.stringify(data),
                    headers: { 'X-CSRFToken': CONFIG.csrfToken },
                    success: function (resp) {
                        completed++;
                        $statusDiv.html('<i class="fa fa-spinner fa-spin"></i> ' +
                            CONFIG.i18n.creatingAll + ' (' + completed + '/' + (completed + uncreated.length) + ')');
                        if (resp.success) {
                            createdQuestions[idx] = resp;
                            var $card = $('.import-question-card[data-index="' + idx + '"]');
                            $card.find('.import-create-btn').replaceWith(
                                '<a href="' + resp.question_url + '" class="import-created-link" target="_blank">' +
                                '<i class="fa fa-check"></i> ' + CONFIG.i18n.created +
                                '</a>'
                            );
                        }
                        autoCreateNext();
                    },
                    error: function () {
                        completed++;
                        autoCreateNext();
                    }
                });
            }
            autoCreateNext();
        } else {
            doCreateQuiz();
        }
    });

    // Utility functions
    function escapeHtml(str) {
        if (!str) return '';
        return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function escapeAttr(str) {
        if (!str) return '';
        return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
});
