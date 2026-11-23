from __future__ import annotations
import json
import sqlite3
from fastapi import HTTPException
from app.db import db
from app.services.core import now

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


def drift_keys():
    """实时漂移键 → 问题，供段册表、湖侧键行、分区册逐行标注。"""
    with db() as conn:
        drifts, _, _ = current_drifts(conn)
        return {d["对象键"]: d["问题"] for d in drifts}


def inventory_list():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM inventory_objects ORDER BY object_key").fetchall()]


def clips_ledger():
    """素材段表：互证口径内的在册素材段（修补真源侧，修补不得改动）。

    返回行只含素材段事实字段——修补前后必须逐字段相等，这是「素材段不因
    修补改动」的证据。漂移标注由 /互证状态 的实时漂移列表按对象键合并。
    """
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
        if conn.execute("SELECT 1 FROM repair_runs WHERE verify_run_id=?", (run_id,)).fetchone():
            raise HTTPException(409, detail="该回执已修补闭合，禁止重复修补")

        # 准入看事务内重算的真实漂移，不看回执快照：
        # - 真实有漂移：无论快照写什么都必须能修补（回执被改成「一致」也不能挡）；
        # - 真实无漂移：仅当回执本就写着「一致」才拒绝。回执仍是「漂移」说明
        #   漂移已被并发那一笔撮合，本笔以零修正闭合，不两边各删各的。
        drifts, _, _ = current_drifts(conn)
        if not drifts and run["status"] != "漂移":
            raise HTTPException(400, detail="当前无漂移，无需修补")
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
            # 修补成功必须能由段册∪湖侧键集合重放为零漂移；否则整笔回滚、熄灭成功灯
            raise HTTPException(500, detail="修补后复证仍漂移，已整体回滚")
        try:
            cur = conn.execute(
                "INSERT INTO repair_runs(verify_run_id, status, fixed_count, detail, created_at) VALUES (?,?,?,?,?)",
                (run_id, "已修补", len(fixes), json.dumps(fixes, ensure_ascii=False), ts),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail="该回执已修补闭合，禁止重复修补")
        # 修后复证回执取同事务内重算结果（此刻必为零漂移），与修正、流水同成败
        recheck = conn.execute(
            "INSERT INTO verify_runs(status, drift_count, detail, created_at) VALUES (?,?,?,?)",
            ("一致", len(left), json.dumps(left, ensure_ascii=False), ts),
        )
        return {
            "repair_id": cur.lastrowid,
            "verify_run_id": run_id,
            "recheck_run_id": recheck.lastrowid,
            "status": "已修补",
            "fixed_count": len(fixes),
            "drift_count": len(left),
            "fixes": fixes,
        }


def repair_still_aligned(conn, fixes):
    """修补成功灯的证据：账页每条动作必须能在湖侧键账上复核成声明的终态。

    - 补齐湖对象行：湖侧现有该键且字节等于后字节；
    - 删除湖对象行：湖侧已无该键；
    - 对齐字节：湖侧字节等于后字节，且前后字节确有变化。

    只有账页、没有真实键账变化的「假修补」在这里被识破（幽灵键不在湖侧，
    声明的终态无法重放）。全局是否对齐由 current_drifts 实时重算决定；
    本函数只回答「这一笔修补的成功是否名副其实」，不替代漂移重算。
    """
    inv = {
        r["object_key"]: r["bytes"]
        for r in conn.execute("SELECT object_key, bytes FROM inventory_objects").fetchall()
    }
    for f in fixes or []:
        key = f.get("对象键")
        action = f.get("动作")
        before = f.get("前字节")
        after = f.get("后字节")
        if not key:
            return False
        if action == "补齐湖对象行":
            if before is not None or after is None or inv.get(key) != after:
                return False
        elif action == "删除湖对象行":
            if after is not None or before is None or key in inv:
                return False
        elif action == "对齐字节":
            if before is None or after is None or before == after or inv.get(key) != after:
                return False
        else:
            return False
    return True


def alignment_state():
    """互证页对齐灯的实时口径：当前漂移重算 + 最近修补账页复核。

    对齐灯不得由回执快照点亮：段册∪湖侧键集合仍漂移，或最近修补账页
    在当前键账上复核不过，一律视为未对齐。
    """
    with db() as conn:
        drifts, meta_keys, inv_keys = current_drifts(conn)
        last = conn.execute(
            "SELECT id, verify_run_id, detail FROM repair_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        repair_aligned = True
        last_repair_id = None
        if last:
            last_repair_id = last["id"]
            repair_aligned = repair_still_aligned(conn, json.loads(last["detail"] or "[]"))
        return {
            "drift_count": len(drifts),
            "drifts": drifts,
            "aligned": not drifts and repair_aligned,
            "repair_evidence": repair_aligned,
            "last_repair_id": last_repair_id,
            "meta_count": len(meta_keys),
            "inventory_count": len(inv_keys),
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
            # 账页「对齐证据」实时复核：账页有行不代表两侧键账仍对齐
            d["aligned"] = repair_still_aligned(conn, d["fixes"])
            out.append(d)
        return out

