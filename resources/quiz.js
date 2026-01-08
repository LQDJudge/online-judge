/**
 * Quiz System JavaScript
 * Reusable utilities for quiz functionality
 */

// Quiz Timer Class
class QuizTimer {
    constructor(timeLimit, onExpire, displayElement) {
        this.timeLimit = timeLimit; // seconds
        this.onExpire = onExpire;
        this.displayElement = displayElement;
        this.startTime = Date.now();
        this.interval = null;
    }

    start() {
        this.tick(); // Initial display
        this.interval = setInterval(() => this.tick(), 1000);
    }

    tick() {
        const elapsed = Math.floor((Date.now() - this.startTime) / 1000);
        const remaining = this.timeLimit - elapsed;

        if (remaining <= 0) {
            this.expire();
        } else {
            this.updateDisplay(remaining);
        }
    }

    updateDisplay(seconds) {
        const hours = Math.floor(seconds / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        const secs = seconds % 60;

        let display;
        if (hours > 0) {
            display = hours + ':' + String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
        } else {
            display = mins + ':' + String(secs).padStart(2, '0');
        }

        $(this.displayElement).text(display);

        // Warning colors
        if (seconds <= 60) {
            $(this.displayElement).removeClass('timer-warning').addClass('timer-danger');
        } else if (seconds <= 300) {
            $(this.displayElement).addClass('timer-warning');
        }
    }

    expire() {
        clearInterval(this.interval);
        this.onExpire();
    }

    stop() {
        if (this.interval) {
            clearInterval(this.interval);
        }
    }
}

// Quiz Navigator Class for prev/next navigation
class QuizNavigator {
    constructor(config) {
        this.questions = config.questions;
        this.currentIndex = config.currentIndex || 0;
        this.saveUrl = config.saveUrl;
        this.csrfToken = config.csrfToken;
        this.onQuestionChange = config.onQuestionChange || function() {};

        this.init();
    }

    init() {
        this.showQuestion(this.currentIndex);
        this.updateButtons();
        this.updateProgress();
    }

    next() {
        if (this.currentIndex < this.questions.length - 1) {
            this.saveCurrent();
            this.currentIndex++;
            this.showQuestion(this.currentIndex);
            this.updateButtons();
            this.updateProgress();
        }
    }

    prev() {
        if (this.currentIndex > 0) {
            this.saveCurrent();
            this.currentIndex--;
            this.showQuestion(this.currentIndex);
            this.updateButtons();
            this.updateProgress();
        }
    }

    goTo(index) {
        if (index >= 0 && index < this.questions.length && index !== this.currentIndex) {
            this.saveCurrent();
            this.currentIndex = index;
            this.showQuestion(this.currentIndex);
            this.updateButtons();
            this.updateProgress();
        }
    }

    showQuestion(index) {
        $('.question-card').hide();
        $('#question-' + this.questions[index].id).show();
        this.onQuestionChange(index, this.questions[index]);
    }

    updateButtons() {
        $('#prev-btn').prop('disabled', this.currentIndex === 0);
        $('#next-btn').prop('disabled', this.currentIndex === this.questions.length - 1);

        // Show/hide submit button on last question
        if (this.currentIndex === this.questions.length - 1) {
            $('#next-btn').hide();
            $('#submit-btn').show();
        } else {
            $('#next-btn').show();
            $('#submit-btn').hide();
        }
    }

    updateProgress() {
        var current = this.currentIndex + 1;
        var total = this.questions.length;
        $('#question-progress').text(current + ' / ' + total);

        // Update progress bar if exists
        var percent = (current / total) * 100;
        $('#progress-bar').css('width', percent + '%');

        // Update question indicators
        $('.question-indicator').removeClass('current');
        $('.question-indicator[data-index="' + this.currentIndex + '"]').addClass('current');
    }

    saveCurrent() {
        var questionId = this.questions[this.currentIndex].id;
        var answer = this.collectAnswer(questionId);
        this.saveAnswer(questionId, answer);
    }

    collectAnswer(questionId) {
        var $card = $('#question-' + questionId);
        var answer;

        // Radio buttons (MC/TF)
        var $radio = $card.find('input[type="radio"]:checked');
        if ($radio.length) {
            return $radio.val();
        }

        // Checkboxes (MA)
        var $checkboxes = $card.find('input[type="checkbox"]:checked');
        if ($card.find('input[type="checkbox"]').length) {
            var checked = [];
            $checkboxes.each(function() {
                checked.push($(this).val());
            });
            return JSON.stringify(checked);
        }

        // Text input (SA)
        var $textInput = $card.find('input[type="text"].question-text');
        if ($textInput.length) {
            return $textInput.val();
        }

        // Textarea (ES)
        var $textarea = $card.find('textarea.question-text');
        if ($textarea.length) {
            return $textarea.val();
        }

        return '';
    }

    saveAnswer(questionId, answer) {
        if (!this.saveUrl) return;

        $.ajax({
            url: this.saveUrl,
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({
                question_id: questionId,
                answer: answer
            }),
            headers: {
                'X-CSRFToken': this.csrfToken
            }
        });
    }

    saveAll() {
        for (var i = 0; i < this.questions.length; i++) {
            var questionId = this.questions[i].id;
            var answer = this.collectAnswer(questionId);
            this.saveAnswer(questionId, answer);
        }
    }
}

// Markdown toolbar helper - inserts text around selection
function insertMarkdown($textarea, before, after, placeholder) {
    var textarea = $textarea[0];
    var start = textarea.selectionStart;
    var end = textarea.selectionEnd;
    var text = textarea.value;
    var selected = text.substring(start, end) || placeholder || '';

    var newText = text.substring(0, start) + before + selected + after + text.substring(end);
    textarea.value = newText;
    $textarea.trigger('input');

    // Set cursor position
    var newCursorPos = start + before.length + selected.length;
    textarea.setSelectionRange(newCursorPos, newCursorPos);
    textarea.focus();
}

// Create markdown toolbar for choice/answer editors
function createMarkdownToolbar($textarea) {
    var $toolbar = $('<div class="markdown-toolbar-mini"></div>');

    // Toolbar buttons with their markdown syntax
    var buttons = [
        { icon: 'fa-bold', title: 'Bold', before: '**', after: '**', placeholder: 'bold text' },
        { icon: 'fa-italic', title: 'Italic', before: '*', after: '*', placeholder: 'italic text' },
        { icon: 'fa-code', title: 'Inline Code', before: '`', after: '`', placeholder: 'code' },
        { icon: 'fa-superscript', title: 'Math (inline)', before: '~', after: '~', placeholder: 'x^2' },
        { icon: 'fa-square-root-alt', title: 'Math (block)', before: '$$\n', after: '\n$$', placeholder: '\\sum_{i=1}^n i' },
        { icon: 'fa-terminal', title: 'Code Block', before: '```\n', after: '\n```', placeholder: 'code' },
        { icon: 'fa-link', title: 'Link', before: '[', after: '](url)', placeholder: 'link text' },
    ];

    buttons.forEach(function(btn) {
        var $btn = $('<button type="button" class="toolbar-btn" title="' + btn.title + '"><i class="fa ' + btn.icon + '"></i></button>');
        $btn.on('click', function(e) {
            e.preventDefault();
            insertMarkdown($textarea, btn.before, btn.after, btn.placeholder);
        });
        $toolbar.append($btn);
    });

    return $toolbar;
}

// Inline Expandable Text Editor for quiz choices/answers
// Replaces input with expanded textarea inline (like comment editor)
function expandInlineEditor($input, onCollapse) {
    // Already expanded?
    if ($input.hasClass('expanded-editor')) return;

    var originalValue = $input.val();
    var $container = $input.closest('.choice-item, .sa-answer-row');

    // Create expanded editor wrapper
    var $wrapper = $('<div class="inline-expanded-wrapper"></div>');
    var $textarea = $('<textarea class="inline-expanded-textarea" rows="5" placeholder="' + gettext('Enter your text here...') + '"></textarea>');
    $textarea.val(originalValue);

    // Add markdown toolbar
    var $toolbar = createMarkdownToolbar($textarea);

    var $actions = $('<div class="inline-expanded-actions"></div>');
    var $collapseBtn = $('<button type="button" class="btn btn-sm collapse-editor-btn" title="' + gettext('Collapse') + '"><i class="fa fa-compress"></i></button>');

    $actions.append($collapseBtn);
    $wrapper.append($toolbar);
    $wrapper.append($textarea);
    $wrapper.append($actions);

    // Hide original input and expand button, show expanded editor
    $input.addClass('expanded-editor').hide();
    $input.siblings('.expand-choice-btn, .expand-sa-btn').hide();
    $input.after($wrapper);

    // Focus textarea
    $textarea.focus();
    $textarea[0].setSelectionRange($textarea.val().length, $textarea.val().length);

    // Sync on input
    $textarea.on('input', function() {
        $input.val($textarea.val());
        $input.trigger('input');
    });

    // Collapse function
    function collapse() {
        $input.val($textarea.val());
        $input.trigger('input');
        $wrapper.remove();
        $input.removeClass('expanded-editor').show();
        $input.siblings('.expand-choice-btn, .expand-sa-btn').show();
        if (typeof onCollapse === 'function') {
            onCollapse();
        }
    }

    // Collapse button
    $collapseBtn.on('click', function(e) {
        e.preventDefault();
        collapse();
    });

    // Collapse on Escape
    $textarea.on('keydown', function(e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            collapse();
        }
    });
}

// Helper function to get expandable editor (for compatibility with existing code)
function getExpandableTextEditor() {
    return {
        open: function(inputElement, title, onSave) {
            expandInlineEditor($(inputElement), onSave);
        }
    };
}

// Choice Editor for question creation/editing
// Uses inline PageDown editor for each choice text field with expand/collapse
class ChoiceEditor {
    constructor(config) {
        this.container = config.container;
        this.inputField = config.inputField; // Hidden input for JSON
        this.questionType = config.questionType;
        this.choices = config.choices || [];
        this.correctAnswers = config.correctAnswers || [];
        this.expandedEditors = {}; // Track expanded state by index

        this.init();
    }

    init() {
        this.render();
        this.bindEvents();
    }

    render() {
        var html = '<div class="choice-editor">';
        html += '<div class="choice-list" id="choice-list">';

        for (var i = 0; i < this.choices.length; i++) {
            html += this.renderChoice(this.choices[i], i);
        }

        html += '</div>';
        html += '<button type="button" class="btn btn-sm btn-success add-choice-btn">';
        html += '<i class="fa fa-plus"></i> ' + gettext('Add Choice') + '</button>';
        html += '</div>';

        $(this.container).html(html);

        // Initialize auto-resize for all textareas
        $(this.container).find('.choice-text-input').each(function() {
            initAutoResizeTextarea($(this));
        });
    }

    renderChoice(choice, index) {
        var isCorrect = this.isCorrect(choice.id);
        var inputType = (this.questionType === 'MA') ? 'checkbox' : 'radio';

        var html = '<div class="choice-item" data-id="' + choice.id + '" data-index="' + index + '">';
        // Row 1: Controls and minimal textarea
        html += '<div class="choice-row-controls">';
        html += '<span class="drag-handle"><i class="fa fa-bars"></i></span>';
        html += '<input type="' + inputType + '" name="correct_choice" value="' + choice.id + '"';
        if (isCorrect) html += ' checked';
        html += ' class="correct-checkbox">';
        html += '<input type="text" class="choice-id" value="' + this.escapeHtml(choice.id) + '" title="' + gettext('Choice ID (e.g., A, B, C)') + '">';
        html += '<textarea class="choice-text-input auto-resize-textarea" rows="1" placeholder="' + gettext('Choice text') + '">' + this.escapeHtml(choice.text) + '</textarea>';
        html += '<button type="button" class="btn btn-sm expand-choice-btn" title="' + gettext('Expand with toolbar') + '"><i class="fa fa-expand"></i></button>';
        html += '<button type="button" class="btn btn-sm btn-danger remove-choice-btn"><i class="fa fa-times"></i></button>';
        html += '</div>';
        // Row 2: Expanded editor (hidden by default)
        html += '<div class="choice-expanded-editor" style="display: none;"></div>';
        html += '</div>';

        return html;
    }

    bindEvents() {
        var self = this;
        var $container = $(this.container);

        // Unbind previous events to prevent duplicates
        $container.off('click', '.add-choice-btn');
        $container.off('click', '.remove-choice-btn');
        $container.off('click', '.expand-choice-btn');
        $container.off('click', '.collapse-choice-btn');
        $container.off('input', '.choice-id');
        $container.off('input', '.choice-text-input');
        $container.off('change', '.correct-checkbox');

        // Add choice
        $container.on('click', '.add-choice-btn', function() {
            self.addChoice();
        });

        // Remove choice
        $container.on('click', '.remove-choice-btn', function() {
            var $item = $(this).closest('.choice-item');
            self.removeChoice($item.data('id'));
        });

        // Expand editor with markdown toolbar
        $container.on('click', '.expand-choice-btn', function() {
            var $item = $(this).closest('.choice-item');
            self.expandEditor($item);
        });

        // Collapse editor
        $container.on('click', '.collapse-choice-btn', function() {
            var $item = $(this).closest('.choice-item');
            self.collapseEditor($item);
        });

        // Update on ID change
        $container.on('input', '.choice-id', function() {
            self.updateFromUI();
        });

        // Update on text change
        $container.on('input', '.choice-text-input', function() {
            self.updateFromUI();
        });

        // Update on correct answer change
        $container.on('change', '.correct-checkbox', function() {
            self.updateFromUI();
        });

        // Make sortable if jQuery UI is available
        if ($.fn.sortable) {
            $container.find('.choice-list').sortable({
                handle: '.drag-handle',
                update: function() {
                    self.updateFromUI();
                }
            });
        }
    }

    expandEditor($item) {
        var $controls = $item.find('.choice-row-controls');
        var $expandedContainer = $item.find('.choice-expanded-editor');
        var $textarea = $controls.find('.choice-text-input');
        var $expandBtn = $controls.find('.expand-choice-btn');
        var index = $item.data('index');

        // Already expanded?
        if ($expandedContainer.is(':visible')) return;

        // Create the expanded editor with PageDown toolbar
        var editorId = 'choice-editor-' + index + '-' + Date.now();
        var currentValue = $textarea.val();

        var expandedHtml = '<div class="wmd-wrapper choice-wmd-wrapper">';
        expandedHtml += '<div class="choice-expanded-header">';
        expandedHtml += '<div id="wmd-button-bar-' + editorId + '" class="wmd-button-bar"></div>';
        expandedHtml += '<button type="button" class="btn btn-sm collapse-choice-btn" title="' + gettext('Collapse') + '"><i class="fa fa-compress"></i></button>';
        expandedHtml += '</div>';
        expandedHtml += '<textarea id="wmd-input-' + editorId + '" class="wmd-input choice-expanded-textarea">' + this.escapeHtml(currentValue) + '</textarea>';
        expandedHtml += '</div>';

        $expandedContainer.html(expandedHtml);
        $expandedContainer.show();

        // Hide the minimal textarea row but keep choice controls visible
        $textarea.hide();
        $expandBtn.hide();

        // Initialize PageDown editor if available
        var self = this;
        if (typeof Markdown !== 'undefined') {
            var converter = Markdown.getSanitizingConverter();
            if (typeof Markdown.Extra !== 'undefined') {
                Markdown.Extra.init(converter, { extensions: 'all' });
            }
            var editor = new Markdown.Editor(converter, '-' + editorId, {});
            editor.run();
            this.expandedEditors[index] = editor;
        }

        // Sync expanded textarea with the minimal one
        var $expandedTextarea = $expandedContainer.find('.choice-expanded-textarea');
        $expandedTextarea.on('input', function() {
            $textarea.val($(this).val());
            self.updateFromUI();
        });

        // Focus the expanded textarea
        $expandedTextarea.focus();
    }

    collapseEditor($item) {
        var $controls = $item.find('.choice-row-controls');
        var $expandedContainer = $item.find('.choice-expanded-editor');
        var $textarea = $controls.find('.choice-text-input');
        var $expandBtn = $controls.find('.expand-choice-btn');
        var $expandedTextarea = $expandedContainer.find('.choice-expanded-textarea');
        var index = $item.data('index');

        // Sync value back
        if ($expandedTextarea.length) {
            $textarea.val($expandedTextarea.val());
            this.updateFromUI();
        }

        // Clear expanded editor
        $expandedContainer.html('').hide();
        delete this.expandedEditors[index];

        // Show minimal textarea
        $textarea.show();
        $expandBtn.show();

        // Resize the textarea
        autoResizeTextarea($textarea[0]);
    }

    addChoice() {
        var newId = this.generateId();
        this.choices.push({ id: newId, text: '' });
        this.render();
        this.bindEvents();
        this.updateHiddenField();
    }

    removeChoice(choiceId) {
        this.choices = this.choices.filter(function(c) { return c.id !== choiceId; });
        this.correctAnswers = this.correctAnswers.filter(function(id) { return id !== choiceId; });
        this.render();
        this.bindEvents();
        this.updateHiddenField();
    }

    updateFromUI() {
        var newChoices = [];
        var newCorrect = [];

        $(this.container).find('.choice-item').each(function() {
            var $item = $(this);
            // Read ID from the input field (user can edit it)
            var id = $item.find('.choice-id').val() || $item.data('id');
            var isChecked = $item.find('.correct-checkbox').is(':checked');

            // Get text from the textarea (either minimal or expanded)
            var $expandedTextarea = $item.find('.choice-expanded-textarea');
            var $minimalTextarea = $item.find('.choice-text-input');
            var text = '';
            if ($expandedTextarea.length && $expandedTextarea.is(':visible')) {
                text = $expandedTextarea.val();
            } else {
                text = $minimalTextarea.val();
            }

            // Update data-id and checkbox value to match edited ID
            $item.data('id', id);
            $item.find('.correct-checkbox').val(id);

            newChoices.push({ id: String(id), text: text });
            if (isChecked) {
                newCorrect.push(String(id));
            }
        });

        this.choices = newChoices;
        this.correctAnswers = newCorrect;
        this.updateHiddenField();
    }

    updateHiddenField() {
        var choicesJson = JSON.stringify(this.choices);
        var correctJson;

        if (this.questionType === 'MA') {
            correctJson = JSON.stringify({ answers: this.correctAnswers });
        } else {
            correctJson = JSON.stringify({ answers: this.correctAnswers[0] || '' });
        }

        $(this.inputField + '_choices').val(choicesJson);
        $(this.inputField + '_correct').val(correctJson);
    }

    isCorrect(choiceId) {
        return this.correctAnswers.indexOf(String(choiceId)) !== -1;
    }

    generateId() {
        // Generate sequential letter IDs (A, B, C, ...)
        var existingIds = this.choices.map(function(c) { return c.id.toUpperCase(); });
        var letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

        for (var i = 0; i < letters.length; i++) {
            var letter = letters[i];
            if (existingIds.indexOf(letter) === -1) {
                return letter;
            }
        }

        // If all letters used, fallback to numbered format
        return 'A' + (this.choices.length + 1);
    }

    escapeHtml(text) {
        var div = document.createElement('div');
        div.textContent = text || '';
        return div.innerHTML;
    }

    destroy() {
        // Clean up expanded editors
        this.expandedEditors = {};
        $(this.container).find('.choice-expanded-editor').html('');
    }
}

// Navigation prevention
function preventNavigation(message) {
    message = message || 'You have unsaved answers. Are you sure you want to leave?';
    window.onbeforeunload = function(e) {
        e.preventDefault();
        e.returnValue = message;
        return message;
    };
}

function allowNavigation() {
    window.onbeforeunload = null;
}

// Auto-save functionality
function initAutoSave(config) {
    var saveTimeout = null;
    var saveQueue = {};

    function saveAnswer(questionId, answer) {
        saveQueue[questionId] = answer;

        clearTimeout(saveTimeout);
        saveTimeout = setTimeout(function() {
            var toSave = Object.assign({}, saveQueue);
            saveQueue = {};

            Object.keys(toSave).forEach(function(qId) {
                $('#save-status-' + qId)
                    .text(config.savingText || 'Saving...')
                    .removeClass('saved error')
                    .addClass('saving');

                $.ajax({
                    url: config.saveUrl,
                    method: 'POST',
                    contentType: 'application/json',
                    data: JSON.stringify({
                        question_id: qId,
                        answer: toSave[qId]
                    }),
                    headers: {
                        'X-CSRFToken': config.csrfToken
                    },
                    success: function(data) {
                        if (data.expired) {
                            alert(config.expiredText || 'Time expired. Your quiz will be submitted.');
                            window.location.href = config.resultUrl;
                            return;
                        }
                        $('#save-status-' + qId)
                            .text(config.savedText || 'Saved')
                            .removeClass('saving error')
                            .addClass('saved');
                    },
                    error: function() {
                        $('#save-status-' + qId)
                            .text(config.errorText || 'Error saving')
                            .removeClass('saving saved')
                            .addClass('error');
                    }
                });
            });
        }, 500);
    }

    return { saveAnswer: saveAnswer };
}

// Initialize quiz taking page
function initQuiz(config) {
    var timer = null;
    var navigator = null;
    var autoSaver = null;

    // Initialize timer if time limit exists
    if (config.timeLimit && config.timeLimit > 0) {
        timer = new QuizTimer(
            config.timeLimit,
            function() {
                $('#timer-display').text(config.timeUpText || 'Time is up!');
                allowNavigation();
                $('#submit-form').submit();
            },
            '#timer-display'
        );
        timer.start();
    }

    // Initialize auto-save
    autoSaver = initAutoSave({
        saveUrl: config.saveUrl,
        csrfToken: config.csrfToken,
        resultUrl: config.resultUrl,
        savingText: config.savingText,
        savedText: config.savedText,
        errorText: config.errorText,
        expiredText: config.expiredText
    });

    // Initialize navigator if using prev/next mode
    if (config.useNavigation && config.questions) {
        navigator = new QuizNavigator({
            questions: config.questions,
            currentIndex: 0,
            saveUrl: config.saveUrl,
            csrfToken: config.csrfToken
        });

        $('#prev-btn').click(function() { navigator.prev(); });
        $('#next-btn').click(function() { navigator.next(); });
    }

    // Bind input handlers for auto-save
    $('.question-radio').on('change', function() {
        var questionId = $(this).data('question');
        var answer = $(this).val();
        autoSaver.saveAnswer(questionId, answer);
    });

    $('.question-checkbox').on('change', function() {
        var questionId = $(this).data('question');
        var checked = [];
        $('input.question-checkbox[data-question="' + questionId + '"]:checked').each(function() {
            checked.push($(this).val());
        });
        autoSaver.saveAnswer(questionId, JSON.stringify(checked));
    });

    $('.question-text').on('input', function() {
        var questionId = $(this).data('question');
        var answer = $(this).val();
        autoSaver.saveAnswer(questionId, answer);
    });

    // Prevent navigation
    preventNavigation(config.leaveWarning);

    // Allow navigation on submit
    $('#submit-form').on('submit', function() {
        allowNavigation();
    });

    return {
        timer: timer,
        navigator: navigator,
        autoSaver: autoSaver
    };
}

// Auto-resize textarea based on content
function autoResizeTextarea(textarea) {
    if (!textarea) return;
    // Reset height to auto to get the correct scrollHeight
    textarea.style.height = 'auto';
    // Set minimum height (1 line)
    var minHeight = 32; // approximately 1 line with padding
    var newHeight = Math.max(textarea.scrollHeight, minHeight);
    textarea.style.height = newHeight + 'px';
}

// Initialize auto-resize for a textarea
function initAutoResizeTextarea($textarea) {
    if (!$textarea || !$textarea.length) return;

    var textarea = $textarea[0];

    // Initial resize
    autoResizeTextarea(textarea);

    // Resize on input
    $textarea.on('input', function() {
        autoResizeTextarea(this);
    });
}

// Initialize all auto-resize textareas in a container
function initAutoResizeInContainer($container) {
    $container.find('.auto-resize-textarea').each(function() {
        initAutoResizeTextarea($(this));
    });
}

// Export for use in templates
window.QuizTimer = QuizTimer;
window.QuizNavigator = QuizNavigator;
window.ChoiceEditor = ChoiceEditor;
window.initQuiz = initQuiz;
window.initAutoSave = initAutoSave;
window.preventNavigation = preventNavigation;
window.allowNavigation = allowNavigation;
window.autoResizeTextarea = autoResizeTextarea;
window.initAutoResizeTextarea = initAutoResizeTextarea;
window.initAutoResizeInContainer = initAutoResizeInContainer;
