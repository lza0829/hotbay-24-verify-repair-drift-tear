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


def test_repair_success_but_ledger_drift_must_not_show_aligned(monkeypatch):
    """夹具：修补绿仍漂移——账页写了行、灯绿，但两侧键账仍漂移，必须收口为未对齐。"""
    _make_drift()
    run = client.post("/api/互证").json()
    assert _repair(run["run_id"]).status_code == 200
    assert client.get("/api/互证状态").json()["aligned"] is True

    # 修补账页原样不动，但两侧键账事后又被拉开（旁路只改漂移数不在此列，这里真删键）
    with db() as c:
        c.execute("DELETE FROM inventory_objects WHERE object_key='reel/测/size.bin'")

    state = client.get("/api/互证状态").json()
    assert state["drift_count"] == 1
    assert state["aligned"] is False
    assert state["repair_evidence"] is False

    ov = client.get("/api/总览").json()
    assert ov["已对齐"] is False
    assert ov["当前漂移"] == 1
    assert ov["最近修补"]["aligned"] is False

    # 账页有行但证据不过：修补流水里这条不得标对齐
    repairs = client.get("/api/修补流水").json()
    assert repairs[0]["aligned"] is False


def test_one_side_deleted_other_untouched_is_repaired_from_clips():
    """夹具：一侧删一侧留——湖侧删、段册留，修补必须补回且段册不动；反向则删湖侧。"""
    with db() as c:
        pid = _partition(c)
        _clip(c, pid, "reel/测/kept.bin", 100)
        _inv(c, "reel/测/kept.bin", 100)
    clips_before = client.get("/api/素材段清单").json()

    with db() as c:
        c.execute("DELETE FROM inventory_objects WHERE object_key='reel/测/kept.bin'")
    run = client.post("/api/互证").json()
    assert run["drift_count"] == 1
    assert _repair(run["run_id"]).status_code == 200
    assert {x["object_key"]: x["bytes"] for x in client.get("/api/目录清单").json()} == {"reel/测/kept.bin": 100}
    assert client.get("/api/素材段清单").json() == clips_before

    with db() as c:
        c.execute("UPDATE inventory_objects SET bytes=777 WHERE object_key='reel/测/kept.bin'")  # 湖侧异形，段册仍 100
    run2 = client.post("/api/互证").json()
    assert run2["drift_count"] == 1
    assert _repair(run2["run_id"]).status_code == 200
    assert {x["object_key"]: x["bytes"] for x in client.get("/api/目录清单").json()} == {"reel/测/kept.bin": 100}
    assert client.get("/api/素材段清单").json() == clips_before


def test_ledger_row_without_key_change_still_reports_unaligned():
    """夹具：账页有行键账未变——修补流水存在但两侧键账未被这笔修补真正动过，证据复核不过。"""
    _make_drift()
    run = client.post("/api/互证").json()
    body = _repair(run["run_id"]).json()
    assert body["drift_count"] == 0

    # 伪造一笔「有行但没改键账」的修补账页：它挂在一张新的漂移回执上，但 fixes 指向从未存在的键
    fake_run = client.post("/api/互证").json()
    with db() as c:
        c.execute(
            "INSERT INTO repair_runs(verify_run_id, status, fixed_count, detail, created_at)"
            " VALUES (?,?,?,?,?)",
            (fake_run["run_id"], "已修补", 1,
             '[{"对象键": "reel/测/ghost.bin", "问题": "字节不一致", "动作": "对齐字节", "前字节": 1, "后字节": 2}]',
             "2026-11-23T00:00:00"),
        )
    repairs = client.get("/api/修补流水").json()
    fake = [r for r in repairs if r["verify_run_id"] == fake_run["run_id"]][0]
    assert fake["aligned"] is False

    state = client.get("/api/互证状态").json()
    assert state["aligned"] is False and state["repair_evidence"] is False
    assert client.get("/api/总览").json()["已对齐"] is False


def test_surfaces_share_one_recomputed_truth():
    """修补 API、漂移条、段册表、湖侧键行、修补账页、总览计数条必须同刷。"""
    _make_drift()
    run = client.post("/api/互证").json()
    assert client.get("/api/总览").json()["当前漂移"] == 3
    assert client.get("/api/互证状态").json()["drift_count"] == 3
    body = _repair(run["run_id"]).json()

    ov = client.get("/api/总览").json()
    st = client.get("/api/互证状态").json()
    reps = client.get("/api/修补流水").json()
    assert ov["当前漂移"] == st["drift_count"] == 0
    assert ov["已对齐"] is True and st["aligned"] is True
    assert ov["最近修补"]["id"] == body["repair_id"] == reps[0]["id"]
    assert ov["最近修补"]["aligned"] is True and reps[0]["aligned"] is True
    assert reps[0]["verify_run_id"] == run["run_id"]

    # 段册表与湖侧键行逐键相等；分区册逐分区漂移段数归零
    meta = {x["object_key"]: x["bytes"] for x in client.get("/api/素材段清单").json()}
    inv = {x["object_key"]: x["bytes"] for x in client.get("/api/目录清单").json()}
    assert meta == inv
    assert all(p["漂移段数"] == 0 for p in client.get("/api/分区").json())


def test_overview_and_state_do_not_trust_receipt_snapshot(monkeypatch):
    """禁止只改漂移数字：回执写着一律一致也不能点亮对齐灯，必须实时重算。"""
    _make_drift()
    run = client.post("/api/互证").json()
    with db() as c:  # 旁路：把所有回执伪造成一致
        c.execute("UPDATE verify_runs SET status='一致', drift_count=0, detail='[]'")

    ov = client.get("/api/总览").json()
    st = client.get("/api/互证状态").json()
    assert ov["当前漂移"] == 3
    assert ov["最近互证"]["status"] == "一致"      # 快照确实被改了
    assert ov["已对齐"] is False and st["aligned"] is False  # 但灯不亮
    assert client.get("/api/分区").json()[0]["漂移段数"] > 0
    # 仍能修补闭合（以素材段为准，不依赖被篡改的快照数值）
    assert _repair(run["run_id"]).status_code == 200
    assert client.get("/api/互证状态").json()["aligned"] is True
