const SVG_NS = "http://www.w3.org/2000/svg";
const WORLD = { w: 1400, h: 820 };
const MAX_DIRECT_CHILDREN = 18;

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  maxTextSize: 1_000_000,        // arvore do Aether passa do default 50k facilmente
  maxEdges: 5000,                // idem
  securityLevel: 'loose',        // habilita html no label sem strip excessivo
  mindmap: { padding: 12 },
});

function rankingCampaignFromStorage() {
  const saved = localStorage.getItem("aetherRankingCampaign") || "active";
  return ["all", "legacy_pre_aether"].includes(saved) ? "active" : saved;
}

function rankingTabFromStorage() {
  return localStorage.getItem("aetherRankingTab") === "discoveries" ? "discoveries" : "validated";
}

const state = {
  data: null,
  ranking: null,
  campaigns: null,
  rankingCampaign: rankingCampaignFromStorage(),
  rankingTab: rankingTabFromStorage(),
  // Default = mindmap. Tree foi descontinuada visualmente (legivel so com poucos nodes).
  viewMode: window.location.hash === "#ranking" ? "ranking" : (window.location.hash === "#tree" ? "tree" : "mindmap"),
  selected: null,
  displayNodeMap: new Map(),
  expandedGroups: new Set(),
  activeNodeIds: new Set(),
  activePathIds: new Set(),
  agentTargets: [],
  nodeIndex: new Map(),
  showPruned: localStorage.getItem("aetherShowPruned") === "1",
  prunerTargets: [],
  recentPrunedIds: new Set(),
  view: { x: 0, y: 0, scale: 1, initialized: false },
  panning: false,
  panLast: null,
};

const $ = (id) => document.getElementById(id);

function cacheBust(path) {
  const sep = String(path).includes("?") ? "&" : "?";
  return `${path}${sep}_=${Date.now()}`;
}

async function api(path, opts = {}) {
  const method = String(opts.method || "GET").toUpperCase();
  const res = await fetch(method === "GET" ? cacheBust(path) : path, {
    cache: "no-store",
    ...opts,
    headers: {
      "Cache-Control": "no-cache",
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function shortText(value, max = 26) {
  const text = String(value || "");
  return text.length > max ? `${text.slice(0, max - 1)}...` : text;
}

function money(value) {
  return Number(value || 0).toLocaleString("pt-BR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function readinessLabel(value) {
  return {
    paper_ready: "Validada",
    needs_review: "Revisar",
    archive: "Arquivo",
  }[value] || value;
}

function bucketLabel(value) {
  return {
    legacy: "Legado",
    web: "Web",
    aether: "Aether",
  }[value] || value || "Aether";
}

function discoveryStageLabel(value) {
  return {
    "critic aprovado": "Critic aprovado",
    validada: "Validada",
    backtest: "Backtest",
    scout: "Scout",
    explorando: "Explorando",
  }[value] || value || "Pipeline";
}

function discoverySourceLabel(row) {
  const source = String(row?.source_path || row?.source_type || "").toLowerCase();
  if (source === "openai" || source.includes("openai")) return "OpenAI";
  if (source.includes("web")) return "Web";
  if (source.includes("legacy")) return "Legado";
  return "Aether";
}

function compactModel(value) {
  return String(value || "-")
    .replace("claude-", "")
    .replace("-20250514", "")
    .replace("opus-4-7", "opus 4.7")
    .replace("sonnet-4", "sonnet 4");
}

function traceSummary(trace) {
  const output = String(trace.output_excerpt || "");
  try {
    const clean = output
      .trim()
      .replace(/^```(?:json)?/i, "")
      .replace(/```$/i, "")
      .trim();
    const payload = JSON.parse(clean);
    const first = Array.isArray(payload.hypotheses) ? payload.hypotheses[0] : null;
    if (first) return `${first.name || first.slug || "Hipotese"}: ${first.hypothesis || ""}`;
  } catch (_) {}
  return trace.summary || output || trace.error || "Sem resumo.";
}

function numValue(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function fmtNumber(value, digits = 2) {
  const n = numValue(value);
  return n == null ? "-" : n.toFixed(digits);
}

function fmtPct(value) {
  const n = numValue(value);
  return n == null ? "-" : `${n.toFixed(2)}%`;
}

function compactJson(value, max = 120) {
  if (value == null || value === "") return "-";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return shortText(text, max);
}

function probeLabel(probe) {
  if (!probe || typeof probe !== "object") return "-";
  const bits = [
    probe.symbol,
    probe.timeframe,
    probe.engine,
    probe.params ? compactJson(probe.params, 72) : null,
  ].filter(Boolean);
  return bits.join(" / ") || compactJson(probe, 120);
}

function latestNodeTasks(nodeId) {
  return (state.data?.tasks || [])
    .filter((t) => t.node_id === nodeId)
    .sort((a, b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || "")));
}

function latestNodeRuns(nodeId) {
  return (state.data?.runs || [])
    .filter((r) => r.node_id === nodeId)
    .sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")));
}

function latestNodeEvents(nodeId) {
  return (state.data?.events || [])
    .filter((e) => e.node_id === nodeId)
    .slice(0, 6);
}

function nodeArtifacts(nodeId) {
  return (state.data?.artifacts || []).filter((a) => a.node_id === nodeId).slice(0, 4);
}

function gateState(ok) {
  if (ok === true) return "pass";
  if (ok === false) return "fail";
  return "wait";
}

function renderGate(label, ok, detail) {
  const stateName = gateState(ok);
  const badge = stateName === "pass" ? "PASS" : stateName === "fail" ? "RECUSA" : "AGUARDA";
  return `
    <div class="gateRow ${stateName}">
      <span>${escapeHtml(badge)}</span>
      <strong>${escapeHtml(label)}</strong>
      <small>${escapeHtml(detail || "-")}</small>
    </div>
  `;
}

function buildGateRows(n) {
  const m = n.metrics || {};
  const best = m.best_probe || {};
  const trades = numValue(m.trade_count ?? best.trade_count);
  const pnl = numValue(m.pnl_pct ?? best.pnl_pct);
  const sharpe = numValue(m.sharpe ?? best.sharpe);
  const mdd = numValue(m.mdd_pct ?? best.mdd_pct);
  const score = numValue(n.score);
  const rows = [];
  rows.push(renderGate("Scout tecnico", m.ok === false ? false : (m.probe_count ? true : null), m.ok === false ? (m.error || "falha tecnica") : `${m.ok_count ?? "-"} ok / ${m.probe_count ?? "-"} probes`));
  rows.push(renderGate("Trades minimos do scout", trades == null ? null : trades >= 6, `${trades ?? "-"} / 6`));
  rows.push(renderGate("PnL scout nao pode sangrar", pnl == null ? null : pnl > -3, `${fmtPct(pnl)} > -3.00%`));
  rows.push(renderGate("Sharpe scout", sharpe == null ? null : sharpe >= 0.05, `${fmtNumber(sharpe)} >= 0.05`));
  rows.push(renderGate("Drawdown scout", mdd == null ? null : mdd <= 35, `${fmtPct(mdd)} <= 35.00%`));
  rows.push(renderGate("Score de sobrevivencia", score == null ? null : score >= 0.12, `${fmtNumber(score)} >= 0.12`));
  rows.push(renderGate("Validacao profunda", m.validation_passed === true ? true : (m.validation_stage ? false : null), `${m.validation_stage || "pendente"}${m.validation_reason ? " / " + m.validation_reason : ""}`));
  rows.push(renderGate("Trades para paper", trades == null ? null : trades >= 30, `${trades ?? "-"} / 30`));
  rows.push(renderGate("Score para paper", score == null ? null : score >= 0.42, `${fmtNumber(score)} >= 0.42`));
  rows.push(renderGate("Critic out-of-sample", m.critic_status === "approved" ? true : (m.critic_status === "rejected" ? false : null), m.critic_reason || m.critic_status || "pendente"));
  return rows.join("");
}

function renderNodeDetails(n) {
  const m = n.metrics || {};
  const best = m.best_probe || {};
  const tasks = latestNodeTasks(n.id);
  const runs = latestNodeRuns(n.id);
  const events = latestNodeEvents(n.id);
  const artifacts = nodeArtifacts(n.id);
  const task = tasks[0] || null;
  const run = runs[0] || null;
  const probes = (task?.payload?.probes || m.probe_specs || []).slice(0, 5);
  const engines = m.engines || (best.engine ? [best.engine] : []);
  const sourceTitle = m.source_title || artifacts.find((a) => a.kind === "web_source")?.title || n.source_path || "-";
  const sourceUrl = m.source_url || artifacts.find((a) => a.kind === "web_source")?.path || n.source_path || "";
  const sourceHtml = sourceUrl && /^https?:\/\//i.test(sourceUrl)
    ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">${escapeHtml(shortText(sourceTitle, 72))}</a>`
    : escapeHtml(shortText(sourceTitle, 72));

  $("nodeDetails").innerHTML = `
    <div class="detailBlock">
      <div class="detailTitle">Pesquisa</div>
      <div class="detailRows">
        <div><span>Origem</span><strong>${escapeHtml(n.source_type || m.generator || "aether")}</strong></div>
        <div><span>Modelo</span><strong>${escapeHtml(m.model || m.provider || "-")}</strong></div>
        <div><span>Fonte</span><strong>${sourceHtml}</strong></div>
        <div><span>Engines</span><strong>${escapeHtml((engines || []).join(", ") || "-")}</strong></div>
        <div><span>Invalida se</span><strong>${escapeHtml(m.invalidation || "-")}</strong></div>
        <div><span>Params atuais</span><strong>${escapeHtml(compactJson(best.params, 160))}</strong></div>
      </div>
    </div>
    <div class="detailBlock">
      <div class="detailTitle">Teste agora</div>
      <div class="detailRows">
        <div><span>Tarefa</span><strong>${escapeHtml(task ? `${task.status} / ${task.task_type}` : "sem tarefa ativa")}</strong></div>
        <div><span>Run</span><strong>${escapeHtml(run ? `${run.status} ${run.completed}/${run.total}` : "sem run recente")}</strong></div>
        <div><span>Throughput</span><strong>${escapeHtml(run?.throughput_per_min ? `${run.throughput_per_min} probes/min` : "-")}</strong></div>
        <div><span>Best probe</span><strong>${escapeHtml(probeLabel(best))}</strong></div>
      </div>
      <div class="probeList">
        ${probes.length ? probes.map((p) => `<div>${escapeHtml(probeLabel(p))}</div>`).join("") : `<div>Sem probes carregados neste snapshot.</div>`}
      </div>
    </div>
    <div class="detailBlock">
      <div class="detailTitle">Checklist de aprovacao</div>
      <div class="gateList">${buildGateRows(n)}</div>
    </div>
    <div class="detailBlock">
      <div class="detailTitle">Historico recente</div>
      <div class="eventList">
        ${events.length ? events.map((e) => `<div><strong>${escapeHtml(e.type)}</strong><span>${escapeHtml(shortText(e.message, 120))}</span></div>`).join("") : `<div><span>Sem evento recente para este node.</span></div>`}
      </div>
    </div>
  `;
}

function computeActivity(data) {
  const tasks = data.tasks || [];
  const agents = data.agents || [];
  const nodeIndex = new Map((data.nodes || []).map((n) => [n.id, n]));
  const taskIndex = new Map(tasks.map((t) => [t.id, t]));
  const activeNodeIds = new Set();
  for (const t of tasks) {
    if (t.status === "running" && t.node_id) activeNodeIds.add(t.node_id);
  }
  const agentTargets = [];
  for (const a of agents) {
    if (a.status !== "running") continue;
    const task = a.current_task_id ? taskIndex.get(a.current_task_id) : null;
    const nodeId = task && task.node_id ? task.node_id : null;
    const node = nodeId ? nodeIndex.get(nodeId) : null;
    if (nodeId) activeNodeIds.add(nodeId);
    agentTargets.push({
      agent: a,
      task,
      nodeId,
      node,
    });
  }
  // ancestors of any active node should also glow on the path
  const activePathIds = new Set(activeNodeIds);
  for (const id of activeNodeIds) {
    let current = nodeIndex.get(id);
    while (current && current.parent_id) {
      activePathIds.add(current.parent_id);
      current = nodeIndex.get(current.parent_id);
    }
  }
  // Pruning: nos pruned/paused recentes, atribuidos ao agent_role responsavel
  const prunedNodes = (data.nodes || []).filter((n) => ["pruned", "paused"].includes(n.status));
  const sortedPruned = prunedNodes.slice().sort((a, b) => {
    const ta = String(b.updated_at || ""); const tb = String(a.updated_at || "");
    return ta.localeCompare(tb);
  });
  const recentPrunedIds = new Set(sortedPruned.slice(0, 12).map((n) => n.id));
  const prunerTargets = sortedPruned.slice(0, 8).map((node) => ({
    node,
    nodeId: node.id,
    agentRole: node.agent_role || "archivist_agent",
    reason: node.prune_reason || "sem razao registrada",
  }));
  return { activeNodeIds, activePathIds, agentTargets, nodeIndex, prunerTargets, recentPrunedIds };
}

const FAMILY_COLORS = {
  donchian: "#5dd39e",
  bollinger: "#5db4ff",
  rsi: "#d6a84d",
  ma_crossover: "#c98bff",
  supertrend: "#ff7e6c",
  unknown: "#6b7280",
};
function familyColor(name) {
  if (FAMILY_COLORS[name]) return FAMILY_COLORS[name];
  // hash determinstico para nomes desconhecidos (web/llm)
  let h = 0;
  for (let i = 0; i < String(name).length; i += 1) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  return `hsl(${hue} 60% 55%)`;
}

function nodeFamily(node) {
  const m = node.metrics || {};
  const best = m.best_probe || {};
  return (
    String(best.engine || "").toLowerCase() ||
    String(node.engine || "").toLowerCase() ||
    String((node.tags || []).find((t) => /^engine:/.test(t)) || "").replace(/^engine:/, "") ||
    "unknown"
  );
}

function shannonEntropy(counts) {
  const total = counts.reduce((a, b) => a + b, 0);
  if (total <= 0 || counts.length <= 1) return 0;
  let h = 0;
  for (const c of counts) {
    if (c <= 0) continue;
    const p = c / total;
    h -= p * Math.log2(p);
  }
  const max = Math.log2(counts.length);
  return max > 0 ? h / max : 0;
}

function parseTs(value) {
  if (!value) return null;
  const t = Date.parse(value.replace(" ", "T"));
  return Number.isFinite(t) ? t : null;
}

function ageHuman(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "--";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}min`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

function formatDateTime(value) {
  const t = parseTs(value);
  if (!t) return "--";
  return new Intl.DateTimeFormat("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(t));
}

function isAfterReset(row, reset) {
  if (!reset?.active || !reset.reset_at) return true;
  const resetTs = parseTs(reset.reset_at);
  const rowTs = parseTs(row?.created_at || row?.updated_at);
  return Boolean(resetTs && rowTs && rowTs > resetTs);
}

function computeHealth(data) {
  const nodes = (data.nodes || []).filter((n) => n.id !== "root" && !String(n.id).startsWith("tf:"));
  const active = nodes.filter((n) => !["pruned", "paused", "archive"].includes(n.status));
  const pruned = nodes.filter((n) => ["pruned", "paused"].includes(n.status));
  const validated = nodes.filter((n) => n.status === "validated");

  // Familias por status
  const familyMap = new Map();
  for (const n of active) {
    const fam = nodeFamily(n);
    if (!familyMap.has(fam)) familyMap.set(fam, { name: fam, count: 0, validated: 0, candidates: 0 });
    const e = familyMap.get(fam);
    e.count += 1;
    if (n.status === "validated") e.validated += 1;
    if (["candidate", "promising", "expand", "expanded"].includes(n.status)) e.candidates += 1;
  }
  const families = [...familyMap.values()].sort((a, b) => b.count - a.count);
  const totalActive = families.reduce((a, e) => a + e.count, 0) || 1;
  const dominant = families[0] || null;
  const dominantPct = dominant ? (dominant.count / totalActive) * 100 : 0;
  const diversity = shannonEntropy(families.map((f) => f.count));

  // Idade do recorde global (validated com maior score)
  let bestNode = null;
  let bestScore = -Infinity;
  for (const n of validated) {
    const sc = Number(n.score || 0);
    if (sc > bestScore) { bestScore = sc; bestNode = n; }
  }
  const now = Date.now();
  const bestTs = bestNode ? (parseTs(bestNode.updated_at) || parseTs(bestNode.created_at)) : null;
  const bestAgeMs = bestTs ? now - bestTs : null;

  // Largura vs profundidade
  const depthBuckets = new Map();
  let maxDepth = 0;
  for (const n of active) {
    const d = Number(n.depth || 0);
    if (d > maxDepth) maxDepth = d;
    depthBuckets.set(d, (depthBuckets.get(d) || 0) + 1);
  }
  const breadth = Math.max(0, ...depthBuckets.values());
  const ratio = maxDepth > 0 ? breadth / maxDepth : breadth;

  // Forced exploration round detection (looks at recent events)
  let forcedRecent = false;
  const events = (data.events || []).slice(0, 30);
  const nowMs = Date.now();
  for (const e of events) {
    if (e.type !== "exploration_forced_round") continue;
    const ts = parseTs(e.ts);
    if (ts && nowMs - ts < 1000 * 60 * 10) { forcedRecent = true; break; }
  }

  // Critic stats
  let criticApproved = 0;
  let criticRejected = 0;
  let criticPending = 0;
  for (const n of nodes) {
    if (!n.metrics) continue;
    const cs = n.metrics.critic_status;
    if (cs === "approved") criticApproved += 1;
    else if (cs === "rejected") criticRejected += 1;
    else if (n.status === "validated") criticPending += 1;
  }

  // Top motivos de poda
  const reasonCounts = new Map();
  for (const n of pruned) {
    const r = String(n.prune_reason || "sem motivo").trim().slice(0, 80);
    reasonCounts.set(r, (reasonCounts.get(r) || 0) + 1);
  }
  const topReasons = [...reasonCounts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([reason, count]) => ({ reason, count }));

  // Veredito
  let verdict = "balanced";
  let hint = "exploracao saudavel";
  if (!active.length) {
    verdict = "balanced"; hint = "sem nos ativos ainda";
  } else if (dominantPct >= 65 && diversity < 0.55) {
    if (bestAgeMs != null && bestAgeMs > 1000 * 60 * 30) {
      verdict = "stuck";
      hint = `${dominant.name} domina e recorde nao avanca`;
    } else {
      verdict = "converging";
      hint = `${dominant.name} concentra ${dominantPct.toFixed(0)}% das ativas`;
    }
  } else if (diversity >= 0.75 && bestAgeMs != null && bestAgeMs < 1000 * 60 * 20) {
    verdict = "exploring";
    hint = "diversidade alta + progresso recente";
  } else if (bestAgeMs != null && bestAgeMs > 1000 * 60 * 45) {
    verdict = "stuck";
    hint = "recorde global parado ha muito tempo";
  }

  return {
    families,
    totalActive,
    dominant,
    dominantPct,
    diversity,
    bestNode,
    bestScore: bestNode ? bestScore : null,
    bestAgeMs,
    breadth,
    maxDepth,
    ratio,
    topReasons,
    verdict,
    hint,
    counts: { active: active.length, pruned: pruned.length, validated: validated.length },
    critic: { approved: criticApproved, rejected: criticRejected, pending: criticPending },
    forcedExploration: forcedRecent,
  };
}

function renderHealth(h) {
  const verdictMap = {
    exploring: "EXPLORANDO",
    balanced: "EQUILIBRADO",
    converging: "CONVERGINDO",
    stuck: "TRAVADO",
  };
  const cell = $("healthVerdict");
  cell.classList.remove("verdict-exploring", "verdict-balanced", "verdict-converging", "verdict-stuck");
  cell.classList.add(`verdict-${h.verdict}`);
  const forcedTag = h.forcedExploration ? " - FORCANDO" : "";
  $("hVerdict").textContent = (verdictMap[h.verdict] || h.verdict) + forcedTag;
  $("hVerdictHint").textContent = h.forcedExploration
    ? "rodada de exploracao forcada nos ultimos 10min"
    : h.hint;

  $("hBestAge").textContent = h.bestAgeMs != null ? ageHuman(h.bestAgeMs) : "--";
  $("hBestAgeHint").textContent = h.bestNode
    ? `melhor: ${shortText(h.bestNode.name || h.bestNode.id, 22)} (score ${Number(h.bestScore || 0).toFixed(2)})`
    : "ainda sem validados";

  $("hDiversity").textContent = h.diversity.toFixed(2);
  $("hDiversityFill").style.width = `${Math.round(h.diversity * 100)}%`;
  $("hDiversityFill").style.background = h.diversity >= 0.7 ? "var(--green)" : h.diversity >= 0.45 ? "var(--gold)" : "var(--red)";
  $("hDiversityHint").textContent = `${h.families.length} familias / ${h.counts.active} ativas`;

  $("hDominant").textContent = h.dominant ? h.dominant.name : "--";
  $("hDominantHint").textContent = h.dominant ? `${h.dominantPct.toFixed(0)}% das ativas` : "sem nos ativos";

  const stack = $("hFamilyStack");
  if (!h.families.length) {
    stack.innerHTML = "";
  } else {
    stack.innerHTML = h.families.map((f) => {
      const pct = (f.count / h.totalActive) * 100;
      return `<div class="seg" style="width:${pct.toFixed(2)}%;background:${familyColor(f.name)}" title="${escapeHtml(f.name)}: ${f.count} ativas">${pct >= 8 ? escapeHtml(shortText(f.name, 10)) : ""}</div>`;
    }).join("");
  }

  const c = h.critic || { approved: 0, rejected: 0, pending: 0 };
  $("hCritic").textContent = `${c.approved}/${c.rejected}`;
  $("hCriticHint").textContent = c.pending
    ? `${c.pending} aguardando avaliacao do Critic`
    : (c.approved + c.rejected ? `${c.rejected ? Math.round(c.rejected / (c.approved + c.rejected) * 100) : 0}% rejeitadas em out-of-sample` : "Critic ainda nao avaliou nada");

  const hyper = (state.data && state.data.hyper_controls) || null;
  if (hyper && typeof hyper === "object") {
    $("hHyper").textContent = `forca a cada ${hyper.force_exploration_every || "?"}`;
    const ageStr = hyper.best_age_min == null ? "?" : `${Math.round(hyper.best_age_min)}min`;
    $("hHyperHint").textContent = `verdict=${hyper.verdict || "?"} / age=${ageStr} / reject=${Math.round((hyper.critic_reject_rate || 0) * 100)}%`;
  } else {
    $("hHyper").textContent = "off";
    $("hHyperHint").textContent = "Hyper Agent nao esta rodando";
  }

  $("hShape").textContent = `${h.breadth}w / ${h.maxDepth}d`;
  $("hShapeHint").textContent = h.ratio >= 6 ? "raso e largo - explorando" : h.ratio >= 2.5 ? "balanceado" : "fundo e estreito - convergindo";

  const reasons = $("hPruneReasons");
  reasons.innerHTML = h.topReasons.length
    ? h.topReasons.map((r) => `<div class="reasonRow"><span title="${escapeHtml(r.reason)}">${escapeHtml(shortText(r.reason, 60))}</span><strong>${r.count}</strong></div>`).join("")
    : `<div class="reasonRow"><span>sem podas registradas</span><strong>0</strong></div>`;
}

function statusClass(status) {
  if (!status) return "candidate";
  if (["pruned", "paused"].includes(status)) return "pruned";
  if (["validated"].includes(status)) return "validated";
  if (["expand", "expanded"].includes(status)) return "expand";
  if (["promising"].includes(status)) return "promising";
  return "candidate";
}

function groupKey(child) {
  if (child.id === "legacy-diag-root" || child.source_type === "diag") return "diag";
  if (child.source_type) return child.source_type;
  if (child.timeframe) return `timeframe-${child.timeframe}`;
  if (child.status) return child.status;
  return "outros";
}

function groupLabel(key) {
  const labels = {
    approved: "Approved",
    staging: "Staging",
    diag: "Diagnostics",
    validated: "Validated",
    pruned: "Pruned",
    paused: "Paused",
    promising: "Promising",
    candidate: "Candidate",
    visited: "Visited",
    outros: "Outros",
  };
  if (key.startsWith("timeframe-")) return `Timeframe ${key.replace("timeframe-", "")}`;
  return labels[key] || key;
}

function groupStatus(children) {
  const statuses = new Set(children.map((c) => c.status));
  if ([...statuses].every((s) => ["pruned", "paused"].includes(s))) return "pruned";
  if (statuses.has("expand") || statuses.has("expanded")) return "expand";
  if (statuses.has("validated")) return "validated";
  if (statuses.has("promising")) return "promising";
  return "candidate";
}

function buildRawTree(nodes) {
  const hidden = state.showPruned ? ["archive"] : ["pruned", "paused", "archive"];
  let visible = nodes.filter((n) => !hidden.includes(n.status));

  // Mantem apenas 1 no de pesquisa em andamento por researcher (llm_agent, web_research, ...).
  // Validados ja aparecem no ranking, podados/pausados ja sairam do filtro acima.
  // Se 'showPruned' estiver ligado, mostra tudo (modo debug).
  if (!state.showPruned) {
    const RESEARCHER_TYPES = new Set(["llm_agent", "web_research"]);
    const groups = new Map();
    for (const n of visible) {
      const key = n.source_type;
      if (!RESEARCHER_TYPES.has(key)) continue;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(n);
    }

    const toHide = new Set();
    for (const group of groups.values()) {
      // Esconde validados desse pesquisador (vivem no ranking).
      for (const n of group) {
        if (n.status === "validated") toHide.add(n.id);
      }
      // Dos restantes (em pesquisa/teste), mantem apenas o mais recente.
      const inflight = group.filter((n) => !toHide.has(n.id));
      if (inflight.length <= 1) continue;
      const sorted = inflight.slice().sort((a, b) => {
        const aActive = state.activeNodeIds.has(a.id) ? 1 : 0;
        const bActive = state.activeNodeIds.has(b.id) ? 1 : 0;
        if (aActive !== bActive) return bActive - aActive;
        const ta = String(a.updated_at || a.created_at || "");
        const tb = String(b.updated_at || b.created_at || "");
        return tb.localeCompare(ta);
      });
      for (const n of sorted.slice(1)) toHide.add(n.id);
    }

    if (toHide.size) {
      const byParent = new Map();
      for (const n of visible) {
        if (!n.parent_id) continue;
        if (!byParent.has(n.parent_id)) byParent.set(n.parent_id, []);
        byParent.get(n.parent_id).push(n.id);
      }
      const stack = [...toHide];
      while (stack.length) {
        const id = stack.pop();
        const kids = byParent.get(id) || [];
        for (const kid of kids) {
          if (!toHide.has(kid)) {
            toHide.add(kid);
            stack.push(kid);
          }
        }
      }
      visible = visible.filter((n) => !toHide.has(n.id));
    }
  }

  const map = new Map(visible.map((n) => [n.id, { ...n, children: [] }]));
  const roots = [];
  for (const n of map.values()) {
    if (n.parent_id && map.has(n.parent_id)) map.get(n.parent_id).children.push(n);
    else roots.push(n);
  }
  for (const n of map.values()) {
    n.children.sort((a, b) => (b.priority || b.score || 0) - (a.priority || a.score || 0));
  }
  return map.get("root") || roots[0] || null;
}

function prepareDisplayNode(node, displayDepth = 0) {
  const out = { ...node, displayDepth, children: [] };
  const children = node.children || [];
  if (children.length > MAX_DIRECT_CHILDREN) {
    const groups = new Map();
    for (const child of children) {
      const key = groupKey(child);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(child);
    }
    const displayChildren = [];
    for (const [key, grouped] of groups.entries()) {
      const groupId = `cluster:${node.id}:${key}`;
      if (state.expandedGroups.has(groupId)) {
        displayChildren.push(...grouped.map((child) => prepareDisplayNode(child, displayDepth + 1)));
      } else {
        const score = Math.max(...grouped.map((child) => Number(child.score || 0)));
        displayChildren.push({
          id: groupId,
          parent_id: node.id,
          code: key.toUpperCase().slice(0, 10),
          name: `${groupLabel(key)} (${grouped.length})`,
          hypothesis: `Grupo visual com ${grouped.length} nos agrupados.`,
          status: groupStatus(grouped),
          score,
          priority: score,
          displayDepth: displayDepth + 1,
          virtual: true,
          groupKey: key,
          groupCount: grouped.length,
          children: [],
        });
      }
    }
    out.children = displayChildren.sort((a, b) => (b.priority || b.score || 0) - (a.priority || a.score || 0));
  } else {
    out.children = children.map((child) => prepareDisplayNode(child, displayDepth + 1));
  }
  return out;
}

function buildTree(nodes) {
  const root = buildRawTree(nodes);
  return root ? prepareDisplayNode(root) : null;
}

function allNodes(root) {
  const out = [];
  function walk(n) {
    out.push(n);
    n.children.forEach(walk);
  }
  if (root) walk(root);
  return out;
}

function layout(root) {
  const nodes = allNodes(root);
  const leaves = [];
  function collect(n) {
    if (!n.children.length) leaves.push(n);
    else n.children.forEach(collect);
  }
  collect(root);

  const padX = 140;
  const padY = 110;
  const maxDepth = Math.max(1, ...nodes.map((n) => n.displayDepth || 0));
  const leafGap = leaves.length > 200 ? 70 : leaves.length > 100 ? 95 : leaves.length > 50 ? 130 : 170;
  const w = Math.max(WORLD.w, padX * 2 + Math.max(1, leaves.length - 1) * leafGap);
  const yGap = maxDepth > 6 ? 150 : maxDepth > 4 ? 175 : 200;

  leaves.forEach((n, i) => {
    n.x = leaves.length === 1 ? w / 2 : padX + i * leafGap;
  });

  function setX(n) {
    n.children.forEach(setX);
    if (n.children.length) {
      n.x = (Math.min(...n.children.map((c) => c.x)) + Math.max(...n.children.map((c) => c.x))) / 2;
    }
    n.y = padY + (n.displayDepth || 0) * yGap;
  }
  setX(root);
  return nodes;
}

function boundsFor(nodes) {
  const xs = nodes.map((n) => n.x);
  const ys = nodes.map((n) => n.y);
  return {
    minX: Math.min(...xs) - 90,
    maxX: Math.max(...xs) + 90,
    minY: Math.min(...ys) - 80,
    maxY: Math.max(...ys) + 80,
  };
}

function fitToNodes(nodes) {
  if (!nodes.length) return;
  const b = boundsFor(nodes);
  const width = Math.max(1, b.maxX - b.minX);
  const height = Math.max(1, b.maxY - b.minY);
  const scale = clamp(Math.min((WORLD.w - 90) / width, (WORLD.h - 80) / height), 0.08, 1.25);
  state.view.scale = scale;
  state.view.x = (WORLD.w - width * scale) / 2 - b.minX * scale;
  state.view.y = (WORLD.h - height * scale) / 2 - b.minY * scale;
  state.view.initialized = true;
  applyView();
}

function applyView() {
  $("viewport").setAttribute(
    "transform",
    `translate(${state.view.x.toFixed(2)},${state.view.y.toFixed(2)}) scale(${state.view.scale.toFixed(4)})`
  );
  $("zoomLabel").textContent = `${Math.round(state.view.scale * 100)}%`;
}

function clearSvg() {
  $("edges").innerHTML = "";
  $("nodes").innerHTML = "";
}

function edgePath(a, b) {
  const mid = (a.y + b.y) / 2;
  return `M ${a.x} ${a.y} C ${a.x} ${mid}, ${b.x} ${mid}, ${b.x} ${b.y}`;
}

function showLabel(n, totalNodes) {
  if (n.virtual) return true;
  if (state.selected === n.id) return true;
  if ((n.displayDepth || 0) <= 1) return true;
  if (state.view.scale < 0.7) return false;
  if (totalNodes > 90) return false;
  return ["candidate", "promising", "expand", "expanded", "validated"].includes(n.status);
}

function renderTree() {
  const root = buildTree(state.data.nodes);
  clearSvg();
  if (!root) return;

  const nodes = layout(root);
  state.displayNodeMap = new Map(nodes.map((n) => [n.id, n]));

  if (!state.view.initialized) fitToNodes(nodes);
  else applyView();

  for (const n of nodes) {
    for (const c of n.children) {
      const p = document.createElementNS(SVG_NS, "path");
      const onPath = state.activePathIds.has(n.id) && state.activePathIds.has(c.id);
      p.setAttribute("class", `edge ${statusClass(c.status)} ${c.virtual ? "cluster" : ""} ${onPath ? "working" : ""}`);
      p.setAttribute("d", edgePath(n, c));
      $("edges").appendChild(p);
    }
  }

  for (const n of nodes) {
    const g = document.createElementNS(SVG_NS, "g");
    const isWorking = state.activeNodeIds.has(n.id);
    const isPruning = state.recentPrunedIds.has(n.id);
    const cs = (n.metrics && n.metrics.critic_status) || "";
    const criticClass = cs === "approved" ? "critic-approved" : cs === "rejected" ? "critic-rejected" : (n.status === "validated" ? "critic-pending" : "");
    g.setAttribute("class", `node ${statusClass(n.status)} ${n.virtual ? "cluster" : ""} ${state.selected === n.id ? "selected" : ""} ${isWorking ? "working" : ""} ${isPruning ? "pruning" : ""} ${criticClass}`);
    g.setAttribute("transform", `translate(${n.x},${n.y})`);
    g.addEventListener("click", (event) => {
      event.stopPropagation();
      state.selected = n.id;
      if (n.virtual) state.expandedGroups.add(n.id);
      renderDetails(n);
      renderTree();
    });

    const outer = document.createElementNS(SVG_NS, "circle");
    outer.setAttribute("class", "outer");
    outer.setAttribute("r", n.displayDepth === 0 ? 18 : n.virtual ? 15 : 10);

    const inner = document.createElementNS(SVG_NS, "circle");
    inner.setAttribute("class", "inner");
    inner.setAttribute("r", n.displayDepth === 0 ? 6 : n.virtual ? 6 : 3 + Math.max(0, Math.min(1, n.score || 0)) * 4);

    const code = document.createElementNS(SVG_NS, "text");
    code.textContent = state.view.scale < 0.45 && n.displayDepth > 1 ? "" : shortText(n.code || n.id, n.virtual ? 12 : 14);
    code.setAttribute("y", n.displayDepth <= 1 ? -22 : 23);

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("class", "label");
    label.textContent = showLabel(n, nodes.length) ? shortText(n.name, n.virtual ? 22 : 28) : "";
    label.setAttribute("y", n.displayDepth <= 1 ? -36 : n.virtual ? -24 : 0);

    g.appendChild(outer);
    g.appendChild(inner);
    g.appendChild(code);
    g.appendChild(label);
    $("nodes").appendChild(g);
  }
}

function renderDetails(n) {
  $("nodeName").textContent = n.name || "No";
  $("nodeHypothesis").textContent = n.hypothesis || "Sem hipotese.";
  $("nodeScore").textContent = Number(n.score || 0).toFixed(2);

  const meta = n.virtual
    ? [
        ["Tipo", "Grupo visual"],
        ["Nos internos", n.groupCount || 0],
        ["Status", n.status],
      ]
    : [
        ["Status", n.status],
        ["Timeframe", n.timeframe || "-"],
        ["Agente", n.agent_role || "-"],
        ["Prioridade", Number(n.priority || 0).toFixed(2)],
        ["Motivo poda", n.prune_reason || "-"],
        ["Resumo", n.result_summary || "-"],
      ];

  $("nodeMeta").innerHTML = meta
    .map(([k, v]) => `<div><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`)
    .join("");
  if (n.virtual) {
    $("nodeDetails").innerHTML = `<div class="detailBlock"><div class="detailTitle">Grupo visual</div><p>Abra o grupo e clique em um node real para ver testes, probes e gates.</p></div>`;
  } else {
    renderNodeDetails(n);
  }
}

function agentBubble(agent) {
  const stats = agent.stats || {};
  if (stats.bubble) return stats.bubble;
  if (agent.status === "running") return "Trabalhando.";
  if (agent.status === "queued") return "Na fila.";
  if (agent.status === "failed") return "Falha registrada.";
  return "Standby.";
}

function agentBadge(agent) {
  if (agent.timeframe) return agent.timeframe;
  return String(agent.label || agent.role || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

function renderSidebars() {
  const taskById = new Map((state.data.tasks || []).map((t) => [t.id, t]));

  // Atuando agora: lista compacta de agentes -> nó
  const activeRows = state.agentTargets;
  const activeCountEl = $("activeBarCount");
  if (activeCountEl) activeCountEl.textContent = activeRows.length + (state.prunerTargets || []).length;

  const workingHtml = activeRows.map(({ agent, task, nodeId, node }) => {
    const nodeLabel = node ? (node.name || node.code || nodeId) : (nodeId || "sem no");
    const code = node ? (node.code || "") : "";
    const meta = task ? (task.task_type || task.status || "") : "";
    const dataId = nodeId ? `data-node="${escapeHtml(nodeId)}"` : "";
    return `
      <div class="activeRow working" ${dataId}>
        <div>
          <div class="agent">${escapeHtml(agent.label || agent.role)}</div>
          <div class="node">${escapeHtml(nodeLabel)}</div>
        </div>
        <div class="meta">testando ${escapeHtml(code || meta)}</div>
        ${nodeId ? `<div class="nodeId">${escapeHtml(nodeId)}</div>` : ""}
      </div>
    `;
  }).join("");

  const pruneHtml = (state.prunerTargets || []).map(({ node, nodeId, agentRole, reason }) => {
    const nodeLabel = node ? (node.name || node.code || nodeId) : nodeId;
    const code = node ? (node.code || "") : "";
    return `
      <div class="activeRow pruning" data-node="${escapeHtml(nodeId)}">
        <div>
          <div class="agent">${escapeHtml(agentRole)}</div>
          <div class="node">${escapeHtml(nodeLabel)}</div>
        </div>
        <div class="meta">podou ${escapeHtml(code)}</div>
        <div class="nodeId">${escapeHtml(shortText(reason, 50))}</div>
      </div>
    `;
  }).join("");

  const combined = workingHtml + pruneHtml;
  $("activeAgents").innerHTML = combined.length ? combined : `<div class="empty">Nenhum agente atuando no momento.</div>`;

  for (const row of document.querySelectorAll("#activeAgents .activeRow")) {
    const id = row.dataset.node;
    if (!id) continue;
    row.addEventListener("click", () => {
      state.selected = id;
      const n = state.data.nodes.find((x) => x.id === id);
      if (n) renderDetails(n);
      renderTree();
    });
  }

  $("agents").innerHTML = state.data.agents.map(a => {
    const task = a.current_task_id ? taskById.get(a.current_task_id) : null;
    const node = task && task.node_id ? state.nodeIndex.get(task.node_id) : null;
    const target = node ? (node.name || node.code || node.id) : (task && task.node_id ? task.node_id : "");
    return `
    <div class="agentNpc ${escapeHtml(a.status)}">
      <div class="bubble">${escapeHtml(agentBubble(a))}</div>
      <div class="avatar">
        <div class="avatarHead">${escapeHtml(agentBadge(a))}</div>
        <div class="avatarBody"></div>
        <div class="avatarBase"></div>
      </div>
      <div class="agentInfo">
        <span>${escapeHtml(a.kind)}</span>
        <strong>${escapeHtml(a.label)}</strong>
        <span>${escapeHtml(a.status)}${a.current_task_id ? " / " + escapeHtml(a.current_task_id) : ""}</span>
        ${target ? `<span class="targetNode">&rarr; ${escapeHtml(shortText(target, 36))}</span>` : ""}
      </div>
    </div>
    `;
  }).join("");

  const traces = state.data.llm_traces || [];
  $("llmTraces").innerHTML = traces.length ? traces.slice(0, 12).map(t => `
    <div class="llmTrace ${escapeHtml(t.status)}">
      <div class="traceTop">
        <strong>${escapeHtml(t.agent_role)}</strong>
        <span>${escapeHtml(t.status)}</span>
      </div>
      <div class="traceMeta">
        <span>${escapeHtml(t.provider || "openai")}</span>
        <span>${escapeHtml(compactModel(t.model))}</span>
        <span>${escapeHtml((t.metadata && t.metadata.effort) || "xhigh")}</span>
      </div>
      <p>${escapeHtml(traceSummary(t))}</p>
      <small>${escapeHtml(t.action)}${t.session_id ? " / " + escapeHtml(t.session_id.slice(0, 8)) : ""}${t.cost_usd ? " / $" + Number(t.cost_usd).toFixed(4) : ""}</small>
      <details class="traceDetails">
        <summary>ver saida</summary>
        <pre>${escapeHtml(t.output_excerpt || t.error || "")}</pre>
      </details>
    </div>
  `).join("") : "<div class=\"emptyTrace\">Nenhuma chamada LLM nesta campanha.</div>";

  $("runs").innerHTML = state.data.runs.slice(0, 8).map(r => `
    <div class="run">
      <span>${escapeHtml(r.status)} / ${escapeHtml(r.completed)}/${escapeHtml(r.total)} / ${escapeHtml(r.worker_count)} workers</span>
      <strong>${escapeHtml(r.sweep_id || r.id)}</strong>
      <span>${r.throughput_per_min ? escapeHtml(r.throughput_per_min) + " probes/min" : ""}</span>
    </div>
  `).join("");

  $("log").innerHTML = state.data.events.slice(0, 60).map(e => `
    <div class="logEntry"><strong>${escapeHtml(e.type)}</strong> / ${escapeHtml(e.message)}<br><small>${escapeHtml(e.ts)}</small></div>
  `).join("");
}

function renderRankingLegacy() {
  if (!state.ranking) {
    $("rankingList").innerHTML = "<div class=\"emptyRanking\">Carregando ranking...</div>";
    return;
  }
  const summary = state.ranking.summary || {};
  const discoverySummary = state.ranking.discovery_summary || {};
  const reset = state.ranking.ranking_reset || {};
  $("rankingSummary").textContent = [
    `${state.ranking.count || 0} estrategias`,
    `${summary.deployed || 0} no bot`,
    `${summary.not_deployed || 0} aguardando deploy`,
    `${discoverySummary.recent || 0} descobertas recentes`,
    reset.active ? `${reset.hidden_count || 0} antigas ocultas` : null,
    `${summary.duplicates_hidden || 0} duplicatas ocultas`,
  ].filter(Boolean).join(" / ");
  const discoveries = state.ranking.discoveries || [];
  const strip = $("discoveryStrip");
  if (strip) {
    strip.innerHTML = `
      <div class="discoveryHead">
        <div>
          <span class="eyebrow">${reset.active ? "Novas depois do reset" : "Descobertas recentes"}</span>
          <strong>${escapeHtml(discoverySummary.recent || 0)} nodes novos no pipeline</strong>
          ${discoverySummary.new_only_since ? `<small>Desde ${escapeHtml(discoverySummary.new_only_since)}</small>` : ""}
        </div>
        <div class="discoveryStats">
          <span>${escapeHtml(discoverySummary.openai || 0)} OpenAI</span>
          <span>${escapeHtml(discoverySummary.with_backtest || 0)} com backtest</span>
          <span>${escapeHtml(discoverySummary.validated || 0)} validados</span>
          <span>${escapeHtml(discoverySummary.deployed || 0)} no bot</span>
        </div>
      </div>
      <div class="discoveryList">
        ${discoveries.length ? discoveries.slice(0, 12).map((d) => {
          const ts = parseTs(d.updated_at);
          const age = ts ? ageHuman(Date.now() - ts) : "--";
          const pnl = d.pnl_pct == null ? "-" : `${Number(d.pnl_pct || 0).toFixed(2)}%`;
          const trades = d.trade_count == null ? "-" : `${escapeHtml(d.trade_count)} trades`;
          const detail = d.hypothesis || d.summary || d.causal_mechanism || d.node_id;
          const mechanism = d.causal_mechanism || d.mutation_from_parent || "";
          return `
            <article class="discoveryCard ${escapeHtml(d.status || "")} ${d.deployed ? "deployed" : ""}" data-node="${escapeHtml(d.node_id)}">
              <div class="discoveryTop">
                <strong>${escapeHtml(d.name || d.node_id)}</strong>
                <span>${escapeHtml(discoveryStageLabel(d.stage))}</span>
              </div>
              <p>${escapeHtml(shortText(detail, 220))}</p>
              ${mechanism ? `<small>${escapeHtml(shortText(mechanism, 180))}</small>` : ""}
              <div class="discoveryMeta">
                <span>${escapeHtml(discoverySourceLabel(d))}</span>
                <span>${escapeHtml(d.asset || "-")}</span>
                <span>${escapeHtml(d.timeframe || "-")}</span>
                <span>${escapeHtml(d.engine || "-")}</span>
                <span>score ${Number(d.score || 0).toFixed(2)}</span>
                <span>pnl ${escapeHtml(pnl)}</span>
                <span>${trades}</span>
                <span>${escapeHtml(age)}</span>
              </div>
            </article>
          `;
        }).join("") : "<div class=\"emptyRanking\">Nenhuma descoberta recente nesta campanha.</div>"}
      </div>
    `;
    for (const card of document.querySelectorAll(".discoveryCard")) {
      card.addEventListener("click", () => {
        state.selected = card.dataset.node;
        const n = state.data?.nodes?.find((node) => node.id === state.selected);
        if (n) renderDetails(n);
      });
    }
  }
  const rows = state.ranking.strategies || [];
  if (!rows.length) {
    $("rankingList").innerHTML = reset.active
      ? "<div class=\"emptyRanking\">Ranking visual zerado. As proximas estrategias aprovadas vao aparecer aqui.</div>"
      : "<div class=\"emptyRanking\">Nenhuma estrategia aprovada para teste ainda.</div>";
    return;
  }
  $("rankingList").innerHTML = rows.map((r, index) => `
    <article class="rankingCard ${escapeHtml(r.readiness)} ${r.deployed ? "deployed" : "notDeployed"}" data-node="${escapeHtml(r.node_id)}">
      <div class="rankNo">#${index + 1}</div>
      <div class="rankMain">
        <div class="rankTitle">
          <strong>${escapeHtml(r.name)}</strong>
          <span>${escapeHtml(readinessLabel(r.readiness))}</span>
          ${r.deployed
            ? `<span class="deployBadge ok" title="Ja esta rodando no bot trader">✅ No bot</span>`
            : `<span class="deployBadge pending" title="${escapeHtml(r.deploy_blocker || 'Aguardando deploy')}">⏳ Fora do bot</span>`}
        </div>
        <div class="rankMeta">
          <span>${escapeHtml(r.campaign_label || r.campaign_id || "-")}</span>
          <span>${escapeHtml(bucketLabel(r.source_bucket))}</span>
          <span>${escapeHtml(r.timeframe || "-")}</span>
          ${r.period ? `<span title="periodo testado">📅 ${escapeHtml(r.period)}</span>` : ""}
          <span>score ${Number(r.score || 0).toFixed(2)}</span>
          <span>sh ${Number(r.sharpe || 0).toFixed(2)}</span>
          <span>mdd ${Number(r.mdd_pct || 0).toFixed(2)}%</span>
          <span>${escapeHtml(r.trade_count)} trades</span>
        </div>
        <p>${escapeHtml(r.summary || r.node_id)}</p>
        <small>${r.deployed
            ? "Ativa no bot trader (active_strategies.json)."
            : (r.deploy_blocker ? "Motivo fora do bot: " + escapeHtml(r.deploy_blocker) : "Sem bloqueios para paper.")
        }${(r.blockers || []).length ? " / " + escapeHtml((r.blockers || []).join(", ")) : ""}${r.duplicate_count > 1 ? " / " + escapeHtml(r.duplicate_count - 1) + " duplicatas ocultas" : ""}</small>
      </div>
      <div class="rankPnl">
        <span>${money(r.starting_capital)} virou</span>
        <strong>${money(r.ending_capital)}</strong>
        <em>${Number(r.pnl_pct || 0).toFixed(2)}%</em>
      </div>
    </article>
  `).join("");

  for (const card of document.querySelectorAll(".rankingCard")) {
    card.addEventListener("click", () => {
      state.selected = card.dataset.node;
      state.viewMode = "tree";
      syncViewMode();
      const n = state.data.nodes.find((node) => node.id === state.selected);
      if (n) renderDetails(n);
      renderTree();
    });
  }
}

function syncRankingTabs() {
  const activeTab = state.rankingTab === "discoveries" ? "discoveries" : "validated";
  for (const btn of document.querySelectorAll("[data-ranking-tab]")) {
    btn.classList.toggle("active", btn.dataset.rankingTab === activeTab);
    btn.setAttribute("aria-selected", btn.dataset.rankingTab === activeTab ? "true" : "false");
  }
  const validatedPane = $("rankingPaneValidated");
  const discoveriesPane = $("rankingPaneDiscoveries");
  if (validatedPane) validatedPane.classList.toggle("hidden", activeTab !== "validated");
  if (discoveriesPane) discoveriesPane.classList.toggle("hidden", activeTab !== "discoveries");
}

function setRankingTab(tab) {
  state.rankingTab = tab === "discoveries" ? "discoveries" : "validated";
  localStorage.setItem("aetherRankingTab", state.rankingTab);
  syncRankingTabs();
  renderRanking();
}

function renderRanking() {
  if (!state.ranking) {
    $("rankingList").innerHTML = "<div class=\"emptyRanking\">Carregando ranking novo...</div>";
    return;
  }

  const summary = state.ranking.summary || {};
  const discoverySummary = state.ranking.discovery_summary || {};
  const reset = state.ranking.ranking_reset || {};
  const resetLabel = reset.active ? formatDateTime(reset.reset_at) : "";
  const rows = (state.ranking.strategies || []).filter((row) => isAfterReset(row, reset));
  const discoveries = (state.ranking.discoveries || []).filter((row) => isAfterReset(row, reset));
  const rankedCount = rows.length;
  const manifestTotal = summary.active_manifest_total ?? summary.deployed ?? 0;
  const deployedInRanking = summary.deployed_in_ranking ?? summary.deployed ?? 0;
  const manifestOutsideRanking = summary.active_manifest_outside_ranking ?? Math.max(0, manifestTotal - deployedInRanking);
  const latestRankedAt = summary.latest_ranked_at ? formatDateTime(summary.latest_ranked_at) : "--";
  const discoveryCount = discoveries.length || discoverySummary.recent || 0;

  if ($("rankingTabValidatedCount")) $("rankingTabValidatedCount").textContent = rankedCount;
  if ($("rankingTabDiscoveriesCount")) $("rankingTabDiscoveriesCount").textContent = discoveryCount;
  syncRankingTabs();

  $("rankingSummary").textContent = state.rankingTab === "discoveries"
    ? [
        reset.active ? "Somente descobertas depois da limpeza" : "Pipeline separado do ranking",
        `${discoveryCount} descobertas recentes`,
        `${discoverySummary.with_backtest || 0} com backtest`,
        `${discoverySummary.validated || 0} validadas`,
        resetLabel && resetLabel !== "--" ? `desde ${resetLabel}` : null,
      ].filter(Boolean).join(" / ")
    : reset.active
    ? [
        "Modo novo ativo",
        `${reset.hidden_count || 0} antigas escondidas`,
        `${rankedCount} novas validadas`,
        resetLabel && resetLabel !== "--" ? `reset ${resetLabel}` : null,
      ].filter(Boolean).join(" / ")
    : [
        `${rankedCount} estrategias validadas no ranking`,
        `${manifestTotal} ativas no bot`,
        `${deployedInRanking} cruzam com ranking`,
        manifestOutsideRanking ? `${manifestOutsideRanking} ativas fora/duplicadas` : null,
        `${summary.not_deployed || 0} validadas fora do bot`,
        latestRankedAt !== "--" ? `ultima validada ${latestRankedAt}` : null,
      ].filter(Boolean).join(" / ");

  const deployBtn = $("deployRankingBtn");
  if (deployBtn) {
    deployBtn.textContent = reset.active ? "Ativar novas no bot" : "Ativar no bot";
    deployBtn.disabled = rows.length === 0 || state.rankingTab !== "validated";
  }

  const strip = $("discoveryStrip");
  if (strip) {
    const discoveryEmpty = reset.active
      ? `<div class="emptyRanking newOnlyEmpty"><strong>Aguardando descoberta nova</strong><span>Nada criado depois do reset (${escapeHtml(resetLabel)}) entrou no pipeline ainda. O proximo ciclo vai preencher esta faixa com scout, backtest e critic.</span></div>`
      : "<div class=\"emptyRanking\">Nenhuma descoberta recente nesta campanha.</div>";
    strip.innerHTML = `
      <div class="discoveryHead">
        <div>
          <span class="eyebrow">${reset.active ? "Somente novas" : "Descobertas recentes"}</span>
          <strong>${escapeHtml(discoveries.length)} descobertas no pipeline</strong>
          ${reset.active ? `<small>Antigas bloqueadas na tela desde ${escapeHtml(resetLabel)}</small>` : ""}
        </div>
        <div class="discoveryStats">
          <span>${escapeHtml(discoverySummary.openai || 0)} OpenAI</span>
          <span>${escapeHtml(discoverySummary.with_backtest || 0)} com backtest</span>
          <span>${escapeHtml(discoverySummary.validated || 0)} validadas</span>
          <span>${escapeHtml(discoverySummary.deployed || 0)} ja no bot</span>
        </div>
      </div>
      <div class="discoveryList">
        ${discoveries.length ? discoveries.slice(0, 12).map((d) => {
          const ts = parseTs(d.updated_at);
          const age = ts ? ageHuman(Date.now() - ts) : "--";
          const pnl = d.pnl_pct == null ? "-" : `${Number(d.pnl_pct || 0).toFixed(2)}%`;
          const trades = d.trade_count == null ? "-" : `${escapeHtml(d.trade_count)} trades`;
          const detail = d.hypothesis || d.summary || d.causal_mechanism || d.node_id;
          const mechanism = d.causal_mechanism || d.mutation_from_parent || "";
          return `
            <article class="discoveryCard ${escapeHtml(d.status || "")} ${d.deployed ? "deployed" : ""}" data-node="${escapeHtml(d.node_id)}">
              <div class="discoveryTop">
                <strong>${escapeHtml(d.name || d.node_id)}</strong>
                <span>${escapeHtml(discoveryStageLabel(d.stage))}</span>
              </div>
              <p>${escapeHtml(shortText(detail, 340))}</p>
              ${mechanism ? `<small>${escapeHtml(shortText(mechanism, 260))}</small>` : ""}
              <div class="discoveryMeta">
                <span>${escapeHtml(discoverySourceLabel(d))}</span>
                <span>${escapeHtml(d.asset || "-")}</span>
                <span>${escapeHtml(d.timeframe || "-")}</span>
                <span>${escapeHtml(d.engine || "-")}</span>
                <span>score ${Number(d.score || 0).toFixed(2)}</span>
                <span>pnl ${escapeHtml(pnl)}</span>
                <span>${trades}</span>
                <span>${escapeHtml(age)}</span>
              </div>
            </article>
          `;
        }).join("") : discoveryEmpty}
      </div>
    `;
    for (const card of document.querySelectorAll(".discoveryCard")) {
      card.addEventListener("click", () => {
        state.selected = card.dataset.node;
        const n = state.data?.nodes?.find((node) => node.id === state.selected);
        if (n) renderDetails(n);
      });
    }
  }

  if (!rows.length) {
    $("rankingList").innerHTML = reset.active
      ? `<div class="emptyRanking newOnlyEmpty"><strong>Ranking limpo</strong><span>As estrategias antigas foram ocultas. A partir de agora, so aparece aqui estrategia criada depois de ${escapeHtml(resetLabel)}, aprovada no backtest/critic e pronta para paper.</span></div>`
      : "<div class=\"emptyRanking\">Nenhuma estrategia aprovada para teste ainda.</div>";
    return;
  }

  $("rankingList").innerHTML = rows.map((r, index) => `
    <article class="rankingCard ${escapeHtml(r.readiness)} ${r.deployed ? "deployed" : "notDeployed"}" data-node="${escapeHtml(r.node_id)}">
      <div class="rankNo">#${index + 1}</div>
      <div class="rankMain">
        <div class="rankTitle">
          <strong>${escapeHtml(r.name)}</strong>
          <span>${escapeHtml(readinessLabel(r.readiness))}</span>
          ${r.deployed
            ? `<span class="deployBadge ok" title="Ja esta rodando no bot trader">No bot</span>`
            : `<span class="deployBadge pending" title="${escapeHtml(r.deploy_blocker || "Aguardando deploy")}">Fora do bot</span>`}
        </div>
        <div class="rankMeta">
          <span>${escapeHtml(r.campaign_label || r.campaign_id || "-")}</span>
          <span>${escapeHtml(bucketLabel(r.source_bucket))}</span>
          <span>${escapeHtml(r.timeframe || "-")}</span>
          ${r.period ? `<span title="periodo testado">${escapeHtml(r.period)}</span>` : ""}
          <span>score ${Number(r.score || 0).toFixed(2)}</span>
          <span>sh ${Number(r.sharpe || 0).toFixed(2)}</span>
          <span>mdd ${Number(r.mdd_pct || 0).toFixed(2)}%</span>
          <span>${escapeHtml(r.trade_count)} trades</span>
        </div>
        <p>${escapeHtml(r.summary || r.node_id)}</p>
        <small>${r.deployed
            ? "Ativa no bot trader (active_strategies.json)."
            : (r.deploy_blocker ? "Motivo fora do bot: " + escapeHtml(r.deploy_blocker) : "Sem bloqueios para paper.")
        }${(r.blockers || []).length ? " / " + escapeHtml((r.blockers || []).join(", ")) : ""}${r.duplicate_count > 1 ? " / " + escapeHtml(r.duplicate_count - 1) + " duplicatas ocultas" : ""}</small>
      </div>
      <div class="rankPnl">
        <span>${money(r.starting_capital)} virou</span>
        <strong>${money(r.ending_capital)}</strong>
        <em>${Number(r.pnl_pct || 0).toFixed(2)}%</em>
      </div>
    </article>
  `).join("");

  for (const card of document.querySelectorAll(".rankingCard")) {
    card.addEventListener("click", () => {
      state.selected = card.dataset.node;
      const n = state.data?.nodes?.find((node) => node.id === state.selected);
      if (n) renderDetails(n);
    });
  }
}

function renderCampaignOptions() {
  const select = $("rankingCampaign");
  if (!select || !state.campaigns) return;
  const campaigns = state.campaigns.campaigns || [];
  const activeId = state.campaigns.active_campaign_id || "aether_current";
  const options = [
    { id: "active", label: `Atual: ${campaigns.find(c => c.id === activeId)?.label || activeId}` },
    ...campaigns
      .filter(c => c.id === activeId)
      .map(c => ({ id: c.id, label: `${c.active ? "* " : ""}${c.label} (${c.rankable_count || 0})` })),
  ];
  const seen = new Set();
  select.innerHTML = options
    .filter(o => {
      if (seen.has(o.id)) return false;
      seen.add(o.id);
      return true;
    })
    .map(o => `<option value="${escapeHtml(o.id)}">${escapeHtml(o.label)}</option>`)
    .join("");
  if (
    ["all", "legacy_pre_aether"].includes(state.rankingCampaign) ||
    ![...select.options].some(o => o.value === state.rankingCampaign)
  ) {
    state.rankingCampaign = "active";
  }
  localStorage.setItem("aetherRankingCampaign", state.rankingCampaign);
  select.value = state.rankingCampaign;
}

async function refreshCampaigns() {
  state.campaigns = await api("/api/campaigns");
  renderCampaignOptions();
}

function renderStats() {
  const nodes = state.data.nodes;
  $("statNodes").textContent = nodes.length;
  $("statPruned").textContent = nodes.filter(n => ["pruned", "paused"].includes(n.status)).length;
  $("statValidated").textContent = nodes.filter(n => n.status === "validated").length;
  $("statTasks").textContent = state.data.tasks.length;
  $("workerCount").textContent = state.data.runtime.default_workers;
  $("runState").textContent = state.data.runtime.cycle_running ? state.data.runtime.cycle_mode : "standby";
  $("runState").className = state.data.runtime.cycle_running ? "running" : "";
  $("statDb").textContent = state.data.nodes_truncated
    ? `SQLite ${nodes.length}/${state.data.nodes_total || nodes.length}`
    : "SQLite";
}

async function refreshRanking() {
  $("rankingList").innerHTML = "<div class=\"emptyRanking\">Atualizando ranking novo...</div>";
  $("discoveryStrip").innerHTML = "";
  if (!state.campaigns) await refreshCampaigns();
  const capital = Number($("capitalInput")?.value || 1000);
  state.ranking = await api(`/api/ranking?capital=${encodeURIComponent(capital)}&limit=250&campaign_id=${encodeURIComponent(state.rankingCampaign)}&dedupe=true&hide_blocked=false&discoveries_limit=80`);
  state.campaigns = state.ranking.campaigns;
  renderCampaignOptions();
  renderRanking();
}

// Mapa mental: mostra somente agentes e o trabalho atual deles (vis-network).
let _agentsNetwork = null;
let _agentsNetData = null;
let _mindmapLoading = false;
let _mindmapLastFetch = 0;
const MINDMAP_MIN_INTERVAL_MS = 6000;

function _waitForVis(retries = 30) {
  return new Promise((resolve, reject) => {
    const tick = () => {
      if (typeof window.vis !== "undefined" && window.vis.Network) return resolve();
      if (--retries <= 0) return reject(new Error("vis-network nao carregou (CDN bloqueado?)"));
      setTimeout(tick, 100);
    };
    tick();
  });
}

function renderMindmapFocus(res) {
  const panel = $("agentsGraphFocus");
  if (!panel) return;
  const focus = res.focus || [];
  if (!focus.length) {
    panel.innerHTML = `
      <div class="graphFocusHead">
        <span>Trabalho agora</span>
        <strong>standby</strong>
      </div>
      <div class="graphFocusEmpty">Nenhum agente trabalhando agora.</div>
    `;
    return;
  }

  panel.innerHTML = `
    <div class="graphFocusHead">
      <span>Trabalho agora</span>
      <strong>${escapeHtml(focus.length)} ativo${focus.length === 1 ? "" : "s"}</strong>
    </div>
    ${focus.slice(0, 10).map((item) => `
      <div class="graphFocusItem ${escapeHtml(item.status || "standby")}" data-graph="${escapeHtml(item.graph_node_id || "")}" data-node="${escapeHtml(item.node_id || "")}">
        <div class="focusPulse"></div>
        <div class="focusMain">
          <span>${escapeHtml(item.agent || item.role || "Agente")}</span>
          <strong>${escapeHtml(shortText(item.node || item.action || "Trabalho", 42))}</strong>
          <small>${escapeHtml([item.timeframe, item.action, item.detail].filter(Boolean).join(" / "))}</small>
        </div>
      </div>
    `).join("")}
  `;

  for (const item of panel.querySelectorAll(".graphFocusItem")) {
    item.addEventListener("click", () => {
      const graphId = item.dataset.graph;
      const rawNodeId = item.dataset.node;
      if (_agentsNetwork && graphId) {
        _agentsNetwork.selectNodes([graphId]);
        _agentsNetwork.focus(graphId, { scale: 1.2, animation: { duration: 450, easingFunction: "easeOutCubic" } });
      }
      if (rawNodeId && state.data) {
        state.selected = rawNodeId;
        const n = state.data.nodes.find((node) => node.id === rawNodeId);
        if (n) renderDetails(n);
      }
    });
  }
}

function updateAgentsGraphData(res) {
  if (!_agentsNetwork || !_agentsNetData) return;
  const incomingNodes = res.nodes || [];
  const incomingEdges = res.edges || [];
  const nextIds = new Set(incomingNodes.map((n) => n.id));
  const currentIds = _agentsNetData.nodes.getIds();
  const stale = currentIds.filter((id) => !nextIds.has(id));
  const hasNewNodes = incomingNodes.some((n) => !currentIds.includes(n.id));

  if (stale.length) _agentsNetData.nodes.remove(stale);
  _agentsNetData.nodes.update(incomingNodes);
  _agentsNetData.edges.clear();
  _agentsNetData.edges.add(incomingEdges);

  if (stale.length || hasNewNodes) {
    _agentsNetwork.setOptions({
      physics: {
        enabled: true,
        stabilization: { enabled: true, iterations: 35, updateInterval: 20, fit: false },
      },
    });
    _agentsNetwork.stabilize(35);
    setTimeout(() => {
      if (_agentsNetwork) _agentsNetwork.setOptions({ physics: { enabled: false } });
    }, 650);
  }
}

async function refreshMindmap(force = false) {
  if (_mindmapLoading) return;
  const now = Date.now();
  if (!force && _agentsNetwork && (now - _mindmapLastFetch) < MINDMAP_MIN_INTERVAL_MS) return;
  _mindmapLoading = true;
  _mindmapLastFetch = now;
  const container = $("agentsGraphContainer");
  if (!container) { _mindmapLoading = false; return; }
  // Spinner enquanto carrega CDN/dados
  if (!_agentsNetwork) {
    container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#94a3b8;font-family:system-ui;font-size:13px;">carregando mapa de agentes...</div>`;
  }
  try {
    await _waitForVis();
    const res = await api("/api/agents_graph?limit_per_agent=4");
    const stats = $("agentsGraphStats");
    if (stats) stats.textContent = `${res.stats?.nodes || 0} nodes · ${res.stats?.edges || 0} edges`;

    const graphStats = res.stats || {};
    if (stats) {
      stats.textContent = [
        `${graphStats.agents || 0} agentes`,
        `${graphStats.running_agents || 0} rodando`,
        `${graphStats.running_tasks || 0} tarefas`,
        `${graphStats.queued_tasks || 0} na fila`,
        `${graphStats.work_items || 0} itens de trabalho`,
      ].join(" / ");
    }
    renderMindmapFocus(res);

    if (_agentsNetwork && _agentsNetData) {
      updateAgentsGraphData(res);
      _mindmapLoading = false;
      return;
      // update incremental: setData re-estabiliza, evite chamar a cada refresh
      _agentsNetData.nodes.update(res.nodes || []);
      _agentsNetData.edges.clear();
      _agentsNetData.edges.add(res.edges || []);
      _mindmapLoading = false;
      return;
    }

    container.innerHTML = "";  // limpa spinner
    _agentsNetData = {
      nodes: new vis.DataSet(res.nodes || []),
      edges: new vis.DataSet(res.edges || []),
    };
    const options = {
      autoResize: true,
      // Stabilizacao curta (60 iter) + fit auto. Depois desliga fisica pra
      // performance (nao fica recalculando a cada refresh).
      physics: {
        enabled: true,
        solver: "forceAtlas2Based",
        forceAtlas2Based: {
          gravitationalConstant: -55,
          centralGravity: 0.012,
          springLength: 130,
          springConstant: 0.05,
          damping: 0.7,
          avoidOverlap: 0.55,
        },
        stabilization: { enabled: true, iterations: 60, updateInterval: 25, fit: true },
        timestep: 0.5,
      },
      interaction: { hover: true, tooltipDelay: 150, dragNodes: true, zoomView: true, dragView: true, navigationButtons: false },
      nodes: { borderWidth: 2, shadow: { enabled: true, color: "rgba(0,0,0,0.5)", size: 8, x: 0, y: 2 } },
      edges: { smooth: { enabled: true, type: "continuous", roundness: 0.3 }, arrows: { to: false } },
      layout: { improvedLayout: true },
    };
    _agentsNetwork = new vis.Network(container, _agentsNetData, options);
    _agentsNetwork.once("stabilizationIterationsDone", () => {
      // Desliga fisica apos estabilizar (zero recompute em hover/refresh)
      _agentsNetwork.setOptions({ physics: { enabled: false } });
      _agentsNetwork.fit({ animation: { duration: 400, easingFunction: "easeOutCubic" } });
    });
    _agentsNetwork.on("click", (params) => {
      if (params.nodes && params.nodes.length) {
        const nid = params.nodes[0];
        const selected = _agentsNetData.nodes.get(nid);
        if (selected && selected.rawNodeId && state.data) {
          state.selected = selected.rawNodeId;
          const n = state.data.nodes.find((node) => node.id === selected.rawNodeId);
          if (n) renderDetails(n);
        }
        const fixed = selected?.fixed === true;
        _agentsNetData.nodes.update({ id: nid, fixed: !fixed });
      }
    });
  } catch (e) {
    console.error("Erro ao carregar mapa de agentes:", e);
    container.innerHTML = `<div style="padding:20px;color:#ef4444;font-family:system-ui;">Erro ao gerar mapa: ${escapeHtml(e.message)}</div>`;
  } finally {
    _mindmapLoading = false;
  }
}

function syncViewMode() {
  const ranking = state.viewMode === "ranking";
  const mindmap = state.viewMode === "mindmap";
  const tree = state.viewMode === "tree";
  document.body.classList.toggle("agentOnlyView", mindmap);

  $("rankingView").classList.toggle("hidden", !ranking);
  $("mindmapView").classList.toggle("hidden", !mindmap);
  $("tree").classList.toggle("hidden", !tree);
  
  document.querySelector(".legend").classList.toggle("hidden", !tree);
  document.querySelector(".navtools").classList.toggle("hidden", !tree);
  
  $("treeViewBtn").classList.toggle("active", tree);
  $("rankingViewBtn").classList.toggle("active", ranking);
  if ($("mindmapViewBtn")) $("mindmapViewBtn").classList.toggle("active", mindmap);
  
  if (ranking) refreshRanking();
  if (mindmap) refreshMindmap(true);
}

async function refresh() {
  if (state.viewMode === "ranking") {
    await refreshRanking();
    return;
  }
  state.data = await api("/api/state");
  state.campaigns = state.data.campaigns;
  const activity = computeActivity(state.data);
  state.activeNodeIds = activity.activeNodeIds;
  state.activePathIds = activity.activePathIds;
  state.agentTargets = activity.agentTargets;
  state.nodeIndex = activity.nodeIndex;
  state.prunerTargets = activity.prunerTargets;
  state.recentPrunedIds = activity.recentPrunedIds;
  renderCampaignOptions();
  renderHealth(computeHealth(state.data));
  renderTree();
  renderSidebars();
  renderStats();
  if (state.selected) {
    const n = state.displayNodeMap.get(state.selected) || state.data.nodes.find(x => x.id === state.selected);
    if (n) renderDetails(n);
  }
  if (state.viewMode === "ranking") refreshRanking();
  if (state.viewMode === "mindmap") refreshMindmap();
}

function svgPoint(event) {
  const svg = $("tree");
  const pt = svg.createSVGPoint();
  pt.x = event.clientX;
  pt.y = event.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

function zoomAt(point, factor) {
  const before = {
    x: (point.x - state.view.x) / state.view.scale,
    y: (point.y - state.view.y) / state.view.scale,
  };
  state.view.scale = clamp(state.view.scale * factor, 0.06, 3.5);
  state.view.x = point.x - before.x * state.view.scale;
  state.view.y = point.y - before.y * state.view.scale;
  applyView();
  renderTree();
}

function setupNavigation() {
  const svg = $("tree");
  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.13 : 0.88;
    zoomAt(svgPoint(event), factor);
  }, { passive: false });

  svg.addEventListener("pointerdown", (event) => {
    if (event.target.closest && event.target.closest(".node")) return;
    state.panning = true;
    state.panLast = svgPoint(event);
    svg.classList.add("panning");
    svg.setPointerCapture(event.pointerId);
  });

  svg.addEventListener("pointermove", (event) => {
    if (!state.panning) return;
    const p = svgPoint(event);
    state.view.x += p.x - state.panLast.x;
    state.view.y += p.y - state.panLast.y;
    state.panLast = p;
    applyView();
  });

  svg.addEventListener("pointerup", (event) => {
    state.panning = false;
    state.panLast = null;
    svg.classList.remove("panning");
    try { svg.releasePointerCapture(event.pointerId); } catch (_) {}
  });

  svg.addEventListener("pointerleave", () => {
    state.panning = false;
    state.panLast = null;
    svg.classList.remove("panning");
  });

  $("zoomInBtn").onclick = () => zoomAt({ x: WORLD.w / 2, y: WORLD.h / 2 }, 1.18);
  $("zoomOutBtn").onclick = () => zoomAt({ x: WORLD.w / 2, y: WORLD.h / 2 }, 0.84);
  $("fitBtn").onclick = () => fitToNodes([...state.displayNodeMap.values()]);
  $("compactBtn").onclick = () => {
    state.expandedGroups.clear();
    state.selected = null;
    renderTree();
  };

  const showPrunedToggle = $("showPrunedToggle");
  if (showPrunedToggle) {
    showPrunedToggle.checked = state.showPruned;
    showPrunedToggle.addEventListener("change", () => {
      state.showPruned = showPrunedToggle.checked;
      localStorage.setItem("aetherShowPruned", state.showPruned ? "1" : "0");
      state.view.initialized = false;
      renderTree();
    });
  }
}

$("seedBtn").onclick = async () => { await api("/api/tree/seed", { method: "POST" }); refresh(); };
$("importBtn").onclick = async () => { await api("/api/import/legacy", { method: "POST" }); refresh(); };
$("llmResearchBtn").onclick = async () => {
  const res = await api("/api/research/llm?max_new=1", { method: "POST" });
  if (res.error) alert(`Agente LLM indisponivel:\n${res.error}`);
  else if (res.skipped) alert(res.message || "Agente LLM aguardando validacao da estrategia anterior.");
  refresh();
};
$("webResearchBtn").onclick = async () => {
  const res = await api("/api/research/web?max_new=1", { method: "POST" });
  if (res && res.skipped) alert(res.message || "Pesquisa web aguardando validacao da estrategia anterior.");
  refresh();
};
$("cycleBtn").onclick = async () => {
  await api(`/api/cycle/start?workers=${state.data?.runtime?.default_workers || 12}&limit_nodes=5&probes_per_node=60&max_tasks=5&continuous=false`, { method: "POST" });
  refresh();
};
$("continuousBtn").onclick = async () => {
  const workers = Math.min(Number(state.data?.runtime?.default_workers || 12), 12);
  await api(`/api/cycle/start?workers=${workers}&limit_nodes=24&probes_per_node=60&max_tasks=24&continuous=true`, { method: "POST" });
  refresh();
};
$("stopBtn").onclick = async () => {
  await api("/api/cycle/stop", { method: "POST" });
  refresh();
};
$("manifestBtn").onclick = async () => {
  if (!state.selected || state.selected.startsWith("cluster:")) return alert("Selecione um no real primeiro.");
  const res = await api(`/api/manifest/${encodeURIComponent(state.selected)}`, { method: "POST" });
  alert(`Manifest gerado:\n${res.path}`);
  refresh();
};
$("treeViewBtn").onclick = () => { state.viewMode = "tree"; window.location.hash = ""; syncViewMode(); };
$("mindmapViewBtn").onclick = () => { state.viewMode = "mindmap"; window.location.hash = "mindmap"; syncViewMode(); };
$("rankingViewBtn").onclick = () => { state.viewMode = "ranking"; window.location.hash = "ranking"; syncViewMode(); };
for (const btn of document.querySelectorAll("[data-ranking-tab]")) {
  btn.addEventListener("click", () => setRankingTab(btn.dataset.rankingTab));
}
$("refreshRankingBtn").onclick = () => refreshRanking();
$("resetRankingBtn").onclick = async () => {
  const btn = $("resetRankingBtn");
  const status = $("deployRankingStatus");
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Zerando...";
  status.textContent = "";
  try {
    const capital = Number($("capitalInput")?.value || 1000);
    const res = await api(
      `/api/ranking/reset?capital=${encodeURIComponent(capital)}&campaign_id=${encodeURIComponent(state.rankingCampaign)}&dedupe=true`,
      { method: "POST" }
    );
    status.textContent = `${res.hidden_count || 0} antigas ocultas`;
    await refreshRanking();
  } catch (e) {
    status.textContent = "Falha no reset";
    alert(`Falha ao zerar ranking:\n${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
};
$("deployRankingBtn").onclick = async () => {
  const btn = $("deployRankingBtn");
  const status = $("deployRankingStatus");
  const ready = (state.ranking?.summary?.paper_ready || 0);
  if (!ready) {
    status.textContent = "nenhuma estrategia pronta";
    return;
  }
  if (!confirm(`Ativar ate ${Math.min(ready, 50)} estrategias prontas no bot paper?`)) return;
  btn.disabled = true;
  status.textContent = "ativando...";
  try {
    const capital = Number($("capitalInput")?.value || 1000);
    const res = await api(
      `/api/ranking/deploy?capital=${encodeURIComponent(capital)}&limit=250&campaign_id=${encodeURIComponent(state.rankingCampaign)}&max_deploy=50`,
      { method: "POST" }
    );
    status.textContent = `${res.deployed_count || 0} ativas no bot`;
    await refresh();
    await refreshRanking();
  } catch (e) {
    status.textContent = "falha ao ativar";
    alert(`Falha ao ativar ranking:\n${e.message}`);
  } finally {
    btn.disabled = false;
  }
};
$("capitalInput").onchange = () => refreshRanking();
$("rankingCampaign").onchange = () => {
  state.rankingCampaign = $("rankingCampaign").value;
  localStorage.setItem("aetherRankingCampaign", state.rankingCampaign);
  refreshRanking();
};
$("newCampaignBtn").onclick = async () => {
  const label = prompt("Nome da nova campanha limpa:", "Aether campanha limpa");
  if (label === null) return;
  await api(`/api/campaigns/new?label=${encodeURIComponent(label || "Aether campanha limpa")}`, { method: "POST" });
  state.rankingCampaign = "active";
  localStorage.setItem("aetherRankingCampaign", state.rankingCampaign);
  await refreshCampaigns();
  await refreshRanking();
  refresh();
};

setupNavigation();
syncViewMode();
refresh();
setInterval(refresh, 6000);
