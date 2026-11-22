from __future__ import annotations
import json
import sqlite3
from fastapi import HTTPException
from app.db import db
from app.services.core import now
from app.services import repair_surface

# 漂移修补全局唯一真源：以在册素材段为准。
#   - 按在册/已过期分区的素材段集合，对齐湖对象行（inventory_objects）：
#     补齐缺失行、删除多出行、对齐字节指纹；
#   - 在册素材段（clips）不因修补增删改；
#   - 修补流水逐键记录动作与前后字节，可重放；
#   - 全局不得混用「以湖对象行为准」的反向修补。

DRIFT_ONLY_META = "元数据有、目录无"
DRIFT_ONLY_INV = "目录有、元数据无"
DRIFT_BYTES = "字节不一致"


def current_drifts(conn):
    """在调用方事务内重算当前漂移（键集合 + 字节指纹），结果按对象键排序。"""
    meta_keys = {
        r["object_key"]: r["bytes"]
        for r in conn.execute(
            '''SELECT c.object_key, c.bytes FROM clips c
               JOIN partitions p ON p.id=c.partition_id
               WHERE p.status IN ('在册','已过期')'''
        ).fetchall()
    }
    inv_keys = {
        r["object_key"]: r["bytes"]
        for r in conn.execute("SELECT object_key, bytes FROM inventory_objects").fetchall()
    }
    drifts = []
    for k in sorted(set(meta_keys) - set(inv_keys)):
        drifts.append({"对象键": k, "问题": DRIFT_ONLY_META, "元数据字节": meta_keys[k], "目录字节": None})
    for k in sorted(set(inv_keys) - set(meta_keys)):
        drifts.append({"对象键": k, "问题": DRIFT_ONLY_INV, "元数据字节": None, "目录字节": inv_keys[k]})
    for k in sorted(set(meta_keys) & set(inv_keys)):
        if meta_keys[k] != inv_keys[k]:
            drifts.append({"对象键": k, "问题": DRIFT_BYTES, "元数据字节": meta_keys[k], "目录字节": inv_keys[k]})
    return drifts, meta_keys, inv_keys


def inventory_list():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM inventory_objects ORDER BY object_key").fetchall()]


def clips_ledger():
    """素材段表：互证口径内的在册素材段（修补真源侧，修补不得改动）。"""
    with db() as conn:
        rows = conn.execute(
            '''SELECT c.id, c.object_key, c.bytes, c.entered_day, c.note,
                      p.part_key, p.title, p.status AS partition_status, v.name AS vault_name
               FROM clips c
               JOIN partitions p ON p.id=c.partition_id
               JOIN vaults v ON v.id=p.vault_id
               WHERE p.status IN ('在册','已过期')
               ORDER BY c.object_key'''
        ).fetchall()
        return [dict(r) for r in rows]


def run_verify():
    with db() as conn:
        drifts, meta_keys, inv_keys = current_drifts(conn)
        status = "一致" if not drifts else "漂移"
        cur = conn.execute(
            "INSERT INTO verify_runs(status, drift_count, detail, created_at) VALUES (?,?,?,?)",
            (status, len(drifts), json.dumps(drifts, ensure_ascii=False), now()),
        )
        return {
            "run_id": cur.lastrowid,
            "status": status,
            "drift_count": len(drifts),
            "drifts": drifts,
            "meta_count": len(meta_keys),
            "inventory_count": len(inv_keys),
        }


def repair_drift(run_id: int):
    """一键修补：以在册素材段为唯一真源，对齐湖对象行。

    闭合性保证：
    - BEGIN IMMEDIATE 串行化并发修补，锁内重算当前漂移而非盲放旧回执，
      并发双修补不会两边各删各的；
    - repair_runs.verify_run_id UNIQUE，同一回执只能闭合一次；
    - 修正、修补流水、复证回执同事务提交，同成败；任一步失败整体回滚；
    - 修完在同事务内复证，仍漂移则抛错回滚，绝不只改漂移数字。
    """
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT * FROM verify_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, detail="互证回执不存在")
        if run["status"] != "漂移":
            raise HTTPException(400, detail="该回执无漂移，无需修补")
        if conn.execute("SELECT 1 FROM repair_runs WHERE verify_run_id=?", (run_id,)).fetchone():
            raise HTTPException(409, detail="该回执已修补闭合，禁止重复修补")

        drifts, _, _ = current_drifts(conn)
        ts = now()
        fixes = []
        for d in drifts:
            k = d["对象键"]
            if d["问题"] == DRIFT_ONLY_META:
                # 在册素材段在、湖对象行缺：按素材段补齐湖对象行
                src = conn.execute(
                    '''SELECT v.name AS vault_name FROM clips c
                       JOIN partitions p ON p.id=c.partition_id
                       JOIN vaults v ON v.id=p.vault_id
                       WHERE c.object_key=?''',
                    (k,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO inventory_objects(object_key, bytes, vault_name, scanned_at) VALUES (?,?,?,?)",
                    (k, d["元数据字节"], src["vault_name"], ts),
                )
                fixes.append({"对象键": k, "问题": d["问题"], "动作": "补齐湖对象行", "前字节": None, "后字节": d["元数据字节"]})
            elif d["问题"] == DRIFT_ONLY_INV:
                # 湖对象行多出、无在册素材段：删除湖对象行（素材段侧不动）
                conn.execute("DELETE FROM inventory_objects WHERE object_key=?", (k,))
                fixes.append({"对象键": k, "问题": d["问题"], "动作": "删除湖对象行", "前字节": d["目录字节"], "后字节": None})
            else:
                # 字节指纹漂移：以素材段字节为准对齐湖对象行
                conn.execute(
                    "UPDATE inventory_objects SET bytes=?, scanned_at=? WHERE object_key=?",
                    (d["元数据字节"], ts, k),
                )
                fixes.append({"对象键": k, "问题": d["问题"], "动作": "对齐字节", "前字节": d["目录字节"], "后字节": d["元数据字节"]})

        left, _, _ = current_drifts(conn)
        if left:
            raise HTTPException(500, detail="修补后复证仍漂移，已整体回滚")
        try:
            cur = conn.execute(
                "INSERT INTO repair_runs(verify_run_id, status, fixed_count, detail, created_at) VALUES (?,?,?,?,?)",
                (run_id, "已修补", len(fixes), json.dumps(fixes, ensure_ascii=False), ts),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail="该回执已修补闭合，禁止重复修补")
        # 修补后复证：回执、流水、总览漂移徽章同刷闭合
        conn.execute(
            "INSERT INTO verify_runs(status, drift_count, detail, created_at) VALUES ('一致', 0, '[]', ?)",
            (ts,),
        )
        return {
            "repair_id": cur.lastrowid,
            "verify_run_id": run_id,
            "status": "已修补",
            "fixed_count": len(fixes),
            "fixes": fixes,
        }


def list_verify_runs():
    with db() as conn:
        rows = conn.execute(
            '''SELECT v.id, v.status, v.drift_count, v.detail, v.created_at,
                      r.id AS repair_id
               FROM verify_runs v
               LEFT JOIN repair_runs r ON r.verify_run_id=v.id
               ORDER BY v.id DESC'''
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["drifts"] = json.loads(d.pop("detail"))
            out.append(d)
        return out


def list_repairs():
    with db() as conn:
        rows = conn.execute(
            '''SELECT r.id, r.verify_run_id, r.status, r.fixed_count, r.detail, r.created_at,
                      v.drift_count AS receipt_drift_count
               FROM repair_runs r
               JOIN verify_runs v ON v.id=r.verify_run_id
               ORDER BY r.id DESC'''
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["fixes"] = json.loads(d.pop("detail"))
            out.append(d)
        return out

_repair_drift_impl = repair_drift


def repair_drift(run_id: int):
    out = _repair_drift_impl(run_id)
    return repair_surface.after_repair(out)

