"""TTLock BLE Passcode Manager — кастомный сервис управления PIN-кодами.

Этот компонент добавляет сервисы для создания, просмотра и удаления PIN-кодов
в замках TTLock напрямую через BLE, используя инфраструктуру ha-ttlock-ble.

Сервисы:
  - ttlock_ble_passcode.add_passcode
  - ttlock_ble_passcode.get_passcodes
  - ttlock_ble_passcode.delete_passcode

Событийный старт (v1.1): операция ставится в per-lock очередь и выполняется
в момент, когда замок появляется в эфире (окно BLE-рекламы). Триггеры, в
порядке приоритета:
  1) bluetooth-колбэк на MAC замка (основной, FR-2);
  2) изменение состояния сенсора «Last seen» устройства (fallback, FR-3);
  3) немедленные попытки с ретраями (если триггеров нет, FR-3/FR-4).

Требования:
  - ha-ttlock-ble установлен и настроен
  - Замки добавлены с Admin Key (для изменения PIN-кодов)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import re
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

DOMAIN = "ttlock_ble_passcode"
TTLOCK_BLE_DOMAIN = "ttlock_ble"

_LOGGER = logging.getLogger(__name__)

SERVICE_ADD_PASSCODE = "add_passcode"
SERVICE_GET_PASSCODES = "get_passcodes"
SERVICE_DELETE_PASSCODE = "delete_passcode"

# Состояния, в которых замок считается «спящим» (не в эфире).
SLEEP_STATES = ("unknown", "unavailable")

# --- Конфигурация домена (FR-7) ---
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional("default_timeout", default=90): vol.All(
                    int, vol.Range(min=10, max=600)
                ),
                vol.Optional("max_attempts_per_window", default=3): vol.All(
                    int, vol.Range(min=1, max=10)
                ),
                vol.Optional("wake_retry_gap", default=2): vol.All(
                    int, vol.Range(min=1, max=15)
                ),
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

# Опциональное поле вызова timeout (FR-7): сек, 10–600, приоритетнее конфига.
_TIMEOUT_FIELD = vol.Any(
    None,
    vol.All(vol.Coerce(float), vol.Range(min=10, max=600)),
)

# --- Service schemas ---
# cv.entity_ids принимает и одну строку (из скриптов), и список entity_id
# (так их передаёт UI/Dev Tools через target) — нормализует всё к списку.
SCHEMA_ADD_PASSCODE = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        # vol.Coerce: принимает и число, и строку (browser_mod/скрипты могут
        # передавать цифровой PIN как int — vol.All(str, ...) его отклонял).
        vol.Required("passcode"): vol.All(vol.Coerce(str), vol.Length(min=4, max=9)),
        vol.Optional("name", default=""): vol.Coerce(str),
        vol.Required("start_time"): vol.Coerce(str),
        vol.Required("end_time"): vol.Coerce(str),
        vol.Optional("timeout"): _TIMEOUT_FIELD,
    }
)

SCHEMA_GET_PASSCODES = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Optional("timeout"): _TIMEOUT_FIELD,
    }
)

SCHEMA_DELETE_PASSCODE = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Required("passcode"): vol.All(vol.Coerce(str), vol.Length(min=4, max=9)),
        vol.Optional("timeout"): _TIMEOUT_FIELD,
    }
)


# --- Структуры очереди (FR-1) ---
@dataclass
class PasscodeOperation:
    """Операция, ожидающая окна BLE-рекламы замка."""

    op: Literal["add", "get", "delete"]
    entity_id: str
    params: dict  # passcode/start_sdk/end_sdk/connection и т.д.
    timeout: float  # секунд, из вызова или из конфигурации
    future: asyncio.Future
    created: float  # loop.time()


@dataclass
class LockQueue:
    """Очередь операций одного замка: FIFO, не более одной операции за раз."""

    entity_id: str
    lock_mac: str  # нормализованный MAC вида AA:BB:CC:DD:EE:FF
    ops: deque = field(default_factory=deque)
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    executor_task: asyncio.Task | None = None
    ad_timestamps: deque = field(default_factory=lambda: deque(maxlen=30))
    trigger: Literal["bt", "last_seen", "none"] = "none"


def _store(hass: HomeAssistant) -> dict:
    """Доступ к состоянию компонента в hass.data (FR-1)."""
    return hass.data.setdefault(
        DOMAIN,
        {
            "config": {},
            "queues": {},
            "unsub_bt": [],
            "unsub_state": [],
        },
    )


def _cfg(hass: HomeAssistant) -> dict:
    return _store(hass).get("config", {})


def _normalize_mac(mac: str) -> str:
    """Приводит MAC к формату AA:BB:CC:DD:EE:FF (верхний регистр).

    Расхождение с буквой ТЗ (FR-2 требует только верхний регистр):
    BluetoothCallbackMatcher требует адрес с двоеточиями, поэтому
    нормализация включает и расстановку разделителей.
    """
    hex_part = "".join(ch for ch in str(mac).upper() if ch in "0123456789ABCDEF")
    if len(hex_part) != 12:
        return str(mac).upper()
    return ":".join(hex_part[i : i + 2] for i in range(0, 12, 2))


# Для поиска MAC внутри unique_id вида "78:58:44:15:28:4D_lock".
_MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}[:\-]?){5}[0-9A-Fa-f]{2}")


def _norm(value: str) -> str:
    """Каноническая форма MAC/строки для сравнения (без разделителей, lower)."""
    return str(value).replace(":", "").replace("-", "").strip().lower()


def _entity_lock_mac(hass: HomeAssistant, entity_id: str) -> str:
    """MAC замка из unique_id сущности ('78:58:44:15:28:4D_lock' → '78:58:44:15:28:4D')."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    unique_id = (entry.unique_id if entry is not None else "") or ""
    match = _MAC_RE.search(unique_id)
    if match:
        return _normalize_mac(match.group(0))
    return _normalize_mac(unique_id.split("-")[0].split("_")[0])


# Параметры выведения ключа имён PIN-кодов — точная копия констант
# интеграции ttlock_ble (data/known_credential.py: PASSCODE_KEY_ITERATIONS).
_PASSCODE_KEY_ITERATIONS = 10_000


def _passcode_name(
    hass: HomeAssistant, lock_mac: str, passcode: str | None
) -> str | None:
    """Имя PIN-кода, заданное в интеграции ttlock_ble (или None).

    ttlock_ble хранит имена кодов в text-сущностях с
    unique_id = f"{lockMac}_passcode_{digest}", где digest —
    pbkdf2_sha256(passcode, salt=lockMac.upper(), 10000 итераций)[:32]
    (см. data/known_credential.py интеграции). Дайджест детерминирован,
    поэтому мы можем найти сущность по значению кода. Сам замок имён
    не хранит — они существуют только на стороне HA.
    """
    if not passcode:
        return None
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(passcode).encode(),
        str(lock_mac).upper().encode(),
        _PASSCODE_KEY_ITERATIONS,
    ).hex()[:32]
    unique_id = f"{lock_mac}_passcode_{digest}"
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("text", TTLOCK_BLE_DOMAIN, unique_id)
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (None, "", "unknown", "unavailable"):
        return None
    return state.state


def _all_known_locks(hass: HomeAssistant) -> list[str]:
    """Список MAC всех замков во всех config entries ttlock_ble (для диагностики)."""
    known: list[str] = []
    for entry in hass.config_entries.async_entries():
        if entry.domain != TTLOCK_BLE_DOMAIN or entry.runtime_data is None:
            continue
        known.extend(
            str(mac)
            for mac in getattr(entry.runtime_data, "connections", {})
        )
    return known


def _parse_datetime(value: str) -> dt.datetime:
    """Разбирает дату из строки YYYY-MM-DD HH:MM[:SS], ISO или ДД.ММ.ГГГГ."""
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",  # ISO без секунд — так отдаёт selector datetime
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
    ):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Не удалось распознать дату: {value!r}")


def _to_sdk_time(d: dt.datetime) -> str:
    """Конвертирует datetime в формат SDK TTLock: YYMMDDHHmm."""
    return d.strftime("%y%m%d%H%M")


def _get_connection(hass: HomeAssistant, entity_id: str):
    """Находит TtlockBleConnection для указанного entity_id замка.

    Возвращает TtlockBleConnection или поднимает ServiceValidationError.
    """
    ent_reg = er.async_get(hass)
    entry_data = ent_reg.async_get(entity_id)
    if entry_data is None:
        raise ServiceValidationError(
            f"Сущность {entity_id!r} не найдена в реестре HA"
        )

    config_entry_id = entry_data.config_entry_id
    if config_entry_id is None:
        raise ServiceValidationError(
            f"Сущность {entity_id!r} не привязана к config entry"
        )

    config_entry = hass.config_entries.async_get_entry(config_entry_id)
    if config_entry is None or config_entry.domain != TTLOCK_BLE_DOMAIN:
        raise ServiceValidationError(
            f"Сущность {entity_id!r} не принадлежит интеграции {TTLOCK_BLE_DOMAIN!r}"
        )

    runtime_data = config_entry.runtime_data
    if runtime_data is None:
        raise ServiceValidationError(
            "Интеграция ttlock_ble не инициализирована. Проверьте её статус."
        )

    # runtime_data.virtual_keys — список VirtualKey (один per config entry)
    # runtime_data.connections  — dict[lockMac, TtlockBleConnection]
    virtual_keys = getattr(runtime_data, "virtual_keys", [])
    connections = getattr(runtime_data, "connections", {})

    if not virtual_keys:
        raise ServiceValidationError(
            f"Нет ключей (VirtualKey) в интеграции для {entity_id!r}"
        )

    # MAC замка берём из unique_id сущности (формат ha-ttlock-ble:
    # "<mac>", "<mac>_lock", "<mac>-lock" и т.п.).
    entity_mac = _entity_lock_mac(hass, entity_id)

    # 1) Ищем соединение с совпадающим MAC по ВСЕМ config entries ttlock_ble:
    # ключ/соединение замка может принадлежать другой entry, чем сущность
    # (в логе: unique_id='78:..._lock', а virtual_keys своей entry — чужой MAC).
    if entity_mac:
        for candidate_entry in hass.config_entries.async_entries():
            if (
                candidate_entry.domain != TTLOCK_BLE_DOMAIN
                or candidate_entry.runtime_data is None
            ):
                continue
            for mac_str, conn in getattr(
                candidate_entry.runtime_data, "connections", {}
            ).items():
                # ВАЖНО: обе стороны сравнения через _norm() — entity_mac
                # отформатирован (78:58:44:15:28:4D), ключи dict могут быть
                # в любом формате (78584415284D и т.п.)
                if _norm(mac_str) != _norm(entity_mac):
                    continue
                for candidate in getattr(
                    candidate_entry.runtime_data, "virtual_keys", []
                ) or []:
                    if _norm(getattr(candidate, "lockMac", "")) == _norm(entity_mac):
                        return candidate, conn
                return None, conn

        _LOGGER.warning(
            "Не найдено соединение/ключ для MAC %s (entity=%s) ни в одной "
            "config entry ttlock_ble; известные замки: %s",
            entity_mac, entity_id, _all_known_locks(hass),
        )

    # 2) Fallback: прежняя логика — virtual_keys своей entry (с предупреждением).
    key = None
    if entity_mac:
        for candidate in virtual_keys:
            if _norm(getattr(candidate, "lockMac", "")) == _norm(entity_mac):
                key = candidate
                break
    if key is None:
        key = virtual_keys[0]
        _LOGGER.warning(
            "Не удалось сопоставить %s (unique_id=%r, MAC=%s) с VirtualKey по MAC; "
            "используется первый ключ (lockMac=%s). Если замков несколько — "
            "проверьте, что выбрали правильный",
            entity_id, entry_data.unique_id, entity_mac or "?", key.lockMac,
        )
    connection = connections.get(key.lockMac)

    if connection is None:
        _LOGGER.error(
            "Нет BLE-соединения для %s: lockMac=%s, доступные соединения: %s",
            entity_id, key.lockMac, list(connections),
        )
        raise ServiceValidationError(
            f"Не найдено BLE-соединение для замка {key.lockMac}"
        )

    return key, connection


async def _async_add_passcode_via_connection(connection, code: str, start_sdk: str, end_sdk: str) -> None:
    """Добавить PIN через TtlockBleConnection — повторяет паттерн _async_run_command."""
    from ttlock_ble import TTLockClient, TTLockError  # noqa: PLC0415
    from ttlock_ble.constants import KeyboardPwdType  # noqa: PLC0415

    # Используем внутренние механизмы connection в точности как это делает сам connection.py:
    # _lock -> _async_ensure_connected_locked -> client.add_passcode
    async with connection._lock:  # noqa: SLF001
        client = await connection._async_ensure_connected_locked()  # noqa: SLF001
        if client is None:
            raise HomeAssistantError("Замок недоступен по BLE")
        try:
            await client.add_passcode(
                code,
                pwd_type=KeyboardPwdType.PERIOD,
                start_date=start_sdk,
                end_date=end_sdk,
            )
        except TTLockError as exc:
            await connection._async_disconnect_locked()  # noqa: SLF001
            raise HomeAssistantError(f"Ошибка BLE при добавлении PIN: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            await connection._async_disconnect_locked()  # noqa: SLF001
            raise HomeAssistantError(f"Ошибка BLE при добавлении PIN: {exc}") from exc


async def _async_delete_passcode_via_connection(connection, code: str) -> None:
    """Удалить PIN через TtlockBleConnection."""
    from ttlock_ble import TTLockError  # noqa: PLC0415
    from ttlock_ble.constants import KeyboardPwdType  # noqa: PLC0415

    async with connection._lock:  # noqa: SLF001
        client = await connection._async_ensure_connected_locked()  # noqa: SLF001
        if client is None:
            raise HomeAssistantError("Замок недоступен по BLE")
        try:
            # PERIOD — для временных кодов, которые мы создаём через этот компонент
            await client.delete_passcode(code, pwd_type=KeyboardPwdType.PERIOD)
        except TTLockError as exc:
            await connection._async_disconnect_locked()  # noqa: SLF001
            raise HomeAssistantError(f"Ошибка BLE при удалении PIN: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            await connection._async_disconnect_locked()  # noqa: SLF001
            raise HomeAssistantError(f"Ошибка BLE при удалении PIN: {exc}") from exc


# =========================================================================
# Триггеры «замок в эфире» (FR-2, FR-3)
# =========================================================================
def _import_bluetooth():
    """Ленивый импорт homeassistant.components.bluetooth (FR-2)."""
    try:
        from homeassistant.components import bluetooth  # noqa: PLC0415
    except ImportError:
        return None
    return bluetooth


def _remember_advertisement(
    hass: HomeAssistant,
    queue: LockQueue,
    source: str,
    service_info=None,
) -> None:
    """Колбэк рекламы: timestamp в кольцевой буфер + взвод wake event (FR-2)."""
    queue.ad_timestamps.append(hass.loop.time())
    _log_period_estimate(queue)
    if service_info is not None:
        _LOGGER.debug(
            "wake event (%s): %s mac=%s name=%s rssi=%s",
            source,
            queue.entity_id,
            queue.lock_mac,
            getattr(service_info, "name", "?"),
            getattr(service_info, "rssi", "?"),
        )
    else:
        _LOGGER.debug(
            "wake event (%s): %s mac=%s", source, queue.entity_id, queue.lock_mac
        )
    queue.wake_event.set()


def _log_period_estimate(queue: LockQueue) -> None:
    """Оценка периода рекламы (вне объёма фазы 1, только debug-лог)."""
    timestamps = tuple(queue.ad_timestamps)
    if len(timestamps) >= 3:
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        _LOGGER.debug(
            "Период рекламы %s: медиана ~%.1f с (%d наблюдений)",
            queue.entity_id,
            statistics.median(gaps),
            len(gaps),
        )


def _async_register_bluetooth_trigger(hass: HomeAssistant, queue: LockQueue) -> bool:
    """FR-2: подписка на advertisement-события MAC замка через bluetooth API."""
    bluetooth = _import_bluetooth()
    if bluetooth is None:
        _LOGGER.debug(
            "bluetooth-компонент недоступен для %s — переход на fallback (FR-3)",
            queue.entity_id,
        )
        return False
    try:
        matcher = bluetooth.BluetoothCallbackMatcher(address=queue.lock_mac)
        disposer = bluetooth.async_register_callback(
            hass,
            # HA вызывает колбэк как callback(service_info, BluetoothChange) —
            # обязательно принимаем второй аргумент (иначе он попадает в _q).
            lambda service_info, _change, _q=queue: _remember_advertisement(
                hass, _q, "bt-advertisement", service_info
            ),
            matcher,
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug(
            "bt-колбэк не зарегистрирован для %s (mac=%s): %s",
            queue.entity_id, queue.lock_mac, exc,
        )
        return False
    _store(hass)["unsub_bt"].append(disposer)
    queue.trigger = "bt"
    _LOGGER.debug(
        "Триггер bt-advertisement зарегистрирован для %s (mac=%s)",
        queue.entity_id, queue.lock_mac,
    )
    return True


def _find_last_seen_entity(hass: HomeAssistant, lock_entity_id: str) -> str | None:
    """FR-3: поиск сенсора «Last seen» среди сущностей устройства замка."""
    ent_reg = er.async_get(hass)
    lock_entry = ent_reg.async_get(lock_entity_id)
    if lock_entry is None or lock_entry.device_id is None:
        return None
    for reg_entry in ent_reg.entities.values():
        if reg_entry.device_id != lock_entry.device_id or reg_entry.domain != "sensor":
            continue
        name_blob = " ".join(
            part
            for part in (
                getattr(reg_entry, "name", None),
                getattr(reg_entry, "original_name", None),
                reg_entry.unique_id,
            )
            if part
        ).lower()
        device_class = str(
            getattr(reg_entry, "original_device_class", "") or ""
        ).lower()
        if (
            device_class == "timestamp"
            or "last_seen" in name_blob
            or "last seen" in name_blob
            or "lastseen" in name_blob
        ):
            return reg_entry.entity_id
    return None


def _register_last_seen_trigger(
    hass: HomeAssistant, queue: LockQueue, sensor_entity_id: str
) -> bool:
    """FR-3 fallback: wake по обновлению сенсора «Last seen»."""

    def _on_state_changed(event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (None, *SLEEP_STATES):
            return
        _remember_advertisement(hass, queue, "last-seen")

    unsub = async_track_state_change_event(hass, [sensor_entity_id], _on_state_changed)
    _store(hass)["unsub_state"].append(unsub)
    queue.trigger = "last_seen"
    _LOGGER.debug(
        "Триггер last-seen зарегистрирован для %s (sensor=%s)",
        queue.entity_id, sensor_entity_id,
    )
    return True


# =========================================================================
# Очереди и executor (FR-1, FR-4, FR-6, FR-10)
# =========================================================================
def _get_or_create_queue(
    hass: HomeAssistant, entity_id: str, lock_mac: str
) -> LockQueue:
    """Очередь замка; при первом обращении создаётся с регистрацией триггеров.

    lock_mac — MAC из unique_id сущности замка (не из VirtualKey): именно по
    нему bluetooth-колбэк узнаёт рекламу нужного замка.
    """
    queues = _store(hass)["queues"]
    queue = queues.get(entity_id)
    if queue is not None:
        return queue
    queue = LockQueue(entity_id=entity_id, lock_mac=_normalize_mac(lock_mac))
    queues[entity_id] = queue
    if not _async_register_bluetooth_trigger(hass, queue):
        sensor_entity_id = _find_last_seen_entity(hass, entity_id)
        if sensor_entity_id is not None:
            _register_last_seen_trigger(hass, queue, sensor_entity_id)
        else:
            queue.trigger = "none"
            _LOGGER.debug(
                "Триггеры недоступны для %s — режим немедленных попыток (FR-3)",
                entity_id,
            )
    return queue


def _connection_entity_state(hass: HomeAssistant, lock_entity_id: str) -> str | None:
    """Состояние диагностической сущности «Bluetooth connection» устройства (FR-4)."""
    ent_reg = er.async_get(hass)
    lock_entry = ent_reg.async_get(lock_entity_id)
    if lock_entry is None or lock_entry.device_id is None:
        return None
    for reg_entry in ent_reg.entities.values():
        if reg_entry.device_id != lock_entry.device_id or reg_entry.domain not in (
            "sensor",
            "binary_sensor",
        ):
            continue
        name_blob = " ".join(
            part
            for part in (
                getattr(reg_entry, "name", None),
                getattr(reg_entry, "original_name", None),
                reg_entry.unique_id,
            )
            if part
        ).lower()
        if "connection" in name_blob or "соединен" in name_blob:
            state = hass.states.get(reg_entry.entity_id)
            if state is not None:
                return state.state
    return None


def _lock_looks_awake(hass: HomeAssistant, entity_id: str) -> bool:
    """FR-4: замок «awake» — state не unknown/unavailable ИЛИ BT-connection connected."""
    state = hass.states.get(entity_id)
    if state is not None and state.state not in SLEEP_STATES:
        return True
    return _connection_entity_state(hass, entity_id) == "connected"


def _enqueue(hass: HomeAssistant, queue: LockQueue, op: PasscodeOperation) -> None:
    """FR-1: постановка в FIFO-очередь + запуск executor'а при необходимости."""
    queue.ops.append(op)
    _LOGGER.debug(
        "enqueue: %s %s (len=%d, trigger=%s)",
        op.op, op.entity_id, len(queue.ops), queue.trigger,
    )
    task = queue.executor_task
    if task is None or task.done():
        queue.executor_task = hass.async_create_task(_async_executor_loop(hass, queue))


async def _async_executor_loop(hass: HomeAssistant, queue: LockQueue) -> None:
    """FR-1/FR-4: обрабатывает очередь замка до опустошения."""
    try:
        while queue.ops:
            op = queue.ops.popleft()
            try:
                await _async_execute_queued(hass, queue, op)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception(
                    "executor: непредвиденная ошибка операции %s для %s",
                    op.op, op.entity_id,
                )
                if not op.future.done():
                    op.future.set_exception(exc)
            finally:
                _LOGGER.debug(
                    "queue: %s op=%s завершена, осталось=%d",
                    queue.entity_id, op.op, len(queue.ops),
                )
    except asyncio.CancelledError:
        # FR-8: при выгрузке компонента — drain очереди без утечек задач.
        for pending in queue.ops:
            if not pending.future.done():
                pending.future.set_exception(
                    HomeAssistantError("Компонент выгружается")
                )
                _LOGGER.debug(
                    "drain: операция %s %s отменена при выгрузке",
                    pending.op, pending.entity_id,
                )
        queue.ops.clear()
        raise
    finally:
        _LOGGER.debug("queue drained: %s", queue.entity_id)


async def _async_execute_queued(
    hass: HomeAssistant, queue: LockQueue, op: PasscodeOperation
) -> None:
    """FR-4: попытки операции в окнах BLE-рекламы до истечения deadline."""
    cfg = _cfg(hass)
    max_attempts = int(cfg.get("max_attempts_per_window", 3))
    retry_gap = float(cfg.get("wake_retry_gap", 2))
    loop = hass.loop
    async with queue.exec_lock:  # FR-6: строго последовательно на один замок
        deadline = loop.time() + op.timeout
        _LOGGER.debug(
            "execute: %s %s timeout=%.0f с trigger=%s",
            op.op, op.entity_id, op.timeout, queue.trigger,
        )
        while loop.time() < deadline:
            # FR-10: awake — попытки сразу; trigger=none — без ожидания события.
            if queue.trigger != "none" and not _lock_looks_awake(hass, op.entity_id):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                _LOGGER.debug(
                    "ожидание wake event: %s %s (осталось %.0f с)",
                    op.op, op.entity_id, remaining,
                )
                try:
                    await asyncio.wait_for(queue.wake_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                queue.wake_event.clear()
            for attempt in range(1, max_attempts + 1):
                if loop.time() >= deadline:
                    break
                _LOGGER.debug(
                    "attempt %d/%d: %s %s", attempt, max_attempts, op.op, op.entity_id
                )
                try:
                    result = await _async_run_op(hass, op)
                    _LOGGER.debug(
                        "attempt %d: success %s %s", attempt, op.op, op.entity_id
                    )
                    if not op.future.done():
                        op.future.set_result(result)
                    return
                except HomeAssistantError as exc:
                    # Ретраится: замок может быть ещё не в окне рекламы.
                    _LOGGER.debug(
                        "attempt %d: fail %s %s: %s",
                        attempt, op.op, op.entity_id, exc,
                    )
                    await asyncio.sleep(retry_gap)
                except (ServiceValidationError, ValueError, AttributeError) as exc:
                    # Не ретраится: некорректные данные/конфигурация или
                    # отсутствующий метод API интеграции/SDK (детерминировано).
                    _LOGGER.debug(
                        "non-retryable: %s %s: %s", op.op, op.entity_id, exc
                    )
                    if not op.future.done():
                        op.future.set_exception(exc)
                    return
        if not op.future.done():
            op.future.set_exception(
                HomeAssistantError(
                    f"Таймаут ожидания окна BLE-рекламы ({op.timeout:.0f} с) "
                    f"для {op.entity_id}"
                )
            )
        _LOGGER.debug("queue timeout: %s %s", op.op, op.entity_id)


# =========================================================================
# Исполнители операций (FR-5): тела прежних обработчиков
# =========================================================================
async def _async_run_op(hass: HomeAssistant, op: PasscodeOperation):
    """Диспетчер к исполнителям операций."""
    if op.op == "add":
        return await _async_run_add_op(op)
    if op.op == "get":
        return await _async_run_get_op(hass, op)
    return await _async_run_delete_op(op)


async def _async_run_add_op(op: PasscodeOperation) -> dict:
    """Добавить PIN-код (один замок). Формат ответа прежний."""
    connection = op.params["connection"]
    passcode = op.params["passcode"]
    await _async_add_passcode_via_connection(
        connection, passcode, op.params["start_sdk"], op.params["end_sdk"]
    )
    _LOGGER.info("PIN %s добавлен в замок %s", passcode, op.entity_id)
    return {"success": True, "passcode": passcode, "entity_id": op.entity_id}


async def _async_run_get_op(hass: HomeAssistant, op: PasscodeOperation) -> list[dict]:
    """Прочитать список PIN-кодов (один замок). Формат ответа прежний
    (дополнено полем ``name`` — имя кода из интеграции ttlock_ble)."""
    connection = op.params["connection"]
    entity_id = op.entity_id

    # Чтение списка PIN-кодов:
    #  - свежие версии ha-ttlock-ble: connection.async_get_passcodes()
    #  - старые версии: метода нет — работаем через клиент SDK напрямую
    #    (client.get_passcodes(), паттерн _async_run_command: _lock ->
    #    _async_ensure_connected_locked -> клиент)
    getter = getattr(connection, "async_get_passcodes", None)
    if callable(getter):
        try:
            passcode_entries = await getter()
        except HomeAssistantError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Логируем исходное исключение — иначе реальная причина
            # (таймаут GATT, спящий замок, отсутствие Admin Key) скрыта.
            _LOGGER.exception(
                "get_passcodes: ошибка BLE при чтении PIN-кодов %s", entity_id
            )
            raise HomeAssistantError(
                f"Ошибка BLE при чтении PIN-кодов {entity_id}: {exc}"
            ) from exc
    else:
        connect = getattr(connection, "_async_ensure_connected_locked", None)
        if not callable(connect):
            raise ServiceValidationError(
                "Установленная версия ha-ttlock-ble не поддерживает чтение "
                "PIN-кодов (нет connection.async_get_passcodes и "
                "_async_ensure_connected_locked). Обновите интеграцию "
                "ttlock_ble через HACS до актуальной версии"
            )
        from ttlock_ble import TTLockError  # noqa: PLC0415

        try:
            async with connection._lock:  # noqa: SLF001
                client = await connect()  # noqa: SLF001
                if client is None:
                    raise HomeAssistantError("Замок недоступен по BLE")
                passcode_entries = await client.get_passcodes()
        except AttributeError as exc:
            # Детерминированная ошибка API — в ретраях смысла нет.
            raise AttributeError(
                f"SDK ttlock-ble не содержит client.get_passcodes: {exc}"
            ) from exc
        except HomeAssistantError:
            raise
        except Exception as exc:  # noqa: BLE001
            await connection._async_disconnect_locked()  # noqa: SLF001
            _LOGGER.exception(
                "get_passcodes: ошибка BLE при чтении PIN-кодов %s", entity_id
            )
            raise HomeAssistantError(
                f"Ошибка BLE при чтении PIN-кодов {entity_id}: {exc}"
            ) from exc

    if passcode_entries is None:
        raise HomeAssistantError(
            f"Замок {entity_id} недоступен по BLE или отклонил запрос (нет Admin Key)"
        )

    items: list[dict] = []
    for entry in passcode_entries:
        item: dict = {}
        # PasscodeEntry атрибуты из SDK
        for attr in ("passcode", "new_passcode", "type", "pwd_type", "start_date", "end_date"):
            val = getattr(entry, attr, None)
            if val is not None:
                item[attr] = str(val) if not isinstance(val, str) else val
        # Нормализуем имя поля типа
        if "pwd_type" in item and "type" not in item:
            item["type"] = _type_name(item["pwd_type"])
        items.append(item)

    # Имя PIN-кода (если пользователь задал его text-сущностью в
    # интеграции ttlock_ble). Ключ сущности — digest кода + MAC замка.
    lock_mac = _entity_lock_mac(hass, op.entity_id)
    for item in items:
        name = _passcode_name(
            hass, lock_mac, item.get("passcode") or item.get("new_passcode")
        )
        if name:
            item["name"] = name
    return items


async def _async_run_delete_op(op: PasscodeOperation) -> dict:
    """Удалить PIN-код (один замок). Формат ответа прежний."""
    connection = op.params["connection"]
    passcode = op.params["passcode"]
    await _async_delete_passcode_via_connection(connection, passcode)
    _LOGGER.info("PIN %s удалён из замка %s", passcode, op.entity_id)
    return {"success": True, "passcode": passcode, "entity_id": op.entity_id}


# =========================================================================
# Хелперы тонких обработчиков (FR-5, FR-7)
# =========================================================================
def _resolve_timeout(hass: HomeAssistant, call_timeout) -> float:
    """timeout вызова приоритетнее default_timeout из конфигурации (FR-7)."""
    if call_timeout:
        return float(call_timeout)
    return float(_cfg(hass).get("default_timeout", 90))


def _make_operation(
    hass: HomeAssistant,
    op_name: str,
    entity_id: str,
    params: dict,
    timeout: float,
) -> PasscodeOperation:
    loop = hass.loop
    return PasscodeOperation(
        op=op_name,
        entity_id=entity_id,
        params=params,
        timeout=timeout,
        future=loop.create_future(),
        created=loop.time(),
    )


async def _async_wait_result(op: PasscodeOperation):
    """FR-5: ожидание Future с запасом +5 с; таймаут → HomeAssistantError."""
    try:
        return await asyncio.wait_for(op.future, timeout=op.timeout + 5)
    except asyncio.TimeoutError as exc:
        raise HomeAssistantError(
            f"Таймаут ожидания окна BLE-рекламы ({op.timeout:.0f} с) "
            f"для {op.entity_id}"
        ) from exc


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Регистрация компонента, конфигурации и его сервисов."""

    # --- Инициализация конфигурации домена (FR-7), идемпотентно к reload ---
    store = _store(hass)
    store["config"] = CONFIG_SCHEMA({DOMAIN: config.get(DOMAIN) or {}})[DOMAIN]
    for unsub in (*store.get("unsub_bt", []), *store.get("unsub_state", [])):
        try:
            unsub()
        except Exception:  # noqa: BLE001
            pass
    store["unsub_bt"] = []
    store["unsub_state"] = []

    async def handle_add_passcode(call: ServiceCall) -> dict:
        """Добавить PIN-код в замок через BLE (тонкий обработчик, FR-5)."""
        data = SCHEMA_ADD_PASSCODE(dict(call.data))
        entity_ids = data["entity_id"]
        passcode = data["passcode"]
        timeout = _resolve_timeout(hass, data.get("timeout"))

        try:
            start_dt = _parse_datetime(data["start_time"])
            end_dt = _parse_datetime(data["end_time"])
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

        params = {
            "connection": None,
            "passcode": passcode,
            "name": data.get("name", ""),
            "start_sdk": _to_sdk_time(start_dt),
            "end_sdk": _to_sdk_time(end_dt),
        }
        _LOGGER.debug(
            "add_passcode: entities=%s code=%s name=%r start=%s end=%s timeout=%.0f",
            entity_ids, passcode, params["name"],
            params["start_sdk"], params["end_sdk"], timeout,
        )

        for entity_id in entity_ids:
            # Ошибки схемы/сущности отдаём сразу, без очереди (FR-5).
            _key, connection = _get_connection(hass, entity_id)
            params["connection"] = connection
            queue = _get_or_create_queue(
                hass, entity_id, _entity_lock_mac(hass, entity_id)
            )
            op = _make_operation(hass, "add", entity_id, dict(params), timeout)
            _enqueue(hass, queue, op)
            await _async_wait_result(op)

        return {"success": True, "passcode": passcode, "entity_id": entity_ids}

    async def handle_get_passcodes(call: ServiceCall) -> dict:
        """Получить список PIN-кодов из памяти замка через BLE (FR-5)."""
        data = SCHEMA_GET_PASSCODES(dict(call.data))
        entity_ids = data["entity_id"]
        timeout = _resolve_timeout(hass, data.get("timeout"))

        result_list: list[dict] = []
        for entity_id in entity_ids:
            _LOGGER.debug("get_passcodes: entity=%s timeout=%.0f", entity_id, timeout)
            _key, connection = _get_connection(hass, entity_id)
            queue = _get_or_create_queue(
                hass, entity_id, _entity_lock_mac(hass, entity_id)
            )
            op = _make_operation(
                hass, "get", entity_id, {"connection": connection}, timeout
            )
            _enqueue(hass, queue, op)
            result_list.extend(await _async_wait_result(op))

        return {"passcodes": result_list, "count": len(result_list)}

    async def handle_delete_passcode(call: ServiceCall) -> dict:
        """Удалить PIN-код из замка через BLE (тонкий обработчик, FR-5)."""
        data = SCHEMA_DELETE_PASSCODE(dict(call.data))
        entity_ids = data["entity_id"]
        passcode = data["passcode"]
        timeout = _resolve_timeout(hass, data.get("timeout"))

        _LOGGER.debug(
            "delete_passcode: entities=%s code=%s timeout=%.0f",
            entity_ids, passcode, timeout,
        )

        for entity_id in entity_ids:
            _key, connection = _get_connection(hass, entity_id)
            queue = _get_or_create_queue(
                hass, entity_id, _entity_lock_mac(hass, entity_id)
            )
            op = _make_operation(
                hass, "delete", entity_id,
                {"connection": connection, "passcode": passcode}, timeout,
            )
            _enqueue(hass, queue, op)
            await _async_wait_result(op)

        return {"success": True, "passcode": passcode, "entity_id": entity_ids}

    # --- Регистрация сервисов ---
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_PASSCODE,
        handle_add_passcode,
        schema=SCHEMA_ADD_PASSCODE,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PASSCODES,
        handle_get_passcodes,
        schema=SCHEMA_GET_PASSCODES,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_PASSCODE,
        handle_delete_passcode,
        schema=SCHEMA_DELETE_PASSCODE,
        supports_response=SupportsResponse.OPTIONAL,
    )

    # FR-8: очистка при остановке Home Assistant.
    async def _async_on_ha_stop(event) -> None:
        st = _store(hass)
        for unsub in (*st.get("unsub_bt", []), *st.get("unsub_state", [])):
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        st["unsub_bt"] = []
        st["unsub_state"] = []
        for queue in st.get("queues", {}).values():
            for pending in queue.ops:
                if not pending.future.done():
                    pending.future.set_exception(
                        HomeAssistantError("Компонент выгружается")
                    )
            queue.ops.clear()
            task = queue.executor_task
            if task is not None and not task.done():
                task.cancel()
        _LOGGER.debug("ttlock_ble_passcode: очистка при STOP выполнена")

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_ha_stop)

    _LOGGER.info(
        "TTLock BLE Passcode Manager запущен. Зарегистрированы сервисы: "
        "%s.%s, %s.%s, %s.%s",
        DOMAIN, SERVICE_ADD_PASSCODE,
        DOMAIN, SERVICE_GET_PASSCODES,
        DOMAIN, SERVICE_DELETE_PASSCODE,
    )
    return True


def _type_name(pwd_type: int | str) -> str:
    """Возвращает человекочитаемое название типа PIN-кода.

    KeyboardPwdType из SDK ttlock-ble: PERMANENT=1, COUNT=2, PERIOD=3, CIRCLE=4.
    """
    try:
        return {1: "permanent", 2: "count", 3: "period", 4: "circle"}.get(
            int(pwd_type), f"type_{pwd_type}"
        )
    except (TypeError, ValueError):
        return str(pwd_type)
