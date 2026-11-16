# TTLock BLE — локальное управление PIN-кодами

Адаптация проекта для работы через интеграцию **ttlock-ble** (BLE Proxy на ESP32) без использования облака TTLock.

> 🌐 English version: [README_EN.md](README_EN.md)

## Новая структура репозитория
```
/config/
├── custom_components/
    └── ttlock_ble_passcode/    ← только код компонента
      ├── __init__.py             # Основной файл компонента
      ├── manifest.json           # Манифест компонента
      └── services.yaml           # Описание предоставляемых сервисов
      └── strings.json.           # В современных версиях Home Assistant регистрация сервисов 
                                  # через 'hass.services.async_register' требует наличия файла
                                  # 'strings.json' в папке интеграции для описания полей сервисов. 
                                  # Без него HA может молча игнорировать регистрацию или 
                                  # падать с ошибкой, которая не всегда явно видна в основном логе.
└── ble_passcode_scripts/       ← только скрипты компонента
    ├── ble_script.yaml           # Скрипт создания PIN-кода (BLE)
    ├── ble_list_script.yaml      # Скрипт просмотра списка PIN-кодов (BLE)
    ├── ble_delete_script.yaml    # Скрипт удаления истёкших PIN-кодов (BLE)
└── ble_passcode_cards/          ← карточки для дашборда
    ├── ble_card.yaml             # Карточка создания PIN-кода
    ├── ble_delete_card.yaml      # Карточка удаления истёкших PIN-кодов
    └── ble_passcodes_card.yaml   # Карточка просмотра PIN-кодов


```
## Как это работает

```
Замок TTLock ←──BLE──→ ESP32 BLE Proxy ←──WiFi──→ Home Assistant
                                                          │
                                                    ttlock_ble_passcode
                                                    (кастомный компонент)
                                                          │
                                                    YAML Scripts
                                                    (ble_script.yaml и др.)
```

1. **`ttlock-ble`** — уже установленная интеграция, управляет lock/unlock через BLE
2. **`ttlock_ble_passcode`** — кастомный компонент (из папки `custom_component/`), добавляет сервисы для PIN-кодов
3. **YAML-скрипты** — вызывают сервисы кастомного компонента

## Шаг 1: Установка кастомного компонента

Скопируй папку `custom_component/` **из этого репозитория** в директорию
`/config/custom_components/ttlock_ble_passcode/` на сервере Home Assistant:

```bash
# Из корня репозитория (на машине с репозиторием):
cp -r ttlock_ble/custom_component/ /config/custom_components/ttlock_ble_passcode/
```

Или вручную создай папку `/config/custom_components/ttlock_ble_passcode/`
и скопируй в неё три файла:
```
/config/custom_components/ttlock_ble_passcode/__init__.py
/config/custom_components/ttlock_ble_passcode/manifest.json
/config/custom_components/ttlock_ble_passcode/services.yaml
```

> ⚠️ Это **только кастомный Python-компонент** (не YAML-скрипты).
> YAML-скрипты копируются отдельно — см. Шаг 2.

### Добавить в `configuration.yaml`:

```yaml
ttlock_ble_passcode:
```

Перезапусти Home Assistant.

## Шаг 2: Копирование YAML-скриптов на HA и их подключение

> ℹ️ YAML-скрипты (`ble_script.yaml` и др.) — это **отдельные файлы** от кастомного
> компонента. Они идут не в `custom_components/`, а в папку `/config/ble_passcode_scripts/`.

### 2а. Скопируй YAML-файлы скриптов и карточек на HA:

```bash
# Создать папку на HA и скопировать скрипты:
cp -r ttlock_ble/ /config/ble_passcode_scripts/
```

В итоге на HA должна появиться структура:
```
/config/ble_passcode_scripts/
├── ble_script.yaml
├── ble_list_script.yaml
├── ble_delete_script.yaml
├── ble_card.yaml
├── ble_passcodes_card.yaml
└── ble_delete_card.yaml
```

### 2б. Добавь включения в `/config/scripts.yaml`:

```yaml
sozdat_pin_kod_ttlock_ble:
  !include ble_passcode_scripts/ble_script.yaml

ttlock_ble_pokazat_spisok_pin_kodov:
  !include ble_passcode_scripts/ble_list_script.yaml

ttlock_ble_udalit_istekshie_pin_kody:
  !include ble_passcode_scripts/ble_delete_script.yaml
```

Перезагрузи скрипты: **Инструменты разработчика → YAML → Перезагрузить скрипты**.

## Шаг 3: Добавление карточек дашборда

Добавь карточки из файлов `ble_card.yaml`, `ble_passcodes_card.yaml`, `ble_delete_card.yaml` через Lovelace Dashboard → Edit → Add Card → Manual.

## Что изменилось по сравнению с облачной версией

| | Облачная (`ttlock/`) | BLE (`ttlock_ble/`) |
|--|--|--|
| Замок | `integration: ttlock` | `integration: ttlock_ble` |
| Создание PIN | `ttlock.create_passcode` | `ttlock_ble_passcode.add_passcode` |
| Список PIN | `ttlock.list_passcodes` | `ttlock_ble_passcode.get_passcodes` |
| Удаление | `ttlock.delete_passcode` + ID | `ttlock_ble_passcode.delete_passcode` + код |
| Задержки | 20 сек (облако) | 10 сек (BLE прямой) |
| Облако | Требуется | Не используется |

## Важное ограничение

Через BLE видны только **Custom Passcode** (те, что физически записаны в память замка):
- ✅ Period Passcode (с датами начала/конца)
- ✅ Permanent Passcode
- ✅ Circle Passcode (циклические)
- ❌ Random Timed Passcode (алгоритмические) — не хранятся в замке, только в облаке

Это значит, что если до перехода на BLE создавались Random Timed Passcode через приложение TTLock, они не будут видны в BLE-списке. Новые коды, созданные через этот скрипт, будут Custom Period Passcode и будут видны.

## Требования

- Home Assistant с интеграцией `ttlock-ble` (HACS)
- ESP32 BLE Proxy в зоне видимости замков
- Замки добавлены с **Admin Key** (нужен для создания/удаления PIN-кодов)

## Отладка типовых ошибок

- **«expected str at 'passcode'»** — Browser Mod передаёт цифровой PIN как число;
  поле `passcode` в `ble_script.yaml` намеренно объявлено **без** `selector` (HA
  валидирует поля скрипта по selector'ам), а компонент использует `vol.Coerce(str)`.
- **«Нет ответа от замка» / «BLE недоступен»** — TTLock нельзя разбудить BLE-запросом:
  замок принимает GATT-соединение только пока сам активен (рекламируется после нажатия
  на клавиатуре/сенсоре). Нажмите на клавиатуре замка и повторите. Подробная причина —
  в логе HA (`get_passcodes: ошибка BLE` / `Нет BLE-соединения для ...`).

