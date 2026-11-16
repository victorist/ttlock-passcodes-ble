# TTLock BLE — local PIN code management

Adaptation of the project to work through the **ttlock-ble** integration (ESP32 BLE Proxy) without using the TTLock cloud.

> 🌐 Русская версия: [README.md](README.md)

## Repository layout
```
/config/
├── custom_components/
│   └── ttlock_ble_passcode/    ← component code only
│     ├── __init__.py             # Main component file
│     ├── manifest.json           # Component manifest
│     └── services.yaml           # Descriptions of the provided services
│     └── strings.json.           # In recent Home Assistant versions, registering services
│                                 # via 'hass.services.async_register' requires a 'strings.json'
│                                 # file in the integration folder that describes the service fields.
│                                 # Without it, HA may silently ignore the registration or
│                                 # fail with an error that is not always visible in the main log.
├── ble_passcode_scripts/       ← component scripts only
│   ├── ble_script.yaml           # PIN code creation script (BLE)
│   ├── ble_list_script.yaml      # PIN code list script (BLE)
│   ├── ble_delete_script.yaml    # Expired PIN code removal script (BLE)
├── ble_passcode_cards/          ← dashboard cards
│   ├── ble_card.yaml             # PIN code creation card
│   ├── ble_delete_card.yaml      # Expired PIN code removal card
│   └── ble_passcodes_card.yaml   # PIN code list card


```
## How it works

```
TTLock lock ←──BLE──→ ESP32 BLE Proxy ←──WiFi──→ Home Assistant
                                                          │
                                                    ttlock_ble_passcode
                                                    (custom component)
                                                          │
                                                    YAML scripts
                                                    (ble_script.yaml etc.)
```

1. **`ttlock-ble`** — an already installed integration that handles lock/unlock over BLE
2. **`ttlock_ble_passcode`** — a custom component (from the `custom_component/` folder) that adds PIN code services
3. **YAML scripts** — call the custom component services

## Step 1: Installing the custom component

Copy the `custom_component/` folder **from this repository** to the
`/config/custom_components/ttlock_ble_passcode/` directory on your Home Assistant server:

```bash
# From the repository root (on the machine hosting the repository):
cp -r ttlock_ble/custom_component/ /config/custom_components/ttlock_ble_passcode/
```

Or manually create the `/config/custom_components/ttlock_ble_passcode/` folder
and copy three files into it:
```
/config/custom_components/ttlock_ble_passcode/__init__.py
/config/custom_components/ttlock_ble_passcode/manifest.json
/config/custom_components/ttlock_ble_passcode/services.yaml
```

> ⚠️ This is the **custom Python component only** (not the YAML scripts).
> The YAML scripts are copied separately — see Step 2.

### Add to `configuration.yaml`:

```yaml
ttlock_ble_passcode:
```

Restart Home Assistant.

## Step 2: Copying the YAML scripts to HA and registering them

> ℹ️ The YAML scripts (`ble_script.yaml` etc.) are **separate files** from the custom
> component. They do not go into `custom_components/` — put them into the
> `/config/ble_passcode_scripts/` folder.

### 2а. Copy the script and card YAML files to HA:

```bash
# Create the folder on HA and copy the scripts:
cp -r ttlock_ble/ /config/ble_passcode_scripts/
```

As a result, the following structure should appear on HA:
```
/config/ble_passcode_scripts/
├── ble_script.yaml
├── ble_list_script.yaml
├── ble_delete_script.yaml
├── ble_card.yaml
├── ble_passcodes_card.yaml
└── ble_delete_card.yaml
```

### 2б. Add the includes to `/config/scripts.yaml`:

```yaml
sozdat_pin_kod_ttlock_ble:
  !include ble_passcode_scripts/ble_script.yaml

ttlock_ble_pokazat_spisok_pin_kodov:
  !include ble_passcode_scripts/ble_list_script.yaml

ttlock_ble_udalit_istekshie_pin_kody:
  !include ble_passcode_scripts/ble_delete_script.yaml
```

Reload the scripts: **Developer Tools → YAML → Reload scripts**.

## Step 3: Adding dashboard cards

Add the cards from the `ble_card.yaml`, `ble_passcodes_card.yaml`, `ble_delete_card.yaml` files via Lovelace Dashboard → Edit → Add Card → Manual.

## What changed compared to the cloud version

| | Cloud (`ttlock/`) | BLE (`ttlock_ble/`) |
|--|--|--|
| Lock | `integration: ttlock` | `integration: ttlock_ble` |
| Create PIN | `ttlock.create_passcode` | `ttlock_ble_passcode.add_passcode` |
| List PINs | `ttlock.list_passcodes` | `ttlock_ble_passcode.get_passcodes` |
| Delete PIN | `ttlock.delete_passcode` + ID | `ttlock_ble_passcode.delete_passcode` + code |
| Delays | 20 sec (cloud) | 10 sec (direct BLE) |
| Cloud | Required | Not used |

## Important limitation

Over BLE only **Custom Passcodes** are visible (the ones physically stored in the lock's memory):
- ✅ Period Passcode (with start/end dates)
- ✅ Permanent Passcode
- ✅ Circle Passcode (recurring)
- ❌ Random Timed Passcode (algorithmic) — not stored in the lock, cloud only

This means that if Random Timed Passcodes were created via the TTLock app before switching to BLE, they will not appear in the BLE list. New codes created through this script are Custom Period Passcodes and will be visible.

## Requirements

- Home Assistant with the `ttlock-ble` integration (HACS)
- ESP32 BLE Proxy within range of the locks
- Locks added with an **Admin Key** (required to create/remove PIN codes)

## Troubleshooting common errors

- **"expected str at 'passcode'"** — Browser Mod sends a numeric PIN as a number;
  the `passcode` field in `ble_script.yaml` is intentionally declared **without** a
  `selector` (HA validates script fields against selectors), and the component
  uses `vol.Coerce(str)`.
- **"No response from the lock" / "BLE unavailable"** — a TTLock cannot be woken up
  by a BLE request: the lock only accepts a GATT connection while it is active
  (advertising after a keypad/sensor touch). Touch the lock's keypad and retry.
  The detailed reason is in the HA log (`get_passcodes: BLE error` /
  `No BLE connection for ...`).
