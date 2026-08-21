import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
from parser import normalize_car_rank


@dataclass
class RaceResult:
    id: int
    car_rank: str
    car: str
    engine: str
    time: str
    time_seconds: Optional[float]
    max_speed: str
    max_speed_value: Optional[float]
    user_id: int
    username: Optional[str]
    user_name: Optional[str]
    confirmed: bool
    created_at: str


class RaceDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _table_name(self, track: str) -> str:
        if track not in config.TRACKS:
            raise ValueError(f"Неизвестная трасса: {track}")
        return f"race_results_{track}"

    def _init_db(self) -> None:
        with self._connect() as conn:
            for track in config.TRACKS:
                table = self._table_name(track)
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        car_rank TEXT NOT NULL,
                        car TEXT NOT NULL,
                        engine TEXT NOT NULL,
                        time TEXT NOT NULL,
                        time_seconds REAL,
                        max_speed TEXT NOT NULL,
                        max_speed_value REAL,
                        user_id INTEGER NOT NULL,
                        username TEXT,
                        user_name TEXT,
                        confirmed INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                    """
                )
                self._ensure_columns(conn, table)
                self._normalize_existing_ranks(conn, table)
            conn.commit()

    def _ensure_columns(self, conn: sqlite3.Connection, table: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "confirmed" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")

    def _normalize_existing_ranks(self, conn: sqlite3.Connection, table: str) -> None:
        rows = conn.execute(f"SELECT id, car_rank FROM {table}").fetchall()
        for row in rows:
            normalized = normalize_car_rank(row["car_rank"])
            if normalized != row["car_rank"]:
                conn.execute(
                    f"UPDATE {table} SET car_rank = ? WHERE id = ?",
                    (normalized, row["id"]),
                )

    def add_result(
        self,
        *,
        track: str,
        car_rank: str,
        car: str,
        engine: str,
        time: str,
        time_seconds: Optional[float],
        max_speed: str,
        max_speed_value: Optional[float],
        user_id: int,
        username: Optional[str] = None,
        user_name: Optional[str] = None,
    ) -> int:
        table = self._table_name(track)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {table} (
                    car_rank, car, engine, time, time_seconds,
                    max_speed, max_speed_value, user_id, username, user_name, confirmed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    normalize_car_rank(car_rank),
                    car,
                    engine,
                    time,
                    time_seconds,
                    max_speed,
                    max_speed_value,
                    user_id,
                    username,
                    user_name,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_result(self, track: str, result_id: int) -> Optional[RaceResult]:
        table = self._table_name(track)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?",
                (result_id,),
            ).fetchone()
        return self._row_to_result(row) if row else None

    def set_confirmed(self, track: str, result_id: int, confirmed: bool) -> bool:
        table = self._table_name(track)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {table} SET confirmed = ? WHERE id = ?",
                (1 if confirmed else 0, result_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def confirm_result_if_pending(self, track: str, result_id: int) -> bool:
        table = self._table_name(track)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {table} SET confirmed = 1 WHERE id = ? AND confirmed = 0",
                (result_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_result(
        self,
        track: str,
        result_id: int,
        *,
        car_rank: Optional[str] = None,
        car: Optional[str] = None,
        engine: Optional[str] = None,
        time: Optional[str] = None,
        time_seconds: Optional[float] = None,
        max_speed: Optional[str] = None,
        max_speed_value: Optional[float] = None,
    ) -> bool:
        fields: dict[str, object] = {}
        if car_rank is not None:
            fields["car_rank"] = normalize_car_rank(car_rank)
        if car is not None:
            fields["car"] = car
        if engine is not None:
            fields["engine"] = engine
        if time is not None:
            fields["time"] = time
        if time_seconds is not None:
            fields["time_seconds"] = time_seconds
        if max_speed is not None:
            fields["max_speed"] = max_speed
        if max_speed_value is not None:
            fields["max_speed_value"] = max_speed_value
        if not fields:
            return False

        table = self._table_name(track)
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values()) + [result_id]
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                values,
            )
            conn.commit()
            return cursor.rowcount > 0

    def move_result(self, from_track: str, result_id: int, to_track: str) -> Optional[int]:
        if from_track not in config.TRACKS or to_track not in config.TRACKS:
            raise ValueError("Неизвестная трасса")
        if from_track == to_track:
            return result_id

        result = self.get_result(from_track, result_id)
        if not result:
            return None

        from_table = self._table_name(from_track)
        to_table = self._table_name(to_track)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {to_table} (
                    car_rank, car, engine, time, time_seconds,
                    max_speed, max_speed_value, user_id, username, user_name, confirmed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.car_rank,
                    result.car,
                    result.engine,
                    result.time,
                    result.time_seconds,
                    result.max_speed,
                    result.max_speed_value,
                    result.user_id,
                    result.username,
                    result.user_name,
                    1 if result.confirmed else 0,
                ),
            )
            new_id = int(cursor.lastrowid)
            conn.execute(f"DELETE FROM {from_table} WHERE id = ?", (result_id,))
            conn.commit()
        return new_id

        table = self._table_name(track)
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE id = ?",
                (result_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_top_results(
        self,
        track: str,
        limit: int = 10,
        car_class: str | None = None,
    ) -> list[RaceResult]:
        table = self._table_name(track)
        where_clause = ""
        params: list[object] = []

        if car_class and car_class != "all":
            where_clause = "WHERE UPPER(REPLACE(car_rank, '+', '')) = ?"
            params.append(normalize_car_rank(car_class).replace("+", ""))

        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM {table}
                {where_clause}
                ORDER BY
                    CASE WHEN time_seconds IS NOT NULL THEN 0 ELSE 1 END,
                    time_seconds ASC,
                    CASE WHEN max_speed_value IS NOT NULL THEN 0 ELSE 1 END,
                    max_speed_value DESC,
                    id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [self._row_to_result(row) for row in rows]

    def clear_all_results(self) -> None:
        with self._connect() as conn:
            for track in config.TRACKS:
                conn.execute(f"DELETE FROM {self._table_name(track)}")
            conn.commit()

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> RaceResult:
        return RaceResult(
            id=row["id"],
            car_rank=row["car_rank"],
            car=row["car"],
            engine=row["engine"],
            time=row["time"],
            time_seconds=row["time_seconds"],
            max_speed=row["max_speed"],
            max_speed_value=row["max_speed_value"],
            user_id=row["user_id"],
            username=row["username"],
            user_name=row["user_name"],
            confirmed=bool(row["confirmed"]),
            created_at=row["created_at"],
        )
