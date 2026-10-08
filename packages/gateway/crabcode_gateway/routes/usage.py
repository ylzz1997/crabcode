"""Provider-reported token usage, scoped to this Gateway's local ledger."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request

from crabcode_core import usage as usage_module
from crabcode_core.usage import UsageStore
from crabcode_gateway.routes.workspace import _resolve_directory, _workspace_roots
from crabcode_gateway.schemas import UsageDailyResponse

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/daily", response_model=UsageDailyResponse)
async def daily_usage(request: Request, start_date: date, end_date: date,
                      timezone: str = Query(..., max_length=100),
                      cwd: str | None = None) -> dict:
    if (end_date - start_date).days < 0 or (end_date - start_date).days >= 366:
        raise HTTPException(status_code=422, detail="日期范围必须为 1–366 天")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        try:
            ZoneInfo("Etc/UTC")
        except ZoneInfoNotFoundError:
            raise HTTPException(status_code=503, detail=(
                "Gateway 缺少 IANA 时区数据；请在 Gateway 使用的 Python 环境安装 tzdata 后重启 Gateway"
            )) from None
        raise HTTPException(status_code=422, detail="无效的 IANA 时区") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="无效的 IANA 时区") from None
    if end_date > datetime.now(zone).date():
        raise HTTPException(status_code=422, detail="结束日期不能晚于今天")
    store = UsageStore()
    if usage_module.recording_error or store.error_marker.is_file():
        raise HTTPException(status_code=503, detail=(
            f"使用记录曾写入失败，统计可能不完整；请检查 Gateway/CLI 日志及 {store.error_marker}"
        ))
    project = str(_resolve_directory(cwd, _workspace_roots(request))) if cwd else None
    try:
        return await asyncio.to_thread(store.daily, start_date, end_date, zone, project)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="使用记录暂时无法读取") from exc
