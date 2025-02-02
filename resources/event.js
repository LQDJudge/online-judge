function EventReceiver(websocket, poller, channels, last_msg, onmessage) {
    this.websocket_path = websocket;
    this.channels = channels;
    this.last_msg = last_msg;
    this.poller_base = poller;
    this.poller_path = poller + channels.join('|');
    if (onmessage)
        this.onmessage = onmessage;
    var receiver = this;
    var time_retry = 1000;
    var isPageHidden = false;
    var currentWebSocket = null;

    function init_poll() {
        function long_poll() {
            if (isPageHidden) return;

            $.ajax({
                url: receiver.poller_path,
                data: { last: receiver.last_msg },
                success: function (data, status, jqXHR) {
                    receiver.onmessage(data.message);
                    receiver.last_msg = data.id;
                    long_poll();
                },
                error: function (jqXHR, status, error) {
                    if (jqXHR.status == 504)
                        long_poll();
                    else {
                        console.log('Long poll failure: ' + status);
                        console.log(jqXHR);
                        setTimeout(long_poll, 2000);
                    }
                },
                dataType: "json"
            });
        }
        long_poll();
    }

    this.onwsclose = null;

    function disconnect() {
        if (currentWebSocket) {
            currentWebSocket.onclose = null; // Prevent triggering reconnect logic during clean-up
            currentWebSocket.close(1000);
            currentWebSocket = null;
        }
    }

    function connect() {
        if (isPageHidden || currentWebSocket) return; // Skip if page is hidden or a connection already exists

        if (!window.WebSocket) {
            init_poll();
            return;
        }

        currentWebSocket = new WebSocket(websocket);
        var timeout = setTimeout(function () {
            if (currentWebSocket) {
                currentWebSocket.close();
                currentWebSocket = null;
                init_poll();
            }
        }, 2000);

        currentWebSocket.onopen = function (event) {
            clearTimeout(timeout);
            this.send(JSON.stringify({
                command: 'start-msg',
                start: last_msg
            }));
            this.send(JSON.stringify({
                command: 'set-filter',
                filter: channels
            }));
        };

        currentWebSocket.onmessage = function (event) {
            var data = JSON.parse(event.data);
            receiver.onmessage(data.message);
            receiver.last_msg = data.id;
        };

        currentWebSocket.onclose = function (event) {
            currentWebSocket = null; // Clear reference on close
            if (event.code != 1000 && receiver.onwsclose !== null)
                receiver.onwsclose(event);
            if (event.code == 1006 && !isPageHidden) {
                setTimeout(connect, time_retry);
                time_retry += 2000;
            }
        };
    }

    window.addEventListener('pagehide', function () {
        isPageHidden = true;
        disconnect();
    });

    window.addEventListener('pageshow', function () {
        if (!isPageHidden) return; // Ensure pageshow logic doesn't trigger multiple times
        isPageHidden = false;
        time_retry = 1000;

        // Add a small timeout before reconnecting
        setTimeout(function () {
            connect();
        }, 500);
    });

    if (window.WebSocket) {
        connect();
    } else {
        this.websocket = null;
        init_poll();
    }
}