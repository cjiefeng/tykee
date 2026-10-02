-- 0009: web access (§7.5). Server-side web tool calls per response, from the response's
-- usage.server_tool_use block. Searches are billed per search (pricing.web_search) and feed the
-- daily search cap; fetches are free beyond tokens but recorded for the dashboard.
ALTER TABLE usage ADD COLUMN web_search_requests INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage ADD COLUMN web_fetch_requests  INTEGER NOT NULL DEFAULT 0;
