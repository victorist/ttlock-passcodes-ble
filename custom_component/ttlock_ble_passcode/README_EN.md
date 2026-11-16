# ttlock_ble_passcode — custom component

This component adds PIN code management services to the `ha-ttlock-ble` integration.
Codes are stored in the lock itself and managed directly over BLE — the TTLock cloud is not used.

> 🌐 Русская версия: [README.md](README.md)

## Installation

Copy the `ttlock_ble_passcode/` folder into the `custom_components/` directory of your Home Assistant:

```
/config/custom_components/ttlock_ble_passcode/
  __init__.py
  manifest.json
  services.yaml
```

Then restart Home Assistant.

## Provided services

After installation the following services become available (Developer Tools → Services):

### `ttlock_ble_passcode.add_passcode`
Adds a PIN code to the lock over BLE.

| Parameter | Type | Description |
|---------|-----|----------|
| `entity_id` | entity | Lock (`ttlock_ble` integration, `lock` domain) |
| `passcode` | string | PIN code (4–9 digits) |
| `name` | string | Code name; after creation it is saved automatically into the integration's name entity |
| `start_time` | string | Start in `YYYY-MM-DD HH:MM` format |
| `end_time` | string | End in `YYYY-MM-DD HH:MM` format |
| `timeout` | number | BLE advertising window wait, seconds (10–600) |

Returns in `response_variable`:
```yaml
success: true
passcode: "12345"
```

### `ttlock_ble_passcode.get_passcodes`
Reads the list of PIN codes from the lock's memory over BLE.

| Parameter | Type | Description |
|---------|-----|----------|
| `entity_id` | entity | Lock |
| `timeout` | number | BLE advertising window wait, seconds (10–600) |

Returns:
```yaml
passcodes:
  - passcode: "123456"
    name: "Xiao's key"          # if the name is set in the integration's text entity
    type: "period"
    start_date: "2024-01-01 00:00"
    end_date: "2024-12-31 23:59"
  - passcode: "654321"
    type: "permanent"          # no name — the field is simply absent
```

> 💡 **Code names.** The lock itself does not store names. The `ttlock_ble`
> integration creates a "name" text entity for every known code — set the
> name there (lock device → an entity like `Passcode name 1•••56`),
> and it will appear in the service response and in the list popup.

> ⚠️ Only Custom Passcodes are visible (written into the lock's memory).
> Random Timed Passcodes (algorithmic) are not visible over BLE.

### `ttlock_ble_passcode.delete_passcode`
Removes a PIN code from the lock by its value.

| Parameter | Type | Description |
|---------|-----|----------|
| `entity_id` | entity | Lock |
| `passcode` | string | Code to remove |
| `timeout` | number | BLE advertising window wait, seconds (10–600) |

## Event-driven start: how the advertising window wait works (v1.1)

TTLock locks are in deep sleep most of the time: the radio is active only when
transmitting advertisement packets (roughly once every ~30 s), and a GATT
connection is possible only inside that window. Therefore a service call started
at a random moment often failed with "Lock unavailable over BLE".

Now the services follow the semantics "put the operation into a queue and return
the result when the lock shows up on air":

1. A call to `add_passcode` / `get_passcodes` / `delete_passcode` puts the operation
   into a per-lock FIFO queue (`hass.data[DOMAIN]`). Operations for one lock run
   strictly sequentially; for different locks — in parallel.
2. "Lock on air" triggers (in priority order):
   - **bluetooth callback** on the lock's MAC — the main path: when an advertisement
     packet appears, the queue's wake event is set;
   - the lock device's **"Last seen" sensor** (fallback when the bluetooth API
     is unavailable): wake on sensor state update;
   - **immediate attempts with retries** (if no trigger entities exist).
3. The queue executor waits for the wake event (unless the lock looks "awake" by its
   state or the "Bluetooth connection" diagnostic entity), then makes up to
   `max_attempts_per_window` attempts with a `wake_retry_gap` pause, until the
   `timeout` expires (default `default_timeout: 90`, can be overridden by the call's
   `timeout` field, 10–600 s).
4. When the timeout expires the operation fails with
   "Timeout waiting for the BLE advertising window (N s) for <entity>". Scripts do not
   need changes — the service contract has not changed.

Logs: the "enqueue → wake event → attempt → success" chain is visible with
`logger: {logs: {custom_components.ttlock_ble_passcode: debug}}` in
`configuration.yaml`.

## Requirements

- Home Assistant 2024.1+
- The `ha-ttlock-ble` integration installed and configured
- Locks configured with an Admin Key (required to change PIN codes)
