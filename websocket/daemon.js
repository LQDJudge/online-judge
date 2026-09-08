const config = require('./config');
const queue = require('qu');
const { Server } = require('socket.io');
const http = require('http');
const express = require('express');
const {
  chatGrantAllowsChannel,
  isAllowedOrigin,
  isChatChannel,
  parseStartMessage,
  validateChannelFilter,
  verifyChatGrant
} = require('./chat_grant');

// Create Express app and HTTP server
const app = express();
const server = http.createServer(app);

// Generate a random token for backend authentication (or use from config)
const BACKEND_AUTH_TOKEN = config.backend_auth_token;

// Initialize Socket.IO
const io = new Server(server, {
  cors: {
    origin: config.allowed_origins,
    methods: ["GET", "POST"]
  },
  allowRequest: (request, callback) => {
    callback(
      null,
      isAllowedOrigin(request.headers.origin, config.allowed_origins)
    );
  },
  transports: ['websocket', 'polling'],
  pingTimeout: config.connection_timeout || 300000,
  pingInterval: 25000,
  maxHttpBufferSize: 10000 // 10KB limit
});

// Connection tracking
let connection_count = 0;
let message_id = Date.now();
const messages = new queue();
const max_queue = config.max_queue || 50;
const max_subscriptions_per_connection = config.max_subscriptions_per_connection || 64;
const max_connections = config.max_connections || 5000;

// Queue methods
messages.catch_up = function(client) {
  this.each(message => {
    if (message.id > client.last_msg && client.channels.has(message.channel)) {
      client.got_message(message);
    }
  });
};

messages.post = function(channel, message) {
  const messageObj = {
    id: ++message_id,
    channel: channel,
    message: message
  };
  
  this.push(messageObj);
  
  if (this.length > max_queue) {
    this.shift();
  }
  
  // Route every delivery through the per-socket authorization gate. A raw
  // room broadcast would let an expired chat grant keep receiving live events.
  const subscribers = io.sockets.adapter.rooms.get(channel);
  if (subscribers) {
    Array.from(subscribers).forEach(socketId => {
      const client = io.sockets.sockets.get(socketId);
      if (client && typeof client.got_message === 'function') {
        client.got_message(messageObj);
      }
    });
  }
  
  return messageObj.id;
};

messages.last = function() {
  return this.tail()?.id || 0;
};

const max_backend_batch = 50;

function senderEventError(data) {
  if (!data || typeof data.channel !== 'string' ||
      data.channel.length === 0 || data.channel.length > 100) {
    return {
      status: 'error',
      code: 'invalid-channel',
      message: 'Invalid channel'
    };
  }
  return null;
}

function postSenderEvent(data) {
  if (data.channel === '__chat_revoke_user_room__') {
    const revocation = data.message || {};
    io.sockets.sockets.forEach(client => {
      const claims = client.chatGrant;
      if (!client.isSender && claims &&
          Number(claims.user_id) === Number(revocation.user_id) &&
          claims.room_ids.includes(Number(revocation.room_id))) {
        if (typeof revocation.channel === 'string') {
          client.leave(revocation.channel);
          client.channels.delete(revocation.channel);
          claims.channels = claims.channels.filter(
            channel => channel !== revocation.channel
          );
        }
        claims.room_ids = claims.room_ids.filter(
          roomId => roomId !== Number(revocation.room_id)
        );
        client.emit('chat-revoked', revocation);
      }
    });
    return ++message_id;
  }
  return messages.post(data.channel, data.message);
}

function answerSender(socket, callback, response, eventName) {
  if (callback) callback(response);
  else socket.emit(eventName, response);
}

// Authentication middleware for socket connections
io.use((socket, next) => {
  const auth = socket.handshake.auth || {};
  const token = auth.token;
  const role = auth.role || 'client';
  
  // If role is sender, require valid token
  if (role === 'sender') {
    if (token !== BACKEND_AUTH_TOKEN) {
      return next(new Error('Authentication failed'));
    }
    socket.isSender = true;
  } else {
    socket.isSender = false;
  }
  
  socket.role = role;
  next();
});

// Socket.IO connection handler
io.on('connection', (socket) => {
  // For sender connections, set up sender commands only
  if (socket.isSender) {
    // Sender commands
    socket.on('post', (data, callback) => {
      const error = senderEventError(data);
      if (error) {
        answerSender(socket, callback, error, 'error');
        return;
      }
      answerSender(
        socket,
        callback,
        { status: 'success', id: postSenderEvent(data) },
        'post-response'
      );
    });

    socket.on('post-batch', (data, callback) => {
      const events = data && data.events;
      if (!Array.isArray(events) || events.length === 0 ||
          events.length > max_backend_batch) {
        answerSender(socket, callback, {
          status: 'error',
          code: 'invalid-batch',
          message: 'Invalid event batch'
        }, 'error');
        return;
      }
      const error = events.map(senderEventError).find(Boolean);
      if (error) {
        answerSender(socket, callback, error, 'error');
        return;
      }
      let id = 0;
      events.forEach(event => {
        id = postSenderEvent(event);
      });
      answerSender(
        socket,
        callback,
        { status: 'success', id: id },
        'post-response'
      );
    });
    
    socket.on('last-msg', (callback) => {
      const response = {
        status: 'success',
        id: message_id
      };
      
      if (callback) callback(response);
      else socket.emit('last-response', response);
    });
    
    return;
  }
  
  // For client connections, proceed with regular setup
  // Connection limiting
  if (connection_count >= max_connections) {
    socket.emit('error', {
      status: 'error',
      code: 'server-capacity',
      message: 'Server at capacity'
    });
    socket.disconnect(true);
    return;
  }
  
  connection_count++;
  
  // Initialize socket properties
  socket.last_msg = 0;
  socket.channels = new Set();
  socket.chatGrant = verifyChatGrant(
    socket.handshake.auth.grant,
    BACKEND_AUTH_TOKEN
  );
  
  // Add client metadata
  socket.metadata = {
    connectedAt: Date.now(),
    lastActivity: Date.now()
  };
  
  // Setup got_message function for this socket
  socket.got_message = (message) => {
    if (!chatGrantAllowsChannel(socket.chatGrant, message.channel)) {
      socket.emit('error', {
        status: 'error',
        code: 'chat-grant-expired',
        message: 'Chat subscription grant expired'
      });
      socket.disconnect(true);
      return;
    }
    socket.emit('message', message);
    socket.last_msg = message.id;
  };
  
  // Client commands
  socket.on('start-msg', (data) => {
    socket.metadata.lastActivity = Date.now();
    const start = parseStartMessage(data);
    if (start === null) {
      socket.emit('error', {
        status: 'error',
        code: 'invalid-start',
        message: 'Invalid starting message ID'
      });
      return;
    }
    socket.last_msg = start;
    socket.emit('status', { status: 'success' });
  });
  
  socket.on('set-filter', (data) => {
    socket.metadata.lastActivity = Date.now();

    const validated = validateChannelFilter(
      data,
      max_subscriptions_per_connection
    );
    if (validated.code === 'invalid-filter') {
      socket.emit('error', {
        status: 'error',
        code: 'invalid-filter',
        message: 'Invalid filter'
      });
      return;
    }
    
    if (validated.code === 'too-many-subscriptions') {
      socket.emit('error', {
        status: 'error',
        code: 'too-many-subscriptions',
        message: `Maximum ${max_subscriptions_per_connection} subscriptions per connection`
      });
      return;
    }
    
    if (validated.code === 'invalid-channel') {
      socket.emit('error', {
        status: 'error',
        code: 'invalid-channel',
        message: 'Channel must be a non-empty string (max 100 chars)'
      });
      return;
    }

    const requestedChatChannels = validated.filter.filter(isChatChannel);
    if (requestedChatChannels.length &&
        requestedChatChannels.some(
          channel => !chatGrantAllowsChannel(socket.chatGrant, channel)
        )) {
      socket.emit('error', {
        status: 'error',
        code: 'unauthorized-chat-channel',
        message: 'A valid chat subscription grant is required'
      });
      return;
    }
    
    // Leave all current rooms/channels
    socket.channels.forEach(channel => {
      socket.leave(channel);
    });
    
    socket.channels.clear();
    
    // Join new channels
    validated.filter.forEach(channel => {
      socket.join(channel);
      socket.channels.add(channel);
    });
    
    socket.emit('status', { status: 'success' });
    
    // Send catch-up messages
    messages.catch_up(socket);
  });
  
  socket.on('last-msg', () => {
    socket.metadata.lastActivity = Date.now();
    
    socket.emit('last-response', {
      status: 'success',
      id: message_id
    });
  });
  
  // Explicitly block sender commands for client connections
  socket.on('post', () => {
    socket.emit('error', {
      status: 'error',
      code: 'unauthorized',
      message: 'Unauthorized operation'
    });
  });
  
  // Handle disconnection
  socket.on('disconnect', () => {
    connection_count--;
  });
});

// Memory monitoring
const logMemoryUsage = () => {
  const used = process.memoryUsage();
  console.log(`Memory Usage: RSS=${Math.round(used.rss/1024/1024)}MB, Heap=${Math.round(used.heapUsed/1024/1024)}MB`);
  console.log(`Connections: ${connection_count}, Rooms: ${io.sockets.adapter.rooms.size}`);
};

// Log memory usage periodically
setInterval(logMemoryUsage, 300000); // Log every 5 minutes

// Start server
server.listen(config.http_port, config.http_host, () => {
  console.log(`Socket.IO server running on http://${config.http_host}:${config.http_port}`);
});
