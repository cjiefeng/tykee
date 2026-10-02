-- 0013: web search rejects country "SG" in user_location ("Country code SG is not supported"),
-- and every request carrying web tools got a 400 (§7.5). Drop it from existing installs; the city
-- and timezone still localise results. Fresh installs get the seed without a country.
UPDATE settings
SET value_json = json_remove(value_json, '$.country'), updated_at = datetime('now')
WHERE key = 'web.user_location' AND json_extract(value_json, '$.country') = 'SG';
