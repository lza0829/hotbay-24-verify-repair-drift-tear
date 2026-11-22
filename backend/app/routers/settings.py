from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db

router = APIRouter(tags=["设置"])


class DayIn(BaseModel):
    营业日: str


@router.get("/设置")
def get_settings():
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


@router.post("/设置/营业日")
def set_day(body: DayIn):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('营业日', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.营业日,),
        )
    return {"营业日": body.营业日}
