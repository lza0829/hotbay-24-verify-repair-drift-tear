from __future__ import annotations
from fastapi import APIRouter
from app.services import verify as svc

router = APIRouter(tags=["互证"])


@router.get("/目录清单")
def inventory():
    return svc.inventory_list()


@router.get("/素材段清单")
def clips():
    return svc.clips_ledger()


@router.get("/互证流水")
def runs():
    return svc.list_verify_runs()


@router.get("/修补流水")
def repairs():
    return svc.list_repairs()


@router.get("/互证状态")
def state():
    return svc.alignment_state()


@router.post("/互证")
def run():
    return svc.run_verify()


@router.post("/互证/{run_id}/修补")
def repair(run_id: int):
    return svc.repair_drift(run_id)
