// ============================================
// Chat Application - Modular JavaScript
// ============================================
// Requires: jQuery, Popper.js, Select2, moment.js
// Config: window.ChatConfig must be set before loading

(function($) {
  'use strict';

  // ============================================
  // State Management
  // ============================================
  var ChatState = {
    roomId: '',
    otherUserId: '',
    isLocked: false,
    lockClickSpace: false,
    unreadCount: 0,
    hasNext: false,
    roomVisible: true,
    pushedMessages: new Set(),
    drafts: {},
    messageLoadToken: 0,
    chatInfoToken: 0,

    init: function() {
      this.roomId = ChatConfig.room.id;
      this.otherUserId = ChatConfig.room.otherUserId;
    }
  };

  // ============================================
  // Cached DOM Elements
  // ============================================
  var ChatElements = {
    chatBox: null,
    chatLog: null,
    chatInput: null,
    chatInfo: null,
    chatOnlineList: null,
    chatInputContainer: null,
    chatSubmitButton: null,
    emojiButton: null,
    loader: null,

    init: function() {
      this.chatBox = $('#chat-box');
      this.chatLog = $('#chat-log');
      this.chatInput = $('#chat-input');
      this.chatInfo = $('#chat-info');
      this.chatOnlineList = $('#chat-online-list');
      this.chatInputContainer = $('#chat-input-container');
      this.chatSubmitButton = $('#chat-submit-button');
      this.emojiButton = $('#emoji-button');
      this.loader = $('#loader');
    }
  };

  // ============================================
  // Utility Functions
  // ============================================
  var ChatUtils = {
    debounce: function(func, wait) {
      var timeout;
      return function() {
        var context = this;
        var args = arguments;
        clearTimeout(timeout);
        timeout = setTimeout(function() {
          func.apply(context, args);
        }, wait);
      };
    },

    isMobile: function() {
      return window.matchMedia('only screen and (max-width: 799px)').matches;
    },

    findInSelfAndDescendants: function($container, selector) {
      return $container.filter(selector).add($container.find(selector));
    },

    postProcessMessages: function($container, regroupMode) {
      if (!$container || !$container.length) {
        $container = ChatElements.chatLog;
      }
      var $elements = $container.filter(function() {
        return this.nodeType === 1;
      });
      if (!$elements.length) return;

      register_time(this.findInSelfAndDescendants($elements, '.time-with-rel'));
      if (typeof renderKatex === 'function') {
        $elements.each(function() {
          renderKatex(this);
        });
      }

      if (regroupMode === 'none') {
        return;
      }

      if (regroupMode === 'incremental') {
        this.mergeConsecutiveMessagesFor(
          this.findInSelfAndDescendants($elements, '.message')
        );
        return;
      }

      this.mergeConsecutiveMessages();
    },

    mergeConsecutiveMessages: function() {
      var lastAuthorId = null;
      var lastTime = null;
      var GROUP_THRESHOLD = 300; // 5 minutes in seconds

      $('#chat-log .message').each(function() {
        var $message = $(this);
        var authorId = $message.attr('data-author');
        var time = parseInt($message.attr('data-time'), 10);

        // Clear existing grouping classes first
        $message.removeClass('grouped group-start');

        if (authorId === lastAuthorId && time - lastTime <= GROUP_THRESHOLD) {
          // Same author and within time threshold - group together
          $message.addClass('grouped');
        } else {
          // Different author or too much time passed - start new group
          $message.addClass('group-start');
        }

        lastAuthorId = authorId;
        lastTime = time;
      });
    },

    mergeConsecutiveMessagesFor: function($messages) {
      var self = this;
      $messages.each(function() {
        var $message = $(this);
        var $previous = $message.prevAll('.message').first();
        self.updateMessageGrouping($message, $previous);
      });
    },

    updateMessageGrouping: function($message, $previous) {
      var GROUP_THRESHOLD = 300; // 5 minutes in seconds
      var authorId = $message.attr('data-author');
      var time = parseInt($message.attr('data-time'), 10);
      var isGrouped = false;

      if ($previous && $previous.length) {
        var previousAuthorId = $previous.attr('data-author');
        var previousTime = parseInt($previous.attr('data-time'), 10);
        isGrouped = authorId === previousAuthorId && time - previousTime <= GROUP_THRESHOLD;
      }

      $message
        .toggleClass('grouped', isGrouped)
        .toggleClass('group-start', !isGrouped);
    },

    insertAtCursor: function(element, text) {
      var start = element.selectionStart;
      var end = element.selectionEnd;
      var value = element.value;
      element.value = value.slice(0, start) + text + value.slice(end);
      element.selectionStart = element.selectionEnd = start + text.length;
    },

    resizeEmoji: function(element) {
      var html = element.html();
      html = html.replace(/(\p{Extended_Pictographic})/ug, '<span class="big-emoji">$1</span>');
      element.html(html);
    }
  };

  // ============================================
  // API Calls
  // ============================================
  var ChatAPI = {
    loadMessages: function(lastId, onlyMessages) {
      if (onlyMessages === undefined) onlyMessages = true;
      var params = { only_messages: onlyMessages };
      if (lastId) params.last_id = lastId;
      return $.get(ChatConfig.urls.chat + ChatState.roomId, params);
    },

    postMessage: function(body, roomId, tmpId) {
      return $.post(ChatConfig.urls.postMessage, {
        body: body,
        room: roomId,
        tmp_id: tmpId
      });
    },

    deleteMessage: function(messageId) {
      return $.ajax({
        url: ChatConfig.urls.deleteMessage,
        type: 'post',
        data: { message: messageId },
        dataType: 'json'
      });
    },

    reactMessage: function(messageId, reaction) {
      return $.ajax({
        url: ChatConfig.urls.react,
        type: 'post',
        data: { message: messageId, reaction: reaction },
        dataType: 'json'
      });
    },

    muteMessage: function(messageId, muteType, reason) {
      return $.ajax({
        url: ChatConfig.urls.muteMessage,
        type: 'post',
        data: {
          message: messageId,
          mute_type: muteType,
          reason: reason
        },
        dataType: 'json'
      });
    },

    getMessage: function(messageId) {
      return $.get(ChatConfig.urls.messageAjax, { message: messageId });
    },

    getOnlineStatus: function() {
      return $.get(ChatConfig.urls.onlineStatus);
    },

    getUserOnlineStatus: function(userId) {
      return $.get(ChatConfig.urls.userOnlineStatus, { user: userId });
    },

    getOrCreateRoom: function(encryptedUser) {
      return $.get(ChatConfig.urls.getOrCreateRoom, { other: encryptedUser });
    },

    updateLastSeen: function(roomId) {
      return $.post(ChatConfig.urls.updateLastSeen, { room: roomId });
    }
  };

  // ============================================
  // UI Updates
  // ============================================
  var ChatUI = {
    newMessageCount: 0,

    // Rebuild a message's reaction pill + picker-active state from a summary
    // ({counts, total, my_reaction}). Used by both the POST response and live events.
    renderReactions: function(messageId, summary) {
      var $container = $('#message-reactions-' + messageId);
      if (!$container.length) return;

      var myReaction = summary.my_reaction || '';
      var counts = summary.counts || {};
      $container.attr('data-my-reaction', myReaction);

      if (summary.total && summary.total > 0) {
        var emojis = '';
        (ChatConfig.reactions || []).forEach(function(pair) {
          if (counts[pair[0]]) {
            emojis += '<span class="reaction-emoji">' + pair[1] + '</span>';
          }
        });
        var mineClass = myReaction ? ' reaction-pill-mine' : '';
        var html = '<button type="button" class="reaction-pill' + mineClass +
          '" data-id="' + messageId + '" title="' + (ChatConfig.i18n.react || '') + '">' +
          '<span class="reaction-pill-emojis">' + emojis + '</span>' +
          '<span class="reaction-pill-count">' + summary.total + '</span></button>';
        $container.removeClass('is-empty').html(html);
      } else {
        $container.addClass('is-empty').empty();
      }

      var $picker = $('#message-' + messageId).find('.reaction-picker');
      $picker.find('.reaction-option').each(function() {
        $(this).toggleClass(
          'reaction-option-active',
          String($(this).data('reaction')) === myReaction
        );
      });
    },

    scrollToBottom: function() {
      ChatElements.chatBox.scrollTop(ChatElements.chatBox[0].scrollHeight);
      this.hideNewMessagesBubble();
    },

    isNearBottom: function(threshold) {
      if (!threshold) threshold = 150;
      var box = ChatElements.chatBox[0];
      return box.scrollHeight - box.scrollTop - box.clientHeight < threshold;
    },

    getScrollTopOfBottom: function() {
      return ChatElements.chatBox[0].scrollHeight - ChatElements.chatBox.innerHeight();
    },

    showNewMessagesBubble: function(count) {
      this.newMessageCount = count;
      var $bubble = $('#new-messages-bubble');
      if (!$bubble.length) {
        $bubble = $('<div id="new-messages-bubble"></div>');
        ChatElements.chatBox.append($bubble);
        $bubble.on('click', function() {
          ChatUI.scrollToBottom();
        });
      }
      var text = count === 1
        ? '1 ' + ChatConfig.i18n.newMessage.toLowerCase()
        : count + ' ' + ChatConfig.i18n.newMessages.toLowerCase();
      $bubble.text('\u2193 ' + text).show();
    },

    hideNewMessagesBubble: function() {
      this.newMessageCount = 0;
      $('#new-messages-bubble').hide();
    },

    showLoader: function() {
      ChatElements.loader.show();
    },

    hideLoader: function() {
      ChatElements.loader.hide();
    },

    showRightPanel: function() {
      // The room's message panel is now on screen (true on desktop as well,
      // where both panels are always visible side by side).
      ChatState.roomVisible = true;
      if (ChatUtils.isMobile()) {
        // Toggle via classes (see .mobile-visible / .mobile-hidden in SCSS)
        // rather than inline styles, so a mobile->desktop resize can't strand
        // a panel with a leftover inline display value.
        $('.chat-area').addClass('mobile-visible');
        $('.chat-sidebar').addClass('mobile-hidden');
        // Returning to the room means we're viewing it again: mark it seen.
        if (ChatState.roomId) {
          ChatAPI.updateLastSeen(ChatState.roomId);
          ChatUI.updateUnreadBadge(ChatState.otherUserId || null, true);
        }
        // Scroll to bottom after display change
        var self = this;
        setTimeout(function() {
          self.scrollToBottom();
        }, 0);
      }
    },

    hideRightPanel: function() {
      // Back button: the message panel is no longer visible. roomId stays set
      // so returning is cheap, but incoming messages must now be treated as
      // background (unread badge) instead of being appended + marked seen.
      ChatState.roomVisible = false;
      if (ChatUtils.isMobile()) {
        $('.chat-area').removeClass('mobile-visible');
        $('.chat-sidebar').removeClass('mobile-hidden');
        // Scroll sidebar to top
        $('#chat-online-content').scrollTop(0);
      }
    },

    highlightSelectedRoom: function() {
      $('.status-row').removeClass('selected');
      if (ChatState.otherUserId) {
        $('#click_space_' + ChatState.otherUserId).addClass('selected');
      } else {
        $('#lobby_row').addClass('selected');
      }
    },

    renderHeaderFromStatusRow: function($row) {
      if (!$row || !$row.length) return;

      var isLobby = $row.attr('id') === 'lobby_row';
      var $avatar = $row.find('.status-pic').first().clone();
      $avatar
        .removeClass('status-pic')
        .addClass('info-pic');

      var $status = $row.find('.status-circle').first().clone();
      $status
        .removeClass('status-circle')
        .addClass('info-circle');

      var nameHtml = isLobby
        ? $row.find('.status-username').html()
        : $row.find('.username').html();

      var $header = $('<div>');
      $header.append(
        $('<div class="back-button"><i class="fa fa-arrow-left"></i></div>')
      );

      var $avatarWrapper = $('<div class="status-container chat-header-avatar">').append($avatar);
      if (!isLobby && $status.length) {
        $avatarWrapper.append($status);
      }
      $header.append($avatarWrapper);
      $header.append($('<span class="info-name username">').html(nameHtml));
      $header.append($('<span class="spacer">'));

      ChatElements.chatInfo.html($header.contents());
    },

    addMessage: function(html, forceScroll) {
      var wasNearBottom = this.isNearBottom();
      var $nodes = $(html);
      ChatElements.chatLog.append($nodes);
      ChatUtils.postProcessMessages($nodes, 'incremental');
      if (forceScroll || wasNearBottom) {
        this.scrollToBottom();
      } else {
        this.newMessageCount++;
        this.showNewMessagesBubble(this.newMessageCount);
      }
    },

    prependMessages: function(html) {
      var chatBox = ChatElements.chatBox[0];
      var scrollHeightBefore = chatBox.scrollHeight;
      var scrollTopBefore = chatBox.scrollTop;
      var $nextMessage = ChatElements.chatLog.children('.message').first();
      var $nodes = $(html);
      var $messages = $nodes.filter('.message').add($nodes.find('.message'));
      ChatElements.chatLog.prepend($nodes);
      ChatUtils.postProcessMessages($nodes, 'none');
      ChatUtils.mergeConsecutiveMessagesFor($messages.add($nextMessage));
      ChatElements.chatBox.scrollTop(
        scrollTopBefore + chatBox.scrollHeight - scrollHeightBefore
      );
    },

    clearMessages: function() {
      ChatElements.chatLog.html('');
    },

    updateUnreadBadge: function(userId, hide) {
      var badge = userId ? $('#unread-count-' + userId) : $('#unread-count-lobby');
      if (hide) {
        badge.hide();
      }
    },

    setUnreadBadge: function(userId, count) {
      var badgeId = userId ? '#unread-count-' + userId : '#unread-count-lobby';
      var $badge = $(badgeId);

      if (count > 0) {
        if ($badge.length) {
          $badge.text(count).show();
        } else {
          // Create badge if it doesn't exist
          var $row = userId ? $('#click_space_' + userId) : $('#lobby_row');
          if ($row.length) {
            var $newBadge = $('<span class="unread-count" id="unread-count-' + (userId || 'lobby') + '">' + count + '</span>');
            // Insert before setting-wrapper or at the end
            var $settingWrapper = $row.find('.setting-wrapper');
            if ($settingWrapper.length) {
              $settingWrapper.before($newBadge);
            } else {
              $row.append($newBadge);
            }
          }
        }
      } else {
        $badge.hide();
      }
    },

    moveConversationToTop: function(userId) {
      var $row = $('#click_space_' + userId);
      if (!$row.length) {
        // User not in list - refresh sidebar to add them
        ChatEvents.refreshStatus();
        return;
      }

      // Find the parent status-list
      var $list = $row.closest('.status-list');
      if ($list.length) {
        // Move to top of the list
        $list.prepend($row);
      }
    },

    setLastMessagePreview: function(roomId, text) {
      var $preview = $('#last_msg-' + roomId);
      if ($preview.length) {
        $preview.text(text);
      }
    },

    setUserOnline: function(userId) {
      // Update sidebar status circle
      var $sidebarCircle = $('#click_space_' + userId + ' .status-circle');
      $sidebarCircle.removeClass('offline').addClass('online');

      // Update chat header status circle if viewing this user
      if (String(userId) === ChatState.otherUserId) {
        var $headerCircle = $('.chat-header .info-circle, #chat-info .info-circle');
        $headerCircle.removeClass('offline').addClass('online');
      }
    },

    setMutedState: function(isMuted) {
      ChatConfig.user.isMuted = isMuted;

      ChatElements.chatInput
        .prop('disabled', isMuted)
        .attr('placeholder', isMuted ? ChatConfig.i18n.chatMuted : ChatConfig.i18n.enterMessage);
      ChatElements.chatInputContainer.toggleClass('is-muted', isMuted);
      ChatElements.chatSubmitButton.toggleClass('is-disabled', isMuted);
      ChatElements.emojiButton
        .toggleClass('is-disabled', isMuted)
        .attr('title', isMuted ? ChatConfig.i18n.chatMuted : ChatConfig.i18n.emoji);

      if (isMuted) {
        ChatElements.chatInput.attr('aria-disabled', 'true');
        ChatElements.chatSubmitButton
          .attr('aria-disabled', 'true')
          .attr('tabindex', '-1');
        ChatElements.emojiButton
          .attr('aria-disabled', 'true')
          .attr('tabindex', '-1');
        $('.emoji-tooltip').removeClass('shown');
      } else {
        ChatElements.chatInput.removeAttr('aria-disabled');
        ChatElements.chatSubmitButton
          .removeAttr('aria-disabled')
          .removeAttr('tabindex');
        ChatElements.emojiButton
          .removeAttr('aria-disabled')
          .removeAttr('tabindex');
      }
    },

    applyMutedState: function() {
      this.setMutedState(ChatConfig.user.isMuted);
    }
  };

  // ============================================
  // Draft Handling
  // ============================================
  var ChatDrafts = {
    getCurrentKey: function() {
      return ChatState.roomId ? 'room:' + ChatState.roomId : 'lobby';
    },

    setInputValue: function(value) {
      ChatElements.chatInput.val(value);
      ChatElements.chatInput.trigger('input');
    },

    saveCurrent: function() {
      if (!ChatElements.chatInput || !ChatElements.chatInput.length) return;

      var key = this.getCurrentKey();
      var value = ChatElements.chatInput.val();
      if (value) {
        ChatState.drafts[key] = value;
      } else {
        delete ChatState.drafts[key];
      }
    },

    restoreCurrent: function() {
      var value = ChatState.drafts[this.getCurrentKey()] || '';
      this.setInputValue(value);
    },

    clearCurrent: function() {
      delete ChatState.drafts[this.getCurrentKey()];
      this.setInputValue('');
    }
  };

  // ============================================
  // Message Handling
  // ============================================
  var ChatMessages = {
    // Apply a reaction event pushed from another user. The broadcast carries the
    // group counts (authoritative) but not this viewer's own reaction, so we keep
    // whatever my_reaction is already reflected in the DOM. If the message isn't in
    // the current view, there's nothing to do -- rooms reload fresh on open.
    applyReaction: function(message) {
      var $container = $('#message-reactions-' + message.message);
      if (!$container.length) return;
      ChatUI.renderReactions(message.message, {
        counts: message.counts || {},
        total: message.total || 0,
        my_reaction: $container.attr('data-my-reaction') || null
      });
    },

    addFromTemplate: function(body, tmpId) {
      if (ChatState.roomId) {
        $('#last_msg-' + ChatState.roomId).html(body);
      }
      var html = ChatConfig.messageTemplate;
      html = html.replace(/\$body/g, body).replace(/\$id/g, tmpId);
      var $html = $(html);
      $html.find('.time-with-rel').attr('data-iso', (new Date()).toISOString());
      ChatUI.addMessage($html[0].outerHTML, true);
    },

    submit: function() {
      if (ChatConfig.user.isMuted || !ChatConfig.room.lastMsgId) return;

      var body = ChatElements.chatInput.val().trim();
      if (!body) return;

      var tmpId = Date.now();

      ChatDrafts.clearCurrent();
      $('#chat-input-container').height('auto');

      this.addFromTemplate(body, tmpId);

      ChatAPI.postMessage(body, ChatState.roomId, tmpId)
        .done(function() {
          $('#empty_msg').hide();
          ChatElements.chatInput.focus();
        })
        .fail(function() {
          var $body = $('#message-text-' + tmpId);
          $body.css('text-decoration', 'line-through');
          $body.css('background', 'red');
        });
    },

    loadNextPage: function(lastId, refreshHtml) {
      var requestRoomId = ChatState.roomId;
      var loadToken = refreshHtml ? ++ChatState.messageLoadToken : ChatState.messageLoadToken;
      if (refreshHtml) {
        ChatState.isLocked = true;
        ChatElements.chatLog.html('');
        ChatUI.showLoader();
      }

      ChatAPI.loadMessages(lastId)
        .done(function(data) {
          if (requestRoomId !== ChatState.roomId || loadToken !== ChatState.messageLoadToken) {
            return;
          }

          $('.has_next').remove();
          ChatUI.hideLoader();

          if (refreshHtml) {
            ChatElements.chatLog.append(data);
            ChatUtils.postProcessMessages(ChatElements.chatLog);
            ChatUI.scrollToBottom();
            // Re-pin to bottom as images load, but only if the user hasn't
            // scrolled up in the meantime (otherwise we'd yank them back down).
            ChatElements.chatLog.find('img').on('load', function() {
              if (ChatUI.isNearBottom()) {
                ChatUI.scrollToBottom();
              }
            });
          } else {
            ChatUI.prependMessages(data);
          }

          ChatState.isLocked = false;
          ChatState.hasNext = parseInt($('.has_next').attr('value')) || 0;
        })
        .fail(function() {
          if (loadToken !== ChatState.messageLoadToken) return;

          console.log('Failed to load messages');
          ChatUI.hideLoader();
          ChatState.isLocked = false;
        });
    },

    addNewMessage: function(messageId, room, isSelfAuthor, wsMessage) {
      // Only treat the room as "live" when its panel is actually on screen.
      // On mobile the back button hides the chat area (roomVisible = false)
      // without changing roomId; messages arriving then must go through the
      // sidebar/unread path instead of being appended + silently marked seen.
      // The panel is always visible on desktop, so scope the flag to mobile —
      // this also avoids a stale roomVisible=false stranding desktop after a
      // mobile->desktop resize.
      var isCurrentRoom = room === ChatState.roomId &&
        (!ChatUtils.isMobile() || ChatState.roomVisible);

      // Sender is online since they just sent a message
      if (wsMessage && wsMessage.author_id) {
        ChatUI.setUserOnline(wsMessage.author_id);
      }

      // Update tab title for any new message from others when tab is hidden
      if (document.hidden && !isSelfAuthor) {
        ChatState.unreadCount++;
        document.title = '(' + ChatState.unreadCount + ') ' + ChatConfig.i18n.newMessages;
      }

      if (isCurrentRoom) {
        // Message is for the room we're viewing - display it live
        ChatAPI.getMessage(messageId)
          .done(function(data) {
            if (room === ChatState.roomId) {
              ChatUI.addMessage(data);
              if (!document.hidden) {
                ChatAPI.updateLastSeen(ChatState.roomId);
              }
              // Update sidebar: last message preview + move to top
              var updateUserId = wsMessage && wsMessage.other_user_id
                ? wsMessage.other_user_id
                : ChatState.otherUserId;
              if (wsMessage && wsMessage.room) {
                var $msg = $(data);
                var msgText = $msg.find('.message-text').text().trim();
                if (msgText.length > 50) {
                  msgText = msgText.substring(0, 50) + '...';
                }
                ChatUI.setLastMessagePreview(wsMessage.room, msgText || ChatConfig.i18n.newMessage);
              }
              if (updateUserId) {
                ChatUI.moveConversationToTop(updateUserId);
              }
            }
          })
          .fail(function() {
            console.log('Could not add new message');
          });
      } else {
        // Message is for a different room - update sidebar
        if (wsMessage && wsMessage.other_user_id) {
          if (wsMessage.unread_count !== undefined) {
            ChatUI.setUnreadBadge(wsMessage.other_user_id, wsMessage.unread_count);
          }
          if (wsMessage.room) {
            ChatUI.setLastMessagePreview(wsMessage.room, ChatConfig.i18n.newMessage);
          }
          ChatUI.moveConversationToTop(wsMessage.other_user_id);
        }
      }
    },

    checkNewMessage: function(messageId, tmpId, room) {
      if (room !== ChatState.roomId) {
        // Our own message confirmed for a room we're no longer viewing. There's
        // no live DOM to reconcile; it will render fresh next time we open it.
        return;
      }

      ChatAPI.getMessage(messageId)
        .done(function(data) {
          var $newMessage = $(data);
          // Disable animation for replacement
          $newMessage.css('animation', 'none');

          if ($('#message-' + tmpId).length) {
            $('#message-' + tmpId).replaceWith($newMessage);
          } else if ($('#message-block-' + tmpId).length) {
            var $bodyBlock = $newMessage.find('.message-block');
            $('#message-block-' + tmpId).replaceWith($bodyBlock);
          } else {
            ChatMessages.addNewMessage(messageId, room, true);
          }
          if (room) {
            var msgText = $newMessage.find('.message-text').text().trim();
            if (msgText.length > 50) {
              msgText = msgText.substring(0, 50) + '...';
            }
            ChatUI.setLastMessagePreview(room, msgText || ChatConfig.i18n.newMessage);
          }
          if (ChatState.otherUserId) {
            ChatUI.moveConversationToTop(ChatState.otherUserId);
          }
          ChatUI.updateUnreadBadge(ChatState.otherUserId, true);
          ChatUtils.postProcessMessages($newMessage, 'incremental');
        })
        .fail(function() {
          console.log('Failed to check message');
          var $body = $('#message-block-' + tmpId + ' p');
          $body.css('text-decoration', 'line-through');
          $body.css('text-decoration-color', 'red');
        });
    }
  };

  // ============================================
  // Event Handlers
  // ============================================
  var ChatEvents = {
    pendingMuteAction: null,

    init: function() {
      this.bindMessageInput();
      this.bindScrollLoad();
      this.bindMessageActionMenus();
      this.bindMessageActions();
      this.bindReactions();
      this.bindRoomSelection();
      this.bindEmojiPicker();
      this.bindVisibilityChange();
      this.bindSettingsMenu();
      this.initSelect2Search();
      this.bindInputAutoResize();
      this.startStatusPolling();
    },

    bindMessageInput: function() {
      ChatElements.chatInput.on('keydown', function(e) {
        if (e.keyCode === 13) {
          if (e.ctrlKey || e.shiftKey) {
            ChatUtils.insertAtCursor(this, '\n');
            $(this).trigger('input');
          } else {
            e.preventDefault();
            ChatMessages.submit();
          }
          return false;
        }
        return true;
      });

      $('#chat-submit-button').on('click', function() {
        ChatMessages.submit();
      });

      if (typeof register_copy_clipboard === 'function') {
        register_copy_clipboard(ChatElements.chatInput, function() {
          ChatElements.chatInput.trigger('input');
        });
      }
    },

    bindInputAutoResize: function() {
      ChatElements.chatInput.on('input', function() {
        this.style.height = 'auto';
        this.style.height = this.scrollHeight + 'px';
        ChatDrafts.saveCurrent();
      });
    },

    bindScrollLoad: function() {
      ChatElements.chatBox.on('scroll', function() {
        // Hide new messages bubble when scrolled to bottom
        if (ChatUI.isNearBottom()) {
          ChatUI.hideNewMessagesBubble();
        }

        // Trigger load when at top
        if (ChatElements.chatBox.scrollTop() === 0 &&
            !ChatState.isLocked &&
            ChatState.hasNext) {
          ChatState.isLocked = true;
          ChatUI.showLoader();
          var oldestMessageId = parseInt(
            ChatElements.chatLog.children('.message').first().attr('data-id'),
            10
          );
          if (oldestMessageId) {
            ChatMessages.loadNextPage(oldestMessageId);
          }
        }
      });
    },

    closeMessageActionMenus: function() {
      $('.message-actions.is-open')
        .removeClass('is-open')
        .find('.message-actions-toggle')
        .attr('aria-expanded', 'false');
      $('.message.has-open-actions').removeClass('has-open-actions');
    },

    bindMessageActionMenus: function() {
      var self = this;

      $(document).on('click', '.message-actions-toggle', function(e) {
        e.preventDefault();
        e.stopPropagation();

        var $actions = $(this).closest('.message-actions');
        $('.message-actions').not($actions)
          .removeClass('is-open')
          .find('.message-actions-toggle')
          .attr('aria-expanded', 'false');
        $('.message').not($actions.closest('.message')).removeClass('has-open-actions');

        var isOpen = !$actions.hasClass('is-open');
        $actions.toggleClass('is-open', isOpen);
        $actions.closest('.message').toggleClass('has-open-actions', isOpen);
        $(this).attr('aria-expanded', isOpen ? 'true' : 'false');
      });

      $(document).on('click', '.message-actions-menu', function(e) {
        e.stopPropagation();
      });

      $(document).on('click', function() {
        self.closeMessageActionMenus();
      });

      $(document).on('keydown', function(e) {
        if (e.keyCode === 27) {
          self.closeMessageActionMenus();
        }
      });
    },

    openMuteModal: function(messageId, canPermanent, reasonRequired) {
      this.pendingMuteAction = {
        messageId: messageId,
        canPermanent: canPermanent,
        reasonRequired: reasonRequired
      };

      $('#chat-mute-title').text(ChatConfig.i18n.muteTitle);
      $('#chat-mute-summary').text(ChatConfig.i18n.muteConfirm);
      $('#chat-mute-type-row').toggle(canPermanent);
      $('input[name="chat-mute-type"][value="temporary"]').prop('checked', true);
      $('#chat-mute-reason-label').text(
        reasonRequired
          ? ChatConfig.i18n.muteReason + ' *'
          : ChatConfig.i18n.muteReason
      );
      $('#chat-mute-reason')
        .val('')
        .attr('placeholder', ChatConfig.i18n.muteReasonPlaceholder);
      $('#chat-mute-error').text('');
      $('#chat-mute-modal')
        .addClass('is-open')
        .attr('aria-hidden', 'false');
      $('#chat-mute-reason').focus();
    },

    closeMuteModal: function() {
      this.pendingMuteAction = null;
      $('#chat-mute-modal')
        .removeClass('is-open')
        .attr('aria-hidden', 'true');
      $('#chat-mute-error').text('');
    },

    submitMuteModal: function() {
      if (!this.pendingMuteAction) return;

      var reason = $('#chat-mute-reason').val().trim();
      if (this.pendingMuteAction.reasonRequired && !reason) {
        $('#chat-mute-error').text(ChatConfig.i18n.muteReasonRequired);
        $('#chat-mute-reason').focus();
        return;
      }

      var muteAction = this.pendingMuteAction;
      var muteType = muteAction.canPermanent
        ? $('input[name="chat-mute-type"]:checked').val()
        : 'temporary';
      var $confirm = $('#chat-mute-confirm');
      $confirm.prop('disabled', true);

      ChatAPI.muteMessage(muteAction.messageId, muteType, reason)
        .done(function() {
          window.location.reload();
        })
        .fail(function(response) {
          var message = response.responseJSON && response.responseJSON.error
            ? response.responseJSON.error
            : ChatConfig.i18n.muteReasonRequired;
          $('#chat-mute-error').text(message);
        })
        .always(function() {
          $confirm.prop('disabled', false);
        });
    },

    bindMessageActions: function() {
      $(document).on('click', '.chat_remove', function() {
        var $this = $(this);
        var messageId = $this.attr('value');
        ChatEvents.closeMessageActionMenus();

        if (!window.confirm(ChatConfig.i18n.deleteConfirm)) {
          return;
        }

        ChatAPI.deleteMessage(messageId)
          .done(function() {
            var $message = $this.closest('li.message');
            if ($message.length) {
              $message.remove();
            } else {
              $this.closest('.message-block').remove();
            }
            // Recalculate message grouping to fix avatar/name visibility
            ChatUtils.mergeConsecutiveMessages();
          })
          .fail(function() {
            console.log('Failed to delete');
          });
      });

      if (ChatConfig.user.canModerateChat) {
        $(document).on('click', '.chat_mute', function() {
          ChatEvents.closeMessageActionMenus();
          var reasonRequired = String($(this).data('reason-required')) === '1';
          var canPermanent = String($(this).data('can-permanent')) === '1';
          ChatEvents.openMuteModal(
            $(this).attr('value'),
            canPermanent,
            reasonRequired
          );
        });
      }

      $('#chat-mute-confirm').on('click', function() {
        ChatEvents.submitMuteModal();
      });

      $('#chat-mute-cancel').on('click', function() {
        ChatEvents.closeMuteModal();
      });

      $('#chat-mute-modal').on('click', function(e) {
        if (e.target === this) {
          ChatEvents.closeMuteModal();
        }
      });
    },

    closeReactionPickers: function($except) {
      $('.message-react.is-open').not($except || []).removeClass('is-open')
        .find('.message-react-toggle').attr('aria-expanded', 'false');
    },

    bindReactions: function() {
      // Open/close the emoji picker from the smiley toggle.
      $(document).on('click', '.message-react-toggle', function(e) {
        e.stopPropagation();
        var $react = $(this).closest('.message-react');
        var wasOpen = $react.hasClass('is-open');
        ChatEvents.closeReactionPickers($react);
        $react.toggleClass('is-open', !wasOpen);
        $(this).attr('aria-expanded', wasOpen ? 'false' : 'true');
      });

      // Clicking the existing pill also opens the picker (to change/remove).
      $(document).on('click', '.reaction-pill', function(e) {
        e.stopPropagation();
        var $react = $('#message-' + $(this).data('id')).find('.message-react');
        ChatEvents.closeReactionPickers($react);
        var open = !$react.hasClass('is-open');
        $react.toggleClass('is-open', open);
        $react.find('.message-react-toggle')
          .attr('aria-expanded', open ? 'true' : 'false');
      });

      // Pick a reaction -> POST, then render the authoritative summary.
      $(document).on('click', '.reaction-option', function(e) {
        e.stopPropagation();
        var messageId = $(this).data('id');
        var reaction = $(this).data('reaction');
        ChatEvents.closeReactionPickers();
        ChatAPI.reactMessage(messageId, reaction)
          .done(function(summary) {
            ChatUI.renderReactions(messageId, summary);
          })
          .fail(function() {
            console.log('Could not react to message');
          });
      });

      // Any outside click closes open pickers.
      $(document).on('click', function() {
        ChatEvents.closeReactionPickers();
      });
    },

    bindRoomSelection: function() {
      $(document).on('click', '.click_space', function() {
        var $row = $(this);
        var clickedUserId = $row.data('user-id') || $row.attr('id').replace('click_space_', '');
        if (clickedUserId === ChatState.otherUserId) {
          // Re-tapping the room we already have open. If it was backgrounded
          // (mobile back button), reload to pull any messages that arrived
          // while hidden before revealing the panel.
          if (!ChatState.roomVisible) {
            ChatMessages.loadNextPage(null, true);
          }
          ChatUI.showRightPanel();
          return;
        }
        var roomId = String($row.data('room') || '');
        if (roomId) {
          ChatEvents.loadKnownRoom(roomId, String(clickedUserId), $row);
        } else {
          ChatEvents.loadRoom($row.attr('value'), $row);
        }
      });

      $(document).on('click', '#lobby_row', function() {
        if (ChatState.roomId) {
          ChatEvents.loadKnownRoom('', '', $(this));
        } else {
          // Already in the lobby; reload if it was backgrounded, then reveal.
          if (!ChatState.roomVisible) {
            ChatMessages.loadNextPage(null, true);
          }
          ChatUI.showRightPanel();
        }
      });

      // Back button for mobile
      $(document).on('click', '.back-button', function() {
        ChatUI.hideRightPanel();
      });
    },

    openCurrentRoom: function($row) {
      history.replaceState(null, '', ChatConfig.urls.chat + ChatState.roomId);
      ChatUI.hideNewMessagesBubble();
      ChatUI.highlightSelectedRoom();
      if ($row && $row.length) {
        ChatUI.renderHeaderFromStatusRow($row);
      }
      ChatMessages.loadNextPage(null, true);
      ChatAPI.updateLastSeen(ChatState.roomId);
      ChatEvents.refreshChatInfo(false);
      ChatUI.updateUnreadBadge(ChatState.otherUserId || null, true);
      ChatUI.showRightPanel();
      ChatUI.applyMutedState();
      // Don't auto-focus on mobile: it would pop the on-screen keyboard the
      // moment a conversation is opened, covering the input and newest messages.
      if (!ChatConfig.user.isMuted && !ChatUtils.isMobile()) {
        ChatElements.chatInput.focus();
      }
      ChatDrafts.restoreCurrent();
    },

    setCurrentRoom: function(roomId, otherUserId) {
      ChatState.roomId = roomId;
      ChatState.otherUserId = otherUserId;
      ChatConfig.room.id = roomId;
      ChatConfig.room.otherUserId = otherUserId;
      ChatElements.chatInput.attr('maxlength', roomId ? 5000 : 200);
    },

    loadKnownRoom: function(roomId, otherUserId, $row) {
      if (ChatState.lockClickSpace) return;
      ChatState.lockClickSpace = true;
      ChatDrafts.saveCurrent();

      this.setCurrentRoom(String(roomId || ''), String(otherUserId || ''));
      this.openCurrentRoom($row);
      ChatState.lockClickSpace = false;
    },

    loadRoom: function(encryptedUser, $row) {
      if (ChatState.lockClickSpace) return;
      ChatState.lockClickSpace = true;
      ChatDrafts.saveCurrent();

      if (encryptedUser) {
        ChatAPI.getOrCreateRoom(encryptedUser)
          .done(function(data) {
            ChatEvents.setCurrentRoom(String(data.room), String(data.other_user_id));
            if ($row && $row.length) {
              $row.attr('data-room', data.room).data('room', data.room);
            }
            ChatEvents.openCurrentRoom($row);
          })
          .fail(function() {
            console.log('Failed to get_or_create_room');
          })
          .always(function() {
            ChatState.lockClickSpace = false;
          });
      } else {
        this.setCurrentRoom('', '');
        this.openCurrentRoom($row);
        ChatState.lockClickSpace = false;
      }
    },

    bindEmojiPicker: function() {
      var button = document.querySelector('#emoji-button');
      var tooltip = document.querySelector('.emoji-tooltip');

      if (!button || !tooltip) return;

      var popper = Popper.createPopper(button, tooltip, {
        placement: ChatUtils.isMobile() ? 'auto-end' : 'left'
      });

      var toggleEmoji = function() {
        tooltip.classList.toggle('shown');
        popper.update();
      };

      $('#emoji-button').on('click', function(e) {
        e.preventDefault();
        e.stopPropagation();
        if (ChatConfig.user.isMuted) return;
        toggleEmoji();
      });

      $(document).on('click', function(e) {
        if (!tooltip.contains(e.target)) {
          tooltip.classList.remove('shown');
        }
      });

      $('emoji-picker').on('emoji-click', function(e) {
        if (ChatConfig.user.isMuted) return;
        var chatInput = ChatElements.chatInput.get(0);
        ChatUtils.insertAtCursor(chatInput, e.detail.unicode);
        chatInput.focus();
      });

      $(document).on('keydown', function(e) {
        if (e.keyCode === 27 && tooltip.classList.contains('shown')) {
          toggleEmoji();
        } else if (e.keyCode === 27 && $('#chat-mute-modal').hasClass('is-open')) {
          ChatEvents.closeMuteModal();
        }
      });
    },

    bindVisibilityChange: function() {
      document.addEventListener('visibilitychange', function() {
        if (!document.hidden && ChatState.unreadCount > 0) {
          ChatAPI.updateLastSeen(ChatState.roomId);
          ChatEvents.refreshStatus();
          ChatState.unreadCount = 0;
          document.title = ChatConfig.i18n.chatBox;
        }
      });
    },

    bindSettingsMenu: function() {
      var bindToElements = function(selector) {
        $(document).on('click', selector, function(e) {
          e.stopPropagation();
          $('.setting-content').not($(this).siblings('.setting-content')).hide();
          $(this).siblings('.setting-content').toggle();
        });
      };

      bindToElements('.setting-button');
      bindToElements('.user-setting-button');

      $(document).on('click', '.setting-content a', function(e) {
        e.stopPropagation();
        var href = $(this).attr('href');
        href += '?next=' + window.location.pathname;
        $(this).attr('href', href);
      });

      $(document).on('click', function() {
        $('.setting-content').hide();
      });
    },

    initSelect2Search: function() {
      $('#search-handle').replaceWith($('<select>').attr({
        id: 'search-handle',
        name: 'other'
      }));

      var inUserRedirect = false;

      $('#search-handle').select2({
        placeholder: '<i class="fa fa-search"></i> ' + ChatConfig.i18n.searchPlaceholder,
        ajax: {
          url: ChatConfig.urls.userSearch,
          delay: 250,
          cache: true
        },
        minimumInputLength: 1,
        escapeMarkup: function(markup) {
          return markup;
        },
        templateResult: function(data) {
          if (!data.id) return data.text;
          return $('<span>')
            .append($('<img>', {
              'class': 'user-search-image',
              src: data.gravatar_url,
              width: 24,
              height: 24
            }))
            .append($('<span>', {
              'class': data.display_rank + ' user-search-name'
            }).text(data.text))
            .append($('<a>', {
              href: '/user/' + data.text,
              'class': 'user-redirect'
            })
              .append($('<i>', { 'class': 'fa fa-mail-forward' }))
              // Hover covers desktop; mousedown/touchstart makes the "select2
              // is selecting" guard fire on touch too (no reliable hover there).
              .on('mouseover', function() { inUserRedirect = true; })
              .on('mouseout', function() { inUserRedirect = false; })
              .on('mousedown touchstart', function() { inUserRedirect = true; })
              // Follow the profile link directly instead of opening a DM.
              .on('click', function(e) {
                e.stopPropagation();
                e.preventDefault();
                window.location.href = $(this).attr('href');
              }));
        }
      }).on('select2:selecting', function() {
        // Consume the flag so a touch that set it (touchstart) but never fired a
        // click (e.g. finger moved into a scroll) can't stick and block the next
        // legitimate result selection.
        if (inUserRedirect) {
          inUserRedirect = false;
          return false;
        }
        return true;
      }).on('select2:close', function() {
        // Clear any stale touch state between dropdown sessions.
        inUserRedirect = false;
      }).on('select2:select', function(e) {
        var encryptedUser = e.params.data.id;
        if (!encryptedUser) return;
        ChatEvents.loadRoom(encryptedUser);
        $(this).val(null).trigger('change');
      });
    },

    refreshStatus: function(refreshChatInfo) {
      ChatAPI.getOnlineStatus()
        .done(function(data) {
          if (data.status === 403) {
            console.log('Failed to retrieve online status');
            return;
          }
          ChatElements.chatOnlineList.html(data);
          ChatUI.highlightSelectedRoom();
        })
        .fail(function() {
          console.log('Failed to get online status');
        });

      if (refreshChatInfo) {
        this.refreshChatInfo(true);
      }
    },

    refreshChatInfo: function(clearFirst) {
      var requestOtherUserId = ChatState.otherUserId;
      var requestToken = ++ChatState.chatInfoToken;

      if (clearFirst) {
        ChatElements.chatInfo.html('');
      }

      ChatAPI.getUserOnlineStatus(requestOtherUserId)
        .done(function(data) {
          if (requestOtherUserId !== ChatState.otherUserId || requestToken !== ChatState.chatInfoToken) {
            return;
          }

          ChatElements.chatInfo.html(data);
          register_time($('.time-with-rel'));
        })
        .fail(function() {
          console.log('Failed to get user online status');
        });
    },

    startStatusPolling: function() {
      var self = this;
      setInterval(function() {
        self.refreshStatus();
      }, 3 * 60 * 1000);
    }
  };

  // ============================================
  // WebSocket Handler
  // ============================================
  var ChatWebSocket = {
    receiver: null,

    init: function() {
      if (typeof EventReceiver === 'undefined') {
        console.log('EventReceiver not available');
        return;
      }

      var self = this;
      this.receiver = new EventReceiver(
        ChatConfig.event.daemonLocation,
        [ChatConfig.event.lobbyChannel, ChatConfig.event.chatChannel],
        ChatConfig.room.lastMsgId,
        function(message) {
          self.handleMessage(message);
        }
      );
    },

    handleMessage: function(message) {
      if (message.type === 'chat_muted') {
        ChatUI.setMutedState(true);
        return;
      }

      if (message.type === 'chat_unmuted') {
        ChatUI.setMutedState(false);
        return;
      }

      // Reactions reuse the message id, so handle them before the new-message
      // dedup (which would otherwise swallow a reaction on an already-seen message).
      if (message.type === 'reaction') {
        ChatMessages.applyReaction(message);
        return;
      }

      if (ChatState.pushedMessages.has(message.message)) {
        return;
      }
      ChatState.pushedMessages.add(message.message);

      var room = message.type === 'lobby' ? '' : String(message.room);

      if (message.author_id === ChatConfig.user.id) {
        ChatMessages.checkNewMessage(message.message, message.tmp_id, room);
      } else {
        ChatMessages.addNewMessage(message.message, room, false, message);
      }
    }
  };

  // ============================================
  // Initialization
  // ============================================
  function initChat() {
    ChatState.init();
    ChatElements.init();

    ChatUI.hideLoader();
    ChatUI.highlightSelectedRoom();
    ChatUI.applyMutedState();
    ChatUtils.postProcessMessages();

    ChatState.hasNext = parseInt($('.has_next').attr('value')) || 0;

    ChatEvents.init();
    ChatWebSocket.init();

    ChatAPI.updateLastSeen(ChatState.roomId);

    // Handle initial mobile state
    if (ChatUtils.isMobile()) {
      if (ChatState.roomId) {
        // Room is selected - show chat area
        ChatUI.showRightPanel();
      } else {
        // No room - show sidebar
        ChatUI.hideRightPanel();
      }
    }

    // Skip auto-focus on mobile so the keyboard doesn't cover the chat on load.
    if (!ChatConfig.user.isMuted && !ChatUtils.isMobile()) {
      ChatElements.chatInput.focus();
    }

    // Show chat log then scroll to bottom
    ChatElements.chatLog.show();
    ChatUI.scrollToBottom();
    // Re-pin to bottom as images load, but only if the user hasn't scrolled up
    // (otherwise slow-loading avatars keep yanking them back down, especially
    // on mobile where images trickle in over several seconds).
    $('#chat-log img').on('load', function() {
      if (ChatUI.isNearBottom()) {
        ChatUI.scrollToBottom();
      }
    });
  }

  // Export for global access if needed
  window.ChatApp = {
    State: ChatState,
    API: ChatAPI,
    UI: ChatUI,
    Utils: ChatUtils,
    Drafts: ChatDrafts,
    Messages: ChatMessages,
    Events: ChatEvents,
    WebSocket: ChatWebSocket,
    init: initChat
  };

  // Initialize on document ready
  $(initChat);

})(jQuery);
