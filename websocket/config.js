const fs = require('fs');
const path = require('path');

const config = {
    http_host: '127.0.0.1',
    http_port: 15100,
    connection_timeout: 300000,
    backend_auth_token: 'lqdoj', // EVENT_DAEMON_KEY in local_settings.py
    allowed_origins: [
        'http://127.0.0.1:8000',
        'http://localhost:8000',
    ],
    max_subscriptions_per_connection: 64,
};

// Keep machine-specific secrets in the ignored config.local.js file.
const localConfigPath = path.join(__dirname, 'config.local.js');
module.exports = fs.existsSync(localConfigPath)
    ? Object.assign(config, require(localConfigPath))
    : config;
