function EventReceiver(socketUrl, channels, last_msg, onmessage) {
  // Configuration
  this.socketUrl = socketUrl;
  this.channels = channels;
  this.last_msg = last_msg || 0;
  
  // Set message handler
  if (onmessage) {
    this.onmessage = onmessage;
  } else {
    this.onmessage = function() {};
  }
  
  // Custom event handler
  this.onwsclose = null;
  
  // Socket reference
  let socket = null;
  
  // Connect to Socket.IO server
  const connect = () => {
    // Disconnect existing connection if any
    if (socket) {
      socket.disconnect();
    }
    
    // Create new Socket.IO connection with client role
    socket = io(this.socketUrl, {
      auth: {
        role: 'client'
      },
      reconnectionAttempts: 10,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      timeout: 10000
    });
    
    // Socket.IO event handlers
    socket.on('connect', () => {
      // Initialize with last message ID
      socket.emit('start-msg', {
        start: this.last_msg
      });
      
      // Set channel filter
      socket.emit('set-filter', {
        filter: this.channels
      });
    });
    
    socket.on('message', (data) => {
      this.onmessage(data.message);
      this.last_msg = data.id;
    });
    
    socket.on('error', (error) => {
      console.error('Socket error:', error);
    });
    
    socket.on('disconnect', (reason) => {
      if (this.onwsclose) {
        this.onwsclose({ code: 1006, reason: reason });
      }
    });
    
    socket.on('reconnect_failed', () => {
      console.error('Failed to reconnect after multiple attempts');
    });
  };
  
  // Handle page hide/show events
  window.addEventListener('pagehide', () => {
    if (socket) {
      socket.disconnect();
      socket = null;
    }
  });

  window.addEventListener('pageshow', (event) => {
    if (!socket) {
      connect();
    }
  });

  connect();
  
  // Public methods
  this.disconnect = () => {
    if (socket) {
      socket.disconnect();
      socket = null;
    }
  };
  
  this.reconnect = () => {
    connect();
  };
  
  // Add method to update channels
  this.updateChannels = (newChannels) => {
    this.channels = newChannels;
    if (socket && socket.connected) {
      socket.emit('set-filter', {
        filter: newChannels
      });
    }
  };
}