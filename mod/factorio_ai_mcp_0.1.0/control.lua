local PROTOCOL_VERSION = 1
local AGENT_ID = "agent-1"
local MAX_OBSERVATIONS = 100
local MAX_RECENT_OBSERVATIONS = 8
local OBSERVATION_RADIUS = 16
local MAP_CELL_SIZE = 8
local MAX_EXPLORED_CELLS = 16384
local TARGET_TTL_TICKS = 60 * 60
local ACTION_TIMEOUT_TICKS = 60 * 120
local ENTITY_STATUS_NAMES = {}
for name, value in pairs(defines.entity_status) do
  ENTITY_STATUS_NAMES[value] = name
end
local PLACEABLE_ITEMS = {
  ["wooden-chest"] = true, ["iron-chest"] = true,
  ["stone-furnace"] = true,
  ["burner-mining-drill"] = true, ["transport-belt"] = true,
  ["underground-belt"] = true, ["splitter"] = true,
  ["long-handed-inserter"] = true, ["pipe-to-ground"] = true,
  ["inserter"] = true, ["burner-inserter"] = true, ["small-electric-pole"] = true,
  ["assembling-machine-1"] = true, ["electric-mining-drill"] = true,
  ["pipe"] = true, ["offshore-pump"] = true, ["boiler"] = true,
  ["steam-engine"] = true, ["lab"] = true
}
local INVENTORY_ROLES = {
  ["wooden-chest"] = {main = defines.inventory.chest},
  ["iron-chest"] = {main = defines.inventory.chest},
  ["stone-furnace"] = {source = defines.inventory.crafter_input,
                       result = defines.inventory.crafter_output,
                       fuel = defines.inventory.fuel},
  ["burner-mining-drill"] = {fuel = defines.inventory.fuel},
  ["burner-inserter"] = {fuel = defines.inventory.fuel},
  ["boiler"] = {fuel = defines.inventory.fuel},
  ["lab"] = {input = defines.inventory.lab_input},
  ["assembling-machine-1"] = {source = defines.inventory.crafter_input,
                              result = defines.inventory.crafter_output}
}
local ROLE_ALIASES = {
  ["stone-furnace"] = {input = "source", output = "result"},
  ["assembling-machine-1"] = {input = "source", output = "result"},
  ["lab"] = {source = "input"}
}

local function ensure_storage()
  storage.ai_mcp = storage.ai_mcp or {
    spawned_once = false,
    character = nil,
    action = nil,
    craft_action = nil,
    last_action = nil,
    completed_actions = {},
    completed_order = {},
    hand_crafted = {},
    path_request_id = nil,
    next_action_id = 1,
    processed = {},
    processed_order = {},
    next_target_id = 1,
    targets = {},
    owned_refs = {},
    last_observation = nil,
    recent_observations = {},
    explored_map = {},
    last_heartbeat_tick = nil
  }
  storage.ai_mcp.next_action_id = storage.ai_mcp.next_action_id or 1
  storage.ai_mcp.next_target_id = storage.ai_mcp.next_target_id or 1
  storage.ai_mcp.targets = storage.ai_mcp.targets or {}
  if not storage.ai_mcp.owned_refs then
    -- Upgrade existing saves once. Keep the oldest ref for each owned unit;
    -- other pre-upgrade refs remain usable until their original TTL expires.
    local state = storage.ai_mcp
    state.owned_refs = {}
    local character = state.character
    if character and character.valid then
      for ref, target in pairs(state.targets) do
        local entity = target.entity
        if entity and entity.valid and entity.force == character.force and entity.unit_number then
          local previous = state.owned_refs[entity.unit_number]
          if not previous or tonumber(ref) < tonumber(previous) then
            state.owned_refs[entity.unit_number] = ref
          end
        end
      end
      for unit_number, ref in pairs(state.owned_refs) do
        state.targets[ref].owned = true
        state.targets[ref].unit_number = unit_number
      end
    end
  end
  storage.ai_mcp.explored_map = storage.ai_mcp.explored_map or {}
  if not storage.ai_mcp.recent_observations then
    storage.ai_mcp.recent_observations = {}
    if storage.ai_mcp.last_observation then
      storage.ai_mcp.recent_observations[1] = storage.ai_mcp.last_observation
    end
  end
  storage.ai_mcp.completed_actions = storage.ai_mcp.completed_actions or {}
  storage.ai_mcp.completed_order = storage.ai_mcp.completed_order or {}
  storage.ai_mcp.hand_crafted = storage.ai_mcp.hand_crafted or {}
  -- Preserve the ID and live crafting queue when loading a single-slot save.
  if storage.ai_mcp.action and storage.ai_mcp.action.type == "craft" and
     not storage.ai_mcp.craft_action then
    storage.ai_mcp.craft_action = storage.ai_mcp.action
    storage.ai_mcp.action = nil
  end
  return storage.ai_mcp
end

local function reply(request_id, ok, result, error_code)
  rcon.print(helpers.table_to_json({
    protocol_version = PROTOCOL_VERSION,
    request_id = request_id,
    ok = ok,
    tick = game.tick,
    result = result,
    error = error_code
  }))
end

local function position_data(entity)
  return {
    surface = entity.surface.name,
    x = entity.position.x,
    y = entity.position.y
  }
end

local function distance_squared(a, b)
  local dx = a.x - b.x
  local dy = a.y - b.y
  return dx * dx + dy * dy
end

local function range_detail(character, position, reach)
  return {distance = math.sqrt(distance_squared(character.position, position)), reach = reach}
end

local function conflict_detail(action)
  return {running_action = action and {id = action.id, type = action.type} or nil}
end

local function sorted_keys(values)
  local keys = {}
  for key in pairs(values) do keys[#keys + 1] = key end
  table.sort(keys)
  return keys
end

local function observation_detail(state)
  local observation = state.last_observation
  if not observation then return {} end
  return {observed = {x = observation.position.x, y = observation.position.y,
          surface = observation.surface, radius = observation.radius or OBSERVATION_RADIUS,
          age_seconds = (game.tick - observation.tick) / 60}}
end

local function valid_character(state)
  return state.character and state.character.valid and state.character.health > 0
end

local function stop_character(character)
  if not character or not character.valid then return end
  character.walking_state = {walking = false, direction = defines.direction.north}
  character.mining_state = {mining = false}
end

local function mining_inventory_count(character)
  local inventory = character.get_main_inventory()
  local count = inventory and inventory.get_item_count() or 0
  -- Include pending final products so completing crafts does not count as mining.
  -- Prerequisite products stay in the crafting queue for use by later recipes.
  for _, item in ipairs(character.crafting_queue or {}) do
    if not item.prerequisite then
      local recipe = character.force.recipes[item.recipe]
      if recipe then
        for _, product in pairs(recipe.products) do
          if product.type == "item" then
            count = count + item.count * (product.amount or 0)
          end
        end
      end
    end
  end
  return count
end

local function adjust_mining_inventory(state, before)
  if before and state.action and state.action.type == "mine" then
    state.action.last_inventory_count = state.action.last_inventory_count +
      mining_inventory_count(state.character) - before
  end
end

local function mining_estimated_seconds(character, action)
  local target = action.target
  if not character or not character.valid or not target or not target.valid then return nil end
  local prototype = target.prototype
  local character_prototype = character.prototype
  local properties = prototype and prototype.mineable_properties
  local base_speed = character_prototype and character_prototype.mining_speed
  if not properties or type(properties.mining_time) ~= "number" or
     not properties.products or type(base_speed) ~= "number" then return nil end
  -- LuaForce documents the 1 + modifier multiplier; the character's bonus is additive.
  local speed = base_speed * (1 + character.force.manual_mining_speed_modifier +
                             character.character_mining_speed_modifier)
  if speed <= 0 or properties.mining_time < 0 then return nil end
  local items = 0
  for _, product in pairs(properties.products) do
    -- gained counts items in the main inventory, not fluid products.
    if product.type == "item" then
      local amount = product.amount
      if amount == nil and product.amount_min ~= nil and product.amount_max ~= nil then
        amount = (product.amount_min + product.amount_max) / 2
      end
      if amount == nil then return nil end
      items = items + amount * (product.probability or 1)
    end
  end
  local operations = math.ceil(math.max(0, action.goal_count - action.gained) / math.max(1, items))
  local progress = 0
  if character.mining_state.mining and character.selected == target then
    progress = math.max(0, math.min(1, character.character_mining_progress))
  end
  local seconds = math.max(0, operations - progress) * properties.mining_time / speed
  if seconds ~= seconds or seconds == math.huge then return nil end
  return math.floor(seconds * 10 + 0.5) / 10
end

local function mine_partial_detail(character, action, detail)
  detail = detail or {}
  detail.gained = action.gained or 0
  detail.inventory = nil
  if character and character.valid then
    local inventory = character.get_main_inventory()
    if inventory and inventory.valid then
      -- Cancellation and early tick failures can precede the next progress update.
      local current = mining_inventory_count(character)
      detail.gained = detail.gained + math.max(0, current - (action.last_inventory_count or current))
      detail.inventory = inventory.get_contents()
    end
  end
  return detail
end

local function adjusted_goal_detail(action, detail)
  if not action or not action.goal_adjusted or not action.original_goal then return detail end
  detail = detail or {}
  detail.goal_adjusted = {x = action.goal_adjusted.x, y = action.goal_adjusted.y}
  detail.original_goal = {x = action.original_goal.x, y = action.original_goal.y}
  return detail
end

local function finish_action(state, slot, status, error_code, detail)
  local action = state[slot]
  if not action then return end
  if action.type == "move" or action.type == "mine" then
    stop_character(state.character)
  end
  if action.type == "mine" and status ~= "succeeded" then
    detail = mine_partial_detail(state.character, action, detail)
  end
  if action.type == "move" and (error_code == "blocked" or error_code == "timeout") and
     valid_character(state) then
    detail = detail or {}
    detail.distance = math.sqrt(distance_squared(state.character.position, action.goal))
    detail.goal = {x = action.goal.x, y = action.goal.y}
  end
  if action.type == "move" and action.within ~= nil then
    detail = detail or {}
    detail.within = action.within
    if state.character and state.character.valid then
      detail.distance = math.sqrt(distance_squared(state.character.position, action.goal))
    end
  end
  if action.type == "move" then detail = adjusted_goal_detail(action, detail) end
  action.status = status
  action.error = error_code
  action.detail = detail
  action.finished_tick = game.tick
  local completed = {
    id = action.id, type = action.type, status = status,
    error = error_code, detail = detail,
    goal_adjusted = action.goal_adjusted, original_goal = action.original_goal,
    position = valid_character(state) and position_data(state.character) or nil,
    started_tick = action.started_tick, finished_tick = game.tick
  }
  state.last_action = completed
  state.completed_actions[action.id] = completed
  state.completed_order[#state.completed_order + 1] = action.id
  if #state.completed_order > 256 then
    local oldest = table.remove(state.completed_order, 1)
    state.completed_actions[oldest] = nil
  end
  state[slot] = nil
  if action.type == "move" then state.path_request_id = nil end
end

local function finish_move_within(state)
  local action = state.action
  if not action or action.type ~= "move" or action.within == nil or
     not valid_character(state) then return false end
  if distance_squared(state.character.position, action.goal) > action.within * action.within then
    return false
  end
  finish_action(state, "action", "succeeded", nil, {position = position_data(state.character)})
  return true
end

local function spawn_character()
  local state = ensure_storage()
  if state.spawned_once then return end
  local surface = game.surfaces["nauvis"] or game.surfaces[1]
  local force = game.forces.player
  local center = force.get_spawn_position(surface)
  local position = surface.find_non_colliding_position("character", center, 12, 0.5)
  if not position then return end
  local character = surface.create_entity{
    name = "character", position = position, force = force
  }
  if not character then return end
  character.name_tag = "AI " .. AGENT_ID
  state.character = character
  state.spawned_once = true
end

local function inventory_contents(character)
  local inventory = character.get_main_inventory()
  if not inventory then return {} end
  return inventory.get_contents()
end

local function status(state)
  if not valid_character(state) then
    return {agent_id = AGENT_ID, state = state.spawned_once and "dead" or "absent"}
  end
  local character = state.character
  return {
    agent_id = AGENT_ID,
    state = "alive",
    position = position_data(character),
    health = character.health,
    inventory = inventory_contents(character),
    crafting_queue_size = character.crafting_queue_size,
    action = state.action and {id = state.action.id, type = state.action.type, status = state.action.status} or nil,
    craft_action = state.craft_action and {id = state.craft_action.id,
      type = state.craft_action.type, status = state.craft_action.status} or nil
  }
end

local function require_character(state)
  if not valid_character(state) then return nil, "character_dead" end
  return state.character
end

local function register_target(state, entity)
  local character = state.character
  local owned = character and character.valid and entity.force == character.force and
                entity.unit_number ~= nil
  if owned then
    local previous = state.owned_refs[entity.unit_number]
    if previous and state.targets[previous] and state.targets[previous].entity == entity then
      return previous
    end
  end
  local target_ref = tostring(state.next_target_id)
  state.next_target_id = state.next_target_id + 1
  state.targets[target_ref] = {entity = entity, tick = game.tick, owned = owned or nil,
                               unit_number = owned and entity.unit_number or nil}
  if owned then state.owned_refs[entity.unit_number] = target_ref end
  return target_ref
end

local function target_entity(character, params, max_distance, mining)
  local state = ensure_storage()
  if type(params.target_ref) == "string" then
    local reference = state.targets[params.target_ref]
    if not reference or (not reference.owned and game.tick - reference.tick > TARGET_TTL_TICKS) then
      return nil, "target_gone", {reason = "expired"}
    end
    local entity = reference.entity
    if not entity or not entity.valid then
      if reference.owned and state.owned_refs[reference.unit_number] == params.target_ref then
        state.owned_refs[reference.unit_number] = nil
      end
      state.targets[params.target_ref] = nil
      return nil, "target_gone", {reason = "entity_gone"}
    end
    if entity.surface ~= character.surface then return nil, "out_of_range" end
    if distance_squared(character.position, entity.position) > max_distance * max_distance then
      local reach = mining and (entity.type == "resource" and
                    character.resource_reach_distance or character.reach_distance) or max_distance
      return nil, "out_of_range", range_detail(character, entity.position, reach)
    end
    return entity
  end
  return nil, "invalid_input"
end

local function begin_action(state, slot, action)
  if state[slot] then return nil, "action_conflict", conflict_detail(state[slot]) end
  action.id = tostring(state.next_action_id)
  state.next_action_id = state.next_action_id + 1
  action.status = "running"
  action.started_tick = game.tick
  state[slot] = action
  return {action_id = action.id, status = action.status}
end

local function move_direction(dx, dy)
  local ax, ay = math.abs(dx), math.abs(dy)
  if ay > ax * 2 then
    return dy < 0 and defines.direction.north or defines.direction.south
  end
  if ax > ay * 2 then
    return dx < 0 and defines.direction.west or defines.direction.east
  end
  if dx >= 0 and dy < 0 then return defines.direction.northeast end
  if dx >= 0 and dy >= 0 then return defines.direction.southeast end
  if dx < 0 and dy >= 0 then return defines.direction.southwest end
  return defines.direction.northwest
end

local function path_direction(position, start, destination)
  local dx, dy = destination.x - position.x, destination.y - position.y
  local ax, ay = math.abs(dx), math.abs(dy)
  local sx, sy = dx < 0 and -1 or 1, dy < 0 and -1 or 1
  -- The heading lies between one cardinal direction and its adjacent diagonal.
  -- Unit vectors keep both nearest-heading and cross-track comparisons unbiased.
  local diagonal = sx > 0 and (sy > 0 and defines.direction.southeast or defines.direction.northeast)
                   or (sy > 0 and defines.direction.southwest or defines.direction.northwest)
  local cardinal, cx, cy
  if ax >= ay then
    cardinal, cx, cy = sx > 0 and defines.direction.east or defines.direction.west, sx, 0
  else
    cardinal, cx, cy = sy > 0 and defines.direction.south or defines.direction.north, 0, sy
  end
  local qx, qy = sx / math.sqrt(2), sy / math.sqrt(2)
  local lx, ly = destination.x - start.x, destination.y - start.y
  local length = math.sqrt(lx * lx + ly * ly)
  if length > 0 then
    local cross_track = (lx * (position.y - start.y) - ly * (position.x - start.x)) / length
    if math.abs(cross_track) > 0.1 then
      -- Choose the step that moves the signed distance back towards zero.
      -- This alternates the bracketing headings instead of drifting into corners.
      local cardinal_change = cross_track * (lx * cy - ly * cx)
      local diagonal_change = cross_track * (lx * qy - ly * qx)
      if math.abs(cardinal_change - diagonal_change) > 0.000001 then
        return cardinal_change < diagonal_change and cardinal or diagonal
      end
    end
  end
  return dx * cx + dy * cy >= dx * qx + dy * qy and cardinal or diagonal
end

local function reset_move_progress(action)
  action.last_waypoint_tick = game.tick
  action.final_best_distance = nil
  action.final_progress_distance = nil
  action.final_progress_tick = nil
end

local function goal_obstacles(character, goal)
  local surface = character.surface
  local prototype = character.prototype
  local layers = prototype.collision_mask.layers
  local box = prototype.collision_box
  local area = {left_top = {x = goal.x + box.left_top.x, y = goal.y + box.left_top.y},
                right_bottom = {x = goal.x + box.right_bottom.x, y = goal.y + box.right_bottom.y}}
  local tiles = {}
  for _, tile in pairs(surface.find_tiles_filtered{area = area}) do
    for layer in pairs(layers) do
      if tile.collides_with(layer) then tiles[tile.name] = true; break end
    end
  end
  local entities = {}
  for _, entity in pairs(surface.find_entities_filtered{area = area}) do
    if entity.valid and entity ~= character then
      local entity_box = entity.bounding_box
      if entity_box.left_top.x < area.right_bottom.x and
         entity_box.right_bottom.x > area.left_top.x and
         entity_box.left_top.y < area.right_bottom.y and
         entity_box.right_bottom.y > area.left_top.y then
        local entity_layers = entity.prototype.collision_mask.layers
        for layer in pairs(layers) do
          if entity_layers[layer] then
            entities[#entities + 1] = {name = entity.name, type = entity.type,
                                      x = entity.position.x, y = entity.position.y}
            break
          end
        end
      end
      if #entities == 5 then break end
    end
  end
  return sorted_keys(tiles), entities
end

local function path_not_found_detail(character, goal, action)
  local detail = {goal = {x = goal.x, y = goal.y}}
  if action then
    detail = adjusted_goal_detail(action, detail)
    if action.path_start_adjusted then
      local start = action.path_start_adjusted
      detail.start_adjusted = {x = start.x, y = start.y}
    end
  end
  -- A path response can arrive after the character has been removed.
  if not character or not character.valid then return detail end
  detail.distance = math.sqrt(distance_squared(character.position, goal))
  local tiles, entities = goal_obstacles(character, goal)
  if #tiles > 0 then detail.goal_tiles = tiles end
  if #entities > 0 then detail.goal_entities = entities end
  local free = character.surface.find_non_colliding_position("character", goal, 10, 0.5)
  if free then detail.nearby_free_position = {x = free.x, y = free.y} end
  return detail
end

local function path_start_blocked(character)
  -- Include touching edges and fixed-point rounding, not just strict overlaps.
  local box = character.bounding_box
  local epsilon = 0.01
  local area = {left_top = {x = box.left_top.x - epsilon, y = box.left_top.y - epsilon},
                right_bottom = {x = box.right_bottom.x + epsilon,
                                y = box.right_bottom.y + epsilon}}
  local surface = character.surface
  local layers = character.prototype.collision_mask.layers
  for _, entity in pairs(surface.find_entities_filtered{area = area}) do
    if entity.valid and entity ~= character then
      local entity_box = entity.bounding_box
      if entity_box.left_top.x < area.right_bottom.x and
         entity_box.right_bottom.x > area.left_top.x and
         entity_box.left_top.y < area.right_bottom.y and
         entity_box.right_bottom.y > area.left_top.y then
        local entity_layers = entity.prototype.collision_mask.layers
        for layer in pairs(layers) do
          if entity_layers[layer] then return true end
        end
      end
    end
  end
  for _, tile in pairs(surface.find_tiles_filtered{area = area}) do
    for layer in pairs(layers) do
      if tile.collides_with(layer) then return true end
    end
  end
  return false
end

local function request_path(state, character, goal, explicit_start)
  local action = state.action
  action.path = nil
  action.path_index = 1
  action.stuck_ticks = 0
  action.last_x, action.last_y = character.position.x, character.position.y
  reset_move_progress(action)
  local start = explicit_start or character.position
  action.path_start_adjusted = nil
  if not explicit_start and path_start_blocked(character) then
    -- Bound the search to 1.5 tiles, with 0.1-tile steps for narrow corridors.
    local free = character.surface.find_non_colliding_position("character", start, 1.5, 0.1)
    if free and distance_squared(start, free) > 0 then
      start = free
    end
  end
  if distance_squared(character.position, start) > 0 then
    action.path_start_adjusted = {x = start.x, y = start.y}
  end
  action.path_start_used = {x = start.x, y = start.y}
  local id = character.surface.request_path{
    bounding_box = character.prototype.collision_box,
    collision_mask = character.prototype.collision_mask,
    start = start,
    goal = goal,
    force = character.force,
    radius = action.within, -- double; nil preserves the API's default radius of 1.
    entity_to_ignore = character,
    can_open_gates = true
  }
  state.path_request_id = id
  return id
end

local function try_escape_path(state)
  local action = state.action
  if not action or action.escape_tried or not valid_character(state) then return false end
  local character = state.character
  local used_start = action.path_start_used or action.path_start_adjusted or character.position
  local free = character.surface.find_non_colliding_position(
    "character", character.position, 2, 0.5, true)
  if not free or distance_squared(character.position, free) == 0 or
     distance_squared(used_start, free) == 0 then
    return false
  end
  -- This belongs to the move action: busy retries and stuck repaths must not reset it.
  action.escape_tried = true
  request_path(state, character, action.goal, free)
  return true
end

local function do_move(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state.action) end
  if type(params.x) ~= "number" or type(params.y) ~= "number" or
     params.x ~= params.x or params.y ~= params.y or
     math.abs(params.x) > 1000000 or math.abs(params.y) > 1000000 then
    return nil, "invalid_input"
  end
  if params.within ~= nil and (type(params.within) ~= "number" or
     params.within ~= params.within or params.within < 0.5 or params.within > 10) then
    return nil, "invalid_input"
  end
  if params.surface ~= character.surface.name then return nil, "out_of_range" end
  local goal = {x = params.x, y = params.y}
  if distance_squared(character.position, goal) > 128 * 128 then
    return nil, "out_of_range", range_detail(character, goal, 128)
  end
  local original_goal, goal_adjusted
  if params.within == nil then
    -- Use the path diagnostic's character collision mask, ignoring this character
    -- itself so moving to the current position does not spuriously adjust the goal.
    local tiles, entities = goal_obstacles(character, goal)
    if #tiles > 0 or #entities > 0 then
      local free = character.surface.find_non_colliding_position("character", goal, 10, 0.5)
      if not free then return nil, "path_not_found", path_not_found_detail(character, goal) end
      original_goal = goal
      goal = {x = free.x, y = free.y}
      goal_adjusted = goal
    end
  end
  local result = begin_action(state, "action", {
    type = "move", goal = goal, within = params.within, path = nil, path_index = 1,
    original_goal = original_goal, goal_adjusted = goal_adjusted,
    last_x = character.position.x, last_y = character.position.y,
    stuck_ticks = 0, retries = 0
  })
  result = adjusted_goal_detail(state.action, result)
  if params.within ~= nil then result.within = params.within end
  if finish_move_within(state) then
    result.status = "succeeded"
    result.distance = math.sqrt(distance_squared(character.position, goal))
    return result
  end
  local ok = pcall(request_path, state, character, goal)
  if not ok then
    local detail = path_not_found_detail(character, goal, state.action)
    finish_action(state, "action", "failed", "path_not_found", detail)
    return nil, "path_not_found", detail
  end
  return result
end

local function do_mine(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state.action) end
  local count = params.count or 1
  if type(count) ~= "number" or count < 1 or count > 100 or count % 1 ~= 0 then
    return nil, "invalid_input"
  end
  local target, target_error, detail = target_entity(character, params,
    math.max(character.resource_reach_distance, character.reach_distance), true)
  if not target then return nil, target_error, detail end
  local reach = (target.type == "resource" or target.type == "tree" or
                 target.type == "simple-entity") and character.resource_reach_distance or
                character.reach_distance
  if target.type == "resource" and
     distance_squared(character.position, target.position) > reach * reach then
    return nil, "out_of_range", range_detail(character, target.position, reach)
  end
  if target.force ~= character.force and target.force.name ~= "neutral" then
    return nil, "invalid_target", {entity = target.name, reason = "other_force"}
  end
  if not target.minable then return nil, "invalid_target" end
  if not character.can_reach_entity(target) then
    return nil, "out_of_range", range_detail(character, target.position, reach)
  end
  local inventory = character.get_main_inventory()
  if not inventory or inventory.is_full() then return nil, "inventory_full" end
  local before = mining_inventory_count(character)
  character.selected = target
  if character.selected ~= target then
    character.selected = nil
    return nil, "invalid_target", {entity = target.name, reason = "not_selectable"}
  end
  local result = begin_action(state, "action", {
    type = "mine", target = target, reach = reach, goal_count = count, gained = 0,
    last_inventory_count = before,
    last_progress_tick = game.tick
  })
  character.mining_state = {mining = true, position = target.position}
  result.estimated_seconds = mining_estimated_seconds(character, state.action)
  return result
end

local function string_filter(value)
  if value == nil then return nil end
  if type(value) ~= "table" or #value > 32 then return nil, "invalid_input" end
  local set = {}
  local count = 0
  for key, name in pairs(value) do
    if type(key) ~= "number" or key % 1 ~= 0 or key < 1 or key > #value or
       type(name) ~= "string" or #name == 0 or #name > 200 then
      return nil, "invalid_input"
    end
    count = count + 1
    set[name] = true
  end
  if count ~= #value then return nil, "invalid_input" end
  return set
end

local function preview_fluid_connections(prototype, position, direction)
  local connections = {}
  local vectors = {
    [0] = {x = 0, y = -1}, [4] = {x = 1, y = 0},
    [8] = {x = 0, y = 1}, [12] = {x = -1, y = 0}
  }
  for index, fluidbox in ipairs(prototype.fluidbox_prototypes or {}) do
    for _, connection in ipairs(fluidbox.pipe_connections) do
      if connection.connection_type == "normal" then
        -- The API supplies an offset for each cardinal entity direction.
        local offset = connection.positions[direction / 4 + 1]
        local port_direction = (connection.direction + direction) % 16
        local vector = vectors[port_direction]
        if offset and vector then
          local port = {x = position.x + offset.x, y = position.y + offset.y}
          connections[#connections + 1] = {
            position = port,
            target_position = {x = port.x + vector.x, y = port.y + vector.y},
            direction = port_direction, flow = connection.flow_direction, fluidbox_index = index
          }
        end
      end
    end
  end
  if #connections > 0 then return connections end
end

local function placed_fluid_connections(entity)
  local connections = {}
  local fluidbox = entity.fluidbox
  if not fluidbox then return nil end
  for index = 1, #fluidbox do
    for _, connection in ipairs(fluidbox.get_pipe_connections(index)) do
      if connection.connection_type == "normal" or connection.connection_type == "underground" then
        local position, target = connection.position, connection.target_position
        local dx, dy = target.x - position.x, target.y - position.y
        local direction
        if dx > 0 then direction = defines.direction.east
        elseif dx < 0 then direction = defines.direction.west
        elseif dy > 0 then direction = defines.direction.south
        else direction = defines.direction.north end
        connections[#connections + 1] = {
          position = {x = position.x, y = position.y},
          target_position = {x = target.x, y = target.y},
          direction = direction, flow = connection.flow_direction, fluidbox_index = index,
          connected = connection.target ~= nil, connection_type = connection.connection_type
        }
      end
    end
  end
  if #connections > 0 then return connections end
end

local function offshore_pump_directions(name, connections)
  if name ~= "offshore-pump" then return nil, nil end
  for _, connection in ipairs(connections or {}) do
    if connection.flow == "output" then
      -- Base pump: south-facing output, north-facing water; rotate with the entity.
      return (connection.direction + 8) % 16, connection.direction
    end
  end
end

local function map_cell_key(cx, cy)
  return cx .. "," .. cy
end

local function map_cell_centre(cell)
  return {x = (cell.cx + 0.5) * MAP_CELL_SIZE, y = (cell.cy + 0.5) * MAP_CELL_SIZE}
end

local function nearest_sample(previous, candidate, origin)
  if not candidate then return previous end
  if not previous then return {x = candidate.x, y = candidate.y} end
  local old_distance = distance_squared(previous, origin)
  local new_distance = distance_squared(candidate, origin)
  if new_distance < old_distance or (new_distance == old_distance and
     (candidate.x < previous.x or (candidate.x == previous.x and candidate.y < previous.y))) then
    return {x = candidate.x, y = candidate.y}
  end
  return previous
end

local function record_explored_map(state, character, radius, entities)
  local surface, position = character.surface, character.position
  local map = state.explored_map[surface.name]
  if not map then
    map = {cells = {}, count = 0}
    state.explored_map[surface.name] = map
  end
  local fresh = {}
  for cx = math.floor((position.x - radius) / MAP_CELL_SIZE),
           math.floor((position.x + radius) / MAP_CELL_SIZE) do
    for cy = math.floor((position.y - radius) / MAP_CELL_SIZE),
             math.floor((position.y + radius) / MAP_CELL_SIZE) do
      local left, top = cx * MAP_CELL_SIZE, cy * MAP_CELL_SIZE
      local right, bottom = left + MAP_CELL_SIZE, top + MAP_CELL_SIZE
      -- A rectangle is fully inside a circle exactly when all four corners are inside.
      if distance_squared(position, {x = left, y = top}) <= radius * radius and
         distance_squared(position, {x = right, y = top}) <= radius * radius and
         distance_squared(position, {x = left, y = bottom}) <= radius * radius and
         distance_squared(position, {x = right, y = bottom}) <= radius * radius then
        local cell = {cx = cx, cy = cy, tick = game.tick, resources = {},
                      water = 0, trees = 0, rocks = 0}
        local centre = map_cell_centre(cell)
        local water = surface.find_tiles_filtered{
          area = {{left, top}, {right, bottom}}, collision_mask = "water_tile"
        }
        for _, tile in pairs(water) do
          local x, y = tile.position.x, tile.position.y
          -- Keep the cell half-open even if a tile search includes a boundary tile.
          if x >= left and x < right and y >= top and y < bottom then
            cell.water = cell.water + 1
            if not surface.get_tile(x - 1, y).collides_with("water_tile") or
               not surface.get_tile(x + 1, y).collides_with("water_tile") or
               not surface.get_tile(x, y - 1).collides_with("water_tile") or
               not surface.get_tile(x, y + 1).collides_with("water_tile") then
              cell.shore = nearest_sample(cell.shore, {x = x + 0.5, y = y + 0.5}, centre)
            end
          end
        end
        fresh[map_cell_key(cx, cy)] = cell
      end
    end
  end
  -- Use the original search, before any observation filters or paging are applied.
  for _, entity in pairs(entities) do
    if entity.valid then
      local position = entity.position
      local cell = fresh[map_cell_key(math.floor(position.x / MAP_CELL_SIZE),
                                      math.floor(position.y / MAP_CELL_SIZE))]
      if cell then
        local centre = map_cell_centre(cell)
        if entity.type == "resource" then
          local resource = cell.resources[entity.name] or {tiles = 0, amount = 0}
          resource.tiles = resource.tiles + 1
          resource.amount = resource.amount + entity.amount
          resource.sample = nearest_sample(resource.sample, position, centre)
          cell.resources[entity.name] = resource
        elseif entity.type == "tree" then
          cell.trees = cell.trees + 1
          cell.tree_sample = nearest_sample(cell.tree_sample, position, centre)
        elseif entity.type == "simple-entity" and string.find(entity.name, "rock", 1, true) then
          cell.rocks = cell.rocks + 1
          cell.rock_sample = nearest_sample(cell.rock_sample, position, centre)
        end
      end
    end
  end
  for key, cell in pairs(fresh) do
    if not map.cells[key] then map.count = map.count + 1 end
    map.cells[key] = cell
  end
  if map.count > MAX_EXPLORED_CELLS then
    local oldest = {}
    for key, cell in pairs(map.cells) do
      oldest[#oldest + 1] = {key = key, tick = cell.tick}
    end
    table.sort(oldest, function(a, b)
      if a.tick ~= b.tick then return a.tick < b.tick end
      return a.key < b.key
    end)
    for index = 1, map.count - MAX_EXPLORED_CELLS do
      map.cells[oldest[index].key] = nil
      map.count = map.count - 1
    end
  end
end

local function extend_map_bounds(bounds, cell)
  local left, top = cell.cx * MAP_CELL_SIZE, cell.cy * MAP_CELL_SIZE
  if not bounds.left_top then
    bounds.left_top = {x = left, y = top}
    bounds.right_bottom = {x = left + MAP_CELL_SIZE, y = top + MAP_CELL_SIZE}
  else
    bounds.left_top.x = math.min(bounds.left_top.x, left)
    bounds.left_top.y = math.min(bounds.left_top.y, top)
    bounds.right_bottom.x = math.max(bounds.right_bottom.x, left + MAP_CELL_SIZE)
    bounds.right_bottom.y = math.max(bounds.right_bottom.y, top + MAP_CELL_SIZE)
  end
end

local function map_components(cells, kind, name, position)
  local remaining, components = {}, {}
  for key, cell in pairs(cells) do
    if (kind == "resources" and cell.resources[name]) or
       (kind ~= "resources" and cell[kind] > 0) then
      remaining[key] = cell
    end
  end
  for key in pairs(cells) do
    local seed = remaining[key]
    if seed then
      local group = {name = name, cells = 0, observed_tick = seed.tick}
      local queue, index = {seed}, 1
      remaining[key] = nil
      local nearest_centre
      while index <= #queue do
        local cell = queue[index]
        index = index + 1
        group.cells = group.cells + 1
        group.observed_tick = math.min(group.observed_tick, cell.tick)
        extend_map_bounds(group, cell)
        if kind == "resources" then
          local resource = cell.resources[name]
          group.tiles = (group.tiles or 0) + resource.tiles
          group.total_amount = (group.total_amount or 0) + resource.amount
          group.nearest = nearest_sample(group.nearest, resource.sample, position)
        elseif kind == "water" then
          group.tiles = (group.tiles or 0) + cell.water
          group.nearest_shore = nearest_sample(group.nearest_shore, cell.shore, position)
          nearest_centre = nearest_sample(nearest_centre, map_cell_centre(cell), position)
        else
          group.count = (group.count or 0) + cell[kind]
          local sample = kind == "trees" and cell.tree_sample or cell.rock_sample
          group.nearest = nearest_sample(group.nearest, sample, position)
        end
        for dx = -1, 1 do
          for dy = -1, 1 do
            if dx ~= 0 or dy ~= 0 then
              local neighbour_key = map_cell_key(cell.cx + dx, cell.cy + dy)
              local neighbour = remaining[neighbour_key]
              if neighbour then
                queue[#queue + 1] = neighbour
                remaining[neighbour_key] = nil
              end
            end
          end
        end
      end
      local nearest = group.nearest or group.nearest_shore or nearest_centre
      group.distance = math.sqrt(distance_squared(position, nearest))
      components[#components + 1] = group
    end
  end
  return components
end

local function do_get_explored_map(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  local limit = params.limit == nil and 10 or params.limit
  if type(limit) ~= "number" or limit % 1 ~= 0 or limit < 1 or limit > 50 then
    return nil, "invalid_input"
  end
  local names, names_error = string_filter(params.resource_names)
  if names_error then return nil, "invalid_input" end
  local map = state.explored_map[character.surface.name] or {cells = {}, count = 0}
  local result = {position = position_data(character), cell_size = MAP_CELL_SIZE,
                  explored = {cells = map.count}, resources = {}}
  local resource_names = {}
  for _, cell in pairs(map.cells) do
    extend_map_bounds(result.explored, cell)
    for name in pairs(cell.resources) do
      if not names or names[name] then resource_names[name] = true end
    end
  end
  for name in pairs(resource_names) do
    for _, group in ipairs(map_components(map.cells, "resources", name, character.position)) do
      result.resources[#result.resources + 1] = group
    end
  end
  for _, kind in ipairs({"water", "trees", "rocks"}) do
    result[kind] = map_components(map.cells, kind, nil, character.position)
  end
  for _, kind in ipairs({"resources", "water", "trees", "rocks"}) do
    local groups = result[kind]
    result[kind .. "_total"] = #groups
    table.sort(groups, function(a, b)
      if a.distance ~= b.distance then return a.distance < b.distance end
      if a.name ~= b.name then return a.name < b.name end
      if a.left_top.x ~= b.left_top.x then return a.left_top.x < b.left_top.x end
      return a.left_top.y < b.left_top.y
    end)
    for index = #groups, limit + 1, -1 do groups[index] = nil end
  end
  return result
end

local function lab_science_units(input, unrounded)
  local units_by_name = {}
  for index = 1, #input do
    local stack = input[index]
    if stack.valid_for_read and stack.type == "tool" then
      local maximum = stack.prototype.get_durability(stack.quality)
      if maximum and maximum > 0 then
        units_by_name[stack.name] = (units_by_name[stack.name] or 0) +
                                    stack.count - 1 + stack.durability / maximum
      end
    end
  end
  local units = {}
  for _, name in ipairs(sorted_keys(units_by_name)) do
    local amount = units_by_name[name]
    units[#units + 1] = {name = name,
                        units = unrounded and amount or math.floor(amount * 1000 + 0.5) / 1000}
  end
  return #units > 0 and units or nil
end

local function do_research_labs(state)
  local character, err = require_character(state)
  if not character then return nil, err end
  local force = character.force
  local current = force.current_research
  local remaining = current and current.research_unit_count * (1 - force.research_progress) or nil
  local required = {}
  if current then
    for _, ingredient in pairs(current.research_unit_ingredients) do
      required[#required + 1] = ingredient.name
    end
    table.sort(required)
  end
  local entities = character.surface.find_entities_filtered{
    position = character.position, radius = OBSERVATION_RADIUS, type = "lab", force = force
  }
  local labs = {}
  local available = {}
  for _, entity in pairs(entities) do
    -- Also check centre distance: the surface query may intersect a lab's bounding box.
    if entity.valid and distance_squared(character.position, entity.position) <=
       OBSERVATION_RADIUS ^ 2 then
      local input = entity.get_inventory(defines.inventory.lab_input)
      local units = input and input.valid and lab_science_units(input, true) or {}
      local by_name = {}
      for _, entry in ipairs(units) do by_name[entry.name] = entry.units end
      local missing = {}
      local holds_required = false
      for _, name in ipairs(required) do
        if (by_name[name] or 0) > 0 then
          holds_required = true
        else
          missing[#missing + 1] = name
        end
      end
      -- Packs stranded in an incomplete lab cannot contribute to this research.
      -- Power is reported separately; unpowered labs still hold usable packs.
      if #missing == 0 then
        for _, name in ipairs(required) do
          available[name] = (available[name] or 0) + (by_name[name] or 0)
        end
      end
      labs[#labs + 1] = {
        target_ref = register_target(state, entity), x = entity.position.x, y = entity.position.y,
        status = ENTITY_STATUS_NAMES[entity.status], science_units = units, missing = missing,
        blocking = holds_required and #missing > 0
      }
    end
  end
  table.sort(labs, function(a, b)
    local da = distance_squared(character.position, a)
    local db = distance_squared(character.position, b)
    if da ~= db then return da < db end
    if a.x ~= b.x then return a.x < b.x end
    return a.y < b.y
  end)
  local short = {}
  for _, name in ipairs(required) do
    local amount = available[name] or 0
    if amount <= remaining then
      short[#short + 1] = {name = name, available_units = amount, remaining_units = remaining}
    end
  end
  return {technology = current and current.name or nil, remaining_units = remaining,
          labs = labs, short_science = short}
end

local function do_observe(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  local entity_offset = params.entity_offset or 0
  local tile_offset = params.tile_offset or 0
  if type(entity_offset) ~= "number" or entity_offset < 0 or entity_offset % 1 ~= 0 or
     type(tile_offset) ~= "number" or tile_offset < 0 or tile_offset % 1 ~= 0 then
    return nil, "invalid_input"
  end
  local radius = params.radius == nil and OBSERVATION_RADIUS or params.radius
  if type(radius) ~= "number" or radius ~= radius or radius < 1 or radius > 16 then
    return nil, "invalid_input"
  end
  for _, name in ipairs({"owned_only", "include_resources", "include_tiles"}) do
    if params[name] ~= nil and type(params[name]) ~= "boolean" then
      return nil, "invalid_input"
    end
  end
  local types, types_error = string_filter(params.types)
  local names, names_error = string_filter(params.names)
  if types_error or names_error then return nil, "invalid_input" end
  local filters = {radius = radius, types = params.types, names = params.names,
                   owned_only = params.owned_only == true,
                   include_resources = params.include_resources ~= false,
                   include_tiles = params.include_tiles ~= false}
  local entities = character.surface.find_entities_filtered{
    position = character.position, radius = radius
  }
  record_explored_map(state, character, radius, entities)
  local visible = {}
  local resources_by_name = {}
  for _, entity in pairs(entities) do
    if entity.valid and entity ~= character and
       (not types or types[entity.type]) and (not names or names[entity.name]) and
       (not filters.owned_only or entity.force == character.force) and
       (filters.include_resources or entity.type ~= "resource") then
      visible[#visible + 1] = entity
    end
    if entity.valid and entity.type == "resource" and
       distance_squared(character.position, entity.position) <=
         radius * radius then
      local resource = resources_by_name[entity.name]
      local position = entity.position
      if not resource then
        resource = {name = entity.name, tiles = 0, total_amount = 0,
                    left_top = {x = position.x, y = position.y},
                    right_bottom = {x = position.x, y = position.y}}
        resources_by_name[entity.name] = resource
      end
      resource.tiles = resource.tiles + 1
      resource.total_amount = resource.total_amount + entity.amount
      resource.left_top.x = math.min(resource.left_top.x, position.x)
      resource.left_top.y = math.min(resource.left_top.y, position.y)
      resource.right_bottom.x = math.max(resource.right_bottom.x, position.x)
      resource.right_bottom.y = math.max(resource.right_bottom.y, position.y)
    end
  end
  local resources = {}
  for _, resource in pairs(resources_by_name) do resources[#resources + 1] = resource end
  table.sort(resources, function(a, b) return a.name < b.name end)
  table.sort(visible, function(a, b)
    local da = distance_squared(character.position, a.position)
    local db = distance_squared(character.position, b.position)
    if da ~= db then return da < db end
    if a.position.x ~= b.position.x then return a.position.x < b.position.x end
    if a.position.y ~= b.position.y then return a.position.y < b.position.y end
    return a.name < b.name
  end)
  local output = {}
  local current_research = character.force.current_research
  state.last_observation = {position = {x = character.position.x, y = character.position.y},
                            surface = character.surface.name, tick = game.tick, radius = radius}
  state.recent_observations[#state.recent_observations + 1] = state.last_observation
  if #state.recent_observations > MAX_RECENT_OBSERVATIONS then
    table.remove(state.recent_observations, 1)
  end
  for index = entity_offset + 1, math.min(entity_offset + MAX_OBSERVATIONS, #visible) do
    local entity = visible[index]
    local observed = {
      target_ref = register_target(state, entity),
      name = entity.name, type = entity.type,
      x = entity.position.x, y = entity.position.y,
      distance = math.sqrt(distance_squared(character.position, entity.position)),
      surface = entity.surface.name, unit_number = entity.unit_number,
      amount = entity.type == "resource" and entity.amount or nil,
      owned = entity.force == character.force
    }
    local roles = INVENTORY_ROLES[entity.name]
    if observed.owned and roles then
      local inventory_roles = sorted_keys(roles)
      if #inventory_roles > 0 then observed.inventory_roles = inventory_roles end
    end
    if observed.owned and entity.type == "lab" then
      local input = entity.get_inventory(defines.inventory.lab_input)
      if input and input.valid then
        observed.science_units = lab_science_units(input)
        if current_research then
          local missing = {}
          for _, ingredient in pairs(current_research.research_unit_ingredients) do
            if input.get_item_count(ingredient.name) == 0 then
              missing[#missing + 1] = ingredient.name
            end
          end
          table.sort(missing)
          if #missing > 0 then observed.missing_science = missing end
        end
      end
    end
    if entity.supports_direction then observed.direction = entity.direction end
    local entity_status = entity.status
    if entity_status ~= nil then observed.status = ENTITY_STATUS_NAMES[entity_status] end
    if entity.type ~= "resource" then
      local box = entity.bounding_box
      observed.bounding_box = {
        left_top = {x = box.left_top.x, y = box.left_top.y},
        right_bottom = {x = box.right_bottom.x, y = box.right_bottom.y}
      }
    end
    if entity.type == "assembling-machine" or entity.type == "furnace" then
      local recipe = entity.get_recipe()
      if recipe then observed.recipe = recipe.name end
    end
    if entity.type == "inserter" then observed.pickup_position = entity.pickup_position end
    if entity.type == "inserter" or entity.type == "mining-drill" then
      observed.drop_position = entity.drop_position
    end
    local fluids = {}
    local fluidbox = entity.fluidbox
    for fluid_index = 1, #fluidbox do
      local fluid = fluidbox[fluid_index]
      if fluid then fluids[#fluids + 1] = {name = fluid.name, amount = fluid.amount} end
    end
    if #fluids > 0 then observed.fluids = fluids end
    if observed.owned then observed.fluid_connections = placed_fluid_connections(entity) end
    if entity.name == "offshore-pump" then
      observed.water_direction, observed.output_direction = offshore_pump_directions(
        entity.name, observed.fluid_connections or placed_fluid_connections(entity))
    end
    if entity.type == "electric-pole" or entity.prototype.electric_energy_source_prototype then
      -- Some entity types cannot expose a network ID even with an electric source.
      local ok, network_id = pcall(function() return entity.electric_network_id end)
      if ok and network_id ~= nil then observed.electric_network_id = network_id end
    end
    if entity.type == "underground-belt" then
      observed.underground_type = entity.belt_to_ground_type
      local neighbour = entity.neighbours
      if neighbour and neighbour.valid then
        observed.neighbour = {x = neighbour.position.x, y = neighbour.position.y}
      end
    end
    if entity.type == "transport-belt" or entity.type == "underground-belt" or
       entity.type == "splitter" then
      local merged = {}
      local items = {}
      for line = 1, entity.get_max_transport_line_index() do
        for _, item in pairs(entity.get_transport_line(line).get_contents()) do
          merged[item.name] = merged[item.name] or {}
          local entry = merged[item.name][item.quality]
          if not entry then
            entry = {name = item.name, quality = item.quality, count = 0}
            merged[item.name][item.quality] = entry
            items[#items + 1] = entry
          end
          entry.count = entry.count + item.count
        end
      end
      table.sort(items, function(a, b)
        if a.name ~= b.name then return a.name < b.name end
        return a.quality < b.quality
      end)
      if #items > 0 then observed.belt_items = items end
    end
    output[#output + 1] = observed
  end
  local blocked = filters.include_tiles and character.surface.find_tiles_filtered{
    position = character.position, radius = radius, collision_mask = "player"
  } or {}
  table.sort(blocked, function(a, b)
    if a.position.x ~= b.position.x then return a.position.x < b.position.x end
    return a.position.y < b.position.y
  end)
  local blocked_output = {}
  for index = tile_offset + 1, math.min(tile_offset + 256, #blocked) do
    local tile = blocked[index]
    blocked_output[#blocked_output + 1] = {
      x = tile.position.x, y = tile.position.y, name = tile.name
    }
  end
  for key, reference in pairs(state.targets) do
    if reference.owned then
      if reference.entity and not reference.entity.valid then
        if state.owned_refs[reference.unit_number] == key then
          state.owned_refs[reference.unit_number] = nil
        end
        -- Keep a tombstone until lookup reports entity_gone, then remove it.
        reference.entity = nil
      end
    elseif game.tick - reference.tick > TARGET_TTL_TICKS then
      state.targets[key] = nil
    end
  end
  return {position = position_data(character), radius = radius, filters = filters,
          reach = {build = character.build_distance, interact = character.reach_distance,
                   mine_resource = character.resource_reach_distance},
          entities = output, entity_total = #visible, resources = resources,
          next_entity_offset = entity_offset + #output < #visible and
                               entity_offset + #output or nil,
          blocked_tiles = blocked_output, blocked_tile_total = #blocked,
          next_tile_offset = tile_offset + #blocked_output < #blocked and
                             tile_offset + #blocked_output or nil}
end

local function hand_craftable(character, recipe)
  return recipe.enabled and not recipe.hidden and recipe.name ~= "recipe-unknown" and
    not recipe.prototype.hidden_from_player_crafting and
    not recipe.prototype.hide_from_player_crafting and
    character.prototype.crafting_categories[recipe.category] and
    not character.force.get_hand_crafting_disabled_for_recipe(recipe.name) or false
end

local function missing_raw_materials(character, recipe, count)
  -- Work on a snapshot only: reporting a shortage must not consume any items.
  local held = {}
  local inventory = character.get_main_inventory()
  if inventory and inventory.valid then
    for _, item in pairs(inventory.get_contents()) do
      if item.quality == "normal" then
        held[item.name] = (held[item.name] or 0) + item.count
      end
    end
  end

  local recipes = {}
  for name, candidate in pairs(character.force.recipes) do
    if hand_craftable(character, candidate) then
      local main_product = candidate.prototype.main_product
      for _, product in pairs(candidate.products) do
        local amount = product.amount or product.amount_min
        if product.type == "item" and amount and amount > 0 and
           (name == product.name or
            (main_product and main_product.type == "item" and
             main_product.name == product.name)) then
          local previous = recipes[product.name]
          if not previous or
             (previous.recipe.name ~= product.name and
              (name == product.name or name < previous.recipe.name)) then
            recipes[product.name] = {recipe = candidate, amount = amount}
          end
        end
      end
    end
  end

  local missing = {}
  local function need(item, amount, depth)
    local taken = math.min(held[item] or 0, amount)
    held[item] = (held[item] or 0) - taken
    local remainder = amount - taken
    if remainder <= 0 then return end
    local producer = recipes[item]
    if depth < 5 and producer then
      local runs = math.ceil(remainder / producer.amount)
      for _, ingredient in pairs(producer.recipe.ingredients) do
        if ingredient.type == "item" then
          need(ingredient.name, ingredient.amount * runs, depth + 1)
        end
      end
    else
      missing[item] = (missing[item] or 0) + remainder
    end
  end

  for _, ingredient in pairs(recipe.ingredients) do
    if ingredient.type == "item" then
      need(ingredient.name, ingredient.amount * count, 0)
    end
  end
  local output = {}
  for _, name in ipairs(sorted_keys(missing)) do
    output[#output + 1] = {name = name, count = missing[name]}
  end
  return #output > 0 and output or nil
end

local function do_recipe_info(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if type(params.recipe) ~= "string" then return nil, "invalid_input" end
  local recipe = character.force.recipes[params.recipe]
  if not recipe then return nil, "invalid_target" end
  return {name = recipe.name, enabled = recipe.enabled, category = recipe.category,
          energy = recipe.energy, ingredients = recipe.ingredients, products = recipe.products,
          hand_craftable = hand_craftable(character, recipe)}
end

local function do_list_craftable(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  local offset = params.offset or 0
  local limit = params.limit or 50
  if type(offset) ~= "number" or offset < 0 or offset % 1 ~= 0 or
     type(limit) ~= "number" or limit < 1 or limit > 100 or limit % 1 ~= 0 then
    return nil, "invalid_input"
  end
  local output = {}
  for name, recipe in pairs(character.force.recipes) do
    if hand_craftable(character, recipe) then
      local count = character.get_craftable_count(name)
      output[#output + 1] = {
        name = name, craftable_count = count, category = recipe.category,
        ingredients = recipe.ingredients, products = recipe.products,
        energy = recipe.energy
      }
    end
  end
  table.sort(output, function(a, b) return a.name < b.name end)
  local total = #output
  local page = {}
  for index = offset + 1, math.min(offset + limit, total) do
    page[#page + 1] = output[index]
  end
  return {recipes = page, total = total,
          next_offset = offset + #page < total and offset + #page or nil}
end

local function research_available(technology)
  if not technology.enabled or technology.researched then return false end
  for _, prerequisite in pairs(technology.prerequisites) do
    if not prerequisite.researched then return false end
  end
  return true
end

local function research_trigger_data(technology)
  local trigger = technology.prototype.research_trigger
  if not trigger then return nil end
  local item = trigger.item
  -- ItemIDFilter uses a name; ItemIDAndQualityIDPair can return a LuaItemPrototype.
  if item and type(item) ~= "string" then
    item = item.name
    if item and type(item) ~= "string" then item = item.name end
  end
  return {type = trigger.type, item = item,
          count = trigger.count or (trigger.type == "craft-item" and 1 or nil)}
end

local function do_technology_info(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if type(params.technology) ~= "string" then return nil, "invalid_input" end
  local technology = character.force.technologies[params.technology]
  if not technology then
    return nil, "invalid_target", {technology = params.technology, reason = "unknown"}
  end
  local prerequisites, ingredients, unlocks = {}, {}, {}
  for name, prerequisite in pairs(technology.prerequisites) do
    prerequisites[#prerequisites + 1] = {name = name, researched = prerequisite.researched}
  end
  for _, ingredient in pairs(technology.research_unit_ingredients) do
    ingredients[#ingredients + 1] = {name = ingredient.name, amount = ingredient.amount}
  end
  for _, effect in pairs(technology.prototype.effects or {}) do
    if effect.type == "unlock-recipe" and effect.recipe then
      unlocks[#unlocks + 1] = effect.recipe
    end
  end
  table.sort(prerequisites, function(a, b) return a.name < b.name end)
  table.sort(ingredients, function(a, b) return a.name < b.name end)
  table.sort(unlocks)
  return {name = technology.name, researched = technology.researched,
          available = research_available(technology), enabled = technology.enabled,
          prerequisites = prerequisites, unit_count = technology.research_unit_count,
          unit_time = technology.research_unit_energy / 60, ingredients = ingredients,
          trigger = research_trigger_data(technology), unlocks_recipes = unlocks}
end

local function record_hand_crafted(state, character, action)
  local recipe = prototypes.recipe[action.recipe]
  if not recipe then return end
  for _, product in pairs(recipe.products) do
    if product.type == "item" and product.amount and (product.probability or 1) == 1 then
      state.hand_crafted[product.name] = (state.hand_crafted[product.name] or 0) +
                                         product.amount * action.count
    end
  end
  for _, technology in pairs(character.force.technologies) do
    local trigger = research_trigger_data(technology)
    if trigger and trigger.type == "craft-item" and trigger.item and
       research_available(technology) and
       (state.hand_crafted[trigger.item] or 0) >= trigger.count then
      -- Player-less characters do not advance the engine's hand crafting triggers.
      -- Only successful craft completion calls this; lab research is never completed here.
      technology.researched = true
    end
  end
end

local function research_queue(force)
  local queue = {}
  for _, technology in ipairs(force.research_queue or {}) do
    queue[#queue + 1] = technology.name
  end
  return queue
end

local function do_start_research(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if type(params.technology) ~= "string" then return nil, "invalid_input" end
  local force = character.force
  local technology = force.technologies[params.technology]
  local detail = {technology = params.technology}
  if not technology then
    detail.reason = "unknown"
  elseif technology.researched then
    detail.reason = "researched"
  elseif not technology.enabled then
    detail.reason = "not_enabled"
  else
    local missing = {}
    for name, prerequisite in pairs(technology.prerequisites) do
      if not prerequisite.researched then missing[#missing + 1] = name end
    end
    if #missing > 0 then
      table.sort(missing)
      detail.reason = "missing_prerequisites"
      detail.missing_prerequisites = missing
    end
  end
  if detail.reason then return nil, "invalid_target", detail end
  if not force.add_research(params.technology) then
    detail.reason = "queue_rejected"
    return nil, "invalid_target", detail
  end
  return {technology = technology.name,
          current = force.current_research and force.current_research.name or nil,
          queue = research_queue(force)}
end

local function do_research(state)
  local character, err = require_character(state)
  if not character then return nil, err end
  local force = character.force
  local researched = {}
  local available = {}
  for name, technology in pairs(force.technologies) do
    if technology.researched then researched[#researched + 1] = name end
    if research_available(technology) then
      local ingredients = {}
      for _, ingredient in pairs(technology.research_unit_ingredients) do
        ingredients[#ingredients + 1] = {name = ingredient.name, amount = ingredient.amount}
      end
      available[#available + 1] = {name = name, unit_count = technology.research_unit_count,
                                  unit_time = technology.research_unit_energy / 60,
                                  ingredients = ingredients,
                                  trigger = research_trigger_data(technology)}
    end
  end
  table.sort(researched)
  table.sort(available, function(a, b) return a.name < b.name end)
  while #available > 100 do table.remove(available) end
  local current = force.current_research
  local progress = current and force.research_progress or nil
  return {
    researched = researched,
    queue = research_queue(force), available = available,
    current = current and current.name or nil,
    progress = progress,
    -- Use the force's technology, including its current level's count-formula cost.
    remaining_units = current and current.research_unit_count * (1 - progress) or nil
  }
end

local function do_craft(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.craft_action then return nil, "action_conflict", conflict_detail(state.craft_action) end
  if type(params.recipe) ~= "string" or type(params.count) ~= "number" or
     params.count < 1 or params.count > 100 or params.count % 1 ~= 0 then
    return nil, "invalid_input"
  end
  local recipe = character.force.recipes[params.recipe]
  if not recipe or not recipe.enabled or recipe.hidden or
     params.recipe == "recipe-unknown" then return nil, "recipe_unavailable" end
  local available = character.get_craftable_count(params.recipe)
  if available < params.count then
    local missing = {}
    local inventory = character.get_main_inventory()
    for _, ingredient in pairs(recipe.ingredients) do
      local held = ingredient.type == "item" and inventory and
                   inventory.get_item_count{name = ingredient.name, quality = "normal"} or 0
      local shortfall = ingredient.amount * params.count - held
      if shortfall > 0 then
        missing[#missing + 1] = {name = ingredient.name, count = shortfall}
      end
    end
    table.sort(missing, function(a, b) return a.name < b.name end)
    return nil, "missing_materials", {recipe = params.recipe, count = params.count,
                                      missing = missing, best_effort = true,
                                      missing_raw = missing_raw_materials(character, recipe,
                                                                         params.count)}
  end
  local before = state.action and state.action.type == "mine" and
                 mining_inventory_count(character) or nil
  local started = character.begin_crafting{recipe = params.recipe, count = params.count}
  adjust_mining_inventory(state, before)
  if started ~= params.count then return nil, "craft_failed" end
  local action = begin_action(state, "craft_action", {type = "craft", recipe = params.recipe,
                                      count = started})
  action.started = started
  return action
end

local function snapped_position(prototype, position, direction)
  local width, height = prototype.tile_width, prototype.tile_height
  if direction == 4 or direction == 12 then width, height = height, width end
  return {
    x = width % 2 == 1 and math.floor(position.x) + 0.5 or math.floor(position.x + 0.5),
    y = height % 2 == 1 and math.floor(position.y) + 0.5 or math.floor(position.y + 0.5)
  }
end

local function placement_bounding_box(prototype, position, direction)
  local box = prototype.collision_box
  local left, top = box.left_top.x, box.left_top.y
  local right, bottom = box.right_bottom.x, box.right_bottom.y
  if direction == 4 then
    left, top, right, bottom = -bottom, left, -top, right
  elseif direction == 8 then
    left, top, right, bottom = -right, -bottom, -left, -top
  elseif direction == 12 then
    left, top, right, bottom = top, -right, bottom, -left
  end
  return {left_top = {x = position.x + left, y = position.y + top},
          right_bottom = {x = position.x + right, y = position.y + bottom}}
end

local function preview_inserter_positions(prototype, position, direction)
  if prototype.type ~= "inserter" then return nil, nil end
  local function absolute(vector)
    if not vector then return nil end
    -- Runtime Vector values are arrays, unlike MapPosition values.
    local x, y = vector[1], vector[2]
    -- Clockwise in map coordinates (positive y is south). North-facing
    -- inserters pick north and drop south; direction=12 picks west.
    if direction == 4 then x, y = -y, x
    elseif direction == 8 then x, y = -x, -y
    elseif direction == 12 then x, y = y, -x end
    return {x = position.x + x, y = position.y + y}
  end
  return absolute(prototype.inserter_pickup_position), absolute(prototype.inserter_drop_position)
end

local function placement_electric_coverage(character, prototype, position, box)
  local is_pole = prototype.type == "electric-pole"
  if not is_pole and prototype.electric_energy_source_prototype == nil then return nil end
  local radius
  if is_pole then
    -- A connection is limited by BOTH poles, so the new pole bounds the search.
    radius = prototype.get_max_wire_distance()
  else
    local max_supply = 0
    for _, pole in pairs(prototypes.get_entity_filtered{{filter = "type", type = "electric-pole"}}) do
      max_supply = math.max(max_supply, pole.get_supply_area_distance())
    end
    -- Supply squares can overlap a rotated box even beyond the centre's radius.
    local dx = math.max(position.x - box.left_top.x, box.right_bottom.x - position.x)
    local dy = math.max(position.y - box.left_top.y, box.right_bottom.y - position.y)
    radius = math.sqrt((max_supply + dx) ^ 2 + (max_supply + dy) ^ 2)
  end
  local poles = {}
  for _, pole in pairs(character.surface.find_entities_filtered{
    position = position, radius = radius, type = "electric-pole", force = character.force
  }) do
    if pole.valid then
      local overlaps
      if is_pole then
        local reach = math.min(radius, pole.prototype.get_max_wire_distance())
        overlaps = distance_squared(pole.position, position) <= reach * reach
      else
        -- 2.0.77 ElectricPolePrototype: supply_area_distance is the square's
        -- half-size (base small pole = 2.5, hence 5x5). Require positive overlap;
        -- merely touching an edge does not cover the placement collision box.
        local supply = pole.prototype.get_supply_area_distance()
        overlaps = box.left_top.x < pole.position.x + supply and
                   box.right_bottom.x > pole.position.x - supply and
                   box.left_top.y < pole.position.y + supply and
                   box.right_bottom.y > pole.position.y - supply
      end
      if overlaps then
        poles[#poles + 1] = {x = pole.position.x, y = pole.position.y,
                             electric_network_id = pole.electric_network_id}
      end
    end
  end
  table.sort(poles, function(a, b)
    local da, db = distance_squared(a, position), distance_squared(b, position)
    if da ~= db then return da < db end
    if a.x ~= b.x then return a.x < b.x end
    return a.y < b.y
  end)
  while #poles > 3 do table.remove(poles) end
  if is_pole then return {connects = #poles > 0, poles = poles} end
  return {covered = #poles > 0, poles = poles}
end

local function ground_stack(entity)
  if not entity.valid or entity.type ~= "item-entity" then return nil end
  local stack = entity.stack
  if not stack or not stack.valid or not stack.valid_for_read then return nil end
  return {name = stack.name, quality = stack.quality.name, count = stack.count}
end

local function placement_blocking(state, character, prototype, position, direction)
  -- Keep the requested position so every alternative direction snaps independently.
  local area = placement_bounding_box(prototype, snapped_position(prototype, position, direction),
                                      direction)
  local layers = prototype.collision_mask.layers
  local function overlaps_and_collides(entity)
    if not entity.valid then return false end
    local box = entity.bounding_box
    if box.left_top.x < area.right_bottom.x and box.right_bottom.x > area.left_top.x and
       box.left_top.y < area.right_bottom.y and box.right_bottom.y > area.left_top.y then
      local entity_layers = entity.prototype.collision_mask.layers
      for layer in pairs(layers) do
        if entity_layers[layer] then return true end
      end
    end
    return false
  end
  -- Check separately so the blocking list's cap cannot hide the character.
  local character_in_the_way = overlaps_and_collides(character)
  local blocking, ground_entities = {}, {}
  local only_ground_items = true
  for _, entity in pairs(character.surface.find_entities_filtered{area = area}) do
    if entity ~= character and overlaps_and_collides(entity) then
      local stack = ground_stack(entity)
      if stack then
        ground_entities[#ground_entities + 1] = entity
      else
        only_ground_items = false
      end
      -- Inspect every collider: a blocker beyond the response cap must prevent collection.
      if #blocking < 10 then
        blocking[#blocking + 1] = {target_ref = register_target(state, entity),
                                  name = entity.name, type = entity.type,
                                  x = entity.position.x, y = entity.position.y, stack = stack}
      end
    end
  end
  local tiles = {}
  for _, tile in pairs(character.surface.find_tiles_filtered{area = area}) do
    for layer in pairs(layers) do
      if tile.collides_with(layer) then tiles[tile.name] = true; break end
    end
  end
  local tile_names = sorted_keys(tiles)
  local detail = {blocking = blocking, tiles = tile_names}
  if character_in_the_way then
    detail.reason = "character_in_the_way"
  elseif #blocking == 0 and #tile_names == 0 then
    detail.reason = "placement_rules"
  end
  local valid_directions = {}
  for _, candidate in ipairs({0, 4, 8, 12}) do
    if candidate ~= direction then
      local snapped = snapped_position(prototype, position, candidate)
      if character.can_place_entity{name = prototype.name, position = snapped, direction = candidate}
         and character.surface.can_place_entity{name = prototype.name, position = snapped,
                                                direction = candidate, force = character.force} then
        valid_directions[#valid_directions + 1] = candidate
      end
    end
  end
  if #valid_directions > 0 then detail.valid_directions = valid_directions end
  if detail.reason == "placement_rules" then
    if prototype.type == "mining-drill" then
      local centre = snapped_position(prototype, position, direction)
      -- 2.0.77 mining_drill_radius is the square's half-size: burner 0.99
      -- covers 2x2 resource centres, electric 2.49 covers 5x5.
      local radius = prototype.mining_drill_radius or 0
      local categories = prototype.resource_categories or {}
      local counts, resources = {}, {}
      for _, resource in pairs(character.surface.find_entities_filtered{
        area = {{centre.x - radius, centre.y - radius},
                {centre.x + radius, centre.y + radius}}, type = "resource"
      }) do
        -- Area queries include collision-box overlaps; mining uses resource centres.
        if resource.valid and categories[resource.prototype.resource_category] and
           math.abs(resource.position.x - centre.x) <= radius and
           math.abs(resource.position.y - centre.y) <= radius then
          counts[resource.name] = (counts[resource.name] or 0) + 1
        end
      end
      for _, name in ipairs(sorted_keys(counts)) do
        resources[#resources + 1] = {name = name, tiles = counts[name]}
      end
      detail.mining_area_resources = resources
      if #resources == 0 then detail.reason_detail = "no_resources_in_mining_area" end
    end
    local nearby, seen = {}, {}
    for dx = -8, 8 do
      for dy = -8, 8 do
        -- Other directions at the requested position are covered above.
        if dx ~= 0 or dy ~= 0 then
          for _, candidate in ipairs({0, 4, 8, 12}) do
            local snapped = snapped_position(prototype, {x = position.x + dx, y = position.y + dy},
                                              candidate)
            local key = snapped.x .. ":" .. snapped.y .. ":" .. candidate
            if not seen[key] then
              seen[key] = true
              if distance_squared(snapped, position) <= 64 and
                 character.can_place_entity{name = prototype.name, position = snapped,
                                            direction = candidate} and
                 character.surface.can_place_entity{name = prototype.name, position = snapped,
                                                    direction = candidate, force = character.force} then
                nearby[#nearby + 1] = {x = snapped.x, y = snapped.y, direction = candidate}
              end
            end
          end
        end
      end
    end
    table.sort(nearby, function(a, b)
      local da, db = distance_squared(a, position), distance_squared(b, position)
      if da ~= db then return da < db end
      if a.direction ~= b.direction then return a.direction < b.direction end
      if a.x ~= b.x then return a.x < b.x end
      return a.y < b.y
    end)
    while #nearby > 3 do table.remove(nearby) end
    if #nearby > 0 then detail.nearby_positions = nearby end
  end
  if only_ground_items and #ground_entities > 0 and not character_in_the_way and
     #tile_names == 0 then
    return detail, ground_entities
  end
  return detail
end

local function placement_ground_items(inventory, entities)
  local counts, items = {}, {}
  for _, entity in ipairs(entities) do
    local stack = ground_stack(entity)
    if not stack then return nil, false end
    counts[stack.name] = counts[stack.name] or {}
    local by_quality = counts[stack.name]
    by_quality[stack.quality] = (by_quality[stack.quality] or 0) + stack.count
  end
  for _, name in ipairs(sorted_keys(counts)) do
    for _, quality in ipairs(sorted_keys(counts[name])) do
      items[#items + 1] = {name = name, quality = quality, count = counts[name][quality]}
    end
  end
  if not inventory then return items, false end
  local empty_slots = inventory.count_empty_stacks(false, false)
  local needed_slots = 0
  for _, item in ipairs(items) do
    local capacity = inventory.get_insertable_count{name = item.name, quality = item.quality}
    if capacity < item.count then return items, false end
    -- Different names/qualities compete for the same unfiltered empty slots.
    -- The remaining capacity belongs to existing stacks or dedicated filtered slots.
    local stack_size = prototypes.item[item.name].stack_size
    local dedicated_capacity = math.max(0, capacity - empty_slots * stack_size)
    needed_slots = needed_slots + math.ceil(math.max(0, item.count - dedicated_capacity) / stack_size)
  end
  return items, needed_slots <= empty_slots
end

local function collect_placement_ground_items(inventory, entities)
  -- get_insertable_count is an estimate for items with durability/health. Retain
  -- the ground stacks until *all* inserts succeed, and undo any partial inserts.
  local original_counts = {}
  for i = 1, #inventory do
    local stack = inventory[i]
    original_counts[i] = stack.valid_for_read and stack.count or 0
  end
  for _, entity in ipairs(entities) do
    local stack = ground_stack(entity)
    if not stack or inventory.insert(entity.stack) ~= stack.count then
      for i = 1, #inventory do
        local slot = inventory[i]
        if slot.valid_for_read and slot.count > original_counts[i] then
          local count = original_counts[i]
          if count == 0 then slot.clear() else slot.count = count end
        end
      end
      return false
    end
  end
  for _, entity in ipairs(entities) do entity.destroy() end
  return true
end

local function valid_underground_type(params)
  return params.underground_type == nil or
         (params.item == "underground-belt" and
          (params.underground_type == "input" or params.underground_type == "output"))
end

local function validate_placement(state, character, params, require_item)
  if not valid_underground_type(params) then return nil, "invalid_input" end
  if type(params.item) ~= "string" or not PLACEABLE_ITEMS[params.item] then
    return nil, "invalid_target"
  end
  local direction = params.direction or defines.direction.north
  if direction ~= 0 and direction ~= 4 and direction ~= 8 and direction ~= 12 then
    return nil, "invalid_input"
  end
  if params.surface ~= character.surface.name or type(params.x) ~= "number" or
     type(params.y) ~= "number" or params.x ~= params.x or params.y ~= params.y or
     math.abs(params.x) > 1000000 or math.abs(params.y) > 1000000 then
    return nil, "invalid_input"
  end
  local item_proto = prototypes.item[params.item]
  local prototype = item_proto and item_proto.place_result
  local requested_position = {x = params.x, y = params.y}
  local position = prototype and snapped_position(prototype, requested_position, direction) or
                   requested_position
  if distance_squared(character.position, position) >
     character.build_distance * character.build_distance then
    return nil, "out_of_range", range_detail(character, position, character.build_distance)
  end
  -- Current surroundings count for placement without recording an observation.
  local visible = distance_squared(character.position, position) <= OBSERVATION_RADIUS ^ 2
  for _, observation in ipairs(state.recent_observations) do
    if observation.surface == character.surface.name and
       game.tick - observation.tick <= TARGET_TTL_TICKS and
       distance_squared(observation.position, position) <=
         (observation.radius or OBSERVATION_RADIUS) ^ 2 then
      visible = true
      break
    end
  end
  if not visible then
    return nil, "not_visible", observation_detail(state)
  end
  if not prototype then return nil, "invalid_target" end
  local entity_name = prototype.name
  local inventory = character.get_main_inventory()
  local has_item = inventory and inventory.get_item_count({name = params.item, quality = "normal"}) >= 1
  if require_item and not has_item then
    return nil, "missing_materials", {item = params.item}
  end
  -- Neither can_place_entity API accepts type in 2.0.77. Use belt travel direction
  -- for both ends (output = direction items leave); the collision box is symmetric.
  -- Base's factoriopedia_underground_belt blueprint uses direction=4 for both ends.
  -- placement_blocking uses these same checks for valid_directions/nearby_positions.
  local ground_entities, ground_items
  if not character.can_place_entity{name = entity_name, position = position, direction = direction} or
     not character.surface.can_place_entity{name = entity_name, position = position,
                                            direction = direction, force = character.force} then
    local detail, entities = placement_blocking(state, character, prototype, requested_position,
                                                direction)
    -- Manual building permits picking up ground items; still check terrain and
    -- prototype-specific rules (e.g. a drill must have ore) before collecting.
    if not entities or not character.surface.can_place_entity{
      name = entity_name, position = position, direction = direction, force = character.force,
      build_check_type = defines.build_check_type.manual
    } then
      return nil, "blocked", detail
    end
    local fits
    ground_items, fits = placement_ground_items(inventory, entities)
    if not fits then
      detail.reason = "ground_items_inventory_full"
      detail.ground_items = ground_items
      return nil, "blocked", detail
    end
    ground_entities = entities
  end
  return {name = entity_name, position = position, direction = direction,
          underground_type = params.item == "underground-belt" and
                             (params.underground_type or "input") or nil,
          inventory = inventory, ground_entities = ground_entities, ground_items = ground_items}
end

local function do_check_placement(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if not valid_underground_type(params) then return nil, "invalid_input" end
  local placement, reason, detail = validate_placement(state, character, params, false)
  -- Metadata is useful even when distance, visibility, or collision checks fail.
  local item_proto = type(params.item) == "string" and prototypes.item[params.item] or nil
  local prototype = item_proto and item_proto.place_result
  local inventory = character.get_main_inventory()
  local has_item = item_proto ~= nil and inventory ~= nil and
    inventory.get_item_count({name = params.item, quality = "normal"}) >= 1
  local direction = params.direction or defines.direction.north
  local position, box, fluid_connections, electric_coverage, pickup_position, drop_position
  if prototype and (PLACEABLE_ITEMS[params.item] or prototype.type == "inserter") and
     params.surface == character.surface.name and
     (direction == 0 or direction == 4 or direction == 8 or direction == 12) and
     type(params.x) == "number" and type(params.y) == "number" and
     params.x == params.x and params.y == params.y and
     math.abs(params.x) <= 1000000 and math.abs(params.y) <= 1000000 then
    position = snapped_position(prototype, {x = params.x, y = params.y}, direction)
    box = placement_bounding_box(prototype, position, direction)
    fluid_connections = preview_fluid_connections(prototype, position, direction)
    electric_coverage = placement_electric_coverage(character, prototype, position, box)
    pickup_position, drop_position = preview_inserter_positions(prototype, position, direction)
  end
  local blocked = reason == "blocked" and detail or {}
  local water_direction, output_direction = offshore_pump_directions(
    prototype and prototype.name, fluid_connections)
  return {placeable = placement ~= nil, reason = reason,
          entity = prototype and prototype.name or nil, has_item = has_item,
          position = position, bounding_box = box, fluid_connections = fluid_connections,
          pickup_position = pickup_position, drop_position = drop_position,
          electric_coverage = electric_coverage,
          water_direction = water_direction, output_direction = output_direction,
          blocking = blocked.blocking, tiles = blocked.tiles,
          valid_directions = blocked.valid_directions, blocked_reason = blocked.reason,
          nearby_positions = blocked.nearby_positions,
          ground_items = placement and placement.ground_items or blocked.ground_items,
          mining_area_resources = blocked.mining_area_resources,
          reason_detail = blocked.reason_detail}
end

local function do_place(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state.action) end
  local placement, reason, detail = validate_placement(state, character, params, true)
  if not placement then return nil, reason, detail end
  local inventory = placement.inventory
  local collected_items = placement.ground_items
  if placement.ground_entities then
    if not collect_placement_ground_items(inventory, placement.ground_entities) then
      local blocked = placement_blocking(state, character, prototypes.entity[placement.name],
                                         {x = params.x, y = params.y}, placement.direction)
      blocked.reason = "ground_items_inventory_full"
      blocked.ground_items = placement.ground_items
      return nil, "blocked", blocked
    end
    -- Re-run all existing checks in this tick, after the items have been moved.
    placement, reason, detail = validate_placement(state, character, params, true)
    if not placement then
      detail = detail or {}
      detail.collected_items = collected_items
      return nil, reason, detail
    end
  end
  local item = {name = params.item, quality = "normal", count = 1}
  local removed = inventory.remove(item)
  if removed ~= 1 then
    return nil, "missing_materials", {item = params.item, collected_items = collected_items}
  end
  local created = character.surface.create_entity{
    name = placement.name, position = placement.position, direction = placement.direction,
    type = placement.underground_type,
    force = character.force, quality = "normal", raise_built = true
  }
  if not created or not created.valid then
    inventory.insert(item)
    local blocked = placement_blocking(state, character, prototypes.entity[placement.name],
                                       {x = params.x, y = params.y}, placement.direction)
    blocked.collected_items = collected_items
    return nil, "blocked", blocked
  end
  return {name = created.name, position = position_data(created),
          target_ref = register_target(state, created),
          direction = created.direction, consumed_item = params.item,
          pickup_position = created.type == "inserter" and created.pickup_position or nil,
          drop_position = created.type == "inserter" and created.drop_position or nil,
          fluid_connections = placed_fluid_connections(created), collected_items = collected_items}
end

local function do_rotate(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if params.reverse ~= nil and type(params.reverse) ~= "boolean" then
    return nil, "invalid_input"
  end
  local target, target_error, detail = target_entity(character, params, character.reach_distance)
  if not target then return nil, target_error, detail end
  if target.force ~= character.force then
    return nil, "invalid_target", {entity = target.name, reason = "other_force"}
  end
  if not character.can_reach_entity(target) then
    return nil, "out_of_range", range_detail(character, target.position, character.reach_distance)
  end
  if not target.rotate{reverse = params.reverse or false} then
    return nil, "invalid_target", {entity = target.name, reason = "not_rotatable"}
  end
  return {name = target.name, direction = target.direction, position = position_data(target)}
end

local function do_set_recipe(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if type(params.recipe) ~= "string" then return nil, "invalid_input" end
  local target, target_error, detail = target_entity(character, params, character.reach_distance)
  if not target then return nil, target_error, detail end
  if target.type ~= "assembling-machine" or target.force ~= character.force then
    return nil, "invalid_target"
  end
  if not character.can_reach_entity(target) then
    return nil, "out_of_range", range_detail(character, target.position, character.reach_distance)
  end
  local recipe = character.force.recipes[params.recipe]
  local categories = target.prototype.crafting_categories
  if not recipe or not recipe.enabled or not categories or not categories[recipe.category] then
    return nil, "recipe_unavailable"
  end
  local modules = target.get_module_inventory()
  if modules and not modules.is_empty() then return nil, "not_empty", {reason = "modules"} end
  local current_recipe, current_quality = target.get_recipe()
  -- Avoid restarting a craft or returning contents when nothing changes.
  if current_recipe and current_recipe.name == params.recipe and
     (not current_quality or current_quality.name == "normal") then
    return {name = target.name, recipe = params.recipe, position = position_data(target)}
  end
  local counts, items = {}, {}
  local function add_item(name, quality, count)
    counts[name] = counts[name] or {}
    counts[name][quality] = (counts[name][quality] or 0) + count
  end
  for _, inventory_id in ipairs({defines.inventory.crafter_input, defines.inventory.crafter_output}) do
    local contents = target.get_inventory(inventory_id)
    if contents then
      for _, item in pairs(contents.get_contents()) do
        add_item(item.name, item.quality, item.count)
      end
    end
  end
  -- A started (possibly stalled) craft holds one set of ingredients outside
  -- the input slots. Fluids are not items and do not go into the character.
  if current_recipe and target.is_crafting() then
    for _, ingredient in pairs(current_recipe.ingredients) do
      if ingredient.type == "item" then
        add_item(ingredient.name, current_quality and current_quality.name or "normal",
                 ingredient.amount)
      end
    end
  end
  local needed_slots = 0
  for _, name in ipairs(sorted_keys(counts)) do
    for _, quality in ipairs(sorted_keys(counts[name])) do
      local count = counts[name][quality]
      items[#items + 1] = {name = name, quality = quality, count = count}
      needed_slots = needed_slots + math.ceil(count / prototypes.item[name].stack_size)
    end
  end
  local inventory = character.get_main_inventory()
  local empty_slots = inventory and inventory.count_empty_stacks(false, false) or 0
  -- Conservative upper bound: reserve fresh unfiltered slots for all items,
  -- even if the new recipe could keep some or existing stacks could hold them.
  if needed_slots > empty_slots then
    return nil, "inventory_full", {items = items, empty_slots = empty_slots}
  end
  -- The API's returned list is authoritative; never insert the estimate above.
  local returned = target.set_recipe(params.recipe)
  local spilled = {}
  for _, item in ipairs(returned) do
    local inserted = inventory and inventory.insert(item) or 0
    if inserted < item.count then
      local remainder = {name = item.name, quality = item.quality, count = item.count - inserted}
      spilled[#spilled + 1] = remainder
      character.surface.spill_item_stack{
        position = character.position,
        stack = remainder,
        enable_looted = true, allow_belts = false, use_start_position_on_failure = true
      }
    end
  end
  return {name = target.name, recipe = params.recipe, position = position_data(target),
          returned_items = #returned > 0 and returned or nil,
          spilled_items = #spilled > 0 and spilled or nil}
end

local function get_entity_inventory(character, params)
  local target, err, detail = target_entity(character, params, character.reach_distance)
  if not target then return nil, nil, err, detail end
  if target.force ~= character.force then
    return nil, nil, "invalid_target", {entity = target.name, reason = "other_force"}
  end
  local roles = INVENTORY_ROLES[target.name]
  if not roles then
    return nil, nil, "invalid_target", {entity = target.name, reason = "unsupported_entity",
                                       supported_entities = sorted_keys(INVENTORY_ROLES)}
  end
  if not character.can_reach_entity(target) then
    return nil, nil, "out_of_range", range_detail(character, target.position, character.reach_distance)
  end
  return target, roles
end

local function inventory_entity_state(target)
  local status = target.status
  local recipe_name
  if target.type == "assembling-machine" or target.type == "furnace" then
    local recipe = target.get_recipe()
    if recipe then
      recipe_name = recipe.name
    elseif target.type == "furnace" then
      -- Idle furnaces expose a RecipeIDAndQualityIDPair, not a LuaRecipe.
      local previous = target.previous_recipe
      if previous then recipe_name = previous.name end
    end
  end
  -- Guard: previous_recipe.name may be a LuaRecipePrototype, not a string.
  -- Normalize before callers index recipes or serialize the value to JSON.
  if recipe_name and type(recipe_name) ~= "string" then recipe_name = recipe_name.name end
  return status ~= nil and ENTITY_STATUS_NAMES[status] or nil, recipe_name
end

local function do_entity_inventory(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  local target, roles, target_error, detail = get_entity_inventory(character, params)
  if not target then return nil, target_error, detail end
  local inventories = {}
  for role, inventory_id in pairs(roles) do
    local inventory = target.get_inventory(inventory_id)
    if inventory and inventory.valid then inventories[role] = inventory.get_contents() end
  end
  local status, recipe = inventory_entity_state(target)
  local products_finished, recipe_products, recipe_product_amounts
  if target.type == "assembling-machine" or target.type == "furnace" then
    products_finished = target.products_finished
    recipe_products, recipe_product_amounts = {}, {}
    -- inventory_entity_state resolves idle furnaces' previous recipe names.
    local recipe_data = recipe and target.force.recipes[recipe]
    for _, product in pairs(recipe_data and recipe_data.products or {}) do
      if product.type == "item" then
        if recipe_product_amounts[product.name] == nil then
          recipe_products[#recipe_products + 1] = product.name
        end
        -- Ignore probability; variable amounts use their midpoint.
        local amount = product.amount or ((product.amount_min + product.amount_max) / 2)
        recipe_product_amounts[product.name] = (recipe_product_amounts[product.name] or 0) + amount
      end
    end
    table.sort(recipe_products)
  end
  return {name = target.name, type = target.type,
          inventories = inventories, status = status, recipe = recipe,
          products_finished = products_finished, recipe_products = recipe_products,
          recipe_product_amounts = recipe_product_amounts,
          direction = target.direction,
          drop_position = target.name == "burner-mining-drill" and
                          target.drop_position or nil}
end

local function transfer_inventory_contents(inventory)
  local contents = inventory.get_contents()
  table.sort(contents, function(a, b)
    if a.name ~= b.name then return a.name < b.name end
    return a.quality < b.quality
  end)
  local preview = {}
  for index = 1, math.min(10, #contents) do
    local item = contents[index]
    preview[#preview + 1] = {name = item.name, count = item.count, quality = item.quality}
  end
  return preview
end

local function transfer_shortage_detail(source, params, role, target)
  local preview = transfer_inventory_contents(source)
  local status, recipe
  if params.direction == "from_entity" then
    status, recipe = inventory_entity_state(target)
  end
  return {item = params.item, requested = params.count, available = 0,
          inventory_role = params.direction == "from_entity" and role or nil,
          status = status, recipe = recipe,
          contents = #preview > 0 and preview or nil}
end

local function transfer_full_detail(destination, params, role, insertable)
  return {item = params.item, requested = params.count,
          destination = params.direction == "to_entity" and "entity" or "character",
          inventory_role = params.direction == "to_entity" and role or nil,
          contents = transfer_inventory_contents(destination), slots = #destination,
          empty_slots = destination.count_empty_stacks(),
          insertable = insertable}
end

local function transfer_rejection_detail(target, params, role, inventory_id)
  if params.direction ~= "to_entity" or inventory_id ~= defines.inventory.crafter_input then
    return nil
  end
  local detail = {item = params.item, entity = target.name, inventory_role = role, insertable = 0}
  if target.type == "assembling-machine" then
    local recipe = target.get_recipe()
    if not recipe then
      detail.reason = "no_recipe"
      return detail
    end
    detail.recipe, detail.ingredients = recipe.name, {}
    local accepted = false
    for _, ingredient in pairs(recipe.ingredients) do
      detail.ingredients[#detail.ingredients + 1] = ingredient.name
      if ingredient.type == "item" and ingredient.name == params.item then accepted = true end
    end
    if accepted then return nil end
    table.sort(detail.ingredients)
    return detail
  elseif target.type == "furnace" then
    -- Prototype recipes include locked recipes; do not filter on force.enabled.
    for category in pairs(target.prototype.crafting_categories or {}) do
      local recipes = prototypes.get_recipe_filtered{{filter = "category", category = category}}
      for _, recipe in pairs(recipes) do
        for _, ingredient in pairs(recipe.ingredients) do
          if ingredient.type == "item" and ingredient.name == params.item then return nil end
        end
      end
    end
    detail.reason = "no_smelting_recipe"
    return detail
  end
end

local function do_transfer(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state.action) end
  if (params.direction ~= "to_entity" and params.direction ~= "from_entity") or
     type(params.item) ~= "string" or type(params.count) ~= "number" or
     params.count < 1 or params.count > 100 or params.count % 1 ~= 0 then
    return nil, "invalid_input"
  end
  local target, roles, target_error, detail = get_entity_inventory(character, params)
  if not target then return nil, target_error, detail end
  local aliases = ROLE_ALIASES[target.name]
  local role = (aliases and aliases[params.inventory_role]) or params.inventory_role
  local inventory_id = roles[role]
  if not inventory_id then
    return nil, "invalid_target", {entity = target.name, valid_roles = sorted_keys(roles),
                                  role_aliases = aliases}
  end
  local entity_inventory = target.get_inventory(inventory_id)
  local player_inventory = character.get_main_inventory()
  if not entity_inventory or not entity_inventory.valid or
     not player_inventory or not player_inventory.valid then
    return nil, "invalid_target", {entity = target.name, valid_roles = sorted_keys(roles),
                                  role_aliases = aliases}
  end
  local rejection = transfer_rejection_detail(target, params, role, inventory_id)
  if rejection then return nil, "item_not_accepted", rejection end
  local source = params.direction == "to_entity" and player_inventory or entity_inventory
  local destination = params.direction == "to_entity" and entity_inventory or player_inventory
  local item_id = {name = params.item, quality = "normal"}
  local available = source.get_item_count(item_id)
  -- 2.0.77 documents this as a best guess, including for assembler filtered slots.
  -- Recipe acceptance is checked above; retain its capacity estimate for accepted items.
  local insertable = destination.get_insertable_count(item_id)
  local count = math.min(params.count, available, insertable)
  if count == 0 then
    if available == 0 then
      return nil, "missing_materials", transfer_shortage_detail(source, params, role, target)
    end
    return nil, "inventory_full", transfer_full_detail(destination, params, role, insertable)
  end
  local removed = source.remove{name = params.item, quality = "normal", count = count}
  if removed == 0 then
    return nil, "missing_materials", transfer_shortage_detail(source, params, role, target)
  end
  local inserted = destination.insert{name = params.item, quality = "normal", count = removed}
  if inserted < removed then
    source.insert{name = params.item, quality = "normal", count = removed - inserted}
  end
  if inserted == 0 then
    -- A positive estimate followed by zero actual insertion is an acceptance failure.
    local rejected = transfer_full_detail(destination, params, role, 0)
    if params.direction == "to_entity" then rejected.entity = target.name end
    return nil, "item_not_accepted", rejected
  end
  local result = {moved = inserted, requested = params.count, item = params.item,
                  direction = params.direction, inventory_role = role}
  if inserted < params.count then
    result.partial = true
    result.limited_by = available <= insertable and inserted == available and
                        "source" or "destination"
    result.available = available
    result.insertable = insertable
  end
  return result
end

local function do_batch(state, params, handler)
  local inventory_read = handler == do_entity_inventory
  local items
  if inventory_read then items = params.target_refs else items = params.items end
  if type(items) ~= "table" then return nil, "invalid_input" end
  -- Validate the entire list before invoking any mutating handler. Reject maps,
  -- sparse lists, and invalid entry types rather than silently skipping them.
  local count = 0
  for index, entry in pairs(items) do
    if type(index) ~= "number" or index % 1 ~= 0 or index < 1 or index > 10 or
       (inventory_read and (type(entry) ~= "string" or #entry == 0)) or
       (not inventory_read and type(entry) ~= "table") then
      return nil, "invalid_input"
    end
    count = count + 1
  end
  if count < 1 or count > 10 then return nil, "invalid_input" end
  for index = 1, count do
    if items[index] == nil then return nil, "invalid_input" end
  end
  local results, succeeded = {}, 0
  for index = 1, count do
    -- Single and batch operations share all reach, observation and force checks.
    local entry = inventory_read and {target_ref = items[index]} or items[index]
    local ok, result, err, detail = pcall(handler, state, entry)
    if not ok then
      log("AI MCP batch handler error: " .. tostring(result))
      err = "internal_error"
    end
    if err == nil then
      succeeded = succeeded + 1
      results[index] = {index = index, ok = true, result = result}
    else
      results[index] = {index = index, ok = false, error = err, detail = detail}
    end
  end
  -- Preserve single-operation hints when get_request_result recovers this batch.
  return {results = results, succeeded = succeeded, failed = count - succeeded,
          operation = inventory_read and "get_entity_inventory" or
                      (handler == do_transfer and "transfer_item" or "place_entity")}
end

local function running_action_slot(state, action_id)
  if state.action and state.action.id == action_id then return "action" end
  if state.craft_action and state.craft_action.id == action_id then return "craft_action" end
end

local function do_action_status(state, params)
  local slot = running_action_slot(state, params.action_id)
  local action = slot and state[slot]
  if not action or action.id ~= params.action_id then
    action = state.completed_actions[params.action_id] or state.last_action
  end
  if not action or action.id ~= params.action_id then return nil, "action_not_found" end
  local detail = action.detail
  if action.status == "running" then
    if action.type == "mine" then
      detail = {gained = action.gained, count = action.goal_count,
                estimated_seconds = mining_estimated_seconds(state.character, action)}
    elseif action.type == "craft" then
      local remaining = 0
      if valid_character(state) then
        for _, item in ipairs(state.character.crafting_queue or {}) do
          if item.recipe == action.recipe and not item.prerequisite then
            remaining = remaining + item.count
          end
        end
      end
      detail = {recipe = action.recipe, count = action.count, remaining = remaining}
    elseif action.type == "move" then
      detail = adjusted_goal_detail(action, detail)
    end
  end
  return adjusted_goal_detail(action, {
    action_id = action.id, type = action.type, status = action.status,
    error = action.error, detail = detail,
    position = action.status == "running" and
               (valid_character(state) and position_data(state.character) or nil) or
               action.position
  })
end

local function do_cancel(state, params)
  local slot = running_action_slot(state, params.action_id)
  if not slot then return nil, "action_not_found" end
  local action = state[slot]
  if action.type == "craft" and valid_character(state) then
    local before = mining_inventory_count(state.character)
    pcall(state.character.cancel_crafting, {count = action.count, index = 1})
    adjust_mining_inventory(state, before)
  end
  finish_action(state, slot, "cancelled", "cancelled")
  return do_action_status(state, params)
end

local handlers = {
  ping = function()
    return {bridge_version = "0.1.0", factorio_version = script.active_mods.base,
            agent_id = AGENT_ID}
  end,
  get_agent_status = function(state) return status(state) end,
  observe_nearby = do_observe,
  get_explored_map = do_get_explored_map,
  get_inventory = function(state)
    local character, err = require_character(state)
    if not character then return nil, err end
    return {agent_id = AGENT_ID, items = inventory_contents(character)}
  end,
  get_entity_inventory = do_entity_inventory,
  get_entity_inventories = function(state, params)
    return do_batch(state, params, do_entity_inventory)
  end,
  list_craftable_recipes = do_list_craftable,
  get_recipe_info = do_recipe_info,
  get_research_status = do_research,
  research_labs = do_research_labs,
  get_technology_info = do_technology_info,
  start_research = do_start_research,
  set_recipe = do_set_recipe,
  get_request_result = function(state, params)
    if type(params.request_id) ~= "string" or #params.request_id > 80 then
      return nil, "invalid_input"
    end
    local cached = state.processed[params.request_id]
    if not cached then return nil, "request_not_found" end
    return {original_response = helpers.json_to_table(cached)}
  end,
  move_to = do_move,
  mine_entity = do_mine,
  craft = do_craft,
  place_entity = do_place,
  place_entities = function(state, params) return do_batch(state, params, do_place) end,
  check_placement = do_check_placement,
  rotate_entity = do_rotate,
  transfer_item = do_transfer,
  transfer_items = function(state, params) return do_batch(state, params, do_transfer) end,
  get_action_status = do_action_status,
  cancel_action = do_cancel
}

commands.add_command("ai_mcp", "AI MCP bridge (RCON only)", function(command)
  local raw = command.parameter
  if command.player_index then return end
  if type(raw) ~= "string" or #raw > 8192 then
    reply(nil, false, nil, "invalid_input")
    return
  end
  local parse_ok, request = pcall(helpers.json_to_table, raw)
  if not parse_ok or type(request) ~= "table" then
    reply(nil, false, nil, "invalid_input")
    return
  end
  local request_id = request.request_id
  if request.protocol_version ~= PROTOCOL_VERSION or
     type(request_id) ~= "string" or #request_id > 80 or
     request.agent_id ~= AGENT_ID or type(request.operation) ~= "string" or
     type(request.params) ~= "table" then
    reply(request_id, false, nil, "invalid_input")
    return
  end
  local handler = handlers[request.operation]
  if not handler then
    reply(request_id, false, nil, "unknown_operation")
    return
  end
  local state = ensure_storage()
  state.last_heartbeat_tick = game.tick
  local cached = state.processed[request_id]
  if cached then
    rcon.print(cached)
    return
  end
  local ok, result, err, detail = pcall(handler, state, request.params)
  if not ok then log("AI MCP handler error: " .. tostring(result)) end
  local response = helpers.table_to_json({
    protocol_version = PROTOCOL_VERSION, request_id = request_id,
    ok = ok and err == nil, tick = game.tick,
    result = ok and err == nil and result or nil,
    error = (not ok) and "internal_error" or err,
    detail = ok and err ~= nil and detail or nil
  })
  if request.operation == "move_to" or request.operation == "mine_entity" or
     request.operation == "cancel_action" or request.operation == "craft" or
     request.operation == "place_entity" or request.operation == "transfer_item" or
     request.operation == "place_entities" or request.operation == "transfer_items" or
     request.operation == "rotate_entity" or request.operation == "set_recipe" or
     request.operation == "start_research" then
    state.processed[request_id] = response
    state.processed_order[#state.processed_order + 1] = request_id
    if #state.processed_order > 256 then
      local oldest = table.remove(state.processed_order, 1)
      state.processed[oldest] = nil
    end
  end
  rcon.print(response)
end)

commands.add_command("ai_mcp_admin", "AI MCP administrator reset (RCON only)",
  function(command)
    if command.player_index then return end
    if command.parameter ~= "reset_dead_agent" then return end
    local state = ensure_storage()
    if valid_character(state) then
      reply(nil, false, nil, "character_alive")
      return
    end
    state.spawned_once = false
    state.character = nil
    state.action = nil
    state.craft_action = nil
    state.last_action = nil
    state.completed_actions = {}
    state.completed_order = {}
    state.targets = {}
    state.owned_refs = {}
    state.last_observation = nil
    state.recent_observations = {}
    state.explored_map = {}
    spawn_character()
    reply(nil, valid_character(state), status(state),
          valid_character(state) and nil or "spawn_blocked")
  end)

script.on_init(ensure_storage)
script.on_configuration_changed(ensure_storage)

script.on_event(defines.events.on_script_path_request_finished, function(event)
  local state = ensure_storage()
  if not state.action or state.action.type ~= "move" or state.path_request_id ~= event.id then
    return
  end
  state.path_request_id = nil
  if not valid_character(state) then
    finish_action(state, "action", "failed", "character_dead")
    return
  end
  if finish_move_within(state) then return end
  if event.try_again_later then
    state.action.retries = state.action.retries + 1
    if state.action.retries > 3 then
      if try_escape_path(state) then return end
      finish_action(state, "action", "failed", "path_not_found",
        path_not_found_detail(state.character, state.action.goal, state.action))
    else
      -- Keep the same start, including an escape start, while the pathfinder is busy.
      request_path(state, state.character, state.action.goal, state.action.path_start_used)
    end
    return
  end
  if not event.path or #event.path == 0 then
    if try_escape_path(state) then return end
    finish_action(state, "action", "failed", "path_not_found",
      path_not_found_detail(state.character, state.action.goal, state.action))
    return
  end
  local start = state.action.path_start_adjusted
  if start and distance_squared(start, event.path[1].position) > 0 then
    -- Always walk to the adjusted start before following the returned route.
    table.insert(event.path, 1, {position = {x = start.x, y = start.y}})
  end
  state.action.path = event.path
  state.action.path_index = 1
  reset_move_progress(state.action)
end)

local function tick_action(state, slot)
  local action = state[slot]
  if not action then return end
  local character = state.character
  if not valid_character(state) then
    finish_action(state, slot, "failed", "character_dead")
    return
  end
  if game.tick - (state.last_heartbeat_tick or action.started_tick) > 60 * 10 then
    if action.type == "craft" then
      pcall(character.cancel_crafting, {count = action.count, index = 1})
    end
    finish_action(state, slot, "failed", "connection_lost")
    return
  end
  if action.type ~= "craft" and action.type ~= "mine" and
     game.tick - action.started_tick > ACTION_TIMEOUT_TICKS then
    finish_action(state, slot, "failed", "timeout")
    return
  end
  if action.type == "move" then
    if finish_move_within(state) then return end
    if not action.path then return end
    local d = math.sqrt(distance_squared(character.position, action.goal))
    if d <= 0.5 then
      finish_action(state, slot, "succeeded", nil, {position = position_data(character)})
      return
    end
    local waypoint = action.path[action.path_index]
    if waypoint and distance_squared(character.position, waypoint.position) <= 0.35 * 0.35 then
      action.path_index = action.path_index + 1
      action.last_waypoint_tick = game.tick
      return
    end
    -- Saved moves from before progress tracking start their timers on this tick.
    action.last_waypoint_tick = action.last_waypoint_tick or game.tick
    if action.path_index > #action.path then
      action.final_best_distance = math.min(action.final_best_distance or d, d)
      -- Measure cumulative improvement so small steps towards the goal still count.
      if not action.final_progress_tick or
         action.final_progress_distance - action.final_best_distance >= 0.05 then
        action.final_progress_distance = action.final_best_distance
        action.final_progress_tick = game.tick
      end
      if game.tick - action.final_progress_tick >= 60 then
        if d <= 1.5 then
          finish_action(state, slot, "succeeded", nil,
            {position = position_data(character), distance = d, goal_blocked = true})
        else
          finish_action(state, slot, "failed", "blocked")
        end
        return
      end
    end
    -- An adjusted path start can be up to 2 tiles away: walk to its first
    -- waypoint as usual. The progress timers and shared retry cap still apply.
    local destination = waypoint and waypoint.position or action.goal
    local dx = destination.x - character.position.x
    local dy = destination.y - character.position.y
    local previous = action.path[action.path_index - 1]
    local start = previous and previous.position or action.path_start_used or character.position
    -- Repaths reset path_index and path_start_used, so steering starts afresh.
    -- A zero-length first segment (including an adjusted start) uses nearest heading.
    local direction = waypoint and path_direction(character.position, start, destination)
                      or move_direction(dx, dy)
    character.walking_state = {walking = true, direction = direction}
    if distance_squared(character.position, {x = action.last_x, y = action.last_y}) < 0.01 then
      action.stuck_ticks = action.stuck_ticks + 1
    else
      action.stuck_ticks = 0
      action.last_x, action.last_y = character.position.x, character.position.y
    end
    if action.stuck_ticks > 120 or
       (waypoint and game.tick - action.last_waypoint_tick >= 180) then
      stop_character(character)
      action.retries = action.retries + 1
      if action.retries > 3 then
        finish_action(state, slot, "failed", "blocked")
      else
        request_path(state, character, action.goal)
      end
    end
  elseif action.type == "mine" then
    local inventory = character.get_main_inventory()
    if not inventory then
      finish_action(state, slot, "failed", "inventory_full")
      return
    end
    local current = mining_inventory_count(character)
    if current > action.last_inventory_count then
      action.gained = action.gained + current - action.last_inventory_count
      action.last_progress_tick = game.tick
    end
    action.last_inventory_count = current
    if action.gained >= action.goal_count then
      finish_action(state, slot, "succeeded", nil,
        {gained = action.gained, inventory = inventory.get_contents()})
      return
    end
    local target = action.target
    if not target or not target.valid then
      finish_action(state, slot, "failed", "target_gone", {reason = "entity_gone"})
      return
    end
    -- Actions saved before reach was recorded still use the target's reach rule.
    local reach = action.reach or ((target.type == "resource" or target.type == "tree" or
                  target.type == "simple-entity") and
                  character.resource_reach_distance or character.reach_distance)
    if (target.type == "resource" and
        distance_squared(character.position, target.position) > reach * reach) or
       not character.can_reach_entity(target) then
      finish_action(state, slot, "failed", "out_of_range",
                    range_detail(character, target.position, reach))
      return
    end
    if inventory.is_full() then
      finish_action(state, slot, "failed", "inventory_full")
      return
    end
    if game.tick - action.last_progress_tick >= 60 * 20 then
      finish_action(state, slot, "failed", "blocked")
      return
    end
    if character.selected ~= target then
      character.selected = target
      character.mining_state = {mining = true, position = target.position}
    end
  elseif action.type == "craft" then
    if character.crafting_queue_size == 0 then
      finish_action(state, slot, "succeeded", nil,
        {recipe = action.recipe, count = action.count,
         inventory = inventory_contents(character)})
      record_hand_crafted(state, character, action)
    end
  end
end

script.on_event(defines.events.on_tick, function()
  local state = ensure_storage()
  if not state.spawned_once then
    spawn_character()
  end
  -- A return or completion in one slot must not skip the other slot.
  tick_action(state, "action")
  tick_action(state, "craft_action")
end)
