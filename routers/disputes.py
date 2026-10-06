from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from database import get_db
import models, schemas
from routers.points import adjust_points

router = APIRouter(prefix="/api/disputes", tags=["服务核验"])
records_router = APIRouter(prefix="/api/service-records", tags=["服务核验"])

# 有权作出复核结论的角色（团委/市级复核人）
REVIEWER_ROLES = {"团委复核人", "市级复核人", "系统管理员"}


def generate_dispute_no(db: Session) -> str:
    today = date.today()
    prefix = f"DSP{today.year}{today.month:02d}"
    count = db.query(func.count(models.ServiceDispute.id)).filter(
        models.ServiceDispute.dispute_no.like(f"{prefix}%")
    ).scalar() or 0
    return f"{prefix}{(count + 1):04d}"


def _log_adjustment(db: Session, dispute: models.ServiceDispute, record: models.ServiceRecord,
                    adjustment_type: models.AdjustmentType, points_delta: int, hours_delta: float,
                    actor: str, note: str) -> models.ServiceAdjustment:
    """登记派生结果调整台账：每次冻结/恢复/发放/驳回都留下带调整后快照的可追溯记录。"""
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == record.volunteer_id).first()
    adjustment = models.ServiceAdjustment(
        dispute_id=dispute.id if dispute else None,
        service_record_id=record.id,
        volunteer_id=record.volunteer_id,
        adjustment_type=adjustment_type,
        points_delta=points_delta,
        hours_delta=hours_delta,
        points_balance_after=volunteer.points_balance if volunteer else None,
        total_hours_after=volunteer.total_service_hours if volunteer else None,
        star_level_id_after=volunteer.star_level_id if volunteer else None,
        actor=actor,
        note=note
    )
    db.add(adjustment)
    db.flush()
    return adjustment


def open_dispute(db: Session, record: models.ServiceRecord, reason: str,
                 raised_by: str = None, raised_school_id: int = None,
                 is_duplicate_suspected: bool = False) -> models.ServiceDispute:
    """将服务记录置入争议状态并冻结其积分、星级与统计贡献。

    幂等保证：仅"有效"状态的记录存在已发放积分，冻结额按当前快照一次性扣回；
    已驳回记录复查立案时冻结额为 0，不会重复扣回。
    """
    if record.verification_status == models.ServiceRecordStatus.DISPUTED:
        raise HTTPException(status_code=409, detail="该服务记录已在争议复核中，请勿重复申诉")

    is_active = record.verification_status == models.ServiceRecordStatus.ACTIVE
    freeze_points = (record.points_awarded or 0) if is_active else 0
    freeze_hours = (record.service_hours or 0.0) if is_active else 0.0

    dispute = models.ServiceDispute(
        dispute_no=generate_dispute_no(db),
        service_record_id=record.id,
        volunteer_id=record.volunteer_id,
        reason=reason,
        raised_by=raised_by,
        raised_school_id=raised_school_id,
        is_duplicate_suspected=is_duplicate_suspected,
        freeze_points=freeze_points,
        freeze_hours=freeze_hours
    )
    db.add(dispute)
    db.flush()

    record.verification_status = models.ServiceRecordStatus.DISPUTED

    if freeze_points > 0:
        adjust_points(
            db=db,
            volunteer_id=record.volunteer_id,
            points=-freeze_points,
            source=models.PointsSource.DISPUTE_FREEZE,
            description=f"争议{dispute.dispute_no}立案，冻结服务积分",
            service_record_id=record.id
        )

    from routers.service_records import update_volunteer_star
    update_volunteer_star(record.volunteer_id, db)

    _log_adjustment(
        db, dispute, record, models.AdjustmentType.FREEZE,
        points_delta=-freeze_points, hours_delta=-freeze_hours,
        actor=raised_by or "system",
        note=f"争议{dispute.dispute_no}立案冻结"
    )
    db.commit()
    db.refresh(dispute)
    return dispute


@records_router.post("/{record_id}/disputes", response_model=schemas.ServiceDisputeDetail)
def appeal_service_record(record_id: int, appeal: schemas.DisputeCreate, db: Session = Depends(get_db)):
    """申诉入口：学校或团委对已入账的服务记录提出异议，记录随即冻结。"""
    record = db.query(models.ServiceRecord).filter(models.ServiceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="服务记录不存在")
    if appeal.raised_school_id:
        school = db.query(models.School).filter(models.School.id == appeal.raised_school_id).first()
        if not school:
            raise HTTPException(status_code=404, detail="申诉学校不存在")

    dispute = open_dispute(
        db, record,
        reason=appeal.reason,
        raised_by=appeal.raised_by,
        raised_school_id=appeal.raised_school_id,
        is_duplicate_suspected=appeal.is_duplicate_suspected
    )
    return _dispute_detail(dispute, db)


@records_router.get("/{record_id}/disputes", response_model=List[schemas.ServiceDispute])
def list_record_disputes(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.ServiceRecord).filter(models.ServiceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="服务记录不存在")
    return db.query(models.ServiceDispute).filter(
        models.ServiceDispute.service_record_id == record_id
    ).order_by(models.ServiceDispute.created_at.desc()).all()


def _dispute_detail(dispute: models.ServiceDispute, db: Session) -> schemas.ServiceDisputeDetail:
    detail = schemas.ServiceDisputeDetail.model_validate(dispute)
    detail.evidences = [
        schemas.DisputeEvidence.model_validate(e)
        for e in db.query(models.DisputeEvidence).filter(
            models.DisputeEvidence.dispute_id == dispute.id
        ).order_by(models.DisputeEvidence.created_at).all()
    ]
    detail.adjustments = [
        schemas.ServiceAdjustment.model_validate(a)
        for a in db.query(models.ServiceAdjustment).filter(
            models.ServiceAdjustment.dispute_id == dispute.id
        ).order_by(models.ServiceAdjustment.created_at).all()
    ]
    return detail


@router.get("/", response_model=List[schemas.ServiceDispute])
def list_disputes(status: str = None, volunteer_id: int = None, service_record_id: int = None,
                  skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(models.ServiceDispute)
    if status:
        status_enum = None
        for s in models.DisputeStatus:
            if s.value == status or s.name == status:
                status_enum = s
                break
        if status_enum:
            query = query.filter(models.ServiceDispute.status == status_enum)
    if volunteer_id:
        query = query.filter(models.ServiceDispute.volunteer_id == volunteer_id)
    if service_record_id:
        query = query.filter(models.ServiceDispute.service_record_id == service_record_id)
    return query.order_by(models.ServiceDispute.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/adjustments", response_model=List[schemas.ServiceAdjustment])
def list_adjustments(volunteer_id: int = None, service_record_id: int = None,
                     skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(models.ServiceAdjustment)
    if volunteer_id:
        query = query.filter(models.ServiceAdjustment.volunteer_id == volunteer_id)
    if service_record_id:
        query = query.filter(models.ServiceAdjustment.service_record_id == service_record_id)
    return query.order_by(models.ServiceAdjustment.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/{dispute_id}", response_model=schemas.ServiceDisputeDetail)
def get_dispute(dispute_id: int, db: Session = Depends(get_db)):
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议记录不存在")
    return _dispute_detail(dispute, db)


@router.post("/{dispute_id}/evidences", response_model=schemas.DisputeEvidence)
def submit_evidence(dispute_id: int, evidence: schemas.DisputeEvidenceCreate,
                    db: Session = Depends(get_db)):
    """争议双方补交证据（仅待复核状态可补交）。"""
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议记录不存在")
    if dispute.status != models.DisputeStatus.OPEN:
        raise HTTPException(status_code=409, detail="争议已结案，不能再补交证据")
    if evidence.school_id:
        school = db.query(models.School).filter(models.School.id == evidence.school_id).first()
        if not school:
            raise HTTPException(status_code=404, detail="学校不存在")

    db_evidence = models.DisputeEvidence(
        dispute_id=dispute_id,
        school_id=evidence.school_id,
        submitted_by=evidence.submitted_by,
        content=evidence.content
    )
    db.add(db_evidence)
    db.commit()
    db.refresh(db_evidence)
    return db_evidence


@router.post("/{dispute_id}/resolve", response_model=schemas.ServiceDisputeDetail)
def resolve_dispute(dispute_id: int, resolution: schemas.DisputeResolve, db: Session = Depends(get_db)):
    """复核结案：由有权限的复核人作出确认、拆分或驳回决定。

    结论以可追溯的调整台账恢复或了结对积分、星级、统计的冻结；
    已结案的争议不可重复结案，保证不会重复扣回或重复恢复。
    """
    dispute = db.query(models.ServiceDispute).filter(models.ServiceDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="争议记录不存在")
    if dispute.status == models.DisputeStatus.RESOLVED:
        raise HTTPException(status_code=409, detail="该争议已结案，复核结论不可重复执行")
    if resolution.reviewer_role not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="无复核权限，仅团委/市级复核人可作出复核结论")

    record = db.query(models.ServiceRecord).filter(
        models.ServiceRecord.id == dispute.service_record_id
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="关联服务记录不存在")
    if record.verification_status != models.ServiceRecordStatus.DISPUTED:
        raise HTTPException(status_code=409, detail="服务记录不在争议状态，无法按本争议结案")

    from routers.service_records import update_volunteer_star, grant_service_points

    actor = resolution.reviewer_name

    if resolution.decision == models.DisputeDecision.CONFIRM:
        if dispute.freeze_points > 0:
            # 事后申诉的冻结：原额恢复，不重复发放
            adjust_points(
                db=db,
                volunteer_id=record.volunteer_id,
                points=dispute.freeze_points,
                source=models.PointsSource.DISPUTE_RESTORE,
                description=f"争议{dispute.dispute_no}确认有效，恢复冻结积分",
                service_record_id=record.id
            )
            record.verification_status = models.ServiceRecordStatus.ACTIVE
            update_volunteer_star(record.volunteer_id, db)
            _log_adjustment(
                db, dispute, record, models.AdjustmentType.RESTORE,
                points_delta=dispute.freeze_points, hours_delta=dispute.freeze_hours,
                actor=actor, note="复核确认有效，解冻恢复全部派生结果"
            )
        else:
            # 立案时积分未发放（疑似重复直接冻结）或已驳回记录复查：按记录当前内容补发
            granted = grant_service_points(db, record, note_prefix=f"争议{dispute.dispute_no}确认，")
            record.verification_status = models.ServiceRecordStatus.ACTIVE
            update_volunteer_star(record.volunteer_id, db)
            _log_adjustment(
                db, dispute, record, models.AdjustmentType.GRANT,
                points_delta=granted, hours_delta=record.service_hours or 0.0,
                actor=actor, note="复核确认有效，补发积分与时长"
            )

    elif resolution.decision == models.DisputeDecision.REJECT:
        # 驳回：立案时冻结扣回的积分转为永久扣回，不再恢复；未发放过的记录本就无积分可扣
        record.verification_status = models.ServiceRecordStatus.REJECTED
        update_volunteer_star(record.volunteer_id, db)
        _log_adjustment(
            db, dispute, record, models.AdjustmentType.REVOKE,
            points_delta=0, hours_delta=0.0,
            actor=actor,
            note=f"复核驳回，立案时冻结的{dispute.freeze_points}积分转为永久扣回"
        )

    elif resolution.decision == models.DisputeDecision.SPLIT:
        if resolution.adjusted_hours is None:
            raise HTTPException(status_code=400, detail="拆分决定必须给出核定服务时长")
        if resolution.adjusted_hours < 0 or resolution.adjusted_hours > (record.service_hours or 0):
            raise HTTPException(status_code=400, detail="核定时长必须介于0与原上报时长之间")

        # 立案冻结已使记录贡献归零（或立案时本就未发放），结案按核定结果一次性发放；
        # 冻结额与核定发放的差额随驳回部分转为永久扣回，台账完整记录差额去向
        record.service_hours = resolution.adjusted_hours
        granted = grant_service_points(db, record, note_prefix=f"争议{dispute.dispute_no}拆分确认，")
        record.verification_status = models.ServiceRecordStatus.ACTIVE
        update_volunteer_star(record.volunteer_id, db)
        _log_adjustment(
            db, dispute, record, models.AdjustmentType.SPLIT,
            points_delta=granted, hours_delta=resolution.adjusted_hours,
            actor=actor,
            note=f"拆分确认：核定{resolution.adjusted_hours}小时，发放{granted}积分；"
                 f"立案冻结{dispute.freeze_points}积分中未恢复部分转为永久扣回"
        )

    dispute.status = models.DisputeStatus.RESOLVED
    dispute.decision = resolution.decision
    dispute.adjusted_hours = resolution.adjusted_hours if resolution.decision == models.DisputeDecision.SPLIT else None
    dispute.resolution_notes = resolution.resolution_notes
    dispute.reviewer_name = resolution.reviewer_name
    dispute.reviewer_role = resolution.reviewer_role
    dispute.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(dispute)
    return _dispute_detail(dispute, db)
