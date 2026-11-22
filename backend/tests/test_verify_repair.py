"""漂移修补（以在册素材段为唯一真源）的闭合性测试。

覆盖：三类漂移识别、一键修补闭合、流水可重放、同回执禁双修补、
并发修补不双删、失败整体回滚、在册素材段不被修补改动。
"""
from __future__ import annotations
import os
import tempfile

os.environ["HOTBAY_DB"] = os.path.join(tempfile.mkdtemp(prefix="hotbay-test-"), "test.db")

import threading  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import db  # noqa: E402
from app.services import verify as verify_svc  # noqa: E402

client = TestClient(app)

VAULT = {}


@pytest.fixture(autouse=True)
def fresh():
    with db() as c:
        for t in ("repair_runs", "verify_runs", "inventory_objects", "clips",
                  "migrate_jobs", "destroy_runs", "partitions", "vaults",
                  "retention_policies", "settings"):
            c.execute(f"DELETE FROM {t}")
        cur = c.execute("INSERT INTO vaults(name, capacity, active) VALUES ('测试间', 100, 1)")
        VAULT["id"] = cur.lastrowid
        c.execute("INSERT INTO settings(key, value) VALUES ('营业日', '2026-11-22')")
    yield


def _partition(c, key="P-测", prefix="reel/测/"):
    cur = c.execute(
        "INSERT INTO partitions(part_key, title, release_day, vault_id, tier, object_prefix, status, created_at)"
        " VALUES (?,?,?,?,'热档',?,'在册','2026-01-01T00:00:00')",
        (key, "测试片", "2026-01-01", VAULT["id"], prefix),
    )
    return cur.lastrowid


def _clip(c, pid, key, nbytes):
    c.execute(
        "INSERT INTO clips(partition_id, object_key, bytes, entered_day, note) VALUES (?,?,?,'2026-01-01','段')",
        (pid, key, nbytes),
    )


def _inv(c, key, nbytes):
    c.execute(
        "INSERT INTO inventory_objects(object_key, bytes, vault_name, scanned_at) VALUES (?,?,'测试间','2026-01-02T00:00:00')",
        (key, nbytes),
    )


def _make_drift():
    """三类漂移：元数据有目录无 / 目录有元数据无 / 字节不一致；另含一个一致键。"""
    with db() as c:
        pid = _partition(c)
        _clip(c, pid, "reel/测/ok.bin", 100)
        _inv(c, "reel/测/ok.bin", 100)
        _clip(c, pid, "reel/测/only-meta.bin", 200)
        _inv(c, "reel/测/only-inv.bin", 300)
        _clip(c, pid, "reel/测/size.bin", 400)
        _inv(c, "reel/测/size.bin", 999)


def _make_aligned():
    with db() as c:
        pid = _partition(c)
        _clip(c, pid, "reel/测/ok.bin", 100)
        _inv(c, "reel/测/ok.bin", 100)


def _repair(run_id):
    return client.post(f"/api/互证/{run_id}/修补")


def test_verify_reports_three_drift_kinds():
    _make_drift()
    run = client.post("/api/互证").json()
    assert run["status"] == "漂移" and run["drift_count"] == 3
    by_key = {d["对象键"]: d["问题"] for d in run["drifts"]}
    assert by_key["reel/测/only-meta.bin"] == "元数据有、目录无"
    assert by_key["reel/测/only-inv.bin"] == "目录有、元数据无"
    assert by_key["reel/测/size.bin"] == "字节不一致"


def test_repair_closes_drift_end_to_end():
    _make_drift()
    clips_before = client.get("/api/素材段清单").json()
    run = client.post("/api/互证").json()
    assert client.get("/api/总览").json()["当前漂移"] == 3

    r = _repair(run["run_id"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "已修补" and body["fixed_count"] == 3

    # 总览漂移徽章、最近互证、最近修补同刷闭合
    ov = client.get("/api/总览").json()
    assert ov["当前漂移"] == 0
    assert ov["最近互证"]["status"] == "一致"
    assert ov["最近互证"]["drift_count"] == 0
    assert ov["最近修补"]["id"] == body["repair_id"]
    assert ov["最近修补"]["verify_run_id"] == run["run_id"]

    # 修完再证必一致：修完仍漂移即未闭合
    again = client.post("/api/互证").json()
    assert again["status"] == "一致" and again["drift_count"] == 0

    # 湖对象行与在册素材段逐键对齐；在册素材段不因修补删改
    meta = {x["object_key"]: x["bytes"] for x in client.get("/api/素材段清单").json()}
    inv = {x["object_key"]: x["bytes"] for x in client.get("/api/目录清单").json()}
    assert inv == meta
    assert client.get("/api/素材段清单").json() == clips_before

    # 回执已闭合，互证流水联动修补号
    runs = client.get("/api/互证流水").json()
    closed = [x for x in runs if x["id"] == run["run_id"]][0]
    assert closed["repair_id"] == body["repair_id"]


def test_repair_ledger_replayable():
    _make_drift()
    before = {x["object_key"]: x["bytes"] for x in client.get("/api/目录清单").json()}
    run = client.post("/api/互证").json()
    assert _repair(run["run_id"]).status_code == 200

    repairs = client.get("/api/修补流水").json()
    assert len(repairs) == 1
    fixes = repairs[0]["fixes"]
    assert {f["动作"] for f in fixes} == {"补齐湖对象行", "删除湖对象行", "对齐字节"}

    # 重放：流水动作施加到修补前快照，必须重放出修补后的湖对象集合
    replayed = dict(before)
    for f in fixes:
        if f["动作"] == "删除湖对象行":
            assert before[f["对象键"]] == f["前字节"]
            replayed.pop(f["对象键"], None)
        else:
            replayed[f["对象键"]] = f["后字节"]
    after = {x["object_key"]: x["bytes"] for x in client.get("/api/目录清单").json()}
    assert replayed == after


def test_same_receipt_cannot_close_twice():
    _make_drift()
    run = client.post("/api/互证").json()
    assert _repair(run["run_id"]).status_code == 200
    r = _repair(run["run_id"])
    assert r.status_code == 409
    assert len(client.get("/api/修补流水").json()) == 1


def test_repair_guards():
    _make_aligned()
    assert client.post("/api/互证/9999/修补").status_code == 404
    clean = client.post("/api/互证").json()
    assert clean["status"] == "一致"
    assert _repair(clean["run_id"]).status_code == 400
    assert client.get("/api/修补流水").json() == []


def test_concurrent_same_receipt_single_closure():
    _make_drift()
    run = client.post("/api/互证").json()
    codes = []

    def hit():
        codes.append(TestClient(app).post(f"/api/互证/{run['run_id']}/修补").status_code)

    ts = [threading.Thread(target=hit) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [200, 409]
    assert len(client.get("/api/修补流水").json()) == 1


def test_concurrent_repairs_no_double_delete():
    with db() as c:
        pid = _partition(c)
        _clip(c, pid, "reel/测/ok.bin", 100)
        _inv(c, "reel/测/ok.bin", 100)
        for i in range(5):
            _inv(c, f"reel/测/orphan-{i}.bin", 10 + i)
    r1 = client.post("/api/互证").json()
    r2 = client.post("/api/互证").json()
    assert r1["drift_count"] == r2["drift_count"] == 5

    results = {}

    def hit(tag, run_id):
        resp = TestClient(app).post(f"/api/互证/{run_id}/修补")
        results[tag] = resp.status_code

    ts = [threading.Thread(target=hit, args=("a", r1["run_id"])),
          threading.Thread(target=hit, args=("b", r2["run_id"]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(results.values()) == {200}

    # 两份回执各自闭合，但孤儿键全局只被删一次：并发修补不得两边各删各的
    repairs = client.get("/api/修补流水").json()
    assert len(repairs) == 2
    deleted = [f["对象键"] for r in repairs for f in r["fixes"] if f["动作"] == "删除湖对象行"]
    assert sorted(deleted) == sorted(f"reel/测/orphan-{i}.bin" for i in range(5))
    assert len(deleted) == len(set(deleted))

    meta = {x["object_key"]: x["bytes"] for x in client.get("/api/素材段清单").json()}
    inv = {x["object_key"]: x["bytes"] for x in client.get("/api/目录清单").json()}
    assert inv == meta
    assert client.post("/api/互证").json()["status"] == "一致"


def test_repair_atomic_rollback_on_failure(monkeypatch):
    _make_drift()
    run = client.post("/api/互证").json()
    before_inv = client.get("/api/目录清单").json()

    real = verify_svc.current_drifts
    calls = {"n": 0}

    def fake(conn):
        calls["n"] += 1
        if calls["n"] >= 2:  # 修后复证注入残留漂移，迫使整体失败
            return ([{"对象键": "ghost", "问题": "目录有、元数据无", "元数据字节": None, "目录字节": 1}], {}, {})
        return real(conn)

    monkeypatch.setattr(verify_svc, "current_drifts", fake)
    r = _repair(run["run_id"])
    assert r.status_code == 500

    # 同成败：湖对象行未动、无修补流水、无复证回执、回执未闭合
    assert client.get("/api/目录清单").json() == before_inv
    assert client.get("/api/修补流水").json() == []
    runs = client.get("/api/互证流水").json()
    assert [x["id"] for x in runs] == [run["run_id"]]
    assert runs[0]["repair_id"] is None

    # 回滚干净后同一回执可重试并闭合
    monkeypatch.undo()
    assert _repair(run["run_id"]).status_code == 200
    assert client.post("/api/互证").json()["status"] == "一致"
