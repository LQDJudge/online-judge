window.renderKatex = (elem=document.body) => {
  var maths = document.querySelectorAll('.arithmatex'),
          tex;
  for (var i = 0; i < maths.length; i++) {
    tex = maths[i].textContent || maths[i].innerText;
    if (tex.startsWith('\\(') && tex.endsWith('\\)')) {
      katex.render(tex.slice(2, -2), maths[i], {
        'displayMode': false,
        'throwOnError': false,
        'strict': false,
      });
    } else if (tex.startsWith('\\[') && tex.endsWith('\\]')) {
      katex.render(tex.slice(2, -2), maths[i], {
        'displayMode': true,
        'throwOnError': false,
        'strict': false,
      });
    }
  }
}