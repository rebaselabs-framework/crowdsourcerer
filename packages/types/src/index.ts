// ─── Task Types ────────────────────────────────────────────────────────────

/** AI-powered task types (executed automatically by RebaseKit APIs) */
export type AITaskType =
  | "web_research"
  | "entity_lookup"
  | "document_parse"
  | "data_transform"
  | "llm_generate"
  | "screenshot"
  | "audio_transcribe"
  | "pii_detect"
  | "code_execute"
  | "web_intel";

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

export type TaskType = AITaskType | HumanTaskType;

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
  url: string;
  instruction?: string;
  extract_tables?: boolean;
  extract_links?: boolean;
  wait_for_selector?: string;
}

export interface EntityLookupInput {
  entity_type: "company" | "person";
  name: string;
  domain?: string;
  linkedin_url?: string;
  enrich_fields?: string[];
}

export interface DocumentParseInput {
  url?: string;
  base64_content?: string;
  mime_type?: string;
  extract_tables?: boolean;
  extract_images?: boolean;
}

export interface DataTransformInput {
  data: unknown;
  transform: string;
  output_format?: "json" | "csv" | "markdown" | "text";
}

export interface LLMGenerateInput {
  messages: Array<{ role: "system" | "user" | "assistant"; content: string }>;
  model?: string;
  temperature?: number;
  max_tokens?: number;
  system_prompt?: string;
}

export interface ScreenshotInput {
  url: string;
  width?: number;
  height?: number;
  full_page?: boolean;
  wait_for_selector?: string;
  format?: "png" | "jpeg" | "webp";
}

export interface AudioTranscribeInput {
  url?: string;
  base64_audio?: string;
  language?: string;
  diarize?: boolean;
}

export interface PiiDetectInput {
  text: string;
  entities?: string[];
  mask?: boolean;
  vault?: boolean;
}

export interface CodeExecuteInput {
  code: string;
  language: "python" | "javascript" | "bash";
  timeout_seconds?: number;
  stdin?: string;
}

export interface WebIntelInput {
  query: string;
  sources?: string[];
  max_results?: number;
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
  | EntityLookupInput
  | DocumentParseInput
  | DataTransformInput
  | LLMGenerateInput
  | ScreenshotInput
  | AudioTranscribeInput
  | PiiDetectInput
  | CodeExecuteInput
  | WebIntelInput
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
  type: TaskType;
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

export interface TaskCreateRequest {
  type: TaskType;
  input: TaskInput;
  priority?: TaskPriority;
  metadata?: Record<string, unknown>;
  webhook_url?: string;
  // Human task options
  worker_reward_credits?: number;
  assignments_required?: number;
  claim_timeout_minutes?: number;
  task_instructions?: string;
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

// ─── Pricing ───────────────────────────────────────────────────────────────

/** AI task costs (credits charged to requester) */
export const TASK_CREDITS: Record<AITaskType, number> = {
  web_research: 10,
  entity_lookup: 5,
  document_parse: 3,
  data_transform: 2,
  llm_generate: 1,
  screenshot: 2,
  audio_transcribe: 8,
  pii_detect: 2,
  code_execute: 3,
  web_intel: 5,
};

/** Default worker reward credits for human tasks (per assignment) */
export const HUMAN_TASK_DEFAULT_REWARDS: Record<HumanTaskType, number> = {
  label_image: 3,
  label_text: 2,
  rate_quality: 2,
  verify_fact: 3,
  moderate_content: 2,
  compare_rank: 2,
  answer_question: 4,
  transcription_review: 5,
};

/** Human task types set (for runtime checks) */
export const HUMAN_TASK_TYPES = new Set<HumanTaskType>([
  "label_image", "label_text", "rate_quality",
  "verify_fact", "moderate_content", "compare_rank",
  "answer_question", "transcription_review",
]);

export const CREDITS_PER_USD = 100; // 1 USD = 100 credits ($0.01/credit)
