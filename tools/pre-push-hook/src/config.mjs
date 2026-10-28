// Reads the API key and URL. Environment wins over the ~/.pipelineiq file.
import { readFile } from "node:fs/promises";
import { join } from "node:path";

// DEFAULT: local dev API address when nothing is configured.
export const DEFAULT_API_URL = "http://127.0.0.1:8000";

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
    if (name === "api_url" || name === "pipelineiq_api_url") out.api_url = value;
  }
  return out;
}

/** Return { apiKey, apiUrl }; apiKey is null when none is configured. Never throws. */
export async function loadConfig(env, home) {
  let file = {};
  try {
    if (home) file = parseConfigFile(await readFile(join(home, ".pipelineiq"), "utf8"));
  } catch {
    // missing or unreadable file: same as no file
  }
  const apiKey = (env.PIPELINEIQ_API_KEY || file.api_key || "").trim() || null;
  const apiUrl = (env.PIPELINEIQ_API_URL || file.api_url || DEFAULT_API_URL).trim();
  return { apiKey, apiUrl };
}
