const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'ui', 'index.html'), 'utf8');
const sendStart = html.indexOf('function wsSend(obj)');
const sendEnd = html.indexOf('/* --- WS resume state ---', sendStart);
const connectStart = html.indexOf('function connectWS(){', sendEnd);
const connectEnd = html.indexOf('function handleEvent(ev){', connectStart);
if ([sendStart, sendEnd, connectStart, connectEnd].some(index => index < 0)) {
  throw new Error('Could not locate RPC UI functions');
}
const source = html.slice(sendStart, sendEnd) + html.slice(connectStart, connectEnd);

function makeContext(contract) {
  const sockets = [];
  const timers = [];
  const calls = [];
  class FakeWebSocket {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(value) { this.sent.push(JSON.parse(value)); }
    close(code, reason) { this.closed = { code, reason }; this.readyState = 3; }
  }
  const context = {
    ws: null, wsRetry: 0, wsContractDoc: null, wsContractLoading: false, wsRpcRequestId: 0,
    lastSeq: 7, wsNoteSeq: () => true, gapResync: () => {}, handleEvent: () => {},
    S: { watchTid: 'thread-1', curId: 'thread-1' }, TOKEN: 'test-token',
    location: { protocol: 'https:', host: 'an.test' }, WebSocket: FakeWebSocket,
    api: async path => { calls.push(path); if (path !== '/api/rpc/contract') throw new Error('unexpected API'); return contract; },
    setConn: () => {}, bootDone: () => {}, viewNow: () => ({ name: 'chat' }), resyncLight: () => {},
    recoverTimeline: () => {}, toast: () => {}, setTimeout: callback => { timers.push(callback); return timers.length; },
    Math, JSON, Number, Array, String, Promise, encodeURIComponent,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, sockets, timers, calls };
}

async function flush() {
  for (let index = 0; index < 8; index++) await Promise.resolve();
}

(async () => {
  const contract = { contractVersion: 1, http: { apiVersion: 1 },
    websocket: { version: 1, defaultMethodPolicy: 'deny', methods: { 'thread.setWatches': {} } } };
  const ok = makeContext(contract);
  ok.context.connectWS();
  await flush();
  if (ok.calls.length !== 1 || ok.calls[0] !== '/api/rpc/contract') throw new Error('UI did not load RPC contract');
  if (ok.sockets.length !== 1) throw new Error('UI did not connect after negotiation');
  const socket = ok.sockets[0];
  const url = new URL(socket.url);
  if (url.searchParams.get('rpc') !== '1' || url.searchParams.get('k') !== 'test-token') {
    throw new Error('WebSocket handshake omitted negotiated version or token');
  }
  socket.readyState = 1;
  socket.onopen();
  const sent = socket.sent[0];
  if (sent.version !== 1 || sent.method !== 'thread.setWatches' ||
      sent.params.threadIds[0] !== 'thread-1' || !sent.id) {
    throw new Error('UI did not emit a versioned typed RPC request');
  }
  socket.onmessage({ data: JSON.stringify({ kind: 'rpc.result', version: 1, id: sent.id, result: { ok: true } }) });
  socket.onmessage({ data: JSON.stringify({ kind: 'rpc.error', version: 1, error: { code: 'unauthorized' } }) });
  if (!socket.closed || socket.closed.code !== 1008) throw new Error('UI did not close a revoked socket');

  const incompatible = makeContext({ contractVersion: 2, http: { apiVersion: 2 },
    websocket: { version: 2, defaultMethodPolicy: 'deny', methods: {} } });
  incompatible.context.connectWS();
  await flush();
  if (incompatible.sockets.length !== 0) throw new Error('UI connected with an incompatible contract');
  process.stdout.write('Versioned RPC UI smoke passed\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
