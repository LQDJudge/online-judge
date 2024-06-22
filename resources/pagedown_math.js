function latex_pagedown($) {
    $.each(window.editors, function (id, editor) {
        var preview = $('div.wmd-preview#' + id + '_wmd_preview')[0];
        editor.hooks.chain('onPreviewRefresh', function () {
            renderKatex(preview);
        });
        renderKatex(preview);
    });
}

window.latex_pagedown = latex_pagedown;

$(function () {
    (latex_pagedown)('$' in window ? $ : django.jQuery);
});