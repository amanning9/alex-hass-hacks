# Alex's Hacks

Each hack is separate: added on its own in Home Assistant (Settings → Devices & services →
Add integration → Alex's Hacks), and removable on its own once upstream catches up.
Generated from the hack modules by `tools/gen_hacks_md.py`; edit the modules, not this file.

## Dreame cleaning mode (`dreame_cleaning_mode`)

**What:** Adds "Cleaning mode (fixed)" to the vacuum, offering "mopping after sweeping" whenever the robot would accept it. Raises a repair once a newer Dreame Vacuum version has run a week without the bug.

**Why:** Dreame Vacuum v2.0.2 removes mopping_after_sweeping from its cleaning-mode options when a task starts and only re-adds it at the moment the task status changes, if the robot doesn't look busy then. Task status and status arrive separately, so it can miss that moment and the option stays missing until the next task.

**Remove when:** Dreame Vacuum re-checks the option on status changes too (the repair says when it looks fixed).

## Dreame mop drying (`dreame_mop_drying`)

**What:** Adds "Allow drying" (switch), "Mop drying time" (number, default 3 hours) and "Mop drying" (sensor: dry / drying / paused / waiting / needs water). A mop that hasn't been washed (e.g. carried back from a floor without the base) is washed first, once the base has water. Needs the robot's own auto-drying off (it raises a repair and leaves drying to the robot while it's on). The pads count as wet from the start of each mop (or mop wash) until they've dried for the drying time in total. While they're wet and drying is allowed, it starts drying whenever the robot is docked; turning the switch off stops it, and turning it back on carries on where it left off. It keeps checking the robot really has stopped, and raises a repair if it won't. Progress is saved, so restarts don't lose it.

**Why:** Dreame drying can only be started or stopped: every start is a fresh run of the robot's drying time (whole hours), and nothing says whether the pads are still wet. So drying can't be paused (e.g. while it's noisy or electricity is dear) and resumed. Dreame's stop also quietly does nothing whenever it thinks drying couldn't be started (e.g. it thinks a task is still running).

**Remove when:** Dreame (or the integration) offers pausing and resuming drying, or reports how dry the pads are. Remove the hack's entities from your automations, then turn the robot's own auto-drying back on if you'd turned it off.

## Dreame rooms (`dreame_rooms`)

**What:** Adds a sensor per room on one of the robot's maps (the one it's on when the hack is added), named as in the Dreame app, whose state is the room number. Pickers can filter to these (integration: alex_hacks, domain: sensor), so they list only rooms, and a room the robot splits in two shows as two. If a room disappears from that map, its entity goes unavailable (not deleted, so automations keep their references) and a repair says which room; submitting the repair removes the entity. While the robot is on another map, nothing changes.

**Why:** Dreame Vacuum's per-room entities can't be told apart from its other entities in an entity picker (no device class to filter on), and Home Assistant's area mapping for vacuums can't be read from templates, so automations can't turn a picked area into room numbers, and an area covering two robot rooms can't be split.

**Remove when:** Dreame Vacuum gives each room a filterable entity or device, or Home Assistant exposes the vacuum area mapping to templates (and part of an area can be picked).

## Dreame vacuum then mop (`dreame_vacuum_then_mop`)

**What:** Adds the alex_hacks.dreame_vacuum_then_mop action: vacuum the rooms, wait until the robot has completely finished (not just a recharge, mop wash or auto-empty), then mop them, each room with its own suction, wetness and passes. Rooms are picked from the Dreame rooms hack. Refuses to start if the robot is on a different map from the rooms. Turns CleanGenius off and customized cleaning on while it runs, then puts both back as they were.

**Why:** Per-room suction and wetness need customized cleaning, which can't be combined with the robot's own mopping after sweeping; and Dreame Vacuum has no single "completely finished" signal (task status briefly reads completed between phases and on mid-run returns), so running the two phases needs careful waiting.

**Remove when:** The robot (or Dreame Vacuum) supports mopping after sweeping with per-room settings, or exposes a reliable "whole job finished" signal so a plain script can do this.

## Ecovacs animal protection (`ecovacs_animal_protection`)

**What:** Adds "Animal protection" (switch) and its "start" and "end" times to an Ecovacs GOAT mower, so they can be set from automations (e.g. following sunset and sunrise). Changes made in the Ecovacs app show up straight away; nothing is polled.

**Why:** Home Assistant's Ecovacs integration (deebot-client 18.7.0) doesn't support the GOAT's animal protection setting, so it can only be changed in the Ecovacs app.

**Remove when:** deebot-client supports animal protection for the mower (DeebotUniverse/client.py PR #1778) and the Ecovacs integration exposes it (a repair says when the library has it).

## Ecovacs area settings (`ecovacs_area_settings`)

**What:** Adds, for each mowing area of an Ecovacs GOAT mower, its mowing direction, mowing height, mowing speed and obstacle avoidance mode, so they can be set from automations (e.g. turning the stripes every week). Areas are named as in the Ecovacs app. Changes made in the app show up straight away; nothing is polled.

**Why:** Home Assistant's Ecovacs integration (deebot-client 18.7.0) doesn't support the GOAT's per-area settings, so they can only be changed in the Ecovacs app.

**Remove when:** deebot-client supports area settings for the mower (DeebotUniverse/client.py PR #1767) and the Ecovacs integration exposes them (a repair says when the library has it).

## Person notify (`person_notify`)

**What:** Adds a notify entity per person (e.g. notify.person_alex) that sends to every phone they're signed in to with the companion app, and the action "alex_hacks.notify_person" which does the same and also passes "data" through (buttons, links, images, channels). Phones are found from the person's device trackers, so adding or removing one needs no change.

**Why:** The companion app only offers one notify action per phone (notify.mobile_app_<phone>), so every automation has to list each person's phones, and a new phone means editing them all. Notify groups have to be kept up to date by hand, and Home Assistant's notify entities can't carry data, so they can't send actionable notifications.

**Remove when:** Home Assistant (or the companion app) can notify a person, or a person's devices, directly, with notification data. Switch automations over, then delete the entry.
