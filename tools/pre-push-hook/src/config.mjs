// Reads the API key and URL. Environment wins over the ~/.pipelineiq file.
import { readFile } from "node:fs/promises";
import { join } from "node:path";

// DEFAULT: local dev API address when nothing is configured.
export const DEFAULT_API_URL = "http://127.0.0.1:8000";
// DEFAULT: the dashboard dev server address, used for the link in desktop notifications.
export const DEFAULT_DASHBOARD_URL = "http://localhost:5173";

/** Parse `key=value` lines (and a bare `piq_...` line) from the ~/.pipelineiq file text. */
export function parseConfigFile(text) {
  const out = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq === -1) {
      // DEFAULT: a file holding only the key (one line) is accepted.
      if (line.startsWith("piq_")) out.api_key = line;
      continue;
    }
    const name = line.slice(0, eq).trim().toLowerCase();
    const value = line.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
    if (name === "api_key" || name === "pipelineiq_api_key") out.api_key = value;
    if (name === "dashboard_url") out.dashboard_url = value;
    if (name === "block_when_no_tty") out.block_when_no_tty = /^(true|1|yes|on)$/i.test(value);
    if (name === "api_url" || name === "pipelineiq_api_url") out.api_url = value;
  }
  return out;
}

/** Return { apiKey, apiUrl, dashboardUrl, blockWhenNoTty }; apiKey is null when none is configured. Never throws. */
export async function loadConfig(env, home) {
  let file = {};
  try {
    if (home) file = parseConfigFile(await readFile(join(home, ".pipelineiq"), "utf8"));
  } catch {
    // missing or unreadable file: same as no file
  }
  const apiKey = (env.PIPELINEIQ_API_KEY || file.api_key || "").trim() || null;
  const apiUrl = (env.PIPELINEIQ_API_URL || file.api_url || DEFAULT_API_URL).trim();
  // DEFAULT: blocking without a terminal is opt-in, so it is off unless the file says true.
  const dashboardUrl = (file.dashboard_url || DEFAULT_DASHBOARD_URL).trim();
  return { apiKey, apiUrl, dashboardUrl, blockWhenNoTty: file.block_when_no_tty === true };
}
