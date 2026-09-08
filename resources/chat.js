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
    statusLoadToken: 0,
    replyToId: null,
    roomFilter: 'all',

    init: function() {
      this.roomId = ChatConfig.room.id;
      this.otherUserId = ChatConfig.room.otherUserId;
    }
  };

  var HISTORY_LOAD_THRESHOLD = 120;

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
    },

    isChatDisabled: function() {
      return ChatConfig.user.isMuted || ChatConfig.user.isRoomMuted ||
        ChatConfig.room.isArchived || !ChatConfig.user.canChat ||
        !ChatConfig.user.canInteractRoom;
    },

    disabledChatMessage: function() {
      if (ChatConfig.room.isArchived) return ChatConfig.i18n.roomArchived;
      if (ChatConfig.user.isRoomMuted) return ChatConfig.i18n.roomMuted;
      return ChatConfig.user.isMuted ? ChatConfig.i18n.chatMuted : ChatConfig.i18n.chatRestricted;
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

    postMessage: function(body, roomId, tmpId, replyToId) {
      return $.post(ChatConfig.urls.postMessage, {
        body: body,
        room: roomId,
        tmp_id: tmpId,
        reply_to: replyToId || ''
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

    getReactionList: function(messageId, reaction) {
      var data = { message: messageId };
      if (reaction) {
        data.reaction = reaction;
      }
      return $.get(ChatConfig.urls.reactionList, data);
    },

    muteMessage: function(messageId, scope, muteType, reason, hideRoomMessages) {
      return $.ajax({
        url: ChatConfig.urls.muteMessage,
        type: 'post',
        data: {
          message: messageId,
          scope: scope,
          mute_type: muteType,
          reason: reason,
          hide_room_messages: hideRoomMessages ? '1' : '0'
        },
        dataType: 'json'
      });
    },

    getMessage: function(messageId) {
      return $.get(ChatConfig.urls.messageAjax, { message: messageId });
    },

    getOnlineStatus: function(section) {
      var data = {};
      if (section && section !== 'all') data.section = section;
      return $.get(ChatConfig.urls.onlineStatus, data);
    },

    getUserOnlineStatus: function(userId) {
      return $.get(ChatConfig.urls.userOnlineStatus, { user: userId });
    },

    getOrCreateRoom: function(encryptedUser) {
      return $.get(ChatConfig.urls.getOrCreateRoom, { other: encryptedUser });
    },

    updateLastSeen: function(roomId) {
      return $.post(ChatConfig.urls.updateLastSeen, { room: roomId });
    },

    createGroup: function(name, memberIds) {
      return $.ajax({
        url: ChatConfig.urls.createGroup,
        type: 'post',
        traditional: true,
        data: { name: name, member_ids: memberIds || [] }
      });
    },

    getChannelOptions: function() {
      return $.get(ChatConfig.urls.channelOptions);
    },

    createChannel: function(data) {
      return $.ajax({
        url: ChatConfig.urls.createChannel,
        type: 'post',
        traditional: true,
        data: data
      });
    },

    getRoomDetails: function(roomId, data) {
      return $.get(
        ChatConfig.urls.roomDetails.replace('/0/', '/' + roomId + '/'),
        data || {}
      );
    },

    getRoomState: function(roomId) {
      return $.get(ChatConfig.urls.chat + roomId, { switch_room: '1' });
    },

    getRoomList: function(cursor, filters) {
      var data = $.extend({}, filters || {});
      if (cursor) data.cursor = cursor;
      return $.get(ChatConfig.urls.roomList, data);
    },

    roomUrl: function(template, roomId) {
      return template.replace('/0/', '/' + roomId + '/');
    },

    roomAction: function(template, roomId, data) {
      return $.ajax({
        url: this.roomUrl(template, roomId),
        type: 'post',
        traditional: true,
        data: data || {}
      });
    },

    toggleIgnore: function(url) {
      return $.post(url, { next: ChatConfig.urls.chat });
    },

    changeRoomAvatar: function(roomId, file, remove) {
      var formData = new FormData();
      if (remove) {
        formData.append('remove', '1');
      } else {
        formData.append('avatar', file);
      }
      return $.ajax({
        url: this.roomUrl(ChatConfig.urls.roomAvatar, roomId),
        type: 'post',
        data: formData,
        processData: false,
        contentType: false
      });
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

      // If a who-reacted popup was open, refresh it after the rebuild instead of
      // letting the innerHTML replacement below make it silently vanish.
      var $openList = $container.find('.reaction-list-popup');
      var listWasOpen = $openList.length > 0;
      var listReaction = listWasOpen ? ($openList.data('reaction') || '') : '';

      var myReaction = summary.my_reaction || '';
      var counts = summary.counts || {};
      $container.attr('data-my-reaction', myReaction);

      if (summary.total && summary.total > 0) {
        var html = '';
        var images = ChatConfig.reactionImages || {};
        (ChatConfig.reactions || []).forEach(function(pair) {
          var code = pair[0];
          if (counts[code]) {
            var emojis = '';
            if (images[code]) {
              emojis += '<img class="reaction-img" src="' + images[code] + '">';
            } else {
              emojis += '<span class="reaction-emoji">' + pair[1] + '</span>';
            }
            var mineClass = myReaction === code ? ' reaction-pill-mine' : '';
            html += '<button type="button" class="reaction-pill' + mineClass +
              '" data-id="' + messageId + '" data-reaction="' + code +
              '" title="' + (ChatConfig.i18n.whoReacted || '') + '">' +
              '<span class="reaction-pill-emojis">' + emojis + '</span>' +
              '<span class="reaction-pill-count">' + counts[code] + '</span></button>';
          }
        });
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

      if (listWasOpen && summary.total && summary.total > 0 && (!listReaction || counts[listReaction])) {
        ChatEvents.openReactionList(messageId, listReaction);
      }
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
      ChatElements.loader
        .css('top', (ChatElements.chatBox.scrollTop() + 12) + 'px')
        .show();
    },

    hideLoader: function() {
      ChatElements.loader.hide();
    },

    getFirstVisibleMessage: function() {
      var chatBoxTop = ChatElements.chatBox[0].getBoundingClientRect().top;
      var $fallback = ChatElements.chatLog.children('.message').first();
      var $visible = $fallback;

      ChatElements.chatLog.children('.message').each(function() {
        if (this.getBoundingClientRect().bottom > chatBoxTop) {
          $visible = $(this);
          return false;
        }
      });

      return $visible;
    },

    restoreMessageAnchor: function($anchor, anchorTop) {
      if (!$anchor || !$anchor.length || !$.contains(document, $anchor[0])) return;

      var currentTop = $anchor[0].getBoundingClientRect().top;
      ChatElements.chatBox.scrollTop(
        ChatElements.chatBox.scrollTop() + currentTop - anchorTop
      );
    },

    keepAnchorWhileImagesLoad: function($nodes, $anchor, anchorTop) {
      if (!$anchor || !$anchor.length) return;

      var expectedScrollTop = ChatElements.chatBox.scrollTop();

      $nodes.find('img').each(function() {
        if (this.complete) return;

        $(this).one('load error', function() {
          if (Math.abs(ChatElements.chatBox.scrollTop() - expectedScrollTop) > 2) {
            return;
          }

          ChatUI.restoreMessageAnchor($anchor, anchorTop);
          expectedScrollTop = ChatElements.chatBox.scrollTop();
        });
      });
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
          ChatUI.updateUnreadBadge(ChatState.roomId, true);
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
      this.hideDetailsPanel();
      if (ChatUtils.isMobile()) {
        $('.chat-area').removeClass('mobile-visible');
        $('.chat-sidebar').removeClass('mobile-hidden');
        // Scroll sidebar to top
        $('#chat-online-content').scrollTop(0);
      }
    },

    showDetailsPanel: function() {
      $('#chat-container').addClass('details-open');
      $('#chat-details-panel').addClass('is-open').attr('aria-hidden', 'false');
      $('#chat-room-details').attr('aria-expanded', 'true');
    },

    hideDetailsPanel: function() {
      $('#chat-container').removeClass('details-open');
      $('#chat-details-panel').removeClass('is-open').attr('aria-hidden', 'true');
      $('#chat-room-details').attr('aria-expanded', 'false');
    },

    highlightSelectedRoom: function() {
      $('.status-row').removeClass('selected');
      $('[data-room="' + ChatState.roomId + '"]').addClass('selected');
    },

    updateRoomAvatar: function(roomId, avatarUrl, roomType) {
      var icon = roomType === 'group' ? 'fa-users' : 'fa-hashtag';
      var appendAvatar = function($container, imageClass) {
        $container.empty();
        if (avatarUrl) {
          $container.append(
            $('<img alt="">').attr('src', avatarUrl).addClass(imageClass + ' room-avatar')
          );
        } else {
          var $fallback = $('<span class="status-room-icon" aria-hidden="true">')
            .append($('<i class="fa">').addClass(icon));
          if (imageClass === 'info-pic') $fallback.addClass('info-pic');
          $container.append($fallback);
        }
      };
      var $rowContainer = $('#room_row_' + roomId + ' .status-container').first();
      if ($rowContainer.length) appendAvatar($rowContainer, 'status-pic');
      if (String(roomId) === ChatState.roomId) {
        var $headerContainer = $('#chat-info .chat-header-avatar').first();
        if ($headerContainer.length) appendAvatar($headerContainer, 'info-pic');
        var $detailsIcon = $('#chat-details-room-icon');
        if ($detailsIcon.length) {
          $detailsIcon.empty().append(avatarUrl ?
            $('<img class="chat-details-room-avatar" alt="">').attr('src', avatarUrl) :
            $('<i class="fa" aria-hidden="true">').addClass(icon)
          );
        }
        var $detailsPreview = $('.chat-room-avatar-preview');
        if ($detailsPreview.length) {
          $detailsPreview.empty().append(avatarUrl ?
            $('<img alt="">').attr('src', avatarUrl) :
            $('<i class="fa" aria-hidden="true">').addClass(icon)
          );
        }
        $('.chat-remove-room-avatar').prop('hidden', !avatarUrl);
      }
    },

    updateRoomMemberCount: function(roomId, count) {
      if (String(roomId) !== ChatState.roomId ||
          ChatConfig.room.type === 'direct' ||
          ChatConfig.room.channelKind === 'lobby') return;
      var label = count === 1 ?
        ChatConfig.i18n.member.toLocaleLowerCase() : ChatConfig.i18n.members;
      ChatElements.chatInfo.find('.active-span').last().text(count + ' ' + label);
      if ($('#chat-details-panel').hasClass('is-open')) {
        $('#chat-details-content .chat-details-members .chat-details-count').text(count);
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
      var $anchor = this.getFirstVisibleMessage();
      var anchorTop = $anchor.length ? $anchor[0].getBoundingClientRect().top : 0;
      var $nextMessage = ChatElements.chatLog.children('.message').first();
      var $nodes = $(html);
      var $messages = $nodes.filter('.message').add($nodes.find('.message'));
      $messages.addClass('message-history');
      ChatElements.chatLog.prepend($nodes);
      ChatUtils.postProcessMessages($nodes, 'none');
      ChatUtils.mergeConsecutiveMessagesFor($messages.add($nextMessage));
      this.restoreMessageAnchor($anchor, anchorTop);
      this.keepAnchorWhileImagesLoad($nodes, $anchor, anchorTop);
    },

    clearMessages: function() {
      ChatElements.chatLog.html('');
    },

    updateUnreadBadge: function(roomId, hide) {
      var $row = $('#room_row_' + roomId + ', #lobby_row[data-room="' + roomId + '"]');
      var badge = $row.find('.unread-count');
      if (hide) {
        badge.hide();
      }
    },

    setUnreadBadge: function(roomId, count) {
      var $row = $('#room_row_' + roomId + ', #lobby_row[data-room="' + roomId + '"]');
      var $badge = $row.find('.unread-count');

      if (count > 0) {
        var displayCount = count > 99 ? '99+' : count;
        if ($badge.length) {
          $badge.data('count', count).text(displayCount).show();
        } else {
          if ($row.length) {
            var $newBadge = $('<span class="unread-count">')
              .data('count', count).text(displayCount);
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

    incrementUnreadBadge: function(roomId) {
      var $row = $('#room_row_' + roomId + ', #lobby_row[data-room="' + roomId + '"]');
      var $badge = $row.find('.unread-count');
      var current = Number($badge.data('count'));
      if (!current) {
        current = $badge.text().trim() === '99+' ? 100 : Number($badge.text()) || 0;
      }
      this.setUnreadBadge(roomId, current + 1);
    },

    moveRoomToTop: function(roomId) {
      var $row = $('#room_row_' + roomId);
      if (!$row.length) {
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
      } else {
        var $row = $('#room_row_' + roomId);
        if ($row.length) {
          $('<span class="status_last_message status-last-message wrapline">')
            .attr('id', 'last_msg-' + roomId)
            .text(text)
            .appendTo($row.find('.status-user'));
        }
      }
    },

    setUserOnline: function(userId) {
      // Update sidebar status circle
      var $sidebarCircle = $('.status-row[data-user-id="' + userId + '"] .status-circle');
      $sidebarCircle.removeClass('offline').addClass('online');

      // Update chat header status circle if viewing this user
      if (String(userId) === ChatState.otherUserId) {
        var $headerCircle = $('.chat-header .info-circle, #chat-info .info-circle');
        $headerCircle.removeClass('offline').addClass('online');
      }
    },

    setMutedState: function(isMuted) {
      ChatConfig.user.isMuted = isMuted;
      var isDisabled = ChatUtils.isChatDisabled();
      var disabledMessage = ChatUtils.disabledChatMessage();

      ChatElements.chatInput
        .prop('disabled', isDisabled)
        .attr('placeholder', isDisabled ? disabledMessage : ChatConfig.i18n.enterMessage);
      ChatElements.chatInputContainer.toggleClass('is-muted', isDisabled);
      ChatElements.chatSubmitButton.toggleClass('is-disabled', isDisabled);
      ChatElements.emojiButton
        .toggleClass('is-disabled', isDisabled)
        .attr('title', isDisabled ? disabledMessage : ChatConfig.i18n.emoji);

      if (isDisabled) {
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
    },

    restoreInteractionAfterUnmute: function() {
      ChatConfig.user.canInteractRoom = !ChatConfig.room.isArchived &&
        !ChatConfig.user.isRoomMuted && ChatConfig.user.canChat;
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
    // Apply a reaction event. The broadcast carries the group counts (shared by
    // everyone) but my_reaction is per-viewer. If this event is the current user's
    // OWN reaction echoed from another tab/device, adopt the actor's reaction so
    // this tab's highlight stays in sync; otherwise keep our own DOM state.
    // If the message isn't in the current view, there's nothing to do.
    applyReaction: function(message) {
      var $container = $('#message-reactions-' + message.message);
      if (!$container.length) return;
      var myReaction;
      if (message.user_id === ChatConfig.user.id) {
        myReaction = message.actor_reaction || null;
      } else {
        myReaction = $container.attr('data-my-reaction') || null;
      }
      ChatUI.renderReactions(message.message, {
        counts: message.counts || {},
        total: message.total || 0,
        my_reaction: myReaction
      });
    },

    addFromTemplate: function(body, tmpId, replyToId) {
      if (ChatState.roomId) {
        $('#last_msg-' + ChatState.roomId).html(body);
      }
      var html = ChatConfig.messageTemplate;
      html = html.replace(/\$body/g, body).replace(/\$id/g, tmpId);
      var $html = $(html);
      $html.find('.time-with-rel').attr('data-iso', (new Date()).toISOString());
      if (replyToId) {
        var $parent = $('#message-' + replyToId);
        if ($parent.length) {
          var author = $parent.find('.message-header a').first().text().trim();
          var text = $parent.find('.message-text').first().text().trim();
          if (text.length > 60) text = text.substring(0, 60) + '…';
          var $quote = $(
            '<div class="message-reply-quote" role="button" tabindex="0" data-reply-target="' +
            replyToId + '"><span class="message-reply-quote-author"></span>' +
            '<span class="message-reply-quote-text"></span></div>'
          );
          $quote.find('.message-reply-quote-author').text(author);
          $quote.find('.message-reply-quote-text').text(text || '[image]');
          // Insert ABOVE the bubble row (matches the server template layout).
          $html.find('.message-bubble-wrapper').before($quote);
        }
      }
      ChatUI.addMessage($html[0].outerHTML, true);
    },

    showReplyBanner: function(messageId, authorName, snippet) {
      ChatState.replyToId = messageId;
      var $banner = $('#chat-reply-banner');
      if (!$banner.length) {
        $banner = $(
          '<div id="chat-reply-banner" class="chat-reply-banner">' +
          '<div class="chat-reply-banner-body">' +
          '<span class="chat-reply-banner-label"></span>' +
          '<span class="chat-reply-banner-snippet"></span>' +
          '</div>' +
          '<button type="button" class="chat-reply-banner-cancel" aria-label="' +
          (ChatConfig.i18n.cancel || 'Cancel') + '">&times;</button>' +
          '</div>'
        );
        $('#chat-input-container').before($banner);
      }
      var label = interpolate(
        ChatConfig.i18n.replyingTo || 'Replying to %(user)s',
        { user: authorName }, true
      );
      $banner.find('.chat-reply-banner-label').text(label);
      $banner.find('.chat-reply-banner-snippet').text(snippet);
      $banner.show();
      ChatElements.chatInput.focus();
    },

    clearReplyBanner: function() {
      ChatState.replyToId = null;
      $('#chat-reply-banner').hide();
    },

    submit: function() {
      if (ChatUtils.isChatDisabled()) return;

      var body = ChatElements.chatInput.val().trim();
      if (!body) return;

      var tmpId = Date.now();
      var replyToId = ChatState.replyToId;
      var roomId = ChatState.roomId;
      $('#chat-send-error').text('');

      ChatDrafts.clearCurrent();
      $('#chat-input-container').height('auto');

      this.addFromTemplate(body, tmpId, replyToId);
      this.clearReplyBanner();

      ChatAPI.postMessage(body, roomId, tmpId, replyToId)
        .done(function() {
          $('#empty_msg').hide();
          ChatElements.chatInput.focus();
          ChatUI.moveRoomToTop(roomId);
        })
        .fail(function(response) {
          $('#message-' + tmpId).remove();
          $('#chat-send-error').text(
            response.responseJSON && response.responseJSON.error ?
              response.responseJSON.error : ChatConfig.i18n.unableSend
          );
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
      var messageSelector = '#message-' + messageId;

      // A room-state response and its WebSocket event can race when opening a
      // newly created room. The history may already contain this message by
      // the time the event is handled, so never fetch or append it twice.
      if (isCurrentRoom && $(messageSelector).length) {
        return;
      }

      // Membership system events are visible in an open room, but they do not
      // affect unread counts, sidebar previews, or activity ordering.
      if (!isCurrentRoom && wsMessage && wsMessage.notifies === false) {
        return;
      }

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
            // Recheck after the request: room history may have finished loading
            // while this individual-message request was in flight.
            if (room === ChatState.roomId && !$(messageSelector).length) {
              ChatUI.addMessage(data);
              if (!document.hidden) {
                ChatAPI.updateLastSeen(ChatState.roomId);
              }
              // Update sidebar: last message preview + move to top
              if (wsMessage && wsMessage.room) {
                var $msg = $(data);
                var msgText = $msg.find('.message-text').text().trim();
                if (msgText.length > 50) {
                  msgText = msgText.substring(0, 50) + '...';
                }
                ChatUI.setLastMessagePreview(wsMessage.room, msgText || ChatConfig.i18n.newMessage);
                ChatUI.moveRoomToTop(wsMessage.room);
              }
            }
          })
          .fail(function() {
            console.log('Could not add new message');
          });
      } else {
        // Message is for a different room - update sidebar
        if (wsMessage && wsMessage.room) {
          if (wsMessage.unread_count !== undefined) {
            ChatUI.setUnreadBadge(wsMessage.room, wsMessage.unread_count);
          } else if (!isSelfAuthor && wsMessage.notifies !== false) {
            ChatUI.incrementUnreadBadge(wsMessage.room);
          }
          if (wsMessage.room) {
            ChatUI.setLastMessagePreview(wsMessage.room, ChatConfig.i18n.newMessage);
          }
          ChatUI.moveRoomToTop(wsMessage.room);
        }
      }
    },

    checkNewMessage: function(messageId, tmpId, room) {
      if (room !== ChatState.roomId) {
        // Our own message confirmed for a room we're no longer viewing. There's
        // no live DOM to reconcile; it will render fresh next time we open it.
        ChatUI.setLastMessagePreview(room, ChatConfig.i18n.newMessage);
        ChatUI.moveRoomToTop(room);
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
          ChatUI.moveRoomToTop(room);
          ChatUI.updateUnreadBadge(ChatState.roomId, true);
          ChatUtils.postProcessMessages($newMessage, 'incremental');
        })
        .fail(function() {
          console.log('Failed to check message');
          $('#message-block-' + tmpId + ' p').addClass('chat-message-sync-failed');
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
      this.bindReply();
      this.bindRoomSelection();
      this.bindEmojiPicker();
      this.bindVisibilityChange();
      this.bindSettingsMenu();
      this.bindRoomManagement();
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

    bindReply: function() {
      $(document).on('click', '.message-reply-toggle', function(e) {
        e.stopPropagation();
        if (ChatUtils.isChatDisabled()) return;
        var messageId = $(this).data('id');
        var $msg = $('#message-' + messageId);
        var author = $msg.find('.message-header a').first().text().trim();
        var text = $msg.find('.message-text').first().text().trim();
        if (text.length > 60) text = text.substring(0, 60) + '…';
        ChatMessages.showReplyBanner(messageId, author, text || '[image]');
      });

      $(document).on('click', '.chat-reply-banner-cancel', function(e) {
        e.stopPropagation();
        ChatMessages.clearReplyBanner();
      });

      var jumpToReplyQuoteParent = function(quote, event) {
        if (event) event.stopPropagation();
        var $q = $(quote);
        var parentId = $q.data('reply-target');
        if (!parentId) return;  // an "unavailable" quote has no target
        var $target = $('#message-' + parentId);
        if ($target.length) {
          // Scroll only the #chat-box container, NOT the whole page. Using
          // Element.scrollIntoView() here also scrolls the window to bring the
          // element into the viewport, which shoves the page up and leaves a
          // blank area at the bottom. Set the container's scrollTop directly to
          // center the parent inside the box (mirrors ChatUI.scrollToBottom).
          var box = ChatElements.chatBox[0];
          var boxTop = box.getBoundingClientRect().top;
          var targetTop = $target[0].getBoundingClientRect().top;
          var centerOffset = (box.clientHeight - $target[0].offsetHeight) / 2;
          box.scrollTo({
            top: box.scrollTop + (targetTop - boxTop) - centerOffset,
            behavior: 'smooth',
          });
          var $block = $('#message-block-' + parentId);
          $block.removeClass('message-highlight');
          void $block[0].offsetWidth;  // reflow so re-adding the class restarts the anim
          $block.addClass('message-highlight');
          setTimeout(function() { $block.removeClass('message-highlight'); }, 2000);
        } else {
          // MVP contract: the parent is older than the loaded window (no
          // gap-fetch yet — that's the backlog "full support" path). Give a
          // clear, predictable cue every time instead of a silent dead click:
          // flash the quote and show a transient loaded-history-limit tooltip.
          $q.attr('data-hint', ChatConfig.i18n.replyNotLoaded || 'Original message is outside the loaded history.')
            .addClass('message-reply-quote-flash message-reply-quote-hint');
          setTimeout(function() {
            $q.removeClass('message-reply-quote-flash message-reply-quote-hint')
              .removeAttr('data-hint');
          }, 1500);
        }
      };

      $(document).on('click', '.message-reply-quote', function(e) {
        // Let clicks on the inner author link navigate normally.
        if ($(e.target).closest('a').length) return;
        jumpToReplyQuoteParent(this, e);
      });

      $(document).on('keydown', '.message-reply-quote', function(e) {
        if ($(e.target).closest('a').length) return;
        var key = e.key || e.which;
        if (key !== 'Enter' && key !== ' ' && key !== 'Spacebar' && key !== 13 && key !== 32) return;
        e.preventDefault();
        jumpToReplyQuoteParent(this, e);
      });
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

        // Trigger slightly before the hard top so history is often ready when
        // the user reaches it.
        if (ChatElements.chatBox.scrollTop() <= HISTORY_LOAD_THRESHOLD &&
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

    updateMuteModalScope: function() {
      if (!this.pendingMuteAction) return;

      var muteAction = this.pendingMuteAction;
      var scope = muteAction.canSiteWide
        ? $('input[name="chat-mute-scope"]:checked').val()
        : 'room';
      var isSiteWide = scope === 'site';
      var durationDays = isSiteWide
        ? muteAction.siteDurationDays
        : muteAction.roomDurationDays;
      muteAction.scope = scope;

      $('#chat-mute-title').text(ChatConfig.i18n.muteUserTitle);
      var summary = ChatConfig.i18n.siteWideMuteSummary;
      if (!isSiteWide) {
        summary = interpolate(
          ChatConfig.i18n.muteInRoomSummary,
          {time: moment().add(durationDays, 'days').format('lll')},
          true
        );
      }
      $('#chat-mute-summary').text(summary);
      $('#chat-mute-type-row').prop('hidden', !isSiteWide);
      $('#chat-mute-temporary-label').text(
        durationDays === 1 ? ChatConfig.i18n.muteTemporaryOneDay : interpolate(
          ChatConfig.i18n.muteTemporaryDays,
          {days: durationDays},
          true
        )
      );
      $('#chat-site-hide-row').prop('hidden', !isSiteWide);
      if (!isSiteWide) {
        $('#chat-site-hide-messages').prop('checked', false);
      }
      $('#chat-mute-confirm').text(ChatConfig.i18n.confirmMute);
    },

    openMuteModal: function(messageId, canSiteWide, reasonRequired, roomDurationDays, siteDurationDays) {
      this.pendingMuteAction = {
        messageId: messageId,
        scope: 'room',
        canSiteWide: canSiteWide,
        reasonRequired: reasonRequired,
        roomDurationDays: roomDurationDays || 1,
        siteDurationDays: siteDurationDays || 1
      };

      $('#chat-mute-scope-row').prop('hidden', !canSiteWide);
      $('input[name="chat-mute-scope"][value="room"]').prop('checked', true);
      $('input[name="chat-mute-type"][value="temporary"]').prop('checked', true);
      $('#chat-site-hide-messages').prop('checked', false);
      $('#chat-mute-reason-label').text(
        reasonRequired
          ? ChatConfig.i18n.muteReason + ' *'
          : ChatConfig.i18n.muteReason
      );
      $('#chat-mute-reason')
        .val('')
        .attr('placeholder', ChatConfig.i18n.muteReasonPlaceholder);
      $('#chat-mute-error').text('');
      this.updateMuteModalScope();
      $('#chat-mute-modal')
        .addClass('is-open')
        .attr('aria-hidden', 'false');
      $('#chat-mute-reason').focus();
    },

    openMemberMuteModal: function(roomId, targetId, targetName, reasonRequired, onSuccess) {
      this.pendingMuteAction = {
        source: 'member',
        roomId: roomId,
        targetId: targetId,
        targetName: targetName,
        scope: 'room',
        canSiteWide: false,
        reasonRequired: reasonRequired,
        onSuccess: onSuccess
      };

      $('#chat-mute-title').text(ChatConfig.i18n.muteUserTitle);
      $('#chat-mute-summary').text(interpolate(
        ChatConfig.i18n.muteMemberSummary,
        {user: targetName},
        true
      ));
      $('#chat-mute-scope-row, #chat-mute-type-row, #chat-site-hide-row')
        .prop('hidden', true);
      $('#chat-site-hide-messages').prop('checked', false);
      $('#chat-mute-reason-label').text(
        reasonRequired
          ? ChatConfig.i18n.muteReason + ' *'
          : ChatConfig.i18n.muteReason
      );
      $('#chat-mute-reason')
        .val('')
        .attr('placeholder', ChatConfig.i18n.muteReasonPlaceholder);
      $('#chat-mute-error').text('');
      $('#chat-mute-confirm').text(ChatConfig.i18n.confirmMute);
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
      var muteType = muteAction.scope === 'site'
        ? $('input[name="chat-mute-type"]:checked').val()
        : 'temporary';
      var hideRoomMessages = muteAction.scope === 'site' &&
        $('#chat-site-hide-messages').prop('checked');
      var $confirm = $('#chat-mute-confirm');
      $confirm.prop('disabled', true);

      var request = muteAction.source === 'member'
        ? ChatAPI.roomAction(ChatConfig.urls.memberAction, muteAction.roomId, {
          action: 'mute',
          user_id: muteAction.targetId,
          reason: reason
        })
        : ChatAPI.muteMessage(
          muteAction.messageId,
          muteAction.scope,
          muteType,
          reason,
          hideRoomMessages
        );
      request
        .done(function() {
          ChatEvents.closeMuteModal();
          if (muteAction.onSuccess) {
            muteAction.onSuccess();
          } else {
            window.location.reload();
          }
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

      $(document).on('click', '.chat_mute', function() {
        ChatEvents.closeMessageActionMenus();
        var reasonRequired = String($(this).data('reason-required')) === '1';
        ChatEvents.openMuteModal(
          $(this).attr('value'),
          String($(this).data('can-site-wide')) === '1',
          reasonRequired,
          parseInt($(this).data('room-duration-days'), 10) || 1,
          parseInt($(this).data('site-duration-days'), 10) || 1
        );
      });

      $('input[name="chat-mute-scope"]').on('change', function() {
        ChatEvents.updateMuteModalScope();
      });

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

    closeReactionLists: function() {
      $('.reaction-list-popup').remove();
    },

    openReactionList: function(messageId, reaction) {
      var $reactions = $('#message-reactions-' + messageId);
      if (!$reactions.length) return;
      ChatEvents.closeReactionLists();
      var $popup = $('<div class="reaction-list-popup"></div>');
      $popup.data('reaction', reaction || '');
      $reactions.append($popup);
      ChatAPI.getReactionList(messageId, reaction)
        .done(function(html) { $popup.html(html); })
        .fail(function() { ChatEvents.closeReactionLists(); });
    },

    bindReactions: function() {
      // Open/close the emoji picker from the smiley toggle.
      $(document).on('click', '.message-react-toggle', function(e) {
        e.stopPropagation();
        if (ChatUtils.isChatDisabled()) return;
        ChatEvents.closeReactionLists();
        var $react = $(this).closest('.message-react');
        var wasOpen = $react.hasClass('is-open');
        ChatEvents.closeReactionPickers($react);
        $react.toggleClass('is-open', !wasOpen);
        $(this).attr('aria-expanded', wasOpen ? 'false' : 'true');
      });

      // Clicking the pill shows WHO reacted (Messenger-style), not the picker.
      $(document).on('click', '.reaction-pill', function(e) {
        e.stopPropagation();
        var messageId = $(this).data('id');
        var reaction = $(this).data('reaction') || '';
        var $existingPopup = $('#message-reactions-' + messageId).find('.reaction-list-popup');
        var alreadyOpen = $existingPopup.length > 0;
        var sameReactionOpen = alreadyOpen && ($existingPopup.data('reaction') || '') === reaction;
        ChatEvents.closeReactionPickers();
        ChatEvents.closeReactionLists();
        if (sameReactionOpen) return;          // second click closes it
        ChatEvents.openReactionList(messageId, reaction);
      });

      // Clicks inside the popup (e.g. a user link) shouldn't close it via the
      // document handler -- but the link's own navigation still works.
      $(document).on('click', '.reaction-list-popup', function(e) {
        e.stopPropagation();
      });

      // Pick a reaction -> POST, then render the authoritative summary.
      $(document).on('click', '.reaction-option', function(e) {
        e.stopPropagation();
        if (ChatUtils.isChatDisabled()) return;
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

      // Any outside click closes open pickers and who-reacted popups.
      $(document).on('click', function() {
        ChatEvents.closeReactionPickers();
        ChatEvents.closeReactionLists();
      });
    },

    bindRoomSelection: function() {
      $(document).on('click', '.click_space', function() {
        var $row = $(this);
        ChatEvents.loadKnownRoom($row.data('room'), $row);
      });

      $(document).on('click', '#lobby_row', function() {
        var $row = $(this);
        ChatEvents.loadKnownRoom($row.data('room'), $row);
      });

      // Back button for mobile
      $(document).on('click', '.back-button', function() {
        ChatUI.hideRightPanel();
      });
    },

    openCurrentRoom: function($row, roomState) {
      history.replaceState(null, '', ChatConfig.urls.chat + ChatState.roomId);
      ChatUI.hideNewMessagesBubble();
      ChatUI.highlightSelectedRoom();
      if (roomState) {
        ChatElements.chatInfo.html(roomState.header_html);
        ChatElements.chatLog.html(roomState.messages_html);
        ChatConfig.messageTemplate = roomState.message_template;
        ChatState.messageLoadToken++;
        ChatState.hasNext = parseInt($('.has_next').attr('value')) || 0;
        ChatUI.hideLoader();
        ChatUtils.postProcessMessages(ChatElements.chatLog);
        ChatUI.scrollToBottom();
      } else if ($row && $row.length) {
        ChatUI.renderHeaderFromStatusRow($row);
      }
      if (!roomState) {
        ChatMessages.loadNextPage(null, true);
      }
      ChatAPI.updateLastSeen(ChatState.roomId);
      if (!roomState) {
        ChatEvents.refreshChatInfo(false);
      }
      ChatUI.updateUnreadBadge(ChatState.roomId, true);
      ChatUI.showRightPanel();
      ChatUI.applyMutedState();
      // Don't auto-focus on mobile: it would pop the on-screen keyboard the
      // moment a conversation is opened, covering the input and newest messages.
      if (!ChatUtils.isChatDisabled() && !ChatUtils.isMobile()) {
        ChatElements.chatInput.focus();
      }
      ChatDrafts.restoreCurrent();
    },

    setCurrentRoom: function(roomId, otherUserId) {
      ChatState.roomId = roomId;
      ChatState.otherUserId = otherUserId;
      ChatConfig.room.id = roomId;
      ChatConfig.room.otherUserId = otherUserId;
      ChatElements.chatInput.attr('maxlength', ChatConfig.room.maxLength);
      // A pending reply belongs to the room we're leaving; don't leak it across rooms.
      ChatMessages.clearReplyBanner();
    },

    loadKnownRoom: function(roomId, $row, fallbackUrl) {
      if (String(roomId) === ChatState.roomId) {
        ChatUI.showRightPanel();
        return;
      }
      if (ChatState.lockClickSpace) return;
      ChatUI.hideDetailsPanel();
      ChatState.lockClickSpace = true;
      ChatDrafts.saveCurrent();

      var self = this;
      ChatAPI.getRoomState(roomId)
        .done(function(data) {
          ChatConfig.room.type = data.room.type;
          ChatConfig.room.channelKind = data.room.channel_kind;
          ChatConfig.room.isArchived = data.room.is_archived;
          ChatConfig.room.maxLength = data.room.max_length;
          ChatConfig.room.lastMsgId = data.room.last_message_id;
          ChatConfig.user.isRoomMuted = data.user.is_room_muted;
          ChatConfig.user.canInteractRoom = data.user.can_interact_room;
          ChatConfig.user.canModerateChat = data.user.can_moderate_chat;
          self.setCurrentRoom(
            String(data.room.id),
            String(data.room.other_user_id || '')
          );
          self.openCurrentRoom($row, data);
          ChatWebSocket.refreshAuthorization();
        })
        .fail(function() {
          window.location.href = fallbackUrl || ($row && $row.data('room-url')) ||
            (ChatConfig.urls.chat + roomId);
        })
        .always(function() {
          ChatState.lockClickSpace = false;
        });
    },

    loadRoom: function(encryptedUser, $row) {
      if (ChatState.lockClickSpace) return;
      ChatState.lockClickSpace = true;
      ChatDrafts.saveCurrent();

      if (encryptedUser) {
        ChatAPI.getOrCreateRoom(encryptedUser)
          .done(function(data) {
            ChatState.lockClickSpace = false;
            ChatEvents.refreshStatus();
            ChatEvents.loadKnownRoom(data.room, null, data.url);
          })
          .fail(function() {
            console.log('Failed to get_or_create_room');
            ChatState.lockClickSpace = false;
          });
      } else {
        this.setCurrentRoom('', '');
        this.openCurrentRoom($row);
        ChatState.lockClickSpace = false;
      }
    },

    bindRoomManagement: function() {
      var pendingMemberAction = null;
      var pendingLeaveRoom = null;
      var openModal = function(selector) {
        $(selector).addClass('is-open').attr('aria-hidden', 'false');
        $(selector).find('input, select, button').filter(':visible').first().focus();
      };
      var closeModal = function($modal) {
        $modal.removeClass('is-open').attr('aria-hidden', 'true');
        $modal.find('.chat-modal-error').text('');
        if ($modal.is('#chat-member-action-modal')) pendingMemberAction = null;
        if ($modal.is('#chat-leave-room-modal')) pendingLeaveRoom = null;
      };
      var openMemberActionModal = function(action, roomId, member, onSuccess) {
        var isBan = action === 'ban';
        var $modal = $('#chat-member-action-modal');
        pendingMemberAction = {
          action: action,
          roomId: roomId,
          targetId: member.id,
          onSuccess: onSuccess,
          reasonRequired: !ChatConfig.user.isStaff
        };
        $('#chat-member-action-title').text(
          isBan ? ChatConfig.i18n.banMemberTitle : ChatConfig.i18n.removeMemberTitle
        );
        $('#chat-member-action-summary').text(interpolate(
          isBan ? ChatConfig.i18n.banMemberSummary : ChatConfig.i18n.removeMemberSummary,
          {user: member.name},
          true
        ));
        $('#chat-member-action-icon').attr(
          'class', isBan ? 'fa fa-ban' : 'fa fa-user-minus'
        );
        $('#chat-member-action-reason-label').text(
          pendingMemberAction.reasonRequired
            ? ChatConfig.i18n.muteReason + ' *'
            : ChatConfig.i18n.muteReason
        );
        $('#chat-member-action-reason')
          .val('')
          .attr('placeholder', ChatConfig.i18n.moderationReasonPlaceholder);
        $('#chat-member-action-confirm').text(
          isBan ? ChatConfig.i18n.ban : ChatConfig.i18n.remove
        );
        openModal('#chat-member-action-modal');
        $('#chat-member-action-reason').focus();
      };
      var initMemberSelect = function(selector, modal, maximumSelectionLength) {
        var $select = $(selector);
        if (!$select.length || $select.hasClass('select2-hidden-accessible')) return;
        $select.select2({
          dropdownParent: $(modal),
          minimumInputLength: 1,
          maximumSelectionLength: maximumSelectionLength || 0,
          width: '100%',
          placeholder: ChatConfig.i18n.searchUsers,
          ajax: { url: ChatConfig.urls.memberSearch, delay: 250 }
        });
      };
      var initOrganizationSelect = function() {
        var $select = $('#chat-channel-organization');
        if (!$select.children().length || $select.hasClass('select2-hidden-accessible')) {
          return;
        }
        $select.select2({
          dropdownParent: $('#chat-channel-modal'),
          minimumResultsForSearch: 0,
          width: '100%',
          ajax: {
            url: ChatConfig.urls.channelOptions,
            delay: 250,
            data: function(params) {
              return {term: params.term || '', page: params.page || 1};
            },
            processResults: function(data) {
              return {
                results: (data.organizations || []).map(function(org) {
                  return {id: org.id, text: org.name};
                }),
                pagination: {more: !!data.more}
              };
            }
          }
        });
      };
      $('.chat-modal-cancel').on('click', function() {
        closeModal($(this).closest('.chat-modal-backdrop'));
      });
      $('#chat-member-action-confirm').on('click', function() {
        if (!pendingMemberAction) return;
        var memberAction = pendingMemberAction;
        var reason = $('#chat-member-action-reason').val().trim();
        var $modal = $('#chat-member-action-modal');
        if (memberAction.reasonRequired && !reason) {
          $modal.find('.chat-modal-error').text(ChatConfig.i18n.muteReasonRequired);
          $('#chat-member-action-reason').focus();
          return;
        }
        var $button = $(this).prop('disabled', true);
        ChatAPI.roomAction(ChatConfig.urls.memberAction, memberAction.roomId, {
          action: memberAction.action,
          user_id: memberAction.targetId,
          reason: reason
        }).done(function() {
          closeModal($modal);
          if (memberAction.onSuccess) memberAction.onSuccess();
        }).fail(function(response) {
          $modal.find('.chat-modal-error').text(
            response.responseJSON ? response.responseJSON.error : ChatConfig.i18n.unableLoadRoom
          );
        }).always(function() {
          $button.prop('disabled', false);
        });
      });
      $('#chat-leave-room-confirm').on('click', function() {
        if (!pendingLeaveRoom) return;
        var room = pendingLeaveRoom;
        var $modal = $('#chat-leave-room-modal');
        var $button = $(this).prop('disabled', true);
        ChatAPI.roomAction(ChatConfig.urls.leaveRoom, room.id)
          .done(function(result) {
            pendingLeaveRoom = null;
            window.location.href = result.url;
          })
          .fail(function(response) {
            $modal.find('.chat-modal-error').text(
              response.responseJSON ? response.responseJSON.error : ChatConfig.i18n.unableLoadRoom
            );
          })
          .always(function() {
            $button.prop('disabled', false);
          });
      });
      $('#chat-new-group').on('click', function() {
        $('#chat-group-name').val('');
        $('#chat-group-members').val(null).trigger('change');
        openModal('#chat-group-modal');
        initMemberSelect('#chat-group-members', '#chat-group-modal', 49);
      });
      $('#chat-group-create').on('click', function() {
        var $button = $(this);
        var $modal = $('#chat-group-modal');
        $button.prop('disabled', true);
        ChatAPI.createGroup(
          $('#chat-group-name').val(),
          $('#chat-group-members').val() || []
        )
          .done(function(data) {
            closeModal($modal);
            ChatEvents.refreshStatus();
            ChatEvents.loadKnownRoom(data.room, null, data.url);
          })
          .fail(function(response) {
            $modal.find('.chat-modal-error').text(
              response.responseJSON ? response.responseJSON.error : ChatConfig.i18n.unableCreateGroup
            );
          })
          .always(function() { $button.prop('disabled', false); });
      });
      $('#chat-new-channel').on('click', function() {
        var $modal = $('#chat-channel-modal');
        $('#chat-channel-name').val('');
        $('#chat-channel-members').val(null).trigger('change');
        ChatAPI.getChannelOptions().done(function(data) {
          var $organizations = $('#chat-channel-organization');
          if ($organizations.hasClass('select2-hidden-accessible')) {
            $organizations.select2('destroy');
          }
          $organizations.empty();
          (data.organizations || []).forEach(function(org) {
            $organizations.append($('<option>').val(org.id).text(org.name));
          });
          var hasOrganizations = !!$organizations.children().length;
          $('#chat-channel-kind option[value="custom"]').toggle(!!data.can_create_custom);
          if (!hasOrganizations && data.can_create_custom) {
            $('#chat-channel-kind').val('custom');
          } else {
            $('#chat-channel-kind').val('organization');
          }
          $('#chat-channel-create').prop(
            'disabled', !hasOrganizations && !data.can_create_custom
          );
          $('#chat-channel-kind').trigger('change');
          openModal('#chat-channel-modal');
          if ($('#chat-channel-kind').val() === 'custom') {
            initMemberSelect('#chat-channel-members', '#chat-channel-modal');
          } else {
            initOrganizationSelect();
          }
          if (!hasOrganizations && !data.can_create_custom) {
            $modal.find('.chat-modal-error').text(ChatConfig.i18n.noAvailableOrganizations);
          }
        });
      });
      $('#chat-channel-kind').on('change', function() {
        var custom = $(this).val() === 'custom';
        $('#chat-channel-custom-fields').prop('hidden', !custom);
        $('#chat-channel-organization-fields').prop('hidden', custom);
        if (custom && $('#chat-channel-modal').hasClass('is-open')) {
          initMemberSelect('#chat-channel-members', '#chat-channel-modal');
        } else if (!custom && $('#chat-channel-modal').hasClass('is-open')) {
          initOrganizationSelect();
        }
      });
      $('#chat-channel-create').on('click', function() {
        var kind = $('#chat-channel-kind').val();
        var data = {
          channel_kind: kind,
          organization_id: $('#chat-channel-organization').val(),
          name: $('#chat-channel-name').val(),
          member_ids: $('#chat-channel-members').val() || []
        };
        var $button = $(this);
        $button.prop('disabled', true);
        ChatAPI.createChannel(data)
          .done(function(result) {
            closeModal($('#chat-channel-modal'));
            ChatEvents.refreshStatus();
            ChatEvents.loadKnownRoom(result.room, null, result.url);
          })
          .fail(function(response) {
            $('#chat-channel-modal .chat-modal-error').text(
              response.responseJSON ? response.responseJSON.error : ChatConfig.i18n.unableCreateChannel
            );
          })
          .always(function() { $button.prop('disabled', false); });
      });
      $(document).on('click', '#chat-unhide-room', function() {
        ChatAPI.roomAction(ChatConfig.urls.roomVisibility, ChatState.roomId, { hidden: '0' })
          .done(function() { window.location.reload(); });
      });

      var openRoomList = function(kind) {
        var filters = kind === 'hidden' ? { hidden: '1' } : { archived: '1' };
        var $modal = $('#chat-room-list-modal');
        var $content = $('#chat-room-list-content').empty();
        var $more = $('#chat-room-list-more').prop('hidden', true).off('click');
        var $search = $('#chat-room-list-search').val('');
        var searchTimer = null;
        $('#chat-room-list-title').text(
          kind === 'hidden' ? ChatConfig.i18n.hiddenRooms : ChatConfig.i18n.archivedRooms
        );
        $('#chat-room-list-description').text(
          kind === 'hidden' ?
            ChatConfig.i18n.hiddenRoomsDescription : ChatConfig.i18n.archivedRoomsDescription
        );
        $('#chat-room-list-icon i')
          .attr('class', kind === 'hidden' ? 'fa fa-eye-slash' : 'fa fa-archive');
        openModal('#chat-room-list-modal');

        var loadPage = function(cursor, append) {
          ChatAPI.getRoomList(cursor, filters).done(function(result) {
            if (!append) $content.empty();
            (result.rooms || []).forEach(function(room) {
              var $row = $('<div class="chat-managed-room">');
              var icon = room.room_type === 'channel' ? 'fa-hashtag' :
                (room.room_type === 'group' ? 'fa-users' : 'fa-user');
              var typeLabel = room.room_type === 'channel' ? ChatConfig.i18n.channel :
                (room.room_type === 'group' ? ChatConfig.i18n.group :
                  ChatConfig.i18n.directMessage);
              $('<span class="chat-managed-room-icon">')
                .append($('<i class="fa">').addClass(icon)).appendTo($row);
              var $identity = $('<span class="chat-managed-room-identity">').appendTo($row);
              $('<a class="chat-managed-room-link">').attr('href', room.url)
                .text(room.name)
                .on('click', function(event) {
                  event.preventDefault();
                  closeModal($modal);
                  ChatEvents.loadKnownRoom(room.id, null, room.url);
                }).appendTo($identity);
              $('<span class="chat-managed-room-type">').text(typeLabel).appendTo($identity);
              if (kind === 'hidden') {
                $('<button type="button" class="action-btn small">')
                  .text(ChatConfig.i18n.unhideRoom)
                  .on('click', function() {
                    ChatAPI.roomAction(ChatConfig.urls.roomVisibility, room.id, { hidden: '0' })
                      .done(function() { $row.remove(); });
                  }).appendTo($row);
              }
              $content.append($row);
            });
            if (!$content.children().length) {
              $content.append($('<p>').text(ChatConfig.i18n.noRooms));
            }
            $more.prop('hidden', !result.has_more).data('cursor', result.next_cursor || '');
          }).fail(function(response) {
            $modal.find('.chat-modal-error').text(
              response.responseJSON ? response.responseJSON.error : ChatConfig.i18n.unableLoadRoom
            );
          });
        };
        $more.on('click', function() { loadPage($more.data('cursor'), true); });
        $search.off('input.chatRoomFilter').on('input.chatRoomFilter', function() {
          clearTimeout(searchTimer);
          filters.search = $(this).val().trim();
          searchTimer = setTimeout(function() { loadPage(null, false); }, 250);
        });
        loadPage(null, false);
      };
      $('#chat-hidden-rooms').on('click', function() { openRoomList('hidden'); });
      $('#chat-archived-rooms').on('click', function() { openRoomList('archived'); });
      $('.chat-details-close').on('click', function() {
        ChatUI.hideDetailsPanel();
        $('#chat-room-details').focus();
      });
      $(document).on('click.chatMemberActions', '.chat-member-actions > summary', function() {
        var currentMenu = $(this).parent().get(0);
        $('.chat-member-actions[open]').each(function() {
          if (this !== currentMenu) $(this).removeAttr('open');
        });
      });
      $(document).on('click.chatMemberActions', function(event) {
        if (!$(event.target).closest('.chat-member-actions').length) {
          $('.chat-member-actions[open]').removeAttr('open');
        }
      });
      $('#chat-details-content').on('scroll.chatMemberActions', function() {
        $('.chat-member-actions[open]').removeAttr('open');
      });
      $(document).on('keydown.chatDetails', function(event) {
        if (event.key !== 'Escape') return;
        if ($('#chat-member-action-modal').hasClass('is-open')) {
          closeModal($('#chat-member-action-modal'));
          event.stopImmediatePropagation();
          return;
        }
        if ($('#chat-leave-room-modal').hasClass('is-open')) {
          closeModal($('#chat-leave-room-modal'));
          event.stopImmediatePropagation();
          return;
        }
        if ($('#chat-mute-modal').hasClass('is-open')) return;
        var $openMemberMenus = $('.chat-member-actions[open]');
        if ($openMemberMenus.length) {
          var $summary = $openMemberMenus.last().children('summary');
          $openMemberMenus.removeAttr('open');
          $summary.focus();
          event.stopImmediatePropagation();
          return;
        }
        if ($('#chat-details-panel').hasClass('is-open')) {
          ChatUI.hideDetailsPanel();
          $('#chat-room-details').focus();
        }
      });
      $(document).on('click', '#chat-room-details', function() {
        var $panel = $('#chat-details-panel');
        if ($panel.hasClass('is-open')) {
          ChatUI.hideDetailsPanel();
          return;
        }
        var $content = $('#chat-details-content').empty().append(
          $('<span class="chat-details-loader">').append(
            $('<i class="fa fa-spinner fa-pulse" aria-hidden="true">')
          )
        );
        var $error = $panel.find('.chat-modal-error').text('');
        $('#chat-details-title').text(ChatConfig.i18n.roomDetails);
        $('#chat-details-summary').text(ChatConfig.i18n.loadingRoomInformation);
        ChatUI.showDetailsPanel();

        var showError = function(response) {
          $error.text(
            response && response.responseJSON ?
              response.responseJSON.error : ChatConfig.i18n.unableLoadRoom
          );
        };
        var expandedDetailsSection = '';
        var createSection = function(icon, title, className) {
          var $section = $('<details class="chat-details-section">')
            .addClass(className || '');
          var $heading = $('<summary class="chat-details-section-heading">').append(
            $('<span class="chat-details-section-icon">').append(
              $('<i class="fa" aria-hidden="true">').addClass(icon)
            ),
            $('<h4>').text(title),
            $('<i class="fa fa-chevron-down chat-details-chevron" aria-hidden="true">')
          );
          var $body = $('<div class="chat-details-section-body">');
          $section.append($heading, $body);
          if (className && expandedDetailsSection === className) {
            $section.prop('open', true);
          }
          $section.on('toggle', function() {
            if (!this.open) {
              if (expandedDetailsSection === className) expandedDetailsSection = '';
              return;
            }
            expandedDetailsSection = className || '';
            $content.find('.chat-details-section[open]').not(this)
              .prop('open', false);
          });
          return {section: $section, body: $body, heading: $heading};
        };
        var roleLabel = function(role) {
          if (role === 'admin') return ChatConfig.i18n.administrator;
          if (role === 'moderator') return ChatConfig.i18n.moderator;
          return ChatConfig.i18n.member;
        };
        var roleRank = function(role) {
          if (role === 'admin') return 3;
          if (role === 'moderator') return 2;
          if (role === 'member') return 1;
          return 0;
        };

        var renderDetails = function(data) {
          $content.find('.chat-details-user-search.select2-hidden-accessible')
            .select2('destroy');
          $content.empty().scrollTop(0);
          var isLobby = data.channel_kind === 'lobby';
          var typeLabel = isLobby ? ChatConfig.i18n.lobby :
            (data.room_type === 'group' ? ChatConfig.i18n.group : ChatConfig.i18n.channel);
          var icon = isLobby ? 'fa-comments' :
            (data.room_type === 'group' ? 'fa-users' : 'fa-hashtag');
          var memberRoleLabel = function(member) {
            if (member.role === 'member') return '';
            var label = roleLabel(member.role);
            if (data.channel_kind === 'organization' &&
                member.synced_role === member.role &&
                roleRank(member.synced_role) > roleRank('member')) {
              return interpolate(
                ChatConfig.i18n.inheritedOrganizationRole,
                {role: label}, true
              );
            }
            return label;
          };
          var updateMemberRoleLabel = function($item, member) {
            var label = memberRoleLabel(member);
            var $roleLabel = $item.find('.chat-member-role-label');
            if (!label) {
              $roleLabel.remove();
              return;
            }
            if (!$roleLabel.length) {
              $roleLabel = $('<span class="chat-member-role-label">')
                .appendTo($item.find('.chat-member-text'));
            }
            $roleLabel.text(label);
          };
          $('#chat-details-title').text(data.name);
          $('#chat-details-summary').text(
            typeLabel + ' · ' + data.member_count + ' ' + ChatConfig.i18n.members
          );
          var $detailsIcon = $('#chat-details-room-icon').empty();
          if (data.avatar_url) {
            $detailsIcon.append(
              $('<img class="chat-details-room-avatar" alt="">').attr('src', data.avatar_url)
            );
          } else {
            $detailsIcon.append($('<i class="fa" aria-hidden="true">').addClass(icon));
          }

          var memberTitle = isLobby ?
            ChatConfig.i18n.lobbyModerators : ChatConfig.i18n.membersHeading;
          var memberSection = createSection('fa-users', memberTitle, 'chat-details-members');
          $('<span class="chat-details-count">').text(
            isLobby ? (data.members || []).length : data.member_count
          ).insertBefore(memberSection.heading.find('.chat-details-chevron'));

          if (data.permissions.direct_add || data.permissions.manage_lobby_moderators) {
            var $addRow = $('<div class="chat-details-add-member">');
            var $memberSearch = $('<select class="chat-details-user-search">');
            var allowMultipleAdd = data.permissions.direct_add &&
              data.channel_kind !== 'organization';
            if (allowMultipleAdd) $memberSearch.attr('multiple', 'multiple');
            var $addButton = $('<button type="button" class="action-btn small">')
              .text(data.permissions.manage_lobby_moderators ?
                ChatConfig.i18n.promoteLobbyModerator :
                (allowMultipleAdd ? ChatConfig.i18n.addMembers : ChatConfig.i18n.addMember))
              .prop('disabled', true)
              .on('click', function() {
                var selection = $memberSearch.val();
                if (!selection || !selection.length) return;
                $addButton.prop('disabled', true);
                var payload = data.permissions.manage_lobby_moderators ?
                  {action: 'lobby_moderator', user_id: selection, enabled: '1'} :
                  (allowMultipleAdd ?
                    {action: 'add', member_ids: selection} :
                    {action: 'add', user_id: selection});
                ChatAPI.roomAction(ChatConfig.urls.memberAction, data.id, payload)
                  .done(loadDetails).fail(showError)
                  .always(function() { $addButton.prop('disabled', false); });
              });
            $addRow.append($memberSearch, $addButton);
            memberSection.body.append($addRow);
            $memberSearch.select2({
              dropdownParent: $panel,
              minimumInputLength: 1,
              placeholder: ChatConfig.i18n.searchUsersToAdd,
              width: '100%',
              closeOnSelect: !allowMultipleAdd,
              maximumSelectionLength: allowMultipleAdd ?
                (data.room_type === 'group' ?
                  Math.max(1, 50 - data.member_count) : 500) : 0,
              ajax: {
                url: ChatConfig.urls.memberSearch,
                delay: 250,
                data: function(params) {
                  return {term: params.term, room: data.id};
                }
              }
            });
            $memberSearch.on('change', function() {
              var selection = $memberSearch.val();
              $addButton.prop('disabled', !selection || !selection.length);
            });
            $memberSearch.on('select2:select', function() {
              // Select2 keeps the last query in its inline field when the
              // multiple picker stays open. Clear it so the next search can
              // start immediately while retaining the selected member chips.
              $memberSearch.next('.select2-container')
                .find('.select2-search__field').val('').trigger('input');
            });
          }

          var $memberFilter;
          if ((data.members || []).length > 4) {
            var $memberFilterWrap = $('<div class="chat-details-member-filter">').append(
              $('<i class="fa fa-search" aria-hidden="true">')
            );
            $memberFilter = $('<input type="search">').attr({
              placeholder: ChatConfig.i18n.searchMembers,
              'aria-label': ChatConfig.i18n.searchMembers
            });
            $memberFilterWrap.append($memberFilter);
            memberSection.body.append($memberFilterWrap);
          }

          var $list = $('<div class="chat-member-list">');
          var $noMatches = $('<p class="chat-details-empty" hidden>')
            .text(isLobby ? ChatConfig.i18n.noLobbyModerators : ChatConfig.i18n.noMatchingMembers);
          var renderMemberRows = function(members) {
            $list.empty();
            $list.toggleClass('allows-action-overflow', members.length <= 4);
            members.forEach(function(member) {
              var $item = $('<div class="chat-member-row">')
                .attr('data-member-name', member.name.toLocaleLowerCase());
              var $avatar = $('<img class="chat-member-avatar" alt="">')
                .attr('src', member.avatar_url)
                .on('error', function() {
                  $(this).replaceWith(
                    $('<span class="chat-member-avatar chat-member-avatar-fallback">').append(
                      $('<i class="fa fa-user" aria-hidden="true">')
                    )
                  );
                });
              var $memberName = $('<strong>');
              if (member.url) {
                $memberName.addClass(member.css_class || '').append(
                  $('<a>').attr('href', member.url).text(member.name)
                );
              } else {
                $memberName.text(member.name);
              }
              var $memberText = $('<span class="chat-member-text">').append($memberName);
              var $identity = $('<div class="chat-member-identity">').append(
                $avatar,
                $memberText
              );
              var $controls = $('<div class="chat-member-controls">');
              $item.append($identity, $controls);
              updateMemberRoleLabel($item, member);
              if (data.permissions.manage_lobby_moderators) {
                $('<button type="button" class="action-btn small background-gray">')
                  .text(ChatConfig.i18n.revokeLobbyModerator)
                  .on('click', function() {
                    var $button = $(this).prop('disabled', true);
                    ChatAPI.roomAction(ChatConfig.urls.memberAction, data.id, {
                      action: 'lobby_moderator', user_id: member.id, enabled: '0'
                    }).done(loadDetails).fail(showError)
                      .always(function() { $button.prop('disabled', false); });
                  }).appendTo($controls);
              } else if (data.permissions.manage) {
                var $role = $('<select class="chat-member-role">');
                ['member', 'moderator', 'admin'].forEach(function(role) {
                  $('<option>').val(role).text(roleLabel(role))
                    .prop(
                      'disabled',
                      data.channel_kind === 'organization' &&
                        roleRank(role) < roleRank(member.synced_role)
                    )
                    .appendTo($role);
                });
                $role.attr('aria-label', ChatConfig.i18n.role + ': ' + member.name)
                  .val(member.role)
                  .on('change', function() {
                    var previousRole = member.role;
                    var $select = $(this).prop('disabled', true);
                    ChatAPI.roomAction(ChatConfig.urls.memberAction, data.id, {
                      action: 'role', user_id: member.id, role: $select.val()
                    }).done(function(result) {
                      member.role = result.role;
                      member.manual_role = result.manual_role;
                      member.synced_role = result.synced_role;
                      $select.val(member.role);
                      updateMemberRoleLabel($item, member);
                    }).fail(function(response) {
                      $select.val(previousRole);
                      showError(response);
                    }).always(function() {
                      $select.prop('disabled', false);
                    });
                  });
                $controls.append($role);
                if (member.id !== ChatConfig.user.id &&
                    (ChatConfig.user.isStaff || member.role !== 'admin')) {
                  var $actions = $('<details class="chat-member-actions">');
                  var $actionMenu = $('<div class="chat-member-action-menu">');
                  $actions.append(
                    $('<summary>').attr({
                      'aria-label': ChatConfig.i18n.moreActions,
                      title: ChatConfig.i18n.moreActions
                    }).append($('<i class="fa fa-ellipsis-h" aria-hidden="true">')),
                    $actionMenu
                  );
                  ['mute', 'remove', 'ban'].forEach(function(action) {
                    var label = action === 'mute' ? ChatConfig.i18n.muteInRoom :
                      (action === 'remove' ? ChatConfig.i18n.remove : ChatConfig.i18n.ban);
                    $('<button type="button">').text(label)
                      .on('click', function() {
                        $actions.removeAttr('open');
                        if (action === 'mute') {
                          ChatEvents.openMemberMuteModal(
                            data.id,
                            member.id,
                            member.name,
                            !ChatConfig.user.isStaff,
                            loadDetails
                          );
                        } else {
                          openMemberActionModal(action, data.id, member, loadDetails);
                        }
                      }).appendTo($actionMenu);
                  });
                  $controls.append($actions);
                }
              }
              $list.append($item);
            });
            $noMatches.prop('hidden', members.length !== 0);
          };
          var initialMembers = data.members || [];
          renderMemberRows(initialMembers);
          memberSection.body.append($list, $noMatches);
          var $membersTruncatedHint;
          if (data.members_truncated) {
            $membersTruncatedHint = $('<p class="chat-field-hint">')
              .text(ChatConfig.i18n.firstMembersShown);
            memberSection.body.append($membersTruncatedHint);
          }
          if ($memberFilter) {
            var memberSearchTimer;
            var memberSearchRequest;
            var memberSearchSequence = 0;
            $memberFilter.on('input', function() {
              var search = $(this).val().trim().toLocaleLowerCase();
              if (data.members_truncated) {
                clearTimeout(memberSearchTimer);
                memberSearchSequence++;
                if (memberSearchRequest) memberSearchRequest.abort();
                if (!search) {
                  renderMemberRows(initialMembers);
                  $membersTruncatedHint.prop('hidden', false);
                  $memberFilter.removeAttr('aria-busy');
                  return;
                }
                var sequence = memberSearchSequence;
                $memberFilter.attr('aria-busy', 'true');
                memberSearchTimer = setTimeout(function() {
                  memberSearchRequest = ChatAPI.getRoomDetails(data.id, {
                    member_search: search
                  }).done(function(result) {
                    if (sequence !== memberSearchSequence) return;
                    renderMemberRows(result.members || []);
                    $membersTruncatedHint.prop('hidden', !result.members_truncated);
                  }).fail(function(response, status) {
                    if (status !== 'abort') showError(response);
                  }).always(function() {
                    if (sequence === memberSearchSequence) {
                      $memberFilter.removeAttr('aria-busy');
                    }
                  });
                }, 250);
                return;
              }
              var visible = 0;
              $list.children('.chat-member-row').each(function() {
                var matches = !search || $(this).attr('data-member-name').includes(search);
                $(this).prop('hidden', !matches);
                if (matches) visible++;
              });
              $noMatches.prop('hidden', visible !== 0);
            });
          }
          $content.append(memberSection.section);

          if (data.permissions.rename || data.permissions.change_avatar) {
            var settingsSection = createSection(
              'fa-cog', ChatConfig.i18n.roomSettings, 'chat-details-settings'
            );
            if (data.permissions.change_avatar) {
              var $avatarPreview = $('<div class="chat-room-avatar-preview">');
              var renderAvatarPreview = function(avatarUrl) {
                $avatarPreview.empty();
                if (avatarUrl) {
                  $avatarPreview.append(
                    $('<img alt="">').attr('src', avatarUrl)
                  );
                } else {
                  $avatarPreview.append(
                    $('<i class="fa" aria-hidden="true">').addClass(icon)
                  );
                }
              };
              renderAvatarPreview(data.avatar_url);
              var $avatarInput = $('<input type="file" accept="image/*">')
                .attr('aria-label', ChatConfig.i18n.roomAvatar);
              var $avatarError = $('<p class="chat-modal-error" aria-live="polite">');
              var $uploadAvatar = $('<button type="button" class="action-btn small">')
                .text(ChatConfig.i18n.upload)
                .on('click', function() {
                  var file = $avatarInput[0].files[0];
                  if (!file) {
                    $avatarError.text(ChatConfig.i18n.chooseRoomAvatar);
                    return;
                  }
                  $avatarError.text('');
                  $uploadAvatar
                    .prop('disabled', true)
                    .attr('aria-busy', 'true')
                    .text(ChatConfig.i18n.uploading);
                  var request = ChatAPI.changeRoomAvatar(data.id, file, false);
                  // Restore the control before rendering the response. If an
                  // image preview ever fails, the upload button must not stay
                  // disabled after the request has already completed.
                  request.always(function() {
                    $uploadAvatar
                      .prop('disabled', false)
                      .removeAttr('aria-busy')
                      .text(ChatConfig.i18n.upload);
                  });
                  request
                    .done(function(result) {
                      data.avatar_url = result.avatar_url;
                      data.has_custom_avatar = result.has_custom_avatar;
                      renderAvatarPreview(data.avatar_url);
                      ChatUI.updateRoomAvatar(data.id, data.avatar_url, data.room_type);
                      $detailsIcon.empty().append(
                        $('<img class="chat-details-room-avatar" alt="">')
                          .attr('src', data.avatar_url)
                      );
                      $removeAvatar.prop('hidden', false);
                      $avatarInput.val('');
                    }).fail(function(response) {
                      $avatarError.text(
                        response && response.responseJSON ?
                          response.responseJSON.error : ChatConfig.i18n.uploadFailed
                      );
                    });
                });
              var $removeAvatar = $('<button type="button" class="action-btn small background-gray chat-remove-room-avatar">')
                .text(ChatConfig.i18n.removeAvatar)
                .prop('hidden', !data.has_custom_avatar)
                .on('click', function() {
                  $removeAvatar.prop('disabled', true);
                  ChatAPI.changeRoomAvatar(data.id, null, true)
                    .done(function(result) {
                      data.avatar_url = result.avatar_url;
                      data.has_custom_avatar = result.has_custom_avatar;
                      renderAvatarPreview(data.avatar_url);
                      ChatUI.updateRoomAvatar(data.id, data.avatar_url, data.room_type);
                      $detailsIcon.empty();
                      if (data.avatar_url) {
                        $detailsIcon.append(
                          $('<img class="chat-details-room-avatar" alt="">')
                            .attr('src', data.avatar_url)
                        );
                      } else {
                        $detailsIcon.append(
                          $('<i class="fa" aria-hidden="true">').addClass(icon)
                        );
                      }
                      $removeAvatar.prop('hidden', true);
                    }).fail(showError)
                    .always(function() { $removeAvatar.prop('disabled', false); });
                });
              settingsSection.body.append(
                $('<label>').text(ChatConfig.i18n.roomAvatar),
                $('<div class="chat-room-avatar-editor">').append(
                  $avatarPreview,
                  $('<div class="chat-room-avatar-fields">').append(
                    $avatarInput,
                    $('<div class="chat-details-secondary-actions">').append(
                      $uploadAvatar, $removeAvatar
                    ),
                    $('<p class="chat-field-hint">').text(ChatConfig.i18n.roomAvatarHelp),
                    $avatarError
                  )
                )
              );
            }
            if (data.permissions.rename) {
              var $name = $('<input type="text" maxlength="100">')
                .val(data.name).attr('aria-label', ChatConfig.i18n.roomName);
              var $renameButton = $('<button type="button" class="action-btn small">')
                .text(ChatConfig.i18n.rename)
                .on('click', function() {
                  $renameButton.prop('disabled', true);
                  ChatAPI.roomAction(ChatConfig.urls.renameRoom, data.id, { name: $name.val() })
                    .done(function(result) {
                      data.name = result.name;
                      $('#chat-details-title').text(result.name);
                      ChatEvents.refreshStatus();
                    }).fail(showError)
                    .always(function() { $renameButton.prop('disabled', false); });
                });
              settingsSection.body.append(
                $('<label>').text(ChatConfig.i18n.roomName),
                $('<div class="chat-details-inline-form">').append($name, $renameButton)
              );
            }
            $content.append(settingsSection.section);
          }
          if (data.permissions.invite) {
            var inviteSection = createSection(
              'fa-link', ChatConfig.i18n.invitePeople, 'chat-details-invite'
            );
            inviteSection.body.append(
              $('<p class="chat-details-help">').text(ChatConfig.i18n.invitationHelp)
            );
            var $invite = $('<input type="text" readonly class="chat-invite-url">')
              .attr('aria-label', ChatConfig.i18n.invite);
            var invitationUrl = ChatAPI.roomUrl(ChatConfig.urls.invitation, data.id);
            var loadInvitation = function() {
              $.get(invitationUrl).done(function(result) {
                $invite.val(result.url || '').attr(
                  'placeholder', result.revoked ? ChatConfig.i18n.invitationRevoked : ''
                );
              }).fail(showError);
            };
            loadInvitation();
            var $copyButton = $('<button type="button" class="action-btn small">')
              .text(ChatConfig.i18n.copy)
              .on('click', function() {
                if (!$invite.val()) return;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                  navigator.clipboard.writeText($invite.val());
                } else {
                  $invite.trigger('select');
                  document.execCommand('copy');
                }
                $(this).text(ChatConfig.i18n.copied);
              });
            inviteSection.body.append(
              $('<div class="chat-details-inline-form">').append($invite, $copyButton)
            );
            var $inviteActions = $('<div class="chat-details-secondary-actions">');
            $('<button type="button" class="action-btn small background-gray">')
              .text(ChatConfig.i18n.rotateInvitation).on('click', function() {
                $.post(invitationUrl, { action: 'rotate' }).done(function(result) {
                  $invite.val(result.url || '').attr('placeholder', '');
                  $copyButton.text(ChatConfig.i18n.copy);
                }).fail(showError);
              }).appendTo($inviteActions);
            $('<button type="button" class="action-btn small background-gray">')
              .text(ChatConfig.i18n.revokeInvitation).on('click', function() {
                $.post(invitationUrl, { action: 'revoke' }).done(function() {
                  $invite.val('').attr('placeholder', ChatConfig.i18n.invitationRevoked);
                  $copyButton.text(ChatConfig.i18n.copy);
                }).fail(showError);
              }).appendTo($inviteActions);
            inviteSection.body.append($inviteActions);
            $content.append(inviteSection.section);
          }

          var $roomActions = $('<div class="chat-details-room-actions">');
          if (data.ignore_url) {
            $('<button type="button" class="action-btn background-gray">')
              .text(data.ignored ? ChatConfig.i18n.unignore : ChatConfig.i18n.ignore)
              .on('click', function() {
                ChatAPI.toggleIgnore(data.ignore_url)
                  .done(function(result) {
                    window.location.href = result.redirect || ChatConfig.urls.chat;
                  })
                  .fail(showError);
              })
              .appendTo($roomActions);
          }
          if (data.permissions.hide) {
            $('<button type="button" class="action-btn background-gray">')
              .text(ChatConfig.i18n.hideRoom).on('click', function() {
                ChatAPI.roomAction(ChatConfig.urls.roomVisibility, data.id, { hidden: '1' })
                  .done(function() { window.location.href = ChatConfig.urls.chat; })
                  .fail(showError);
              }).appendTo($roomActions);
          }
          if (data.permissions.leave) {
            $('<button type="button" class="action-btn chat-leave-room-button">')
              .text(ChatConfig.i18n.leave).on('click', function() {
                pendingLeaveRoom = {id: data.id, name: data.name};
                $('#chat-leave-room-summary').text(interpolate(
                  ChatConfig.i18n.leaveRoomSummary,
                  {room: data.name},
                  true
                ));
                openModal('#chat-leave-room-modal');
              }).appendTo($roomActions);
          }
          if (data.permissions.archive) {
            $('<button type="button" class="action-btn background-gray">')
              .text(ChatConfig.i18n.archive).on('click', function() {
                ChatAPI.roomAction(ChatConfig.urls.archiveRoom, data.id)
                  .done(function() { window.location.reload(); })
                  .fail(showError);
              }).appendTo($roomActions);
          }
          if (data.permissions.restore) {
            $('<button type="button" class="action-btn">')
              .text(ChatConfig.i18n.restore).on('click', function() {
                ChatAPI.roomAction(ChatConfig.urls.restoreRoom, data.id)
                  .done(function() { window.location.reload(); })
                  .fail(showError);
              }).appendTo($roomActions);
          }
          if ($roomActions.children().length) {
            var actionsSection = createSection(
              'fa-cog', ChatConfig.i18n.roomActions, 'chat-details-actions'
            );
            actionsSection.body.append($roomActions);
            $content.append(actionsSection.section);
          }

          if (data.permissions.view_moderation) {
            var moderationUrl = ChatAPI.roomUrl(ChatConfig.urls.moderation, data.id);
            var $moderation = $('<div class="chat-room-moderation">');
            var moderationSection = createSection(
              'fa-shield',
              ChatConfig.i18n.moderationTools,
              'chat-details-moderation'
            );
            moderationSection.body.append(
              $('<p class="chat-details-help">').text(ChatConfig.i18n.moderationHelp),
              $moderation
            );
            $content.append(moderationSection.section);
            var renderModeration = function(result) {
              $moderation.empty();
              var renderModerationTarget = function(entry, tagName) {
                var $target = $('<' + tagName + '>')
                  .addClass(entry.target_css_class || '');
                if (entry.target_url) {
                  $target.append(
                    $('<a>').attr('href', entry.target_url).text(entry.target_name)
                  );
                } else {
                  $target.text(entry.target_name);
                }
                return $target;
              };
              var $mutes = $('<div class="chat-moderation-card chat-active-mutes">')
                .append($('<h4>').text(ChatConfig.i18n.activeMutes));
              if (!(result.mutes || []).length) {
                $mutes.append($('<p>').text(ChatConfig.i18n.noActiveMutes));
              }
              (result.mutes || []).forEach(function(mute) {
                var expiryText = interpolate(
                  ChatConfig.i18n.mutedUntil,
                  {time: moment(mute.expires_at).format('lll')},
                  true
                );
                var $details = $('<div class="chat-active-mute-details">').append(
                  renderModerationTarget(mute, 'strong')
                    .addClass('chat-active-mute-target'),
                  $('<span class="chat-active-mute-expiry">').text(expiryText)
                );
                if (mute.reason) {
                  $details.append(
                    $('<span class="chat-active-mute-reason">')
                      .text(interpolate(
                        ChatConfig.i18n.muteReasonDetail,
                        {reason: mute.reason},
                        true
                      ))
                      .attr('title', mute.reason)
                  );
                }
                var $row = $('<div class="chat-active-mute">').append($details);
                $('<button type="button" class="action-btn small chat-unmute-button">')
                  .text(ChatConfig.i18n.unmute).on('click', function() {
                    $.post(ChatAPI.roomUrl(ChatConfig.urls.moderation, data.id), {
                      mute_id: mute.id
                    }).done(function() { $row.remove(); });
                  }).appendTo($row);
                $mutes.append($row);
              });
              $moderation.append($mutes);

              var $bans = $('<div class="chat-moderation-card chat-room-bans">')
                .append($('<h4>').text(ChatConfig.i18n.bans));
              if (!(result.bans || []).length) {
                $bans.append($('<p>').text(ChatConfig.i18n.noBans));
              }
              (result.bans || []).forEach(function(ban) {
                var $banText = $('<span>').append(
                  renderModerationTarget(ban, 'strong')
                );
                if (ban.reason) {
                  $banText.append($('<span>').text(' — ' + ban.reason));
                }
                var $row = $('<div class="chat-managed-row">').append(
                  $banText
                );
                if (data.permissions.manage) {
                  $('<button type="button" class="action-btn small">')
                    .text(ChatConfig.i18n.unban).on('click', function() {
                      ChatAPI.roomAction(ChatConfig.urls.memberAction, data.id, {
                        action: 'unban', user_id: ban.target_id
                      }).done(loadModeration);
                    }).appendTo($row);
                }
                $bans.append($row);
              });
              $moderation.append($bans);
            };
            var loadModeration = function() {
              $moderation.html(
                $('<span class="chat-details-loader">').append(
                  $('<i class="fa fa-spinner fa-pulse" aria-hidden="true">')
                )
              );
              $.get(moderationUrl).done(function(result) {
                renderModeration(result);
              }).fail(showError);
            };
            moderationSection.section.one('toggle', function() {
              if (this.open) loadModeration();
            });
          }
        };
        var loadDetails = function() {
          $error.text('');
          return ChatAPI.getRoomDetails(ChatState.roomId)
            .done(renderDetails).fail(showError);
        };
        loadDetails();
      });
      $(document).on('click', '.chat-room-filter', function() {
        var filter = $(this).attr('data-room-filter') || 'all';
        if (filter === ChatState.roomFilter) return;
        ChatState.roomFilter = filter;
        $('.chat-room-filter')
          .removeClass('is-active').attr('aria-pressed', 'false');
        $(this).addClass('is-active').attr('aria-pressed', 'true');
        ChatEvents.refreshStatus();
      });
      $(document).on('click', '.chat-load-more-rooms', function() {
        var $button = $(this);
        var section = $button.attr('data-room-section');
        var fetchNext = function(cursor) {
          var filters = section === 'all' ? {} : {section: section};
          ChatAPI.getRoomList(cursor, filters).done(function(data) {
            (data.rooms || []).forEach(function(room) {
              if ($('#room_row_' + room.id).length) return;
              var $list = $('.status-list[data-room-section="' + section + '"]');
              var icon = room.room_type === 'group' ? 'fa-users' :
                (room.room_type === 'channel' ? 'fa-hashtag' : 'fa-circle');
              var $row = $('<li class="click_space status-row">')
                .attr('id', 'room_row_' + room.id)
                .attr('data-room', room.id)
                .attr('data-room-url', room.url);
              if (room.room_type === 'direct' && room.avatar_url) {
                $row.attr('data-user-id', room.other_user_id || '')
                  .attr('data-is-self', room.is_self ? '1' : '0');
                var $avatar = $('<div class="status-container">').append(
                  $('<img>', {
                    'class': 'status-pic user-img',
                    loading: 'lazy',
                    src: room.avatar_url
                  })
                );
                $avatar.append(
                  $('<span class="status-circle">').addClass(room.is_online ? 'online' : 'offline')
                );
                $row.append($avatar);
              } else if (room.avatar_url) {
                $row.append(
                  $('<div class="status-container">').append(
                    $('<img>', {
                      'class': 'status-pic room-avatar',
                      loading: 'lazy',
                      src: room.avatar_url,
                      alt: ''
                    })
                  )
                );
              } else {
                $row.append(
                  $('<div class="status-container">').append(
                    $('<span class="status-room-icon">').append(
                      $('<i class="fa">').addClass(icon)
                    )
                  )
                );
              }
              var $text = $('<div class="status-user">').append(
                $('<span class="username wrapline">').text(room.name)
              );
              if (room.last_message) {
                $text.append(
                  $('<span class="status-last-message wrapline">').text(room.last_message)
                );
              }
              $row.append($text);
              if (room.unread_count) {
                $row.append($('<span class="unread-count">').text(
                  room.unread_count > 99 ? '99+' : room.unread_count
                ));
              }
              if (room.room_type === 'direct' && !room.is_self && room.ignore_url) {
                var $ignoreButton = $('<button type="button" class="red">')
                  .text(ChatConfig.i18n.ignore)
                  .on('click', function() {
                    ChatAPI.toggleIgnore(room.ignore_url)
                      .done(function(result) {
                        window.location.href = result.redirect || ChatConfig.urls.chat;
                      })
                      .fail(showError);
                  });
                $row.append(
                  $('<div class="setting-wrapper">').append(
                    $('<div class="control-button small setting-button">').append(
                      $('<i class="fa fa-ellipsis-h" aria-hidden="true">')
                    ),
                    $('<div class="setting-content">').append($ignoreButton)
                  )
                );
              }
              $list.append($row);
            });
            $button.data('cursor', data.next_cursor || '');
            $button.toggle(!!data.has_more);
            ChatWebSocket.refreshAuthorization();
          });
        };
        fetchNext($button.data('cursor') || null);
      });
      $('.chat-modal-backdrop').on('click', function(e) {
        if (e.target === this) closeModal($(this));
      });
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
        if (ChatUtils.isChatDisabled()) return;
        toggleEmoji();
      });

      $(document).on('click', function(e) {
        if (!tooltip.contains(e.target)) {
          tooltip.classList.remove('shown');
        }
      });

      $('emoji-picker').on('emoji-click', function(e) {
        if (ChatUtils.isChatDisabled()) return;
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
          var $button = $(this);
          var $content = $button.siblings('.setting-content');
          $('.setting-content').not($content).hide();
          $('.setting-button, .user-setting-button').not($button).attr('aria-expanded', 'false');
          $content.toggle();
          $button.attr('aria-expanded', $content.is(':visible') ? 'true' : 'false');
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
        $('.setting-button, .user-setting-button').attr('aria-expanded', 'false');
      });

      $(document).on('keydown', '.chat-actions-wrapper', function(e) {
        if (e.keyCode !== 27) return;
        $(this).find('.setting-content').hide();
        $(this).find('.chat-actions-toggle').attr('aria-expanded', 'false').focus();
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
          if (data.kind === 'room') {
            var roomIcon = data.room_type === 'group' ? 'fa-users' : 'fa-hashtag';
            var $roomAvatar = data.avatar_url ?
              $('<img>', {
                'class': 'user-search-image room-avatar',
                src: data.avatar_url,
                width: 24,
                height: 24,
                alt: ''
              }) :
              $('<span class="chat-search-room-icon">')
                .append($('<i>', { 'class': 'fa ' + roomIcon, 'aria-hidden': 'true' }));
            return $('<span class="chat-search-room-result">')
              .append($roomAvatar)
              .append($('<span class="user-search-name">').text(data.text));
          }
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
        var result = e.params.data;
        if (result.kind === 'room' && result.url) {
          ChatEvents.loadKnownRoom(
            String(result.id).replace('room:', ''),
            null,
            result.url
          );
          return;
        }
        var encryptedUser = result.id;
        if (!encryptedUser) return;
        ChatEvents.loadRoom(encryptedUser);
        $(this).val(null).trigger('change');
      });
    },

    refreshStatus: function(refreshChatInfo) {
      var requestToken = ++ChatState.statusLoadToken;
      ChatAPI.getOnlineStatus(ChatState.roomFilter)
        .done(function(data) {
          if (requestToken !== ChatState.statusLoadToken) return;
          if (data.status === 403) {
            console.log('Failed to retrieve online status');
            return;
          }
          ChatElements.chatOnlineList.html(data);
          ChatUI.highlightSelectedRoom();
        })
        .fail(function() {
          if (requestToken !== ChatState.statusLoadToken) return;
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
        ChatConfig.event.channels,
        ChatConfig.room.lastMsgId,
        function(message) {
          self.handleMessage(message);
        },
        ChatConfig.event.grant
      );
      setInterval(function() { self.refreshAuthorization(); }, 10 * 60 * 1000);
    },

    refreshAuthorization: function() {
      if (!this.receiver) return;
      var roomIds = [];
      $('.status-row[data-room]').each(function() {
        var roomId = String($(this).data('room') || '');
        if (roomId && roomIds.indexOf(roomId) === -1) roomIds.push(roomId);
      });
      if (roomIds.indexOf(String(ChatState.roomId)) === -1) {
        roomIds.push(String(ChatState.roomId));
      }
      var self = this;
      $.get(ChatAPI.roomUrl(ChatConfig.urls.eventGrant, ChatState.roomId), {
        room_ids: roomIds.slice(0, 63).join(',')
      }).done(function(data) {
        if (self.receiver && data.grant) {
          self.receiver.updateAuthorization(data.grant, data.channels || []);
        }
      });
    },

    handleMessage: function(message) {
      if (message.member_count !== undefined) {
        ChatUI.updateRoomMemberCount(message.room, Number(message.member_count));
      }
      if (message.type === 'room_access_revoked') {
        if (String(message.room) === ChatState.roomId) {
          window.location.href = ChatConfig.urls.chat;
        }
        return;
      }
      if (message.type === 'chat_muted') {
        ChatUI.setMutedState(true);
        return;
      }

      if (message.type === 'chat_unmuted') {
        ChatUI.restoreInteractionAfterUnmute();
        ChatUI.setMutedState(false);
        return;
      }

      if (message.type === 'room_muted' && String(message.room) === ChatState.roomId) {
        ChatConfig.user.isRoomMuted = true;
        ChatUI.applyMutedState();
        return;
      }

      if (message.type === 'room_unmuted' && String(message.room) === ChatState.roomId) {
        ChatConfig.user.isRoomMuted = false;
        ChatUI.restoreInteractionAfterUnmute();
        ChatUI.applyMutedState();
        return;
      }

      if (message.type === 'message_hidden') {
        $('#message-' + message.message).remove();
        ChatUtils.mergeConsecutiveMessages();
        return;
      }

      if (message.type === 'user_messages_hidden') {
        if (String(message.room) === ChatState.roomId) {
          $('.message[data-author="' + message.user + '"]').remove();
          ChatUtils.mergeConsecutiveMessages();
        }
        ChatEvents.refreshStatus();
        return;
      }

      if (message.type === 'room_archived') {
        ChatConfig.room.isArchived = true;
        ChatUI.applyMutedState();
        return;
      }

      if (message.type === 'room_avatar_changed') {
        ChatUI.updateRoomAvatar(
          String(message.room),
          message.avatar_url || null,
          message.room_type
        );
        return;
      }

      if (message.type === 'room_renamed') {
        $('.info-name').first().text(message.name);
        ChatEvents.refreshStatus();
        if (message.message) {
          ChatMessages.addNewMessage(message.message, String(message.room), false, message);
        }
        return;
      }

      if (!message.message) {
        ChatEvents.refreshStatus();
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

      var room = String(message.room);

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
    if (!ChatUtils.isChatDisabled() && !ChatUtils.isMobile()) {
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
