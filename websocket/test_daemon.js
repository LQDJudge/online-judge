const { io: SocketIOClient } = require('socket.io-client');
const http = require('http');
const url = require('url');
const fs = require('fs');
const path = require('path');

// Load config from the same file as the server
let config;
try {
    // First try to load from the same location as the server
    config = require('../config');
} catch (e) {
    try {
        // Then try to load from relative path
        config = require('./config');
    } catch (e) {
        // Fallback to default config
        console.warn('Could not load config file, using default test config');
        config = {
            backend_auth_token: 'test-token',
            http_port: 15100,
            http_host: '127.0.0.1'
        };
    }
}

// Test configuration
const TEST_CONFIG = {
    port: config.http_port || 15100,
    host: config.http_host || '127.0.0.1',
    auth_token: config.backend_auth_token || 'test-token'
};

console.log(`Using server at ${TEST_CONFIG.host}:${TEST_CONFIG.port} with auth token: ${TEST_CONFIG.auth_token.substring(0, 3)}...`);

// Utility functions
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

// Test runner
class TestRunner {
    constructor() {
        this.tests = [];
        this.passed = 0;
        this.failed = 0;
    }
    
    test(name, fn) {
        this.tests.push({ name, fn });
    }
    
    async run() {
        console.log(`\n🧪 Running ${this.tests.length} tests...\n`);
        
        for (const test of this.tests) {
            try {
                console.log(`▶️  ${test.name}`);
                await test.fn();
                console.log(`✅ ${test.name} - PASSED\n`);
                this.passed++;
            } catch (error) {
                console.log(`❌ ${test.name} - FAILED`);
                console.log(`   Error: ${error.message}\n`);
                this.failed++;
            }
        }
        
        console.log(`\n📊 Results: ${this.passed} passed, ${this.failed} failed`);
        return this.failed === 0;
    }
}

// Helper to create Socket.IO sender client
const createSender = () => {
    return new Promise((resolve, reject) => {
        const socket = SocketIOClient(`http://${TEST_CONFIG.host}:${TEST_CONFIG.port}`, {
            auth: {
                role: 'sender',
                token: TEST_CONFIG.auth_token
            },
            transports: ['websocket']
        });
        
        socket.on('connect', () => {
            console.log('Sender connected');
            resolve(socket);
        });
        
        socket.on('connect_error', (error) => {
            console.error('Sender connection error:', error);
            reject(new Error(`Socket.IO connection error: ${error.message}`));
        });
        
        // Set a connection timeout
        const timeout = setTimeout(() => {
            socket.disconnect();
            reject(new Error('Socket.IO connection timeout'));
        }, 5000);
        
        socket.on('connect', () => {
            clearTimeout(timeout);
        });
    });
};

// Helper to send message via Socket.IO
const sendMessage = async (channel, message) => {
    const sender = await createSender();
    
    return new Promise((resolve, reject) => {
        sender.emit('post', {
            channel: channel,
            message: message
        }, (response) => {
            sender.disconnect();
            if (response && response.status === 'success') {
                resolve(response);
            } else {
                reject(new Error(`Failed to send message: ${JSON.stringify(response)}`));
            }
        });
        
        // Set a response timeout
        setTimeout(() => {
            sender.disconnect();
            reject(new Error('Socket.IO response timeout'));
        }, 5000);
    });
};

// Helper to get last message ID
const getLastMessageId = async () => {
    const sender = await createSender();
    
    return new Promise((resolve, reject) => {
        sender.emit('last-msg', (response) => {
            sender.disconnect();
            if (response && response.status === 'success') {
                resolve(response.id || 0);
            } else {
                reject(new Error(`Failed to get last message ID: ${JSON.stringify(response)}`));
            }
        });
        
        // Set a response timeout
        setTimeout(() => {
            sender.disconnect();
            reject(new Error('Socket.IO response timeout'));
        }, 5000);
    });
};

// Helper to create Socket.IO receiver client with proper initialization
const createReceiver = async (channels = [], startMsgId = null) => {
    // If startMsgId is not provided, get the current last message ID
    if (startMsgId === null) {
        try {
            startMsgId = await getLastMessageId();
            console.log(`Using last message ID: ${startMsgId}`);
        } catch (error) {
            console.warn(`Failed to get last message ID: ${error.message}`);
            startMsgId = 0;
        }
    }
    
    return new Promise((resolve, reject) => {
        const socket = SocketIOClient(`http://${TEST_CONFIG.host}:${TEST_CONFIG.port}`, {
            transports: ['websocket']
        });
        
        const messages = [];
        
        socket.on('connect', () => {
            console.log(`Receiver connected, initializing with start-msg: ${startMsgId}`);
            
            // Initialize with start-msg
            socket.emit('start-msg', { start: startMsgId });
            
            // Set filter if channels provided
            if (channels.length > 0) {
                socket.emit('set-filter', { filter: channels });
            }
            
            resolve({ socket, messages, startMsgId });
        });
        
        socket.on('message', (message) => {
            if (message.id > startMsgId) {
                messages.push(message);
            } else {
                console.log(`Skipping older message: ${message.id} (start: ${startMsgId})`);
            }
        });
        
        socket.on('error', (error) => {
            console.error('Receiver error:', error);
        });
        
        socket.on('connect_error', (error) => {
            console.error('Receiver connection error:', error);
            reject(new Error(`Socket.IO receiver connection error: ${error.message}`));
        });
        
        // Set a connection timeout
        const timeout = setTimeout(() => {
            socket.disconnect();
            reject(new Error('Socket.IO receiver connection timeout'));
        }, 5000);
        
        socket.on('connect', () => {
            clearTimeout(timeout);
        });
    });
};

// Create test instance
const runner = new TestRunner();

// Test 1: Basic connection and authentication
runner.test('Basic connection and authentication', async () => {
    // Test receiver connection
    const { socket: receiverSocket } = await createReceiver();
    receiverSocket.disconnect();
    
    // Test sender connection
    const senderSocket = await createSender();
    senderSocket.disconnect();
});

// Test 2: Basic message sending and receiving
runner.test('Basic message sending and receiving', async () => {
    // Get current last message ID to start from
    const lastId = await getLastMessageId();
    
    // Create receiver starting from lastId
    const { socket: receiverSocket, messages } = await createReceiver(['test-channel'], lastId);
    
    // Send a message
    const response = await sendMessage('test-channel', 'Hello Socket.IO!');
    
    // Verify response has success status and an ID
    if (response.status !== 'success' || !response.id) {
        throw new Error(`Invalid response: ${JSON.stringify(response)}`);
    }
    
    // Wait for message to arrive
    await sleep(300);
    
    // Check received message
    if (messages.length !== 1) {
        throw new Error(`Expected 1 message, got ${messages.length}`);
    }
    
    if (messages[0].message !== 'Hello Socket.IO!') {
        throw new Error(`Expected message 'Hello Socket.IO!', got '${messages[0].message}'`);
    }
    
    if (messages[0].channel !== 'test-channel') {
        throw new Error(`Expected channel 'test-channel', got '${messages[0].channel}'`);
    }
    
    receiverSocket.disconnect();
});

// Test 3: Channel-based message routing
runner.test('Channel-based message routing', async () => {
    // Get current last message ID to start from
    const lastId = await getLastMessageId();
    
    // Create receivers all starting from the same lastId
    const { socket: socket1, messages: messages1 } = await createReceiver(['channel1'], lastId);
    const { socket: socket2, messages: messages2 } = await createReceiver(['channel2'], lastId);
    const { socket: socket3, messages: messages3 } = await createReceiver(['channel1', 'channel2'], lastId);
    
    // Send messages to different channels
    await sendMessage('channel1', 'Message for channel1');
    await sendMessage('channel2', 'Message for channel2');
    
    // Wait for messages to arrive
    await sleep(300);
    
    // Check socket1 (only channel1)
    if (messages1.length !== 1 || messages1[0].message !== 'Message for channel1') {
        throw new Error(`Receiver1 did not receive correct message. Got ${messages1.length} messages`);
    }
    
    // Check socket2 (only channel2)
    if (messages2.length !== 1 || messages2[0].message !== 'Message for channel2') {
        throw new Error(`Receiver2 did not receive correct message. Got ${messages2.length} messages`);
    }
    
    // Check socket3 (both channels)
    if (messages3.length !== 2) {
        throw new Error(`Receiver3 expected 2 messages, got ${messages3.length}`);
    }
    
    const hasChannel1Msg = messages3.some(m => m.channel === 'channel1' && m.message === 'Message for channel1');
    const hasChannel2Msg = messages3.some(m => m.channel === 'channel2' && m.message === 'Message for channel2');
    
    if (!hasChannel1Msg || !hasChannel2Msg) {
        throw new Error(`Receiver3 did not receive both channel messages correctly`);
    }
    
    socket1.disconnect();
    socket2.disconnect();
    socket3.disconnect();
});

// Test 4: Message queuing and catch-up
runner.test('Message queuing and catch-up', async () => {
    // Get current last message ID
    const initialLastId = await getLastMessageId();
    
    // Send messages first
    const msg1 = await sendMessage('catchup-channel', 'Message 1');
    const msg2 = await sendMessage('catchup-channel', 'Message 2');
    const msg3 = await sendMessage('catchup-channel', 'Message 3');
    
    // Wait for messages to be processed
    await sleep(100);
    
    // Connect a receiver that specifically starts from before these messages
    const { socket, messages } = await createReceiver(['catchup-channel'], initialLastId);
    
    // Wait for catch-up messages to arrive
    await sleep(300);
    
    // Should receive all three messages
    if (messages.length !== 3) {
        throw new Error(`Expected 3 catch-up messages, got ${messages.length}`);
    }
    
    // Verify message order
    const msg1Index = messages.findIndex(m => m.message === 'Message 1');
    const msg2Index = messages.findIndex(m => m.message === 'Message 2');
    const msg3Index = messages.findIndex(m => m.message === 'Message 3');
    
    if (msg1Index === -1 || msg2Index === -1 || msg3Index === -1) {
        throw new Error('Not all expected messages were received');
    }
    
    if (msg1Index > msg2Index || msg2Index > msg3Index) {
        throw new Error('Messages received out of order');
    }
    
    socket.disconnect();
});

// Test 5: Filter changes mid-session
runner.test('Filter changes mid-session', async () => {
    // Get current last message ID
    const lastId = await getLastMessageId();
    
    const { socket, messages } = await createReceiver(['channel-a'], lastId);
    
    // Send message to channel-a
    await sendMessage('channel-a', 'Message for A');
    
    // Wait for message to arrive
    await sleep(200);

    // Change filter to channel-b
    socket.emit('set-filter', { filter: ['channel-b'] });
    
    // Wait for set-filter to be processed
    await sleep(100);
    
    // Send message to both channels
    await sendMessage('channel-a', 'Second message for A');
    await sendMessage('channel-b', 'Message for B');
    
    // Wait for messages to arrive
    await sleep(200);
    
    // Should have received the first message for A and the message for B, but not the second message for A
    if (messages.length !== 2) {
        throw new Error(`Expected 2 messages, got ${messages.length}`);
    }
    
    const hasFirstMsgA = messages.some(m => m.channel === 'channel-a' && m.message === 'Message for A');
    const hasSecondMsgA = messages.some(m => m.channel === 'channel-a' && m.message === 'Second message for A');
    const hasMsgB = messages.some(m => m.channel === 'channel-b' && m.message === 'Message for B');
    
    if (!hasFirstMsgA) {
        throw new Error('Did not receive first message for channel-a');
    }
    
    if (hasSecondMsgA) {
        throw new Error('Should not have received second message for channel-a after filter change');
    }
    
    if (!hasMsgB) {
        throw new Error('Did not receive message for channel-b after filter change');
    }
    
    socket.disconnect();
});

// Test 6: Last message ID functionality
runner.test('Last message ID functionality', async () => {
    // Get initial last message ID
    const initialId = await getLastMessageId();
    
    // Send a new message
    const sendResponse = await sendMessage('last-id-test', 'Testing last ID');
    
    // Get updated last message ID
    const updatedId = await getLastMessageId();
    
    // Verify the ID increased
    if (updatedId <= initialId) {
        throw new Error(`Last message ID didn't increase (initial: ${initialId}, updated: ${updatedId})`);
    }
    
    // Verify the send response ID matches the last ID
    if (sendResponse.id !== updatedId) {
        throw new Error(`Send response ID (${sendResponse.id}) doesn't match last ID (${updatedId})`);
    }
});

// Test 7: Multiple connections
runner.test('Multiple connections', async () => {
    // Get current last message ID
    const lastId = await getLastMessageId();
    
    // Create multiple receivers
    const connections = [];
    const connectionCount = 5; // Using 5 connections
    
    for (let i = 0; i < connectionCount; i++) {
        connections.push(createReceiver([`multi-conn-${i}`], lastId));
    }
    
    // Wait for all connections to be established
    const receivers = await Promise.all(connections);
    
    // Send messages to each channel
    for (let i = 0; i < connectionCount; i++) {
        await sendMessage(`multi-conn-${i}`, `Message for connection ${i}`);
    }
    
    // Wait for messages to arrive
    await sleep(500);
    
    // Verify each receiver got its message
    for (let i = 0; i < connectionCount; i++) {
        const { socket, messages } = receivers[i];
        
        if (messages.length !== 1) {
            throw new Error(`Receiver ${i} expected 1 message, got ${messages.length}`);
        }
        
        if (messages[0].message !== `Message for connection ${i}`) {
            throw new Error(`Receiver ${i} got wrong message: ${messages[0].message}`);
        }
        
        socket.disconnect();
    }
});

// Test 8: Invalid channel error handling
runner.test('Invalid channel error handling', async () => {
    const sender = await createSender();
    
    try {
        // Should fail with a promise rejection
        await new Promise((resolve, reject) => {
            sender.emit('post', {
                channel: null, // Invalid channel
                message: 'Test'
            }, (response) => {
                sender.disconnect();
                
                if (response.status === 'error' && response.code === 'invalid-channel') {
                    resolve(response);
                } else {
                    reject(new Error(`Expected error response with code 'invalid-channel', got ${JSON.stringify(response)}`));
                }
            });
            
            // Set a response timeout
            setTimeout(() => {
                sender.disconnect();
                reject(new Error('Socket.IO response timeout'));
            }, 5000);
        });
    } catch (error) {
        sender.disconnect();
        throw error;
    }
});

// Test 9: Client can't send messages
runner.test('Client cannot send messages', async () => {
    // Create a normal client (not a sender)
    const socket = SocketIOClient(`http://${TEST_CONFIG.host}:${TEST_CONFIG.port}`, {
        transports: ['websocket']
    });
    
    return new Promise((resolve, reject) => {
        socket.on('connect', () => {
            // Try to send a message (should be rejected)
            socket.emit('post', {
                channel: 'test-channel',
                message: 'Unauthorized message'
            });
            
            socket.on('error', (error) => {
                socket.disconnect();
                
                if (error.status === 'error' && error.code === 'unauthorized') {
                    resolve();
                } else {
                    reject(new Error(`Expected unauthorized error, got ${JSON.stringify(error)}`));
                }
            });
            
            // Set a response timeout
            setTimeout(() => {
                socket.disconnect();
                reject(new Error('Did not receive error response for unauthorized action'));
            }, 5000);
        });
        
        socket.on('connect_error', (error) => {
            reject(new Error(`Connection error: ${error.message}`));
        });
    });
});

// Test 10: Empty or invalid filter handling
runner.test('Empty or invalid filter handling', async () => {
    const { socket } = await createReceiver();
    let receivedError = false;
    
    return new Promise((resolve, reject) => {
        socket.on('error', (error) => {
            if (error.status === 'error' && error.code === 'invalid-filter') {
                receivedError = true;
            }
        });
        
        // Send empty filter
        socket.emit('set-filter', { filter: [] });
        
        // Wait and check for error
        setTimeout(() => {
            socket.disconnect();
            
            if (receivedError) {
                resolve();
            } else {
                reject(new Error('Did not receive error for empty filter'));
            }
        }, 500);
    });
});

// Main execution
async function main() {
    console.log('🚀 Starting Socket.IO Server Tests');
    console.log('⚠️  Make sure the Socket.IO server is running');
    
    // Quick connectivity test
    try {
        const socket = SocketIOClient(`http://${TEST_CONFIG.host}:${TEST_CONFIG.port}`, {
            transports: ['websocket']
        });
        
        await new Promise((resolve, reject) => {
            socket.on('connect', () => {
                socket.disconnect();
                resolve();
            });
            
            socket.on('connect_error', (error) => {
                reject(new Error(`Cannot connect to Socket.IO server: ${error.message}`));
            });
            
            setTimeout(() => reject(new Error('Connection timeout')), 5000);
        });
    } catch (error) {
        console.log(`❌ ${error.message}`);
        process.exit(1);
    }
    
    try {
        const success = await runner.run();
        process.exit(success ? 0 : 1);
    } catch (error) {
        console.error('Test suite error:', error);
        process.exit(1);
    }
}

// Run tests if this file is executed directly
if (require.main === module) {
    main().catch(console.error);
}

module.exports = { TestRunner, sendMessage, createReceiver, getLastMessageId };