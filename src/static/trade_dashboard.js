const $ = (id) => document.getElementById(id);
const fmt = (v,d=2) => Number(v || 0).toLocaleString("pt-BR",{minimumFractionDigits:d,maximumFractionDigits:d});
const pct = (v) => `${Number(v || 0) >= 0 ? "+" : ""}${fmt(v,2)}%`;
const cls = (v) => Number(v || 0) >= 0 ? "pos" : "neg";
const sign = (v) => (Number(v || 0) >= 0 ? "+" : "");
function esc(v){return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;")}
function pill(text, type=""){return `<span class="pill ${type}">${esc(text)}</span>`}
function tsLabel(s){if(!s)return "—";const v=String(s);return v.length>=19?v.slice(0,19).replace("T"," "):v}
function durLabel(s){if(s==null)return "—";if(s<60)return s+"s";const m=Math.floor(s/60);if(m<60)return m+"m";const h=Math.floor(m/60);return h+"h "+(m%60)+"m"}
function sparkline(values, w=80, h=22){
  if (!values || values.length < 2) return `<svg width="${w}" height="${h}" aria-hidden="true"></svg>`;
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const span = (max - min) || 1;
  const sx = i => (i / (values.length - 1)) * (w - 2) + 1;
  const sy = v => h - 1 - ((v - min) / span) * (h - 2);
  let d = "";
  values.forEach((v, i) => { d += (i ? " L" : "M") + sx(i).toFixed(1) + " " + sy(v).toFixed(1); });
  const last = values[values.length - 1];
  const color = last >= 0 ? "var(--green)" : "var(--red)";
  const baseY = sy(0).toFixed(1);
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <line x1="0" y1="${baseY}" x2="${w}" y2="${baseY}" stroke="var(--line)" stroke-dasharray="2 3"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`;
}

let LATEST = null;
const state = { tab: "overview", filter: "all", search: "", sort: { col: "opened_at", dir: "desc" } };

async function load(){
  try {
    const res = await fetch("/api/monitor?trade_limit=200",{cache:"no-store"});
    const data = await res.json();
    LATEST = data;
    render(data);
  } catch (e) {
    console.error("monitor load failed", e);
  }
}

function render(data){
  const o = data.overview || {};
  const bs = data.bot_status || {};
  const today = data.today || {};
  const unified = data.unified_trades || [];

  // Status pill (running / parado / quieto) - formato "rodando - 08/05/2026, 09:40:05"
  const sp = $("statusPill");
  const stxt = $("statusText");
  sp.className = "status-pill " + (bs.color === "ok" ? "running" : bs.color === "wait" ? "wait" : bs.color === "no" ? "no" : "");
  const updated = o.updated_at || "";  // formato "YYYY-MM-DD HH:MM:SS"
  let stampStr = "";
  if (updated && updated.length >= 19) {
    const [d, h] = updated.split(" ");
    if (d && h) {
      const [y, mo, da] = d.split("-");
      stampStr = `${da}/${mo}/${y}, ${h}`;
    }
  }
  const ageStr = bs.log_age_seconds !== null && bs.log_age_seconds !== undefined
    ? ` (${bs.log_age_seconds < 60 ? bs.log_age_seconds + "s" : Math.round(bs.log_age_seconds/60) + "m"})`
    : "";
  stxt.textContent = `${(bs.label || "?").toLowerCase()}${stampStr ? " - " + stampStr : ""}${ageStr}`;

  const badge = $("botBadge");
  badge.className = "bot-badge " + (bs.color || "");
  badge.textContent = `${bs.label || "?"}`;
  badge.title = `BOT alive=${bs.alive} pid=${bs.pid || "-"} log_age=${bs.log_age_seconds || "?"}s`;
  $("updated").textContent = `Atualizado ${o.updated_at || "-"}`;

  // Alertas e kills
  renderAlerts(data);
  renderKills(data);

  // ===== Visao geral =====
  $("hero").innerHTML = [
    {lbl:"Patrimonio",   big:`$${fmt(o.balance)}`, sub:`Inicial $${fmt(o.initial)}`, c:cls(o.pnl_usd)},
    {lbl:"PnL total",    big:`${sign(o.pnl_usd)}$${fmt(o.pnl_usd)}`, sub:pct(o.pnl_pct), c:cls(o.pnl_usd)},
    {lbl:"Em posicao",   big:`$${fmt(o.allocated_usd)}`, sub:`Livre $${fmt(o.cash_balance)}`, c:"blue"},
    {lbl:"Posicoes",     big:`${today.open_positions||0}`, sub:`Fechados ${today.closed_today||0} · WR ${fmt(today.winrate_today||0,1)}%`, c:(today.open_positions||0) ? "blue" : ""}
  ].map(x=>`<div class="cell"><div class="lbl">${x.lbl}</div><div class="big ${x.c}">${x.big}</div><div class="sub">${x.sub}</div></div>`).join("");

  $("todayKpis").innerHTML = [
    {l:"Resultado hoje", v:`${sign(today.result_today_usd)}$${fmt(today.result_today_usd)}`, c:cls(today.result_today_usd)},
    {l:"Trades hoje",    v:`${today.closed_today||0}`, c:""},
    {l:"Win rate hoje",  v:`${fmt(today.winrate_today||0,1)}%`, c:""},
    {l:"Exposicao aberta", v:`$${fmt(today.exposure_open_usd)}`, c:"blue"}
  ].map(x=>`<div class="kpi"><span>${x.l}</span><strong class="${x.c}">${x.v}</strong></div>`).join("");

  renderPositionCards(data);

  // Stats agregados (compartilhado entre Visao geral e Estrategias)
  const stratActive = (data.active_strategies || []).length;
  const ranking = data.tier_ranking || {};
  const tCounts = Object.values(ranking);
  const t1 = tCounts.filter(t=>t===1).length;
  const t2 = tCounts.filter(t=>t===2).length;
  const t3 = stratActive - t1 - t2;
  const stats = data.strategy_stats || {};
  const stratWithTrades = Object.values(stats).filter(s => Number(s.trades||0) > 0).length;
  const stratSummaryHtml = `
    <h2>Estrategias rodando <span class="meta">${stratWithTrades} com historico</span></h2>
    <div class="strat-summary">
      <div class="total"><div class="n">${stratActive}</div><div class="lbl">total ativas no loop</div></div>
      <div class="breakdown">
        <div class="tcell t1"><span class="label">Tier 1</span><span class="value">${t1}</span></div>
        <div class="tcell t2"><span class="label">Tier 2</span><span class="value">${t2}</span></div>
        <div class="tcell t3"><span class="label">Tier 3</span><span class="value">${t3}</span></div>
      </div>
    </div>`;
  $("stratSummary").innerHTML = stratSummaryHtml;
  if ($("stratSummary2")) $("stratSummary2").innerHTML = stratSummaryHtml;

  // ===== Trades (rich table) =====
  const counts = { all: unified.length, open: 0, closed: 0, pending: 0 };
  for (const t of unified) {
    if (t.status === "open") counts.open++;
    else if (t.status === "closed") counts.closed++;
    else counts.pending++;
  }
  $("cAll").textContent = counts.all;
  $("cOpen").textContent = counts.open;
  $("cClosed").textContent = counts.closed;
  $("cPending").textContent = counts.pending;
  $("tabTradesBadge").textContent = counts.all;
  renderRichTable();

  // ===== Estrategias =====
  const rankRows = Object.entries(stats)
    .filter(([_,s]) => Number(s.trades||0) > 0)
    .map(([name,s]) => {
      const tradeCount = Number(s.trades || 0);
      const wins = Number(s.wins || 0);
      const wr = tradeCount ? wins / tradeCount * 100 : 0;
      const tier = ranking[name] || 3;
      const shortName = name.split("#").pop() || name;
      return {name, shortName, trades: tradeCount, wr, pnl: Number(s.pnl_usd||0), tier};
    })
    .sort((a,b) => b.pnl - a.pnl);
  $("rankMeta").textContent = `${rankRows.length} ${rankRows.length===1?"estrategia operada":"estrategias operadas"}`;
  const equityMap = data.equity_by_strategy || {};
  $("ranking").innerHTML = rankRows.length ? simpleTable(["#","Tier","Estrategia","Trades","WR","PnL","Curva"], rankRows.map((r,i) => [
    `<strong style="color:var(--muted);font-family:ui-monospace,monospace">${String(i+1).padStart(2,"0")}</strong>`,
    `<span class="tier t${r.tier}">T${r.tier}</span>`,
    `<span class="small" title="${esc(r.name)}">#${esc(r.shortName)}</span>`,
    r.trades,
    `${fmt(r.wr,0)}%`,
    `<span class="${cls(r.pnl)}">${sign(r.pnl)}$${fmt(r.pnl)}</span>`,
    sparkline(equityMap[r.name] || [])
  ])) : `<div class="empty">Nenhuma estrategia entrou em trade ainda.</div>`;
  $("tabStratsBadge").textContent = stratActive;

  const actives = data.active_strategies || [];
  $("activeMeta").textContent = `${actives.length} ${actives.length===1?"estrategia":"estrategias"}`;
  $("activeList").innerHTML = actives.length ? simpleTable(
    ["Estrategia","Engine","TF","Score","Asset","Loop"],
    actives.slice(0,40).map(a => [
      `<span class="small" title="${esc(a.key)}">#${esc((a.key||"").split("#").pop()||"-")}</span>`,
      esc(a.engine || "-"),
      `<span class="tf">${esc(a.timeframe || "-")}</span>`,
      a.score ? fmt(a.score, 2) : "—",
      esc(a.asset || "-"),
      a.in_bot_loop ? pill("ATIVA","ok") : pill("STAND-BY","")
    ])
  ) : `<div class="empty">Nenhuma estrategia ativa carregada.</div>`;

  // ===== Risco =====
  renderRisk(data);

  // ===== Logs =====
  const events = data.events || [];
  const evHtml = events.slice(0,80).map(e=>`<div class="event">${esc(e)}</div>`).join("") || `<div class="empty">Nenhum evento recente.</div>`;
  $("eventsOverview").innerHTML = events.slice(0,20).map(e=>`<div class="event">${esc(e)}</div>`).join("") || `<div class="empty">Nenhum evento recente.</div>`;
  $("eventsLogs").innerHTML = evHtml;
  $("eventsMeta").textContent = `${events.length} ${events.length===1?"linha":"linhas"}`;
  $("tabLogsBadge").textContent = events.length;
}

function renderAlerts(data){
  const alerts = data.json_alerts || [];
  $("alerts").innerHTML = alerts.length === 0 ? "" :
    `<div class="panel" style="border:1px solid #b94646;background:#3a1818;color:#f6cccc;padding:12px;margin-bottom:12px;">
      <strong>JSON corrompido detectado</strong>
      <ul style="margin:6px 0 0 18px;padding:0;">
        ${alerts.map(a => `<li>${esc(a.path)}: ${esc(a.error)} <span class="small">(${esc(a.at)})</span></li>`).join("")}
      </ul>
    </div>`;
}

function renderKills(data){
  const kills = data.kills || {global: null, assets: []};
  const killsBox = $("killsBox");
  const blocks = [];
  if (kills.global) {
    blocks.push(`<div class="panel" style="border:2px solid #ef4444;background:#3a1010;color:#ffd9d9;padding:14px;margin-bottom:8px;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
        <strong style="font-size:15px;">KILL SWITCH GLOBAL</strong>
        <button onclick="clearKill('global')" style="background:#ef4444;color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-weight:600;">Resume Global</button>
      </div>
      <div style="margin-top:8px;font-size:13px;line-height:1.4;">${esc(kills.global.reason || "Sem motivo registrado")}</div>
    </div>`);
  }
  if ((kills.assets || []).length > 0) {
    const items = kills.assets.map(k => `<div style="border:1px solid #b58a2c;background:#2c2310;padding:10px 12px;border-radius:6px;margin-bottom:6px;color:#ffe7a3;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
        <strong>${esc(k.asset)} <span class="small" style="opacity:.7">(mode=${esc(k.mode || "graceful")})</span></strong>
        <span class="small" style="opacity:.6;">expira ${esc(k.expires_at || "-")}</span>
        <button onclick="clearKill('asset','${esc(k.asset)}')" style="background:#b58a2c;color:#000;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-weight:600;">Resume ${esc(k.asset)}</button>
      </div>
      <div style="margin-top:6px;font-size:12px;line-height:1.4;">${esc(k.reason || "")}</div>
    </div>`).join("");
    blocks.push(`<div class="panel" style="margin-bottom:8px;"><h2 style="margin-top:0">Kills por asset</h2>${items}</div>`);
  }
  killsBox.classList.toggle("hidden", blocks.length === 0);
  killsBox.innerHTML = blocks.join("");
  const totalKills = (kills.global ? 1 : 0) + (kills.assets || []).length;
  const paused = data.paused_strategies || {};
  const pausedCount = Object.keys(paused).length;
  $("tabRiskBadge").textContent = totalKills + pausedCount;
}

function renderPositionCards(data){
  const positions = (data.paper || []).filter(p=>p.position);
  const open = positions.length;
  $("posCount").textContent = `${open} ${open===1?"aberta":"abertas"}`;
  $("assets").innerHTML = positions.length ? positions.map(a => {
    const dataAttrs = a.position
      ? `data-asset="${esc(a.asset)}" data-qty="${a.qty}" data-entry="${a.entry_price}" data-allocated="${a.allocated_usd}"`
      : '';
    return `
    <article class="asset" ${dataAttrs}>
      <div class="assetTop">
        <h3>${esc(a.asset)} ${a.position ? '<span class="livedot" title="atualizando preco a cada 2s"></span>' : ''}</h3>
        ${pill(a.position ? "LONG" : "SEM POS", a.position ? "ok" : "")}
      </div>
      <div class="assetGrid">
        <div class="metric"><span>Posicao</span><strong class="blue">$${fmt(a.allocated_usd)}</strong></div>
        <div class="metric"><span>PnL aberto</span><strong data-live="pnl" class="${cls(a.unrealized_pnl_usd)}">${sign(a.unrealized_pnl_usd)}$${fmt(a.unrealized_pnl_usd)}</strong></div>
        <div class="metric"><span>PnL total</span><strong class="${cls(a.pnl_usd)}">${sign(a.pnl_usd)}$${fmt(a.pnl_usd)}</strong></div>
      </div>
      ${a.position ? `<div class="small" style="margin-top:10px">Entrada $${fmt(a.entry_price,4)} · Atual <span data-live="price">$${fmt(a.live_price||a.entry_price,4)}</span> · Qty ${fmt(a.qty,6)}</div>` : `<div class="small" style="margin-top:10px">Aguardando sinal · ${a.trades} trades · WR ${fmt(a.winrate,1)}%</div>`}
    </article>`;
  }).join("") : `<div class="empty">Nenhuma posicao aberta.</div>`;
}

function renderRichTable(){
  if (!LATEST) return;
  const items = LATEST.unified_trades || [];
  const filter = state.filter;
  const q = (state.search || "").trim().toLowerCase();
  const filtered = items.filter(t => {
    if (filter !== "all" && t.status !== filter) return false;
    if (!q) return true;
    return [t.asset, t.strategy_full, t.strategy_key, t.timeframe, t.strategy_engine]
      .filter(Boolean)
      .some(v => String(v).toLowerCase().includes(q));
  });
  // Sort
  const col = state.sort.col;
  const dir = state.sort.dir === "asc" ? 1 : -1;
  filtered.sort((a, b) => {
    const va = a[col], vb = b[col];
    // null/undef sempre por ultimo
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });
  // marca header ativo
  document.querySelectorAll("#richTradeTable th[data-sort]").forEach(th => {
    th.setAttribute("aria-sort", th.dataset.sort === col ? state.sort.dir : "none");
  });
  const body = $("richTradeBody");
  if (!filtered.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="16">Nenhum registro corresponde aos filtros.</td></tr>`;
    return;
  }
  body.innerHTML = filtered.map(t => {
    const sentido = t.side === "SHORT" ? "venda" : "compra";
    const sentidoLabel = sentido === "venda" ? "Venda" : "Compra";
    const pnlCls = cls(t.pnl_usd);
    const maxPos = (t.max_pos_usd != null) ? `<span class="${cls(t.max_pos_usd)}">${sign(t.max_pos_usd)}$${fmt(t.max_pos_usd)}</span>` : `<span class="dim">—</span>`;
    const maxNeg = (t.max_neg_usd != null) ? `<span class="${cls(t.max_neg_usd)}">${sign(t.max_neg_usd)}$${fmt(t.max_neg_usd)}</span>` : `<span class="dim">—</span>`;
    const compra = t.entry_price != null ? fmt(t.entry_price, 4) : '<span class="dim">—</span>';
    const venda = t.exit_price != null ? fmt(t.exit_price, 4) : '<span class="dim">—</span>';
    const atual = t.current_price != null ? fmt(t.current_price, 4) : '<span class="dim">—</span>';
    // Distancia ate take/stop em % (so faz sentido em posicao aberta com preco vivo)
    const cur = t.current_price;
    const distTake = (t.take_price != null && cur != null && cur > 0)
      ? ((t.take_price - cur) / cur * 100) : null;
    const distStop = (t.stop_price != null && cur != null && cur > 0)
      ? ((cur - t.stop_price) / cur * 100) : null;
    const take = t.take_price != null
      ? `${fmt(t.take_price, 4)} <span class="dim" style="font-size:10px">${distTake!=null?(distTake>=0?"+":"")+fmt(distTake,2)+"%":""}</span>`
      : '<span class="dim">—</span>';
    const stop = t.stop_price != null
      ? `${fmt(t.stop_price, 4)} <span class="dim" style="font-size:10px">${distStop!=null?"-"+fmt(Math.abs(distStop),2)+"%":""}</span>`
      : '<span class="dim">—</span>';
    const tfBadge = t.timeframe ? `<span class="tf">${esc(t.timeframe)}</span>` : '<span class="dim">—</span>';
    const stratHash = (t.strategy_key && t.strategy_key.split("#")[1]) ? "#" + t.strategy_key.split("#")[1] : "";
    const stratEngine = t.strategy_engine || (t.strategy_key && t.strategy_key.split(":")[1] ? t.strategy_key.split(":")[1].split("#")[0] : "");
    // Linhas abertas recebem data-* attrs pra polling Binance atualizar Atual/PnL/Max ao vivo
    const isOpen = t.status === "open";
    const liveAttrs = isOpen
      ? `data-asset="${esc(t.asset)}" data-qty="${t.qty}" data-entry="${t.entry_price}" data-size="${t.size_usd}"`
      : "";
    return `<tr data-trade-id="${esc(t.id)}" ${liveAttrs}>
      <td class="lft"><span class="ativo">${esc(t.asset || "—")}</span></td>
      <td>$${fmt(t.size_usd)}</td>
      <td><span class="sentido ${sentido}">${sentidoLabel}</span></td>
      <td>${compra}</td>
      <td>${venda}</td>
      <td ${isOpen ? 'data-live="atual"' : ""}>${atual}</td>
      <td>${take}</td>
      <td>${stop}</td>
      <td ${isOpen ? 'data-live="maxneg"' : ""}>${maxNeg}</td>
      <td ${isOpen ? 'data-live="pnl"' : ""}><span class="${pnlCls}">${sign(t.pnl_usd)}$${fmt(t.pnl_usd)}</span></td>
      <td ${isOpen ? 'data-live="maxpos"' : ""}>${maxPos}</td>
      <td>${tsLabel(t.opened_at)}</td>
      <td>${t.closed_at ? tsLabel(t.closed_at) : '<span class="dim">—</span>'}</td>
      <td ${isOpen ? 'data-live="dur" data-opened="'+esc(t.opened_at||'')+'"' : ""}>${durLabel(t.duration_s)}</td>
      <td class="lft estrat">${esc(stratEngine || "-")} <span class="hash" title="${esc(t.strategy_key||'')}">${esc(stratHash)}</span></td>
      <td>${tfBadge}</td>
    </tr>`;
  }).join("");
}

function renderRisk(data){
  const risk = [];
  // exposicao + caixa livre
  const o = data.overview || {};
  risk.push({title:"Exposicao", rows:[
    ["Em posicao", `$${fmt(o.allocated_usd)}`],
    ["Caixa livre", `$${fmt(o.cash_balance)}`],
    ["Posicoes abertas", String((data.today||{}).open_positions || 0)],
  ]});
  // limites por ativo (loss streaks etc)
  const positions = (data.paper || []);
  const lossStreaks = positions
    .map(p => ({asset: p.asset, ls: Number((p.risk||{}).consecutive_losses||0), wr: p.winrate, trades: p.trades}))
    .filter(x => x.ls > 0 || x.trades > 0);
  if (lossStreaks.length) {
    risk.push({title:"Por ativo", rows: lossStreaks.map(x => [
      x.asset,
      `WR ${fmt(x.wr,1)}% · ${x.trades}t · streak ${x.ls}`
    ])});
  }
  // dia
  const t = data.today || {};
  risk.push({title:"Hoje", rows:[
    ["Resultado realizado", `<span class="${cls(t.realized_today_usd)}">${sign(t.realized_today_usd)}$${fmt(t.realized_today_usd)}</span>`],
    ["Resultado aberto", `<span class="${cls(t.unrealized_open_usd)}">${sign(t.unrealized_open_usd)}$${fmt(t.unrealized_open_usd)}</span>`],
    ["Trades fechados", String(t.closed_today||0)],
    ["WR hoje", `${fmt(t.winrate_today,1)}%`],
  ]});
  $("riskGrid").innerHTML = risk.map(card => `
    <div class="risk-card">
      <h3>${esc(card.title)}</h3>
      ${card.rows.map(([k,v]) => `<div class="row"><span class="muted">${esc(k)}</span><strong>${v}</strong></div>`).join("")}
    </div>`).join("");
  // pausadas
  const paused = data.paused_strategies || {};
  const pkeys = Object.keys(paused);
  $("pausedMeta").textContent = `${pkeys.length} ${pkeys.length===1?"pausada":"pausadas"}`;
  $("pausedList").innerHTML = pkeys.length ? simpleTable(["Estrategia","Motivo"], pkeys.map(k => [
    `<span class="small" title="${esc(k)}">#${esc(k.split("#").pop()||k)}</span>`,
    esc(paused[k] || "-")
  ])) : `<div class="empty">Nenhuma estrategia pausada.</div>`;
}

function simpleTable(headers, rows){
  if (!rows.length) return `<div class="empty">Sem dados.</div>`;
  return `<table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map((c,i)=>`<td class="${i>2?'num':''}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

// Tab switching (persiste em localStorage)
function setTab(tab){
  state.tab = tab;
  document.querySelectorAll(".tabnav button").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false"));
  document.querySelectorAll(".tabpanel").forEach(p => p.classList.toggle("active", p.dataset.tab === tab));
  persistState();
}
document.querySelectorAll(".tabnav button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));

// Trade sub-filters
document.querySelectorAll("#tradeFilter button").forEach(b => b.addEventListener("click", () => {
  state.filter = b.dataset.filter;
  document.querySelectorAll("#tradeFilter button").forEach(x => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
  persistState();
  renderRichTable();
}));
$("tradeSearch").addEventListener("input", (e) => { state.search = e.target.value; persistState(); renderRichTable(); });
$("tradeReset").addEventListener("click", () => {
  state.filter = "all";
  state.search = "";
  state.sort = { col: "opened_at", dir: "desc" };
  $("tradeSearch").value = "";
  document.querySelectorAll("#tradeFilter button").forEach(x => x.setAttribute("aria-pressed", x.dataset.filter === "all" ? "true" : "false"));
  persistState();
  renderRichTable();
});

// Header clicks ordenam: 1o clique = desc, 2o = asc, 3o = limpa pra default
document.querySelectorAll("#richTradeTable th[data-sort]").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (state.sort.col !== col) {
      state.sort = { col, dir: "desc" };
    } else if (state.sort.dir === "desc") {
      state.sort.dir = "asc";
    } else {
      state.sort = { col: "opened_at", dir: "desc" };
    }
    persistState();
    renderRichTable();
  });
});

// Persistir aba/filtro/busca/sort em localStorage
function persistState(){
  try {
    localStorage.setItem("hnb_dash_ui", JSON.stringify({
      tab: state.tab, filter: state.filter, search: state.search, sort: state.sort
    }));
  } catch(_) {}
}
function restoreState(){
  try {
    const raw = localStorage.getItem("hnb_dash_ui");
    if (!raw) return;
    const s = JSON.parse(raw);
    if (s.tab) state.tab = s.tab;
    if (s.filter) state.filter = s.filter;
    if (typeof s.search === "string") state.search = s.search;
    if (s.sort && s.sort.col) state.sort = s.sort;
  } catch(_) {}
}
// ===== Drawer de detalhes =====
function openDrawer(tradeId){
  if (!LATEST) return;
  const t = (LATEST.unified_trades || []).find(x => x.id === tradeId);
  if (!t) return;
  document.querySelectorAll("#richTradeBody tr").forEach(r => r.classList.toggle("selected", r.dataset.tradeId === tradeId));
  $("drawerTitle").textContent = `${t.asset || "—"} · ${t.side || "LONG"}`;
  const stratEngine = t.strategy_engine || (t.strategy_key && t.strategy_key.split(":")[1] ? t.strategy_key.split(":")[1].split("#")[0] : "—");
  $("drawerSub").textContent = `${stratEngine}${t.strategy_key ? " · " + t.strategy_key : ""}${t.timeframe ? " · " + t.timeframe : ""}`;
  // Distancias %
  const cur = t.current_price;
  const distTake = (t.take_price != null && cur && cur > 0) ? ((t.take_price - cur) / cur * 100) : null;
  const distStop = (t.stop_price != null && cur && cur > 0) ? ((cur - t.stop_price) / cur * 100) : null;
  const cells = [
    ["Status",      t.status === "open" ? `<span class="pill ok">ABERTO</span>` : `<span class="pill">FECHADO</span>`],
    ["Tamanho",     `$${fmt(t.size_usd)}`],
    ["Qty",         fmt(t.qty, 6)],
    ["Compra",      t.entry_price != null ? `$${fmt(t.entry_price, 4)}` : "—"],
    ["Venda",       t.exit_price != null ? `$${fmt(t.exit_price, 4)}` : "—"],
    ["Atual",       cur != null ? `$${fmt(cur, 4)}` : "—"],
    ["Take",        t.take_price != null ? `$${fmt(t.take_price, 4)}` + (distTake != null ? ` <span class="dim">(${distTake>=0?"+":""}${fmt(distTake,2)}%)</span>` : "") : "—"],
    ["Stop / BE",   t.stop_price != null ? `$${fmt(t.stop_price, 4)}` + (distStop != null ? ` <span class="dim">(-${fmt(Math.abs(distStop),2)}%)</span>` : "") : "—"],
    ["PnL atual",   `<span class="${cls(t.pnl_usd)}">${sign(t.pnl_usd)}$${fmt(t.pnl_usd)} (${sign(t.pnl_pct)}${fmt(t.pnl_pct,2)}%)</span>`],
    ["Max.Pos",     t.max_pos_usd != null ? `<span class="${cls(t.max_pos_usd)}">${sign(t.max_pos_usd)}$${fmt(t.max_pos_usd)}</span>` : "—"],
    ["Max.Neg",     t.max_neg_usd != null ? `<span class="${cls(t.max_neg_usd)}">${sign(t.max_neg_usd)}$${fmt(t.max_neg_usd)}</span>` : "—"],
    ["Tempo",       durLabel(t.duration_s)],
    ["Abertura",    tsLabel(t.opened_at)],
    ["Fechamento",  t.closed_at ? tsLabel(t.closed_at) : "—"],
  ];
  let html = `<div class="grid2">${cells.map(([k,v]) => `<div class="cell"><span class="lbl">${esc(k)}</span><span class="v">${v}</span></div>`).join("")}</div>`;

  // Votos da entrada (se posicao aberta)
  const vs = t.vote_snapshot;
  if (vs && typeof vs === "object") {
    const votes = [];
    (vs.buy_votes || []).forEach(v => votes.push({k:v, tag:"buy"}));
    (vs.sell_votes || []).forEach(v => votes.push({k:v, tag:"sell"}));
    (vs.neutral_votes || []).forEach(v => votes.push({k:v, tag:"neutral"}));
    if (votes.length) {
      html += `<h4>Votos na entrada</h4><ul class="vote-list">` + votes.map(v =>
        `<li><span title="${esc(v.k)}">${esc((v.k||"").split("#").pop()||v.k)}</span><span class="tag ${v.tag}">${v.tag}</span></li>`
      ).join("") + `</ul>`;
    }
    // Tier config
    if (t.tier_config) {
      const tc = t.tier_config;
      html += `<h4>Tier config</h4><div class="grid2">` + [
        ["Tier", tc.tier ?? "—"],
        ["Position $", tc.position_usd != null ? `$${fmt(tc.position_usd)}` : "—"],
        ["SL $", tc.sl_usd != null ? `$${fmt(tc.sl_usd)}` : "—"],
        ["Trail activate $", tc.trail_activation_usd != null ? `$${fmt(tc.trail_activation_usd)}` : "—"],
        ["Trail floor $", tc.trail_floor_usd != null ? `$${fmt(tc.trail_floor_usd)}` : "—"],
        ["Trail giveback %", tc.trail_giveback_pct != null ? `${fmt(tc.trail_giveback_pct,1)}%` : "—"],
      ].map(([k,v]) => `<div class="cell"><span class="lbl">${esc(k)}</span><span class="v">${v}</span></div>`).join("") + `</div>`;
    }
    // Snapshot bruto colapsado
    html += `<h4>Snapshot completo</h4><pre class="json">${esc(JSON.stringify(vs, null, 2))}</pre>`;
  } else if (t.entry_votes && t.entry_votes.length) {
    html += `<h4>Estrategias na entrada</h4><ul class="vote-list">` +
      t.entry_votes.map(v => `<li><span title="${esc(v)}">${esc((v||"").split("#").pop()||v)}</span><span class="tag buy">buy</span></li>`).join("") +
      `</ul>`;
  }

  $("drawerBody").innerHTML = html;
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  $("drawerBackdrop").classList.add("open");
}
function closeDrawer(){
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  $("drawerBackdrop").classList.remove("open");
  document.querySelectorAll("#richTradeBody tr").forEach(r => r.classList.remove("selected"));
}
$("drawerClose").addEventListener("click", closeDrawer);
$("drawerBackdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
$("richTradeBody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-trade-id]");
  if (tr) openDrawer(tr.dataset.tradeId);
});



// ===== Export CSV =====
$("tradeExport").addEventListener("click", () => {
  if (!LATEST) return;
  const items = LATEST.unified_trades || [];
  const filter = state.filter;
  const q = (state.search || "").trim().toLowerCase();
  const filtered = items.filter(t => {
    if (filter !== "all" && t.status !== filter) return false;
    if (!q) return true;
    return [t.asset, t.strategy_full, t.strategy_key, t.timeframe, t.strategy_engine]
      .filter(Boolean).some(v => String(v).toLowerCase().includes(q));
  });
  const cols = ["status","asset","side","size_usd","qty","entry_price","exit_price","current_price",
    "take_price","stop_price","max_neg_usd","pnl_usd","pnl_pct","max_pos_usd",
    "opened_at","closed_at","duration_s","strategy_key","timeframe"];
  const escCsv = (v) => {
    if (v == null) return "";
    const s = String(v);
    return /[,\";\n]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s;
  };
  const lines = [cols.join(",")];
  filtered.forEach(t => lines.push(cols.map(c => escCsv(t[c])).join(",")));
  const blob = new Blob([lines.join("\n")], {type:"text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().slice(0,19).replace(/[-:T]/g,"");
  a.href = url;
  a.download = `trades_${state.filter}_${stamp}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
});

restoreState();
// Aplicar estado restaurado no DOM (setTab/persistState ja estao definidos acima)
setTab(state.tab);
$("tradeSearch").value = state.search || "";
document.querySelectorAll("#tradeFilter button").forEach(x => x.setAttribute("aria-pressed", x.dataset.filter === state.filter ? "true" : "false"));

$("refreshBtn").onclick = load;
$("resetBtn").onclick = async () => {
  if (!confirm("Zerar paper e iniciar nova simulacao com banca global limpa?")) return;
  await fetch("/api/paper/reset", {method:"POST"});
  await load();
};

window.clearKill = async (scope, asset) => {
  const label = scope === "global" ? "GLOBAL" : asset;
  if (!confirm(`Remover kill switch ${label}? Bot voltara a operar ${scope === "global" ? "todos os pares" : "em " + asset}.`)) return;
  let url = `/api/kill/clear?scope=${encodeURIComponent(scope)}`;
  if (asset) url += `&asset=${encodeURIComponent(asset)}`;
  const r = await fetch(url, {method: "POST"});
  const j = await r.json();
  console.log("kill clear:", j);
  await load();
};

load();
setInterval(load, 5000);

// Polling de preco em tempo real direto da Binance (publico, sem auth).
// Atualiza Atual/PnL/Max.Pos/Max.Neg ao vivo: nos cards de Visao geral E
// nas linhas abertas da tabela rica (aba Trades).
const lastPnl = {};
const livePeak = {};   // por ativo: maior PnL ja visto na sessao
const liveTrough = {}; // por ativo: menor PnL ja visto na sessao
function fmtPrice(p){return `$${p.toLocaleString("pt-BR",{minimumFractionDigits:4,maximumFractionDigits:4})}`}
function fmtPnl(v){return `${v>=0?"+":""}$${v.toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})}`}
function flashCell(el, prev, cur){
  if (prev === undefined || Math.abs(cur - prev) <= 0.001) return;
  const f = cur > prev ? "flash-up" : "flash-down";
  el.classList.remove("flash-up","flash-down");
  void el.offsetWidth;
  el.classList.add(f);
}
function durLabelJs(s){if(s==null)return "—";if(s<60)return s+"s";const m=Math.floor(s/60);if(m<60)return m+"m";const h=Math.floor(m/60);return h+"h "+(m%60)+"m"}

async function pollLivePrices(){
  const cards = document.querySelectorAll('.asset[data-asset]');
  const rows  = document.querySelectorAll('#richTradeBody tr[data-asset]');
  const all = [...cards, ...rows];
  if (!all.length) return;
  const symbols = Array.from(new Set(all.map(c => c.dataset.asset).filter(Boolean)));
  if (!symbols.length) return;
  const param = encodeURIComponent(JSON.stringify(symbols));
  let prices = {};
  try {
    const res = await fetch(`https://api.binance.com/api/v3/ticker/price?symbols=${param}`, {cache:"no-store"});
    const arr = await res.json();
    if (Array.isArray(arr)) arr.forEach(it => { prices[it.symbol] = parseFloat(it.price); });
  } catch (e) { return; }

  // Cards (Visao geral)
  cards.forEach(card => {
    const asset = card.dataset.asset;
    const qty = parseFloat(card.dataset.qty);
    const entry = parseFloat(card.dataset.entry);
    const price = prices[asset];
    if (!Number.isFinite(price) || !qty || !entry) return;
    const pnl = qty * (price - entry);
    const priceEl = card.querySelector('[data-live="price"]');
    const pnlEl = card.querySelector('[data-live="pnl"]');
    if (priceEl) priceEl.textContent = fmtPrice(price);
    if (pnlEl) {
      pnlEl.textContent = fmtPnl(pnl);
      pnlEl.className = pnl >= 0 ? "pos" : "neg";
      flashCell(pnlEl, lastPnl[asset], pnl);
    }
    lastPnl[asset] = pnl;
    livePeak[asset]   = livePeak[asset]   === undefined ? pnl : Math.max(livePeak[asset], pnl);
    liveTrough[asset] = liveTrough[asset] === undefined ? pnl : Math.min(liveTrough[asset], pnl);
  });

  // Linhas abertas da tabela rica (aba Trades)
  const nowSec = Math.floor(Date.now() / 1000);
  rows.forEach(row => {
    const asset = row.dataset.asset;
    const qty = parseFloat(row.dataset.qty);
    const entry = parseFloat(row.dataset.entry);
    const price = prices[asset];
    if (!Number.isFinite(price) || !qty || !entry) return;
    const pnl = qty * (price - entry);
    livePeak[asset]   = livePeak[asset]   === undefined ? pnl : Math.max(livePeak[asset], pnl);
    liveTrough[asset] = liveTrough[asset] === undefined ? pnl : Math.min(liveTrough[asset], pnl);
    const atualEl = row.querySelector('[data-live="atual"]');
    const pnlEl   = row.querySelector('[data-live="pnl"]');
    const maxPosEl= row.querySelector('[data-live="maxpos"]');
    const maxNegEl= row.querySelector('[data-live="maxneg"]');
    const durEl   = row.querySelector('[data-live="dur"]');
    if (atualEl) atualEl.textContent = price.toLocaleString("pt-BR",{minimumFractionDigits:4,maximumFractionDigits:4});
    if (pnlEl) {
      const cls = pnl >= 0 ? "pos" : "neg";
      pnlEl.innerHTML = `<span class="${cls}">${fmtPnl(pnl)}</span>`;
      flashCell(pnlEl, lastPnl[asset+"_row"], pnl);
      lastPnl[asset+"_row"] = pnl;
    }
    if (maxPosEl) {
      const v = livePeak[asset];
      const cls = v >= 0 ? "pos" : "neg";
      maxPosEl.innerHTML = `<span class="${cls}">${fmtPnl(v)}</span>`;
    }
    if (maxNegEl) {
      const v = liveTrough[asset];
      const cls = v >= 0 ? "pos" : "neg";
      maxNegEl.innerHTML = `<span class="${cls}">${fmtPnl(v)}</span>`;
    }
    if (durEl) {
      const opened = row.querySelector('[data-live="dur"]').dataset.opened;
      if (opened) {
        const t = Date.parse(opened);
        if (!Number.isNaN(t)) durEl.textContent = durLabelJs(nowSec - Math.floor(t/1000));
      }
    }
  });
}
setInterval(pollLivePrices, 2000);
// Primeira chamada apos primeiro render do load()
setTimeout(pollLivePrices, 800);
