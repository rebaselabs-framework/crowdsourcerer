"""Worker Skill Assessment — mini-quiz to auto-set proficiency level."""
from __future__ import annotations

import random
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import SkillQuizQuestionDB, SkillQuizAttemptDB, WorkerSkillDB
from models.schemas import (
    SkillQuizQuestionOut, SkillQuizSubmitRequest,
    SkillQuizResultOut, SkillQuizAttemptOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/worker/skill-quiz", tags=["skill-quiz"])

PASS_THRESHOLD = 0.6   # 60% to pass
QUESTIONS_PER_QUIZ = 10
CREDITS_FOR_PASSING = 25  # bonus credits on first pass

# ── Supported categories (matches task types + general skill areas) ──────────
SKILL_CATEGORIES = [
    "label_image",
    "label_text",
    "moderate_content",
    "transcription_review",
    "verify_fact",
    "answer_question",
    "data_entry",
    "rate_quality",
    "compare_rank",
]

# ── Seed questions ────────────────────────────────────────────────────────────
# Built-in static questions seeded on first use
SEED_QUESTIONS: dict[str, list[dict]] = {
    "label_image": [
        {"q": "What is the primary purpose of image labeling in ML datasets?", "opts": ["Compressing images", "Providing ground-truth annotations for model training", "Enhancing image resolution", "Converting images to text"], "a": 1, "d": 1, "exp": "Labels provide the ground truth that supervised ML models learn from."},
        {"q": "When labeling bounding boxes, you should:", "opts": ["Draw boxes as large as possible", "Tightly fit the box around the object with minimal background", "Only label the most prominent object", "Ignore partially visible objects"], "a": 1, "d": 1, "exp": "Tight bounding boxes reduce noise and improve model accuracy."},
        {"q": "Which of the following is an example of a classification label?", "opts": ["A polygon outlining an object", "A bounding box coordinate", "The category 'cat' assigned to an image", "A depth map"], "a": 2, "d": 1, "exp": "Classification assigns a category label to an entire image or region."},
        {"q": "When you are unsure about a label, the best approach is:", "opts": ["Skip the task entirely", "Guess randomly", "Mark it as uncertain or use the 'other' category per guidelines", "Apply the most common label"], "a": 2, "d": 2, "exp": "Marking uncertainty maintains dataset quality; random guessing corrupts training data."},
        {"q": "What does 'inter-annotator agreement' measure?", "opts": ["How fast annotators work", "The consistency of labels across different annotators", "The number of labels per image", "Image resolution quality"], "a": 1, "d": 2, "exp": "High agreement means annotations are reliable and consistent."},
        {"q": "A 'polygon annotation' is used when:", "opts": ["The object is rectangular", "You need pixel-precise boundaries for irregular shapes", "You want to label multiple objects at once", "Speed is more important than precision"], "a": 1, "d": 2, "exp": "Polygons trace the exact outline of irregular shapes for segmentation tasks."},
        {"q": "Which quality issue most harms image labeling datasets?", "opts": ["Having too many annotators", "Label inconsistency across similar images", "Using high-resolution images", "Including diverse image sources"], "a": 1, "d": 2, "exp": "Inconsistent labels confuse models and reduce performance."},
        {"q": "When a task guideline says 'label all occurrences', you should:", "opts": ["Label only the largest instance", "Label every visible instance of the target class", "Label only instances in the foreground", "Skip the task if there are too many"], "a": 1, "d": 1, "exp": "All occurrences must be labeled to avoid false negatives in training."},
        {"q": "What is 'semantic segmentation'?", "opts": ["Labeling every pixel with a class", "Drawing bounding boxes", "Transcribing image text", "Classifying the overall image"], "a": 0, "d": 3, "exp": "Semantic segmentation assigns a class label to every pixel in an image."},
        {"q": "If an object is more than 50% occluded, you should:", "opts": ["Never label it", "Always label it", "Follow the specific task guidelines", "Label it only if you can infer the full shape"], "a": 2, "d": 2, "exp": "Task-specific guidelines take precedence; they define occlusion handling."},
        {"q": "The term 'label taxonomy' refers to:", "opts": ["The history of labeling tools", "The defined hierarchy and set of valid label classes", "An automated labeling system", "The annotation file format"], "a": 1, "d": 2, "exp": "A taxonomy defines the valid classes and their relationships for a project."},
        {"q": "What is a 'key point' annotation used for?", "opts": ["Marking the center of a bounding box", "Marking specific body joints or landmarks", "Outlining the full object boundary", "Categorizing the image"], "a": 1, "d": 3, "exp": "Key point annotations mark specific landmarks like body joints for pose estimation."},
    ],
    "label_text": [
        {"q": "In sentiment analysis labeling, 'neutral' typically means:", "opts": ["The text is positive", "The text expresses neither clear positive nor negative sentiment", "The text contains profanity", "The labeler is unsure"], "a": 1, "d": 1, "exp": "Neutral indicates absence of strong sentiment polarity."},
        {"q": "Named Entity Recognition (NER) involves labeling:", "opts": ["Overall document sentiment", "Specific entities like people, places, and organizations in text", "Grammar errors", "Translation quality"], "a": 1, "d": 1, "exp": "NER tags specific mention spans by entity type."},
        {"q": "When labeling text for 'intent classification', you are categorizing:", "opts": ["The author's writing style", "The purpose or goal behind a message", "The language of the text", "The emotion of the reader"], "a": 1, "d": 1, "exp": "Intent classification identifies what action or goal a user's message represents."},
        {"q": "What is 'span labeling' in text annotation?", "opts": ["Labeling the whole document", "Marking a specific contiguous sequence of words", "Translating text", "Scoring text quality"], "a": 1, "d": 2, "exp": "A span is a contiguous substring; span labeling marks its start and end position."},
        {"q": "Why is 'label schema consistency' important in text annotation?", "opts": ["It speeds up annotation", "It ensures all annotators apply labels by the same definitions", "It reduces file sizes", "It improves text readability"], "a": 1, "d": 1, "exp": "Consistent schemas ensure labels mean the same thing across the dataset."},
        {"q": "When text contains irony or sarcasm, a best practice is:", "opts": ["Always label as positive", "Always label as negative", "Follow the annotation guidelines, which may have an 'irony' flag", "Skip the sample"], "a": 2, "d": 2, "exp": "Guidelines handle edge cases like sarcasm explicitly; follow them rather than guessing."},
        {"q": "In relation extraction, you are labeling:", "opts": ["Sentence structure", "Relationships between entities in text (e.g., 'works_for')", "Document topics", "Word frequencies"], "a": 1, "d": 2, "exp": "Relation extraction identifies semantic relationships between named entities."},
        {"q": "What is 'token classification'?", "opts": ["Sorting documents by topic", "Assigning a label to each individual word/token", "Counting tokens in a document", "Classifying the whole text"], "a": 1, "d": 2, "exp": "Token classification labels each token independently, used in NER and POS tagging."},
        {"q": "Coreference resolution labeling involves:", "opts": ["Finding grammar mistakes", "Linking pronouns and noun phrases that refer to the same entity", "Translating between languages", "Measuring sentence length"], "a": 1, "d": 3, "exp": "Coreference resolution tracks which mentions refer to the same real-world entity."},
        {"q": "The best way to handle ambiguous text labels is:", "opts": ["Pick the first option", "Flag for review per the task guidelines", "Flip a coin", "Always pick the most common label"], "a": 1, "d": 1, "exp": "Ambiguous cases should be flagged so they can be resolved by supervisors."},
        {"q": "What does 'Cohen's Kappa' measure in annotation?", "opts": ["Annotation speed", "Inter-annotator agreement corrected for chance", "Text complexity", "Label count"], "a": 1, "d": 3, "exp": "Cohen's Kappa is a standard metric for measuring annotator agreement above chance."},
        {"q": "When a sentence is too short to determine sentiment, you should:", "opts": ["Label it as positive", "Mark it as neutral or insufficient context per guidelines", "Skip it", "Split it into parts"], "a": 1, "d": 2, "exp": "Short texts without enough signal should be labeled per the guideline's 'insufficient context' rule."},
    ],
    "moderate_content": [
        {"q": "Content moderation's primary goal is:", "opts": ["Maximizing user engagement", "Keeping platforms safe by removing harmful content", "Increasing content volume", "Reducing server costs"], "a": 1, "d": 1, "exp": "Moderation protects communities from harmful, illegal, or policy-violating content."},
        {"q": "When you encounter content you find personally offensive but it doesn't violate policy, you should:", "opts": ["Remove it immediately", "Leave it and apply no action per policy guidelines", "Escalate to a supervisor", "Report it as spam"], "a": 1, "d": 2, "exp": "Personal distaste ≠ policy violation; apply the platform's rules, not your own preferences."},
        {"q": "Which of the following is typically considered 'hate speech'?", "opts": ["Criticism of a government policy", "Content that attacks people based on protected characteristics", "Negative product reviews", "Political debate"], "a": 1, "d": 1, "exp": "Hate speech targets individuals or groups based on protected characteristics."},
        {"q": "In a tiered moderation system, 'escalation' means:", "opts": ["Deleting content immediately", "Banning the user automatically", "Sending a case to a higher-level reviewer for a final decision", "Adding a warning label"], "a": 2, "d": 2, "exp": "Escalation passes ambiguous or severe cases to specialists or senior moderators."},
        {"q": "CSAM (Child Sexual Abuse Material) should be:", "opts": ["Removed with a warning to the user", "Reviewed carefully before removing", "Immediately removed and reported per legal requirements", "Left visible with an age-gate"], "a": 2, "d": 1, "exp": "CSAM must be immediately removed and reported to authorities; it has zero tolerance."},
        {"q": "What is a 'false positive' in content moderation?", "opts": ["Correctly removing harmful content", "Removing content that did NOT violate policy", "Missing harmful content", "Approving all content"], "a": 1, "d": 2, "exp": "A false positive is wrongly removing benign content — it harms the user experience."},
        {"q": "Context is important in moderation because:", "opts": ["It slows down decisions", "The same words can be harmful or benign depending on context", "Platform rules never consider context", "Context only matters for images"], "a": 1, "d": 2, "exp": "Context (satire, education, news reporting) can make otherwise problematic content acceptable."},
        {"q": "When moderating graphic violence, which factor is LEAST relevant?", "opts": ["Whether it glorifies violence", "The platform's specific policy on violence", "The file size of the content", "Whether it was posted in a relevant community"], "a": 2, "d": 2, "exp": "File size has no bearing on whether content violates policy."},
        {"q": "What is 'misinformation' in a content moderation context?", "opts": ["Satirical content", "False or misleading claims presented as factual", "Opinion pieces", "Content in foreign languages"], "a": 1, "d": 1, "exp": "Misinformation is false information presented as fact, distinct from satire or opinion."},
        {"q": "Moderator wellbeing programs exist because:", "opts": ["Moderators don't need training", "Exposure to harmful content causes psychological harm", "Moderation is easy work", "Moderators prefer working alone"], "a": 1, "d": 2, "exp": "Regular exposure to graphic or disturbing content causes vicarious trauma."},
        {"q": "A 'shadow ban' is best described as:", "opts": ["Permanently deleting an account", "Making a user's content invisible to others without notifying them", "Suspending an account temporarily", "Sending a policy warning"], "a": 1, "d": 2, "exp": "A shadow ban reduces a user's reach without explicit notification."},
        {"q": "Which action is MOST appropriate for spam content?", "opts": ["Leave it with a warning label", "Remove and apply rate limiting or ban", "Escalate to legal", "Add a disclaimer"], "a": 1, "d": 1, "exp": "Spam violates most platform policies and is removed; repeat offenders are rate-limited or banned."},
    ],
    "verify_fact": [
        {"q": "When verifying a factual claim, the BEST type of source is:", "opts": ["A popular blog post", "A primary source or peer-reviewed research", "A social media post with many likes", "A Wikipedia article"], "a": 1, "d": 1, "exp": "Primary sources and peer-reviewed research provide the highest evidential quality."},
        {"q": "A claim is 'unverifiable' when:", "opts": ["You can't find it with a quick search", "There are no reliable sources to confirm or deny it", "It seems implausible", "It contradicts your prior beliefs"], "a": 1, "d": 2, "exp": "Unverifiable means insufficient evidence exists — not just hard to find."},
        {"q": "What is 'source triangulation'?", "opts": ["Using one authoritative source", "Confirming a claim using multiple independent sources", "Citing the most popular source", "Checking a claim against its original social media post"], "a": 1, "d": 2, "exp": "Triangulating across independent sources increases confidence in accuracy."},
        {"q": "When a fact-check task says 'provide evidence URL', you should:", "opts": ["Link to any web page mentioning the claim", "Link to a reliable source directly supporting your verdict", "Provide a search engine results page", "Skip if you believe the claim is obvious"], "a": 1, "d": 1, "exp": "Evidence must directly support the verdict using a reliable, citable source."},
        {"q": "Which of these is a red flag for an unreliable source?", "opts": ["A .gov domain", "Clear author attribution with credentials", "No publication date and sensational headlines", "Links to primary research"], "a": 2, "d": 1, "exp": "Missing dates and sensationalism are common signs of low-quality or misleading sources."},
        {"q": "A claim says 'X always causes Y.' The most accurate fact-check result is:", "opts": ["True if any study shows a correlation", "Likely false or misleading — 'always' is an absolute claim requiring extraordinary evidence", "True", "Unverifiable"], "a": 1, "d": 2, "exp": "Absolute claims ('always', 'never') are rarely supportable and should be carefully checked."},
        {"q": "What does 'cherry-picking' mean in the context of misinformation?", "opts": ["Selecting the best sources", "Selectively using evidence that supports one conclusion while ignoring contradictory evidence", "Verifying multiple claims at once", "Using recent data"], "a": 1, "d": 2, "exp": "Cherry-picking creates a misleading impression by omitting contradictory evidence."},
        {"q": "If a claim is technically true but highly misleading, it should be rated:", "opts": ["Fully true", "False", "Misleading or missing context, per task guidelines", "Unverifiable"], "a": 2, "d": 2, "exp": "Technically true but misleading content is distinct from clearly false claims; guidelines have specific ratings for this."},
        {"q": "Which domain type is generally MOST reliable for scientific facts?", "opts": [".com", ".org", ".edu or .gov", ".net"], "a": 2, "d": 1, "exp": ".edu (educational institutions) and .gov (government agencies) are generally more authoritative for factual claims."},
        {"q": "When a source is behind a paywall, you should:", "opts": ["Skip it entirely", "Mark the claim as unverifiable", "Try to find an abstract, preprint, or alternative access method before concluding", "Assume it confirms the claim"], "a": 2, "d": 2, "exp": "Abstracts, preprints (e.g., arXiv), or library access can often provide the information needed."},
        {"q": "What is 'recency bias' in fact-checking?", "opts": ["Preferring older, more established sources", "Over-weighting recent information even if older evidence is more reliable", "Only checking news from this year", "Ignoring historical context"], "a": 1, "d": 3, "exp": "Recent doesn't always mean more accurate; established consensus often outweighs a single new study."},
        {"q": "A 'satire' site publishes a fake news story. Someone shares it as real. The claim is:", "opts": ["True because it was published", "False — it originates from satire", "Unverifiable", "Misleading — context shows it is satire"], "a": 3, "d": 2, "exp": "Satire presented without satirical context becomes misleading rather than outright false."},
    ],
    "answer_question": [
        {"q": "When answering a customer question, the first priority is:", "opts": ["Answering as quickly as possible", "Understanding the exact question being asked", "Providing the longest possible answer", "Using technical terminology"], "a": 1, "d": 1, "exp": "Comprehension precedes response — misunderstanding the question leads to unhelpful answers."},
        {"q": "If you don't know the answer to a question, the best response is:", "opts": ["Make up a plausible-sounding answer", "Acknowledge you don't know and direct to appropriate resources", "Refuse to answer", "Copy-paste from an unverified source"], "a": 1, "d": 1, "exp": "Honesty maintains trust; fabricating answers causes harm and damages credibility."},
        {"q": "A 'follow-up question' in a Q&A task is most useful when:", "opts": ["You want to extend the conversation", "The original question is ambiguous and clarification is needed", "You want to show expertise", "The question is simple"], "a": 1, "d": 2, "exp": "Clarifying questions reduce the risk of answering the wrong question."},
        {"q": "What makes a good answer to a complex question?", "opts": ["It is brief and uses jargon", "It is structured, accurate, and written at the appropriate level for the audience", "It covers every possible related topic", "It matches the question's exact wording"], "a": 1, "d": 2, "exp": "Good answers are accurate, well-organized, and appropriately scoped for the reader."},
        {"q": "Citing sources in an answer is important because:", "opts": ["It makes the answer look longer", "It allows the reader to verify the information", "It is required by all platforms", "It hides uncertainty"], "a": 1, "d": 1, "exp": "Citations enable verification and increase trust in the answer."},
        {"q": "What is 'answer confidence calibration'?", "opts": ["Writing more confident-sounding answers", "Matching expressed confidence levels to actual certainty about the answer", "Always saying you are certain", "Refusing to answer uncertain questions"], "a": 1, "d": 2, "exp": "Well-calibrated answers signal certainty appropriately — not over- or under-confident."},
        {"q": "For a medical question, the safest approach is:", "opts": ["Provide a specific diagnosis", "Give general information and recommend consulting a healthcare professional", "Refuse to answer any health questions", "Copy-paste from WebMD without context"], "a": 1, "d": 1, "exp": "Medical questions require disclaimers and referrals to professionals for specific advice."},
        {"q": "The term 'scope creep' in answering refers to:", "opts": ["Making answers shorter", "Expanding beyond the question into tangentially related topics", "Repeating the question", "Using bullet points"], "a": 1, "d": 2, "exp": "Scope creep dilutes the answer and frustrates readers seeking specific information."},
        {"q": "What is the most appropriate tone for a professional Q&A task?", "opts": ["Casual and humorous", "Clear, respectful, and neutral", "Formal and legalistic", "Enthusiastic and promotional"], "a": 1, "d": 1, "exp": "Professional Q&A uses a clear, respectful, neutral tone appropriate to the platform."},
        {"q": "When an answer might be outdated, you should:", "opts": ["Present it as current fact", "Mention the date of the information and note it may have changed", "Remove the answer entirely", "Ask the user to search elsewhere"], "a": 1, "d": 2, "exp": "Disclosing the information date lets readers assess freshness for themselves."},
        {"q": "What does 'hallucination' mean in the context of AI-assisted answer generation?", "opts": ["Very creative writing", "Generating plausible-sounding but factually incorrect information", "Answering too quickly", "Refusing to answer"], "a": 1, "d": 2, "exp": "Hallucination is a critical failure mode where AI generates confident but false information."},
        {"q": "The key difference between 'summarizing' and 'paraphrasing' an answer is:", "opts": ["There is no difference", "Summarizing condenses content; paraphrasing rewrites at roughly the same length in different words", "Paraphrasing always shortens", "Summarizing changes the meaning"], "a": 1, "d": 2, "exp": "Summary reduces length; paraphrase reformulates without necessarily shortening."},
    ],
    "rate_quality": [
        {"q": "When rating output quality, the most important factor is:", "opts": ["Your personal aesthetic preference", "Whether the output meets the defined task requirements and quality rubric", "How long it took to produce", "Whether you would have done it differently"], "a": 1, "d": 1, "exp": "Ratings must be based on defined criteria, not personal preference."},
        {"q": "A '5-point Likert scale' in quality rating means:", "opts": ["Five yes/no questions", "A scale from 1 (lowest) to 5 (highest) measuring degree of agreement", "A binary pass/fail rating", "Rating five separate dimensions"], "a": 1, "d": 1, "exp": "Likert scales measure the degree of a quality on a numeric range."},
        {"q": "What is 'inter-rater reliability' in quality assessment?", "opts": ["How fast raters work", "The degree to which different raters give the same ratings to the same items", "Whether raters enjoy the work", "The number of items rated"], "a": 1, "d": 2, "exp": "High inter-rater reliability means ratings are consistent and reproducible across raters."},
        {"q": "Rating 'fluency' in a text output means assessing:", "opts": ["Factual accuracy", "Whether the text reads naturally and is grammatically correct", "Topic relevance", "Formatting correctness"], "a": 1, "d": 1, "exp": "Fluency refers to the naturalness and grammatical correctness of the text."},
        {"q": "What is 'anchoring bias' in quality rating?", "opts": ["Using the highest rating for all items", "Being overly influenced by the first item seen when rating subsequent items", "Rating items in alphabetical order", "Preferring shorter outputs"], "a": 1, "d": 2, "exp": "Anchoring bias occurs when early examples set an implicit standard that skews later ratings."},
        {"q": "When a rubric says 'rate coherence', you are evaluating:", "opts": ["Grammar and spelling", "Whether ideas flow logically and the output makes sense as a whole", "The number of paragraphs", "Factual correctness"], "a": 1, "d": 2, "exp": "Coherence measures logical structure and flow between ideas."},
        {"q": "A 'calibration session' before a rating task helps:", "opts": ["Make ratings faster", "Align raters on how to interpret the rubric through examples", "Reduce the number of items to rate", "Eliminate the need for guidelines"], "a": 1, "d": 2, "exp": "Calibration aligns raters' interpretation of the scale, improving consistency."},
        {"q": "What is 'central tendency bias' in rating?", "opts": ["Always giving extreme ratings", "Avoiding extreme ratings and clustering around the middle of the scale", "Rating items at random", "Giving the same rating to all items"], "a": 1, "d": 2, "exp": "Central tendency bias makes raters avoid the extremes, compressing the range of ratings."},
        {"q": "You are rating relevance of an answer to a question. If the answer is fluent but off-topic, you should:", "opts": ["Rate relevance high because the answer is well-written", "Rate relevance low because it doesn't address the question", "Skip the item", "Rate both fluency and relevance as medium"], "a": 1, "d": 1, "exp": "Fluency and relevance are separate dimensions; off-topic content is low relevance regardless of fluency."},
        {"q": "The purpose of a 'rubric' in quality rating is:", "opts": ["To limit the number of ratings per day", "To provide specific criteria and examples that define each rating level", "To speed up rating by reducing options", "To automatically generate ratings"], "a": 1, "d": 1, "exp": "Rubrics define exactly what each rating level means, enabling consistent evaluation."},
        {"q": "'Completeness' as a quality dimension measures:", "opts": ["Whether the output is short", "Whether the output addresses all required aspects of the task", "Grammar correctness", "Formatting quality"], "a": 1, "d": 1, "exp": "Completeness checks that the output covers everything the task required."},
        {"q": "What should you do if you feel fatigued during a long rating session?", "opts": ["Continue at the same pace to maintain throughput", "Take a break to maintain rating accuracy", "Start giving higher ratings to finish faster", "Skip difficult items"], "a": 1, "d": 1, "exp": "Fatigue degrades rating consistency; breaks maintain quality throughout the session."},
    ],
    "compare_rank": [
        {"q": "In a pairwise comparison task, you are asked to:", "opts": ["Rate each item individually", "Choose which of two items better meets a criterion", "Sort all items from best to worst", "Find errors in items"], "a": 1, "d": 1, "exp": "Pairwise comparison focuses on relative quality between exactly two items."},
        {"q": "What is 'transitivity' in a ranking system?", "opts": ["Ranking items randomly", "If A > B and B > C, then A > C must hold", "All items receiving the same rank", "Swapping A and B positions"], "a": 1, "d": 2, "exp": "Transitive rankings are logically consistent — the same ordering holds across all comparisons."},
        {"q": "When comparing two AI-generated responses for helpfulness, you should:", "opts": ["Choose the longer one", "Choose whichever you personally find more interesting", "Choose the one that better addresses the user's actual need", "Choose the one with fewer words"], "a": 2, "d": 1, "exp": "Helpfulness is about meeting the user's need, not length or personal interest."},
        {"q": "A 'tie' in a comparison task is appropriate when:", "opts": ["You can't decide quickly", "Both items are genuinely equivalent on the criterion being evaluated", "You want to finish faster", "The items are in different formats"], "a": 1, "d": 2, "exp": "Ties should only be used when both options are truly equivalent — not to avoid difficult decisions."},
        {"q": "What is the main advantage of ranking over absolute rating?", "opts": ["It is faster", "It is easier to be consistent because comparisons are relative", "It produces more data points", "It doesn't require guidelines"], "a": 1, "d": 2, "exp": "Relative judgments are more reliable than absolute ones because they avoid scale interpretation issues."},
        {"q": "When comparing creative writing samples, 'originality' refers to:", "opts": ["Length", "How different it is from typical/expected responses", "Grammar correctness", "Use of metaphors"], "a": 1, "d": 2, "exp": "Originality measures how novel and unexpected the content is relative to common outputs."},
        {"q": "If one response is accurate but dull and another is engaging but slightly inaccurate, and the criterion is 'overall quality', you should:", "opts": ["Always pick accuracy over engagement", "Always pick engagement over accuracy", "Weigh both dimensions per the task rubric", "Skip the item"], "a": 2, "d": 2, "exp": "Multi-dimension quality trade-offs should be resolved by the task rubric's weighting."},
        {"q": "What is 'preference data' in RLHF (Reinforcement Learning from Human Feedback)?", "opts": ["User click data", "Human rankings of AI outputs used to train a reward model", "Automatically generated rankings", "Error annotations in model outputs"], "a": 1, "d": 3, "exp": "RLHF uses human preference comparisons to train reward models that guide AI alignment."},
        {"q": "When completing ranking tasks, maintaining consistency means:", "opts": ["Always ranking the first item higher", "Applying the same criteria in the same way across all comparisons", "Varying your criteria to cover more dimensions", "Rating quickly without overthinking"], "a": 1, "d": 1, "exp": "Consistency requires applying the same rubric uniformly across all items."},
        {"q": "You notice a preference for longer responses in your rankings. This is a sign of:", "opts": ["Good judgment", "'Verbosity bias' — a tendency to rate more verbose outputs higher regardless of quality", "Appropriate weighting", "Correct application of the rubric"], "a": 1, "d": 2, "exp": "Verbosity bias incorrectly equates length with quality; the rubric should guide selection."},
        {"q": "The purpose of human ranking data in AI training is:", "opts": ["To replace automated evaluation entirely", "To capture nuanced human preferences that metrics cannot measure", "To reduce training costs", "To provide faster feedback than automated systems"], "a": 1, "d": 2, "exp": "Human preferences capture subtle quality dimensions that automated metrics miss."},
        {"q": "What is 'position bias' in A/B comparison tasks?", "opts": ["Rating items based on their alphabetical name", "Systematically preferring whichever response appears first or second", "Comparing items from different categories", "Preferring shorter responses"], "a": 1, "d": 2, "exp": "Position bias is the tendency to favor the first (or second) response regardless of quality."},
    ],
    "transcription_review": [
        {"q": "When reviewing a transcription, 'verbatim accuracy' means:", "opts": ["Summarizing what was said", "The transcript exactly matches the spoken words, including filler words", "Correcting grammar in the transcript", "Adding punctuation where it sounds natural"], "a": 1, "d": 1, "exp": "Verbatim transcription captures every spoken word exactly as uttered."},
        {"q": "In a 'clean transcription', filler words like 'um' and 'uh' are:", "opts": ["Always included", "Removed unless they carry meaning", "Replaced with ellipses", "Highlighted in brackets"], "a": 1, "d": 2, "exp": "Clean transcription omits disfluencies; verbatim includes them."},
        {"q": "When a speaker's words are inaudible, the correct notation is typically:", "opts": ["Leaving a blank space", "Writing [inaudible] or [unclear]", "Guessing what was said", "Skipping that section entirely"], "a": 1, "d": 1, "exp": "Standard notation flags inaudible sections so reviewers know it wasn't omitted accidentally."},
        {"q": "Timestamps in a transcript serve to:", "opts": ["Add word count", "Allow listeners to locate specific moments in the audio", "Indicate speaker changes only", "Note audio quality issues"], "a": 1, "d": 1, "exp": "Timestamps synchronize the transcript to the media, enabling navigation."},
        {"q": "When multiple speakers overlap, the best approach is:", "opts": ["Only transcribe the loudest speaker", "Use speaker labels and indicate the overlap", "Combine their words into one utterance", "Mark the section as inaudible"], "a": 1, "d": 2, "exp": "Speaker labels maintain attribution; overlapping speech should be noted for clarity."},
        {"q": "What does '[crosstalk]' indicate in a transcript?", "opts": ["Technical audio noise", "Multiple speakers talking simultaneously, making individual words unclear", "A speaker pause", "Background music"], "a": 1, "d": 2, "exp": "[crosstalk] flags simultaneous speech that cannot be individually transcribed."},
        {"q": "Proper nouns in transcripts (names, brands) should be:", "opts": ["Left lowercase always", "Verified and capitalized correctly", "Spelled phonetically", "Placed in brackets"], "a": 1, "d": 1, "exp": "Proper nouns require correct capitalization and spelling for accuracy."},
        {"q": "Speaker diarization refers to:", "opts": ["Correcting grammar in transcripts", "The process of identifying and labeling who is speaking at each moment", "Removing filler words", "Adding punctuation"], "a": 1, "d": 2, "exp": "Diarization segments audio by speaker identity."},
        {"q": "The term 'WER' (Word Error Rate) in transcription measures:", "opts": ["Transcription speed", "The percentage of words that differ between the transcript and ground truth", "The number of speaker changes", "Audio quality"], "a": 1, "d": 2, "exp": "WER = (substitutions + insertions + deletions) / total reference words."},
        {"q": "When reviewing a transcript for errors, you should listen to the audio while reading:", "opts": ["Only at normal speed", "At a slower speed to catch every word", "At multiple speeds as needed to verify accuracy", "At maximum speed to save time"], "a": 2, "d": 1, "exp": "Variable playback speed helps catch different types of errors efficiently."},
        {"q": "Non-verbal sounds like laughter should be transcribed as:", "opts": ["Ignored entirely", "Written as '[laughter]' or similar notation if they carry meaning", "Converted to punctuation", "Transcribed phonetically"], "a": 1, "d": 2, "exp": "Non-verbal sounds with communicative value are captured in brackets per convention."},
        {"q": "When a transcribed technical term looks wrong, you should:", "opts": ["Leave it as is", "Correct it based on context and a quick verification search", "Remove it from the transcript", "Mark the whole section as uncertain"], "a": 1, "d": 2, "exp": "Technical terms should be verified and corrected; context clues and quick research help."},
    ],
    "data_entry": [
        {"q": "The most important quality in data entry work is:", "opts": ["Speed", "Accuracy", "Creativity", "Volume"], "a": 1, "d": 1, "exp": "Data entry errors propagate through systems — accuracy is the primary requirement."},
        {"q": "When a source document has an unclear value, you should:", "opts": ["Guess and enter the most likely value", "Leave the field blank and flag for review", "Enter zero", "Skip the record entirely"], "a": 1, "d": 1, "exp": "Flagging unclear values prevents silent data corruption."},
        {"q": "Data normalization in entry means:", "opts": ["Entering data as fast as possible", "Ensuring data follows a consistent format (e.g., dates as YYYY-MM-DD)", "Compressing files", "Sorting records alphabetically"], "a": 1, "d": 1, "exp": "Normalization ensures data is in a consistent, usable format across all records."},
        {"q": "Double-entry verification involves:", "opts": ["Entering each record twice by the same person", "Having two different people enter the same data and comparing for discrepancies", "Backing up data to two locations", "Entering numeric data and its text equivalent"], "a": 1, "d": 2, "exp": "Two-person entry catches errors that a single operator misses consistently."},
        {"q": "What is a 'validation rule' in data entry?", "opts": ["A spelling correction tool", "A constraint ensuring data meets required format or value ranges before acceptance", "A data backup procedure", "An entry speed target"], "a": 1, "d": 2, "exp": "Validation rules catch entry errors at the point of entry before they enter the database."},
        {"q": "When entering dates, ISO 8601 format (YYYY-MM-DD) is preferred because:", "opts": ["It is shorter than other formats", "It is unambiguous and sorts correctly as a string", "It is required by law", "It matches the US convention"], "a": 1, "d": 2, "exp": "YYYY-MM-DD is unambiguous (MM/DD/YYYY vs DD/MM/YYYY ambiguity) and lexicographically sortable."},
        {"q": "What causes most data entry errors?", "opts": ["Slow entry speed", "Fatigue, distraction, and unclear source documents", "Complex validation rules", "Large number of fields"], "a": 1, "d": 1, "exp": "Human error in data entry is mainly driven by fatigue and unclear source material."},
        {"q": "When the task asks for 'exact match' transcription, you should:", "opts": ["Correct obvious spelling errors in the source", "Enter the data exactly as it appears, preserving errors in the original", "Normalize formatting", "Skip unclear entries"], "a": 1, "d": 2, "exp": "Exact match means transcribing the source as-is, preserving its format and errors."},
        {"q": "A 'null' value in a database record means:", "opts": ["The value is zero", "The value is unknown or not applicable", "The field is optional", "An entry error occurred"], "a": 1, "d": 2, "exp": "NULL means absent or unknown — distinct from zero, empty string, or 'N/A'."},
        {"q": "What is 'key verification' in data entry?", "opts": ["Verifying the enter key works correctly", "Re-keying critical fields to verify accuracy", "Checking field labels", "Locking fields after entry"], "a": 1, "d": 2, "exp": "Key verification re-enters critical fields to catch transcription errors."},
        {"q": "When you spot a pattern of systematic errors in a data entry task, you should:", "opts": ["Continue entering data the same way", "Correct the errors silently", "Flag the pattern to the task manager before continuing", "Abandon the task"], "a": 2, "d": 2, "exp": "Systematic errors may indicate a source problem or guideline misunderstanding that needs supervisor input."},
        {"q": "OCR (Optical Character Recognition) output in data entry tasks:", "opts": ["Is always accurate and can be accepted as-is", "Should be reviewed for common errors like '0' vs 'O' and '1' vs 'l'", "Cannot be corrected by human reviewers", "Only applies to handwritten documents"], "a": 1, "d": 2, "exp": "OCR makes systematic character confusion errors that human reviewers catch and correct."},
    ],
}


def _score_to_proficiency(score: int, total: int) -> int:
    """Map a quiz score to a proficiency level 1-5."""
    pct = score / total if total else 0
    if pct >= 0.9:
        return 5
    elif pct >= 0.75:
        return 4
    elif pct >= 0.6:
        return 3
    elif pct >= 0.4:
        return 2
    else:
        return 1


async def _ensure_questions_seeded(skill_category: str, db: AsyncSession) -> None:
    """Seed questions for a category if none exist yet."""
    count_q = await db.execute(
        select(func.count()).where(SkillQuizQuestionDB.skill_category == skill_category)
    )
    if count_q.scalar_one() > 0:
        return

    seed = SEED_QUESTIONS.get(skill_category, [])
    for item in seed:
        q = SkillQuizQuestionDB(
            skill_category=skill_category,
            question=item["q"],
            options=item["opts"],
            correct_index=item["a"],
            difficulty=item["d"],
            explanation=item.get("exp"),
        )
        db.add(q)
    await db.commit()


@router.get("/categories")
async def list_categories(
    user_id: str = Depends(get_current_user_id),
) -> list[dict]:
    """List available skill quiz categories."""
    return [
        {"category": c, "label": c.replace("_", " ").title()}
        for c in SKILL_CATEGORIES
    ]


@router.get("/attempts", response_model=list[SkillQuizAttemptOut])
async def list_attempts(
    limit: int = Query(20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[SkillQuizAttemptOut]:
    """List this worker's past quiz attempts."""
    uid = UUID(user_id)
    res = await db.execute(
        select(SkillQuizAttemptDB)
        .where(SkillQuizAttemptDB.worker_id == uid)
        .order_by(SkillQuizAttemptDB.created_at.desc())
        .limit(limit)
    )
    return res.scalars().all()


@router.get("/{skill_category}/questions", response_model=list[SkillQuizQuestionOut])
async def get_quiz_questions(
    skill_category: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[SkillQuizQuestionOut]:
    """Return up to QUESTIONS_PER_QUIZ random questions for the given category."""
    if skill_category not in SKILL_CATEGORIES:
        raise HTTPException(400, f"Unknown skill category. Valid: {SKILL_CATEGORIES}")

    await _ensure_questions_seeded(skill_category, db)

    res = await db.execute(
        select(SkillQuizQuestionDB).where(
            SkillQuizQuestionDB.skill_category == skill_category
        ).limit(QUESTIONS_PER_QUIZ * 20)  # safety cap; random.sample picks from this pool
    )
    all_qs = res.scalars().all()
    chosen = random.sample(all_qs, min(QUESTIONS_PER_QUIZ, len(all_qs)))

    return [
        SkillQuizQuestionOut(
            id=q.id,
            question=q.question,
            options=q.options,
            difficulty=q.difficulty,
        )
        for q in chosen
    ]


@router.post("/{skill_category}/submit", response_model=SkillQuizResultOut)
async def submit_quiz(
    skill_category: str,
    payload: SkillQuizSubmitRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> SkillQuizResultOut:
    """Grade the submitted answers and update the worker's proficiency level."""
    if skill_category not in SKILL_CATEGORIES:
        raise HTTPException(400, f"Unknown skill category.")

    uid = UUID(user_id)
    total = len(payload.answers)
    if total == 0:
        raise HTTPException(400, "No answers provided.")

    await _ensure_questions_seeded(skill_category, db)

    # Load questions to grade against — cap at a generous limit (safety bound)
    res = await db.execute(
        select(SkillQuizQuestionDB).where(
            SkillQuizQuestionDB.skill_category == skill_category
        ).limit(1_000)
    )
    all_qs = {str(q.id): q for q in res.scalars().all()}

    if not all_qs:
        raise HTTPException(400, "No questions available for this skill category. Please try again later.")

    if payload.question_ids:
        # Client sent the question IDs in the order they were displayed — use them for
        # correct positional grading (fixes the re-shuffle misalignment bug).
        questions_used = [all_qs[qid] for qid in payload.question_ids if qid in all_qs]
    else:
        # Legacy fallback: no question_ids provided — use deterministic ordering so
        # grading matches what the GET endpoint returned (alphabetical by ID).
        qs_list = sorted(all_qs.values(), key=lambda q: str(q.id))
        questions_used = qs_list[:total]

    n = len(questions_used)
    if n == 0:
        raise HTTPException(400, "No questions available for this skill category. Please try again later.")

    score = 0
    results = []
    for i, q in enumerate(questions_used):
        chosen = payload.answers[i] if i < len(payload.answers) else -1
        correct = q.correct_index
        is_correct = chosen == correct
        if is_correct:
            score += 1
        results.append({
            "question": q.question,
            "options": q.options,
            "your_answer": chosen,
            "correct_index": correct,
            "is_correct": is_correct,
            "explanation": q.explanation,
        })

    passed = score / n >= PASS_THRESHOLD
    prof_level = _score_to_proficiency(score, n)

    # Save attempt
    attempt = SkillQuizAttemptDB(
        worker_id=uid,
        skill_category=skill_category,
        question_ids=[str(q.id) for q in questions_used],
        answers=payload.answers,
        score=score,
        total=n,
        passed=passed,
        proficiency_level=prof_level,
    )
    db.add(attempt)

    # Update or create WorkerSkillDB proficiency
    skill_res = await db.execute(
        select(WorkerSkillDB).where(
            WorkerSkillDB.worker_id == uid,
            WorkerSkillDB.task_type == skill_category,
        )
    )
    existing_skill = skill_res.scalar_one_or_none()
    if existing_skill:
        # Only upgrade proficiency, never downgrade via quiz
        if prof_level > existing_skill.proficiency_level:
            existing_skill.proficiency_level = prof_level
    else:
        new_skill = WorkerSkillDB(
            worker_id=uid,
            task_type=skill_category,
            proficiency_level=prof_level,
            match_weight=0.8 + (prof_level * 0.1),  # 0.9-1.3
        )
        db.add(new_skill)

    # Award bonus credits for passing (first pass only)
    credits_earned = 0
    if passed:
        from sqlalchemy import and_
        prev_passes = await db.execute(
            select(func.count()).where(
                SkillQuizAttemptDB.worker_id == uid,
                SkillQuizAttemptDB.skill_category == skill_category,
                SkillQuizAttemptDB.passed == True,  # noqa: E712
            )
        )
        prev_count = prev_passes.scalar_one() or 0
        if prev_count == 0:  # This is the first pass (not yet committed)
            from models.db import UserDB, CreditTransactionDB
            # Lock the user row so two concurrent first-pass submissions cannot
            # both read prev_count=0 and double-award the bonus credits.
            user_res = await db.execute(
                select(UserDB).where(UserDB.id == uid).with_for_update()
            )
            user = user_res.scalar_one_or_none()
            if user:
                user.credits += CREDITS_FOR_PASSING
                credits_earned = CREDITS_FOR_PASSING
                txn = CreditTransactionDB(
                    user_id=uid,
                    amount=CREDITS_FOR_PASSING,
                    type="credit",
                    description=f"Skill quiz bonus: {skill_category} (level {prof_level})",
                )
                db.add(txn)

    await db.commit()

    logger.info("skill_quiz_submitted",
                worker_id=str(uid),
                skill=skill_category,
                score=score,
                total=n,
                passed=passed,
                prof=prof_level)

    return SkillQuizResultOut(
        score=score,
        total=n,
        passed=passed,
        proficiency_level=prof_level,
        skill_category=skill_category,
        questions=results,
        credits_earned=credits_earned,
    )
