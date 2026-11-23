from __future__ import annotations
import json
from fastapi import APIRouter
from app.db import db
from app.services.verify import current_drifts, repair_still_aligned

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        def cnt(sql, *a):
            return conn.execute(sql, a).fetchone()["c"]

        drifts, _, _ = current_drifts(conn)
        last_verify = conn.execute(
            "SELECT status, drift_count, created_at FROM verify_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_repair = conn.execute(
            "SELECT id, verify_run_id, fixed_count, detail, created_at FROM repair_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        repair = dict(last_repair) if last_repair else None
        if repair is not None:
            fixes = json.loads(repair.pop("detail") or "[]")
            # 账页有行不等于对齐：对齐证据必须能在当前两侧键账上复核通过
            repair["aligned"] = repair_still_aligned(conn, fixes)
        # 漂移条、互证灯、修补账同源于这一次重算，任一处对不上就不算已对齐
        aligned = not drifts and (repair is None or repair["aligned"])
        return {
            "热档": cnt("SELECT COUNT(*) AS c FROM partitions WHERE tier='热档' AND status='在册'"),
            "温档": cnt("SELECT COUNT(*) AS c FROM partitions WHERE tier='温档' AND status='在册'"),
            "冷档": cnt("SELECT COUNT(*) AS c FROM partitions WHERE tier='冷档' AND status='在册'"),
            "迁移执行中": cnt("SELECT COUNT(*) AS c FROM migrate_jobs WHERE status='执行中'"),
            "待销毁候选": cnt("SELECT COUNT(*) AS c FROM partitions WHERE status='已过期'"),
            "已销毁": cnt("SELECT COUNT(*) AS c FROM partitions WHERE status='已销毁'"),
            "目录对象": cnt("SELECT COUNT(*) AS c FROM inventory_objects"),
            # 实时重算的漂移徽章，不取回执快照，杜绝只改数字
            "当前漂移": len(drifts),
            "已对齐": aligned,
            "最近互证": (
                lambda r: dict(r) if r else None
            )(last_verify),
            "最近修补": repair,
            "片库间": [dict(x) for x in conn.execute("SELECT id, name, capacity, active FROM vaults ORDER BY id").fetchall()],
        }
