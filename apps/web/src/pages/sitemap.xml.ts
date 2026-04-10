// Dynamic sitemap generator
// Serves /sitemap.xml with all public pages

export async function GET() {
  const siteUrl = "https://crowdsourcerer.rebaselabs.online";
  const now = new Date().toISOString().split("T")[0];

  const staticPages = [
    { loc: "/", priority: "1.0", changefreq: "weekly" },
    { loc: "/pricing", priority: "0.9", changefreq: "monthly" },
    { loc: "/docs", priority: "0.8", changefreq: "weekly" },
    { loc: "/docs/api-reference", priority: "0.8", changefreq: "weekly" },
    { loc: "/docs/sandbox", priority: "0.8", changefreq: "weekly" },
    { loc: "/use-cases", priority: "0.9", changefreq: "monthly" },
    { loc: "/use-cases/web-research", priority: "0.8", changefreq: "monthly" },
    { loc: "/use-cases/document-parsing", priority: "0.8", changefreq: "monthly" },
    { loc: "/use-cases/entity-extraction", priority: "0.8", changefreq: "monthly" },
    { loc: "/use-cases/data-transformation", priority: "0.8", changefreq: "monthly" },
    { loc: "/use-cases/content-moderation", priority: "0.8", changefreq: "monthly" },
    { loc: "/register", priority: "0.7", changefreq: "monthly" },
    { loc: "/login", priority: "0.5", changefreq: "monthly" },
    { loc: "/tasks", priority: "0.6", changefreq: "daily" },
    { loc: "/widget", priority: "0.5", changefreq: "monthly" },
  ];

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${staticPages.map(({ loc, priority, changefreq }) => `  <url>
    <loc>${siteUrl}${loc}</loc>
    <lastmod>${now}</lastmod>
    <changefreq>${changefreq}</changefreq>
    <priority>${priority}</priority>
  </url>`).join("\n")}
</urlset>`;

  return new Response(xml, {
    headers: {
      "Content-Type": "application/xml; charset=utf-8",
      "Cache-Control": "public, max-age=3600",
    },
  });
}
