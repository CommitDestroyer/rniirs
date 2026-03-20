"""
core/tle_parser.py — парсинг, валидация и загрузка TLE-данных.

Подключение в tle.py (заменить inline-реализации):
    from core.tle_parser import (
        TLERecord,
        tle_checksum,
        validate_tle_lines,
        parse_tle_text,
        load_records_into_db,
        export_tle_text,
        sha256_short,
        now_iso,
    )

    # Алиасы для обратной совместимости:
    _checksum         = tle_checksum
    _sha256           = sha256_short
    _now_iso          = now_iso
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from core.orbital import parse_tle_meta, orbit_type_from_meta

# ══════════════════════════════════════════════════════════════════════════════
#  СХЕМЫ
# ══════════════════════════════════════════════════════════════════════════════

class TLERecord(BaseModel):
    """Одна запись спутника из TLE-файла с распарсенными метаданными."""
    name:            str
    line1:           str
    line2:           str
    norad_id:        Optional[int]   = None
    epoch:           Optional[str]   = None
    orbit_type:      Optional[str]   = None
    inclination_deg: Optional[float] = None
    period_min:      Optional[float] = None
    apogee_km:       Optional[float] = None
    perigee_km:      Optional[float] = None
    eccentricity:    Optional[float] = None
    operator:        str             = "Unknown"
    source:          str             = "manual"


class TLEUploadResult(BaseModel):
    """Результат загрузки TLE — возвращается всеми upload-эндпоинтами."""
    source:      str
    added:       int
    skipped:     int
    total_in_db: int
    sha256:      str
    loaded_at:   str


class TLEValidationResult(BaseModel):
    """Результат валидации одной пары TLE-строк."""
    valid:      bool
    name:       Optional[str]   = None
    norad_id:   Optional[int]   = None
    orbit_type: Optional[str]   = None
    period_min: Optional[float] = None
    epoch:      Optional[str]   = None
    errors:     list[str]       = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def now_iso() -> str:
    """Текущее время в UTC ISO-формате: 2025-04-01T12:00:00Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_short(content: str) -> str:
    """Первые 16 символов SHA-256 от строки — используется как fingerprint."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
#  ВАЛИДАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

def tle_checksum(line: str) -> int:
    """
    Контрольная сумма строки TLE по стандарту NORAD.

    Алгоритм: сумма всех цифр + 1 за каждый минус, по модулю 10.
    Последний символ строки — ожидаемая контрольная сумма.
    """
    total = 0
    for ch in line[:-1]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def validate_tle_lines(line1: str, line2: str) -> list[str]:
    """
    Полная валидация пары TLE-строк.

    Проверяет:
        - формат начала строк ("1 " / "2 ")
        - длину (ровно 69 символов)
        - контрольные суммы (NORAD-алгоритм)
        - совпадение NORAD ID в обеих строках

    Returns:
        Список ошибок. Пустой список → TLE валидно.
    """
    errors: list[str] = []

    if not line1.startswith("1 "):
        errors.append("Line1 must start with '1 '")
    if not line2.startswith("2 "):
        errors.append("Line2 must start with '2 '")
    if len(line1) != 69:
        errors.append(f"Line1 length must be 69, got {len(line1)}")
    if len(line2) != 69:
        errors.append(f"Line2 length must be 69, got {len(line2)}")

    if not errors:
        cs1 = tle_checksum(line1)
        if cs1 != int(line1[-1]):
            errors.append(
                f"Line1 checksum mismatch: expected {int(line1[-1])}, got {cs1}"
            )
        cs2 = tle_checksum(line2)
        if cs2 != int(line2[-1]):
            errors.append(
                f"Line2 checksum mismatch: expected {int(line2[-1])}, got {cs2}"
            )

    try:
        norad1 = int(line1[2:7])
        norad2 = int(line2[2:7])
        if norad1 != norad2:
            errors.append(f"NORAD ID mismatch: line1={norad1}, line2={norad2}")
    except ValueError:
        errors.append("Cannot parse NORAD ID")

    return errors


def validate_single(name: str, line1: str, line2: str) -> TLEValidationResult:
    """
    Валидирует одну запись и возвращает TLEValidationResult.
    Удобный shortcut для эндпоинта /tle/validate.
    """
    errors = validate_tle_lines(line1, line2)
    if errors:
        return TLEValidationResult(valid=False, errors=errors)

    meta       = parse_tle_meta(line1, line2)
    orbit_type = orbit_type_from_meta(meta)

    return TLEValidationResult(
        valid=True,
        name=name or f"NORAD-{meta.get('norad_id', '?')}",
        norad_id=meta.get("norad_id"),
        orbit_type=orbit_type,
        period_min=meta.get("period_min"),
        epoch=meta.get("epoch"),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ПАРСЕР
# ══════════════════════════════════════════════════════════════════════════════

def parse_tle_text(
    content:  str,
    source:   str = "manual",
    operator: str = "Various",
) -> tuple[list[TLERecord], int]:
    """
    Парсит TLE-текст в форматах 3LE и 2LE.

    Форматы:
        3LE — строка имени + Line1 + Line2  (стандарт Celestrak)
        2LE — только Line1 + Line2 (имя генерируется автоматически)

    Args:
        content:  текст файла
        source:   источник (celestrak, upload, manual и т.д.)
        operator: оператор/владелец по умолчанию

    Returns:
        (records, skipped)
        records  — список валидных TLERecord
        skipped  — количество пропущенных строк (ошибка или мусор)
    """
    lines   = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
    records: list[TLERecord] = []
    skipped = 0
    i       = 0

    while i < len(lines):
        # ── 3LE: имя + Line1 + Line2 ─────────────────────────────────────────
        if (
            i + 2 < len(lines)
            and not lines[i].startswith("1 ")
            and lines[i + 1].startswith("1 ")
            and lines[i + 2].startswith("2 ")
        ):
            name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
            i += 3

        # ── 2LE: Line1 + Line2 без имени ─────────────────────────────────────
        elif (
            i + 1 < len(lines)
            and lines[i].startswith("1 ")
            and lines[i + 1].startswith("2 ")
        ):
            name = f"NORAD-{lines[i][2:7].strip()}"
            l1, l2 = lines[i], lines[i + 1]
            i += 2

        else:
            skipped += 1
            i += 1
            continue

        # ── Валидация ─────────────────────────────────────────────────────────
        errs = validate_tle_lines(l1, l2)
        if errs:
            skipped += 1
            continue

        # ── Метаданные ────────────────────────────────────────────────────────
        meta       = parse_tle_meta(l1, l2)
        orbit_type = orbit_type_from_meta(meta)

        records.append(TLERecord(
            name=name.strip(),
            line1=l1,
            line2=l2,
            norad_id=meta.get("norad_id"),
            epoch=meta.get("epoch"),
            orbit_type=orbit_type,
            inclination_deg=meta.get("inclination_deg"),
            period_min=meta.get("period_min"),
            apogee_km=meta.get("apogee_km"),
            perigee_km=meta.get("perigee_km"),
            eccentricity=meta.get("eccentricity"),
            operator=operator,
            source=source,
        ))

    return records, skipped


def parse_tle_file(path: str, source: Optional[str] = None,
                   operator: str = "Various") -> tuple[list[TLERecord], int]:
    """
    Читает TLE-файл с диска и возвращает записи.

    Args:
        path:     путь к файлу
        source:   имя источника (по умолчанию — имя файла)
        operator: оператор по умолчанию

    Returns:
        (records, skipped)

    Raises:
        FileNotFoundError если файл не существует.
    """
    import os
    src_name = source or os.path.basename(path)
    content  = open(path, encoding="utf-8", errors="replace").read()
    return parse_tle_text(content, source=src_name, operator=operator)


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАГРУЗКА В БД
# ══════════════════════════════════════════════════════════════════════════════

def load_records_into_db(
    records: list[TLERecord],
    db=None,          # SatelliteDB — передаётся явно или берётся из satellites
) -> tuple[int, int]:
    """
    Загружает список TLERecord в SatelliteDB.
    Дубли по NORAD ID пропускаются.

    Args:
        records: список записей из parse_tle_text
        db:      экземпляр SatelliteDB. Если None — импортируется из satellites.

    Returns:
        (added, skipped)
    """
    if db is None:
        from satellites import db as _db
        db = _db

    added = skipped = 0
    for rec in records:
        if rec.norad_id and db.get_by_norad(rec.norad_id):
            skipped += 1
            continue
        sid = db.add(
            name=rec.name,
            l1=rec.line1,
            l2=rec.line2,
            operator=rec.operator,
            sat_type=rec.orbit_type or "Unknown",
        )
        if sid:
            added += 1
        else:
            skipped += 1

    return added, skipped


def make_upload_result(
    source:  str,
    content: str,
    added:   int,
    skipped: int,
    db=None,
) -> TLEUploadResult:
    """
    Создаёт TLEUploadResult с sha256 и временной меткой.
    Удобный shortcut для всех upload-эндпоинтов.
    """
    if db is None:
        from satellites import db as _db
        db = _db

    return TLEUploadResult(
        source=source,
        added=added,
        skipped=skipped,
        total_in_db=db.count(),
        sha256=sha256_short(content),
        loaded_at=now_iso(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ЭКСПОРТ
# ══════════════════════════════════════════════════════════════════════════════

def export_tle_text(records: list[dict]) -> str:
    """
    Экспортирует список записей из SatelliteDB в 3LE-текст.

    Args:
        records: записи из db.list() (dict с ключами name, line1, line2)

    Returns:
        Текст в формате 3LE, каждая запись — 3 строки.
    """
    lines: list[str] = []
    for rec in records:
        if rec.get("name") and rec.get("line1") and rec.get("line2"):
            lines.extend([rec["name"], rec["line1"], rec["line2"]])
    return "\n".join(lines) + "\n" if lines else ""


def records_to_tle_text(records: list[TLERecord]) -> str:
    """
    Сериализует список TLERecord в 3LE-текст.
    Используется для export/preview без обращения к БД.
    """
    lines: list[str] = []
    for rec in records:
        lines.extend([rec.name, rec.line1, rec.line2])
    return "\n".join(lines) + "\n" if lines else ""


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРАТНАЯ СОВМЕСТИМОСТЬ — алиасы для tle.py
# ══════════════════════════════════════════════════════════════════════════════

# В tle.py заменить inline-функции на:
#   from core.tle_parser import (
#       TLERecord, TLEUploadResult, TLEValidationResult,
#       validate_tle_lines,
#       parse_tle_text,
#       load_records_into_db,
#       make_upload_result   as _make_result,
#       sha256_short         as _sha256,
#       now_iso              as _now_iso,
#       tle_checksum         as _checksum,
#       validate_single,
#       export_tle_text,
#   )

_checksum         = tle_checksum
_sha256           = sha256_short
_now_iso          = now_iso