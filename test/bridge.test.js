import assert from 'node:assert/strict';
import { test } from 'node:test';
import { BridgeError, FactorioBridge, parseBridgeResponse } from '../src/bridge.js';

test('accepts only the matching protocol response', () => {
  const raw = [
    'unrelated log line',
    JSON.stringify({ protocol_version: 1, request_id: 'other', ok: true }),
    JSON.stringify({ protocol_version: 1, request_id: 'wanted', ok: true, result: { x: 2 } })
  ].join('\n');
  assert.deepEqual(parseBridgeResponse(raw, 'wanted').result, { x: 2 });
  assert.throws(() => parseBridgeResponse(raw, 'missing'),
    (error) => error instanceof BridgeError && error.code === 'mod_unavailable');
});

test('passes through game errors and rejects remote RCON hosts', () => {
  const raw = JSON.stringify({ protocol_version: 1, request_id: 'x', ok: false,
    error: 'missing_materials' });
  assert.throws(() => parseBridgeResponse(raw, 'x'),
    (error) => error instanceof BridgeError && error.code === 'missing_materials');
  assert.throws(() => new FactorioBridge({ host: '192.0.2.1', password: 'test' }),
    (error) => error instanceof BridgeError && error.code === 'invalid_config');
});

test('retries an uncertain send with the same request ID', async () => {
  const requestIds = [];
  const bridge = new FactorioBridge({
    password: 'test',
    connect: async () => ({
      send: async (command) => {
        const request = JSON.parse(command.slice('/ai_mcp '.length));
        requestIds.push(request.request_id);
        if (requestIds.length === 1) throw new Error('lost response');
        return JSON.stringify({ protocol_version: 1, request_id: request.request_id,
          ok: true, result: { action_id: '1' } });
      },
      end: () => {}
    })
  });
  assert.equal((await bridge.call('move_to', { x: 1 })).result.action_id, '1');
  assert.equal(requestIds.length, 2);
  assert.equal(requestIds[0], requestIds[1]);
});
