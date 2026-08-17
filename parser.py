import re
from dataclasses import dataclass
from typing import Optional

import config


@dataclass
class ParsedRaceData:
    track: str
    track_name: str
    car_rank: str
    car: str
    engine: str
    time: str
    time_seconds: Optional[float]
    max_speed: str
    max_speed_value: Optional[float]


TRACK_PATTERNS: dict[str, re.Pattern[str]] = {
    "obiezdnaya": re.compile(r"(?:об[ьъ]?[её]здн|ob[ie][e]?zdn)", re.IGNORECASE),
    "proseka": re.compile(r"(?:просек|prosek)", re.IGNORECASE),
    "ferma": re.compile(r"(?:ферм|ferm)", re.IGNORECASE),
}


LABELS = {
    "car_rank": r"(?:ранг\s*авто|rank|ранг|класс|class|tier)",
    "car": r"(?:авто|car|машина|vehicle|model)",
    "engine": r"(?:мотор|engine|двигатель|motor)",
    "time": r"(?:время|time|lap|круг|результат)",
    "max_speed": r"(?:макс(?:\.|имальная)?\s*скорость|max(?:imum)?\s*speed|top\s*speed|скорость)",
}

TIME_PATTERN = re.compile(
    r"(?P<time>\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\s*(?:с|s|sec)?",
    re.IGNORECASE,
)
RANK_HEADER_PATTERN = re.compile(r"ранг\s+([A-ZА-Я]\+?)", re.IGNORECASE)
ENGINE_PATTERN = re.compile(r"([A-ZА-Я0-9]+\s*\(\s*\d+\s*HP\s*\))", re.IGNORECASE)
TABLE_ROW_PATTERN = re.compile(
    r"^1[\t\s]+(?P<rest>.+\d{2}:\d{2}[.,]\d{1,3}.+)$",
    re.IGNORECASE | re.MULTILINE,
)
SPEED_PATTERN = re.compile(
    r"(?P<speed>\d+(?:[.,]\d+)?)\s*(?:km/h|kmh|км/ч|км\/ч|mph)?",
    re.IGNORECASE,
)


def parse_race_text(text: str) -> ParsedRaceData:
    normalized = text.replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    track, track_name = _extract_track(normalized)

    table_row = _parse_first_place_row(normalized)
    if table_row:
        car_rank = _extract_rank_from_header(normalized) or "—"
        return ParsedRaceData(
            track=track,
            track_name=track_name,
            car_rank=car_rank,
            car=table_row["car"],
            engine=table_row["engine"],
            time=table_row["time"],
            time_seconds=table_row["time_seconds"],
            max_speed=table_row["max_speed"],
            max_speed_value=table_row["max_speed_value"],
        )

    labeled = _extract_labeled_values(lines)
    time_value, time_seconds = _extract_time(normalized, labeled.get("time"))
    speed_value, speed_number = _extract_speed(normalized, labeled.get("max_speed"))

    car_rank = labeled.get("car_rank") or _extract_rank_from_header(normalized) or _guess_rank(lines) or "—"
    car = labeled.get("car") or _guess_car(lines) or "—"
    engine = labeled.get("engine") or _guess_engine(lines) or "—"

    if not time_value:
        raise ValueError("Не удалось распознать время заезда")
    if not speed_value:
        raise ValueError("Не удалось распознать максимальную скорость")

    return ParsedRaceData(
        track=track,
        track_name=track_name,
        car_rank=car_rank,
        car=car,
        engine=engine,
        time=time_value,
        time_seconds=time_seconds,
        max_speed=speed_value,
        max_speed_value=speed_number,
    )


def _extract_track(text: str) -> tuple[str, str]:
    for track_id, pattern in TRACK_PATTERNS.items():
        if pattern.search(text):
            return track_id, config.TRACKS[track_id]
    raise ValueError("Не удалось распознать трассу (Обьездная, Просека, ферма)")


def _extract_rank_from_header(text: str) -> Optional[str]:
    match = RANK_HEADER_PATTERN.search(text)
    if match:
        return match.group(1).upper()
    return None


def _parse_first_place_row(text: str) -> Optional[dict[str, object]]:
    match = TABLE_ROW_PATTERN.search(text)
    if not match:
        return None

    rest = match.group("rest")
    time_match = TIME_PATTERN.search(rest)
    if not time_match:
        return None

    time_value = time_match.group("time").replace(",", ".")
    time_seconds = time_to_seconds(time_value)
    if time_seconds is None:
        return None

    after_time = rest[time_match.end() :].strip()
    speed_match = re.search(r"(?P<speed>\d{2,3})\s*$", after_time)
    if not speed_match:
        return None

    speed_number = float(speed_match.group("speed"))
    if speed_number < 50 or speed_number > 400:
        return None

    before_time = rest[: time_match.start()].strip()
    engine_match = ENGINE_PATTERN.search(before_time)
    if not engine_match:
        return None

    engine = engine_match.group(1).strip()
    car = before_time[: engine_match.start()].strip()
    if not car:
        return None

    return {
        "car": car,
        "engine": engine,
        "time": time_value,
        "time_seconds": time_seconds,
        "max_speed": f"{int(speed_number)} km/h",
        "max_speed_value": speed_number,
    }


def _extract_labeled_values(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}

    for line in lines:
        for field, label_pattern in LABELS.items():
            if field in result:
                continue
            match = re.search(
                rf"(?:{label_pattern})\s*[:：\-]?\s*(.+)$",
                line,
                re.IGNORECASE,
            )
            if match:
                value = match.group(1).strip()
                if value:
                    result[field] = value

    return result


def _extract_time(text: str, labeled: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    candidates: list[str] = []
    if labeled:
        candidates.append(labeled)

    for match in TIME_PATTERN.finditer(text):
        candidates.append(match.group("time"))

    for candidate in candidates:
        seconds = time_to_seconds(candidate)
        if seconds is not None:
            return candidate.replace(",", "."), seconds

    return None, None


def _extract_speed(text: str, labeled: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    candidates: list[str] = []
    if labeled:
        candidates.append(labeled)

    for match in SPEED_PATTERN.finditer(text):
        candidates.append(match.group("speed"))

    best_value: Optional[float] = None
    best_raw: Optional[str] = None

    for candidate in candidates:
        number = _to_float(candidate)
        if number is None or number < 50 or number > 400:
            continue
        if best_value is None or number > best_value:
            best_value = number
            best_raw = candidate.replace(",", ".")

    if best_raw is None or best_value is None:
        return None, None

    return f"{best_raw} km/h", best_value


def time_to_seconds(value: str) -> Optional[float]:
    cleaned = value.strip().replace(",", ".")

    if ":" in cleaned:
        parts = cleaned.split(":")
        if len(parts) != 2:
            return None
        try:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        except ValueError:
            return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _guess_rank(lines: list[str]) -> Optional[str]:
    for line in lines:
        match = re.search(r"\b([A-ZА-Я]{1,2}\+?|[1-9]\d?)\b", line, re.IGNORECASE)
        if match and len(line) <= 20:
            return match.group(1).upper()
    return None


def _guess_car(lines: list[str]) -> Optional[str]:
    skip_words = {"время", "time", "скорость", "speed", "мотор", "engine", "ранг", "rank"}
    for line in lines:
        lower = line.lower()
        if any(word in lower for word in skip_words):
            continue
        if re.search(r"\d", line) and TIME_PATTERN.search(line):
            continue
        if len(line) >= 3 and not line.isdigit():
            return line
    return None


def _guess_engine(lines: list[str]) -> Optional[str]:
    for line in lines:
        if re.search(r"(turbo|v\d|engine|мотор|двигатель)", line, re.IGNORECASE):
            return line
    return None
