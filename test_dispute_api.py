"""跨校服务核验流程 API 验收脚本（由 pytest 以子进程方式运行）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

if os.path.exists("redscarf.db"):
    os.remove("redscarf.db")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
print("=" * 60)
print("跨校服务核验流程 API 验收")
print("=" * 60)

schools = client.get("/api/schools").json()
school_a, school_b = schools[0]["id"], schools[1]["id"]
print(f"\n[准备] 原学校={schools[0]['name']}#{school_a} 借调学校={schools[1]['name']}#{school_b}")


def new_volunteer(name):
    r = client.post("/api/volunteers", json={
        "name": name, "school_id": school_a, "grade": "五年级",
        "parent_name": "家长", "parent_phone": f"139{school_a}{name[-1:]}000",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def report(vid, school_id, tr, activity="市级讲解活动", day="2026-10-02", hours=2.0):
    r = client.post("/api/service-records/", json={
        "volunteer_id": vid, "service_date": day, "service_hours": hours,
        "school_id": school_id, "activity_name": activity, "time_range": tr,
        "audience_count": 30, "teacher_name": "带队老师",
    })
    assert r.status_code == 200, r.text
    return r.json()


def balance(vid):
    return client.get(f"/api/points/volunteer/{vid}").json()["points_balance"]


def hours(vid):
    return client.get(f"/api/volunteers/{vid}/service-hours").json()["total_service_hours"]


# ---------------------------------------------------------------- 1. 双校重复上报
print("\n[1] 原学校与借调学校就同一段服务各自上报...")
vid = new_volunteer("核验学生甲")
r1 = report(vid, school_a, "09:00-11:00")
assert r1["status"] == "正常", r1
assert r1["points_awarded"] == 20
assert r1["evidence_fingerprint"], "接收时必须固化指纹"
assert balance(vid) == 20

r2 = report(vid, school_b, "09:30-11:30")  # 时段重叠、不同学校
assert r2["status"] == "争议中", f"第二条应触发自动立案冻结: {r2}"
r1 = client.get(f"/api/service-records/{r1['id']}").json()
assert r1["status"] == "争议中"
print(f"  ✅ 记录#{r1['id']}(原校)、#{r2['id']}(借调校) 自动进入争议并冻结积分")

# ---------------------------------------------------------------- 2. 冻结即生效
print("\n[2] 冻结的积分、时长、统计贡献立即移除...")
assert balance(vid) == 0, f"双方积分均应冻结，余额={balance(vid)}"
assert hours(vid) == 0.0, "争议记录不计服务时长"

dsps = client.get("/api/disputes/").json()
assert len(dsps) == 1
dsp = client.get(f"/api/disputes/{dsps[0]['id']}").json()
assert dsp["status"] == "待受理"
linked = sorted(l["service_record_id"] for l in dsp["links"])
assert linked == sorted([r1["id"], r2["id"]])
sys_ev = [e for e in dsp["evidences"] if e["submitter_type"] == "系统"]
assert len(sys_ev) == 2 and all("固定证据" in e["content"] for e in sys_ev), "接收证据必须固化"
print(f"  ✅ 争议单{dsp['dispute_no']} 余额=0 时长=0，系统证据摘要 {len(sys_ev)} 条")

# 争议记录禁止编辑/删除
assert client.put(f"/api/service-records/{r1['id']}", json={
    "volunteer_id": vid, "service_date": "2026-10-02", "service_hours": 3,
    "school_id": school_a, "time_range": "09:00-12:00",
}).status_code == 400
assert client.delete(f"/api/service-records/{r1['id']}").status_code == 400
print("  ✅ 复核期间禁止修改/删除争议记录")

# 查重接口
dup = client.get(f"/api/disputes/records/{r2['id']}/duplicates").json()
assert dup["is_duplicate"] and r1["id"] in dup["duplicate_record_ids"]

# ---------------------------------------------------------------- 3. 双方补证 + 受理
print("\n[3] 双方补交证据，团委受理...")
for rid, sid, who in [(r1["id"], school_a, "原校德育主任"), (r2["id"], school_b, "借调负责老师")]:
    re = client.post(f"/api/disputes/{dsp['id']}/evidence?service_record_id={rid}", json={
        "content": f"{who}提交签到表与现场照片", "submitter_school_id": sid, "submitter_name": who,
    })
    assert re.status_code == 201, re.text
ra = client.post(f"/api/disputes/{dsp['id']}/accept", json={"reviewer": "市团委李老师"})
assert ra.status_code == 200 and ra.json()["status"] == "复核中"
print("  ✅ 证据已收录，争议进入复核中")

# ---------------------------------------------------------------- 4. 复核期间新服务挂接
print("\n[4] 复核期间学校又补报同段服务，挂入原争议单而非另立...")
r3 = report(vid, school_b, "10:00-10:30", day="2026-10-02")
assert r3["status"] == "争议中"
dsp = client.get(f"/api/disputes/{dsp['id']}").json()
late = [l for l in dsp["links"] if l["service_record_id"] == r3["id"]]
assert late and late[0]["role"] == "late"
assert len(client.get("/api/disputes/").json()) == 1, "挂接不应产生新争议单"
assert balance(vid) == 0
print(f"  ✅ 新记录#{r3['id']} 已挂入争议单并冻结，争议单仍为 1 张")

# ---------------------------------------------------------------- 5. 拆分裁决
print("\n[5] 复核人拆分：原学校认可2小时，其余两条驳回...")
rd = client.post(f"/api/disputes/{dsp['id']}/decision", json={
    "decision": "拆分", "reviewer": "市团委李老师",
    "comment": "同一时段只服务一次，时长归原学校",
    "split_hours": {str(r1["id"]): 2, str(r2["id"]): 0, str(r3["id"]): 0},
})
assert rd.status_code == 200, rd.text
dsp = rd.json()
assert dsp["status"] == "已结案" and dsp["decision"] == "拆分"
children = client.get(f"/api/service-records/{r1['id']}").json()["child_records"]
assert len(children) == 1 and children[0]["service_hours"] == 2.0 and children[0]["status"] == "正常"
r2_after = client.get(f"/api/service-records/{r2['id']}").json()
r3_after = client.get(f"/api/service-records/{r3['id']}").json()
assert r2_after["status"] == "已驳回" and r3_after["status"] == "已驳回"
assert balance(vid) == 20, f"一段服务只能得一次积分，余额={balance(vid)}"
assert hours(vid) == 2.0
# 统计口径：原记录已拆分不计入，拆分子记录(正常)的20分计入；冻结红冲为管控流水不计入
ps = client.get(f"/api/points/volunteer/{vid}").json()
assert ps["total_earned"] == 20 and ps["total_spent"] == 0, ps
print(f"  ✅ 拆分后余额=20 时长=2.0；正常累计获得={ps['total_earned']} 正常消耗={ps['total_spent']}")

# ---------------------------------------------------------------- 6. 重复申诉不重复扣回
print("\n[6] 确认结论后重复申诉两轮，积分只做一次翻转，不重复扣回...")
vid2 = new_volunteer("核验学生乙")
s = report(vid2, school_a, "14:00-16:00", day="2026-10-03")
assert balance(vid2) == 20
for cycle in (1, 2):
    d = client.post("/api/disputes/", json={
        "service_record_ids": [s["id"]], "reason": f"第{cycle}轮重复申诉",
        "initiator_school_id": school_b, "created_by": "借调老师",
    })
    assert d.status_code == 201, d.text
    assert balance(vid2) == 0, f"第{cycle}轮申诉后应冻结至0: {balance(vid2)}"
    did = d.json()["id"]
    rc = client.post(f"/api/disputes/{did}/decision",
                     json={"decision": "确认有效", "reviewer": "市团委李老师"})
    assert rc.status_code == 200
    assert balance(vid2) == 20, f"第{cycle}轮确认后应恢复为20: {balance(vid2)}"
    s = client.get(f"/api/service-records/{s['id']}").json()
    assert s["status"] == "正常"
assert hours(vid2) == 2.0
print("  ✅ 两轮申诉：余额 20→0→20→0→20，无重复扣回，时长保持2小时")

# 未结案争议不能就同一记录再申诉
d_open = client.post("/api/disputes/", json={
    "service_record_ids": [s["id"]], "reason": "阻塞验证", "initiator_school_id": school_b,
})
assert d_open.status_code == 201
d_again = client.post("/api/disputes/", json={
    "service_record_ids": [s["id"]], "reason": "重复立案应被拒绝", "initiator_school_id": school_a,
})
assert d_again.status_code == 400
print("  ✅ 同一记录在未结案争议中不能重复立案")

# ---------------------------------------------------------------- 7. 结案后更正
print("\n[7] 结案后更正：驳回改确认、再改驳回，台账幂等无重复扣回...")
did = d_open.json()["id"]
client.post(f"/api/disputes/{did}/decision",
            json={"decision": "驳回", "reviewer": "市团委李老师", "comment": "初判无效"})
assert balance(vid2) == 0 and hours(vid2) == 0.0
cc = client.post(f"/api/disputes/{did}/correct",
                 json={"decision": "确认有效", "reviewer": "市团委王老师", "comment": "补交证据后改判"})
assert cc.status_code == 200, cc.text
assert balance(vid2) == 20 and hours(vid2) == 2.0
cc2 = client.post(f"/api/disputes/{did}/correct",
                  json={"decision": "驳回", "reviewer": "市团委王老师", "comment": "再次核查仍无效"})
assert cc2.status_code == 200
assert balance(vid2) == 0 and hours(vid2) == 0.0
adjs = client.get(f"/api/disputes/{did}/adjustments").json()
keys = [a["adjustment_key"] for a in adjs]
assert len(keys) == len(set(keys)), "调整台账键必须唯一（幂等）"
print(f"  ✅ 两次更正余额 0→20→0，台账 {len(adjs)} 条调整全部幂等可追溯")

# ---------------------------------------------------------------- 8. 全局统计不失真
print("\n[8] 全局统计口径：争议/驳回记录不贡献时长，管控流水不进积分统计...")
ov = client.get("/api/stats/overview").json()
# 学生全部有效时长：甲2h + 乙0h（最终驳回）= 2h 来自本次新增
assert ov["total_service_hours"] >= 2.0
pt = client.get("/api/stats/points").json()
assert isinstance(pt["total_points_earned"], int)
print(f"  ✅ 总服务时长={ov['total_service_hours']}h；积分统计净额={pt['net_points']}（管控流水已排除）")

print("\n" + "=" * 60)
print("🎉 跨校服务核验流程全部验收通过！")
print("=" * 60)
