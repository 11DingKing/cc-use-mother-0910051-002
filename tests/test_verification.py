"""跨校服务核验流程测试。

覆盖：接收时固定证据摘要、疑似重复自动立案、申诉冻结积分/星级/统计、
双方补交证据、复核人权限、确认/拆分/驳回结案、可追溯调整台账，
以及重复申诉、结案后更正、复核期间新服务等幂等场景。
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from main import app
from database import engine

# 基线测试的子进程会删除并重建 redscarf.db；
# 释放模块导入时建立的池化连接，确保后续用例在新库文件上打开连接。
engine.dispose()

client = TestClient(app)

REVIEWER = {"reviewer_name": "团委王老师", "reviewer_role": "团委复核人"}


def make_school():
    name = f"核验测试学校-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/schools", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def make_volunteer(school_id):
    name = f"核验测试员-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/volunteers", json={"name": name, "school_id": school_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def make_record(volunteer_id, hours, activity=None, school_id=None, rating=None,
                service_date="2026-09-20"):
    payload = {
        "volunteer_id": volunteer_id,
        "service_date": service_date,
        "service_hours": hours,
        "audience_count": 20,
    }
    if activity:
        payload["activity_name"] = activity
    if school_id:
        payload["reporting_school_id"] = school_id
    if rating:
        payload["teacher_rating"] = rating
    r = client.post("/api/service-records/", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def get_volunteer(volunteer_id):
    r = client.get(f"/api/volunteers/{volunteer_id}")
    assert r.status_code == 200, r.text
    return r.json()


def get_balance(volunteer_id):
    r = client.get(f"/api/points/volunteer/{volunteer_id}")
    assert r.status_code == 200, r.text
    return r.json()["points_balance"]


def get_hours(volunteer_id):
    return get_volunteer(volunteer_id)["total_service_hours"]


def appeal(record_id, reason="两校重复上报同一段服务", school_id=None):
    payload = {"reason": reason, "raised_by": "学校教务处", "is_duplicate_suspected": True}
    if school_id:
        payload["raised_school_id"] = school_id
    return client.post(f"/api/service-records/{record_id}/disputes", json=payload)


def resolve(dispute_id, decision, adjusted_hours=None):
    payload = {"decision": decision, **REVIEWER}
    if adjusted_hours is not None:
        payload["adjusted_hours"] = adjusted_hours
    return client.post(f"/api/disputes/{dispute_id}/resolve", json=payload)


def overview_hours():
    r = client.get("/api/stats/overview")
    assert r.status_code == 200
    return r.json()["total_service_hours"]


def test_evidence_digest_fixed_on_intake():
    """接收记录时固定活动、时段、人员、上报学校的证据摘要，且不随后续修改变化。"""
    school_id = make_school()
    vid = make_volunteer(school_id)

    rec = make_record(vid, 2.0, activity="市级讲解活动", school_id=school_id)
    assert rec["verification_status"] == "有效"
    assert rec["evidence_digest"] and len(rec["evidence_digest"]) == 64
    assert "市级讲解活动" in rec["evidence_summary"]
    assert str(school_id) in rec["evidence_summary"]
    digest_at_intake = rec["evidence_digest"]

    # 修改记录后，接收时固定的证据摘要不变
    r = client.put(f"/api/service-records/{rec['id']}", json={
        "volunteer_id": vid, "service_date": "2026-09-20", "service_hours": 3.0
    })
    assert r.status_code == 200, r.text
    assert r.json()["evidence_digest"] == digest_at_intake


def test_duplicate_report_auto_disputed():
    """同一讲解员在原学校和借调学校重复上报同一段服务：第二条自动进入争议且不发放积分。"""
    school_a, school_b = make_school(), make_school()
    vid = make_volunteer(school_a)

    rec1 = make_record(vid, 2.0, activity="市级巡展讲解", school_id=school_a)
    assert rec1["verification_status"] == "有效"
    assert get_balance(vid) == 20
    assert get_hours(vid) == 2.0

    rec2 = make_record(vid, 2.0, activity="市级巡展讲解", school_id=school_b)
    assert rec2["verification_status"] == "争议中"
    assert rec2["points_awarded"] == 0
    # 疑似重复的记录不发放积分、不计入时长
    assert get_balance(vid) == 20
    assert get_hours(vid) == 2.0

    r = client.get(f"/api/service-records/{rec2['id']}/disputes")
    assert r.status_code == 200
    disputes = r.json()
    assert len(disputes) == 1
    assert disputes[0]["is_duplicate_suspected"] is True
    assert disputes[0]["status"] == "待复核"
    assert disputes[0]["freeze_points"] == 0


def test_appeal_freezes_points_hours_and_stats():
    """申诉立案后，记录的积分、时长与统计贡献立即冻结。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 8.0, activity="周末驻馆讲解", school_id=school_id)
    assert get_balance(vid) == 80
    assert get_hours(vid) == 8.0
    overview_before = overview_hours()

    r = appeal(rec["id"], school_id=school_id)
    assert r.status_code == 200, r.text
    dispute = r.json()
    assert dispute["status"] == "待复核"
    assert dispute["freeze_points"] == 80
    assert dispute["freeze_hours"] == 8.0

    assert get_balance(vid) == 0
    assert get_hours(vid) == 0.0
    assert overview_hours() == overview_before - 8.0

    # 冻结动作留有可追溯的积分流水与调整台账
    r = client.get(f"/api/points/volunteer/{vid}/records")
    sources = [p["source"] for p in r.json()]
    assert "争议冻结" in sources
    r = client.get(f"/api/disputes/{dispute['id']}")
    freezes = [a for a in r.json()["adjustments"] if a["adjustment_type"] == "争议冻结"]
    assert len(freezes) == 1
    assert freezes[0]["points_delta"] == -80
    assert freezes[0]["points_balance_after"] == 0


def test_repeat_appeal_no_double_freeze():
    """重复申诉不会重复扣回：争议中的记录再次申诉返回409，余额只扣一次。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 5.0, activity="重复申诉场景", school_id=school_id)
    assert get_balance(vid) == 50

    r1 = appeal(rec["id"])
    assert r1.status_code == 200
    assert get_balance(vid) == 0

    r2 = appeal(rec["id"])
    assert r2.status_code == 409
    assert get_balance(vid) == 0

    # 积分流水中只有一条争议冻结记录
    r = client.get(f"/api/points/volunteer/{vid}/records")
    freezes = [p for p in r.json() if p["source"] == "争议冻结"]
    assert len(freezes) == 1


def test_evidence_submission_by_both_schools():
    """争议双方学校均可补交证据；结案后不能再补交。"""
    school_a, school_b = make_school(), make_school()
    vid = make_volunteer(school_a)
    rec = make_record(vid, 2.0, activity="证据补交场景", school_id=school_a)
    dispute = appeal(rec["id"]).json()

    r = client.post(f"/api/disputes/{dispute['id']}/evidences", json={
        "school_id": school_a, "submitted_by": "原学校教务", "content": "签到表与现场照片"
    })
    assert r.status_code == 200, r.text
    r = client.post(f"/api/disputes/{dispute['id']}/evidences", json={
        "school_id": school_b, "submitted_by": "借调学校教务", "content": "借调函与考勤记录"
    })
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/disputes/{dispute['id']}").json()
    assert len(detail["evidences"]) == 2
    assert {e["school_id"] for e in detail["evidences"]} == {school_a, school_b}

    resolve(dispute["id"], "确认有效")
    r = client.post(f"/api/disputes/{dispute['id']}/evidences", json={
        "school_id": school_a, "submitted_by": "原学校教务", "content": "补充材料"
    })
    assert r.status_code == 409


def test_resolve_requires_privileged_reviewer():
    """只有团委/市级复核人可以作出复核结论。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 2.0, activity="权限校验场景", school_id=school_id)
    dispute = appeal(rec["id"]).json()

    r = client.post(f"/api/disputes/{dispute['id']}/resolve", json={
        "decision": "确认有效", "reviewer_name": "学校老师", "reviewer_role": "学校管理员"
    })
    assert r.status_code == 403

    # 无权限的尝试不结案，有权限的复核人仍可正常结案
    r = resolve(dispute["id"], "确认有效")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "已结案"
    assert r.json()["reviewer_name"] == "团委王老师"


def test_confirm_restores_all_derived_results():
    """确认有效后，积分、时长、星级全部恢复，且台账可追溯。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    make_record(vid, 8.0, activity="日常讲解", school_id=school_id, service_date="2026-09-10")
    rec = make_record(vid, 4.0, activity="市级活动", school_id=school_id, service_date="2026-09-11")
    assert get_hours(vid) == 12.0
    assert get_volunteer(vid)["star_level_id"] is not None  # 一星级

    dispute = appeal(rec["id"]).json()
    assert get_balance(vid) == 80
    assert get_hours(vid) == 8.0
    assert get_volunteer(vid)["star_level_id"] is None  # 跌破10小时，星级冻结消失

    r = resolve(dispute["id"], "确认有效")
    assert r.status_code == 200, r.text
    assert get_balance(vid) == 120
    assert get_hours(vid) == 12.0
    assert get_volunteer(vid)["star_level_id"] is not None

    detail = client.get(f"/api/disputes/{dispute['id']}").json()
    types = [a["adjustment_type"] for a in detail["adjustments"]]
    assert types == ["争议冻结", "解冻恢复"]
    restore = detail["adjustments"][1]
    assert restore["points_delta"] == 40
    assert restore["points_balance_after"] == 120
    assert restore["total_hours_after"] == 12.0

    # 结案后不能重复结案（防止重复恢复）
    r = resolve(dispute["id"], "确认有效")
    assert r.status_code == 409
    assert get_balance(vid) == 120


def test_reject_deducts_exactly_once():
    """驳回后积分永久扣回且只扣一次；复查再驳回不会二次扣回。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 5.0, activity="驳回场景", school_id=school_id)
    assert get_balance(vid) == 50

    dispute = appeal(rec["id"]).json()
    r = resolve(dispute["id"], "驳回记录")
    assert r.status_code == 200, r.text
    assert get_balance(vid) == 0
    assert get_hours(vid) == 0.0
    assert client.get(f"/api/service-records/{rec['id']}").json()["verification_status"] == "已驳回"

    # 结案后复查：允许再次申诉，但冻结额为0，再驳回不会重复扣回
    r = appeal(rec["id"], reason="复查申请")
    assert r.status_code == 200, r.text
    dispute2 = r.json()
    assert dispute2["freeze_points"] == 0
    r = resolve(dispute2["id"], "驳回记录")
    assert r.status_code == 200, r.text
    assert get_balance(vid) == 0
    assert get_hours(vid) == 0.0


def test_post_closure_correction_restores_once():
    """结案后更正：驳回的记录经复查确认有效，积分按记录补发且只补发一次。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 5.0, activity="更正场景", school_id=school_id)

    dispute = appeal(rec["id"]).json()
    resolve(dispute["id"], "驳回记录")
    assert get_balance(vid) == 0

    dispute2 = appeal(rec["id"], reason="新证据表明服务真实").json()
    r = resolve(dispute2["id"], "确认有效")
    assert r.status_code == 200, r.text
    assert get_balance(vid) == 50
    assert get_hours(vid) == 5.0
    assert client.get(f"/api/service-records/{rec['id']}").json()["verification_status"] == "有效"

    detail = client.get(f"/api/disputes/{dispute2['id']}").json()
    grants = [a for a in detail["adjustments"] if a["adjustment_type"] == "确认发放"]
    assert len(grants) == 1
    assert grants[0]["points_delta"] == 50


def test_split_adjusts_partial_hours():
    """拆分确认：先恢复冻结额，再按核定时长重新发放，差额可追溯。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 4.0, activity="跨校联合讲解", school_id=school_id, rating=5)
    assert get_balance(vid) == 50  # 40基础 + 10好评

    dispute = appeal(rec["id"]).json()
    assert get_balance(vid) == 0

    r = resolve(dispute["id"], "拆分确认", adjusted_hours=2.0)
    assert r.status_code == 200, r.text
    # 冻结已使记录贡献归零，结案按核定2小时发放30（20基础+10好评），差额永久扣回
    assert get_balance(vid) == 30
    assert get_hours(vid) == 2.0

    rec_after = client.get(f"/api/service-records/{rec['id']}").json()
    assert rec_after["service_hours"] == 2.0
    assert rec_after["points_awarded"] == 30
    assert rec_after["verification_status"] == "有效"

    detail = client.get(f"/api/disputes/{dispute['id']}").json()
    assert detail["decision"] == "拆分确认"
    assert detail["adjusted_hours"] == 2.0
    split = [a for a in detail["adjustments"] if a["adjustment_type"] == "拆分调整"][0]
    assert split["points_delta"] == 30
    assert split["points_balance_after"] == 30

    # 核定时长超出原时长不允许
    rec2 = make_record(vid, 3.0, activity="拆分校验", school_id=school_id, service_date="2026-09-22")
    dispute2 = appeal(rec2["id"]).json()
    r = resolve(dispute2["id"], "拆分确认", adjusted_hours=5.0)
    assert r.status_code == 400


def test_new_service_during_dispute_unaffected():
    """复核期间的新服务正常累计，不受争议结案影响，也不会被重复扣回。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec_a = make_record(vid, 3.0, activity="争议中的服务", school_id=school_id,
                        service_date="2026-09-15")
    dispute = appeal(rec_a["id"]).json()
    assert get_balance(vid) == 0

    # 复核期间产生的新服务正常发放
    rec_b = make_record(vid, 2.0, activity="复核期间的新服务", school_id=school_id,
                        service_date="2026-09-18")
    assert rec_b["verification_status"] == "有效"
    assert get_balance(vid) == 20
    assert get_hours(vid) == 2.0

    # 旧记录被驳回，只影响旧记录自身
    resolve(dispute["id"], "驳回记录")
    assert get_balance(vid) == 20
    assert get_hours(vid) == 2.0


def test_disputed_record_readonly_and_delete_rules():
    """争议中的记录禁止修改和删除；有争议历史的记录禁止删除以保证可追溯。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 5.0, activity="只读场景", school_id=school_id)
    dispute = appeal(rec["id"]).json()

    r = client.put(f"/api/service-records/{rec['id']}", json={
        "volunteer_id": vid, "service_date": "2026-09-20", "service_hours": 9.0
    })
    assert r.status_code == 409
    r = client.delete(f"/api/service-records/{rec['id']}")
    assert r.status_code == 409

    resolve(dispute["id"], "驳回记录")
    assert get_balance(vid) == 0
    # 已驳回且带有争议历史的记录保留在案，删除被拒绝且不会再扣积分
    r = client.delete(f"/api/service-records/{rec['id']}")
    assert r.status_code == 409
    assert get_balance(vid) == 0

    # 无争议历史的有效记录仍可正常删除，积分按发放额扣回一次
    rec2 = make_record(vid, 2.0, activity="正常删除场景", school_id=school_id,
                       service_date="2026-09-21")
    assert get_balance(vid) == 20
    r = client.delete(f"/api/service-records/{rec2['id']}")
    assert r.status_code == 200
    assert get_balance(vid) == 0


def test_stats_exclude_disputed_and_restore_after_confirm():
    """市级汇总统计在冻结期间剔除争议记录，确认有效后恢复。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    before = overview_hours()

    rec = make_record(vid, 6.0, activity="统计口径场景", school_id=school_id)
    assert overview_hours() == before + 6.0

    dispute = appeal(rec["id"]).json()
    assert overview_hours() == before

    resolve(dispute["id"], "确认有效")
    assert overview_hours() == before + 6.0

    # 月度统计同样遵循有效口径
    r = client.get("/api/stats/monthly", params={"year": 2026})
    assert r.status_code == 200
    sept = [m for m in r.json() if m["month"] == 9][0]
    assert sept["total_service_hours"] >= 6.0


def test_list_filters_by_status():
    """服务记录与争议列表支持按状态筛选（中文值或枚举名均可）。"""
    school_id = make_school()
    vid = make_volunteer(school_id)
    rec = make_record(vid, 2.0, activity="筛选场景", school_id=school_id)
    appeal(rec["id"])

    r = client.get("/api/service-records/", params={"volunteer_id": vid, "verification_status": "争议中"})
    assert r.status_code == 200
    assert len(r.json()) == 1 and r.json()[0]["id"] == rec["id"]

    r = client.get("/api/service-records/", params={"volunteer_id": vid, "verification_status": "DISPUTED"})
    assert len(r.json()) == 1

    r = client.get("/api/disputes/", params={"status": "待复核", "volunteer_id": vid})
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["service_record_id"] == rec["id"]
