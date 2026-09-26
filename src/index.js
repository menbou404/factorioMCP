import { McpServer } from '@modelcontextprotocol/server';
import { serveStdio } from '@modelcontextprotocol/server/stdio';
import * as z from 'zod/v4';
import { BridgeError, FactorioBridge } from './bridge.js';

const password = process.env.FACTORIO_RCON_PASSWORD;
const port = Number(process.env.FACTORIO_RCON_PORT ?? 27015);
const bridge = password ? new FactorioBridge({ port, password }) : null;
let heartbeatInFlight = false;
if (bridge) {
  setInterval(async () => {
    if (heartbeatInFlight) return;
    heartbeatInFlight = true;
    try { await bridge.call('ping'); } catch { /* Game may not be hosted yet. */ }
    finally { heartbeatInFlight = false; }
  }, 2000).unref();
}

function response(value) {
  return { content: [{ type: 'text', text: JSON.stringify(value) }] };
}

function normalizeResult(operation, result) {
  if (operation === 'observe_nearby') {
    if (!Array.isArray(result.entities)) result.entities = [];
    if (!Array.isArray(result.blocked_tiles)) result.blocked_tiles = [];
  }
  if (operation === 'get_inventory' && !Array.isArray(result.items)) result.items = [];
  if (operation === 'get_agent_status' && !Array.isArray(result.inventory)) result.inventory = [];
  if (operation === 'list_craftable_recipes' && !Array.isArray(result.recipes)) result.recipes = [];
  if (operation === 'get_research_status' && !Array.isArray(result.researched)) result.researched = [];
  if (operation === 'get_entity_inventory') {
    for (const [role, items] of Object.entries(result.inventories ?? {})) {
      if (!Array.isArray(items)) result.inventories[role] = [];
    }
  }
  return result;
}

async function invoke(operation, params = {}) {
  if (!bridge) {
    return { content: [{ type: 'text', text: JSON.stringify({
      ok: false, error: 'not_configured',
      message: 'Set FACTORIO_RCON_PASSWORD before starting the MCP server.'
    }) }], isError: true };
  }
  try {
    const result = await bridge.call(operation, params);
    return response({ ok: true, tick: result.tick,
      ...normalizeResult(operation, result.result ?? {}) });
  } catch (error) {
    const code = error instanceof BridgeError ? error.code : 'internal_error';
    return { content: [{ type: 'text', text: JSON.stringify({
      ok: false, error: code, message: error.message,
      request_id: error instanceof BridgeError ? error.detail?.request_id : undefined
    }) }], isError: true };
  }
}

serveStdio(() => {
  const server = new McpServer({ name: 'factorio-ai', version: '0.1.0' });

  server.registerTool('factorio_ping', {
    description: 'Check the connection to the Factorio AI Mod.',
    inputSchema: z.object({})
  }, () => invoke('ping'));

  server.registerTool('get_agent_status', {
    description: 'Get the AI character position, health, inventory, and current action.',
    inputSchema: z.object({})
  }, () => invoke('get_agent_status'));

  server.registerTool('observe_nearby', {
    description: 'Observe nearby entities and blocked terrain tiles within 16 tiles. Follow offsets for additional pages.',
    inputSchema: z.object({
      entity_offset: z.number().int().min(0).default(0),
      tile_offset: z.number().int().min(0).default(0)
    })
  }, (args) => invoke('observe_nearby', args));

  server.registerTool('get_inventory', {
    description: 'Read the AI character’s actual in-game inventory.',
    inputSchema: z.object({})
  }, () => invoke('get_inventory'));

  server.registerTool('list_craftable_recipes', {
    description: 'Page through unlocked hand crafting recipes, ingredients, and current craftable counts.',
    inputSchema: z.object({
      offset: z.number().int().min(0).default(0),
      limit: z.number().int().min(1).max(100).default(50)
    })
  }, (args) => invoke('list_craftable_recipes', args));

  server.registerTool('get_research_status', {
    description: 'Read technologies researched by the AI character’s force and current research.',
    inputSchema: z.object({})
  }, () => invoke('get_research_status'));

  server.registerTool('move_to', {
    description: 'Walk to a position on the current surface. Returns an action ID for polling.',
    inputSchema: z.object({
      surface: z.string().min(1), x: z.number().finite(), y: z.number().finite()
    })
  }, (args) => invoke('move_to', args));

  server.registerTool('mine_entity', {
    description: 'Mine an observed entity within reach over normal game time. Returns an action ID.',
    inputSchema: z.object({
      target_ref: z.string().min(1),
      count: z.number().int().min(1).max(100).default(1)
    })
  }, (args) => invoke('mine_entity', args));

  server.registerTool('craft', {
    description: 'Start normal hand crafting using the AI character’s own materials. Poll the returned action ID.',
    inputSchema: z.object({recipe: z.string().min(1), count: z.number().int().min(1).max(100)})
  }, (args) => invoke('craft', args));

  server.registerTool('place_entity', {
    description: 'Place a supported entity within build reach, consuming one real inventory item.',
    inputSchema: z.object({
      item: z.string().min(1), surface: z.string().min(1),
      x: z.number().finite(), y: z.number().finite(),
      direction: z.union([z.literal(0), z.literal(4), z.literal(8), z.literal(12)]).default(0)
    })
  }, (args) => invoke('place_entity', args));

  server.registerTool('get_entity_inventory', {
    description: 'Read accessible inventory sections of a nearby observed structure.',
    inputSchema: z.object({target_ref: z.string().min(1)})
  }, (args) => invoke('get_entity_inventory', args));

  server.registerTool('transfer_item', {
    description: 'Move a real item between the AI character and a nearby observed structure.',
    inputSchema: z.object({
      target_ref: z.string().min(1),
      direction: z.enum(['to_entity', 'from_entity']),
      inventory_role: z.string().min(1),
      item: z.string().min(1),
      count: z.number().int().min(1).max(100)
    })
  }, (args) => invoke('transfer_item', args));

  server.registerTool('get_action_status', {
    description: 'Get the status of a walking, mining, or crafting action.',
    inputSchema: z.object({ action_id: z.string().min(1) })
  }, (args) => invoke('get_action_status', args));

  server.registerTool('cancel_action', {
    description: 'Stop an ongoing walking, mining, or crafting action.',
    inputSchema: z.object({ action_id: z.string().min(1) })
  }, (args) => invoke('cancel_action', args));

  server.registerTool('get_request_result', {
    description: 'After an uncertain RCON timeout, check whether the original change request executed. Do not repeat the change first.',
    inputSchema: z.object({ request_id: z.string().min(1).max(80) })
  }, (args) => invoke('get_request_result', args));

  return server;
});
