import hashlib
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
import models, schemas
from routers.points import add_points
from routers.star_certificates import check_and_issue_star_certificate

router = APIRouter(prefix="/api/service-records", tags=["服务记录"])


def calculate_service_points(service_hours: float, teacher_rating: int = None) -> int:
    base_points = int(service_hours * 10)
    rating_points = 0
    if teacher_rating and teacher_rating >= 4:
        rating_points = (teacher_rating - 3) * 5
    return base_points + rating_points


def build_evidence_snapshot(db: Session, record: schemas.ServiceRecordCreate,
                            volunteer: models.Volunteer):
    """接收记录时固定证据摘要：活动、日期时段、人员、上报学校。

    返回 (可读摘要, 摘要哈希)。摘要在接收时生成后不再随记录修改而变化，
    后续任何对记录关键字段的改动都无法回溯篡改接收时的证据。
    """
    slot = None
    if record.time_slot_id:
        slot = db.query(models.TimeSlot).filter(models.TimeSlot.id == record.time_slot_id).first()
    school = None
    if record.reporting_school_id:
        school = db.query(models.School).filter(models.School.id == record.reporting_school_id).first()

    activity = record.activity_name or (slot.topic if slot else None) or "未命名活动"
    period = f"{slot.start_time}-{slot.end_time}" if slot and slot.start_time else "未登记时段"
    school_part = f"{school.id}:{school.name}" if school else "未填报"
    summary = (f"活动:{activity}|日期:{record.service_date}|时段:{period}|"
               f"人员:{volunteer.id}:{volunteer.name}|上报学校:{school_part}")
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    return summary, digest


def find_suspected_duplicates(db: Session, volunteer_id: int, service_date,
                              activity_name: str = None, time_slot_id: int = None,
                              exclude_id: int = None):
    """跨校重复上报检测：同一名讲解员在同一天、同一活动或同一时段出现多条有效/争议记录。"""
    query = db.query(models.ServiceRecord).filter(
        models.ServiceRecord.volunteer_id == volunteer_id,
        models.ServiceRecord.service_date == service_date,
        models.ServiceRecord.verification_status != models.ServiceRecordStatus.REJECTED
    )
    if exclude_id:
        query = query.filter(models.ServiceRecord.id != exclude_id)
    candidates = query.all()
    duplicates = []
    for r in candidates:
        same_slot = bool(time_slot_id) and r.time_slot_id == time_slot_id
        same_activity = (bool(activity_name) and bool(r.activity_name)
                         and r.activity_name.strip() == activity_name.strip())
        if same_slot or same_activity:
            duplicates.append(r)
    return duplicates


def grant_service_points(db: Session, record: models.ServiceRecord, note_prefix: str = "") -> int:
    """按记录当前时长与评分发放积分流水（基础 + 好评），并同步 points_awarded。"""
    points = calculate_service_points(record.service_hours, record.teacher_rating)
    if points <= 0:
        record.points_awarded = 0
        return 0

    base_points = int(record.service_hours * 10)
    if base_points > 0:
        add_points(
            db=db,
            volunteer_id=record.volunteer_id,
            points=base_points,
            source=models.PointsSource.SERVICE_COMPLETION,
            description=f"{note_prefix}完成讲解服务 {record.service_hours}小时",
            service_record_id=record.id
        )

    if record.teacher_rating and record.teacher_rating >= 4:
        rating_points = (record.teacher_rating - 3) * 5
        if rating_points > 0:
            add_points(
                db=db,
                volunteer_id=record.volunteer_id,
                points=rating_points,
                source=models.PointsSource.TEACHER_RATING,
                description=f"{note_prefix}老师好评 {record.teacher_rating}星",
                service_record_id=record.id
            )

    record.points_awarded = points
    return points


def update_volunteer_star(volunteer_id: int, db: Session):
    from sqlalchemy import func
    # 会话未开启 autoflush，先把待定的状态/时长变更落库，保证统计口径一致
    db.flush()
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        return
    # 仅"有效"状态的记录计入累计时长；争议中/已驳回的记录自动冻结其星级与统计贡献
    total_hours = db.query(func.sum(models.ServiceRecord.service_hours)).filter(
        models.ServiceRecord.volunteer_id == volunteer_id,
        models.ServiceRecord.verification_status == models.ServiceRecordStatus.ACTIVE
    ).scalar() or 0.0
    volunteer.total_service_hours = total_hours

    star_levels = db.query(models.StarLevel).order_by(models.StarLevel.min_hours.desc()).all()
    new_star = None
    for sl in star_levels:
        if total_hours >= sl.min_hours:
            new_star = sl
            break

    old_star_id = volunteer.star_level_id
    volunteer.star_level_id = new_star.id if new_star else None

    if old_star_id != volunteer.star_level_id and volunteer.star_level_id:
        db.commit()
        check_and_issue_star_certificate(db, volunteer_id)
    else:
        db.commit()


@router.get("/", response_model=List[schemas.ServiceRecord])
def list_service_records(volunteer_id: int = None, verification_status: str = None,
                         skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(models.ServiceRecord)
    if volunteer_id:
        query = query.filter(models.ServiceRecord.volunteer_id == volunteer_id)
    if verification_status:
        status_enum = None
        for s in models.ServiceRecordStatus:
            if s.value == verification_status or s.name == verification_status:
                status_enum = s
                break
        if status_enum:
            query = query.filter(models.ServiceRecord.verification_status == status_enum)
    return query.order_by(models.ServiceRecord.service_date.desc()).offset(skip).limit(limit).all()


@router.get("/{record_id}", response_model=schemas.ServiceRecord)
def get_service_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.ServiceRecord).filter(models.ServiceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="服务记录不存在")
    return record


@router.post("/", response_model=schemas.ServiceRecord)
def create_service_record(record: schemas.ServiceRecordCreate, db: Session = Depends(get_db)):
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == record.volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")

    if record.reporting_school_id:
        school = db.query(models.School).filter(models.School.id == record.reporting_school_id).first()
        if not school:
            raise HTTPException(status_code=404, detail="上报学校不存在")

    if record.time_slot_id:
        slot = db.query(models.TimeSlot).filter(models.TimeSlot.id == record.time_slot_id).first()
        if slot:
            slot.status = models.TimeSlotStatus.COMPLETED

    db_record = models.ServiceRecord(**record.model_dump())
    db_record.points_awarded = 0

    # 接收时固定证据摘要（活动、时段、人员、上报学校），此后不再随记录修改而变化
    summary, digest = build_evidence_snapshot(db, record, volunteer)
    if not db_record.evidence_summary:
        db_record.evidence_summary = summary
    db_record.evidence_digest = digest
    db.add(db_record)
    db.flush()

    # 疑似重复上报（跨校重复提交同一服务）：直接立案进入争议，积分与统计贡献先不生效
    duplicates = find_suspected_duplicates(
        db, record.volunteer_id, record.service_date,
        activity_name=record.activity_name, time_slot_id=record.time_slot_id,
        exclude_id=db_record.id
    )
    if duplicates:
        from routers.disputes import open_dispute
        dup_ids = "、".join(f"#{d.id}" for d in duplicates)
        open_dispute(
            db, db_record,
            reason=f"系统检测到疑似重复上报：与记录{dup_ids}同日期同活动",
            raised_by="系统自动检测",
            is_duplicate_suspected=True
        )
        db.refresh(db_record)
        return db_record

    grant_service_points(db, db_record)

    db.commit()
    db.refresh(db_record)

    update_volunteer_star(record.volunteer_id, db)

    db.refresh(db_record)
    return db_record


@router.put("/{record_id}", response_model=schemas.ServiceRecord)
def update_service_record(record_id: int, record_update: schemas.ServiceRecordCreate,
                          db: Session = Depends(get_db)):
    record = db.query(models.ServiceRecord).filter(models.ServiceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="服务记录不存在")
    if record.verification_status != models.ServiceRecordStatus.ACTIVE:
        raise HTTPException(status_code=409, detail="争议中或已驳回的记录禁止修改，请先完成复核")

    old_points = record.points_awarded or 0

    update_data = record_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(record, key, value)

    new_points = calculate_service_points(record.service_hours, record.teacher_rating)
    record.points_awarded = new_points

    if old_points != new_points:
        from routers.points import spend_points
        if old_points > 0:
            spend_points(
                db=db,
                volunteer_id=record.volunteer_id,
                points=old_points,
                source=models.PointsSource.OTHER,
                description="服务记录更新，扣除原积分",
                service_record_id=record.id
            )
        if new_points > 0:
            add_points(
                db=db,
                volunteer_id=record.volunteer_id,
                points=new_points,
                source=models.PointsSource.SERVICE_COMPLETION,
                description=f"更新讲解服务 {record.service_hours}小时",
                service_record_id=record.id
            )

            if record.teacher_rating and record.teacher_rating >= 4:
                rating_points = (record.teacher_rating - 3) * 5
                if rating_points > 0:
                    add_points(
                        db=db,
                        volunteer_id=record.volunteer_id,
                        points=rating_points,
                        source=models.PointsSource.TEACHER_RATING,
                        description=f"老师好评 {record.teacher_rating}星",
                        service_record_id=record.id
                    )

    db.commit()
    db.refresh(record)

    update_volunteer_star(record.volunteer_id, db)

    return record


@router.delete("/{record_id}")
def delete_service_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.ServiceRecord).filter(models.ServiceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="服务记录不存在")
    if record.verification_status == models.ServiceRecordStatus.DISPUTED:
        raise HTTPException(status_code=409, detail="争议中的记录禁止删除，请先完成复核")

    has_dispute_history = db.query(models.ServiceDispute).filter(
        models.ServiceDispute.service_record_id == record_id
    ).first() is not None
    if has_dispute_history:
        raise HTTPException(status_code=409, detail="存在争议历史的记录禁止删除，以保证复核过程可追溯")

    vid = record.volunteer_id
    # 仅有效记录仍持有已发放积分；已驳回记录的积分已在复核时扣回，删除不再重复扣
    points_to_deduct = (record.points_awarded or 0) \
        if record.verification_status == models.ServiceRecordStatus.ACTIVE else 0

    if points_to_deduct > 0:
        from routers.points import spend_points
        spend_points(
            db=db,
            volunteer_id=vid,
            points=points_to_deduct,
            source=models.PointsSource.OTHER,
            description="删除服务记录，扣除积分",
            service_record_id=record.id
        )

    db.delete(record)
    db.commit()
    update_volunteer_star(vid, db)
    return {"message": "删除成功"}
