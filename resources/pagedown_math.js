function mathjax_pagedown($) {
    $.each(window.editors, function (id, editor) {
        console.log(id);
        var preview = $('div.wmd-preview#' + id + '_wmd_preview')[0];
        editor.hooks.chain('onPreviewRefresh', function () {
            renderKatex(preview);
        });
        renderKatex(preview);
    });
}

window.mathjax_pagedown = mathjax_pagedown;

$(function () {
    (mathjax_pagedown)('$' in window ? $ : django.jQuery);
});