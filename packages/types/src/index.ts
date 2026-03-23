// ─── Task Types ────────────────────────────────────────────────────────────

export type TaskType =
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

export type TaskStatus =
  | "pending"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type TaskPriority = "low" | "normal" | "high" | "urgent";

// ─── Task Inputs ───────────────────────────────────────────────────────────

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
  | WebIntelInput;

// ─── Task Output ───────────────────────────────────────────────────────────

export interface TaskOutput {
  raw: unknown;
  summary?: string;
  error?: string;
}

// ─── Task Object ───────────────────────────────────────────────────────────

export interface Task {
  id: string;
  type: TaskType;
  status: TaskStatus;
  priority: TaskPriority;
  input: TaskInput;
  output?: TaskOutput;
  created_at: string;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
  credits_used?: number;
  metadata?: Record<string, unknown>;
  error?: string;
}

export interface TaskCreateRequest {
  type: TaskType;
  input: TaskInput;
  priority?: TaskPriority;
  metadata?: Record<string, unknown>;
  webhook_url?: string;
}

export interface TaskCreateResponse {
  task_id: string;
  status: TaskStatus;
  estimated_credits: number;
  estimated_duration_ms?: number;
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
  type: "charge" | "credit" | "refund";
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
  credits: number;
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
  type: "task.completed" | "task.failed" | "credits.low";
  data: unknown;
  created_at: string;
}

// ─── Pricing ───────────────────────────────────────────────────────────────

export const TASK_CREDITS: Record<TaskType, number> = {
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

export const CREDITS_PER_USD = 100; // 1 USD = 100 credits ($0.01/credit)
