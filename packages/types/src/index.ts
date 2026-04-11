// ─── Task Types ────────────────────────────────────────────────────────────

/** Human task types (completed by human workers in the marketplace) */
export type HumanTaskType =
  | "label_image"
  | "label_text"
  | "rate_quality"
  | "verify_fact"
  | "moderate_content"
  | "compare_rank"
  | "answer_question"
  | "transcription_review";

/**
 * AI task types are **pipeline-only primitives** — not user-submittable
 * via POST /v1/tasks. Pipelines can chain these with human steps for
 * hybrid human+AI workflows; see /v1/pipelines. Kept in the public
 * types so the pipeline builder UI and Task record shape stay honest.
 *
 * - `web_research`   httpx fetch → BeautifulSoup extract → LLM summary
 * - `document_parse` pypdf / python-docx / openpyxl local extraction
 * - `data_transform` LLM query with a structured transform prompt
 * - `llm_generate`   direct provider call (Anthropic / Gemini / OpenAI)
 * - `pii_detect`     local regex detector (email / phone / SSN / etc.)
 * - `code_execute`   Python subprocess sandbox (temp dir + rlimits)
 */
export type AITaskType =
  | "web_research"
  | "document_parse"
  | "data_transform"
  | "llm_generate"
  | "pii_detect"
  | "code_execute";

/** Directly submittable via POST /v1/tasks. */
export type TaskType = HumanTaskType;

/** Union of every type that can appear on a stored Task row — includes
 *  AI primitives emitted by pipeline step execution. */
export type PipelineStepTaskType = HumanTaskType | AITaskType;

export type TaskStatus =
  | "pending"
  | "queued"
  | "running"
  | "open"       // Human task: available in marketplace
  | "assigned"   // Human task: claimed by a worker
  | "completed"
  | "failed"
  | "cancelled";

export type TaskPriority = "low" | "normal" | "high" | "urgent";

export type ExecutionMode = "ai" | "human";

export type UserRole = "requester" | "worker" | "both";

// ─── AI Task Inputs ────────────────────────────────────────────────────────

export interface WebResearchInput {
  /** URL to fetch and summarise. */
  url: string;
  /** Optional free-text instruction that shapes the summary. */
  instruction?: string;
}

export interface DocumentParseInput {
  /** URL of the document to fetch. Mutually exclusive with `content_base64`. */
  url?: string;
  /** Base64-encoded document content. */
  content_base64?: string;
  /** Pull tables out as structured rows. */
  include_tables?: boolean;
}

export interface DataTransformInput {
  /** The input data — arbitrary JSON-serialisable shape. */
  data: unknown;
  /** Free-text instruction describing the desired transformation. */
  transform: string;
  /** Preferred output format. */
  output_format?: "json" | "csv" | "markdown" | "text";
}

export interface LLMGenerateInput {
  messages: Array<{ role: "system" | "user" | "assistant"; content: string }>;
  model?: string;
  temperature?: number;
  max_tokens?: number;
  system_prompt?: string;
}

export interface PiiDetectInput {
  text: string;
  /** Optional subset of entity types to detect (default: all). */
  entities?: string[];
  /** When true, include a redacted copy of the input in the output. */
  mask?: boolean;
}

export interface CodeExecuteInput {
  code: string;
  /** Only Python is supported by the in-process sandbox. */
  language?: "python";
  /** Wall-clock timeout in seconds (default 10, max 30). */
  timeout_seconds?: number;
  /** Optional input passed to the script on stdin. */
  stdin?: string;
}

// ─── Human Task Inputs ────────────────────────────────────────────────────

export interface LabelImageInput {
  image_url: string;
  labels: string[];               // Possible labels to choose from
  description?: string;           // Additional context
  allow_multiple?: boolean;       // Allow selecting multiple labels
}

export interface LabelTextInput {
  text: string;
  categories: string[];           // Possible categories
  allow_multiple?: boolean;
}

export interface RateQualityInput {
  content: string;
  title?: string;
  criteria?: string;              // What to evaluate (e.g., "clarity", "accuracy")
  scale?: [number, number];       // Default: [1, 5]
}

export interface VerifyFactInput {
  claim: string;
  context?: string;               // Background information
}

export interface ModerateContentInput {
  content: string;
  content_type?: "text" | "image_url" | "video_url";
  policy_context?: string;        // Description of relevant policy
}

export interface CompareRankInput {
  option_a: string;
  option_b: string;
  criterion?: string;             // What dimension to compare on
}

export interface AnswerQuestionInput {
  content: string;                // The context/document
  question: string;
  answer_format?: "free_text" | "yes_no" | "multiple_choice";
  choices?: string[];             // For multiple_choice format
}

export interface TranscriptionReviewInput {
  audio_url: string;
  ai_transcript: string;          // The AI-generated transcript to review/correct
  language?: string;
}

// ─── Combined Task Input ───────────────────────────────────────────────────

export type TaskInput =
  | WebResearchInput
  | DocumentParseInput
  | DataTransformInput
  | LLMGenerateInput
  | PiiDetectInput
  | CodeExecuteInput
  | LabelImageInput
  | LabelTextInput
  | RateQualityInput
  | VerifyFactInput
  | ModerateContentInput
  | CompareRankInput
  | AnswerQuestionInput
  | TranscriptionReviewInput;

// ─── Task Output ───────────────────────────────────────────────────────────

export interface TaskOutput {
  raw: unknown;
  summary?: string;
  error?: string;
}

/** Worker response to a human task */
export interface WorkerResponse {
  // label_image / label_text
  labels?: string[];
  // rate_quality
  rating?: number;
  justification?: string;
  // verify_fact
  verdict?: "true" | "false" | "unverifiable";
  citation?: string;
  // moderate_content
  decision?: "approve" | "reject" | "escalate";
  reason?: string;
  // compare_rank
  choice?: "a" | "b" | "tie";
  // answer_question
  answer?: string;
  // transcription_review
  corrected_text?: string;
}

// ─── Task Object ───────────────────────────────────────────────────────────

export interface Task {
  id: string;
  /** Stored task rows include pipeline-emitted AI steps, so the type
   *  field uses the broader union. Directly-created tasks are always
   *  a HumanTaskType. */
  type: PipelineStepTaskType;
  status: TaskStatus;
  priority: TaskPriority;
  execution_mode: ExecutionMode;
  input: TaskInput;
  output?: TaskOutput;
  created_at: string;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
  credits_used?: number;
  metadata?: Record<string, unknown>;
  error?: string;
  // Human task fields
  worker_reward_credits?: number;
  assignments_required?: number;
  assignments_completed?: number;
  task_instructions?: string;
}

/** Supported webhook event types */
export type WebhookEventType =
  | "task.created"
  | "task.assigned"
  | "task.submission_received"
  | "task.completed"
  | "task.failed"
  | "task.approved"
  | "task.rejected"
  | "sla.breach";

export const WEBHOOK_EVENTS: WebhookEventType[] = [
  "task.created",
  "task.assigned",
  "task.submission_received",
  "task.completed",
  "task.failed",
  "task.approved",
  "task.rejected",
  "sla.breach",
];

export const DEFAULT_WEBHOOK_EVENTS: WebhookEventType[] = ["task.completed"];

export interface TaskCreateRequest {
  type: TaskType;
  input: TaskInput;
  priority?: TaskPriority;
  metadata?: Record<string, unknown>;
  webhook_url?: string;
  /** Which webhook events to subscribe to. Defaults to ["task.completed"] */
  webhook_events?: WebhookEventType[];
  // Human task options
  worker_reward_credits?: number;
  assignments_required?: number;
  claim_timeout_minutes?: number;
  task_instructions?: string;
  consensus_strategy?: "any_first" | "majority_vote" | "unanimous" | "requester_review";
  /** Minimum worker proficiency level (1–5) required to claim this task */
  min_skill_level?: number;
}

export interface TaskCreateResponse {
  task_id: string;
  status: TaskStatus;
  estimated_credits: number;
  estimated_duration_ms?: number;
}

// ─── Worker / Assignments ─────────────────────────────────────────────────

export interface TaskAssignment {
  id: string;
  task_id: string;
  worker_id: string;
  status: "active" | "submitted" | "approved" | "rejected" | "released" | "timed_out";
  response?: WorkerResponse;
  worker_note?: string;
  earnings_credits: number;
  xp_earned: number;
  claimed_at: string;
  submitted_at?: string;
  released_at?: string;
  timeout_at?: string;
}

export interface MarketplaceTask {
  id: string;
  type: HumanTaskType;
  priority: TaskPriority;
  reward_credits: number;
  estimated_minutes: number;
  assignments_required: number;
  assignments_completed: number;
  slots_available: number;
  task_instructions?: string;
  created_at: string;
  /** Skill match score 0.0–1.0 (present in /v1/worker/tasks/feed responses) */
  match_score?: number;
  /** Required proficiency level for this task type (1–5) */
  min_skill_level?: number;
}

export interface WorkerStats {
  tasks_completed: number;
  tasks_active: number;
  tasks_released: number;
  total_earnings_credits: number;
  accuracy?: number;
  reliability?: number;
  level: number;
  xp: number;
  xp_to_next_level: number;
  streak_days: number;
}

// ─── Billing ───────────────────────────────────────────────────────────────

export interface CreditBalance {
  available: number;
  reserved: number;
  total_used: number;
  plan: "free" | "starter" | "pro" | "enterprise";
}

export interface CreditTransaction {
  id: string;
  task_id?: string;
  amount: number;
  type: "charge" | "credit" | "refund" | "earning";
  description: string;
  created_at: string;
}

// ─── API Keys ──────────────────────────────────────────────────────────────

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at?: string;
  scopes: string[];
}

export interface ApiKeyCreateRequest {
  name: string;
  scopes?: string[];
}

export interface ApiKeyCreateResponse {
  id: string;
  key: string;
  name: string;
  created_at: string;
}

// ─── User / Workspace ──────────────────────────────────────────────────────

export interface User {
  id: string;
  email: string;
  name?: string;
  created_at: string;
  plan: "free" | "starter" | "pro" | "enterprise";
  role: UserRole;
  credits: number;
  // Worker fields (populated when role includes "worker")
  worker_xp?: number;
  worker_level?: number;
  worker_accuracy?: number;
  worker_reliability?: number;
  worker_tasks_completed?: number;
  worker_streak_days?: number;
}

// ─── Pagination ────────────────────────────────────────────────────────────

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

// ─── Errors ────────────────────────────────────────────────────────────────

export interface ApiError {
  error: string;
  message: string;
  details?: unknown;
  request_id?: string;
}

// ─── Webhooks ──────────────────────────────────────────────────────────────

export interface WebhookEvent {
  id: string;
  type: "task.completed" | "task.failed" | "credits.low" | "assignment.submitted";
  data: unknown;
  created_at: string;
}

// ─── Task type metadata ────────────────────────────────────────────────────
//
// Single source of truth for every task type: id, human-readable label,
// icon, execution mode, base credit cost, and description. Every frontend
// page that wants to render task-type pickers, filter dropdowns, or icon
// maps should pull from here instead of keeping a private copy. The
// Python mirror lives at apps/api/core/task_types.py — keep them in sync.

export interface TaskTypeMeta {
  id: PipelineStepTaskType;
  label: string;
  icon: string;
  executionMode: ExecutionMode;
  /** Base credit cost. For human tasks this is the minimum reward per
   *  worker assignment; the requester can set a higher reward. For AI
   *  tasks it's the actual cost per submission. */
  baseCredits: number;
  description: string;
  /** ``null`` for human tasks. For AI tasks, ``"local"`` means the
   *  handler runs in-process and is always available; ``"llm"`` means
   *  it needs a configured LLM provider key. */
  aiSubkind: "local" | "llm" | null;
}

export const TASK_METADATA: Readonly<Record<PipelineStepTaskType, TaskTypeMeta>> = {
  // Human-submittable task types
  label_image: {
    id: "label_image", label: "Label Image", icon: "🖼️",
    executionMode: "human", baseCredits: 3,
    description: "Bounding boxes, segmentation, or classification on an image.",
    aiSubkind: null,
  },
  label_text: {
    id: "label_text", label: "Label Text", icon: "🏷️",
    executionMode: "human", baseCredits: 2,
    description: "Sentiment, intent, categories, or spam detection on text.",
    aiSubkind: null,
  },
  rate_quality: {
    id: "rate_quality", label: "Rate Quality", icon: "⭐",
    executionMode: "human", baseCredits: 2,
    description: "Score content on a 1–5 (or custom) scale with a written critique.",
    aiSubkind: null,
  },
  verify_fact: {
    id: "verify_fact", label: "Verify Fact", icon: "✅",
    executionMode: "human", baseCredits: 3,
    description: "Check a claim against sources — true / false / unverifiable.",
    aiSubkind: null,
  },
  moderate_content: {
    id: "moderate_content", label: "Moderate Content", icon: "🛡️",
    executionMode: "human", baseCredits: 2,
    description: "Approve, reject, or escalate user-submitted content.",
    aiSubkind: null,
  },
  compare_rank: {
    id: "compare_rank", label: "Compare & Rank", icon: "📊",
    executionMode: "human", baseCredits: 2,
    description: "Pick A vs B (or rank N) on any criterion.",
    aiSubkind: null,
  },
  answer_question: {
    id: "answer_question", label: "Answer Question", icon: "💬",
    executionMode: "human", baseCredits: 4,
    description: "Open-ended Q&A with optional context.",
    aiSubkind: null,
  },
  transcription_review: {
    id: "transcription_review", label: "Review Transcript", icon: "📝",
    executionMode: "human", baseCredits: 5,
    description: "Correct an AI-generated transcript.",
    aiSubkind: null,
  },

  // Pipeline-internal AI primitives
  llm_generate: {
    id: "llm_generate", label: "LLM Generate", icon: "🤖",
    executionMode: "ai", baseCredits: 1,
    description: "Direct LLM completion via the configured provider.",
    aiSubkind: "llm",
  },
  data_transform: {
    id: "data_transform", label: "Data Transform", icon: "🔄",
    executionMode: "ai", baseCredits: 2,
    description: "LLM-backed structured data transformation.",
    aiSubkind: "llm",
  },
  pii_detect: {
    id: "pii_detect", label: "PII Detect", icon: "🔒",
    executionMode: "ai", baseCredits: 2,
    description: "Regex detector for email, phone, SSN, credit card, and more.",
    aiSubkind: "local",
  },
  document_parse: {
    id: "document_parse", label: "Document Parse", icon: "📄",
    executionMode: "ai", baseCredits: 3,
    description: "Extract text from PDF / DOCX / XLSX.",
    aiSubkind: "local",
  },
  code_execute: {
    id: "code_execute", label: "Code Execute", icon: "⚡",
    executionMode: "ai", baseCredits: 3,
    description: "Sandboxed Python subprocess.",
    aiSubkind: "local",
  },
  web_research: {
    id: "web_research", label: "Web Research", icon: "🌐",
    executionMode: "ai", baseCredits: 10,
    description: "Fetch a URL, extract text, and summarise with the LLM.",
    aiSubkind: "llm",
  },
};

/** Stable display order for human task types. */
export const HUMAN_TASK_TYPE_IDS = [
  "label_image", "label_text", "rate_quality", "verify_fact",
  "moderate_content", "compare_rank", "answer_question", "transcription_review",
] as const satisfies readonly HumanTaskType[];

/** Stable display order for AI (pipeline-internal) task types. */
export const AI_TASK_TYPE_IDS = [
  "llm_generate", "data_transform", "pii_detect",
  "document_parse", "code_execute", "web_research",
] as const satisfies readonly AITaskType[];

/** All task-type metadata records, in stable display order. */
export const ALL_TASK_METADATA: readonly TaskTypeMeta[] = [
  ...HUMAN_TASK_TYPE_IDS.map((id) => TASK_METADATA[id]),
  ...AI_TASK_TYPE_IDS.map((id) => TASK_METADATA[id]),
];

/** Only the human-submittable task types. */
export const HUMAN_TASK_METADATA: readonly TaskTypeMeta[] =
  HUMAN_TASK_TYPE_IDS.map((id) => TASK_METADATA[id]);

/** Only the pipeline-internal AI primitives. */
export const AI_TASK_METADATA: readonly TaskTypeMeta[] =
  AI_TASK_TYPE_IDS.map((id) => TASK_METADATA[id]);

// ─── Backwards-compat exports derived from TASK_METADATA ──────────────────
// Kept so callers that still want a raw credits map or a membership Set
// don't have to reach into the metadata record themselves.

/** AI task costs (credits per submission) — derived from TASK_METADATA. */
export const TASK_CREDITS: Readonly<Record<AITaskType, number>> = Object.freeze(
  Object.fromEntries(
    AI_TASK_TYPE_IDS.map((id) => [id, TASK_METADATA[id].baseCredits]),
  ) as Record<AITaskType, number>,
);

/** Default worker reward credits per human task assignment. */
export const HUMAN_TASK_DEFAULT_REWARDS: Readonly<Record<HumanTaskType, number>> = Object.freeze(
  Object.fromEntries(
    HUMAN_TASK_TYPE_IDS.map((id) => [id, TASK_METADATA[id].baseCredits]),
  ) as Record<HumanTaskType, number>,
);

/** Runtime membership set for human task types. */
export const HUMAN_TASK_TYPES: ReadonlySet<HumanTaskType> = new Set(HUMAN_TASK_TYPE_IDS);

/** Runtime membership set for AI (pipeline-internal) task types. */
export const AI_TASK_TYPES: ReadonlySet<AITaskType> = new Set(AI_TASK_TYPE_IDS);

export const CREDITS_PER_USD = 100; // 1 USD = 100 credits ($0.01/credit)

// ─── Leaderboard ───────────────────────────────────────────────────────────

export interface LeaderboardEntry {
  rank: number;
  user_id: string;
  name: string | null;
  worker_level: number;
  worker_xp: number;
  worker_tasks_completed: number;
  worker_accuracy: number | null;
  worker_reliability: number | null;
  worker_streak_days: number;
}

export interface Leaderboard {
  period: "all_time" | "weekly";
  category: "xp" | "tasks" | "earnings";
  entries: LeaderboardEntry[];
  generated_at: string;
}

// ─── Badges ────────────────────────────────────────────────────────────────

export interface Badge {
  badge_id: string;
  name: string;
  description: string;
  icon: string;
  earned_at: string | null;
  earned: boolean;
}

export interface WorkerBadges {
  earned: Badge[];
  locked: Badge[];
  total_earned: number;
}

// ─── Daily Challenges ──────────────────────────────────────────────────────

export interface DailyChallenge {
  id: string;
  challenge_date: string;
  task_type: HumanTaskType;
  title: string;
  description: string | null;
  bonus_xp: number;
  bonus_credits: number;
  target_count: number;
}

export interface DailyChallengeProgress {
  challenge: DailyChallenge;
  tasks_completed: number;
  bonus_claimed: boolean;
  is_complete: boolean;
  tasks_remaining: number;
}

// ─── Quality Control ──────────────────────────────────────────────────────

export interface QualityReport {
  worker_id: string;
  name: string | null;
  tasks_evaluated: number;
  tasks_correct: number;
  accuracy: number;
  reliability: number | null;
  worker_level: number;
  worker_xp: number;
}

export interface GoldStandardCreateRequest {
  task_id: string;
  gold_answer: Record<string, unknown>;
}

// ─── Webhook Logs ─────────────────────────────────────────────────────────

export interface WebhookLog {
  id: string;
  task_id: string;
  url: string;
  event_type: WebhookEventType;
  attempt: number;
  status_code: number | null;
  success: boolean;
  error: string | null;
  duration_ms: number | null;
  created_at: string;
}

export interface WebhookEventInfo {
  type: WebhookEventType;
  description: string;
  is_default: boolean;
}

export interface WebhookStats {
  total_deliveries: number;
  succeeded: number;
  failed: number;
  success_rate: number;
  avg_duration_ms: number | null;
  by_event_type: Record<WebhookEventType, number>;
}

// ─── Admin ────────────────────────────────────────────────────────────────

export interface PlatformStats {
  users: {
    total: number;
    active: number;
    workers: number;
    new_this_week: number;
  };
  tasks: {
    total: number;
    completed: number;
    failed: number;
    running: number;
    open_human: number;
    this_week: number;
    success_rate: number;
    type_breakdown: Array<{ type: string; count: number }>;
  };
  worker_assignments: {
    total: number;
    submitted: number;
  };
  credits: {
    in_circulation: number;
    total_purchased: number;
  };
  webhooks: {
    total: number;
    failed: number;
    success_rate: number;
  };
  generated_at: string;
}

// ─── Template Marketplace ────────────────────────────────────────────────────

export interface Template {
  id: string;
  creator_id: string | null;
  name: string;
  description: string | null;
  task_type: string;
  execution_mode: "ai" | "human";
  category: string | null;
  tags: string[] | null;
  task_config: Record<string, unknown>;
  example_input: Record<string, unknown> | null;
  is_public: boolean;
  is_featured: boolean;
  use_count: number;
  rating_sum: number;
  rating_count: number;
  avg_rating: number | null;
  created_at: string;
}

export interface TemplateCreateRequest {
  name: string;
  description?: string | null;
  task_type: string;
  execution_mode?: "ai" | "human";
  category?: string | null;
  tags?: string[] | null;
  task_config?: Record<string, unknown>;
  example_input?: Record<string, unknown> | null;
  is_public?: boolean;
}

export interface TemplateUseResponse {
  template_id: string;
  task_type: string;
  execution_mode: string;
  task_config: Record<string, unknown>;
  example_input: Record<string, unknown> | null;
}

export interface TemplateRateResponse {
  template_id: string;
  your_rating: number;
  new_avg: number | null;
  total_ratings: number;
}

// ─── Quota ───────────────────────────────────────────────────────────────────

export interface QuotaLimitEntry {
  used: number;
  limit: number | null;
  unlimited: boolean;
}

export interface QuotaStatus {
  plan: string;
  tasks: QuotaLimitEntry;
  pipeline_runs: QuotaLimitEntry;
  pipelines_total: QuotaLimitEntry;
  batch_task_size: number;
  max_worker_assignments: number;
}
