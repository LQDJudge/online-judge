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

// Choice Editor for question creation/editing
class ChoiceEditor {
    constructor(config) {
        this.container = config.container;
        this.inputField = config.inputField; // Hidden input for JSON
        this.questionType = config.questionType;
        this.choices = config.choices || [];
        this.correctAnswers = config.correctAnswers || [];

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
        html += '<i class="fa fa-plus"></i> Add Choice</button>';
        html += '</div>';

        $(this.container).html(html);
    }

    renderChoice(choice, index) {
        var isCorrect = this.isCorrect(choice.id);
        var inputType = (this.questionType === 'MA') ? 'checkbox' : 'radio';

        var html = '<div class="choice-item" data-id="' + choice.id + '" data-index="' + index + '">';
        html += '<span class="drag-handle"><i class="fa fa-bars"></i></span>';
        html += '<input type="' + inputType + '" name="correct_choice" value="' + choice.id + '"';
        if (isCorrect) html += ' checked';
        html += ' class="correct-checkbox">';
        html += '<input type="text" class="choice-id" value="' + this.escapeHtml(choice.id) + '" style="width: 50px; text-align: center; margin-right: 5px;" title="Choice ID (e.g., A, B, C)">';
        html += '<input type="text" class="choice-text" value="' + this.escapeHtml(choice.text) + '" placeholder="Choice text...">';
        html += '<button type="button" class="btn btn-sm btn-danger remove-choice-btn"><i class="fa fa-times"></i></button>';
        html += '</div>';

        return html;
    }

    bindEvents() {
        var self = this;
        var $container = $(this.container);

        // Unbind previous events to prevent duplicates
        $container.off('click', '.add-choice-btn');
        $container.off('click', '.remove-choice-btn');
        $container.off('input', '.choice-text');
        $container.off('input', '.choice-id');
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

        // Update on text change
        $container.on('input', '.choice-text', function() {
            self.updateFromUI();
        });

        // Update on ID change
        $container.on('input', '.choice-id', function() {
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
        var self = this;
        var newChoices = [];
        var newCorrect = [];

        $(this.container).find('.choice-item').each(function() {
            // Read ID from the input field (user can edit it)
            var id = $(this).find('.choice-id').val() || $(this).data('id');
            var text = $(this).find('.choice-text').val();
            var isChecked = $(this).find('.correct-checkbox').is(':checked');

            // Update data-id and checkbox value to match edited ID
            $(this).data('id', id);
            $(this).find('.correct-checkbox').val(id);

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

// Export for use in templates
window.QuizTimer = QuizTimer;
window.QuizNavigator = QuizNavigator;
window.ChoiceEditor = ChoiceEditor;
window.initQuiz = initQuiz;
window.initAutoSave = initAutoSave;
window.preventNavigation = preventNavigation;
window.allowNavigation = allowNavigation;
