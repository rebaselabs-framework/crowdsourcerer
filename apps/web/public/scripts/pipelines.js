// Pipeline builder client script
// Loaded via <script src="/scripts/pipelines.js"> to avoid @astrojs/compiler bug
// that corrupts { chars inside <script> tags in complex pages.

const API = document.getElementById("pipeline-config").dataset.apiUrl ?? "";
const AI_TYPES = [
  { value: "web_research", label: "🔍 Web Research" },
  { value: "entity_lookup", label: "🏷️ Entity Lookup" },
  { value: "document_parse", label: "📄 Document Parse" },
  { value: "data_transform", label: "🔄 Data Transform" },
  { value: "llm_generate", label: "🤖 LLM Generate" },
  { value: "screenshot", label: "📸 Screenshot" },
  { value: "audio_transcribe", label: "🎙️ Audio Transcribe" },
  { value: "pii_detect", label: "🔒 PII Detect" },
  { value: "code_execute", label: "💻 Code Execute" },
  { value: "web_intel", label: "🌐 Web Intel" },
];
const HUMAN_TYPES = [
  { value: "label_image", label: "🖼️ Label Image" },
  { value: "label_text", label: "📝 Label Text" },
  { value: "rate_quality", label: "⭐ Rate Quality" },
  { value: "verify_fact", label: "✅ Verify Fact" },
  { value: "moderate_content", label: "🛡️ Moderate Content" },
  { value: "compare_rank", label: "📊 Compare & Rank" },
  { value: "answer_question", label: "❓ Answer Question" },
  { value: "transcription_review", label: "📋 Transcription Review" },
];

// ── Retry / Cancel run handlers ────────────────────────────────────────
document.addEventListener("click", async function(e) {
  const TOKEN = document.cookie.split(";").find(function(c) { return c.trim().startsWith("cs_token="); })?.split("=")[1] ?? "";
  const retryBtn = e.target.closest(".retry-run-btn");
  if (retryBtn) {
    const runId = retryBtn.dataset.runId;
    retryBtn.textContent = "Retrying…";
    retryBtn.disabled = true;
    try {
      const res = await fetch(`${API}/v1/pipelines/runs/${runId}/retry`, {
        method: "POST",
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        alert(d.detail ?? "Retry failed");
        retryBtn.textContent = "🔄 Retry";
        retryBtn.disabled = false;
      } else {
        window.location.reload();
      }
    } catch (err) {
      alert("Network error: " + err.message);
      retryBtn.textContent = "🔄 Retry";
      retryBtn.disabled = false;
    }
    return;
  }

  const cancelBtn = e.target.closest(".cancel-run-btn");
  if (cancelBtn) {
    if (!confirm("Cancel this running pipeline?")) return;
    const runId = cancelBtn.dataset.runId;
    cancelBtn.textContent = "Cancelling…";
    cancelBtn.disabled = true;
    try {
      const res = await fetch(`${API}/v1/pipelines/runs/${runId}/cancel`, {
        method: "POST",
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
      if (res.ok || res.status === 200) {
        window.location.reload();
      } else {
        alert("Cancel failed");
        cancelBtn.disabled = false;
        cancelBtn.textContent = "✕ Cancel";
      }
    } catch (err) {
      alert("Network error");
      cancelBtn.disabled = false;
      cancelBtn.textContent = "✕ Cancel";
    }
    return;
  }
});

// ── Expand/collapse run details ───────────────────────────────────────
document.querySelectorAll(".expand-run-btn").forEach(function(btn) {
  btn.addEventListener("click", function() {
    const runId = btn.dataset.runId;
    const details = document.getElementById(`run-details-${runId}`);
    if (details) {
      const isHidden = details.classList.contains("hidden");
      details.classList.toggle("hidden");
      btn.textContent = isHidden ? "▼ Hide step outputs" : "▶ Show step outputs";
    }
  });
});

let steps = [];

function addStep() {
  const idx = steps.length;
  steps.push({
    name: `Step ${idx}`,
    task_type: "llm_generate",
    execution_mode: "ai",
    task_config: {},
    input_mapping: null,
    condition: null,
    next_on_pass: null,
    next_on_fail: null,
    max_retries: 0,
  });
  renderSteps();
}

function removeStep(idx) {
  steps.splice(idx, 1);
  // Renumber steps and fix any branch references
  steps.forEach((s, i) => {
    s.step_order_preview = i;
    if (s.next_on_pass != null && s.next_on_pass >= steps.length) s.next_on_pass = null;
    if (s.next_on_fail != null && s.next_on_fail >= 0 && s.next_on_fail >= steps.length) s.next_on_fail = null;
  });
  renderSteps();
}

function renderSteps() {
  const container = document.getElementById("steps-container");
  const noMsg = document.getElementById("no-steps-msg");
  noMsg.classList.toggle("hidden", steps.length > 0);

  container.innerHTML = steps.map((step, i) => {
    const stepIdxOptions = steps.map((_, j) =>
      `<option value="${j}" ${step.next_on_pass === j ? "selected" : ""}>${j}</option>`
    ).join("");
    const failIdxOptions = [
      `<option value="-1" ${step.next_on_fail === -1 ? "selected" : ""}>fail pipeline (default)</option>`,
      ...steps.map((_, j) => `<option value="${j}" ${step.next_on_fail === j ? "selected" : ""}>→ step ${j}</option>`)
    ].join("");

    return `
      <div class="border border-gray-200 rounded-xl p-4 bg-gray-50">
        <!-- Step header -->
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center gap-2">
            <span class="w-7 h-7 rounded-full bg-violet-600 text-white text-xs font-bold flex items-center justify-center flex-shrink-0">${i}</span>
            <input
              type="text"
              value="${step.name.replace(/"/g, '&quot;')}"
              onchange="steps[${i}].name = this.value; syncJSON()"
              class="text-sm font-semibold bg-transparent border-b border-transparent hover:border-gray-300 focus:border-violet-500 outline-none px-1 w-48"
            />
          </div>
          <button type="button" onclick="removeStep(${i})" class="text-red-400 hover:text-red-600 text-sm px-2">✕</button>
        </div>

        <!-- Type + Mode -->
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-xs text-gray-500 font-medium">Task Type</label>
            <select onchange="steps[${i}].task_type = this.value; syncJSON()"
              class="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-sm mt-1 focus:ring-2 focus:ring-violet-500 outline-none">
              <optgroup label="AI Tasks">${AI_TYPES.map(t => `<option value="${t.value}" ${step.task_type === t.value ? "selected" : ""}>${t.label}</option>`).join("")}</optgroup>
              <optgroup label="Human Tasks">${HUMAN_TYPES.map(t => `<option value="${t.value}" ${step.task_type === t.value ? "selected" : ""}>${t.label}</option>`).join("")}</optgroup>
            </select>
          </div>
          <div>
            <label class="text-xs text-gray-500 font-medium">Mode</label>
            <select onchange="steps[${i}].execution_mode = this.value; syncJSON()"
              class="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-sm mt-1 focus:ring-2 focus:ring-violet-500 outline-none">
              <option value="ai" ${step.execution_mode === "ai" ? "selected" : ""}>🤖 AI (automatic)</option>
              <option value="human" ${step.execution_mode === "human" ? "selected" : ""}>👤 Human (marketplace)</option>
            </select>
          </div>
        </div>

        <!-- Input mapping -->
        <div class="mt-3">
          <label class="text-xs text-gray-500 font-medium">Input Mapping <span class="font-normal text-gray-400">(optional JSON — how to pull data from prior steps)</span></label>
          <textarea
            rows="2"
            placeholder='{"prompt": "$.steps.${i > 0 ? i-1 : 0}.output.text", "url": "$.input.url"}'
            onchange="try { steps[${i}].input_mapping = this.value ? JSON.parse(this.value) : null; syncJSON(); } catch(e) {}"
            class="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-xs font-mono mt-1 resize-none focus:ring-2 focus:ring-violet-500 outline-none"
          ></textarea>
        </div>

        <!-- Condition branches (collapsible) -->
        <div class="mt-3 border-t border-gray-200 pt-3">
          <button type="button"
            onclick="this.nextElementSibling.classList.toggle('hidden'); this.textContent = this.textContent.includes('▶') ? '▼ Condition & Branches' : '▶ Condition & Branches'"
            class="text-xs text-violet-600 hover:text-violet-800 font-medium">
            ${(step.condition || step.next_on_pass != null || step.next_on_fail != null) ? '▼' : '▶'} Condition & Branches
          </button>
          <div class="space-y-2 mt-2 ${(step.condition || step.next_on_pass != null || step.next_on_fail != null) ? '' : 'hidden'}">
            <div>
              <label class="text-xs text-gray-500 font-medium">Run condition <span class="font-normal text-gray-400">(skip step if false)</span></label>
              <input
                type="text"
                value="${(step.condition ?? '').replace(/"/g, '&quot;')}"
                placeholder='$.steps.${i > 0 ? i-1 : 0}.output.score >= 0.8'
                onchange="steps[${i}].condition = this.value || null; syncJSON()"
                class="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-xs font-mono mt-1 focus:ring-2 focus:ring-violet-500 outline-none"
              />
              <p class="text-xs text-gray-400 mt-0.5">Supported: ==, !=, &gt;, &gt;=, &lt;, &lt;=  |  e.g. <code>$.steps.0.output.score &gt;= 0.8</code></p>
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="text-xs text-gray-500 font-medium">On success → step</label>
                <select onchange="steps[${i}].next_on_pass = this.value === '' ? null : parseInt(this.value); syncJSON()"
                  class="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-xs mt-1">
                  <option value="" ${step.next_on_pass == null ? "selected" : ""}>next sequential</option>
                  ${stepIdxOptions}
                </select>
              </div>
              <div>
                <label class="text-xs text-gray-500 font-medium">On failure → step</label>
                <select onchange="steps[${i}].next_on_fail = this.value === '' ? null : parseInt(this.value); syncJSON()"
                  class="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-xs mt-1">
                  <option value="" ${step.next_on_fail == null ? "selected" : ""}>fail pipeline</option>
                  ${failIdxOptions}
                </select>
              </div>
            </div>
            <!-- Auto-retry -->
            <div>
              <label class="text-xs text-gray-500 font-medium">Auto-retry on failure</label>
              <select onchange="steps[${i}].max_retries = parseInt(this.value); syncJSON()"
                class="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-xs mt-1">
                <option value="0" ${(step.max_retries ?? 0) === 0 ? "selected" : ""}>No retry (fail immediately)</option>
                <option value="1" ${step.max_retries === 1 ? "selected" : ""}>Retry 1×</option>
                <option value="2" ${step.max_retries === 2 ? "selected" : ""}>Retry 2× (with backoff)</option>
                <option value="3" ${step.max_retries === 3 ? "selected" : ""}>Retry 3× (with backoff)</option>
                <option value="5" ${step.max_retries === 5 ? "selected" : ""}>Retry 5× (with backoff)</option>
              </select>
            </div>
          </div>
        </div>
      </div>
      ${i < steps.length - 1 ? `
        <div class="flex justify-center">
          <div class="flex flex-col items-center">
            <div class="w-0.5 h-4 bg-gray-300"></div>
            <span class="text-gray-400 text-xs">↓</span>
          </div>
        </div>
      ` : ''}
    `;
  }).join("");

  syncJSON();
}

function syncJSON() {
  document.getElementById("steps-json-input").value = JSON.stringify(
    steps.map((s, i) => ({
      name: s.name,
      task_type: s.task_type,
      execution_mode: s.execution_mode,
      task_config: s.task_config || {},
      input_mapping: s.input_mapping || null,
      condition: s.condition || null,
      next_on_pass: s.next_on_pass ?? null,
      next_on_fail: s.next_on_fail ?? null,
      max_retries: s.max_retries ?? 0,
    }))
  );
}

// Init with empty steps
renderSteps();

// Pre-populate run input textarea with default JSON
var runInput = document.getElementById("run-input-json");
if (runInput && !runInput.value) {
  runInput.value = "{}";
}
