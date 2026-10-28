// Desktop notification for pushes with no terminal (GUI git clients, AI agents).
// Never throws and never blocks: any failure (missing binary, bad platform) is ignored.
import { execFile } from "node:child_process";

// DEFAULT: notifications must not hold up a push for long.
export const NOTIFY_TIMEOUT_MS = 3000;

/** Make text safe for an AppleScript string literal: no control characters, escaped \ and ". */
export function appleScriptEscape(text) {
  return String(text)
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"');
}

/** The command for this platform as { file, args }, or null when unsupported. No shell is used. */
export function notificationCommand(platform, title, message) {
  if (platform === "darwin") {
    const script = `display notification "${appleScriptEscape(message)}" with title "${appleScriptEscape(title)}"`;
    return { file: "osascript", args: ["-e", script] };
  }
  if (platform === "linux") {
    return { file: "notify-send", args: [String(title), String(message).replace(/[\u0000-\u001f\u007f]+/g, " ")] };
  }
  return null;
}

/** Send the notification. Always resolves; resolves false when skipped or failed. */
export function sendNotification(title, message, { platform = process.platform, exec = execFile } = {}) {
  return new Promise((resolve) => {
    try {
      const cmd = notificationCommand(platform, title, message);
      if (!cmd) return resolve(false);
      exec(cmd.file, cmd.args, { timeout: NOTIFY_TIMEOUT_MS }, (error) => resolve(!error));
    } catch {
      resolve(false);
    }
  });
}
