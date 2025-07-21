const config = require('./config');
const queue = require('qu');
const { Server } = require('socket.io');
const http = require('http');
const express = require('express');

// Create Express app and HTTP server
const app = express();
const server = http.createServer(app);

// Generate a random token for backend authentication (or use from config)
const BACKEND_AUTH_TOKEN = config.backend_auth_token;

// Initialize Socket.IO
const io = new Server(server, {
  cors: {
    origin: "*",
    methods: ["GET", "POST"]
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
const max_subscriptions_per_connection = config.max_subscriptions_per_connection || 10;
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
  
  // Emit to all subscribers of this channel
  io.to(channel).emit('message', messageObj);
  
  return messageObj.id;
};

messages.last = function() {
  return this.tail()?.id || 0;
};

// Authentication middleware for socket connections
io.use((socket, next) => {
  const token = socket.handshake.auth.token;
  const role = socket.handshake.auth.role || 'client';
  
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
      if (typeof data.channel !== 'string' || data.channel.length === 0 || data.channel.length > 100) {
        const error = {
          status: 'error',
          code: 'invalid-channel',
          message: 'Invalid channel'
        };
        
        if (callback) callback(error);
        else socket.emit('error', error);
        return;
      }
      
      const id = messages.post(data.channel, data.message);
      const response = {
        status: 'success',
        id: id
      };
      
      if (callback) callback(response);
      else socket.emit('post-response', response);
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
  
  // Add client metadata
  socket.metadata = {
    connectedAt: Date.now(),
    lastActivity: Date.now()
  };
  
  // Setup got_message function for this socket
  socket.got_message = (message) => {
    socket.emit('message', message);
    socket.last_msg = message.id;
  };
  
  // Client commands
  socket.on('start-msg', (data) => {
    socket.metadata.lastActivity = Date.now();
    socket.last_msg = data.start || 0;
    socket.emit('status', { status: 'success' });
  });
  
  socket.on('set-filter', (data) => {
    socket.metadata.lastActivity = Date.now();
    
    if (!Array.isArray(data.filter) || data.filter.length === 0) {
      socket.emit('error', {
        status: 'error',
        code: 'invalid-filter',
        message: 'Invalid filter'
      });
      return;
    }
    
    if (data.filter.length > max_subscriptions_per_connection) {
      socket.emit('error', {
        status: 'error',
        code: 'too-many-subscriptions',
        message: `Maximum ${max_subscriptions_per_connection} subscriptions per connection`
      });
      return;
    }
    
    // Validate channels
    const validChannels = data.filter.every(channel => {
      return typeof channel === 'string' && channel.length > 0 && channel.length <= 100;
    });
    
    if (!validChannels) {
      socket.emit('error', {
        status: 'error',
        code: 'invalid-channel',
        message: 'Channel must be a non-empty string (max 100 chars)'
      });
      return;
    }
    
    // Leave all current rooms/channels
    socket.channels.forEach(channel => {
      socket.leave(channel);
    });
    
    socket.channels.clear();
    
    // Join new channels
    data.filter.forEach(channel => {
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