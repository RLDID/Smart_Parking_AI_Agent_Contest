from simulator.world import initial_world, advance, set_observation_mode


def test_stationary_gate_sensor_uses_visible_observation_and_unknown_on_loss():
    world = initial_world(1, 's3-closing-v1')
    for _ in range(4):
        advance(world)
    gates = world['device_state']['gates']
    assert all(g['physical_state'] == 'open' and g['obstacle_detected'] is False for g in gates)
    version = [g['resource_version'] for g in gates]
    for _ in range(4):
        advance(world)
    assert [g['resource_version'] for g in world['device_state']['gates']] == version
    set_observation_mode(world, 'unavailable')
    for _ in range(4):
        advance(world)
    assert all(g['obstacle_detected'] is None for g in world['device_state']['gates'])


def test_stationary_gate_fault_does_not_claim_sensor_clear():
    world = initial_world(2, 's3-closing-v1')
    world['device_faults']['gate'] = True
    for _ in range(4):
        advance(world)
    assert all(g['obstacle_detected'] is None for g in world['device_state']['gates'])
