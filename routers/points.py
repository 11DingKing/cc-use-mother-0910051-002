from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List
from database import get_db
import models, schemas

router = APIRouter(prefix="/api/points", tags=["积分管理"])


def effective_points_query(db: Session):
    """有效积分流水口径：
    - 排除争议管控流水（冻结/恢复只纠正余额，不进统计）；
    - 关联服务记录的流水仅当记录为"正常"时计入（争议中/已驳回/
      已拆分的原发放随记录状态移出统计，拆分产生的子记录正常计入）。
    """
    return db.query(models.PointsRecord).outerjoin(
        models.ServiceRecord,
        models.PointsRecord.service_record_id == models.ServiceRecord.id,
    ).filter(
        models.PointsRecord.control_entry == False,  # noqa: E712
        or_(
            models.PointsRecord.service_record_id.is_(None),
            models.ServiceRecord.status == models.ServiceRecordStatus.ACTIVE,
        ),
    )


def add_points(db: Session, volunteer_id: int, points: int, source: models.PointsSource,
               description: str = None, service_record_id: int = None, exchange_id: int = None):
    if points <= 0:
        return None

    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        return None

    volunteer.points_balance = (volunteer.points_balance or 0) + points

    record = models.PointsRecord(
        volunteer_id=volunteer_id,
        points_type=models.PointsType.EARN,
        points_amount=points,
        source=source,
        description=description,
        service_record_id=service_record_id,
        exchange_id=exchange_id
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def spend_points(db: Session, volunteer_id: int, points: int, source: models.PointsSource,
                description: str = None, exchange_id: int = None,
                service_record_id: int = None):
    if points <= 0:
        return None

    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")

    if (volunteer.points_balance or 0) < points:
        raise HTTPException(status_code=400, detail="积分不足")

    volunteer.points_balance -= points

    record = models.PointsRecord(
        volunteer_id=volunteer_id,
        points_type=models.PointsType.SPEND,
        points_amount=points,
        source=source,
        description=description,
        exchange_id=exchange_id,
        service_record_id=service_record_id
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/records", response_model=List[schemas.PointsRecord])
def list_points_records(volunteer_id: int = None, points_type: str = None,
                        source: str = None, skip: int = 0, limit: int = 100,
                        db: Session = Depends(get_db)):
    query = db.query(models.PointsRecord)
    if volunteer_id:
        query = query.filter(models.PointsRecord.volunteer_id == volunteer_id)
    if points_type:
        query = query.filter(models.PointsRecord.points_type == points_type)
    if source:
        query = query.filter(models.PointsRecord.source == source)
    return query.order_by(models.PointsRecord.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/volunteer/{volunteer_id}", response_model=schemas.VolunteerPoints)
def get_volunteer_points(volunteer_id: int, db: Session = Depends(get_db)):
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")

    total_earned = effective_points_query(db).filter(
        models.PointsRecord.volunteer_id == volunteer_id,
        models.PointsRecord.points_type == models.PointsType.EARN
    ).with_entities(func.sum(models.PointsRecord.points_amount)).scalar() or 0

    total_spent = effective_points_query(db).filter(
        models.PointsRecord.volunteer_id == volunteer_id,
        models.PointsRecord.points_type == models.PointsType.SPEND
    ).with_entities(func.sum(models.PointsRecord.points_amount)).scalar() or 0

    return schemas.VolunteerPoints(
        volunteer_id=volunteer_id,
        name=volunteer.name,
        points_balance=volunteer.points_balance or 0,
        total_earned=total_earned,
        total_spent=total_spent
    )


@router.get("/volunteer/{volunteer_id}/records", response_model=List[schemas.PointsRecord])
def get_volunteer_points_records(volunteer_id: int, skip: int = 0, limit: int = 100,
                                 db: Session = Depends(get_db)):
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")

    return db.query(models.PointsRecord).filter(
        models.PointsRecord.volunteer_id == volunteer_id
    ).order_by(models.PointsRecord.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/manual-adjust", response_model=schemas.PointsRecord)
def manual_adjust_points(adjust: schemas.PointsRecordCreate, db: Session = Depends(get_db)):
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == adjust.volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")

    if adjust.points_type == models.PointsType.EARN:
        volunteer.points_balance = (volunteer.points_balance or 0) + adjust.points_amount
    else:
        if (volunteer.points_balance or 0) < adjust.points_amount:
            raise HTTPException(status_code=400, detail="积分不足")
        volunteer.points_balance -= adjust.points_amount

    record = models.PointsRecord(**adjust.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/stats", response_model=schemas.PointsStats)
def get_points_stats(db: Session = Depends(get_db)):
    base = effective_points_query(db)

    def agg(ptype):
        return base.filter(models.PointsRecord.points_type == ptype).with_entities(
            func.sum(models.PointsRecord.points_amount)
        ).scalar() or 0

    def cnt(ptype):
        return base.filter(models.PointsRecord.points_type == ptype).with_entities(
            func.count(models.PointsRecord.id)
        ).scalar() or 0

    total_earned = agg(models.PointsType.EARN)
    total_spent = agg(models.PointsType.SPEND)
    earn_count = cnt(models.PointsType.EARN)
    spend_count = cnt(models.PointsType.SPEND)

    return schemas.PointsStats(
        total_points_earned=total_earned,
        total_points_spent=total_spent,
        net_points=total_earned - total_spent,
        earn_count=earn_count,
        spend_count=spend_count
    )
