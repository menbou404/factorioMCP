// Manual test only: creates raw-resource fixtures in a disposable save via /c.
// It connects to the isolated test RCON port 27277 and must not be used on a real save.
import fs from 'node:fs';
import { Rcon } from 'rcon-client';
import { FactorioBridge } from '../../src/bridge.js';

const password = fs.readFileSync('.factorio-test/rcon-secret.txt', 'utf8');
const bridge = new FactorioBridge({ port: 27277, password });
const resume = process.argv.includes('--resume');
if (!resume) {
  const rcon = await Rcon.connect({ host: '127.0.0.1', port: 27277, password });
  const fixture = '/c local s=game.surfaces.nauvis;local n=0;for _,v in ipairs({{"stone",2,3},{"coal",3,5},{"iron-ore",5,5},{"iron-ore",6,5},{"iron-ore",5,6},{"iron-ore",6,6}}) do if s.create_entity{name=v[1],position={v[2],v[3]},amount=100} then n=n+1 end end;if s.create_entity{name="tree-01",position={3,1}} then n=n+1 end;rcon.print("fixture:"..n)';
  let fixtureResult = await rcon.send(fixture);
  if (!fixtureResult.includes('fixture:')) fixtureResult = await rcon.send(fixture);
  console.log(fixtureResult.trim());
  rcon.end();
}

async function call(operation, params = {}) {
  const result = (await bridge.call(operation, params)).result;
  console.log(operation, JSON.stringify(result).slice(0,600));
  return result;
}

async function action(operation, params) {
  const started = await call(operation, params);
  for (let i = 0; i < 500; i++) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    const state = (await bridge.call('get_action_status', { action_id: started.action_id })).result;
    if (state.status !== 'running') {
      console.log(`${operation}_done`, JSON.stringify(state).slice(0,400));
      if (state.status !== 'succeeded') throw new Error(`${operation}: ${state.error}`);
      return state;
    }
  }
  throw new Error(`${operation}: timeout`);
}

async function reference(name) {
  const observed = (await bridge.call('observe_nearby')).result;
  const found = observed.entities.find((entity) => entity.name === name);
  if (!found) throw new Error(`${name} not visible`);
  return found.target_ref;
}

async function mine(name, count) {
  return action('mine_entity', { target_ref: await reference(name), count });
}

async function transfer(name, role, item, count) {
  return call('transfer_item', {
    target_ref: await reference(name), direction: 'to_entity',
    inventory_role: role, item, count
  });
}

async function inventory(name) {
  return (await bridge.call('get_entity_inventory',
    { target_ref: await reference(name) })).result;
}

function amount(items, name) {
  return Array.isArray(items) ? items.find((item) => item.name === name)?.count ?? 0 : 0;
}

if (!resume) {
  await action('move_to', { surface: 'nauvis', x: 2, y: 2.5 });
  await mine('stone', 10);
  await mine('tree-01', 1);
  await action('move_to', { surface: 'nauvis', x: 4, y: 4 });
  await mine('coal', 3);
  await mine('iron-ore', 9);
  await call('get_inventory');
  await action('craft', { recipe: 'stone-furnace', count: 2 });
}
await call('observe_nearby');
await call('place_entity', { item: 'stone-furnace', surface: 'nauvis', x: 6, y: 4, direction: 0 });
await transfer('stone-furnace', 'fuel', 'wood', 2);
await transfer('stone-furnace', 'source', 'iron-ore', 9);
for (let i = 0; i < 70; i++) {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const furnace = await inventory('stone-furnace');
  const plates = amount(furnace.inventories.result, 'iron-plate');
  if (i % 5 === 0) console.log('bootstrap_plates', plates);
  if (plates >= 9) break;
  if (i === 69) throw new Error(`Only ${plates} bootstrap plates`);
}
await call('transfer_item', {
  target_ref: await reference('stone-furnace'), direction: 'from_entity',
  inventory_role: 'result', item: 'iron-plate', count: 9
});
await action('craft', { recipe: 'iron-gear-wheel', count: 3 });
await action('craft', { recipe: 'burner-mining-drill', count: 1 });
await call('get_inventory');
await call('place_entity', { item: 'burner-mining-drill', surface: 'nauvis', x: 6, y: 6, direction: 0 });
console.log('drill', JSON.stringify(await inventory('burner-mining-drill')));
await transfer('burner-mining-drill', 'fuel', 'coal', 2);
await transfer('stone-furnace', 'fuel', 'coal', 1);
for (let i = 0; i < 90; i++) {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const furnace = await inventory('stone-furnace');
  const plates = amount(furnace.inventories.result, 'iron-plate');
  if (i % 5 === 0) console.log('production_plates', plates);
  if (plates >= 10) {
    console.log('PRODUCTION_LINE_SUCCEEDED', plates);
    break;
  }
  if (i === 89) throw new Error(`Only ${plates} automated plates`);
}
