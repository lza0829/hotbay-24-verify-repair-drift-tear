from __future__ import annotations
from fastapi import HTTPException
from app.db import db
from app.services.core import now, require_tier, vault_partition_count
from app.services.verify import drift_keys


def list_partitions(conn=None):
    def _q(c):
        rows = c.execute(
            '''SELECT p.id, p.part_key, p.title, p.release_day, p.vault_id, p.tier,
                      p.object_prefix, p.status, p.created_at, v.name AS vault_name,
                      (SELECT COUNT(*) FROM clips c WHERE c.partition_id=p.id) AS clip_count,
                      (SELECT COALESCE(SUM(bytes),0) FROM clips c WHERE c.partition_id=p.id) AS total_bytes
               FROM partitions p JOIN vaults v ON v.id=p.vault_id
               ORDER BY p.id DESC'''
        ).fetchall()
        return [dict(r) for r in rows]

    if conn is not None:
        return _q(conn)
    with db() as c:
        rows = _q(c)
    # 分区册与互证页同源：逐分区标注实时重算的漂移段数，不许旁路改数
    drifts = drift_keys()
    with db() as c:
        for r in rows:
            keys = [
                x["object_key"]
                for x in c.execute("SELECT object_key FROM clips WHERE partition_id=?", (r["id"],)).fetchall()
            ]
            r["漂移段数"] = sum(1 for k in keys if drifts.get(k))
    return rows


def create_partition(part_key: str, title: str, release_day: str, vault_id: int, tier: str, object_prefix: str, clips: list):
    tier = require_tier(tier)
    with db() as conn:
        v = conn.execute("SELECT * FROM vaults WHERE id=?", (vault_id,)).fetchone()
        if not v or not v["active"]:
            raise HTTPException(400, detail="片库间不可用")
        used = vault_partition_count(conn, vault_id)
        if used >= v["capacity"]:
            raise HTTPException(400, detail="片库间分区已满，拒绝登记")
        if conn.execute("SELECT 1 FROM partitions WHERE part_key=?", (part_key,)).fetchone():
            raise HTTPException(400, detail="分区键已存在")
        if not clips:
            raise HTTPException(400, detail="至少挂一段素材元数据")
        cur = conn.execute(
            "INSERT INTO partitions(part_key, title, release_day, vault_id, tier, object_prefix, status, created_at) VALUES (?,?,?,?,?,?, '在册', ?)",
            (part_key, title, release_day, vault_id, tier, object_prefix, now()),
        )
        pid = cur.lastrowid
        for clip in clips:
            ok = clip["object_key"]
            if conn.execute("SELECT 1 FROM clips WHERE object_key=?", (ok,)).fetchone():
                raise HTTPException(400, detail=f"对象键重复：{ok}")
            conn.execute(
                "INSERT INTO clips(partition_id, object_key, bytes, entered_day, note) VALUES (?,?,?,?,?)",
                (pid, ok, int(clip["bytes"]), clip.get("entered_day") or release_day, clip.get("note") or ""),
            )
            conn.execute(
                "INSERT OR REPLACE INTO inventory_objects(object_key, bytes, vault_name, scanned_at) VALUES (?,?,?,?)",
                (ok, int(clip["bytes"]), v["name"], now()),
            )
        return {"id": pid, "part_key": part_key, "tier": tier}
