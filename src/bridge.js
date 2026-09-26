import { randomUUID } from 'node:crypto';
import { Rcon } from 'rcon-client';

export class BridgeError extends Error {
  constructor(code, message, detail) {
    super(message ?? code);
    this.name = 'BridgeError';
    this.code = code;
    this.detail = detail;
  }
}

export function parseBridgeResponse(raw, requestId) {
  const lines = String(raw).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  for (const line of lines) {
    try {
      const response = JSON.parse(line);
      if (response.request_id === requestId && response.protocol_version === 1) {
        if (!response.ok) {
          throw new BridgeError(response.error ?? 'game_error', response.error, response.result);
        }
        return response;
      }
    } catch (error) {
      if (error instanceof BridgeError) throw error;
    }
  }
  throw new BridgeError('mod_unavailable', 'Factorio Mod did not return a matching response');
}

export class FactorioBridge {
  constructor({ host = '127.0.0.1', port = 27015, password, connect = Rcon.connect } = {}) {
    if (host !== '127.0.0.1' && host !== 'localhost' && host !== '::1') {
      throw new BridgeError('invalid_config', 'Only loopback RCON hosts are supported');
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new BridgeError('invalid_config', 'RCON port must be in 1..65535');
    }
    if (!password) throw new BridgeError('invalid_config', 'FACTORIO_RCON_PASSWORD is required');
    this.host = host;
    this.port = port;
    this.password = password;
    this.connect = connect;
    this.pending = Promise.resolve();
  }

  call(operation, params = {}) {
    const requestId = randomUUID();
    const request = {
      protocol_version: 1,
      request_id: requestId,
      agent_id: 'agent-1',
      operation,
      params
    };
    const work = this.pending.then(async () => {
      for (let attempt = 0; attempt < 2; attempt++) {
        let rcon;
        try {
          rcon = await this.connect({
            host: this.host,
            port: this.port,
            password: this.password,
            timeout: 5000
          });
          const raw = await rcon.send(`/ai_mcp ${JSON.stringify(request)}`);
          return parseBridgeResponse(raw, requestId);
        } catch (error) {
          if (error instanceof BridgeError) throw error;
          if (attempt === 1) {
            throw new BridgeError('not_connected', 'Could not reach Factorio RCON',
              { request_id: requestId });
          }
        } finally {
          rcon?.end();
        }
      }
    });
    this.pending = work.catch(() => {});
    return work;
  }
}
