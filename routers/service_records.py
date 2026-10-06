from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
import models, schemas
from routers.points import add_points
from routers.star_certificates import check_and_issue_star_certificate
from routers.disputes import (
    build_fingerprint, find_duplicate_candidates, open_dispute, recompute_derivatives,
    find_open_dispute_for, attach_record_to_open_dispute,
)

router = APIRouter(prefix="/api/service-records", tags=["服务记录"])


def calculate_service_points(service_hours: float, teacher_rating: int = None) -> int:
    base_points = int(service_hours * 10)
    rating_points = 0
    if teacher_rating and teacher_rating >= 4:
        rating_points = (teacher_rating - 3) * 5
    return base_points + rating_points


def update_volunteer_star(volunteer_id: int, db: Session):
    """按正常状态的服务记录重算时长/星级（争议中、已驳回、已拆分的原记录不计）。"""
    recompute_derivatives(db, volunteer_id)
    db.commit()
    check_and_issue_star_certificate(db, volunteer_id)


@router.get("/", response_model=List[schemas.ServiceRecord])
def list_service_records(volunteer_id: int = None, status: str = None,
                         skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(models.ServiceRecord)
    if volunteer_id:
        query = query.filter(models.ServiceRecord.volunteer_id == volunteer_id)
    if status:
        status_enum = None
        for s in models.ServiceRecordStatus:
            if s.value == status or s.name == status:
                status_enum = s
                break
        if status_enum:
            query = query.filter(models.ServiceRecord.status == status_enum)
    return query.order_by(models.ServiceRecord.service_date.desc()).offset(skip).limit(limit).all()


@router.get("/{record_id}", response_model=schemas.ServiceRecordDetail)
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

    if record.time_slot_id:
        slot = db.query(models.TimeSlot).filter(models.TimeSlot.id == record.time_slot_id).first()
        if slot:
            slot.status = models.TimeSlotStatus.COMPLETED

    # 上报学校缺省取讲解员当前所属学校（借调场景由上报方显式传入）
    school_id = record.school_id or volunteer.school_id
    time_range = record.time_range
    if not time_range and record.time_slot_id:
        slot = db.query(models.TimeSlot).filter(models.TimeSlot.id == record.time_slot_id).first()
        if slot and slot.start_time:
            time_range = f"{slot.start_time}-{slot.end_time}"

    points_awarded = calculate_service_points(record.service_hours, record.teacher_rating)

    data = record.model_dump()
    data["school_id"] = school_id
    data["time_range"] = time_range
    db_record = models.ServiceRecord(**data)
    db_record.points_awarded = points_awarded
    db_record.evidence_fingerprint = build_fingerprint(
        record.volunteer_id, record.service_date, time_range, school_id, record.activity_name
    )
    db.add(db_record)
    db.flush()

    # 积分先入账成长档案，保证后续冻结/恢复有完整流水轨迹
    if points_awarded > 0:
        add_points(
            db=db,
            volunteer_id=record.volunteer_id,
            points=points_awarded,
            source=models.PointsSource.SERVICE_COMPLETION,
            description=f"完成讲解服务 {record.service_hours}小时",
            service_record_id=db_record.id
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
                    service_record_id=db_record.id
                )

    db.commit()
    db.refresh(db_record)

    # 接收时查重：同讲解员同一天、指纹一致或时段重叠 → 挂入已有争议或自动立案冻结
    duplicate_ids = find_duplicate_candidates(db, db_record)
    if duplicate_ids:
        open_dsp = find_open_dispute_for(db, duplicate_ids)
        if open_dsp:
            # 复核期间的新服务挂入当前争议单，不另立案、不产生重复扣回
            attach_record_to_open_dispute(db, open_dsp, db_record, operator="系统自动检测")
        else:
            all_ids = [db_record.id] + duplicate_ids
            open_dispute(
                db,
                record_ids=all_ids,
                reason="系统接收时检测到同一讲解员同时段疑似重复上报，自动立案核验",
                created_by="系统自动检测",
                initiator_school_id=school_id,
                automatic=True,
            )
        db.refresh(db_record)
        return db_record

    update_volunteer_star(record.volunteer_id, db)
    db.refresh(db_record)
    return db_record


@router.put("/{record_id}", response_model=schemas.ServiceRecord)
def update_service_record(record_id: int, record_update: schemas.ServiceRecordCreate,
                          db: Session = Depends(get_db)):
    record = db.query(models.ServiceRecord).filter(models.ServiceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="服务记录不存在")
    if record.status == models.ServiceRecordStatus.DISPUTED:
        raise HTTPException(status_code=400, detail="记录处于争议复核期，冻结期间不能修改")
    if record.status in (models.ServiceRecordStatus.REJECTED, models.ServiceRecordStatus.SPLIT):
        raise HTTPException(status_code=400, detail="记录已经核验结案，不能直接修改")

    old_points = record.points_awarded or 0

    update_data = record_update.model_dump(exclude_unset=True)
    if "school_id" not in update_data:
        update_data["school_id"] = record.volunteer.school_id
    for key, value in update_data.items():
        setattr(record, key, value)

    if not record.time_range and record.time_slot_id:
        slot = db.query(models.TimeSlot).filter(models.TimeSlot.id == record.time_slot_id).first()
        if slot and slot.start_time:
            record.time_range = f"{slot.start_time}-{slot.end_time}"

    record.evidence_fingerprint = build_fingerprint(
        record.volunteer_id, record.service_date, record.time_range,
        record.school_id, record.activity_name
    )

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
    if record.status == models.ServiceRecordStatus.DISPUTED:
        raise HTTPException(status_code=400, detail="记录处于争议复核期，冻结期间不能删除")
    if record.status in (models.ServiceRecordStatus.REJECTED, models.ServiceRecordStatus.SPLIT):
        raise HTTPException(status_code=400, detail="记录已纳入核验台账，不能删除")
    vid = record.volunteer_id
    points_to_deduct = record.points_awarded or 0

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
