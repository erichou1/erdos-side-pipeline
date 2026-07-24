// Vercel serverless function: read / update the pipeline control command.
//
// The pipeline polls a tiny control.json on the "control" branch of the
// erdos-side-pipeline repo. This endpoint reads it and, on POST, updates it:
//   action=start   -> state:"run"     (resume working)
//   action=stop    -> state:"pause"   (idle; process stays alive, keeps polling)
//   action=restart -> restart:<nonce> (one browser relaunch; problems NOT reset)
//
// Requires a Vercel env var GITHUB_TOKEN: a fine-grained PAT for erichou1 with
// Contents: Read and write on the erdos-side-pipeline repo. Optional overrides:
//   CONTROL_REPO (default erdos-side-pipeline), CONTROL_BRANCH (default control),
//   CONTROL_PATH (default control.json), CONTROL_OWNER (default erichou1).

const OWNER = process.env.CONTROL_OWNER || "erichou1";
const REPO = process.env.CONTROL_REPO || "erdos-side-pipeline";
const BRANCH = process.env.CONTROL_BRANCH || "control";
const FILE = process.env.CONTROL_PATH || "control.json";
const API = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${FILE}`;

function ghHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "side-pipeline-control",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function getFile(token) {
  const r = await fetch(`${API}?ref=${BRANCH}&t=${Date.now()}`, {
    headers: ghHeaders(token),
    cache: "no-store",
  });
  if (r.status === 404) return { sha: null, content: { state: "run", restart: 0 } };
  if (!r.ok) throw new Error(`GitHub GET ${r.status}: ${await r.text()}`);
  const j = await r.json();
  const content = JSON.parse(Buffer.from(j.content, "base64").toString("utf-8"));
  return { sha: j.sha, content };
}

async function putFile(token, sha, content, message) {
  const body = {
    message,
    branch: BRANCH,
    content: Buffer.from(JSON.stringify(content, null, 2) + "\n").toString("base64"),
  };
  if (sha) body.sha = sha;
  const r = await fetch(API, {
    method: "PUT",
    headers: { ...ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`GitHub PUT ${r.status}: ${await r.text()}`);
  return content;
}

export default async function handler(req, res) {
  const token = process.env.GITHUB_TOKEN;
  if (!token) return res.status(500).json({ error: "GITHUB_TOKEN is not configured" });
  try {
    if (req.method === "GET") {
      const { content } = await getFile(token);
      return res.status(200).json(content);
    }
    if (req.method === "POST") {
      let action = "";
      try {
        const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
        action = String(body.action || "").toLowerCase();
      } catch (_) {}
      if (!action) {
        const u = new URL(req.url, "http://localhost");
        action = String(u.searchParams.get("action") || "").toLowerCase();
      }
      const { sha, content } = await getFile(token);
      if (action === "start") content.state = "run";
      else if (action === "stop") content.state = "pause";
      else if (action === "restart") content.restart = Date.now();
      else if (action === "update") content.update = Date.now();
      else return res.status(400).json({ error: "action must be start | stop | restart | update" });
      const saved = await putFile(token, sha, content, `control: ${action}`);
      return res.status(200).json(saved);
    }
    res.setHeader("Allow", "GET, POST");
    return res.status(405).json({ error: "method not allowed" });
  } catch (e) {
    return res.status(500).json({ error: String((e && e.message) || e) });
  }
}
