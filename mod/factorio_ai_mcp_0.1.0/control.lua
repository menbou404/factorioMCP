local PROTOCOL_VERSION = 1
local AGENT_ID = "agent-1"
local MAX_OBSERVATIONS = 100
local OBSERVATION_RADIUS = 16
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

local function ensure_storage()
  storage.ai_mcp = storage.ai_mcp or {
    spawned_once = false,
    character = nil,
    action = nil,
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
    last_observation = nil,
    last_heartbeat_tick = nil
  }
  storage.ai_mcp.next_action_id = storage.ai_mcp.next_action_id or 1
  storage.ai_mcp.next_target_id = storage.ai_mcp.next_target_id or 1
  storage.ai_mcp.targets = storage.ai_mcp.targets or {}
  storage.ai_mcp.completed_actions = storage.ai_mcp.completed_actions or {}
  storage.ai_mcp.completed_order = storage.ai_mcp.completed_order or {}
  storage.ai_mcp.hand_crafted = storage.ai_mcp.hand_crafted or {}
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

local function conflict_detail(state)
  return {running_action = state.action and {id = state.action.id, type = state.action.type} or nil}
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

local function mine_partial_detail(character, action, detail)
  detail = detail or {}
  detail.gained = action.gained or 0
  detail.inventory = nil
  if character and character.valid then
    local inventory = character.get_main_inventory()
    if inventory and inventory.valid then
      -- Cancellation and early tick failures can precede the next progress update.
      local current = inventory.get_item_count()
      detail.gained = detail.gained + math.max(0, current - (action.last_inventory_count or current))
      detail.inventory = inventory.get_contents()
    end
  end
  return detail
end

local function finish_action(state, status, error_code, detail)
  local action = state.action
  if not action then return end
  stop_character(state.character)
  if action.type == "mine" and status ~= "succeeded" then
    detail = mine_partial_detail(state.character, action, detail)
  end
  if action.type == "move" and (error_code == "blocked" or error_code == "timeout") and
     valid_character(state) then
    detail = detail or {}
    detail.distance = math.sqrt(distance_squared(state.character.position, action.goal))
    detail.goal = {x = action.goal.x, y = action.goal.y}
  end
  action.status = status
  action.error = error_code
  action.detail = detail
  action.finished_tick = game.tick
  local completed = {
    id = action.id, type = action.type, status = status,
    error = error_code, detail = detail,
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
  state.action = nil
  state.path_request_id = nil
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
    action = state.action and {id = state.action.id, type = state.action.type, status = state.action.status} or nil
  }
end

local function require_character(state)
  if not valid_character(state) then return nil, "character_dead" end
  return state.character
end

local function register_target(state, entity)
  local target_ref = tostring(state.next_target_id)
  state.next_target_id = state.next_target_id + 1
  state.targets[target_ref] = {entity = entity, tick = game.tick}
  return target_ref
end

local function target_entity(character, params, max_distance, mining)
  local state = ensure_storage()
  if type(params.target_ref) == "string" then
    local reference = state.targets[params.target_ref]
    if not reference or game.tick - reference.tick > TARGET_TTL_TICKS then
      return nil, "target_gone", {reason = "expired"}
    end
    local entity = reference.entity
    if not entity or not entity.valid then
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

local function begin_action(state, action)
  if state.action then return nil, "action_conflict", conflict_detail(state) end
  action.id = tostring(state.next_action_id)
  state.next_action_id = state.next_action_id + 1
  action.status = "running"
  action.started_tick = game.tick
  state.action = action
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

local function reset_move_progress(action)
  action.last_waypoint_tick = game.tick
  action.final_best_distance = nil
  action.final_progress_distance = nil
  action.final_progress_tick = nil
end

local function path_not_found_detail(character, goal)
  local detail = {goal = {x = goal.x, y = goal.y}}
  -- A path response can arrive after the character has been removed.
  if not character or not character.valid then return detail end
  detail.distance = math.sqrt(distance_squared(character.position, goal))
  local surface = character.surface
  local prototype = character.prototype
  local layers = prototype.collision_mask.layers
  local tile = surface.get_tile(goal.x, goal.y)
  local tiles = {}
  for layer in pairs(layers) do
    if tile.collides_with(layer) then tiles[tile.name] = true; break end
  end
  local tile_names = sorted_keys(tiles)
  if #tile_names > 0 then detail.goal_tiles = tile_names end

  local box = prototype.collision_box
  local area = {left_top = {x = goal.x + box.left_top.x, y = goal.y + box.left_top.y},
                right_bottom = {x = goal.x + box.right_bottom.x, y = goal.y + box.right_bottom.y}}
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
  if #entities > 0 then detail.goal_entities = entities end
  local free = surface.find_non_colliding_position("character", goal, 10, 0.5)
  if free then detail.nearby_free_position = {x = free.x, y = free.y} end
  return detail
end

local function request_path(state, character, goal)
  local action = state.action
  action.path = nil
  action.path_index = 1
  action.stuck_ticks = 0
  action.last_x, action.last_y = character.position.x, character.position.y
  reset_move_progress(action)
  local id = character.surface.request_path{
    bounding_box = character.prototype.collision_box,
    collision_mask = character.prototype.collision_mask,
    start = character.position,
    goal = goal,
    force = character.force,
    entity_to_ignore = character,
    can_open_gates = true
  }
  state.path_request_id = id
  return id
end

local function do_move(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state) end
  if type(params.x) ~= "number" or type(params.y) ~= "number" or
     params.x ~= params.x or params.y ~= params.y or
     math.abs(params.x) > 1000000 or math.abs(params.y) > 1000000 then
    return nil, "invalid_input"
  end
  if params.surface ~= character.surface.name then return nil, "out_of_range" end
  local goal = {x = params.x, y = params.y}
  if distance_squared(character.position, goal) > 128 * 128 then
    return nil, "out_of_range", range_detail(character, goal, 128)
  end
  local result = begin_action(state, {
    type = "move", goal = goal, path = nil, path_index = 1,
    last_x = character.position.x, last_y = character.position.y,
    stuck_ticks = 0, retries = 0
  })
  local ok = pcall(request_path, state, character, goal)
  if not ok then
    local detail = path_not_found_detail(character, goal)
    finish_action(state, "failed", "path_not_found", detail)
    return nil, "path_not_found", detail
  end
  return result
end

local function do_mine(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state) end
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
  local before = inventory.get_item_count()
  character.selected = target
  if character.selected ~= target then
    character.selected = nil
    return nil, "invalid_target", {entity = target.name, reason = "not_selectable"}
  end
  local result = begin_action(state, {
    type = "mine", target = target, reach = reach, goal_count = count, gained = 0,
    last_inventory_count = before,
    last_progress_tick = game.tick
  })
  character.mining_state = {mining = true, position = target.position}
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
      if connection.connection_type == "normal" then
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
          connected = connection.target ~= nil
        }
      end
    end
  end
  if #connections > 0 then return connections end
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
    if a.position.x ~= b.position.x then return a.position.x < b.position.x end
    if a.position.y ~= b.position.y then return a.position.y < b.position.y end
    return a.name < b.name
  end)
  local output = {}
  state.last_observation = {position = {x = character.position.x, y = character.position.y},
                            surface = character.surface.name, tick = game.tick, radius = radius}
  for index = entity_offset + 1, math.min(entity_offset + MAX_OBSERVATIONS, #visible) do
    local entity = visible[index]
    local observed = {
      target_ref = register_target(state, entity),
      name = entity.name, type = entity.type,
      x = entity.position.x, y = entity.position.y,
      surface = entity.surface.name, unit_number = entity.unit_number,
      amount = entity.type == "resource" and entity.amount or nil,
      owned = entity.force == character.force
    }
    local roles = INVENTORY_ROLES[entity.name]
    if observed.owned and roles then
      local inventory_roles = sorted_keys(roles)
      if #inventory_roles > 0 then observed.inventory_roles = inventory_roles end
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
    if entity.type == "electric-pole" or entity.prototype.electric_energy_source_prototype then
      -- Some entity types cannot expose a network ID even with an electric source.
      local ok, network_id = pcall(function() return entity.electric_network_id end)
      if ok and network_id ~= nil then observed.electric_network_id = network_id end
    end
    if entity.type == "transport-belt" then
      local merged = {}
      local items = {}
      for line = 1, 2 do
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
    if game.tick - reference.tick > TARGET_TTL_TICKS then state.targets[key] = nil end
  end
  return {position = position_data(character), radius = radius, filters = filters,
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
  if not technology or not research_available(technology) then return nil, "invalid_target" end
  if not force.add_research(params.technology) then return nil, "blocked" end
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
  return {
    researched = researched,
    queue = research_queue(force), available = available,
    current = force.current_research and force.current_research.name or nil,
    progress = force.current_research and force.research_progress or nil
  }
end

local function do_craft(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state) end
  if type(params.recipe) ~= "string" or type(params.count) ~= "number" or
     params.count < 1 or params.count > 100 or params.count % 1 ~= 0 then
    return nil, "invalid_input"
  end
  local recipe = character.force.recipes[params.recipe]
  if not recipe or not recipe.enabled or recipe.hidden or
     params.recipe == "recipe-unknown" then return nil, "recipe_unavailable" end
  if character.crafting_queue_size > 0 then return nil, "action_conflict", conflict_detail(state) end
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
                                      missing = missing, best_effort = true}
  end
  local started = character.begin_crafting{recipe = params.recipe, count = params.count}
  if started ~= params.count then return nil, "craft_failed" end
  local action = begin_action(state, {type = "craft", recipe = params.recipe,
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
  if direction == 4 or direction == 12 then
    left, top, right, bottom = -bottom, left, -top, right
  end
  return {left_top = {x = position.x + left, y = position.y + top},
          right_bottom = {x = position.x + right, y = position.y + bottom}}
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
  local blocking = {}
  for _, entity in pairs(character.surface.find_entities_filtered{area = area}) do
    if entity ~= character and overlaps_and_collides(entity) then
      blocking[#blocking + 1] = {target_ref = register_target(state, entity),
                                name = entity.name, type = entity.type,
                                x = entity.position.x, y = entity.position.y}
      if #blocking == 10 then break end
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
  return detail
end

local function validate_placement(state, character, params, require_item)
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
  local observation = state.last_observation
  if not observation or observation.surface ~= character.surface.name or
     game.tick - observation.tick > TARGET_TTL_TICKS or
     distance_squared(observation.position, position) >
       (observation.radius or OBSERVATION_RADIUS) ^ 2 then
    return nil, "not_visible", observation_detail(state)
  end
  if not prototype then return nil, "invalid_target" end
  local entity_name = prototype.name
  local inventory = character.get_main_inventory()
  local has_item = inventory and inventory.get_item_count({name = params.item, quality = "normal"}) >= 1
  if require_item and not has_item then
    return nil, "missing_materials", {item = params.item}
  end
  if not character.can_place_entity{name = entity_name, position = position,
                                     direction = direction} then
    return nil, "blocked", placement_blocking(state, character, prototype, requested_position, direction)
  end
  if not character.surface.can_place_entity{name = entity_name, position = position,
                                             direction = direction, force = character.force} then
    return nil, "blocked", placement_blocking(state, character, prototype, requested_position, direction)
  end
  return {name = entity_name, position = position, direction = direction,
          inventory = inventory}
end

local function do_check_placement(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  local placement, reason, detail = validate_placement(state, character, params, false)
  -- Metadata is useful even when distance, visibility, or collision checks fail.
  local item_proto = type(params.item) == "string" and prototypes.item[params.item] or nil
  local prototype = item_proto and item_proto.place_result
  local inventory = character.get_main_inventory()
  local has_item = item_proto ~= nil and inventory ~= nil and
    inventory.get_item_count({name = params.item, quality = "normal"}) >= 1
  local direction = params.direction or defines.direction.north
  local position, box, fluid_connections
  if prototype and PLACEABLE_ITEMS[params.item] and params.surface == character.surface.name and
     (direction == 0 or direction == 4 or direction == 8 or direction == 12) and
     type(params.x) == "number" and type(params.y) == "number" and
     params.x == params.x and params.y == params.y and
     math.abs(params.x) <= 1000000 and math.abs(params.y) <= 1000000 then
    position = snapped_position(prototype, {x = params.x, y = params.y}, direction)
    box = placement_bounding_box(prototype, position, direction)
    fluid_connections = preview_fluid_connections(prototype, position, direction)
  end
  local blocked = reason == "blocked" and detail or {}
  return {placeable = placement ~= nil, reason = reason,
          entity = prototype and prototype.name or nil, has_item = has_item,
          position = position, bounding_box = box, fluid_connections = fluid_connections,
          blocking = blocked.blocking, tiles = blocked.tiles,
          valid_directions = blocked.valid_directions, blocked_reason = blocked.reason}
end

local function do_place(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state) end
  local placement, reason, detail = validate_placement(state, character, params, true)
  if not placement then return nil, reason, detail end
  local inventory = placement.inventory
  local item = {name = params.item, quality = "normal", count = 1}
  local removed = inventory.remove(item)
  if removed ~= 1 then return nil, "missing_materials", {item = params.item} end
  local created = character.surface.create_entity{
    name = placement.name, position = placement.position, direction = placement.direction,
    force = character.force, quality = "normal", raise_built = true
  }
  if not created or not created.valid then
    inventory.insert(item)
    return nil, "blocked", placement_blocking(state, character, prototypes.entity[placement.name],
                                              {x = params.x, y = params.y}, placement.direction)
  end
  return {name = created.name, position = position_data(created),
          direction = created.direction, consumed_item = params.item,
          fluid_connections = placed_fluid_connections(created)}
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
  for _, inventory_id in ipairs({defines.inventory.crafter_input, defines.inventory.crafter_output}) do
    local inventory = target.get_inventory(inventory_id)
    if inventory and not inventory.is_empty() then return nil, "not_empty" end
  end
  local modules = target.get_module_inventory()
  if modules and not modules.is_empty() then return nil, "not_empty" end
  -- An in-flight craft also holds ingredients outside the input inventory.
  if target.is_crafting() then return nil, "not_empty" end
  local returned = target.set_recipe(params.recipe)
  local inventory = character.get_main_inventory()
  for _, item in ipairs(returned) do
    local inserted = inventory and inventory.insert(item) or 0
    if inserted < item.count then
      character.surface.spill_item_stack{
        position = character.position,
        stack = {name = item.name, quality = item.quality, count = item.count - inserted},
        enable_looted = true, allow_belts = false, use_start_position_on_failure = true
      }
    end
  end
  return {name = target.name, recipe = params.recipe, position = position_data(target),
          returned_items = #returned > 0 and returned or nil}
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
  return {name = target.name, inventories = inventories,
          direction = target.direction,
          drop_position = target.name == "burner-mining-drill" and
                          target.drop_position or nil}
end

local function transfer_shortage_detail(source, params)
  local contents = source.get_contents()
  table.sort(contents, function(a, b)
    if a.name ~= b.name then return a.name < b.name end
    return a.quality < b.quality
  end)
  local preview = {}
  for index = 1, math.min(10, #contents) do
    local item = contents[index]
    preview[#preview + 1] = {name = item.name, count = item.count, quality = item.quality}
  end
  return {item = params.item, requested = params.count, available = 0,
          inventory_role = params.direction == "from_entity" and params.inventory_role or nil,
          contents = #preview > 0 and preview or nil}
end

local function do_transfer(state, params)
  local character, err = require_character(state)
  if not character then return nil, err end
  if state.action then return nil, "action_conflict", conflict_detail(state) end
  if (params.direction ~= "to_entity" and params.direction ~= "from_entity") or
     type(params.item) ~= "string" or type(params.count) ~= "number" or
     params.count < 1 or params.count > 100 or params.count % 1 ~= 0 then
    return nil, "invalid_input"
  end
  local target, roles, target_error, detail = get_entity_inventory(character, params)
  if not target then return nil, target_error, detail end
  local inventory_id = roles[params.inventory_role]
  if not inventory_id then
    return nil, "invalid_target", {entity = target.name, valid_roles = sorted_keys(roles)}
  end
  local entity_inventory = target.get_inventory(inventory_id)
  local player_inventory = character.get_main_inventory()
  if not entity_inventory or not entity_inventory.valid or
     not player_inventory or not player_inventory.valid then
    return nil, "invalid_target", {entity = target.name, valid_roles = sorted_keys(roles)}
  end
  local source = params.direction == "to_entity" and player_inventory or entity_inventory
  local destination = params.direction == "to_entity" and entity_inventory or player_inventory
  local item_id = {name = params.item, quality = "normal"}
  local count = math.min(params.count, source.get_item_count(item_id),
                         destination.get_insertable_count(item_id))
  if count == 0 then
    if source.get_item_count(item_id) == 0 then
      return nil, "missing_materials", transfer_shortage_detail(source, params)
    end
    return nil, "inventory_full"
  end
  local removed = source.remove{name = params.item, quality = "normal", count = count}
  if removed == 0 then
    local shortage = source.get_item_count(item_id) == 0 and
                     transfer_shortage_detail(source, params) or nil
    return nil, "missing_materials", shortage
  end
  local inserted = destination.insert{name = params.item, quality = "normal", count = removed}
  if inserted < removed then
    source.insert{name = params.item, quality = "normal", count = removed - inserted}
  end
  if inserted == 0 then return nil, "inventory_full" end
  return {moved = inserted, item = params.item, direction = params.direction,
          inventory_role = params.inventory_role}
end

local function do_action_status(state, params)
  local action = state.action
  if not action or action.id ~= params.action_id then
    action = state.completed_actions[params.action_id] or state.last_action
  end
  if not action or action.id ~= params.action_id then return nil, "action_not_found" end
  local detail = action.detail
  if action.status == "running" then
    if action.type == "mine" then
      detail = {gained = action.gained, count = action.goal_count}
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
    end
  end
  return {
    action_id = action.id, type = action.type, status = action.status,
    error = action.error, detail = detail,
    position = action.status == "running" and
               (valid_character(state) and position_data(state.character) or nil) or
               action.position
  }
end

local function do_cancel(state, params)
  if not state.action or state.action.id ~= params.action_id then
    return nil, "action_not_found"
  end
  if state.action.type == "craft" and valid_character(state) then
    pcall(state.character.cancel_crafting, {count = state.action.count, index = 1})
  end
  finish_action(state, "cancelled", "cancelled")
  return do_action_status(state, params)
end

local handlers = {
  ping = function()
    return {bridge_version = "0.1.0", factorio_version = script.active_mods.base,
            agent_id = AGENT_ID}
  end,
  get_agent_status = function(state) return status(state) end,
  observe_nearby = do_observe,
  get_inventory = function(state)
    local character, err = require_character(state)
    if not character then return nil, err end
    return {agent_id = AGENT_ID, items = inventory_contents(character)}
  end,
  get_entity_inventory = do_entity_inventory,
  list_craftable_recipes = do_list_craftable,
  get_recipe_info = do_recipe_info,
  get_research_status = do_research,
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
  check_placement = do_check_placement,
  rotate_entity = do_rotate,
  transfer_item = do_transfer,
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
    state.last_action = nil
    state.completed_actions = {}
    state.completed_order = {}
    state.targets = {}
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
  if event.try_again_later then
    state.action.retries = state.action.retries + 1
    if state.action.retries > 3 then
      finish_action(state, "failed", "path_not_found",
        path_not_found_detail(state.character, state.action.goal))
    else
      request_path(state, state.character, state.action.goal)
    end
    return
  end
  if not event.path or #event.path == 0 then
    finish_action(state, "failed", "path_not_found",
      path_not_found_detail(state.character, state.action.goal))
    return
  end
  state.action.path = event.path
  state.action.path_index = 1
  reset_move_progress(state.action)
end)

script.on_event(defines.events.on_tick, function()
  local state = ensure_storage()
  if not state.spawned_once then
    spawn_character()
  end
  local action = state.action
  if not action then return end
  local character = state.character
  if not valid_character(state) then
    finish_action(state, "failed", "character_dead")
    return
  end
  if game.tick - (state.last_heartbeat_tick or action.started_tick) > 60 * 10 then
    if action.type == "craft" then
      pcall(character.cancel_crafting, {count = action.count, index = 1})
    end
    finish_action(state, "failed", "connection_lost")
    return
  end
  if action.type ~= "craft" and action.type ~= "mine" and
     game.tick - action.started_tick > ACTION_TIMEOUT_TICKS then
    finish_action(state, "failed", "timeout")
    return
  end
  if action.type == "move" then
    if not action.path then return end
    local d = math.sqrt(distance_squared(character.position, action.goal))
    if d <= 0.5 then
      finish_action(state, "succeeded", nil, {position = position_data(character)})
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
          finish_action(state, "succeeded", nil,
            {position = position_data(character), distance = d, goal_blocked = true})
        else
          finish_action(state, "failed", "blocked")
        end
        return
      end
    end
    local destination = waypoint and waypoint.position or action.goal
    local dx = destination.x - character.position.x
    local dy = destination.y - character.position.y
    character.walking_state = {walking = true, direction = move_direction(dx, dy)}
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
        finish_action(state, "failed", "blocked")
      else
        request_path(state, character, action.goal)
      end
    end
  elseif action.type == "mine" then
    local inventory = character.get_main_inventory()
    if not inventory then
      finish_action(state, "failed", "inventory_full")
      return
    end
    local current = inventory.get_item_count()
    if current > action.last_inventory_count then
      action.gained = action.gained + current - action.last_inventory_count
      action.last_progress_tick = game.tick
    end
    action.last_inventory_count = current
    if action.gained >= action.goal_count then
      finish_action(state, "succeeded", nil,
        {gained = action.gained, inventory = inventory.get_contents()})
      return
    end
    local target = action.target
    if not target or not target.valid then
      finish_action(state, "failed", "target_gone", {reason = "entity_gone"})
      return
    end
    -- Actions saved before reach was recorded still use the target's reach rule.
    local reach = action.reach or ((target.type == "resource" or target.type == "tree" or
                  target.type == "simple-entity") and
                  character.resource_reach_distance or character.reach_distance)
    if (target.type == "resource" and
        distance_squared(character.position, target.position) > reach * reach) or
       not character.can_reach_entity(target) then
      finish_action(state, "failed", "out_of_range",
                    range_detail(character, target.position, reach))
      return
    end
    if inventory.is_full() then
      finish_action(state, "failed", "inventory_full")
      return
    end
    if game.tick - action.last_progress_tick >= 60 * 20 then
      finish_action(state, "failed", "blocked")
      return
    end
    if character.selected ~= target then
      character.selected = target
      character.mining_state = {mining = true, position = target.position}
    end
  elseif action.type == "craft" then
    if character.crafting_queue_size == 0 then
      finish_action(state, "succeeded", nil,
        {recipe = action.recipe, count = action.count,
         inventory = inventory_contents(character)})
      record_hand_crafted(state, character, action)
    end
  end
end)
