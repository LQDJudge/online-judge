# WebSocket Service

This directory contains the WebSocket daemon that handles real-time communication between the LQDOJ web application and clients.

## Purpose

The WebSocket service provides:
- Real-time event broadcasting to connected clients
- Message queue management with catch-up functionality  
- Authentication for backend services
- Connection management and rate limiting

## Architecture

- `daemon.js` - Main WebSocket server using Socket.IO
- `config.js` - Configuration settings (host, port, timeouts, auth token)
- `daemon_amqp.js` - AMQP integration variant
- `test_daemon.js` - Testing utilities
- `wscat.js` - WebSocket client testing tool

## Installation

1. Install Node.js (version 14+ recommended):
   - Ubuntu/Debian: `sudo apt install nodejs npm`
   - CentOS/RHEL: `sudo yum install nodejs npm`
   - macOS: `brew install node`
   - Windows: Download from [nodejs.org](https://nodejs.org)

2. Install project dependencies:
```bash
cd websocket
npm install
```

This will install:
- express (^5.1.0) - Web framework
- socket.io (^4.8.1) - WebSocket server
- socket.io-client (^4.8.1) - WebSocket client
- qu (^0.1.0) - Queue implementation
- simplesets (^1.2.0) - Set data structures
- websocket (^1.0.35) - WebSocket protocol
- ws (^8.18.3) - WebSocket library

3. Configure settings in `config.js`:
- Set `backend_auth_token` to match `EVENT_DAEMON_KEY` in Django settings
- Adjust `http_host` and `http_port` as needed
- Configure connection limits and timeouts

## Django Configuration

Add these settings to your `local_settings.py`:

```python
# WebSocket daemon settings
EVENT_DAEMON_KEY = 'your-secret-token'  # Must match backend_auth_token in config.js
EVENT_DAEMON_URL = 'http://127.0.0.1:15100'
EVENT_DAEMON_PUBLIC_URL = 'http://127.0.0.1:15100'  # Same as EVENT_DAEMON_URL in development

# For production with SSL/domain
# EVENT_DAEMON_PUBLIC_URL = 'wss://your-domain.com'  # nginx proxies to port 15100
```

## Running the Service

Start the WebSocket daemon:
```bash
node websocket/daemon.js
```

The service will listen on the configured host/port (default: 127.0.0.1:15100).

## Production Deployment

For production deployments with nginx, add this location block to your nginx configuration:

```nginx
location /socket.io/ {
    proxy_pass http://127.0.0.1:15100/socket.io/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
}
```

Then configure Django to use the public WebSocket URL:

```python
# In local_settings.py for production
EVENT_DAEMON_PUBLIC_URL = 'wss://your-domain.com'  # nginx proxies to port 15100
```

## Integration

The Python client (`judge/event_poster_ws.py`) connects to this service to:
- Post events to channels: `post(channel, message)`
- Get last message ID: `last()`

Clients authenticate using the role 'sender' and the configured auth token.

## Dependencies

- express: Web framework
- socket.io: WebSocket library
- qu: Queue implementation
- simplesets: Set data structures
- ws/websocket: WebSocket protocol support