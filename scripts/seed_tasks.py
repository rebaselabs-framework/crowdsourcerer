#!/usr/bin/env python3
"""Seed the production CrowdSorcerer instance with diverse human tasks."""
import httpx
import sys
import time

BASE = "https://crowdsourcerer.rebaselabs.online"

# Login as the seed requester
def login():
    r = httpx.post(f"{BASE}/v1/auth/login", json={
        "email": "seed-requester@example.com",
        "password": "SeedPass123!",
    })
    if r.status_code != 200:
        # Try registering first
        r2 = httpx.post(f"{BASE}/v1/auth/register", json={
            "email": "seed-requester@example.com",
            "password": "SeedPass123!",
            "name": "CrowdSorcerer Team",
            "role": "requester",
        })
        if r2.status_code == 200:
            return r2.json()["access_token"]
        # Maybe already registered, try login again
        r = httpx.post(f"{BASE}/v1/auth/login", json={
            "email": "seed-requester@example.com",
            "password": "SeedPass123!",
        })
    return r.json()["access_token"]

def create_task(token, task_data):
    r = httpx.post(
        f"{BASE}/v1/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json=task_data,
        timeout=30.0,
    )
    if r.status_code in (200, 201):
        d = r.json()
        return d.get("task_id", "ok")
    else:
        return f"ERROR {r.status_code}: {r.text[:200]}"

TASKS = [
    # === LABEL TEXT: Sentiment classification ===
    {
        "type": "label_text",
        "title": "Classify product review sentiment",
        "instructions": "Read the product review and classify the overall sentiment.",
        "input": {"text": "The battery life is incredible - lasts all day with heavy use. Camera is decent but not the best. Overall very happy with my purchase."},
        "labels": ["positive", "negative", "neutral", "mixed"],
        "priority": "high",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Classify product review sentiment",
        "instructions": "Read the product review and classify the overall sentiment.",
        "input": {"text": "Terrible build quality. The hinge broke after 2 weeks and customer support was completely unhelpful. Do NOT buy this."},
        "labels": ["positive", "negative", "neutral", "mixed"],
        "priority": "high",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Classify product review sentiment",
        "instructions": "Read the product review and classify the overall sentiment.",
        "input": {"text": "It works fine I guess. Nothing special. Does what it says on the box."},
        "labels": ["positive", "negative", "neutral", "mixed"],
        "priority": "normal",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    # === LABEL TEXT: Support ticket urgency ===
    {
        "type": "label_text",
        "title": "Classify support ticket urgency",
        "instructions": "Read the support ticket and classify how urgent it is.",
        "input": {"text": "Our production database is down and we cannot process any orders. This is affecting thousands of customers. Need immediate help."},
        "labels": ["critical", "high", "medium", "low"],
        "priority": "high",
        "worker_reward_credits": 8,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Classify support ticket urgency",
        "instructions": "Read the support ticket and classify how urgent it is.",
        "input": {"text": "Would it be possible to add dark mode to the settings page? Not urgent but would be a nice quality of life improvement."},
        "labels": ["critical", "high", "medium", "low"],
        "priority": "normal",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Classify support ticket urgency",
        "instructions": "Read the support ticket and classify how urgent it is.",
        "input": {"text": "Getting a 500 error when trying to checkout. Customers are abandoning carts. Happening since the latest deploy 30 mins ago."},
        "labels": ["critical", "high", "medium", "low"],
        "priority": "high",
        "worker_reward_credits": 8,
        "assignments_required": 3,
    },
    # === LABEL TEXT: Programming language identification ===
    {
        "type": "label_text",
        "title": "Identify programming language",
        "instructions": "Look at the code snippet and identify the programming language.",
        "input": {"text": "fn main() {\n    let greeting = String::from(\"Hello, world!\");\n    println!(\"{}\", greeting);\n}"},
        "labels": ["Python", "JavaScript", "Rust", "Go", "C++", "Java", "TypeScript"],
        "priority": "normal",
        "worker_reward_credits": 3,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Identify programming language",
        "instructions": "Look at the code snippet and identify the programming language.",
        "input": {"text": "const fetchData = async () => {\n  const res = await fetch('/api/data');\n  const data = await res.json();\n  return data;\n};"},
        "labels": ["Python", "JavaScript", "Rust", "Go", "C++", "Java", "TypeScript"],
        "priority": "normal",
        "worker_reward_credits": 3,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Identify programming language",
        "instructions": "Look at the code snippet and identify the programming language.",
        "input": {"text": "def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)\n\nprint(fibonacci(10))"},
        "labels": ["Python", "JavaScript", "Rust", "Go", "C++", "Java", "TypeScript"],
        "priority": "normal",
        "worker_reward_credits": 3,
        "assignments_required": 3,
    },
    # === LABEL TEXT: Email intent classification ===
    {
        "type": "label_text",
        "title": "Classify email intent",
        "instructions": "Read the email excerpt and classify its primary intent.",
        "input": {"text": "Hi, I ordered a laptop last week but it arrived with a cracked screen. I need a replacement or refund. Order #12345."},
        "labels": ["complaint", "question", "request", "feedback", "spam"],
        "priority": "normal",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Classify email intent",
        "instructions": "Read the email excerpt and classify its primary intent.",
        "input": {"text": "Your service has been fantastic! The onboarding was smooth, the team was responsive, and our productivity has doubled since we started using your platform."},
        "labels": ["complaint", "question", "request", "feedback", "spam"],
        "priority": "normal",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    # === LABEL TEXT: Toxicity detection ===
    {
        "type": "label_text",
        "title": "Detect toxicity in online comment",
        "instructions": "Read the comment and determine if it contains toxic, harmful, or abusive language.",
        "input": {"text": "I respectfully disagree with your analysis. While I understand your perspective, the data suggests a different conclusion. Here's why..."},
        "labels": ["not toxic", "mildly toxic", "toxic", "severely toxic"],
        "priority": "high",
        "worker_reward_credits": 6,
        "assignments_required": 3,
    },
    {
        "type": "label_text",
        "title": "Detect toxicity in online comment",
        "instructions": "Read the comment and determine if it contains toxic, harmful, or abusive language.",
        "input": {"text": "This is the worst take I've ever seen. Anyone who believes this garbage is either lying or completely clueless."},
        "labels": ["not toxic", "mildly toxic", "toxic", "severely toxic"],
        "priority": "high",
        "worker_reward_credits": 6,
        "assignments_required": 3,
    },
    # === COMPARE & RANK ===
    {
        "type": "compare_rank",
        "title": "Which headline is more engaging?",
        "instructions": "Read both headlines for the same article and pick which one would make you more likely to click and read the full article.",
        "input": {"item_a": "New Study Finds Link Between Sleep and Productivity", "item_b": "Scientists Discover Why Night Owls Are Secretly More Productive Than Early Birds"},
        "priority": "normal",
        "worker_reward_credits": 3,
        "assignments_required": 5,
    },
    {
        "type": "compare_rank",
        "title": "Which headline is more engaging?",
        "instructions": "Read both headlines for the same article and pick which one would make you more likely to click and read the full article.",
        "input": {"item_a": "Climate Change Report Released", "item_b": "The World Has 7 Years to Avoid Climate Catastrophe, New Report Warns"},
        "priority": "normal",
        "worker_reward_credits": 3,
        "assignments_required": 5,
    },
    {
        "type": "compare_rank",
        "title": "Which product description converts better?",
        "instructions": "Imagine you're shopping online. Which product description would make you more likely to buy?",
        "input": {"item_a": "Wireless earbuds with 8-hour battery life, Bluetooth 5.3, and IPX5 water resistance. Available in black, white, and navy.", "item_b": "Never miss a beat. These wireless earbuds last all day (8 hours!), connect instantly, and survive your sweatiest workouts. Pick your vibe: midnight, snow, or ocean."},
        "priority": "normal",
        "worker_reward_credits": 4,
        "assignments_required": 5,
    },
    {
        "type": "compare_rank",
        "title": "Which logo concept is stronger?",
        "instructions": "Compare these two logo descriptions for a coffee shop called 'Morning Ritual'. Which concept would make a better logo?",
        "input": {"item_a": "A minimalist sunrise icon above the text in a clean sans-serif font. Colors: warm orange gradient on white.", "item_b": "A steaming coffee cup where the steam forms the shape of a sun. Hand-drawn script font. Colors: dark brown and gold."},
        "priority": "normal",
        "worker_reward_credits": 4,
        "assignments_required": 5,
    },
    # === VERIFY FACT ===
    {
        "type": "verify_fact",
        "title": "Verify AI-generated claim",
        "instructions": "An AI generated the following claim. Verify whether it is factually correct, incorrect, or unverifiable based on your knowledge.",
        "input": {"claim": "Python was created by Guido van Rossum and first released in 1991.", "source": "AI-generated text about programming languages"},
        "labels": ["correct", "incorrect", "unverifiable"],
        "priority": "normal",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    {
        "type": "verify_fact",
        "title": "Verify AI-generated claim",
        "instructions": "An AI generated the following claim. Verify whether it is factually correct, incorrect, or unverifiable based on your knowledge.",
        "input": {"claim": "JavaScript was developed at Microsoft and is primarily used for backend server programming.", "source": "AI-generated text about web development"},
        "labels": ["correct", "incorrect", "unverifiable"],
        "priority": "normal",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    {
        "type": "verify_fact",
        "title": "Verify AI-generated claim",
        "instructions": "An AI generated the following claim. Verify whether it is factually correct, incorrect, or unverifiable based on your knowledge.",
        "input": {"claim": "The Great Wall of China is visible from space with the naked eye.", "source": "AI-generated trivia content"},
        "labels": ["correct", "incorrect", "unverifiable"],
        "priority": "high",
        "worker_reward_credits": 5,
        "assignments_required": 3,
    },
    # === RATE CONTENT ===
    {
        "type": "rate_quality",
        "title": "Rate AI-generated summary quality",
        "instructions": "Read the original text and the AI summary. Rate the summary quality from 1-5. Consider accuracy, completeness, and readability.",
        "input": {"original": "The global semiconductor shortage that began in 2020 has had far-reaching effects across multiple industries. Automotive manufacturers were among the hardest hit, with some plants shutting down for weeks. Consumer electronics also saw delays and price increases. The shortage was caused by a combination of factors including pandemic-related factory closures, increased demand for electronics during lockdowns, and a fire at a major Japanese chip factory.", "summary": "A semiconductor shortage starting in 2020 affected cars and electronics due to factory closures, higher demand, and a factory fire in Japan."},
        "priority": "normal",
        "worker_reward_credits": 8,
        "assignments_required": 3,
    },
    {
        "type": "rate_quality",
        "title": "Rate AI-generated summary quality",
        "instructions": "Read the original text and the AI summary. Rate the summary quality from 1-5. Consider accuracy, completeness, and readability.",
        "input": {"original": "Machine learning models are increasingly being used in healthcare for tasks ranging from medical image analysis to drug discovery. Recent studies have shown that deep learning algorithms can detect certain cancers from radiology images with accuracy comparable to trained physicians. However, challenges remain in terms of model interpretability, data privacy, and regulatory approval.", "summary": "AI is used in healthcare."},
        "priority": "normal",
        "worker_reward_credits": 8,
        "assignments_required": 3,
    },
    # === MODERATE CONTENT ===
    {
        "type": "moderate_content",
        "title": "Review user comment for policy violations",
        "instructions": "Check if this user comment violates community guidelines. Flag: spam, harassment, misinformation, hate speech, or inappropriate content. If clean, mark as approved.",
        "input": {"text": "Just discovered this amazing new framework for building web apps. Been using it for a week and productivity is through the roof!", "context": "Posted in a developer forum about web frameworks"},
        "priority": "high",
        "worker_reward_credits": 6,
        "assignments_required": 3,
    },
    {
        "type": "moderate_content",
        "title": "Review user comment for policy violations",
        "instructions": "Check if this user comment violates community guidelines. Flag: spam, harassment, misinformation, hate speech, or inappropriate content. If clean, mark as approved.",
        "input": {"text": "CLICK HERE for FREE IPHONES!!! Visit totally-not-a-scam.com to claim your prize NOW!!!", "context": "Posted in a tech discussion thread about smartphone reviews"},
        "priority": "high",
        "worker_reward_credits": 6,
        "assignments_required": 3,
    },
    {
        "type": "moderate_content",
        "title": "Review user comment for policy violations",
        "instructions": "Check if this user comment violates community guidelines. Flag: spam, harassment, misinformation, hate speech, or inappropriate content. If clean, mark as approved.",
        "input": {"text": "While I appreciate the effort, I think the methodology in this study is flawed. The sample size is too small and the control group wasn't properly randomized. Here's a paper that addresses this...", "context": "Posted in a science discussion forum"},
        "priority": "normal",
        "worker_reward_credits": 6,
        "assignments_required": 3,
    },
]

def main():
    print(f"Logging in...")
    token = login()
    print(f"Authenticated. Seeding {len(TASKS)} tasks...\n")

    created = 0
    errors = 0
    for i, task in enumerate(TASKS):
        result = create_task(token, task)
        if result.startswith("ERROR"):
            print(f"  [{i+1}/{len(TASKS)}] FAIL: {task['title'][:40]} — {result}")
            errors += 1
        else:
            print(f"  [{i+1}/{len(TASKS)}] OK: {task['title'][:40]} → {result[:8]}...")
            created += 1
        time.sleep(0.3)  # Be gentle

    print(f"\nDone! Created {created}/{len(TASKS)} tasks ({errors} errors)")

if __name__ == "__main__":
    main()
