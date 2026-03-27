"""Worker Certification endpoints — test workers for specific task types."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import (
    CertificationDB, CertificationQuestionDB, WorkerCertificationDB, UserDB,
)
from models.schemas import (
    CertificationOut, CertificationDetailOut, CertificationQuestionOut,
    CertAttemptRequest, CertAttemptResult, WorkerCertificationOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/certifications", tags=["certifications"])

# Cooldown between retake attempts (minutes)
RETAKE_COOLDOWN_MINUTES = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Admin: seed default certifications ────────────────────────────────────────

DEFAULT_CERTIFICATIONS = [
    {
        "task_type": "label_image",
        "name": "Image Labeling Certification",
        "description": "Demonstrate skill in accurate image classification and object detection labeling.",
        "passing_score": 70,
        "badge_icon": "🖼️",
        "questions": [
            {
                "question": "When labeling objects in an image, what should you do if an object is only partially visible at the edge of the frame?",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "Ignore it — only label fully visible objects"},
                    {"id": "b", "text": "Label it if at least 50% is visible"},
                    {"id": "c", "text": "Label it if it's clearly identifiable, noting it's partially cut off"},
                    {"id": "d", "text": "Label it regardless of how little is visible"},
                ],
                "correct_answer": "c",
                "explanation": "Partially visible objects should be labeled when they are clearly identifiable. The task instructions will specify minimum visibility thresholds.",
                "points": 10,
                "order_index": 0,
            },
            {
                "question": "Which of the following are good practices when drawing bounding boxes? (Select all that apply)",
                "question_type": "multi_choice",
                "options": [
                    {"id": "a", "text": "Boxes should be as tight as possible around the object"},
                    {"id": "b", "text": "Leave 10–20px padding around all objects"},
                    {"id": "c", "text": "Each object instance gets its own separate box"},
                    {"id": "d", "text": "Overlapping boxes for the same object class are fine"},
                ],
                "correct_answer": ["a", "c"],
                "explanation": "Bounding boxes should be tight and one per object instance. Loose boxes reduce annotation quality.",
                "points": 15,
                "order_index": 1,
            },
            {
                "question": "You're asked to label 'person' in an image. There is a mannequin dressed in clothes in a store window. What do you label it?",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "Label as 'person' since it looks like one"},
                    {"id": "b", "text": "Label as 'mannequin' if that category exists, otherwise skip"},
                    {"id": "c", "text": "Always label as 'person' to be safe"},
                    {"id": "d", "text": "Skip it — only label real people"},
                ],
                "correct_answer": "b",
                "explanation": "Follow task taxonomy carefully. A mannequin is not a person. If 'mannequin' isn't available, use 'other' or skip per instructions.",
                "points": 10,
                "order_index": 2,
            },
            {
                "question": "Quality in image labeling means:",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "Labeling as many objects as possible, even uncertain ones"},
                    {"id": "b", "text": "Speed over accuracy to complete more tasks"},
                    {"id": "c", "text": "Consistent, accurate labels following the provided guidelines"},
                    {"id": "d", "text": "Using your own judgment even if it contradicts guidelines"},
                ],
                "correct_answer": "c",
                "explanation": "Quality means consistency and accuracy per the guidelines. When in doubt, follow the instructions over personal judgment.",
                "points": 10,
                "order_index": 3,
            },
        ],
    },
    {
        "task_type": "verify_fact",
        "name": "Fact Verification Certification",
        "description": "Demonstrate your ability to verify claims against reliable sources.",
        "passing_score": 75,
        "badge_icon": "✅",
        "questions": [
            {
                "question": "What makes a source reliable for fact verification?",
                "question_type": "multi_choice",
                "options": [
                    {"id": "a", "text": "It has a large social media following"},
                    {"id": "b", "text": "It is a peer-reviewed publication or primary source"},
                    {"id": "c", "text": "Multiple independent sources agree on the same information"},
                    {"id": "d", "text": "The information matches your prior beliefs"},
                    {"id": "e", "text": "It is a government or major institutional website"},
                ],
                "correct_answer": ["b", "c", "e"],
                "explanation": "Reliable sources include peer-reviewed research, primary documents, and authoritative institutions. Social media and personal bias are not reliability indicators.",
                "points": 15,
                "order_index": 0,
            },
            {
                "question": "A claim states 'Company X's revenue grew 200% last year.' You find one press release from Company X confirming this but no other sources. You should:",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "Mark it as TRUE — the company itself confirmed it"},
                    {"id": "b", "text": "Mark it as UNVERIFIED — only one self-reported source"},
                    {"id": "c", "text": "Mark it as FALSE — financial claims need regulatory filings"},
                    {"id": "d", "text": "Skip it as too complex"},
                ],
                "correct_answer": "b",
                "explanation": "Self-reported data with no independent corroboration should be marked unverified. Independent confirmation from filings or news would upgrade it to true.",
                "points": 15,
                "order_index": 1,
            },
            {
                "question": "Which verdict is appropriate when a claim is technically true but missing important context?",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "TRUE"},
                    {"id": "b", "text": "FALSE"},
                    {"id": "c", "text": "MISLEADING"},
                    {"id": "d", "text": "UNVERIFIED"},
                ],
                "correct_answer": "c",
                "explanation": "Claims can be technically accurate but misleading. 'Misleading' is the correct verdict when context changes the meaning significantly.",
                "points": 10,
                "order_index": 2,
            },
        ],
    },
    {
        "task_type": "moderate_content",
        "name": "Content Moderation Certification",
        "description": "Learn to identify policy violations and moderate content effectively.",
        "passing_score": 80,
        "badge_icon": "🛡️",
        "questions": [
            {
                "question": "When moderating content, what is the most important principle?",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "Remove anything that could possibly offend someone"},
                    {"id": "b", "text": "Apply the platform's stated policies consistently and objectively"},
                    {"id": "c", "text": "Use personal judgment about what is acceptable"},
                    {"id": "d", "text": "When in doubt, always approve to avoid false positives"},
                ],
                "correct_answer": "b",
                "explanation": "Consistent policy application is paramount. Personal feelings should not influence moderation decisions.",
                "points": 15,
                "order_index": 0,
            },
            {
                "question": "Which of the following typically requires immediate escalation rather than standard moderation? (Select all that apply)",
                "question_type": "multi_choice",
                "options": [
                    {"id": "a", "text": "Mild profanity"},
                    {"id": "b", "text": "Credible threats of violence"},
                    {"id": "c", "text": "Child sexual abuse material (CSAM)"},
                    {"id": "d", "text": "Spam advertising"},
                    {"id": "e", "text": "Terrorist content"},
                ],
                "correct_answer": ["b", "c", "e"],
                "explanation": "CSAM, credible violence threats, and terrorist content require immediate escalation to legal/trust & safety teams, not just standard removal.",
                "points": 20,
                "order_index": 1,
            },
            {
                "question": "You encounter content that discusses self-harm in an educational context (e.g., mental health awareness). You should:",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "Remove it — any self-harm content is prohibited"},
                    {"id": "b", "text": "Approve it — educational content is always fine"},
                    {"id": "c", "text": "Evaluate against platform policy on educational vs. glorifying content"},
                    {"id": "d", "text": "Skip it — it's too nuanced to decide"},
                ],
                "correct_answer": "c",
                "explanation": "Context matters. Educational mental health content is usually allowed while content glorifying self-harm is not. Always refer to platform policy.",
                "points": 15,
                "order_index": 2,
            },
        ],
    },
    {
        "task_type": "rate_quality",
        "name": "Quality Rating Certification",
        "description": "Learn to assess and rate the quality of AI-generated and human content.",
        "passing_score": 70,
        "badge_icon": "⭐",
        "questions": [
            {
                "question": "When rating text quality on a 1-5 scale, a response that is accurate but poorly written should receive:",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "5 — accuracy is the only thing that matters"},
                    {"id": "b", "text": "1 — poor writing is unacceptable"},
                    {"id": "c", "text": "A middle score — good accuracy, poor quality"},
                    {"id": "d", "text": "Skip — can't rate without knowing the topic"},
                ],
                "correct_answer": "c",
                "explanation": "Quality ratings balance multiple factors. A response can score middling when strong in some dimensions (accuracy) but weak in others (writing quality).",
                "points": 10,
                "order_index": 0,
            },
            {
                "question": "What is 'calibration' in quality rating?",
                "question_type": "single_choice",
                "options": [
                    {"id": "a", "text": "Using the full rating scale consistently and as intended"},
                    {"id": "b", "text": "Always rating items in the middle to avoid extremes"},
                    {"id": "c", "text": "Giving high ratings to get tasks approved faster"},
                    {"id": "d", "text": "Matching your ratings to what others have rated"},
                ],
                "correct_answer": "a",
                "explanation": "Calibrated raters use the full scale appropriately. Rating inflation (always rating high) or central tendency (always middle) reduces data quality.",
                "points": 10,
                "order_index": 1,
            },
        ],
    },
]


async def seed_certifications(db: AsyncSession) -> None:
    """Seed default certifications if they don't exist."""
    for cert_data in DEFAULT_CERTIFICATIONS:
        existing = await db.execute(
            select(CertificationDB).where(CertificationDB.task_type == cert_data["task_type"])
        )
        if existing.scalar_one_or_none():
            continue

        cert = CertificationDB(
            task_type=cert_data["task_type"],
            name=cert_data["name"],
            description=cert_data["description"],
            passing_score=cert_data["passing_score"],
            badge_icon=cert_data["badge_icon"],
            created_at=utcnow(),
        )
        db.add(cert)
        await db.flush()

        for q_data in cert_data["questions"]:
            question = CertificationQuestionDB(
                cert_id=cert.id,
                question=q_data["question"],
                question_type=q_data["question_type"],
                options=q_data.get("options"),
                correct_answer=q_data["correct_answer"],
                explanation=q_data.get("explanation"),
                points=q_data.get("points", 10),
                order_index=q_data.get("order_index", 0),
                created_at=utcnow(),
            )
            db.add(question)

    await db.commit()


# ── Public: List certifications ────────────────────────────────────────────────

@router.get("", response_model=list[CertificationOut])
async def list_certifications(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all available certification programs."""
    # Seed defaults if empty
    count = (await db.execute(select(func.count()).select_from(CertificationDB))).scalar() or 0
    if count == 0:
        await seed_certifications(db)

    result = await db.execute(select(CertificationDB).order_by(CertificationDB.task_type))
    certs = result.scalars().all()

    # Bulk-load question counts for all certs in one GROUP BY query
    cert_ids = [c.id for c in certs]
    qcount_map: dict = {}
    if cert_ids:
        qc_res = await db.execute(
            select(CertificationQuestionDB.cert_id, func.count().label("cnt"))
            .where(CertificationQuestionDB.cert_id.in_(cert_ids))
            .group_by(CertificationQuestionDB.cert_id)
        )
        qcount_map = {str(r.cert_id): r.cnt for r in qc_res}

    out = []
    for cert in certs:
        item = CertificationOut.model_validate(cert)
        item.question_count = qcount_map.get(str(cert.id), 0)
        out.append(item)
    return out


@router.get("/{task_type}", response_model=CertificationDetailOut)
async def get_certification(
    task_type: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get a certification with its questions (answers hidden)."""
    cert = await _get_cert_by_type(task_type, db)

    questions_result = await db.execute(
        select(CertificationQuestionDB)
        .where(CertificationQuestionDB.cert_id == cert.id)
        .order_by(CertificationQuestionDB.order_index)
    )
    questions = questions_result.scalars().all()

    q_count = len(questions)
    out = CertificationDetailOut.model_validate(cert)
    out.question_count = q_count
    # Return questions WITHOUT correct_answer or explanation (those come after attempt)
    out.questions = [
        CertificationQuestionOut(
            id=q.id,
            question=q.question,
            question_type=q.question_type,
            options=q.options,
            explanation=None,  # Hidden until after attempt
            points=q.points,
            order_index=q.order_index,
        )
        for q in questions
    ]
    return out


# ── Attempt ────────────────────────────────────────────────────────────────────

@router.post("/{task_type}/attempt", response_model=CertAttemptResult)
async def attempt_certification(
    task_type: str,
    req: CertAttemptRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Submit answers for a certification test and get your score."""
    cert = await _get_cert_by_type(task_type, db)
    uid = UUID(user_id)

    # Lock the worker cert row (or confirm absence) before scoring.
    # with_for_update() serialises concurrent quiz submissions for the same
    # (worker, cert) pair — prevents lost-update on attempt_count and stale
    # cooldown checks, and prevents IntegrityError from duplicate-insert races
    # when existing is None (the lock turns the second submitter into an updater
    # rather than an inserter once the first commit is visible).
    existing_result = await db.execute(
        select(WorkerCertificationDB).where(
            WorkerCertificationDB.worker_id == uid,
            WorkerCertificationDB.cert_id == cert.id,
        ).with_for_update()
    )
    existing = existing_result.scalar_one_or_none()

    if existing and existing.last_attempt_at:
        mins_since = (utcnow() - existing.last_attempt_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
        if mins_since < RETAKE_COOLDOWN_MINUTES and existing.passed is False and existing.attempt_count > 0:
            wait_mins = int(RETAKE_COOLDOWN_MINUTES - mins_since)
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {wait_mins} more minutes before retaking this certification."
            )

    # Load questions with correct answers
    questions_result = await db.execute(
        select(CertificationQuestionDB)
        .where(CertificationQuestionDB.cert_id == cert.id)
        .order_by(CertificationQuestionDB.order_index)
    )
    questions = questions_result.scalars().all()
    if not questions:
        raise HTTPException(status_code=400, detail="This certification has no questions yet")

    # Build answer lookup
    answer_map = {str(a.question_id): a.answer for a in req.answers}

    # Score
    total_points = sum(q.points for q in questions)
    earned_points = 0
    correct_count = 0
    details = []

    for q in questions:
        qid = str(q.id)
        submitted = answer_map.get(qid)
        correct = _check_answer(submitted, q.correct_answer, q.question_type)
        pts = q.points if correct else 0
        earned_points += pts
        if correct:
            correct_count += 1
        details.append({
            "question_id": qid,
            "question": q.question,
            "submitted": submitted,
            "correct_answer": q.correct_answer,
            "is_correct": correct,
            "points_earned": pts,
            "points_possible": q.points,
            "explanation": q.explanation,
        })

    score_pct = round((earned_points / total_points) * 100) if total_points > 0 else 0
    passed = score_pct >= cert.passing_score

    # Update worker certification record
    now = utcnow()
    if existing:
        existing.attempt_count += 1
        existing.score = score_pct
        existing.last_attempt_at = now
        if score_pct > existing.best_score:
            existing.best_score = score_pct
        if passed and not existing.passed:
            existing.passed = True
            existing.certified_at = now
    else:
        wc = WorkerCertificationDB(
            worker_id=uid,
            cert_id=cert.id,
            score=score_pct,
            passed=passed,
            attempt_count=1,
            best_score=score_pct,
            certified_at=now if passed else None,
            last_attempt_at=now,
            created_at=now,
        )
        db.add(wc)

    await db.commit()

    # Send notification
    if passed:
        try:
            await create_notification(
                db=db,
                user_id=uid,
                type=NotifType.BADGE_EARNED,
                title=f"Certified: {cert.name}! {cert.badge_icon or ''}",
                body=f"You passed the {cert.name} with a score of {score_pct}%. You can now work on {task_type} tasks.",
                link=f"/worker/certifications",
            )
        except Exception:
            logger.warning(
                "certifications.pass_notification_failed",
                user_id=str(uid),
                task_type=task_type,
                exc_info=True,
            )

    # ── Onboarding: mark cert step on any attempt ─────────────────────────
    try:
        from routers.onboarding import mark_onboarding_step
        await mark_onboarding_step(uid, "cert", db)
        await db.commit()  # flush → commit so the step is actually persisted
    except Exception:
        logger.warning(
            "certifications.onboarding_step_failed",
            user_id=str(uid),
            step="cert",
            exc_info=True,
        )

    return CertAttemptResult(
        score=score_pct,
        passed=passed,
        total_points=total_points,
        earned_points=earned_points,
        question_count=len(questions),
        correct_count=correct_count,
        details=details,
    )


# ── Worker's certifications ────────────────────────────────────────────────────

@router.get("/me/earned", response_model=list[WorkerCertificationOut])
async def my_certifications(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get all certifications for the current worker."""
    uid = UUID(user_id)
    result = await db.execute(
        select(WorkerCertificationDB, CertificationDB)
        .join(CertificationDB, WorkerCertificationDB.cert_id == CertificationDB.id)
        .where(WorkerCertificationDB.worker_id == uid)
        .order_by(WorkerCertificationDB.certified_at.desc().nullslast())
    )
    rows = result.all()

    out = []
    for wc, cert in rows:
        out.append(WorkerCertificationOut(
            id=wc.id,
            cert_id=wc.cert_id,
            task_type=cert.task_type,
            cert_name=cert.name,
            badge_icon=cert.badge_icon,
            score=wc.score,
            passed=wc.passed,
            attempt_count=wc.attempt_count,
            best_score=wc.best_score,
            certified_at=wc.certified_at,
            last_attempt_at=wc.last_attempt_at,
        ))
    return out


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_cert_by_type(task_type: str, db: AsyncSession) -> CertificationDB:
    result = await db.execute(
        select(CertificationDB).where(CertificationDB.task_type == task_type)
    )
    cert = result.scalar_one_or_none()
    if not cert:
        raise HTTPException(status_code=404, detail=f"No certification found for task type: {task_type}")
    return cert


def _check_answer(submitted, correct_answer, question_type: str) -> bool:
    """Check if a submitted answer is correct."""
    if submitted is None:
        return False

    if question_type == "single_choice":
        return str(submitted).strip().lower() == str(correct_answer).strip().lower()

    elif question_type == "multi_choice":
        if isinstance(submitted, str):
            submitted = [submitted]
        if isinstance(correct_answer, str):
            correct_answer = [correct_answer]
        return set(s.strip().lower() for s in submitted) == set(c.strip().lower() for c in correct_answer)

    elif question_type == "text_match":
        return str(submitted).strip().lower() == str(correct_answer).strip().lower()

    return False
