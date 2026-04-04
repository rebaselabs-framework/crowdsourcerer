"""Seed demo/tutorial tasks for new worker onboarding.

Creates a system requester account and populates the task feed with
tutorial-quality tasks across all 8 human task types.  Workers can
complete these to learn the platform, earn starter XP, and build
initial accuracy stats (gold-standard answers are included).

Follows the lazy-seed pattern used by marketplace templates and
certifications: seed once on first worker feed access, skip on
subsequent calls.
"""

import uuid
import logging
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import UserDB, TaskDB

logger = logging.getLogger(__name__)

# ── System requester account ──────────────────────────────────────────────

SYSTEM_USER_EMAIL = "system@crowdsourcerer.app"
SYSTEM_USER_NAME = "CrowdSorcerer Tutorials"

# ── Demo task definitions ─────────────────────────────────────────────────
# Each task is a realistic example with gold-standard answers so worker
# accuracy can be tracked from day one.

DEMO_TASKS: list[dict] = [
    # ── label_image ────────────────────────────────────────────────────
    {
        "type": "label_image",
        "input": {
            "image_url": "https://images.unsplash.com/photo-1574158622682-e40e69881006?w=600",
            "labels": ["cat", "dog", "bird", "other"],
            "description": "Classify the animal in this photo.",
        },
        "task_instructions": (
            "Tutorial: Image Classification\n\n"
            "Look at the image and select the label that best describes the "
            "main subject. If the image contains multiple subjects, choose "
            "the most prominent one. Click the label button, then hit Submit."
        ),
        "worker_reward_credits": 3,
        "gold_answer": {"label": "cat"},
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "label_image",
        "input": {
            "image_url": "https://images.unsplash.com/photo-1587300003388-59208cc962cb?w=600",
            "labels": ["cat", "dog", "bird", "other"],
            "description": "What animal is shown in this photo?",
        },
        "task_instructions": (
            "Tutorial: Image Classification\n\n"
            "Identify the animal in the image. Select exactly one label. "
            "Take your time -- accuracy matters more than speed on this platform."
        ),
        "worker_reward_credits": 3,
        "gold_answer": {"label": "dog"},
        "tags": ["demo", "tutorial", "beginner"],
    },

    # ── label_text ─────────────────────────────────────────────────────
    {
        "type": "label_text",
        "input": {
            "text": "I absolutely love this product! Best purchase I've made all year. The quality exceeded my expectations.",
            "categories": ["positive", "negative", "neutral"],
        },
        "task_instructions": (
            "Tutorial: Sentiment Classification\n\n"
            "Read the text carefully and classify its overall sentiment. "
            "Positive = happy, satisfied, recommending. "
            "Negative = unhappy, complaining, warning others. "
            "Neutral = factual, no strong emotion."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"label": "positive"},
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "label_text",
        "input": {
            "text": "The Federal Reserve announced a 0.25% interest rate cut today, citing concerns about slowing economic growth and rising unemployment figures.",
            "categories": ["politics", "business", "sports", "technology", "science"],
        },
        "task_instructions": (
            "Tutorial: Topic Classification\n\n"
            "Read the text and select the category that best describes its topic. "
            "Focus on the primary subject matter, not secondary mentions."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"label": "business"},
        "tags": ["demo", "tutorial", "beginner"],
    },

    # ── rate_quality ───────────────────────────────────────────────────
    {
        "type": "rate_quality",
        "input": {
            "title": "Product Description Quality",
            "content": (
                "Experience premium audio with our wireless headphones. Featuring "
                "40mm custom drivers, active noise cancellation, and 30-hour battery "
                "life. The ergonomic design ensures all-day comfort, while Bluetooth "
                "5.3 delivers seamless connectivity across all your devices."
            ),
            "criteria": "Rate the clarity, persuasiveness, and professionalism of this product description on a scale of 1-5.",
        },
        "task_instructions": (
            "Tutorial: Quality Rating\n\n"
            "Read the content and rate its quality using the star scale. Consider:\n"
            "- 1 star: Very poor, unclear, or misleading\n"
            "- 2 stars: Below average, needs significant improvement\n"
            "- 3 stars: Average, acceptable but not impressive\n"
            "- 4 stars: Good, clear and professional\n"
            "- 5 stars: Excellent, couldn't be better\n\n"
            "Optionally add a brief justification for your rating."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"rating": 4},
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "rate_quality",
        "input": {
            "title": "Article Summary Review",
            "content": "Climate change is bad. It makes things hot. We should fix it. The end.",
            "criteria": "Rate the depth, accuracy, and informativeness of this article summary.",
        },
        "task_instructions": (
            "Tutorial: Quality Rating\n\n"
            "Evaluate this content against the stated criteria. A low rating "
            "is appropriate when the content lacks depth or effort. Be honest "
            "in your assessment -- consistent, calibrated ratings build your "
            "reputation on the platform."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"rating": 2},
        "tags": ["demo", "tutorial", "beginner"],
    },

    # ── verify_fact ────────────────────────────────────────────────────
    {
        "type": "verify_fact",
        "input": {
            "claim": "The Great Wall of China is visible from space with the naked eye.",
            "context": "This is a popular claim often repeated in trivia. NASA astronauts have consistently reported that the Great Wall is not visible from low Earth orbit without aid.",
        },
        "task_instructions": (
            "Tutorial: Fact Verification\n\n"
            "Read the claim and any supporting context. Determine whether the "
            "claim is:\n"
            "- True: supported by evidence\n"
            "- False: contradicted by evidence\n"
            "- Can't tell: insufficient evidence either way\n\n"
            "Optionally provide a citation or source to support your verdict."
        ),
        "worker_reward_credits": 3,
        "gold_answer": {"verdict": "false"},
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "verify_fact",
        "input": {
            "claim": "Water boils at 100 degrees Celsius at standard atmospheric pressure.",
            "context": "Standard atmospheric pressure is defined as 101.325 kPa (1 atm). The boiling point of water varies with pressure.",
        },
        "task_instructions": (
            "Tutorial: Fact Verification\n\n"
            "Evaluate the claim using the context provided and your own knowledge. "
            "Select the appropriate verdict. Facts that are technically true under "
            "the stated conditions should be marked as True."
        ),
        "worker_reward_credits": 3,
        "gold_answer": {"verdict": "true"},
        "tags": ["demo", "tutorial", "beginner"],
    },

    # ── moderate_content ───────────────────────────────────────────────
    {
        "type": "moderate_content",
        "input": {
            "content": "Just finished a great hike in the mountains! The views were incredible and the weather was perfect. Highly recommend the Sunset Trail.",
            "content_type": "text",
            "policy_context": "Community guidelines: no harassment, hate speech, explicit content, spam, or personal attacks.",
        },
        "task_instructions": (
            "Tutorial: Content Moderation\n\n"
            "Review the content against the community guidelines. Choose:\n"
            "- Approve: content is safe and follows guidelines\n"
            "- Reject: content clearly violates guidelines\n"
            "- Escalate: borderline or needs expert review\n\n"
            "Provide a brief reason for your decision. When in doubt, escalate."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"decision": "approve"},
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "moderate_content",
        "input": {
            "content": "BUY NOW!!! Amazing weight loss pills! Lose 50 pounds in 1 week GUARANTEED! Click here: http://totallylegit.biz FREE TRIAL!!!",
            "content_type": "text",
            "policy_context": "Community guidelines prohibit spam, misleading health claims, deceptive marketing, and suspicious external links.",
        },
        "task_instructions": (
            "Tutorial: Content Moderation\n\n"
            "Evaluate whether this content violates the stated policies. "
            "Look for: spam patterns (ALL CAPS, excessive punctuation), "
            "misleading claims, suspicious links, and deceptive marketing."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"decision": "reject"},
        "tags": ["demo", "tutorial", "beginner"],
    },

    # ── compare_rank ───────────────────────────────────────────────────
    {
        "type": "compare_rank",
        "input": {
            "items": [
                "Our hand-crafted leather wallet is made from premium Italian calfskin, featuring 8 card slots, RFID blocking technology, and a slim profile that fits comfortably in any pocket.",
                "Wallet. Good quality. Holds cards and cash. Brown color available.",
            ],
            "criteria": "Which product description is more compelling and likely to drive sales?",
        },
        "task_instructions": (
            "Tutorial: Comparison Ranking\n\n"
            "Read both options carefully and select which one better meets "
            "the stated criterion. If they're roughly equal, you can select "
            "'About equal'. Optionally explain your reasoning."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"choice": "a"},
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "compare_rank",
        "input": {
            "items": [
                "Scientists Discover New Species in Deep Ocean Trench",
                "You Won't BELIEVE What Scientists Found at the Bottom of the Ocean!!!",
            ],
            "criteria": "Which headline is more professional and trustworthy for a news publication?",
        },
        "task_instructions": (
            "Tutorial: Comparison Ranking\n\n"
            "Compare the two options against the criterion. Consider tone, "
            "professionalism, accuracy, and reader trust. Clickbait patterns "
            "(ALL CAPS, excessive punctuation, vague promises) reduce trust."
        ),
        "worker_reward_credits": 2,
        "gold_answer": {"choice": "a"},
        "tags": ["demo", "tutorial", "beginner"],
    },

    # ── answer_question ────────────────────────────────────────────────
    {
        "type": "answer_question",
        "input": {
            "content": (
                "Python is a high-level, general-purpose programming language created "
                "by Guido van Rossum and first released in 1991. It emphasizes code "
                "readability with its use of significant indentation. Python supports "
                "multiple programming paradigms, including procedural, object-oriented, "
                "and functional programming."
            ),
            "question": "Who created Python and when was it first released?",
            "answer_format": "free_text",
        },
        "task_instructions": (
            "Tutorial: Question Answering\n\n"
            "Read the provided content and answer the question based on what "
            "you find. Your answer should be:\n"
            "- Accurate: based on the text, not assumptions\n"
            "- Concise: answer the question directly\n"
            "- Complete: include all relevant details from the text\n\n"
            "Minimum 5 characters required."
        ),
        "worker_reward_credits": 4,
        "gold_answer": {"answer": "Python was created by Guido van Rossum and first released in 1991."},
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "answer_question",
        "input": {
            "content": (
                "Photosynthesis is the process by which green plants and some other "
                "organisms use sunlight to synthesize foods from carbon dioxide and "
                "water. The process primarily takes place in the chloroplasts of plant "
                "cells. It generates oxygen as a byproduct, which is released into "
                "the atmosphere."
            ),
            "question": "What is the main byproduct of photosynthesis?",
            "answer_format": "multiple_choice",
            "choices": ["Carbon dioxide", "Oxygen", "Nitrogen", "Hydrogen"],
        },
        "task_instructions": (
            "Tutorial: Multiple Choice\n\n"
            "Read the passage and select the correct answer from the choices. "
            "For multiple-choice questions, type the exact answer text in the "
            "response field."
        ),
        "worker_reward_credits": 4,
        "gold_answer": {"answer": "Oxygen"},
        "tags": ["demo", "tutorial", "beginner"],
    },

    # ── transcription_review ───────────────────────────────────────────
    {
        "type": "transcription_review",
        "input": {
            "audio_url": "",
            "ai_transcript": (
                "The whether today is going to be partly cloudy with a hi of "
                "seventy too degrees. Their is a chance of reign later this "
                "evening, so bring an umbrela just in case."
            ),
            "language": "en",
        },
        "task_instructions": (
            "Tutorial: Transcript Correction\n\n"
            "The AI-generated transcript below contains errors. Your job is to:\n"
            "1. Fix spelling mistakes (e.g., 'whether' -> 'weather')\n"
            "2. Fix homophones (e.g., 'their' -> 'there')\n"
            "3. Fix number formatting (e.g., 'seventy too' -> 'seventy-two')\n"
            "4. Preserve the original meaning and structure\n\n"
            "Edit the text in the textarea and submit your corrected version."
        ),
        "worker_reward_credits": 5,
        "gold_answer": {
            "text": (
                "The weather today is going to be partly cloudy with a high of "
                "seventy-two degrees. There is a chance of rain later this "
                "evening, so bring an umbrella just in case."
            ),
        },
        "tags": ["demo", "tutorial", "beginner"],
    },
    {
        "type": "transcription_review",
        "input": {
            "audio_url": "",
            "ai_transcript": (
                "To install the package, run pip install num pie. Then import "
                "it with import numpie as np. You can create a raise using "
                "np dot a range of ten."
            ),
            "language": "en",
        },
        "task_instructions": (
            "Tutorial: Technical Transcript Correction\n\n"
            "This transcript contains technical terms that the AI misheard. "
            "Common patterns to watch for:\n"
            "- Package names spelled phonetically\n"
            "- Programming keywords misinterpreted\n"
            "- Code syntax described as regular words\n\n"
            "Correct the transcript to accurately reflect the technical content."
        ),
        "worker_reward_credits": 5,
        "gold_answer": {
            "text": (
                "To install the package, run pip install numpy. Then import "
                "it with import numpy as np. You can create an array using "
                "np.arange(10)."
            ),
        },
        "tags": ["demo", "tutorial", "beginner"],
    },
]


async def _get_or_create_system_user(db: AsyncSession) -> UserDB:
    """Return the system tutorial requester, creating it if needed."""
    result = await db.execute(
        select(UserDB).where(UserDB.email == SYSTEM_USER_EMAIL)
    )
    user = result.scalar_one_or_none()
    if user:
        return user

    user = UserDB(
        id=uuid.uuid4(),
        email=SYSTEM_USER_EMAIL,
        name=SYSTEM_USER_NAME,
        password_hash=None,       # Cannot login -- system account
        plan="enterprise",
        role="requester",
        credits=100_000,          # Generous pool for demo tasks
        is_active=True,
        is_admin=False,
    )
    db.add(user)
    await db.flush()
    logger.info("system_tutorial_user_created", user_id=str(user.id))
    return user


async def seed_demo_tasks(db: AsyncSession) -> None:
    """Seed demo/tutorial tasks if none exist yet.

    Follows the lazy-seed pattern: checks for existing demo tasks via the
    system user's task count, returns immediately if already seeded.
    Otherwise creates one task per entry in DEMO_TASKS with high
    ``assignments_required`` so many workers can practice on the same task.
    """
    # Fast check: does the system tutorial user already have tasks?
    result = await db.execute(
        select(UserDB).where(UserDB.email == SYSTEM_USER_EMAIL)
    )
    system_user = result.scalar_one_or_none()
    if system_user:
        task_count = await db.scalar(
            select(func.count()).select_from(TaskDB).where(
                TaskDB.user_id == system_user.id
            )
        )
        if task_count and task_count > 0:
            return  # Already seeded
    else:
        system_user = await _get_or_create_system_user(db)
    now = datetime.now(timezone.utc)

    for task_def in DEMO_TASKS:
        task = TaskDB(
            id=uuid.uuid4(),
            user_id=system_user.id,
            type=task_def["type"],
            status="open",
            priority="normal",
            execution_mode="human",
            input=task_def["input"],
            task_instructions=task_def["task_instructions"],
            worker_reward_credits=task_def["worker_reward_credits"],
            assignments_required=1000,     # Many workers can practice
            assignments_completed=0,
            claim_timeout_minutes=60,      # Generous timeout for tutorials
            is_gold_standard=True,
            gold_answer=task_def["gold_answer"],
            consensus_strategy="any_first",
            tags=task_def["tags"],
            created_at=now,
        )
        db.add(task)

    await db.commit()
    logger.info("demo_tasks_seeded", count=len(DEMO_TASKS))
