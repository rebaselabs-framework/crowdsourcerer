/**
 * PUT /api/worker/availability-schedule
 * Translates frontend weekly_schedule format to backend slots array format
 * and proxies to PUT /v1/worker/availability.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

const DAY_MAP: Record<string, number> = {
  monday: 0, tuesday: 1, wednesday: 2, thursday: 3,
  friday: 4, saturday: 5, sunday: 6,
};

export const PUT: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  let weekly_schedule: Record<string, Array<{ start: number; end: number }>> = {};
  try {
    const body = await request.json();
    weekly_schedule = body.weekly_schedule ?? {};
  } catch {
    return new Response(JSON.stringify({ detail: "Invalid JSON" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Convert {monday: [{start, end}]} → [{day_of_week, start_hour, end_hour}]
  const slots: Array<{ day_of_week: number; start_hour: number; end_hour: number }> = [];
  for (const [dayKey, timeSlots] of Object.entries(weekly_schedule)) {
    const dayOfWeek = DAY_MAP[dayKey];
    if (dayOfWeek === undefined) continue;
    for (const slot of timeSlots) {
      slots.push({ day_of_week: dayOfWeek, start_hour: slot.start, end_hour: slot.end });
    }
  }

  const res = await fetch(`${API_URL}/v1/worker/availability`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ slots }),
  });

  const data = await res.json().catch(() => ({}));
  return new Response(JSON.stringify(data), {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
};
