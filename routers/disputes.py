"""跨校服务核验流程。

设计要点：
- 接收服务记录时固定活动、时段、人员、上报学校的证据摘要与指纹；
- 疑似重复（系统检测）或被申诉（人工发起）的记录进入争议状态，
  立即以"管控流水"冻结积分，服务时长/星级/统计同步排除；
- 双方可随时补交证据，复核人作出 确认有效 / 拆分 / 驳回 结论；
- 所有积分变动都登记《积分调整台账》并带幂等键，只新增流水、
  永不修改或删除历史流水；重复申诉、结案后更正按上一轮结论做
  差异红冲，不会重复扣回，历史统计口径始终排除管控流水。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date
import hashlib
import re
from typing import List, Optional

from database import get_db
import models, schemas

router = APIRouter(prefix="/api/disputes", tags=["跨校服务核验"])

ACTIVE = models.ServiceRecordStatus.ACTIVE
DISPUTED = models.ServiceRecordStatus.DISPUTED
REJECTED = models.ServiceRecordStatus.REJECTED
SPLIT = models.ServiceRecordStatus.SPLIT


# ---------------------------------------------------------------- 证据固定

def normalize_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())


def build_fingerprint(volunteer_id: int, service_date: date, time_range: Optional[str],
                      school_id: Optional[int], activity_name: Optional[str]) -> str:
    raw = "|".join([
        str(volunteer_id),
        service_date.isoformat(),
        normalize_text(time_range),
        str(school_id or ""),
        normalize_text(activity_name),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_evidence_summary(record: models.ServiceRecord, school_name: Optional[str]) -> str:
    if record.evidence_summary:
        return record.evidence_summary
    slot = record.time_slot
    activity = record.activity_name or (slot.topic if slot else None) or "未命名活动"
    location = (slot.location if slot else None)
    time_range = record.time_range or (
        f"{slot.start_time}-{slot.end_time}" if slot and slot.start_time else "时段未登记"
    )
    parts = [
        f"活动：{activity}",
        f"日期：{record.service_date.isoformat()}",
        f"时段：{time_range}",
        f"讲解员：{record.volunteer_id}",
        f"上报学校：{school_name or school_id_placeholder(record.school_id)}",
    ]
    if location:
        parts.append(f"地点：{location}")
    parts.append(f"服务时长：{record.service_hours}小时")
    return "；".join(parts)


def school_id_placeholder(school_id: Optional[int]) -> str:
    return f"学校ID{school_id}" if school_id else "未关联学校"


def _parse_range(time_range: Optional[str]):
    if not time_range:
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*[-~—–]\s*(\d{1,2}):(\d{2})\s*$", time_range)
    if not m:
        return None
    a, b, c, d = map(int, m.groups())
    return a * 60 + b, c * 60 + d


def ranges_overlap(a: Optional[str], b: Optional[str]) -> bool:
    pa, pb = _parse_range(a), _parse_range(b)
    if not pa or not pb:
        return False
    return pa[0] < pb[1] and pb[0] < pa[1]


def find_duplicate_candidates(db: Session, record: models.ServiceRecord) -> List[int]:
    """同一讲解员、同一天：指纹一致或时段重叠的其他未驳回记录。"""
    candidates = db.query(models.ServiceRecord).filter(
        models.ServiceRecord.volunteer_id == record.volunteer_id,
        models.ServiceRecord.service_date == record.service_date,
        models.ServiceRecord.id != record.id,
        models.ServiceRecord.status.in_([ACTIVE, DISPUTED]),
    ).all()
    dup = []
    for c in candidates:
        if record.evidence_fingerprint and c.evidence_fingerprint == record.evidence_fingerprint:
            dup.append(c.id)
        elif ranges_overlap(record.time_range, c.time_range):
            dup.append(c.id)
    return dup


# ---------------------------------------------------------------- 积分台账

def _ensure_adjustment(db: Session, key: str, dispute_id: int, record_id: int,
                       volunteer_id: int, action: str, amount: int, reason: str,
                       operator: Optional[str]) -> models.PointsAdjustment:
    adj = db.query(models.PointsAdjustment).filter(
        models.PointsAdjustment.adjustment_key == key
    ).first()
    if adj:
        return adj
    adj = models.PointsAdjustment(
        adjustment_key=key,
        dispute_id=dispute_id,
        service_record_id=record_id,
        volunteer_id=volunteer_id,
        action=action,
        amount=amount,
        reason=reason,
        operator=operator,
    )
    db.add(adj)
    db.flush()
    return adj


def _points_entry(db: Session, volunteer_id: int, ptype: models.PointsType, amount: int,
                  source: models.PointsSource, key: str, description: str,
                  service_record_id: int = None, adjustment_id: int = None,
                  control_entry: bool = True) -> models.PointsRecord:
    """带幂等键的管控流水：同键只产生一条，直接作用余额，不受"积分不足"限制。

    control_entry=True 的流水只服务于余额纠正，不计入积分发放/消耗的统计口径；
    拆分补发是讲解员真实应得积分，使用 False 进入正常统计。
    """
    if amount <= 0:
        return None
    existing = db.query(models.PointsRecord).filter(
        models.PointsRecord.idempotency_key == key
    ).first()
    if existing:
        return existing

    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")

    delta = amount if ptype == models.PointsType.EARN else -amount
    volunteer.points_balance = (volunteer.points_balance or 0) + delta

    entry = models.PointsRecord(
        volunteer_id=volunteer_id,
        points_type=ptype,
        points_amount=amount,
        source=source,
        service_record_id=service_record_id,
        idempotency_key=key,
        control_entry=control_entry,
        adjustment_id=adjustment_id,
        description=description,
    )
    db.add(entry)
    db.flush()
    return entry


def _clawback_earned(db: Session, record: models.ServiceRecord, key: str,
                     dispute_id: int, action: str, reason: str,
                     operator: Optional[str], amount: int = None):
    """红冲一条记录已发放的正常积分（用于冻结或撤销拆分产物）。"""
    amount = amount if amount is not None else (record.points_awarded or 0)
    if amount <= 0:
        return None
    adj = _ensure_adjustment(db, key, dispute_id, record.id, record.volunteer_id,
                             action, amount, reason, operator)
    return _points_entry(
        db, record.volunteer_id, models.PointsType.SPEND, amount,
        models.PointsSource.DISPUTE_HOLD, key, reason,
        service_record_id=record.id, adjustment_id=adj.id,
    )


# ---------------------------------------------------------------- 派生结果

def recompute_derivatives(db: Session, volunteer_id: int):
    """按有效（正常状态）服务记录重算时长与星级；降级时旧证书留痕失活。"""
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        return

    total_hours = db.query(func.sum(models.ServiceRecord.service_hours)).filter(
        models.ServiceRecord.volunteer_id == volunteer_id,
        models.ServiceRecord.status == ACTIVE,
    ).scalar() or 0.0
    volunteer.total_service_hours = float(total_hours)

    star_levels = db.query(models.StarLevel).order_by(models.StarLevel.min_hours.desc()).all()
    new_star = None
    for sl in star_levels:
        if total_hours >= sl.min_hours:
            new_star = sl
            break
    new_star_id = new_star.id if new_star else None
    old_star_id = volunteer.star_level_id
    volunteer.star_level_id = new_star_id

    if old_star_id and old_star_id != new_star_id:
        # 冻结/驳回导致时长跌破门槛：仅停用当前时长已无法支撑的证书，
        # 讲解员仍满足的低星历史证书保持有效，全程留痕不删除
        for cert in db.query(models.StarCertificate).filter(
            models.StarCertificate.volunteer_id == volunteer_id,
            models.StarCertificate.is_active == True,  # noqa: E712
        ).all():
            level = db.query(models.StarLevel).filter(
                models.StarLevel.id == cert.star_level_id
            ).first()
            if level and total_hours < level.min_hours:
                cert.is_active = False


def refresh_star_certificates(db: Session, volunteer_id: int):
    """提交后调用：星级恢复/升级时补发证书（原有同级有效证书不重复发）。"""
    from routers.star_certificates import check_and_issue_star_certificate
    check_and_issue_star_certificate(db, volunteer_id)


def _touch_volunteers(db: Session, record_ids: List[int]):
    vids = {r[0] for r in db.query(models.ServiceRecord.volunteer_id).filter(
        models.ServiceRecord.id.in_(record_ids)
    ).all()}
    for vid in vids:
        recompute_derivatives(db, vid)
    return vids


# ---------------------------------------------------------------- 立案/冻结

def _last_closed_dispute(db: Session, record_id: int, exclude_dispute_id: int = None):
    q = db.query(models.ServiceDispute).join(
        models.ServiceDisputeRecord,
        models.ServiceDisputeRecord.dispute_id == models.ServiceDispute.id,
    ).filter(
        models.ServiceDisputeRecord.service_record_id == record_id,
        models.ServiceDispute.status == models.DisputeStatus.CLOSED,
    )
    if exclude_dispute_id:
        q = q.filter(models.ServiceDispute.id != exclude_dispute_id)
    return q.order_by(models.ServiceDispute.closed_at.desc()).first()


def freeze_record(db: Session, dispute: models.ServiceDispute, record: models.ServiceRecord,
                  operator: Optional[str] = None,
                  prev_decision: Optional[models.DisputeDecision] = None,
                  prev_dispute: Optional[models.ServiceDispute] = None,
                  round_tag: str = "r0"):
    """记录进入争议：根据上一轮结论做差异处理，保证不重复扣回。

    正常立案时自动查找该记录上一张已结案争议单；
    同一争议单结案后更正时，prev_* 由调用方传入本单上一轮结论。
    round_tag 区分同一争议单的多轮更正，保证红冲键互不撞车。
    """
    record.status = DISPUTED
    did = dispute.id

    if prev_decision is None:
        prev = prev_dispute or _last_closed_dispute(db, record.id, exclude_dispute_id=did)
        prev_decision = prev.decision if prev else None
        prev_dispute = prev
    else:
        prev = prev_dispute

    if prev_decision is None:
        # 首次争议：原始（或拆分子记录的）已发积分尚未冲回，冻结一次即可
        key = f"freeze:{did}:{round_tag}:{record.id}"
        reason = f"争议单{dispute.dispute_no}冻结服务记录#{record.id}积分"
        _clawback_earned(db, record, key, did, "freeze", reason, operator)
        return

    if prev_decision == models.DisputeDecision.CONFIRM:
        # 上一轮恢复过积分（重复申诉或更正重开）：红冲上次恢复，不重复扣原始积分
        prev_id = prev.id if prev else did
        prev_no = prev.dispute_no if prev else dispute.dispute_no
        release_adj = db.query(models.PointsAdjustment).filter(
            models.PointsAdjustment.dispute_id == prev_id,
            models.PointsAdjustment.service_record_id == record.id,
            models.PointsAdjustment.action == "release",
        ).order_by(models.PointsAdjustment.id.desc()).first()
        amount = release_adj.amount if release_adj else (record.points_awarded or 0)
        key = f"clawback-release:{did}:{round_tag}:{prev_id}:{record.id}"
        reason = f"争议单{dispute.dispute_no}重开，撤销争议单{prev_no}的积分恢复"
        _clawback_earned(db, record, key, did, "clawback_release", reason, operator, amount)

    elif prev_decision == models.DisputeDecision.SPLIT:
        # 上一轮拆分产物随重新争议全部失效并红冲；原记录的首次冻结仍然有效
        children = db.query(models.ServiceRecord).filter(
            models.ServiceRecord.parent_record_id == record.id,
            models.ServiceRecord.status == ACTIVE,
        ).all()
        for child in children:
            child.status = REJECTED
            ckey = f"clawback-child:{did}:{round_tag}:{child.id}"
            creason = f"争议单{dispute.dispute_no}重开，撤销拆分记录#{child.id}积分"
            _clawback_earned(db, child, ckey, did, "clawback_child", creason, operator)

    # prev_decision == REJECT：原始冻结流水仍在，无需再次扣回


def generate_dispute_no(db: Session) -> str:
    today = date.today().strftime("%Y%m%d")
    prefix = f"DSP{today}"
    count = db.query(func.count(models.ServiceDispute.id)).filter(
        models.ServiceDispute.dispute_no.like(f"{prefix}%")
    ).scalar() or 0
    return f"{prefix}{(count + 1):04d}"


def open_dispute(db: Session, record_ids: List[int], reason: str,
                 created_by: Optional[str] = None, initiator_school_id: int = None,
                 evidence_text: Optional[str] = None, automatic: bool = False
                 ) -> models.ServiceDispute:
    if not record_ids:
        raise HTTPException(status_code=400, detail="至少选择一条服务记录")

    records = db.query(models.ServiceRecord).filter(
        models.ServiceRecord.id.in_(record_ids)
    ).all()
    if len(records) != len(set(record_ids)):
        raise HTTPException(status_code=404, detail="存在不存在的服务记录")

    open_link = db.query(models.ServiceDisputeRecord.service_record_id).join(
        models.ServiceDispute,
        models.ServiceDispute.id == models.ServiceDisputeRecord.dispute_id,
    ).filter(
        models.ServiceDisputeRecord.service_record_id.in_(record_ids),
        models.ServiceDispute.status.in_([models.DisputeStatus.OPEN, models.DisputeStatus.IN_REVIEW]),
    ).all()
    if open_link:
        raise HTTPException(
            status_code=400,
            detail=f"记录{[r[0] for r in open_link]}已在未结案争议中，不能重复申诉",
        )

    dispute = models.ServiceDispute(
        dispute_no=generate_dispute_no(db),
        reason=reason,
        status=models.DisputeStatus.OPEN,
        initiator_school_id=initiator_school_id,
        created_by=created_by or ("系统自动检测" if automatic else None),
    )
    db.add(dispute)
    db.flush()

    for record in records:
        db.add(models.ServiceDisputeRecord(
            dispute_id=dispute.id, service_record_id=record.id,
            role="duplicate" if len(records) > 1 else "primary",
        ))
        # 固定接收证据摘要：系统证据，后续不可篡改
        school_name = record.school.name if record.school else None
        summary = build_evidence_summary(record, school_name)
        if not record.evidence_summary:
            record.evidence_summary = summary
        db.add(models.ServiceEvidence(
            dispute_id=dispute.id,
            service_record_id=record.id,
            submitter_type=models.EvidenceSubmitterType.SYSTEM,
            submitter_name="系统",
            content=f"接收记录时固定证据：{summary}；指纹={record.evidence_fingerprint or '无'}",
        ))
        freeze_record(db, dispute, record, operator=dispute.created_by)

    if evidence_text:
        db.add(models.ServiceEvidence(
            dispute_id=dispute.id,
            submitter_type=models.EvidenceSubmitterType.SCHOOL,
            submitter_school_id=initiator_school_id,
            submitter_name=created_by,
            content=evidence_text,
        ))

    db.add(models.DisputeReviewLog(
        dispute_id=dispute.id,
        action="auto_open" if automatic else "open",
        operator=dispute.created_by,
        comment=reason,
    ))

    vids = _touch_volunteers(db, record_ids)
    db.commit()
    for vid in vids:
        refresh_star_certificates(db, vid)
    db.refresh(dispute)
    return dispute


# ---------------------------------------------------------------- 裁决

def attach_record_to_open_dispute(db: Session, dispute: models.ServiceDispute,
                                  record: models.ServiceRecord,
                                  operator: Optional[str] = None) -> models.ServiceDispute:
    """复核期间双方又上报同段新服务：挂入当前争议单并冻结，不另立新单、不重复扣回。"""
    exists = db.query(models.ServiceDisputeRecord).filter(
        models.ServiceDisputeRecord.dispute_id == dispute.id,
        models.ServiceDisputeRecord.service_record_id == record.id,
    ).first()
    if exists:
        return dispute

    db.add(models.ServiceDisputeRecord(
        dispute_id=dispute.id, service_record_id=record.id, role="late",
    ))
    school_name = record.school.name if record.school else None
    summary = build_evidence_summary(record, school_name)
    if not record.evidence_summary:
        record.evidence_summary = summary
    db.add(models.ServiceEvidence(
        dispute_id=dispute.id,
        service_record_id=record.id,
        submitter_type=models.EvidenceSubmitterType.SYSTEM,
        submitter_name="系统",
        content=f"复核期间新上报，挂入现有争议单。固定证据：{summary}；指纹={record.evidence_fingerprint or '无'}",
    ))
    freeze_record(db, dispute, record, operator=operator)
    db.add(models.DisputeReviewLog(
        dispute_id=dispute.id, action="attach", operator=operator,
        comment=f"复核期间新服务记录#{record.id}挂入本争议单",
    ))
    recompute_derivatives(db, record.volunteer_id)
    db.commit()
    refresh_star_certificates(db, record.volunteer_id)
    db.refresh(dispute)
    return dispute


def find_open_dispute_for(db: Session, record_ids: List[int]) -> Optional[models.ServiceDispute]:
    return db.query(models.ServiceDispute).join(
        models.ServiceDisputeRecord,
        models.ServiceDisputeRecord.dispute_id == models.ServiceDispute.id,
    ).filter(
        models.ServiceDisputeRecord.service_record_id.in_(record_ids),
        models.ServiceDispute.status.in_([models.DisputeStatus.OPEN, models.DisputeStatus.IN_REVIEW]),
    ).order_by(models.ServiceDispute.created_at.desc()).first()


# ---------------------------------------------------------------- 裁决

def _apply_decision(db: Session, dispute: models.ServiceDispute,
                    decision: models.DisputeDecision, split_hours: Optional[dict],
                    operator: Optional[str], round_tag: str):
    records = [link.service_record for link in dispute.links]

    for record in records:
        key_prefix = f"{round_tag}:{dispute.id}:{record.id}"

        if decision == models.DisputeDecision.CONFIRM:
            amount = record.points_awarded or 0
            record.status = ACTIVE
            if amount > 0:
                key = f"release:{key_prefix}"
                reason = f"争议单{dispute.dispute_no}确认有效，恢复记录#{record.id}积分"
                adj = _ensure_adjustment(db, key, dispute.id, record.id,
                                         record.volunteer_id, "release", amount, reason, operator)
                _points_entry(db, record.volunteer_id, models.PointsType.EARN, amount,
                              models.PointsSource.DISPUTE_HOLD, key, reason,
                              service_record_id=record.id, adjustment_id=adj.id)

        elif decision == models.DisputeDecision.REJECT:
            record.status = REJECTED
            key = f"reject:{key_prefix}"
            reason = f"争议单{dispute.dispute_no}驳回记录#{record.id}，冻结积分转为最终扣减"
            _ensure_adjustment(db, key, dispute.id, record.id, record.volunteer_id,
                               "reject", record.points_awarded or 0, reason, operator)

        elif decision == models.DisputeDecision.SPLIT:
            raw_hours = (split_hours or {}).get(str(record.id))
            if raw_hours is None:
                raise HTTPException(status_code=400, detail=f"拆分结论需要记录#{record.id}的认可时长")
            new_hours = float(raw_hours)
            if new_hours < 0 or new_hours > record.service_hours:
                raise HTTPException(
                    status_code=400,
                    detail=f"记录#{record.id}拆分时长须在0与原时长{record.service_hours}之间",
                )

            if new_hours == 0:
                # 该条上报整体不予认可
                record.status = REJECTED
                zkey = f"split-zero:{key_prefix}"
                zreason = f"争议单{dispute.dispute_no}拆分认定记录#{record.id}无效"
                _ensure_adjustment(db, zkey, dispute.id, record.id, record.volunteer_id,
                                   "reject", record.points_awarded or 0, zreason, operator)
                continue

            record.status = SPLIT
            skey = f"split:{key_prefix}"
            sreason = f"争议单{dispute.dispute_no}拆分记录#{record.id}，认可{new_hours}小时"
            _ensure_adjustment(db, skey, dispute.id, record.id, record.volunteer_id,
                               "split", 0, sreason, operator)

            original_points = record.points_awarded or 0
            child_points = round(original_points * new_hours / record.service_hours) \
                if record.service_hours else 0

            child = models.ServiceRecord(
                volunteer_id=record.volunteer_id,
                service_date=record.service_date,
                service_hours=new_hours,
                audience_count=record.audience_count,
                points_awarded=child_points,
                school_id=record.school_id,
                activity_name=record.activity_name,
                time_range=record.time_range,
                evidence_summary=f"由争议单{dispute.dispute_no}拆分自记录#{record.id}",
                evidence_fingerprint=record.evidence_fingerprint,
                status=ACTIVE,
                parent_record_id=record.id,
            )
            db.add(child)
            db.flush()

            if child_points > 0:
                key = f"split-earn:{dispute.id}:{child.id}"
                reason = f"争议单{dispute.dispute_no}拆分确认，发放记录#{child.id}积分"
                adj = _ensure_adjustment(db, key, dispute.id, child.id,
                                         child.volunteer_id, "split_earn", child_points,
                                         reason, operator)
                # 拆分补发是真实应得积分：计入正常统计口径（control_entry=False）
                _points_entry(db, child.volunteer_id, models.PointsType.EARN, child_points,
                              models.PointsSource.DISPUTE_ADJUST, key, reason,
                              service_record_id=child.id, adjustment_id=adj.id,
                              control_entry=False)

    parent_ids = [r.id for r in records]
    children = db.query(models.ServiceRecord).filter(
        models.ServiceRecord.parent_record_id.in_(parent_ids)
    ).all() if parent_ids else []
    return parent_ids + [c.id for c in children]


def decide_dispute(db: Session, dispute: models.ServiceDispute,
                   decision: models.DisputeDecision, reviewer: str, comment: Optional[str],
                   split_hours: Optional[dict], correction: bool = False) -> models.ServiceDispute:
    if not correction and dispute.status == models.DisputeStatus.CLOSED:
        raise HTTPException(status_code=400, detail="争议已结案，请使用结案后更正接口")
    if correction and dispute.status != models.DisputeStatus.CLOSED:
        raise HTTPException(status_code=400, detail="只有已结案争议可以更正")

    round_tag = "c0"
    if correction:
        dispute.correction_round = (dispute.correction_round or 0) + 1
        round_tag = f"c{dispute.correction_round}"
        # 重开：按本单上一轮结论差异红冲（同一争议单的第 N 轮更正）
        prev_decision = dispute.decision
        for link in dispute.links:
            freeze_record(db, dispute, link.service_record, operator=reviewer,
                          prev_decision=prev_decision, prev_dispute=dispute,
                          round_tag=round_tag)
        db.add(models.DisputeReviewLog(
            dispute_id=dispute.id, action="reopen", operator=reviewer,
            comment=f"结案后更正，重新进入复核：{comment or ''}",
        ))

    affected = _apply_decision(db, dispute, decision, split_hours, reviewer, round_tag)

    dispute.status = models.DisputeStatus.CLOSED
    dispute.reviewer = reviewer
    dispute.decision = decision
    dispute.decision_comment = comment
    dispute.decided_at = datetime.utcnow()
    dispute.closed_at = datetime.utcnow()

    db.add(models.DisputeReviewLog(
        dispute_id=dispute.id,
        action=f"decision_{decision.value}",
        operator=reviewer,
        comment=comment,
    ))

    vids = _touch_volunteers(db, affected)
    db.commit()
    for vid in vids:
        refresh_star_certificates(db, vid)
    db.refresh(dispute)
    return dispute


# ---------------------------------------------------------------- 路由

@router.get("/", response_model=List[schemas.ServiceDispute])
def list_disputes(status: models.DisputeStatus = None, skip: int = 0, limit: int = 100,
                  db: Session = Depends(get_db)):
    query = db.query(models.ServiceDispute)
    if status:
        query = query.filter(models.ServiceDispute.status == status)
    return query.order_by(models.ServiceDispute.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/{dispute_id}", response_model=schemas.ServiceDisputeDetail)
def get_dispute(dispute_id: int, db: Session = Depends(get_db)):
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议单不存在")
    return dispute


@router.post("/", response_model=schemas.ServiceDispute, status_code=201)
def create_dispute(payload: schemas.DisputeCreate, db: Session = Depends(get_db)):
    return open_dispute(
        db,
        record_ids=payload.service_record_ids,
        reason=payload.reason,
        created_by=payload.created_by,
        initiator_school_id=payload.initiator_school_id,
        evidence_text=payload.evidence,
    )


@router.post("/{dispute_id}/accept", response_model=schemas.ServiceDispute)
def accept_dispute(dispute_id: int, action: schemas.DisputeReviewAction, db: Session = Depends(get_db)):
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议单不存在")
    if dispute.status == models.DisputeStatus.CLOSED:
        raise HTTPException(status_code=400, detail="争议已结案")
    dispute.status = models.DisputeStatus.IN_REVIEW
    dispute.reviewer = action.reviewer
    db.add(models.DisputeReviewLog(
        dispute_id=dispute.id, action="accept", operator=action.reviewer, comment=action.comment,
    ))
    db.commit()
    db.refresh(dispute)
    return dispute


@router.post("/{dispute_id}/evidence", response_model=schemas.Evidence, status_code=201)
def submit_evidence(dispute_id: int, payload: schemas.EvidenceCreate,
                    service_record_id: int = None, db: Session = Depends(get_db)):
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议单不存在")
    if service_record_id and not db.query(models.ServiceDisputeRecord).filter(
        models.ServiceDisputeRecord.dispute_id == dispute_id,
        models.ServiceDisputeRecord.service_record_id == service_record_id,
    ).first():
        raise HTTPException(status_code=400, detail="证据只能关联本争议单内的服务记录")

    evidence = models.ServiceEvidence(
        dispute_id=dispute_id,
        service_record_id=service_record_id,
        submitter_type=models.EvidenceSubmitterType.SCHOOL,
        submitter_school_id=payload.submitter_school_id,
        submitter_name=payload.submitter_name,
        content=payload.content,
        attachment_url=payload.attachment_url,
    )
    db.add(evidence)
    db.add(models.DisputeReviewLog(
        dispute_id=dispute_id, action="evidence",
        operator=payload.submitter_name, comment=payload.content[:200],
    ))
    db.commit()
    db.refresh(evidence)
    return evidence


@router.post("/{dispute_id}/decision", response_model=schemas.ServiceDisputeDetail)
def make_decision(dispute_id: int, payload: schemas.DisputeDecisionRequest,
                  db: Session = Depends(get_db)):
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议单不存在")
    if not payload.reviewer:
        raise HTTPException(status_code=400, detail="须指定有权限的复核人")
    return decide_dispute(db, dispute, payload.decision, payload.reviewer,
                          payload.comment, payload.split_hours)


@router.post("/{dispute_id}/correct", response_model=schemas.ServiceDisputeDetail)
def correct_decision(dispute_id: int, payload: schemas.DisputeCorrectionRequest,
                     db: Session = Depends(get_db)):
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议单不存在")
    if not payload.reviewer:
        raise HTTPException(status_code=400, detail="须指定有权限的复核人")
    return decide_dispute(db, dispute, payload.decision, payload.reviewer,
                          payload.comment, payload.split_hours, correction=True)


@router.get("/{dispute_id}/adjustments", response_model=List[schemas.PointsAdjustmentOut])
def list_adjustments(dispute_id: int, db: Session = Depends(get_db)):
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议单不存在")
    return db.query(models.PointsAdjustment).filter(
        models.PointsAdjustment.dispute_id == dispute_id
    ).order_by(models.PointsAdjustment.id).all()


@router.get("/records/{record_id}/duplicates", response_model=schemas.DuplicateCheckResult)
def check_duplicates(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.ServiceRecord).filter(models.ServiceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="服务记录不存在")
    dup_ids = find_duplicate_candidates(db, record)
    return schemas.DuplicateCheckResult(
        service_record_id=record_id,
        duplicate_record_ids=dup_ids,
        is_duplicate=bool(dup_ids),
    )
