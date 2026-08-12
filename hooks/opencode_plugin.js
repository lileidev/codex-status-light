/**
 * AgentsLight OpenCode plugin
 *
 * Drives the shared AgentsLight menu-bar light from OpenCode lifecycle events.
 * This is a drop-in for the standalone `opencode-status-light` plugin: instead of
 * calling a bundled opencode CLI, it calls the shared `agents-light` CLI so every
 * OpenCode session lands in ~/.agents-status-light/sessions and shows up in the
 * single AgentsLight window alongside Claude and Codex.
 *
 * Installed to ~/.config/opencode/plugins/status-light.js (OpenCode loads any
 * plugin in that directory). Does not rely on the LLM choosing to call a tool.
 */

import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const CLI = join(homedir(), ".agents-status-light", "bin", "agents-light");

// Each OpenCode process gets its own stable session id so multiple concurrent
// instances do not overwrite each other's status light. Using process.pid keeps
// the id stable even if the plugin module is loaded more than once within the
// same OpenCode process.
const SESSION_ID = process.env.OPENCODE_SESSION_ID || process.pid.toString();

let currentState = null;
let currentStreaming = false;
let lastToolTime = 0;
let awaitingUserInput = false;

// Debounce CLI writes so a burst of events (e.g. streaming tokens + tool calls)
// coalesces into a single subprocess spawn instead of one per event.
let pendingCall = null;
let debounceTimer = null;
const DEBOUNCE_MS = 150;

function callCli(state, message, isStreaming = false) {
  pendingCall = { state, message, isStreaming };
  if (debounceTimer) return;

  debounceTimer = setTimeout(() => {
    const call = pendingCall;
    pendingCall = null;
    debounceTimer = null;
    if (!call) return;

    const args = [
      call.state,
      "--session", SESSION_ID,
      "--source", "plugin",
      "--message", call.message,
      "--quiet",
    ];
    if (call.isStreaming) args.push("--streaming");

    try {
      const child = spawn(CLI, args, { stdio: "ignore" });
      child.on("error", () => {
        // Best-effort: do not let status-light failures break OpenCode.
      });
      // Avoid leaving defunct/zombie processes if the parent never waits.
      child.unref();
    } catch {
      // Best-effort: do not let status-light failures break OpenCode.
    }
  }, DEBOUNCE_MS);
}

async function setState(state, message, isStreaming = false) {
  if (awaitingUserInput && state !== "waiting" && state !== "error") {
    return;
  }
  if (currentState === state && currentStreaming === isStreaming && state !== "running") {
    return;
  }
  currentState = state;
  currentStreaming = isStreaming;
  callCli(state, message, isStreaming);
}

function toolName(event) {
  const props = event?.properties || {};
  return props.tool || props.tool_name || props.name || event?.tool || event?.tool_name || "";
}

function truncateHeader(header, max = 40) {
  if (!header) return "Question";
  const text = header.trim().split(/\r?\n/)[0];
  if (!text) return "Question";
  return text.length > max ? text.slice(0, max - 3) + "..." : text;
}

export const StatusLightPlugin = async () => {
  return {
    event: async ({ event }) => {
      const type = event?.type;
      if (!type) return;

      switch (type) {
        case "session.created":
          // New session means work is starting.
          await setState("running", "OpenCode is working");
          break;

        case "session.status": {
          // A status update may indicate idle or ongoing work.
          const props = event?.properties;
          if (props?.status?.type === "idle") {
            await setState("done", "OpenCode turn completed");
          } else {
            await setState("running", "OpenCode is working");
          }
          break;
        }

        case "message.part.updated":
          // Model is actively streaming output → blink blue.
          await setState("running", "OpenCode is working", true);
          break;

        case "tool.execute.before": {
          // Throttle: avoid resetting running on every single tool call.
          const now = Date.now();
          if (currentState === "waiting") {
            // User just replied; resume running.
            await setState("running", "OpenCode is working");
          } else if (currentStreaming || now - lastToolTime > 2000) {
            await setState("running", "OpenCode is working");
          }
          lastToolTime = now;
          break;
        }

        case "permission.asked": {
          awaitingUserInput = true;
          const tool = toolName(event);
          await setState("waiting", tool ? `Approval needed: ${tool}` : "Approval needed");
          break;
        }

        case "permission.replied":
          awaitingUserInput = false;
          await setState("running", "OpenCode is working");
          break;

        case "session.error": {
          const tool = toolName(event);
          await setState("error", tool ? `Tool failed: ${tool}` : "Tool failed");
          break;
        }

        case "session.idle":
          // Idle means OpenCode is ready and waiting for the next user prompt.
          await setState("done", "OpenCode turn completed");
          break;

        default:
          if (
            type === "question.v2.asked" ||
            type === "question.asked"
          ) {
            const props = event?.properties;
            const questions = props?.questions;
            const header = questions?.[0]?.header || "Question";
            awaitingUserInput = true;
            await setState("waiting", `Question: ${truncateHeader(header)}`);
          } else if (
            type === "question.v2.replied" ||
            type === "question.replied" ||
            type === "question.v2.rejected" ||
            type === "question.rejected"
          ) {
            awaitingUserInput = false;
            await setState("running", "OpenCode is working");
          }
          break;
      }
    },
  };
};

export default StatusLightPlugin;