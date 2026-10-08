"""Request-level, provider-reported token ledger and local calendar aggregation.

This store deliberately does not estimate missing usage or rewrite session totals.
Each network attempt has its own row, including retries and interrupted streams.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import logging
import os
import sqlite3
import time
import uuid
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk, usage_int_field
from crabcode_core.paths import get_config_home

logger = logging.getLogger(__name__)
recording_error: str | None = None


class UsageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_config_home() / "usage.sqlite3"

    @property
    def error_marker(self) -> Path:
        return self.path.with_name("usage-recording-error.txt")

    def mark_error(self) -> None:
        """Make a write gap visible to another CLI/Gateway process when possible."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.error_marker.write_text(
                "Usage recording failed in a CrabCode process. Historical totals may be incomplete.\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Unable to persist usage recording failure marker", exc_info=True)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 3
        while True:
            db = sqlite3.connect(self.path, timeout=3)
            try:
                db.execute("PRAGMA busy_timeout=3000")
                if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("""CREATE TABLE IF NOT EXISTS requests (
                        id TEXT PRIMARY KEY, started_ms INTEGER NOT NULL, ended_ms INTEGER,
                        project_key TEXT, project_path TEXT, requested_model TEXT NOT NULL,
                        model_id TEXT NOT NULL, provider TEXT NOT NULL, purpose TEXT NOT NULL,
                        session_id TEXT, operation_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
                        reported_total_tokens INTEGER, cache_read_tokens INTEGER,
                        cache_write_tokens INTEGER, reasoning_tokens INTEGER,
                        usage_status TEXT NOT NULL, status TEXT NOT NULL, stop_reason TEXT
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS requests_time ON requests(started_ms)")
                    db.execute("CREATE INDEX IF NOT EXISTS requests_project_time ON requests(project_key, started_ms)")
                    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version', '1')")
                    db.execute("PRAGMA user_version=1")
                    db.commit()
                return db
            except sqlite3.OperationalError as exc:
                db.close()
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
            except BaseException:
                db.close()
                raise

    @staticmethod
    def project_key(cwd: str | None) -> str | None:
        return os.path.normcase(os.path.normpath(os.path.abspath(cwd))) if cwd else None

    def begin(self, request_id: str, started_ms: int, *, cwd: str | None,
              model: str, provider: str, purpose: str, session_id: str | None,
              operation_id: str | None = None) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('tracking_started_ms', ?)", (str(started_ms),))
            db.execute("""INSERT INTO requests
                (id, started_ms, project_key, project_path, requested_model, model_id,
                 provider, purpose, session_id, operation_id, usage_status, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'missing', 'running')""",
                (request_id, started_ms, self.project_key(cwd), cwd,
                 model, model, provider, purpose, session_id, operation_id))

    def snapshot(self, request_id: str, usage: dict[str, int], model_id: str | None = None) -> None:
        """Persist changed cumulative usage, including before a process crashes."""
        inputs = usage.get("total_input_tokens", usage.get("input_tokens"))
        outputs = usage.get("output_tokens")
        total = usage.get("total_tokens")
        status = "complete" if inputs is not None and outputs is not None else "partial" if usage else "missing"
        with closing(self._connect()) as db, db:
            db.execute("""UPDATE requests SET input_tokens=?, output_tokens=?,
                reported_total_tokens=?, cache_read_tokens=?, cache_write_tokens=?,
                reasoning_tokens=?, usage_status=?, model_id=COALESCE(?, model_id) WHERE id=?""",
                (inputs, outputs, total, usage.get("cache_read_tokens"),
                 usage.get("cache_write_tokens"), usage.get("reasoning_tokens"),
                 status, model_id, request_id))

    def finish(self, request_id: str, usage: dict[str, int],
               status: str, stop_reason: str | None = None, model_id: str | None = None) -> None:
        inputs = usage.get("total_input_tokens", usage.get("input_tokens"))
        outputs = usage.get("output_tokens")
        usage_status = "complete" if inputs is not None and outputs is not None else "partial" if usage else "missing"
        with closing(self._connect()) as db, db:
            db.execute("""UPDATE requests SET ended_ms=?, status=?, stop_reason=?,
                input_tokens=?, output_tokens=?, reported_total_tokens=?,
                cache_read_tokens=?, cache_write_tokens=?, reasoning_tokens=?,
                usage_status=?, model_id=COALESCE(?, model_id) WHERE id=?""",
                (int(time.time() * 1000), status, stop_reason, inputs, outputs,
                 usage.get("total_tokens"), usage.get("cache_read_tokens"),
                 usage.get("cache_write_tokens"), usage.get("reasoning_tokens"),
                 usage_status, model_id, request_id))

    def daily(self, start: date, end: date, zone: ZoneInfo,
              cwd: str | None = None) -> dict[str, Any]:
        if end < start or (end - start).days >= 366:
            raise ValueError("日期范围必须为 1–366 天")
        lower = int(datetime.combine(start, day_time.min, zone).timestamp() * 1000)
        upper = int(datetime.combine(end + timedelta(days=1), day_time.min, zone).timestamp() * 1000)
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT value FROM metadata WHERE key='tracking_started_ms'").fetchone()
            tracking_start = int(row[0]) if row else None
            args: list[Any] = [lower, upper]
            clause = ""
            if cwd is not None:
                clause = " AND project_key=?"
                args.append(self.project_key(cwd))
            rows = db.execute("""SELECT started_ms, provider, model_id, input_tokens,
                output_tokens, reported_total_tokens, usage_status, status
                FROM requests WHERE started_ms>=? AND started_ms<?""" + clause,
                args).fetchall()
            activity_rows = db.execute("""SELECT provider, model_id, COUNT(*)
                FROM requests WHERE 1=1""" + clause + " GROUP BY provider, model_id",
                [self.project_key(cwd)] if cwd is not None else []).fetchall()
            bounds = db.execute("SELECT MIN(started_ms), MAX(started_ms) FROM requests WHERE 1=1" + clause,
                                [self.project_key(cwd)] if cwd is not None else []).fetchone()

        days: dict[str, dict[str, Any]] = {}
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            days[key] = {"date": key, "input_tokens": 0, "output_tokens": 0,
                         "total_tokens": None, "request_count": 0,
                         "unknown_requests": 0, "missing_requests": 0,
                         "partial_requests": 0, "coverage": "unavailable"}
            cursor += timedelta(days=1)
        by_model: dict[str, dict[str, int]] = {}
        model_totals: dict[str, int] = {}
        for started_ms, provider, model_id, input_tokens, output_tokens, reported_total, usage_status, status in rows:
            key = datetime.fromtimestamp(started_ms / 1000, timezone.utc).astimezone(zone).date().isoformat()
            day = days[key]
            day["request_count"] += 1
            if usage_status != "complete" or status not in {"completed", "output_limit"}:
                day["unknown_requests"] += 1
                day["missing_requests" if usage_status == "missing" else "partial_requests"] += 1
            inputs = input_tokens or 0
            outputs = output_tokens or 0
            amount = reported_total if reported_total is not None else inputs + outputs
            day["input_tokens"] += inputs
            day["output_tokens"] += outputs
            day["total_tokens"] = (day["total_tokens"] or 0) + amount
            model_key = f"{provider}/{model_id or '未知模型'}"
            model_day = by_model.setdefault(model_key, {})
            model_day[key] = model_day.get(key, 0) + amount
            model_totals[model_key] = model_totals.get(model_key, 0) + amount
        for key, day in days.items():
            day_start = int(datetime.combine(date.fromisoformat(key), day_time.min, zone).timestamp() * 1000)
            if day["unknown_requests"]:
                day["coverage"] = "partial"
            elif tracking_start is None or day_start < tracking_start:
                day["coverage"] = "partial" if day["request_count"] else "unavailable"
            else:
                day["coverage"] = "complete"
            if day["coverage"] == "complete" and day["total_tokens"] is None:
                day["total_tokens"] = 0
        activity_counts = {f"{provider}/{model_id or '未知模型'}": count
                           for provider, model_id, count in activity_rows}
        models = [{"key": model_key, "provider": model_key.split("/", 1)[0],
                    "model_id": model_key.split("/", 1)[1], "model": model_key,
                    "total_tokens": model_totals[model_key],
                    "recorded_request_count": activity_counts[model_key],
                    "points": [{"date": key, "total_tokens": totals.get(key, 0) if days[key]["coverage"] != "unavailable" else None}
                              for key in days]}
                  for model_key, totals in sorted(by_model.items())]
        return {"start": start.isoformat(), "end": end.isoformat(), "timezone": str(zone),
                "scope": "project" if cwd is not None else "global",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "tracking_started_at": datetime.fromtimestamp(tracking_start / 1000, timezone.utc).isoformat() if tracking_start else None,
                "first_recorded_at": datetime.fromtimestamp(bounds[0] / 1000, timezone.utc).isoformat() if bounds[0] else None,
                "last_recorded_at": datetime.fromtimestamp(bounds[1] / 1000, timezone.utc).isoformat() if bounds[1] else None,
                "days": list(days.values()), "models": models,
                "summary": {"input_tokens": sum(d["input_tokens"] for d in days.values()),
                            "output_tokens": sum(d["output_tokens"] for d in days.values()),
                            "total_tokens": sum(d["total_tokens"] or 0 for d in days.values()),
                            "request_count": sum(d["request_count"] for d in days.values()),
                            "unknown_requests": sum(d["unknown_requests"] for d in days.values()),
                            "missing_requests": sum(d["missing_requests"] for d in days.values()),
                            "partial_requests": sum(d["partial_requests"] for d in days.values())}}


async def tracked_stream_message(
    adapter: APIAdapter, *, messages: Any, system: list[str], tools: list[dict[str, Any]],
    config: ModelConfig, cwd: str | None = None, session_id: str | None = None,
    purpose: str = "conversation", operation_id: str | None = None,
) -> AsyncIterator[StreamChunk]:
    """Yield unchanged provider chunks; persist one row per attempt on close."""
    global recording_error
    store = UsageStore()
    request_id = uuid.uuid4().hex
    provider = str(getattr(getattr(adapter, "config", None), "provider", None) or type(adapter).__name__)
    recorded = False
    try:
        await asyncio.to_thread(store.begin, request_id, int(time.time() * 1000),
                                cwd=cwd, model=config.model, provider=provider,
                                purpose=purpose, session_id=session_id,
                                operation_id=operation_id)
        recorded = True
    except Exception:
        recording_error = "使用记录数据库无法写入；部分请求可能未被统计"
        store.mark_error()
        logger.warning("Unable to start usage record", exc_info=True)
    usage: dict[str, int] = {}
    outcome = "interrupted"
    stop_reason: str | None = None
    actual_model: str | None = None
    source = None
    try:
        source = adapter.stream_message(messages=messages, system=system, tools=tools, config=config)
        async for chunk in source:
            if chunk.model_id:
                actual_model = chunk.model_id
            if chunk.usage:
                changed = False
                for key in ("total_input_tokens", "input_tokens", "output_tokens", "total_tokens",
                            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
                    value, present = usage_int_field(chunk.usage, key)
                    if present and (key not in usage or value > usage[key]):
                        usage[key] = value
                        changed = True
                if changed and recorded:
                    try:
                        await asyncio.to_thread(store.snapshot, request_id, usage.copy(), actual_model)
                    except Exception:
                        recording_error = "使用记录数据库无法写入；部分请求可能未被统计"
                        store.mark_error()
                        logger.warning("Unable to save usage snapshot", exc_info=True)
            if chunk.stop_reason:
                stop_reason = chunk.stop_reason
            if chunk.type == "error":
                outcome = "error"
            yield chunk
        if outcome != "error":
            outcome = "output_limit" if stop_reason in {"max_tokens", "max_output_tokens", "length"} else "completed"
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "interrupted"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        try:
            if source is not None:
                await source.aclose()
        finally:
            if recorded:
                try:
                    await asyncio.to_thread(store.finish, request_id, usage.copy(), outcome, stop_reason, actual_model)
                except Exception:
                    recording_error = "使用记录数据库无法写入；部分请求可能未被统计"
                    store.mark_error()
                    logger.warning("Unable to finish usage record", exc_info=True)
