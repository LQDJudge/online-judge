var WebSocketServer = require('ws').Server;
var set = require('simplesets').Set;
var queue = require('qu');
var amqp = require('amqp');
var url = require('url');

if (typeof String.prototype.startsWith != 'function') {
    String.prototype.startsWith = function (str){
        return this.slice(0, str.length) == str;
    };
}

const argv = require('yargs')
    .demandCommand(3)
    .strict()
    .usage('Usage: event [options] <amqp url> <exchange> <port>')
    .options({
        host: {
            default: '127.0.0.1',
            describe: 'websocket address to listen on'
        },
        http_host: {
            default: '127.0.0.1',
            describe: 'http address to listen on'
        },
        http_port: {
            default: null,
            describe: 'http port to listen on'
        },
        max_queue: {
            default: 10,
            describe: 'queue buffer size'
        },
        comet_timeout: {
            default: 60000,
            describe: 'comet long poll timeout'
        }
    })
    .argv;

var followers = new set();
var pollers = new set();
var messages = new queue();
var max_queue = argv.max_queue;
var comet_timeout = argv.comet_timeout;

var rabbitmq = amqp.createConnection({url: argv._[0]});

rabbitmq.on('error', function(e) {
  console.log('amqp connection error...', e);
  process.exit(1);
});

rabbitmq.on('ready', function () {
    rabbitmq.queue('', {exclusive: true}, function (q) {
        q.bind(argv._[1], '#');
        q.subscribe(function (data) {
            message = JSON.parse(data.data.toString('utf8'));
            messages.push(message);
            if (messages.length > max_queue)
                messages.shift();
            followers.each(function (client) {
                client.got_message(message);
            });
            pollers.each(function (request) {
                request.got_message(message);
            });
        });
    });
});

var wss = new WebSocketServer({host: argv.host, port: parseInt(argv._[2])});

messages.catch_up = function (client) {
    this.each(function (message) {
        if (message.id > client.last_msg)
            client.got_message(message);
    });
};

wss.on('connection', function (socket) {
    socket.channel = null;
    socket.last_msg = 0;

    var commands = {
        start_msg: function (request) {
            socket.last_msg = request.start;
        },
        set_filter: function (request) {
            var filter = {};
            if (Array.isArray(request.filter) && request.filter.length > 0 &&
                request.filter.every(function (channel, index, array) {
                    if (typeof channel != 'string')
                        return false;
                    filter[channel] = true;
                    return true;
            })) {
                socket.filter = filter;
                followers.add(socket);
                messages.catch_up(socket);
            } else {
                socket.send(JSON.stringify({
                    status: 'error',
                    code: 'invalid-filter',
                    message: 'invalid filter: ' + request.filter
                }));
            }
        }
    };

    socket.got_message = function (message) {
        if (message.channel in socket.filter)
            socket.send(JSON.stringify(message));
        socket.last_msg = message.id;
    };

    socket.on('message', function (request) {
        try {
            request = JSON.parse(request);
            if (typeof request.command !== 'string')
                throw {message: 'no command specified'};
        } catch (err) {
            socket.send(JSON.stringify({
                status: 'error',
                code: 'syntax-error',
                message: err.message
            }));
            return;
        }
        request.command = request.command.replace(/-/g, '_');
        if (request.command in commands)
            commands[request.command](request);
        else
            socket.send(JSON.stringify({
                status: 'error',
                code: 'bad-command',
                message: 'bad command: ' + request.command
            }));
    });

    socket.on('close', function(code, message) {
        followers.remove(socket);
    });
});

if (argv.http_port !== null) {
    require('http').createServer(function (req, res) {
        var parts = url.parse(req.url, true);

        if (!parts.pathname.startsWith('/channels/')) {
            res.writeHead(404, {'Content-Type': 'text/plain'});
            res.end('404 Not Found');
            return;
        }

        var channels = parts.pathname.slice(10).split('|');
        if (channels.length == 1 && !channels[0].length) {
            res.writeHead(400, {'Content-Type': 'text/plain'});
            res.end('400 Bad Request');
            return;
        }

        req.channels = {};
        req.last_msg = parseInt(parts.query.last);
        if (isNaN(req.last_msg)) req.last_msg = 0;

        channels.forEach(function (channel) {
            req.channels[channel] = true;
        });

        req.on('close', function () {
            pollers.remove(req);
        });

        req.got_message = function (message) {
            if (message.channel in req.channels) {
                res.writeHead(200, {'Content-Type': 'application/json'});
                res.end(JSON.stringify(message));
                pollers.remove(req);
                return true;
            }
            return false;
        };
        var got = false;
        messages.each(function (message) {
            if (!got && message.id > req.last_msg)
                got = req.got_message(message);
        });
        if (!got) {
            pollers.add(req);
            res.setTimeout(comet_timeout, function () {
                pollers.remove(req);
                res.writeHead(504, {'Content-Type': 'application/json'});
                res.end('{"error": "timeout"}');
            });
        }
    }).listen(argv.http_port, argv.http_host);
}