# Техническое задание: событийный старт BLE-операций в компоненте `ttlock_ble_passcode` (v1.1)

## 1. Контекст и проблема (для понимания агентом)

Замки TTLock работают через ESP32 BLE Proxy и большую часть времени находятся в deep sleep: радиомодуль активен только в моменты передачи advertisement-пакетов (по логам устройства — примерно раз в 30 с). GATT-соединение может быть установлено **только внутри окна рекламы**. Запрос соединения, стартовавший в случайный момент, часто умирает по внутреннему таймауту раньше, чем открывается окно, — поэтому сервисы компонента возвращают «Замок недоступен по BLE», хотя замок физически достижим.

Текущий обходной путь в YAML-скриптах («пробуждение» вызовом `get_passcodes` перед операцией) ненадёжен и дублируется в трёх скриптах.

**Цель:** перенести логику ожидания окна рекламы внутрь компонента. Семантика сервиса становится: «положи операцию в очередь и верни результат, когда она выполнена в момент появления замка в эфире». Скрипты при этом не меняются.

## 2. Целевая архитектура

```
Вызов сервиса (add/get/delete)
        │  (создать Future, положить операцию в очередь замка)
        ▼
   LockQueue[entity_id]  ──await Future with timeout──►  возврат результата вызывающему
        │
        │ триггер: advertisement-событие (замок «в эфире»)
        ▼
   Executor (per lock) ── попытка операции (через connection._lock /
                          _async_ensure_connected_locked) ──► Future.set_result / set_exception
```

## 3. Область изменений

| Файл | Изменение |
|---|---|
| `custom_components/ttlock_ble_passcode/__init__.py` | Основная реализация (очередь, триггер, executor, новые обработчики) |
| `custom_components/ttlock_ble_passcode/manifest.json` | `version: "1.1.0"` |
| `custom_components/ttlock_ble_passcode/services.yaml` | Опциональное поле `timeout` у всех трёх сервисов |
| `custom_components/ttlock_ble_passcode/strings.json` | Зеркально добавить `timeout` в описание сервисов (без синхронизации сервисы могут не отобразиться в UI — проверено эмпирически) |
| `custom_components/ttlock_ble_passcode/README.md` | Раздел «Событийный старт: как работает ожидание окна рекламы» |

**Запрещено:** модифицировать файлы интеграции `ttlock_ble`; менять имена сервисов, обязательные поля схем и формат возвращаемых ответов; править YAML-скрипты `ble_script.yaml`, `ble_list_script.yaml`, `ble_delete_script.yaml` (они должны остаться совместимыми без правок).

## 4. Функциональные требования

**FR-1. Очередь операций.** В `hass.data[DOMAIN]` хранить структуру очередей по замкам. Операция описывается dataclass:
```python
@dataclass
class PasscodeOperation:
    op: Literal["add", "get", "delete"]
    entity_id: str
    params: dict          # passcode/start_sdk/end_sdk и т.д.
    timeout: float        # секунд, из вызова или из конфигурации
    future: asyncio.Future
    created: float        # loop.time()
```
Очередь — FIFO; на один замок одновременно выполняется не более одной операции.

**FR-2. Основной триггер «замок в эфире».** Использовать API компонента `bluetooth`: `async_register_callback` с `BluetoothCallbackMatcher(address=<LOCK_MAC>)`, где MAC берётся из `key.lockMac` (нормализовать к верхнему регистру). Колбэк взводит `asyncio.Event` («wake event») соответствующей очереди и пишет timestamp в кольцевой буфер (для будущей оценки периода, см. «Вне объёма»). Импорт `homeassistant.components.bluetooth` выполнять лениво внутри функции; при `ImportError`/отсутствии интеграции — переход на fallback (FR-3).

**FR-3. Fallback-триггер.** Если bluetooth-колбэк недоступен: отслеживать изменение состояния сущности «Last seen» устройства замка через `async_track_state_change_event`. Поиск сущности: entity registry → `device_id` сущности замка → сущности того же устройства → sensor с `device_class == "timestamp"` либо именем/`original_name`, содержащим `last_seen`/`Last seen`. Если и её нет — режим «немедленные попытки с ретраями» (FR-4 без ожидания события).

**FR-4. Алгоритм executor (псевдокод):**
```python
async def _async_execute_queued(queue: LockQueue, op: PasscodeOperation) -> None:
    async with queue.exec_lock:
        deadline = loop.time() + op.timeout
        while loop.time() < deadline:
            if not _lock_looks_awake(hass, op.entity_id):     # state not in ("unknown","unavailable")
                try:
                    await asyncio.wait_for(queue.wake_event.wait(),
                                           timeout=deadline - loop.time())
                except TimeoutError:
                    break
                queue.wake_event.clear()
            for _ in range(cfg["max_attempts_per_window"]):
                try:
                    result = await _async_run_op(op)   # текущая логика обработчиков
                    op.future.set_result(result)
                    return
                except HomeAssistantError:
                    await asyncio.sleep(cfg["wake_retry_gap"])
        if not op.future.done():
            op.future.set_exception(HomeAssistantError(
                f"Таймаут ожидания окна BLE-рекламы ({op.timeout} с) для {op.entity_id}"))
```
`_lock_looks_awake` = состояние сущности замка не в `("unknown", "unavailable")` **или** диагностическая сущность «Bluetooth connection» устройства в состоянии connected.

**FR-5. Семантика обработчиков сервисов.** Вынести тела текущих `handle_add_passcode` / `handle_get_passcodes` / `handle_delete_passcode` в `_async_run_op`-функции, возвращающие dict (формат ответов не менять!). Обработчики становятся тонкими: валидация схемы → `_get_connection` → создать `PasscodeOperation` → положить в очередь → `await asyncio.wait_for(op.future, op.timeout + 5)` → вернуть результат; `TimeoutError` превращать в `HomeAssistantError` с понятным текстом. Ошибку «сущность не принадлежит ttlock_ble» по-прежнему отдавать сразу, без очереди.

**FR-6. Конкурентность.** Операции к разным замкам выполняются параллельно; к одному — строго последовательно (плюс к существующему `connection._lock`). Приватные API `ttlock_ble` (`connection._lock`, `_async_ensure_connected_locked`, `_async_disconnect_locked`) использовать ровно как сейчас.

**FR-7. Конфигурация домена** (`configuration.yaml`, секция уже присутствует):
```python
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional("default_timeout", default=90): vol.All(int, vol.Range(min=10, max=600)),
        vol.Optional("max_attempts_per_window", default=3): vol.All(int, vol.Range(min=1, max=10)),
        vol.Optional("wake_retry_gap", default=2): vol.All(int, vol.Range(min=1, max=15)),
    }),
}, extra=vol.ALLOW_EXTRA)
```
Опциональное поле вызова `timeout` (selector: number, секунд, 10–600) приоритетнее конфигурации. Добавить его в `services.yaml` и `strings.json` у всех трёх сервисов.

**FR-8. Очистка.** При `EVENT_HOMEASSISTANT_STOP`: отписать bluetooth-колбэки и state-tracker'ы, всем незавершённым Future выставить `HomeAssistantError("Компонент выгружается")`, отменить задачи executor'ов. Утечек задач быть не должно.

**FR-9. Логирование.** `_LOGGER.debug` на: постановку в очередь; получение wake-события (с MAC и источником триггера); каждую попытку операции и её исход; таймаут очереди; drain очереди. `_LOGGER.info` оставить только для успешных add/delete (как сейчас).

**FR-10. Быстрый путь.** Если на момент вызова замок «awake» по FR-4 — executor стартует немедленно, без ожидания события.

## 5. Критерии приёмки (чек-лист для самопроверки агента)

1. Конфигурация валидна, три сервиса регистрируются и видны в UI (проверить синхронность `services.yaml` и `strings.json`).
2. Замок спит (`state == unknown`, Last seen обновляется): `get_passcodes` возвращает словарь с `passcodes` в пределах `default_timeout`; в логе видна цепочка «enqueue → wake event → attempt → success».
3. `add_passcode` на спящем замке завершается успехом; последующий `get_passcodes` показывает созданный код.
4. `delete_passcode` на спящем замке завершается успехом.
5. Два одновременных вызова к одному замку выполняются последовательно, исключений о конфликте блокировок нет.
6. Вызовы к двум разным замкам выполняются параллельно (timestamps в логе перекрываются).
7. При выключенном ESP32 Proxy вызов завершается ошибкой с текстом про таймаут ожидания рекламы; после него в логе «queue drained», висячих задач нет.
8. Скрипты `ble_script.yaml`, `ble_list_script.yaml`, `ble_delete_script.yaml` работают без правок (схемы и ответы совместимы).
9. В логе нет новых warning'ов о депрекейтах и нет ошибок регистрации сервисов.

## 6. Вне объёма (не реализовывать сейчас)

- Предиктивное планирование по периоду рекламы (медиана интервалов из кольцевого буфера) — только собрать буфер timestamps и логировать оценку периода в debug.
- Правки YAML-скриптов (удаление «пробуждающих» вызовов) — отдельная фаза 2 после приёмки фазы 1.
- UI-карточки, сенсоры-счётчики очередей, изменения в `ttlock_ble`.

## 7. Порядок работы и формат результата

1. Реализовать FR-1…FR-10 в `__init__.py` (рефакторинг обработчиков без изменения внешнего контракта).
2. Обновить `manifest.json`, `services.yaml`, `strings.json`, `README.md`.
3. Прогнать критерии приёмки 1–9 (минимум статически: совместимость схем/ответов), приложить список изменённых файлов и краткое описание изменений к коммиту/PR.
4. Стиль: существующие соглашения файла (noqa-комментарии приватных API, русские docstrings допустимы).

Если какой-то пункт ТЗ конфликтует с реальным API `ttlock_ble` в репозитории — агенту зафиксировать конфликт в описании PR и реализовать ближайший совместимый вариант, не меняя внешний контракт сервисов.