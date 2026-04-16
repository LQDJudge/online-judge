/**
 * Problem Author Chatbot JavaScript
 * Handles chat UI interactions and API communication
 */

$(document).ready(function() {
  var config = window.CHATBOT_CONFIG || {};
  var $messages = $('#chatbot-messages');
  var $input = $('#chat-input');
  var $sendBtn = $('#send-btn');
  var $status = $('#chat-status');
  var $clearBtn = $('#clear-chat-btn');
  var $modelBtn = $('#model-btn');
  var $modelDropdown = $('#model-dropdown');
  var $modelName = $('#current-model-name');
  var isProcessing = false;
  var currentModel = config.currentModel || '';

  /**
   * Scroll chat to bottom
   */
  function scrollToBottom() {
    $messages.scrollTop($messages[0].scrollHeight);
  }

  /**
   * Check if user is near the bottom of the chat (within 100px)
   */
  function isNearBottom() {
    var el = $messages[0];
    return el.scrollHeight - el.scrollTop - el.clientHeight < 100;
  }

  /**
   * Scroll to bottom only if user hasn't scrolled up
   */
  function scrollToBottomIfNear() {
    if (isNearBottom()) {
      scrollToBottom();
    }
  }

  /**
   * Auto-resize textarea based on content
   */
  function autoResizeTextarea() {
    $input.css('height', 'auto');
    var scrollHeight = $input[0].scrollHeight;
    var maxHeight = 150;
    $input.css('height', Math.min(scrollHeight, maxHeight) + 'px');
  }

  /**
   * Escape HTML to prevent XSS
   */
  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  /**
   * Format content for display
   * @param content - HTML content (pre-rendered from server) or plain text
   * @param isHtml - whether content is already HTML
   */
  function formatContent(content, isHtml) {
    if (isHtml) {
      // Content is pre-rendered HTML from server
      return content;
    }
    // Plain text - escape and convert newlines
    return '<div class="md-typeset content-description"><p>' + escapeHtml(content).replace(/\n/g, '<br>') + '</p></div>';
  }

  /**
   * Render markdown on the client side (used during streaming)
   */
  function renderMarkdownClient(text) {
    if (typeof marked !== 'undefined') {
      try {
        var html = marked.parse(text);
        return '<div class="md-typeset content-description">' + html + '</div>';
      } catch (e) {
        // Fallback to plain text
      }
    }
    return formatContent(text, false);
  }

  /**
   * Post-process messages (render KaTeX math)
   * Uses the global renderKatex function from katex_config.js
   */
  function postProcessMessage($element) {
    if (typeof renderKatex === 'function') {
      renderKatex($element[0]);
    }
  }

  /**
   * Reindex data-index attributes on all messages after a deletion
   */
  function reindexMessages() {
    $messages.children('.message').each(function(i) {
      $(this).attr('data-index', i);
    });
  }

  /**
   * Add a message to the chat UI
   * @param role - 'user' or 'assistant'
   * @param content - message content
   * @param toolCalls - optional tool calls array
   * @param isHtml - whether content is pre-rendered HTML (default: true)
   * @returns the message element
   */
  function addMessage(role, content, toolCalls, isHtml) {
    if (typeof isHtml === 'undefined') isHtml = true;

    var index = $messages.children('.message').length;
    var $msg = $('<div class="message ' + role + '"></div>');
    $msg.attr('data-index', index);

    var $avatar = $('<div class="message-avatar"></div>');

    // Use user's avatar image or robot icon for assistant
    if (role === 'user' && config.userAvatar) {
      $avatar.html('<img src="' + config.userAvatar + '" alt="">');
    } else {
      $avatar.html('<i class="fa ' + (role === 'user' ? 'fa-user' : 'fa-robot') + '"></i>');
    }

    var $body = $('<div class="message-body"></div>');
    var $content = $('<div class="message-content"></div>').html(formatContent(content, isHtml));

    // Add delete button
    var $deleteBtn = $('<button class="message-delete-btn" title="Delete"><i class="fa fa-times"></i></button>');
    $body.append($deleteBtn);

    $body.append($content);

    // Add tool call badges if present
    if (toolCalls && toolCalls.length > 0) {
      var $tools = $('<div class="tool-calls"></div>');
      toolCalls.forEach(function(tool) {
        var title = tool.result_summary || '';
        var $badge = $('<span class="tool-badge" title="' + escapeHtml(title) + '"></span>');
        $badge.html('<i class="fa fa-wrench"></i> ' + escapeHtml(tool.tool));
        $tools.append($badge);
      });
      $body.append($tools);
    }

    $msg.append($avatar).append($body);

    // Remove welcome message if present
    $('#welcome-message').remove();

    $messages.append($msg);

    // Post-process messages (render KaTeX math)
    postProcessMessage($content);

    scrollToBottom();

    return $msg;
  }

  /**
   * Add a typing indicator
   */
  function addTypingIndicator() {
    var $indicator = $('<div class="message assistant typing-indicator" id="typing-indicator"></div>');
    $indicator.html(
      '<div class="message-avatar"><i class="fa fa-robot"></i></div>' +
      '<div class="message-body">' +
        '<div class="message-content">' +
          '<span class="typing-dots"><span>.</span><span>.</span><span>.</span></span>' +
        '</div>' +
      '</div>'
    );
    $messages.append($indicator);
    scrollToBottom();
  }

  /**
   * Remove typing indicator
   */
  function removeTypingIndicator() {
    $('#typing-indicator').remove();
  }

  /**
   * Set processing state
   */
  function setProcessing(processing, statusText) {
    isProcessing = processing;
    $sendBtn.prop('disabled', processing);
    $input.prop('disabled', processing);

    if (processing) {
      $status.text(statusText || config.translations.processing).show();
    } else {
      $status.hide();
    }
  }

  /**
   * Poll task status with streaming partial updates
   * @param taskId - the Celery task ID
   */
  function pollTaskStatus(taskId) {
    var $assistantMsg = null;
    var $contentEl = null;
    var lastPartialLen = 0;
    var lastUpdateTime = Date.now();
    var toolIndicatorShown = false;

    // Show typing indicator until first partial arrives
    addTypingIndicator();

    // Poll for streaming partial content (fast: every 500ms)
    var streamInterval = setInterval(function() {
      $.ajax({
        url: config.streamUrl + '?id=' + taskId,
        type: 'GET',
        success: function(data) {
          if (data.partial && data.partial.length > lastPartialLen) {
            // New content arrived — reset stale timer
            lastUpdateTime = Date.now();
            if (toolIndicatorShown) {
              toolIndicatorShown = false;
            }
            // First partial: replace typing indicator with message bubble
            if (!$assistantMsg) {
              removeTypingIndicator();
              $assistantMsg = addMessage('assistant', '', null, true);
              $assistantMsg.addClass('streaming');
              $contentEl = $assistantMsg.find('.message-content');
            }
            $contentEl.html(renderMarkdownClient(data.partial));
            lastPartialLen = data.partial.length;
            scrollToBottomIfNear();
          } else if ($assistantMsg && !toolIndicatorShown && Date.now() - lastUpdateTime > 3000) {
            // Stream stalled for 3s with existing text — AI is likely executing tools
            toolIndicatorShown = true;
            if (!$contentEl.find('.tool-working-indicator').length) {
              $contentEl.append(
                '<div class="tool-working-indicator">' +
                  '<i class="fa fa-cog fa-spin"></i> ' +
                  '<span>Đang xử lý...</span>' +
                '</div>'
              );
              scrollToBottomIfNear();
            }
          }
        }
      });
    }, 500);

    // Poll for task completion (every 2s, tolerates transient errors)
    var pollErrorCount = 0;
    var maxPollErrors = 5;
    var taskInterval = setInterval(function() {
      $.ajax({
        url: config.taskStatusUrl + '?id=' + taskId,
        type: 'GET',
        timeout: 10000,
        success: function(data) {
          pollErrorCount = 0; // Reset on success
          if (data.code === 'SUCCESS') {
            clearInterval(taskInterval);
            clearInterval(streamInterval);
            setProcessing(false);

            if (data.success) {
              if ($assistantMsg) {
                // Replace partial with final rendered HTML
                $assistantMsg.removeClass('streaming');
                $contentEl.html(formatContent(data.content || 'No response', true));
                postProcessMessage($contentEl);
              } else {
                // No partials were received; add message directly
                removeTypingIndicator();
                addMessage('assistant', data.content || 'No response', data.tool_calls, true);
              }
            } else {
              removeTypingIndicator();
              if ($assistantMsg) {
                $assistantMsg.removeClass('streaming');
                $contentEl.html(formatContent(
                  config.translations.error + ': ' + (data.error || 'Unknown error'), false
                ));
              } else {
                addMessage('assistant', config.translations.error + ': ' + (data.error || 'Unknown error'), null, false);
              }
            }
            scrollToBottom();
          } else if (data.code === 'FAILURE') {
            clearInterval(taskInterval);
            clearInterval(streamInterval);
            setProcessing(false);
            removeTypingIndicator();
            if ($assistantMsg) {
              $assistantMsg.removeClass('streaming');
              $contentEl.html(formatContent(
                config.translations.error + ': ' + (data.error || 'Task failed'), false
              ));
            } else {
              addMessage('assistant', config.translations.error + ': ' + (data.error || 'Task failed'), null, false);
            }
          }
          // Continue polling for PROGRESS/WORKING states
        },
        error: function() {
          pollErrorCount++;
          if (pollErrorCount >= maxPollErrors) {
            clearInterval(taskInterval);
            clearInterval(streamInterval);
            setProcessing(false);
            removeTypingIndicator();
            if ($assistantMsg) {
              $assistantMsg.removeClass('streaming');
              $contentEl.html(formatContent(config.translations.networkError, false));
            } else {
              addMessage('assistant', config.translations.networkError, null, false);
            }
          }
          // Otherwise keep polling — transient error
        }
      });
    }, 2000); // Poll every 2 seconds
  }

  /**
   * Send a message
   */
  function sendMessage() {
    var message = $input.val().trim();
    if (!message || isProcessing) return;

    // Show placeholder user message while sending
    var $userMsg = addMessage('user', message, null, false);
    $input.val('');
    autoResizeTextarea();

    setProcessing(true, config.translations.sending);

    $.ajax({
      url: config.sendUrl,
      type: 'POST',
      data: {
        message: message,
        csrfmiddlewaretoken: config.csrfToken
      },
      success: function(data) {
        if (data.success && data.task_id) {
          // Update user message with rendered markdown from response
          if (data.user_content) {
            var $userContent = $userMsg.find('.message-content');
            $userContent.html(data.user_content);
            postProcessMessage($userContent);
          }
          // Task dispatched, start polling
          $status.text(config.translations.processing);
          pollTaskStatus(data.task_id);
        } else {
          setProcessing(false);
          addMessage('assistant', config.translations.error + ': ' + (data.error || 'Failed to send message'));
        }
      },
      error: function() {
        setProcessing(false);
        addMessage('assistant', config.translations.networkError);
      }
    });
  }

  /**
   * Delete a message
   */
  function deleteMessage($msg) {
    if (isProcessing) return;
    if (!confirm(config.translations.deleteConfirm)) return;

    var index = parseInt($msg.attr('data-index'));

    $.ajax({
      url: config.deleteUrl,
      type: 'POST',
      data: {
        message_index: index,
        csrfmiddlewaretoken: config.csrfToken
      },
      success: function(data) {
        if (data.success) {
          var isUser = $msg.hasClass('user');
          if (isUser) {
            // Also remove the next assistant message (pair deletion)
            var $next = $msg.next('.message.assistant');
            if ($next.length) $next.remove();
          }
          $msg.remove();
          reindexMessages();

          // Show welcome message if no messages left
          if ($messages.children('.message').length === 0) {
            window.location.reload();
          }
        }
      },
      error: function() {
        alert(config.translations.networkError);
      }
    });
  }

  /**
   * Clear conversation history
   */
  function clearHistory() {
    if (!confirm(config.translations.clearConfirm)) return;

    $.ajax({
      url: config.clearUrl,
      type: 'POST',
      data: {
        csrfmiddlewaretoken: config.csrfToken
      },
      success: function(data) {
        if (data.success) {
          // Reload page to show empty state
          window.location.reload();
        }
      },
      error: function() {
        alert(config.translations.networkError);
      }
    });
  }

  /**
   * Get model display name by ID
   */
  function getModelName(modelId) {
    var models = config.supportedModels || [];
    for (var i = 0; i < models.length; i++) {
      if (models[i].id === modelId) {
        return models[i].name;
      }
    }
    return modelId;
  }

  /**
   * Update model display
   */
  function updateModelDisplay() {
    $modelName.text(getModelName(currentModel));
    // Update active state in dropdown
    $modelDropdown.find('.model-option').removeClass('active');
    $modelDropdown.find('.model-option[data-model="' + currentModel + '"]').addClass('active');
  }

  /**
   * Toggle model dropdown
   */
  function toggleModelDropdown() {
    $modelDropdown.toggleClass('show');
  }

  /**
   * Close model dropdown
   */
  function closeModelDropdown() {
    $modelDropdown.removeClass('show');
  }

  /**
   * Switch to a different model
   */
  function switchModel(modelId) {
    if (modelId === currentModel || isProcessing) return;

    $.ajax({
      url: config.modelUrl,
      type: 'POST',
      data: {
        model: modelId,
        csrfmiddlewaretoken: config.csrfToken
      },
      success: function(data) {
        if (data.success) {
          currentModel = data.model;
          updateModelDisplay();
          closeModelDropdown();
        } else {
          alert(data.error || 'Failed to switch model');
        }
      },
      error: function() {
        alert(config.translations.networkError);
      }
    });
  }

  // Event handlers
  $sendBtn.click(sendMessage);

  // Send on Enter, newline on Shift+Enter
  $input.keydown(function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Auto-resize textarea on input
  $input.on('input', autoResizeTextarea);

  // Clear history button
  $clearBtn.click(clearHistory);

  // Delete message (delegated)
  $messages.on('click', '.message-delete-btn', function(e) {
    e.stopPropagation();
    deleteMessage($(this).closest('.message'));
  });

  // Model selector button
  $modelBtn.click(function(e) {
    e.stopPropagation();
    toggleModelDropdown();
  });

  // Model option click
  $modelDropdown.on('click', '.model-option', function(e) {
    e.stopPropagation();
    var modelId = $(this).data('model');
    switchModel(modelId);
  });

  // Close dropdown when clicking outside
  $(document).click(function() {
    closeModelDropdown();
  });

  // Prevent dropdown from closing when clicking inside
  $modelDropdown.click(function(e) {
    e.stopPropagation();
  });

  // Initialize model display
  updateModelDisplay();

  // Initial scroll to bottom
  scrollToBottom();

  // Focus input on page load
  $input.focus();
});
