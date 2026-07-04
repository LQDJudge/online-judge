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
    pushedMessages: new Set(),
    drafts: {},
    roomCache: {},
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
      if (ChatUtils.isMobile()) {
        $('.chat-sidebar').hide();
        $('.chat-area').css('display', 'flex');
        // Scroll to bottom after display change
        var self = this;
        setTimeout(function() {
          self.scrollToBottom();
        }, 0);
      }
    },

    hideRightPanel: function() {
      if (ChatUtils.isMobile()) {
        $('.chat-sidebar').css('display', 'flex');
        $('.chat-area').hide();
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
        $('<div onclick="ChatApp.UI.hideRightPanel()" class="back-button"><i class="fa fa-arrow-left"></i></div>')
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
      ChatRoomCache.updateCurrent();
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
      ChatRoomCache.updateCurrent();
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
  // Message Cache
  // ============================================
  var ChatRoomCache = {
    getCurrentKey: function() {
      return ChatState.roomId ? 'room:' + ChatState.roomId : 'lobby';
    },

    saveCurrent: function() {
      if (!ChatElements.chatLog || !ChatElements.chatLog.length) return;

      ChatState.roomCache[this.getCurrentKey()] = {
        html: ChatElements.chatLog.html(),
        scrollTop: ChatElements.chatBox.scrollTop(),
        hasNext: ChatState.hasNext,
        cachedAt: Date.now()
      };
    },

    restoreCurrent: function() {
      var cached = ChatState.roomCache[this.getCurrentKey()];
      if (!cached) return false;

      $('.has_next').remove();
      ChatElements.chatLog.html(cached.html);
      ChatUtils.postProcessMessages(ChatElements.chatLog);
      ChatState.hasNext = cached.hasNext;
      ChatUI.hideLoader();
      ChatElements.chatBox.scrollTop(cached.scrollTop);
      return true;
    },

    updateCurrent: function() {
      this.saveCurrent();
    },

    appendToRoom: function(roomId, html) {
      var key = roomId ? 'room:' + roomId : 'lobby';
      var cached = ChatState.roomCache[key];
      if (cached) {
        cached.html += html;
        cached.cachedAt = Date.now();
      }
    },

    replaceTemporaryMessage: function(roomId, tmpId, html) {
      var key = roomId ? 'room:' + roomId : 'lobby';
      var cached = ChatState.roomCache[key];
      if (!cached) return;

      var $container = $('<div>').html(cached.html);
      var $newMessage = $(html);

      if ($container.find('#message-' + tmpId).length) {
        $container.find('#message-' + tmpId).replaceWith($newMessage);
      } else if ($container.find('#message-block-' + tmpId).length) {
        $container.find('#message-block-' + tmpId).replaceWith($newMessage.find('.message-block'));
      } else {
        $container.append($newMessage);
      }

      cached.html = $container.html();
      cached.cachedAt = Date.now();
    },

    hasRoom: function(roomId) {
      var key = roomId ? 'room:' + roomId : 'lobby';
      return !!ChatState.roomCache[key];
    },

    invalidateCurrent: function() {
      delete ChatState.roomCache[this.getCurrentKey()];
    }
  };

  // ============================================
  // Message Handling
  // ============================================
  var ChatMessages = {
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
        if (!ChatRoomCache.restoreCurrent()) {
          ChatElements.chatLog.html('');
          ChatUI.showLoader();
        }
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
            // Scroll again after images load
            ChatElements.chatLog.find('img').on('load', function() {
              ChatUI.scrollToBottom();
            });
            ChatRoomCache.updateCurrent();
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
      var isCurrentRoom = room === ChatState.roomId;

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

        if (ChatRoomCache.hasRoom(room)) {
          ChatAPI.getMessage(messageId)
            .done(function(data) {
              ChatRoomCache.appendToRoom(room, data);
            })
            .fail(function() {
              console.log('Could not cache new message');
            });
        }
      }
    },

    checkNewMessage: function(messageId, tmpId, room) {
      if (room !== ChatState.roomId) {
        if (ChatRoomCache.hasRoom(room)) {
          ChatAPI.getMessage(messageId)
            .done(function(data) {
              ChatRoomCache.replaceTemporaryMessage(room, tmpId, data);
            })
            .fail(function() {
              console.log('Failed to cache confirmed message');
            });
        }
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
          ChatRoomCache.updateCurrent();
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
            ChatRoomCache.updateCurrent();
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

    bindRoomSelection: function() {
      $(document).on('click', '.click_space', function() {
        var $row = $(this);
        var clickedUserId = $row.data('user-id') || $row.attr('id').replace('click_space_', '');
        if (clickedUserId === ChatState.otherUserId) {
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
      if (!ChatConfig.user.isMuted) {
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
      ChatRoomCache.saveCurrent();

      this.setCurrentRoom(String(roomId || ''), String(otherUserId || ''));
      this.openCurrentRoom($row);
      ChatState.lockClickSpace = false;
    },

    loadRoom: function(encryptedUser, $row) {
      if (ChatState.lockClickSpace) return;
      ChatState.lockClickSpace = true;
      ChatDrafts.saveCurrent();
      ChatRoomCache.saveCurrent();

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
              .on('mouseover', function() { inUserRedirect = true; })
              .on('mouseout', function() { inUserRedirect = false; }));
        }
      }).on('select2:selecting', function() {
        return !inUserRedirect;
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

    if (!ChatConfig.user.isMuted) {
      ChatElements.chatInput.focus();
    }

    // Show chat log then scroll to bottom
    ChatElements.chatLog.show();
    ChatUI.scrollToBottom();
    // Scroll again after images load
    $('#chat-log img').on('load', function() {
      ChatUI.scrollToBottom();
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
