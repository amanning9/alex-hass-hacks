# Alex's Hacks

A Home Assistant integration holding small, separate workarounds for gaps in other
integrations. Each hack:

- lives in its own module (`custom_components/alex_hacks/hacks/<key>.py`),
- is added on its own (one entry per hack and target), so it can be removed on its own,
- records **what** it does, **why** it's needed and **what upstream change would let it go**,
  shown when you add it and listed in [HACKS.md](HACKS.md).

## Install

With HACS:

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/amanning9/alex-hass-hacks`,
   type **Integration**.
2. Find **Alex's Hacks** in HACS, download it and restart Home Assistant.

By hand: copy `custom_components/alex_hacks` into `/config/custom_components/` and restart.

Then: Settings → Devices & services → Add integration → **Alex's Hacks** → tick the hacks you want
   (each becomes its own entry, removable on its own).

## Actions

- `alex_hacks.dreame_vacuum_then_mop` (*Dreame vacuum then mop* hack; pick rooms from the
  *Dreame rooms* hack): vacuum the rooms, wait until the robot has completely finished, then mop
  them. Call it from any automation or script; it waits until the whole clean is done, so
  following steps run afterwards. Refuses to start if the robot is on a different map from the
  rooms. A phase no room needs is skipped: give every room `wetness: 0` to only vacuum, or
  `suction: none` to only mop. (Wetness defaults to 16, so rooms mop unless told not to.)

  ```yaml
  - action: alex_hacks.dreame_vacuum_then_mop
    target:
      entity_id: vacuum.robbie
    data:
      rooms:
        - room: sensor.robbie_dreame_rooms_kitchen
          suction: turbo      # none (don't vacuum) / quiet / standard / strong / turbo
          wetness: 20         # 0 = don't mop, 1-32
          passes: 2
          vacuum_route: quick # optional: standard / quick
          mop_route: deep     # optional: standard / intensive / deep / quick
        - room: sensor.robbie_dreame_rooms_lounge_rug
          suction: none
  ```

  Routes are optional. A mop route is set per room (rooms without one get standard if any
  room has one). The robot only takes a vacuum route for the whole clean, so when any room has
  one the vacuum phase runs with customized cleaning off, the robot's cleaning mode set to
  sweeping and its route set: one run per stretch of rooms sharing a route, in the order
  given (rooms without one get standard), each room still with its own suction and passes. The
  next run starts as soon as the last one finishes, without waiting for the robot to get home.
  The robot's cleaning mode and route are put back afterwards.

## Mop drying

The *Dreame mop drying* hack adds, on its own device ("<vacuum>: Dreame mop drying"):

- `switch.<vacuum>_dreame_mop_drying_allow_drying`: turn it on and off from your own automations. Off stops
  drying; on resumes it, counting only the time still owed.
- `number.<vacuum>_dreame_mop_drying_mop_drying_time`: total drying per wash (default 180 minutes).
- `sensor.<vacuum>_dreame_mop_drying_mop_drying`: `dry`, `drying`, `paused` (wet, not allowed) or `waiting`
  (wet and allowed, but the robot is out or busy), or `robot_auto_drying` (see below), with `minutes_left` and `dried_minutes`.

Turn the robot's own auto-drying **off** (its Auto drying switch, or in the Dreame app).
While it's on, the hack leaves drying to the robot (the sensor reads `robot_auto_drying`, and
Allow drying does nothing) and raises a repair until it's off; submitting the repair turns
it off for you.

The pads count as wet from the start of each mop (with customized cleaning, from the base's
first mop wash, since per-room modes aren't reported). Drying is lowest priority: it only
starts once the robot and base have been idle for 3 minutes (after a mop, once its wash has
happened; if no wash comes within 5 minutes, e.g. it was carried back from a floor without the
base, the hack starts one, waiting for the base's tank to be refilled if it's empty, when the
sensor reads `needs_water`), and anything else that starts takes over. If the
robot's own run ends early the hack starts another. Drying started from the Dreame button
while allowed is left alone. While the mop is wet and drying is allowed, stopping it from the
Dreame app or button only lasts a couple of minutes; turn Allow drying off instead.

Stopping doesn't use Dreame's own stop (which quietly does nothing whenever Dreame thinks
drying couldn't start): the hack sends the robot's drying-off command itself, checks the robot
reports it stopped, retries every 2 minutes, and raises a repair after 3 tries.

## Person notify

The *Person notify* hack adds `notify.person_<person>` (e.g. `notify.person_ada`) for every
person, sending to each phone they're signed in to with the companion app (found from the
person's device trackers, so new phones are picked up by themselves). Use it with
`notify.send_message` for plain messages:

```yaml
- action: notify.send_message
  target:
    entity_id: notify.person_ada
  data:
    title: Robbie
    message: Carry him back to the base.
```

Notify entities can't carry notification data, so for buttons, links, images or channels use
the action, which takes the same `data` as `notify.mobile_app_<phone>`:

```yaml
- action: alex_hacks.notify_person
  data:
    person: person.ada
    message: The washing machine has finished. Unload it now?
    data:
      actions:
        - action: UNLOAD_NOW
          title: On my way
```

Each entity's `phones` attribute lists the phones it will send to. One phone failing doesn't
stop the others; it's an error only if nobody could be notified.

## Ecovacs animal protection

The *Ecovacs animal protection* hack adds, for an Ecovacs GOAT mower (on its own device,
"<mower>: Ecovacs animal protection"):

- `switch.<mower>_ecovacs_animal_protection_animal_protection`: animal protection on or off.
- `time.<mower>_ecovacs_animal_protection_animal_protection_start` and `…_end`: the nightly
  window when the mower stays docked.

It reads the setting once at start-up (and again when the mower comes back online); changes
made in the Ecovacs app arrive through the Ecovacs integration's existing connection, so
nothing is polled. To follow sunset and sunrise through the year:

```yaml
- alias: Mower animal protection follows the sun
  triggers:
    - trigger: time
      at: "12:00:00"
    - trigger: homeassistant
      event: start
  actions:
    # At midday, next_setting is this evening and next_rising is tomorrow morning.
    - action: time.set_value
      target:
        entity_id: time.goaty_ecovacs_animal_protection_animal_protection_start
      data:
        time: "{{ (as_datetime(state_attr('sun.sun', 'next_setting')) | as_local).strftime('%H:%M') }}"
    - action: time.set_value
      target:
        entity_id: time.goaty_ecovacs_animal_protection_animal_protection_end
      data:
        time: "{{ (as_datetime(state_attr('sun.sun', 'next_rising')) | as_local).strftime('%H:%M') }}"
```

## Ecovacs area settings

The *Ecovacs area settings* hack adds, for each mowing area of an Ecovacs GOAT mower (on its
own device, "<mower>: Ecovacs area settings"), named as in the Ecovacs app:

- `number.<mower>_ecovacs_area_settings_<area>_direction`: the mowing direction, in degrees,
  as the app shows it.
- `number.…_<area>_mowing_height`: 3 to 8 cm, in half centimetres.
- `select.…_<area>_mowing_speed`: Delicate (0.35 m/s) or Quick (0.5 m/s).
- `select.…_<area>_obstacle_avoidance_mode`: Short grass environment (avoids things 10 cm
  or taller), Normal environment (15 cm) or High grass environment (20 cm).

Each has a `device_value` attribute with the mower's own value. A select shows unknown if the
mower reports a value it doesn't know.

The mower takes all of an area's settings at once, so changing one sends the others unchanged.
Settings are read at start-up (and when the mower comes back online); after that the mower's
own updates and changes made in the app arrive without polling. Renaming an area in the app
renames the entities (their ids stay). To turn the stripes a little every week:

```yaml
- alias: Turn the lawn stripes weekly
  triggers:
    - trigger: time
      at: "06:00:00"
  conditions:
    - condition: time
      weekday: mon
  actions:
    - action: number.set_value
      target:
        entity_id: number.goaty_ecovacs_area_settings_front_direction
      data:
        value: "{{ (states('number.goaty_ecovacs_area_settings_front_direction') | int + 45) % 360 }}"
```

## Adding a hack

Add `hacks/<key>.py` with `KEY`, `TITLE`, `WHAT`, `WHY`, `REMOVE_WHEN`, `PLATFORMS` and the
functions listed in `hacks/__init__.py`, register it in `HACKS`, add a `<platform>.py`
pass-through if it uses a new platform, then run `uv run tools/gen_hacks_md.py`.

## Releasing

Bump `version` in `custom_components/alex_hacks/manifest.json` (e.g. `1.0.2`, no "v"), commit, then tag and push
(`git tag v1.0.2 && git push --tags`). The release workflow checks the tag matches the manifest
and publishes a GitHub release, which HACS offers as an update.

## Tests

From the repository root, with [uv](https://docs.astral.sh/uv/): `uv run pytest tests` (uv
installs the pinned test harness from `uv.lock` first). CI runs these,
plus HACS validation and hassfest, on every push. CI also checks `.python-version` and
`requires-python` match what the locked Home Assistant needs (`uv run --no-project
tools/check_python.py`); when Renovate bumps one, it says what else to change.
`tests/custom_components/dreame_vacuum` is a fake Dreame integration for the tests; don't
install it.
