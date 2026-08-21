// research-radar-feedback — Cloudflare Worker
// Mirrors feedback/server.py: appends feedback / keyword requests to the
// GitHub repo via the Contents API, so the site works without the local
// 127.0.0.1:8710 service.
//
// Secrets (wrangler secret put):
//   GH_TOKEN  fine-grained PAT, repo yan-zys/research-radar, contents:write
//   APP_KEY   shared key required as &k= on every request (anti-casual-abuse)

const OWNER = "yan-zys";
const REPO = "research-radar";
const BRANCH = "main";
const RATINGS = ["good", "ok", "bad", "read", "star"];
const MAX_PAPER_IDS = 10;
const MAX_REASON = 200;
const MAX_TEXT = 1000;

function json(status, obj, extra) {
  const headers = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Private-Network": "true",
  };
  if (extra) Object.assign(headers, extra);
  return new Response(JSON.stringify(obj), { status, headers });
}

function shanghaiDate() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
}

function safePaperId(s) {
  s = String(s || "").slice(0, 200);
  return /^[A-Za-z0-9:._-]+$/.test(s) ? s : null;
}

function splitPaperIds(raw) {
  return String(raw || "")
    .split(/[,，;；\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, MAX_PAPER_IDS);
}

async function ghReadFile(env, path) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${path}?ref=${BRANCH}`;
  const resp = await fetch(url, {
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "research-radar-feedback",
    },
  });
  if (resp.status === 404) return { exists: false, sha: null, text: "" };
  if (!resp.ok) throw new Error(`github read ${path}: HTTP ${resp.status}`);
  const data = await resp.json();
  const text = atob(String(data.content).replace(/\n/g, ""));
  return { exists: true, sha: data.sha, text };
}

async function ghWriteFile(env, path, text, sha, message) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${path}`;
  const body = {
    message,
    content: btoa(unescape(encodeURIComponent(text))),
    branch: BRANCH,
  };
  if (sha) body.sha = sha;
  const resp = await fetch(url, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "research-radar-feedback",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`github write ${path}: HTTP ${resp.status}`);
}

// Append one JSON line to a repo file. Retries once on sha conflict.
async function appendJsonl(env, path, lineObj, mutate, message) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const file = await ghReadFile(env, path);
    const lines = file.text ? file.text.split("\n").filter((l) => l.trim()) : [];
    const next = mutate(lines, lineObj);
    if (next === null) return; // dedup: nothing to write
    const text = next.join("\n") + "\n";
    try {
      await ghWriteFile(env, path, text, file.sha, message);
      return;
    } catch (e) {
      if (attempt === 1) throw e;
    }
  }
}

function mutateFeedback(lines, entry) {
  // Same day, same paper_id + same rating -> update in place (reason may change).
  for (let i = lines.length - 1; i >= 0; i--) {
    let obj;
    try {
      obj = JSON.parse(lines[i]);
    } catch (e) {
      continue;
    }
    if (obj.paper_id === entry.paper_id && obj.rating === entry.rating) {
      if (entry.reason === (obj.reason || "")) return null;
      const updated = Object.assign({}, obj, entry);
      lines[i] = JSON.stringify(updated);
      return lines;
    }
  }
  lines.push(JSON.stringify(entry));
  return lines;
}

function mutateAppend(lines, entry) {
  lines.push(JSON.stringify(entry));
  return lines;
}

async function handleFeedback(url, env) {
  const paperId = safePaperId(url.searchParams.get("paper_id") || "");
  const rating = String(url.searchParams.get("rating") || "");
  const reason = String(url.searchParams.get("reason") || "").slice(0, MAX_REASON);
  if (!paperId || !RATINGS.includes(rating)) {
    return json(400, { ok: false, error: "invalid params" });
  }
  const ts = Math.floor(Date.now() / 1000);
  const entry = { ts, paper_id: paperId, rating };
  if (reason) entry.reason = reason;
  const path = `input/user_feedback/${shanghaiDate()}.jsonl`;
  await appendJsonl(env, path, entry, mutateFeedback, `feedback: ${rating} ${paperId}`);
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Private-Network": "true",
    },
  });
}

async function handleKeywordRequest(url, env, type) {
  let entry;
  let label;
  if (type === "keywords") {
    const text = String(url.searchParams.get("text") || "").trim().slice(0, MAX_TEXT);
    if (!text) return json(400, { ok: false, error: "missing text" });
    entry = { type: "keywords", text, ts: Math.floor(Date.now() / 1000) };
    label = "keywords";
  } else {
    const ids = splitPaperIds(url.searchParams.get("ids") || "");
    if (!ids.length) return json(400, { ok: false, error: "missing ids" });
    entry = { type: "papers", ids, ts: Math.floor(Date.now() / 1000) };
    label = "papers";
  }
  const path = `input/keyword_requests/${shanghaiDate()}.jsonl`;
  await appendJsonl(env, path, entry, mutateAppend, `keyword request: ${label}`);
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Private-Network": "true",
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Access-Control-Allow-Private-Network": "true",
          "Access-Control-Max-Age": "86400",
        },
      });
    }
    if (request.method !== "GET") {
      return json(405, { ok: false, error: "method not allowed" });
    }
    if (!env.APP_KEY || url.searchParams.get("k") !== env.APP_KEY) {
      return json(403, { ok: false, error: "bad key" });
    }
    try {
      if (url.pathname === "/feedback") return await handleFeedback(url, env);
      if (url.pathname === "/keywords") return await handleKeywordRequest(url, env, "keywords");
      if (url.pathname === "/keyword_papers") return await handleKeywordRequest(url, env, "papers");
      return json(404, { ok: false, error: "not found" });
    } catch (e) {
      return json(502, { ok: false, error: String(e && e.message ? e.message : e) });
    }
  },
};
