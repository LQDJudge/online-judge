function defaultQuizImportChoices(questionType) {
    if (questionType === 'TF') {
        return [{ id: 'A', text: '' }];
    }
    if (['MC', 'MA'].indexOf(questionType) !== -1) {
        return ['A', 'B', 'C', 'D'].map(function (id) {
            return { id: id, text: '' };
        });
    }
    return [];
}

function convertQuizImportQuestionType(question, questionType) {
    var converted = Object.assign({}, question, { question_type: questionType });
    // A suggestion for a different answer format is no longer trustworthy.
    converted.suggested_answers = null;
    converted.suggestion_explanation = '';
    converted.answer_source = null;
    var choices = Array.isArray(question.choices) ? question.choices.map(function (choice) {
        return { id: String(choice.id || ''), text: choice.text || '' };
    }) : [];
    var answerConfig = question.correct_answers || {};
    var answers = answerConfig.answers;
    var selectedIds = [];

    if (Array.isArray(answers)) {
        selectedIds = answers.map(String);
    } else if (typeof answers === 'string' && answers) {
        selectedIds = [answers];
    } else if (answers && typeof answers === 'object') {
        selectedIds = Object.keys(answers).filter(function (id) {
            return answers[id] === true;
        });
    }

    if (questionType === 'TF') {
        if (!choices.length) choices = defaultQuizImportChoices(questionType);
        var statementAnswers = Object.create(null);
        choices.forEach(function (choice) {
            var id = String(choice.id);
            if (answers && typeof answers === 'object' && !Array.isArray(answers) &&
                    typeof answers[id] === 'boolean') {
                statementAnswers[id] = answers[id];
            } else if (selectedIds.length) {
                statementAnswers[id] = selectedIds.indexOf(id) !== -1;
            }
        });
        converted.choices = choices;
        converted.correct_answers = { answers: statementAnswers };
        if (!Array.isArray(converted.multiple_true_false_score_table) ||
                converted.multiple_true_false_score_table.length !== choices.length + 1) {
            converted.multiple_true_false_score_table = [];
        }
        return converted;
    }

    converted.multiple_true_false_score_table = [];
    if (questionType === 'ES') {
        converted.choices = [];
        converted.correct_answers = null;
    } else if (questionType === 'SA') {
        converted.choices = [];
        converted.correct_answers = {
            answers: selectedIds,
            type: 'exact',
            case_sensitive: false
        };
    } else {
        if (!choices.length) choices = defaultQuizImportChoices(questionType);
        var choiceIds = choices.map(function (choice) { return String(choice.id); });
        selectedIds = selectedIds.filter(function (id) {
            return choiceIds.indexOf(String(id)) !== -1;
        });
        converted.choices = choices;
        converted.correct_answers = {
            answers: questionType === 'MA' ? selectedIds : (selectedIds[0] || '')
        };
    }
    return converted;
}

function applyQuizImportSuggestion(original, edited) {
    if (!original.suggested_answers || edited.question_type === 'ES' ||
            original.question_type !== edited.question_type ||
            original.content !== edited.content ||
            JSON.stringify(original.choices) !== JSON.stringify(edited.choices)) {
        return null;
    }
    return Object.assign({}, edited, {
        correct_answers: JSON.parse(JSON.stringify(original.suggested_answers)),
        suggested_answers: null,
        suggestion_explanation: '',
        answer_source: 'ai'
    });
}

if (typeof window !== 'undefined') {
    window.convertQuizImportQuestionType = convertQuizImportQuestionType;
}

$(function () {
    var CONFIG = window.QUIZ_IMPORT_CONFIG;
    if (!CONFIG) return;

    // Pure: build the final question title from an optional prefix + 1-based,
    // zero-padded counter. Empty prefix -> plain base title. Caps at 255 by
    // truncating the base title, never the prefix/counter. Exposed on window
    // for deterministic tests.
    function composeQuizTitle(prefix, index, total, baseTitle) {
        prefix = (prefix || '').trim();
        baseTitle = baseTitle || '';
        if (!prefix) return baseTitle;
        var width = String(Math.max(total, 1)).length;
        var counter = String(index + 1);
        while (counter.length < width) counter = '0' + counter;
        var head = prefix + ' C' + counter + ': ';
        var maxBase = 255 - head.length;
        if (maxBase < 0) maxBase = 0;
        if (baseTitle.length > maxBase) baseTitle = baseTitle.slice(0, maxBase);
        // Final clamp guarantees <= 255 even if the prefix alone overflows.
        return (head + baseTitle).slice(0, 255);
    }
    window.composeQuizImportTitle = composeQuizTitle;

    // State
    var questionsData = [];
    var createdQuestions = {}; // index -> {question_id, question_url}
    var choiceEditors = {}; // index -> ChoiceEditor instance
    var multipleTrueFalseEditors = {}; // index -> MultipleTrueFalseEditor instance

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

    // Live-update each card's title-prefix preview as the prefix is typed.
    $(document).on('input', '#import-title-prefix', function () {
        var prefix = $(this).val();
        $('.import-question-card').each(function () {
            var index = $(this).data('index');
            $(this).find('.import-title-prefix-preview').text(
                composeQuizTitle(prefix, index, questionsData.length, '')
            );
        });
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

        initQuestionCards();
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
        var prefixHead = composeQuizTitle($('#import-title-prefix').val(), index, questionsData.length, '');
        html += '<span class="import-title-prefix-preview">' + escapeHtml(prefixHead) + '</span>';
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
        html += '<label>' + escapeHtml(q.question_type === 'TF' ? CONFIG.i18n.optionalContext : CONFIG.i18n.questionContent) + ':</label>';
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
        var groupedTrueFalse = q.question_type === 'TF';
        if (q.choices && q.choices.length && !groupedTrueFalse) {
            html += '<div class="import-field-row">';
            html += '<label>' + escapeHtml(CONFIG.i18n.answerChoices) + ':</label>';
            html += '<div class="import-choice-editor-container" id="choice-editor-' + index + '"></div>';
            html += '</div>';
        }

        if (groupedTrueFalse) {
            html += '<div class="import-field-row">';
            html += '<label>' + escapeHtml(CONFIG.i18n.trueFalseStatements) + ':</label>';
            html += '<div class="import-mtf-editor-container" id="mtf-editor-' + index + '"></div>';
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
        if (!q.correct_answers && ['SA', 'ES'].indexOf(q.question_type) === -1) {
            html += '<div class="import-field-row import-no-answers"><i class="fa fa-exclamation-triangle"></i> ' + escapeHtml(CONFIG.i18n.noAnswersHint) + '</div>';
        }

        if (q.answer_source) {
            html += '<p class="import-answer-source import-field-meta">' + escapeHtml(
                q.answer_source === 'document' ? CONFIG.i18n.fromDocument : CONFIG.i18n.suggestionApplied
            ) + '</p>';
        }
        if (q.question_type !== 'ES' && (q.suggested_answers || q.suggestion_explanation)) {
            html += '<section class="import-answer-suggestion">';
            html += '<strong>' + escapeHtml(CONFIG.i18n.aiSuggestedAnswer) + '</strong>';
            html += '<p class="import-field-meta">' + escapeHtml(CONFIG.i18n.reviewSuggestion) + '</p>';
            if (q.suggested_answers) {
                html += '<p class="import-field-meta">' + escapeHtml(CONFIG.answerInstructions[q.question_type]) + '</p>';
                var suggested = q.suggested_answers.answers;
                var lines;
                if (q.question_type === 'TF') {
                    lines = (q.choices || []).map(function (choice) {
                        return choice.id + ': ' + (suggested[choice.id] ? CONFIG.i18n.trueAnswer : CONFIG.i18n.falseAnswer);
                    });
                } else {
                    lines = Array.isArray(suggested) ? suggested : [suggested];
                }
                html += '<ul>' + lines.map(function (answer) {
                    return '<li>' + escapeHtml(String(answer)) + '</li>';
                }).join('') + '</ul>';
            }
            if (q.suggestion_explanation) {
                html += '<p class="import-suggestion-explanation">' + escapeHtml(q.suggestion_explanation) + '</p>';
            }
            if (q.suggested_answers) {
                html += '<button type="button" class="action-btn import-apply-suggestion">' + escapeHtml(CONFIG.i18n.applySuggestion) + '</button>';
            }
            html += '<p class="import-suggestion-status" role="status"></p></section>';
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
    function getScopedQuestionCards($scope) {
        if (!$scope) return $('.import-question-card');
        return $scope.filter('.import-question-card').add($scope.find('.import-question-card'));
    }

    function initQuestionCards($scope) {
        initContentEditors($scope);
        initChoiceEditors($scope);
        initMultipleTrueFalseEditors($scope);
    }

    function initContentEditors($scope) {
        if (typeof Markdown === 'undefined') return;
        getScopedQuestionCards($scope).each(function () {
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

                    element.disabled = true;
                    window.uploadPagedownImage(blob)
                        .then(function (imageUrl) {
                            element.disabled = false;
                            element.focus();
                            var markdownImg = '![](' + imageUrl + ')';
                            var start = element.selectionStart;
                            var end = element.selectionEnd;
                            var before = element.value.slice(0, start);
                            var after = element.value.slice(end);
                            if (before) before += '\n';
                            if (after) markdownImg += '\n';
                            element.value = before + markdownImg + after;
                            var pos = before.length + markdownImg.length;
                            element.setSelectionRange(pos, pos);
                        })
                        .catch(function () {
                            element.disabled = false;
                            element.focus();
                        });
                    break;
                }
            }
        });
    }

    // Initialize ChoiceEditor instances for MC/MA/TF questions
    function initChoiceEditors($scope) {
        if (!$scope) choiceEditors = {};
        getScopedQuestionCards($scope).find('.import-choice-editor-container').each(function () {
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

    function initMultipleTrueFalseEditors($scope) {
        if (!$scope) multipleTrueFalseEditors = {};
        getScopedQuestionCards($scope).find('.import-mtf-editor-container').each(function () {
            var index = $(this).closest('.import-question-card').data('index');
            var q = questionsData[index];
            var correctAnswers = q.correct_answers && q.correct_answers.answers;
            multipleTrueFalseEditors[index] = new MultipleTrueFalseEditor({
                container: '#mtf-editor-' + index,
                choices: (q.choices || []).map(function (choice) {
                    return { id: String(choice.id), text: choice.text };
                }),
                correctAnswers: correctAnswers || {},
                scoreTable: q.multiple_true_false_score_table || []
            });
        });
    }

    function snapshotQuestionCard(index) {
        var $card = $('.import-question-card[data-index="' + index + '"]');
        var question = Object.assign({}, questionsData[index]);
        question.title = $card.find('.import-question-title').val() || question.title;
        question.content = $card.find('.import-question-content').val() ?? question.content;

        var choiceEditor = choiceEditors[index];
        if (choiceEditor) {
            choiceEditor.updateFromUI();
            question.choices = choiceEditor.choices;
            question.correct_answers = {
                answers: question.question_type === 'MA' ?
                    choiceEditor.correctAnswers : (choiceEditor.correctAnswers[0] || '')
            };
        }

        var multipleTrueFalseEditor = multipleTrueFalseEditors[index];
        if (multipleTrueFalseEditor) {
            multipleTrueFalseEditor.updateFromUI();
            question.choices = multipleTrueFalseEditor.choices;
            question.correct_answers = { answers: multipleTrueFalseEditor.correctAnswers };
            question.multiple_true_false_score_table = multipleTrueFalseEditor.scoreTable;
        }

        if (question.question_type === 'SA') {
            var acceptedAnswers = [];
            $card.find('.import-sa-answer-input').each(function () {
                var value = $(this).val().trim();
                if (value) acceptedAnswers.push(value);
            });
            question.correct_answers = {
                answers: acceptedAnswers,
                type: 'exact',
                case_sensitive: false
            };
        }
        return question;
    }

    function replaceQuestionCard(index, question) {
        var $card = $('.import-question-card[data-index="' + index + '"]');
        var wasExpanded = $card.find('.import-question-body').hasClass('show');
        questionsData[index] = question;
        delete choiceEditors[index];
        delete multipleTrueFalseEditors[index];
        var $replacement = $(renderQuestionCard(index, question));
        if (wasExpanded) {
            $replacement.find('.import-question-body').addClass('show');
            $replacement.find('.import-chevron').removeClass('fa-chevron-right').addClass('fa-chevron-down');
        }
        $card.replaceWith($replacement);
        initQuestionCards($replacement);
    }

    $questions.on('click', '.import-apply-suggestion', function () {
        var $card = $(this).closest('.import-question-card');
        var index = $card.data('index');
        if (createdQuestions[index]) return;
        var edited = snapshotQuestionCard(index);
        var applied = applyQuizImportSuggestion(questionsData[index], edited);
        if (!applied) {
            $card.find('.import-suggestion-status').text(CONFIG.i18n.staleSuggestion);
            $(this).prop('disabled', true);
            return;
        }
        var answers = edited.correct_answers && edited.correct_answers.answers;
        var hasAnswers = answers && (typeof answers === 'string' ? answers.length : Object.keys(answers).length);
        if (hasAnswers && !window.confirm(CONFIG.i18n.replaceAnswers)) return;
        replaceQuestionCard(index, applied);
    });

    $questions.on('input change', '.correct-checkbox, .mtf-correct-answer, .import-sa-answer-input', function () {
        var $card = $(this).closest('.import-question-card');
        questionsData[$card.data('index')].answer_source = null;
        $card.find('.import-answer-source').remove();
    });

    $questions.on('change', '.import-question-type-select', function () {
        var $card = $(this).closest('.import-question-card');
        var index = $card.data('index');
        if (createdQuestions[index]) return;

        var question = snapshotQuestionCard(index);
        question = convertQuizImportQuestionType(question, $(this).val());
        replaceQuestionCard(index, question);
    });

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
        var baseTitle = $card.find('.import-question-title').val() || q.title;
        var title = composeQuizTitle($('#import-title-prefix').val(), index, questionsData.length, baseTitle);
        var qtype = $card.find('.import-question-type-select').val() || q.question_type;
        var content = $card.find('.import-question-content').val() ?? q.content;

        // Read choices and correct answers from ChoiceEditor
        var choices = q.choices;
        var correctAnswers = q.correct_answers;
        var multipleTrueFalseScoreTable = q.multiple_true_false_score_table;

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


        var multipleTrueFalseEditor = multipleTrueFalseEditors[index];
        if (multipleTrueFalseEditor) {
            multipleTrueFalseEditor.updateFromUI();
            choices = multipleTrueFalseEditor.choices;
            correctAnswers = { answers: multipleTrueFalseEditor.correctAnswers };
            multipleTrueFalseScoreTable = multipleTrueFalseEditor.scoreTable;
        }

        // Read edited SA answers
        var $saInputs = $card.find('.import-sa-answer-input');
        if (qtype === 'SA') {
            var saAnswers = [];
            $saInputs.each(function () {
                var val = $(this).val().trim();
                if (val) saAnswers.push(val);
            });
            correctAnswers = saAnswers.length ? { answers: saAnswers } : null;
        }

        return {
            title: title,
            question_type: qtype,
            content: content,
            choices: choices,
            correct_answers: correctAnswers,
            multiple_true_false_score_table: multipleTrueFalseScoreTable,
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
                $btn.prop('disabled', false).html('<i class="fa fa-exclamation-triangle"></i> ' + escapeHtml(msg));
                setTimeout(function () {
                    $btn.html('<i class="fa fa-plus"></i> ' + escapeHtml(CONFIG.i18n.create));
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
