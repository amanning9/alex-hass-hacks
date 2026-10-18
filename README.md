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
  rooms.

  ```yaml
  - action: alex_hacks.dreame_vacuum_then_mop
    target:
      entity_id: vacuum.robbie
    data:
      rooms:
        - room: sensor.robbie_kitchen
          suction: turbo      # none (don't vacuum) / quiet / standard / strong / turbo
          wetness: 20         # 0 = don't mop, 1-32
          passes: 2
        - room: sensor.robbie_lounge_rug
          suction: none
  ```

## Mop drying

The *Dreame mop drying* hack adds, on the vacuum's device:

- `switch.<vacuum>_allow_drying`: turn it on and off from your own automations. Off stops
  drying; on resumes it, counting only the time still owed.
- `number.<vacuum>_mop_drying_time`: total drying per wash (default 180 minutes).
- `sensor.<vacuum>_mop_drying`: `dry`, `drying`, `paused` (wet, not allowed) or `waiting`
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
plus HACS validation and hassfest, on every push.
`tests/custom_components/dreame_vacuum` is a fake Dreame integration for the tests; don't
install it.
