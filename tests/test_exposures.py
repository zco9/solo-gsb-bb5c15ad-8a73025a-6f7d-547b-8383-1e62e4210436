"""POST /api/v1/exposures 与 GET /api/v1/flags/{flag_key}/exposure-summary 测试。"""

from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import Exposure

BASE = datetime(2026, 9, 17, 8, 0, 0)


def _evaluate(client, user_id, request_id, flag_key="btn_color", context=None):
    payload = {"user_id": user_id, "request_id": request_id, "context": context or {}}
    resp = client.post(f"/api/v1/flags/{flag_key}/evaluate", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _post_exposure(client, exposure_id, request_id):
    return client.post(
        "/api/v1/exposures",
        json={"exposure_id": exposure_id, "request_id": request_id},
    )


def _insert_exposure(
    *,
    exposure_id: str,
    request_id: str = "req-x",
    flag_key: str = "sum_flag",
    user_id: str = "u1",
    config_version: int = 1,
    variant_key: str = "on",
    decided_at: datetime = BASE,
    exposed_at: datetime,
):
    db = SessionLocal()
    try:
        db.add(
            Exposure(
                exposure_id=exposure_id,
                request_id=request_id,
                flag_key=flag_key,
                user_id=user_id,
                config_version=config_version,
                variant_key=variant_key,
                decided_at=decided_at,
                exposed_at=exposed_at,
            )
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/v1/exposures
# ---------------------------------------------------------------------------


def test_create_exposure_freezes_snapshot(client, simple_flag):
    decision = _evaluate(client, "user-1001", "req-100", context={"region": "cn"})
    assert decision["variant_key"] == "red"
    assert decision["config_version"] == 1

    resp = _post_exposure(client, "exp-100", "req-100")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["exposure_id"] == "exp-100"
    assert body["request_id"] == "req-100"
    assert body["flag_key"] == "btn_color"
    assert body["user_id"] == "user-1001"
    assert body["config_version"] == 1
    assert body["variant_key"] == "red"

    # 固化原判定时间：与判定快照的 decided_at 为同一时刻（快照响应为 naive UTC）
    snap = client.get("/api/v1/decisions/req-100").json()
    decided_at = datetime.fromisoformat(body["decided_at"])
    assert decided_at.tzinfo is not None
    assert decided_at.replace(tzinfo=None) == datetime.fromisoformat(snap["decided_at"])
    # 落库时间不早于原判定时间
    assert datetime.fromisoformat(body["exposed_at"]) >= decided_at


def test_exposure_idempotent_retry_returns_first_record(client, simple_flag):
    _evaluate(client, "u1", "req-idem", context={"region": "cn"})
    first = _post_exposure(client, "exp-idem", "req-idem")
    assert first.status_code == 201

    retry = _post_exposure(client, "exp-idem", "req-idem")
    assert retry.status_code == 200
    assert retry.json() == first.json()

    # 重试不产生新记录
    body = client.get("/api/v1/flags/btn_color/exposure-summary").json()
    assert body["total_exposures"] == 1
    assert body["unique_users"] == 1


def test_exposure_id_reused_for_other_snapshot_returns_409(client, simple_flag):
    _evaluate(client, "u1", "req-a")
    _evaluate(client, "u1", "req-b")
    assert _post_exposure(client, "exp-x", "req-a").status_code == 201

    resp = _post_exposure(client, "exp-x", "req-b")
    assert resp.status_code == 409
    assert "exp-x" in resp.json()["detail"]


def test_exposure_unknown_snapshot_returns_404(client):
    resp = _post_exposure(client, "exp-404", "no-such-request")
    assert resp.status_code == 404


def test_exposure_validation_returns_422(client):
    assert client.post("/api/v1/exposures", json={}).status_code == 422
    resp = client.post(
        "/api/v1/exposures", json={"exposure_id": "", "request_id": "r"}
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/exposures", json={"exposure_id": "e", "request_id": ""}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/flags/{flag_key}/exposure-summary
# ---------------------------------------------------------------------------


def test_summary_groups_by_config_version_and_variant(client, simple_flag):
    # v1：u1 / u2 命中 region=cn 规则 -> red
    r1 = _evaluate(client, "u1", "req-1", context={"region": "cn"})
    r2 = _evaluate(client, "u2", "req-2", context={"region": "cn"})
    assert r1["variant_key"] == r2["variant_key"] == "red"

    # 发布 v2：blue 100%，之后所有判定都落 blue
    resp = client.post(
        "/api/v1/flags",
        json={
            "flag_key": "btn_color",
            "variants": [
                {"key": "red", "weight": 0},
                {"key": "blue", "weight": 100},
            ],
            "rules": [],
        },
    )
    assert resp.status_code == 201, resp.text
    r3 = _evaluate(client, "u1", "req-3")
    r4 = _evaluate(client, "u3", "req-4")
    r5 = _evaluate(client, "u1", "req-5")  # 同一用户在新版本下重复曝光
    for r in (r3, r4, r5):
        assert r["variant_key"] == "blue" and r["config_version"] == 2

    for eid, rid in [
        ("e1", "req-1"),
        ("e2", "req-2"),
        ("e3", "req-3"),
        ("e4", "req-4"),
        ("e5", "req-5"),
    ]:
        assert _post_exposure(client, eid, rid).status_code == 201

    body = client.get("/api/v1/flags/btn_color/exposure-summary").json()
    assert body["flag_key"] == "btn_color"
    assert body["exposed_from"] is None and body["exposed_to"] is None
    assert body["total_exposures"] == 5
    # u1 同时出现在 (v1, red) 与 (v2, blue)，跨分组只计一次
    assert body["unique_users"] == 3

    groups = {(g["config_version"], g["variant_key"]): g for g in body["groups"]}
    assert set(groups) == {(1, "red"), (2, "blue")}
    assert groups[(1, "red")]["exposures"] == 2
    assert groups[(1, "red")]["unique_users"] == 2
    # u1 在该分组内曝光两次，去重用户数仍为 2（u1、u3）
    assert groups[(2, "blue")]["exposures"] == 3
    assert groups[(2, "blue")]["unique_users"] == 2


def test_summary_time_filter_with_timezone(client):
    _insert_exposure(exposure_id="x1", user_id="u1", exposed_at=BASE)
    _insert_exposure(
        exposure_id="x2", user_id="u2", exposed_at=BASE + timedelta(hours=1)
    )
    _insert_exposure(
        exposure_id="x3", user_id="u3", exposed_at=BASE + timedelta(hours=2)
    )

    # +08:00 偏移归一为 UTC：16:00+08:00 == 08:00Z，边界含 BASE 与 BASE+1h
    body = client.get(
        "/api/v1/flags/sum_flag/exposure-summary",
        params={
            "exposed_from": "2026-09-17T16:00:00+08:00",
            "exposed_to": "2026-09-17T09:00:00Z",
        },
    ).json()
    assert body["total_exposures"] == 2
    assert body["unique_users"] == 2
    assert len(body["groups"]) == 1
    # 回显统一归一为 UTC
    assert datetime.fromisoformat(body["exposed_from"]) == datetime(
        2026, 9, 17, 8, 0, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(body["exposed_to"]) == datetime(
        2026, 9, 17, 9, 0, tzinfo=timezone.utc
    )

    # 边界含：起止精确等于首末行落库时间时三行全部命中
    body = client.get(
        "/api/v1/flags/sum_flag/exposure-summary",
        params={
            "exposed_from": "2026-09-17T08:00:00Z",
            "exposed_to": "2026-09-17T10:00:00Z",
        },
    ).json()
    assert body["total_exposures"] == 3


def test_summary_rejects_naive_invalid_and_inverted_range(client):
    base = "/api/v1/flags/sum_flag/exposure-summary"
    # 不带时区
    resp = client.get(base, params={"exposed_from": "2026-09-17T08:00:00"})
    assert resp.status_code == 422
    # 非法时间
    resp = client.get(base, params={"exposed_to": "not-a-time"})
    assert resp.status_code == 422
    # 区间倒置
    resp = client.get(
        base,
        params={
            "exposed_from": "2026-09-18T00:00:00Z",
            "exposed_to": "2026-09-17T00:00:00Z",
        },
    )
    assert resp.status_code == 422


def test_summary_empty_for_flag_without_exposures(client):
    body = client.get("/api/v1/flags/__no_such_flag__/exposure-summary").json()
    assert body["flag_key"] == "__no_such_flag__"
    assert body["total_exposures"] == 0
    assert body["unique_users"] == 0
    assert body["groups"] == []


def test_delete_flag_keeps_exposures(client, simple_flag):
    _evaluate(client, "u1", "req-del", context={"region": "cn"})
    assert _post_exposure(client, "exp-del", "req-del").status_code == 201

    assert client.delete("/api/v1/flags/btn_color").status_code == 204

    # 删除开关不删除历史曝光，汇总接口对不存在的开关同样可用
    body = client.get("/api/v1/flags/btn_color/exposure-summary").json()
    assert body["total_exposures"] == 1
    assert body["unique_users"] == 1
    assert body["groups"][0]["config_version"] == 1
    assert body["groups"][0]["variant_key"] == "red"

    # 判定快照同样保留，仍可为同一快照补记曝光（新的 exposure_id）
    resp = _post_exposure(client, "exp-del-2", "req-del")
    assert resp.status_code == 201
    assert resp.json()["flag_key"] == "btn_color"
