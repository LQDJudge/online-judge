(function ($) {
  if (window.BLOG_COMPOSER_INITIALIZED) return;
  window.BLOG_COMPOSER_INITIALIZED = true;

  $(function () {
    var config = window.BLOG_COMPOSER || {};
    var $feedback = $('#composer-feedback');
    var $send = $('#composer-send');
    var $proposal = $('#composer-proposal');
    var $messages = $('#composer-messages');
    var $status = $('#composer-status');

    function markdownInput() {
      return $('#wmd-input-composer');
    }

    function renderMath(container, attempts) {
      attempts = attempts || 0;
      if (!container) return;
      if (typeof window.renderKatex === 'function' && window.katex) {
        window.renderKatex(container);
        return;
      }
      if (attempts < 10) {
        window.setTimeout(function () {
          renderMath(container, attempts + 1);
        }, 100);
      }
    }

    function initializeMarkdownEditor() {
      var wrapper = document.getElementById('composer-markdown-wrapper');
      if (!wrapper || !window.DjangoPagedown) return;
      DjangoPagedown.createEditor(wrapper);

      var input = markdownInput()[0];
      var preview = document.getElementById('wmd-preview-composer');
      if (!input || !preview) return;
      input.addEventListener('input', function () {
        window.setTimeout(function () {
          renderMath(preview);
        }, 0);
      });
      renderMath(preview);
    }

    function addMessage(role, content) {
      $('<article>', {'class': 'composer-message ' + role}).text(content).appendTo($messages);
      $messages.scrollTop($messages.prop('scrollHeight'));
    }

    function showPreview(name) {
      $('.composer-preview-tab').removeClass('active');
      $('.composer-preview-tab[data-composer-preview="' + name + '"]').addClass('active');
      $('#composer-rendered').prop('hidden', name !== 'rendered');
      $('#composer-markdown-wrapper').prop('hidden', name !== 'markdown');
    }

    function renderProposal(proposal, renderedContent) {
      var previousWrapper = document.getElementById('composer-markdown-wrapper');
      if (previousWrapper && window.DjangoPagedown) DjangoPagedown.destroyEditor(previousWrapper);
      $proposal.html(
        '<input type="hidden" id="proposal-id">' +
        '<h3 id="proposal-title"></h3><p id="proposal-summary"></p>' +
        '<div class="composer-preview-tabs" role="tablist">' +
        '<button class="composer-preview-tab active" type="button" data-composer-preview="rendered"></button>' +
        '<button class="composer-preview-tab" type="button" data-composer-preview="markdown"></button>' +
        '</div><article class="composer-rendered" id="composer-rendered"></article>' +
        '<div class="wmd-wrapper composer-markdown" id="composer-markdown-wrapper" hidden>' +
        '<div class="wmd-panel"><div id="wmd-button-bar-composer"></div>' +
        '<textarea class="wmd-input" id="wmd-input-composer"></textarea></div></div>' +
        '<button id="composer-approve" type="button" class="action-btn"></button>'
      );
      $('#proposal-id').val(proposal.id);
      $('#proposal-title').text(proposal.title);
      $('#proposal-summary').text(proposal.summary);
      $('#composer-rendered').html(renderedContent);
      renderMath(document.getElementById('composer-rendered'));
      markdownInput().val(proposal.content);
      initializeMarkdownEditor();
      $('.composer-preview-tab[data-composer-preview="rendered"]').text(config.renderedLabel);
      $('.composer-preview-tab[data-composer-preview="markdown"]').text(config.markdownLabel);
      $('#composer-approve').text(config.approveLabel);
    }

    function requestRenderedMarkdown(proposalId, content, done) {
      $.post(config.previewUrl, {
        csrfmiddlewaretoken: config.csrfToken,
        post_id: config.postId,
        proposal_id: proposalId,
        content: content
      }).done(function (data) {
        done(data.rendered_content);
      }).fail(function (xhr) {
        alert(xhr.responseJSON && xhr.responseJSON.error || config.requestFailed);
      });
    }

    function previewProposal(proposal, done) {
      requestRenderedMarkdown(proposal.id, proposal.content, function (renderedContent) {
        renderProposal(proposal, renderedContent);
        done();
      });
    }

    function request(url, data, success) {
      $.post(url, $.extend({csrfmiddlewaretoken: config.csrfToken}, data))
        .done(success)
        .fail(function (xhr) {
          $send.prop('disabled', false);
          alert(xhr.responseJSON && xhr.responseJSON.error || config.requestFailed);
        });
    }

    function poll(taskId) {
      if (window.BLOG_COMPOSER_POLL_TIMER) {
        clearInterval(window.BLOG_COMPOSER_POLL_TIMER);
      }
      window.BLOG_COMPOSER_POLL_TIMER = setInterval(function () {
        $.get(config.taskStatusUrl, {id: taskId}, function (data) {
          if (data.code === 'WORKING') return;
          if (data.code === 'PROGRESS') {
            $status.text(data.stage);
            return;
          }
          clearInterval(window.BLOG_COMPOSER_POLL_TIMER);
          window.BLOG_COMPOSER_POLL_TIMER = null;
          $send.prop('disabled', false);
          if (data.code !== 'SUCCESS' || !data.success) {
            alert(data.error || config.generationFailed);
            return;
          }
          previewProposal(data.proposal, function () {
            addMessage('assistant', data.message || config.approveLabel);
            $feedback.val('');
            $status.text('');
          });
        });
      }, 1000);
    }

    $send.on('click', function () {
      var feedback = $feedback.val().trim();
      if (!feedback) return;
      $send.prop('disabled', true);
      $status.text(config.startingLabel);
      request(config.sendUrl, {
        post_id: config.postId,
        feedback: feedback,
        organization_id: config.organizationId || $('#composer-organization').val(),
        initial_title: $('#composer-title').val(),
        author_username: $('#composer-author').val()
      }, function (data) {
        addMessage('user', feedback);
        poll(data.task_id);
      });
    });

    $(document).on('click', '#composer-approve', function () {
      if (!window.confirm(config.approveConfirm)) return;
      request(config.approveUrl, {
        post_id: config.postId,
        proposal_id: $('#proposal-id').val(),
        organization_id: config.organizationId || $('#composer-organization').val(),
        author_username: $('#composer-author').val(),
        content: markdownInput().val()
      }, function (data) { window.location.href = data.url; });
    });

    $(document).on('click', '.composer-preview-tab', function () {
      var preview = $(this).data('composer-preview');
      if (preview !== 'rendered') {
        showPreview(preview);
        return;
      }
      requestRenderedMarkdown(
        $('#proposal-id').val(),
        markdownInput().val(),
        function (renderedContent) {
        $('#composer-rendered').html(renderedContent);
        renderMath(document.getElementById('composer-rendered'));
        showPreview('rendered');
        }
      );
    });

    $('#composer-clear').on('click', function () {
      request(config.clearUrl, {post_id: config.postId}, function () { window.location.reload(); });
    });

    initializeMarkdownEditor();
    renderMath(document.getElementById('composer-rendered'));
  });
})(jQuery);
