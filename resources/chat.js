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
    loader: null,

    init: function() {
      this.chatBox = $('#chat-box');
      this.chatLog = $('#chat-log');
      this.chatInput = $('#chat-input');
      this.chatInfo = $('#chat-info');
      this.chatOnlineList = $('#chat-online-list');
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

    postProcessMessages: function() {
      register_time($('.time-with-rel'));
      if (typeof renderKatex === 'function') {
        renderKatex();
      }
      this.mergeConsecutiveMessages();
    },

    mergeConsecutiveMessages: function() {
      var lastAuthorId = null;

      $('#chat-log .message').each(function() {
        var $message = $(this);
        var authorId = $message.attr('data-author');

        if (authorId === lastAuthorId) {
          // Same author as previous - add 'grouped' class to hide avatar/header
          $message.addClass('grouped');
        } else {
          // New author - add 'group-start' class for spacing
          $message.addClass('group-start');
          lastAuthorId = authorId;
        }
      });
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

    muteMessage: function(messageId) {
      return $.ajax({
        url: ChatConfig.urls.muteMessage,
        type: 'post',
        data: { message: messageId },
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
    scrollToBottom: function() {
      ChatElements.chatBox.scrollTop(ChatElements.chatBox[0].scrollHeight);
    },

    getScrollTopOfBottom: function() {
      return ChatElements.chatBox[0].scrollHeight - ChatElements.chatBox.innerHeight();
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
      $('#room-' + ChatState.otherUserId).addClass('selected');
    },

    addMessage: function(html) {
      ChatElements.chatLog.append(html);
      this.scrollToBottom();
      ChatUtils.postProcessMessages();
    },

    prependMessages: function(html) {
      var scrollTopBefore = this.getScrollTopOfBottom();
      ChatElements.chatLog.prepend(html);
      ChatUtils.postProcessMessages();
      var scrollTopAfter = this.getScrollTopOfBottom();
      ChatElements.chatBox.scrollTop(scrollTopAfter - scrollTopBefore);
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
      ChatUI.addMessage($html[0].outerHTML);
    },

    submit: function() {
      if (ChatConfig.user.isMuted || !ChatConfig.room.lastMsgId) return;

      var body = ChatElements.chatInput.val().trim();
      if (!body) return;

      var tmpId = Date.now();

      ChatElements.chatInput.val('');
      ChatElements.chatInput.css('height', '');
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
      if (refreshHtml) {
        ChatState.isLocked = true;
        ChatElements.chatLog.html('');
        ChatUI.showLoader();
      }

      // Save scroll position before loading - use old approach
      var scrollHeightBefore = ChatElements.chatBox[0].scrollHeight;

      ChatAPI.loadMessages(lastId)
        .done(function(data) {
          $('.has_next').remove();
          ChatUI.hideLoader();

          if (refreshHtml) {
            ChatElements.chatLog.append(data);
            ChatUtils.postProcessMessages();
            ChatUI.scrollToBottom();
          } else {
            // Prepend older messages
            ChatElements.chatLog.prepend(data);
            ChatUtils.postProcessMessages();

            // Restore scroll position - keep view on same content
            var scrollHeightAfter = ChatElements.chatBox[0].scrollHeight;
            var scrollDiff = scrollHeightAfter - scrollHeightBefore;
            ChatElements.chatBox.scrollTop(scrollDiff);
          }

          ChatState.isLocked = false;
          ChatState.hasNext = parseInt($('.has_next').attr('value')) || 0;
        })
        .fail(function() {
          console.log('Failed to load messages');
          ChatState.isLocked = false;
        });
    },

    addNewMessage: function(messageId, room, isSelfAuthor, wsMessage) {
      var isCurrentRoom = room === ChatState.roomId;

      // Sender is online since they just sent a message
      if (wsMessage && wsMessage.author_id) {
        ChatUI.setUserOnline(wsMessage.author_id);
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
              // Update last message preview in sidebar with actual text
              if (wsMessage && wsMessage.room) {
                var $msg = $(data);
                var msgText = $msg.find('.message-text').text().trim();
                if (msgText.length > 50) {
                  msgText = msgText.substring(0, 50) + '...';
                }
                ChatUI.setLastMessagePreview(wsMessage.room, msgText || ChatConfig.i18n.newMessage);
              }
            }
          })
          .fail(function() {
            console.log('Could not add new message');
          });
      } else {
        // Message is for a different room - update sidebar
        if (!document.hidden) {
          if (wsMessage && wsMessage.other_user_id) {
            if (wsMessage.unread_count !== undefined) {
              ChatUI.setUnreadBadge(wsMessage.other_user_id, wsMessage.unread_count);
            }
            if (wsMessage.room) {
              ChatUI.setLastMessagePreview(wsMessage.room, ChatConfig.i18n.newMessage);
            }
            ChatUI.moveConversationToTop(wsMessage.other_user_id);
          }
        } else if (!isSelfAuthor) {
          ChatState.unreadCount++;
          document.title = '(' + ChatState.unreadCount + ') ' + ChatConfig.i18n.newMessages;
        }
      }
    },

    checkNewMessage: function(messageId, tmpId, room) {
      if (room !== ChatState.roomId) return;

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
          ChatUI.updateUnreadBadge(ChatState.otherUserId, true);
          ChatUtils.postProcessMessages();
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
    init: function() {
      this.bindMessageInput();
      this.bindScrollLoad();
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
        if (this.scrollHeight > this.clientHeight) {
          this.style.height = this.scrollHeight + 'px';
          $(this).css('border-radius', '30px');
        } else {
          $(this).css('height', '');
        }
      });

      ChatElements.chatInput.on('keyup', function() {
        $(this).scrollTop(this.scrollHeight);
      });
    },

    bindScrollLoad: function() {
      ChatElements.chatBox.on('scroll', function() {
        // Trigger load when at top
        if (ChatElements.chatBox.scrollTop() === 0 &&
            !ChatState.isLocked &&
            ChatState.hasNext) {
          ChatState.isLocked = true;
          ChatUI.showLoader();
          var messageIds = $('.message').map(function() {
            return parseInt($(this).attr('data-id'));
          }).get();
          if (messageIds.length) {
            ChatMessages.loadNextPage(Math.min.apply(null, messageIds));
          }
        }
      });
    },

    bindMessageActions: function() {
      $(document).on('click', '.chat_remove', function() {
        var $this = $(this);
        var messageId = $this.attr('value');

        ChatAPI.deleteMessage(messageId)
          .done(function() {
            var $block = $this.parent();
            if ($block.parent().find('.message-block').length > 1) {
              $block.remove();
            } else {
              $this.closest('li').remove();
            }
          })
          .fail(function() {
            console.log('Failed to delete');
          });
      });

      if (ChatConfig.user.isStaff) {
        $(document).on('click', '.chat_mute', function() {
          if (confirm(ChatConfig.i18n.muteConfirm)) {
            var messageId = $(this).attr('value');
            ChatAPI.muteMessage(messageId)
              .done(function() {
                window.location.reload();
              })
              .fail(function() {
                console.log('Failed to mute');
              });
          }
        });
      }
    },

    bindRoomSelection: function() {
      $(document).on('click', '.click_space', function() {
        var clickedUserId = $(this).attr('id').replace('click_space_', '');
        if (clickedUserId === ChatState.otherUserId) {
          ChatUI.showRightPanel();
          return;
        }
        var encryptedUser = $(this).attr('value');
        ChatEvents.loadRoom(encryptedUser);
      });

      $(document).on('click', '#lobby_row', function() {
        if (ChatState.roomId) {
          ChatEvents.loadRoom(null);
        } else {
          ChatUI.showRightPanel();
        }
      });

      // Back button for mobile
      $(document).on('click', '.back-button', function() {
        ChatUI.hideRightPanel();
      });
    },

    loadRoom: function(encryptedUser) {
      if (ChatState.lockClickSpace) return;
      ChatState.lockClickSpace = true;

      var onRoomReady = function() {
        history.replaceState(null, '', ChatConfig.urls.chat + ChatState.roomId);
        ChatMessages.loadNextPage(null, true);
        ChatAPI.updateLastSeen(ChatState.roomId);
        ChatEvents.refreshStatus(true);
        ChatUI.showRightPanel();
        ChatElements.chatInput.focus();
        ChatElements.chatInput.val('').trigger('input');
      };

      if (encryptedUser) {
        ChatAPI.getOrCreateRoom(encryptedUser)
          .done(function(data) {
            ChatState.roomId = data.room;
            ChatState.otherUserId = data.other_user_id;
            ChatUI.highlightSelectedRoom();
            ChatElements.chatInput.attr('maxlength', 5000);
            onRoomReady();
          })
          .fail(function() {
            console.log('Failed to get_or_create_room');
          })
          .always(function() {
            ChatState.lockClickSpace = false;
          });
      } else {
        ChatState.roomId = '';
        ChatState.otherUserId = '';
        ChatUI.highlightSelectedRoom();
        ChatElements.chatInput.attr('maxlength', 200);
        onRoomReady();
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
        toggleEmoji();
      });

      $(document).on('click', function(e) {
        if (!tooltip.contains(e.target)) {
          tooltip.classList.remove('shown');
        }
      });

      $('emoji-picker').on('emoji-click', function(e) {
        var chatInput = ChatElements.chatInput.get(0);
        ChatUtils.insertAtCursor(chatInput, e.detail.unicode);
        chatInput.focus();
      });

      $(document).on('keydown', function(e) {
        if (e.keyCode === 27 && tooltip.classList.contains('shown')) {
          toggleEmoji();
        }
      });
    },

    bindVisibilityChange: function() {
      if (typeof MutationObserver === 'undefined') return;

      var observer = new MutationObserver(function() {
        if (!document.hidden && ChatState.unreadCount > 0) {
          ChatAPI.updateLastSeen(ChatState.roomId);
          ChatEvents.refreshStatus();
          ChatState.unreadCount = 0;
          document.title = ChatConfig.i18n.chatBox;
        }
      });

      observer.observe(document.body, {
        attributes: true,
        attributeFilter: ['class'],
        childList: false,
        characterData: false
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
        name: 'other',
        onchange: 'form.submit()'
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
        ChatElements.chatInfo.html('');
      }

      ChatAPI.getUserOnlineStatus(ChatState.otherUserId)
        .done(function(data) {
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
    ChatUI.scrollToBottom();
    ChatUI.highlightSelectedRoom();
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

    ChatElements.chatInput.focus();

    // Show chat log
    ChatElements.chatLog.show();
  }

  // Export for global access if needed
  window.ChatApp = {
    State: ChatState,
    API: ChatAPI,
    UI: ChatUI,
    Utils: ChatUtils,
    Messages: ChatMessages,
    Events: ChatEvents,
    WebSocket: ChatWebSocket,
    init: initChat
  };

  // Initialize on document ready
  $(initChat);

})(jQuery);
