$(function () {
    window.register_dmmd_preview = function ($preview) {
        var $form = $preview.parents('form').first();
        var $update = $preview.find('.dmmd-preview-update');
        var $content = $preview.find('.dmmd-preview-content');
        var preview_url = $preview.attr('data-preview-url');
        var $textarea = $('#' + $preview.attr('data-textarea-id'));

        // Submit the form if Ctrl+Enter is pressed in pagedown textarea.
        $textarea.keydown(function (ev) {
            // Ctrl+Enter pressed (metaKey used to support command key on mac).
            if ((ev.metaKey || ev.ctrlKey) && ev.which == 13) {
                $form.submit();
            }
        });

        $update.click(function () {
            var text = $textarea.val();
            if (text) {
                $preview.addClass('dmmd-preview-stale');
                $.post(preview_url, {
                    preview: text,
                    csrfmiddlewaretoken: $.cookie('csrftoken')
                }, function (result) {
                    $content.html(result);
                    $preview.addClass('dmmd-preview-has-content').removeClass('dmmd-preview-stale');
                    renderKatex($content[0]);
                });
            } else {
                $content.empty();
                $preview.removeClass('dmmd-preview-has-content').removeClass('dmmd-preview-stale');
            }
        }).click();

        var timeout = $preview.attr('data-timeout');
        var last_event = null;
        var last_text = $textarea.val();
        if (timeout) {
            $textarea.on('keyup paste', function () {
                var text = $textarea.val();
                if (last_text == text) return;
                last_text = text;

                $preview.addClass('dmmd-preview-stale');
                if (last_event)
                    clearTimeout(last_event);
                last_event = setTimeout(function () {
                    $update.click();
                    last_event = null;
                }, timeout);
            });
        }
    };

    $('.dmmd-preview').each(function () {
        register_dmmd_preview($(this));
    });

    if ('django' in window && 'jQuery' in window.django)
        django.jQuery(document).on('formset:added', function(event) {
            var $row = $(event.target);
            var $preview = $row.find('.dmmd-preview');
            if ($preview.length) {
                var id = $row.attr('id');
                id = id.substr(id.lastIndexOf('-') + 1);
                $preview.attr('data-textarea-id', $preview.attr('data-textarea-id').replace('__prefix__', id));
                register_dmmd_preview($preview);
            }
        });
});
