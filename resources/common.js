// IE 8
if (!Array.prototype.indexOf) {
    Array.prototype.indexOf = function (obj) {
        for (var i = 0; i < this.length; i++) {
            if (this[i] == obj) {
                return i;
            }
        }
        return -1;
    }
}

if (!String.prototype.startsWith) {
    String.prototype.startsWith = function (searchString, position) {
        return this.substr(position || 0, searchString.length) === searchString;
    };
}

if (!String.prototype.endsWith) {
    String.prototype.endsWith = function (searchString, position) {
        var subjectString = this.toString();
        if (typeof position !== 'number' || !isFinite(position) || Math.floor(position) !== position || position > subjectString.length) {
            position = subjectString.length;
        }
        position -= searchString.length;
        var lastIndex = subjectString.lastIndexOf(searchString, position);
        return lastIndex !== -1 && lastIndex === position;
    };
}


function register_toggle(link) {
    link.click(function () {
        var toggled = link.next('.toggled');
        if (toggled.is(':visible')) {
            toggled.hide(400);
            link.removeClass('open');
            link.addClass('closed');
        } else {
            toggled.show(400);
            link.addClass('open');
            link.removeClass('closed');
        }
    });
}

$(function register_all_toggles() {
    $('.toggle').each(function () {
        register_toggle($(this));
    });
});

function featureTest(property, value, noPrefixes) {
    var prop = property + ':',
        el = document.createElement('test'),
        mStyle = el.style;

    if (!noPrefixes) {
        mStyle.cssText = prop + ['-webkit-', '-moz-', '-ms-', '-o-', ''].join(value + ';' + prop) + value + ';';
    } else {
        mStyle.cssText = prop + value;
    }
    return !!mStyle[property];
}

window.fix_div = function (div, height) {
    var div_offset = div.offset().top - $('html').offset().top;
    var is_moving;
    var moving = function () {
        div.css('position', 'absolute').css('top', div_offset);
        is_moving = true;
    };
    var fix = function () {
        div.css('position', 'fixed').css('top', height);
        is_moving = false;
    };
    ($(window).scrollTop() - div_offset > -height) ? fix() : moving();
    $(window).scroll(function () {
        if (($(window).scrollTop() - div_offset > -height) == is_moving)
            is_moving ? fix() : moving();
    });
};

if (!Date.now) {
    Date.now = function () {
        return new Date().getTime();
    };
}

function count_down(label) {
    var end_time = new Date(label.attr('data-secs').replace(' ', 'T'));

    function format(num) {
        var s = "0" + num;
        return s.substr(s.length - 2);
    }

    var timer = setInterval(function () {
        var time = Math.round((end_time - Date.now()) / 1000);
        if (time <= 0) {
            clearInterval(timer);
            setTimeout(function() {
                window.location.reload();
            }, 2000);
        }
        var d = Math.floor(time / 86400);
        var h = Math.floor(time % 86400 / 3600);
        var m = Math.floor(time % 3600 / 60);
        var s = time % 60;
        if (d > 0)
            label.text(npgettext('time format with day', '%d day %h:%m:%s', '%d days %h:%m:%s', d)
                .replace('%d', d).replace('%h', format(h)).replace('%m', format(m)).replace('%s', format(s)));
        else
            label.text(pgettext('time format without day', '%h:%m:%s')
                .replace('%h', format(h)).replace('%m', format(m)).replace('%s', format(s)));
    }, 1000);
}

function register_time(elems, limit) { // in hours
    if ('limit_time' in window) limit = window.limit_time;
    else limit = limit || 300 * 24;
    elems.each(function () {
        var outdated = false;
        var $this = $(this);
        var time = moment($this.attr('data-iso'));
        var rel_format = $this.attr('data-format');
        var abs = $this.text();

        function update() {
            if ($('body').hasClass('window-hidden'))
                return outdated = true;
            outdated = false;
            if (moment().diff(time, 'hours') > limit) {
                $this.text(abs);
                return;
            }
            $this.text(rel_format.replace('{time}', time.fromNow()));
            setTimeout(update, 10000);
        }

        $(window).on('dmoj:window-visible', function () {
            if (outdated)
                update();
        });

        update();
    });
}

window.notification_template = {
    icon: '/logo.png'
};
window.notification_timeout = 5000;

window.notify = function (type, title, data, timeout) {
    if (localStorage[type + '_notification'] != 'true') return;
    var template = window[type + '_notification_template'] || window.notification_template;
    var data = (typeof data !== 'undefined' ? $.extend({}, template, data) : template);
    var object = new Notification(title, data);
    if (typeof timeout === 'undefined')
        timeout = window.notification_timeout;
    if (timeout)
        setTimeout(function () {
            object.close();
        }, timeout);
    return object;
};

window.register_notify = function (type, options) {
    if (typeof options === 'undefined')
        options = {};

    function status_change() {
        if ('change' in options)
            options.change(localStorage[key] == 'true');
    }

    var key = type + '_notification';
    if ('Notification' in window) {
        if (!(key in localStorage) || Notification.permission !== 'granted')
            localStorage[key] = 'false';

        if ('$checkbox' in options) {
            options.$checkbox.change(function () {
                var status = $(this).is(':checked');
                if (status) {
                    if (Notification.permission === 'granted') {
                        localStorage[key] = 'true';
                        notify(type, 'Notification enabled!');
                        status_change();
                    } else
                        Notification.requestPermission(function (permission) {
                            if (permission === 'granted') {
                                localStorage[key] = 'true';
                                notify(type, 'Notification enabled!');
                            } else localStorage[key] = 'false';
                            status_change();
                        });
                } else {
                    localStorage[key] = 'false';
                    status_change();
                }
            }).prop('checked', localStorage[key] == 'true');
        }

        $(window).on('storage', function (e) {
            e = e.originalEvent;
            if (e.key === key) {
                if ('$checkbox' in options)
                    options.$checkbox.prop('checked', e.newValue == 'true');
                status_change();
            }
        });
    } else {
        if ('$checkbox' in options) options.$checkbox.hide();
        localStorage[key] = 'false';
    }
    status_change();
};

window.notify_clarification = function(msg) {
    var message = `Problem ${msg.order} (${msg.problem__name}):\n` + msg.description;
    alert(message);
}

window.register_contest_notification = function(url) {
    function get_clarifications() {
        $.get(url)
            .fail(function() {
                console.log("Fail to update clarification");
            })
            .done(function(data) {
                for (i of data) {
                    window.notify_clarification(i);
                }
                if (data.status == 403) {
                    console.log("Fail to retrieve data");
                }
            })
    }
    get_clarifications();
    setInterval(get_clarifications, 60 * 1000);
}

$.fn.textWidth = function () {
    var html_org = $(this).html();
    var html_calc = '<span style="white-space: nowrap;">' + html_org + '</span>';
    $(this).html(html_calc);
    var width = $(this).find('span:first').width();
    $(this).html(html_org);
    return width;
};

function registerPopper($trigger, $dropdown) {
    const popper = Popper.createPopper($trigger[0], $dropdown[0], {
        placement: 'bottom-end',
        modifiers: [
            {
                name: 'offset',
                options: {
                    offset: [0, 8],
                },
            },
        ],
    });
    $trigger.click(function(e) {
        $dropdown.toggle();
        popper.update();
    });
    $dropdown.css("min-width", $trigger.width() + 'px');
    
    $(document).on("click touchend", function(e) {
        var target = $(e.target);
        if (target.closest($trigger).length === 0 && target.closest($dropdown).length === 0) {
            $dropdown.hide();
        }
    })
}

function onWindowReady() {
    // http://stackoverflow.com/a/1060034/1090657
    var hidden = 'hidden';

    // Standards:
    if (hidden in document)
        document.addEventListener('visibilitychange', onchange);
    else if ((hidden = 'mozHidden') in document)
        document.addEventListener('mozvisibilitychange', onchange);
    else if ((hidden = 'webkitHidden') in document)
        document.addEventListener('webkitvisibilitychange', onchange);
    else if ((hidden = 'msHidden') in document)
        document.addEventListener('msvisibilitychange', onchange);
    // IE 9 and lower:
    else if ('onfocusin' in document)
        document.onfocusin = document.onfocusout = onchange;
    // All others:
    else
        window.onpageshow = window.onpagehide
            = window.onfocus = window.onblur = onchange;

    function onchange(evt) {
        var v = 'window-visible', h = 'window-hidden', evtMap = {
            focus: v, focusin: v, pageshow: v, blur: h, focusout: h, pagehide: h
        };

        evt = evt || window.event;
        if (evt.type in evtMap)
            document.body.className = evtMap[evt.type];
        else
            document.body.className = this[hidden] ? 'window-hidden' : 'window-visible';

        if ('$' in window)
            $(window).trigger('dmoj:' + document.body.className);
    }

    $('.tabs').each(function () {
        var $this = $(this), $h2 = $(this).find('h2'), $ul = $(this).find('ul');
        var cutoff = ($h2.textWidth() || 400) + 20, handler;
        $ul.children().each(function () {
            cutoff += $(this).width();
        });
        $(window).resize(handler = function () {
            $this.toggleClass('tabs-no-flex', $this.width() < cutoff);
        });
        handler();
    });

    // set the initial state (but only if browser supports the Page Visibility API)
    if (document[hidden] !== undefined)
        onchange({type: document[hidden] ? 'blur' : 'focus'});

    $("a.close").click(function () {
        var $closer = $(this);
        $closer.parent().fadeOut(200);
    });

    register_time($('.time-with-rel'));

    if (typeof window.orientation !== 'undefined') {
        $(window).resize(function () {
            var width = Math.max(document.documentElement.clientWidth, window.innerWidth || 0);
            // $('#viewport').attr('content', width > 480 ? 'initial-scale=1' : 'width=480');
        });
    }

    $.ajaxSetup({
        beforeSend: function (xhr, settings) {
            if (!(/^(GET|HEAD|OPTIONS|TRACE)$/.test(settings.type)) && !this.crossDomain)
                xhr.setRequestHeader('X-CSRFToken', $.cookie('csrftoken'));
        }
    });

    setTimeout(() => {
        $("[data-src]img").each(function() {
            $(this).attr("src", $(this).attr("data-src"));
        })
        $("[data-src]iframe").each(function() {
            $(this).attr("src", $(this).attr("data-src"));
        })
    }, "100");

    $('form').submit(function (evt) {
        // Prevent multiple submissions of forms, see #565
        $("input[type='submit']").prop('disabled', true);
    });

    $('.lang-dropdown-item').click(function() {
        $('select[name="language"]').val($(this).attr('value'));
        $('#form-lang').submit();
    })
    $('#logout').on('click', () => $('#logout-form').submit());

    var copyButton;
    $('pre code').each(function () {
        $(this).parent().before($('<div>', {'class': 'copy-clipboard'})
                .append(copyButton = $('<span>', {
                'class': 'btn-clipboard',
                'data-clipboard-text': $(this).text(),
                'title': 'Click to copy'
            }).text('Copy')));

        $(copyButton.get(0)).mouseleave(function () {
            $(this).attr('class', 'btn-clipboard');
            $(this).removeAttr('aria-label');
        });

        var curClipboard = new Clipboard(copyButton.get(0));

        curClipboard.on('success', function (e) {
            e.clearSelection();
            showTooltip(e.trigger, 'Copied!');
        });

        curClipboard.on('error', function (e) {
            showTooltip(e.trigger, fallbackMessage(e.action));
        });
    });
    $('a').click(function() {
        var href = $(this).attr('href');
        if (!href || href === '#' || href.startsWith("javascript")) {
            return;
        }

        $("#loading-bar").show();
        $("#loading-bar").animate({ width: "100%" }, 2000, function() {
            $(this).hide().css({ width: 0});
        });    
    });
}

$(function() {
    onWindowReady();
    registerPopper($('#nav-lang-icon'), $('#lang-dropdown'));
    registerPopper($('#user-links'), $('#userlink_dropdown'));
    var $nav_list = $('#nav-list');
    $('#navicon').click(function (event) {
        event.stopPropagation();
        $nav_list.toggle();
        if ($nav_list.is(':hidden'))
            $(this).blur().removeClass('hover');
        else {
            $(this).addClass('hover');
            $nav_list.find('li ul').css('left', $('#nav-list').width()).hide();
        }
    }).hover(function () {
        $(this).addClass('hover');
    }, function () {
        $(this).removeClass('hover');
    });

    $nav_list.find('li a .nav-expand').click(function (event) {
        event.preventDefault();
        $(this).parent().siblings('ul').css('display', 'block');
    });

    $nav_list.find('li a').each(function () {
        if (!$(this).siblings('ul').length)
            return;
        $(this).on('contextmenu', function (event) {
            event.preventDefault();
        }).on('taphold', function () {
            $(this).siblings('ul').css('display', 'block');
        });
    });

    $nav_list.click(function (event) {
        event.stopPropagation();
    });

    $('html').click(function () {
        $nav_list.hide();
    });

});