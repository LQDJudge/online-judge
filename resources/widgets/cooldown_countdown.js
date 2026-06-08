/* Cooldown countdown widget.
 *
 * Used by Request Public buttons on problem edit (templates/problem/edit.html)
 * and contest edit (templates/contest/edit.html). The server returns a
 * translated message containing a single MM:SS substring (e.g. "Vui lòng chờ
 * 04:12 trước khi yêu cầu xem xét lại."). We patch THAT substring once per
 * second so the translation stays correct without the client knowing the
 * gettext template.
 *
 * The MM:SS contract is enforced server-side by `judge/utils/timefmt.py
 * format_mmss` — keep these two files in sync if you change the width.
 *
 * Accepts raw DOM elements (not jQuery wrappers) so both vanilla and jQuery
 * callers can use it via `$el.get(0)`.
 */
(function () {
  var MMSS_RE = /\d{2}:\d{2}/;

  function formatMMSS(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  // Each call returns a "stopper": invoke it to cancel early. Multiple
  // concurrent timers per button would race; callers that may re-start the
  // countdown should hold onto the stopper and call it before re-starting.
  function start(statusEl, btnEl, remainingSeconds, serverMessage) {
    var ends = Date.now() + remainingSeconds * 1000;
    var timer = null;

    function render() {
      var left = Math.ceil((ends - Date.now()) / 1000);
      if (left <= 0) {
        clearInterval(timer);
        timer = null;
        if (statusEl) {
          statusEl.textContent = '';
          // Drop the visual-warning class set by the active state below.
          statusEl.classList.remove('cooldown-countdown-active');
        }
        if (btnEl) btnEl.disabled = false;
        return;
      }
      if (statusEl) {
        statusEl.textContent = serverMessage.replace(MMSS_RE, formatMMSS(left));
        // Color comes from CSS so dark mode can override. See
        // resources/review_list.scss / .red utility class.
        statusEl.classList.add('cooldown-countdown-active');
      }
    }

    render();
    timer = setInterval(render, 1000);

    return function stop() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
  }

  window.CooldownCountdown = {
    start: start,
    formatMMSS: formatMMSS,
  };
})();
