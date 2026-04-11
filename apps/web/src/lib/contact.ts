/**
 * Contact / support email addresses used across the site.
 *
 * Overridable per environment via CONTACT_EMAIL / SUPPORT_EMAIL.
 * Defaults target the production rebaselabs.online mailbox. Every
 * page should import from here rather than hardcoding — that way
 * a tenant can change the address in one place.
 */
export const CONTACT_EMAIL =
  import.meta.env.CONTACT_EMAIL || "crowdsource@rebaselabs.online";

export const SUPPORT_EMAIL =
  import.meta.env.SUPPORT_EMAIL || "support@rebaselabs.online";

/** Build a mailto: href with a subject. */
export function mailto(email: string, subject?: string): string {
  if (!subject) return `mailto:${email}`;
  return `mailto:${email}?subject=${encodeURIComponent(subject)}`;
}
