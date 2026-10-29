"""The status page markup. One self-contained HTML document, no external assets."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ccv2-portal-mcp status</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --text: #1c1e21; --muted: #5f6672; --line: #e2e5ea;
    --ok: #1f8a4c; --warn: #b7791f; --bad: #c0392b; --info: #2b6cb0; --chip: #eef1f5; --prod: #fdecec;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #121417; --panel: #1b1e23; --text: #e6e8eb; --muted: #9aa3ad; --line: #2b3038;
            --ok: #3fbf74; --warn: #e0a84a; --bad: #e5655a; --info: #6aa6e8; --chip: #262b33; --prod: #3a2222; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  header { display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: baseline; padding: 18px 24px; border-bottom: 1px solid var(--line); background: var(--panel); }
  header h1 { font-size: 18px; margin: 0; }
  header .meta { color: var(--muted); }
  header .spacer { flex: 1; }
  button { font: inherit; padding: 6px 12px; border: 1px solid var(--line); background: var(--chip); color: var(--text); border-radius: 6px; cursor: pointer; }
  button:hover { border-color: var(--muted); }
  main { padding: 20px 24px; display: grid; gap: 20px; max-width: 1400px; }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px 18px; }
  h2 { font-size: 15px; margin: 0 0 12px; }
  h3 { font-size: 13px; margin: 14px 0 6px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; }
  tr.prod td:first-child { border-left: 3px solid var(--bad); }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; word-break: break-all; }
  .chip { display: inline-block; padding: 1px 8px; border-radius: 10px; background: var(--chip); font-size: 12px; margin: 1px 4px 1px 0; }
  .chip.off { opacity: .45; text-decoration: line-through; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); } .info { color: var(--info); } .muted { color: var(--muted); }
  .dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); } .dot.bad { background: var(--bad); } .dot.muted { background: var(--muted); }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
  .err { white-space: pre-wrap; color: var(--bad); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  details summary { cursor: pointer; color: var(--muted); }
  .small { font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>ccv2-portal-mcp</h1>
  <span class="meta" id="hdr-server">loading…</span>
  <span class="spacer"></span>
  <span class="small" id="hdr-time"></span>
  <button id="btn-refresh" title="Re-read the portal, bypassing the cache">Refresh live data</button>
</header>
<main id="main"><section>Loading…</section></main>
<script>
(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fmtAge = (s) => s == null ? "" : s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s/60)} min` : `${(s/3600).toFixed(1)} h`;
  const fmtTs = (t) => t ? new Date(t).toLocaleString() : "";
  const ago = (t) => { if (!t) return ""; const s = Math.round((Date.now() - new Date(t)) / 1000); return s < 0 ? "" : `${fmtAge(s)} ago`; };
  const dot = (cls) => `<span class="dot ${cls}"></span>`;

  function envStatusClass(e) {
    if (e.error) return "bad";
    const st = (e.status || "").toUpperCase();
    if (st && st !== "AVAILABLE") return "warn";
    if (e.active_deployment) return "info";
    if ((e.hibernation_status || "").toUpperCase().includes("HIBERNAT")) return "warn";
    return "ok";
  }

  function chips(perms, all) {
    const have = new Set(perms);
    return all.map((p) => `<span class="chip ${have.has(p) ? "" : "off"}">${esc(p)}</span>`).join("");
  }

  function renderServer(d) {
    const s = d.server, t = d.tools;
    const run = s.served_by === "mcp"
      ? `${dot("ok")}MCP server running (${esc(s.transport)}, <code>${esc(s.mcp_endpoint)}</code>)`
      : `${dot("muted")}standalone status page; MCP transport configured: <b>${esc(s.transport)}</b>`;
    const settings = Object.entries(s.settings).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    const toolsList = t.items.map((i) => `<span class="chip" title="${i.read_only ? "read-only" : i.destructive ? "write, destructive" : "write"}">${esc(i.name.replace(/^ccv2portal_/, ""))}${i.read_only ? "" : " ✎"}</span>`).join("");
    return `<section>
      <h2>Server</h2>
      <div class="grid2">
        <dl class="kv">
          <dt>status</dt><dd>${run}</dd>
          <dt>version</dt><dd>${esc(s.version)} · Python ${esc(s.python)} · ${esc(s.platform)}</dd>
          <dt>process</dt><dd>pid ${esc(s.pid)}, up ${fmtAge(s.uptime_sec)} (since ${fmtTs(s.started_at)})</dd>
          <dt>configuration</dt><dd>${esc(s.config_source)}</dd>
          <dt>audit log</dt><dd>${s.audit_log_path ? `<code>${esc(s.audit_log_path)}</code>` : "stderr only (AUDIT_LOG_PATH not set)"}</dd>
          <dt>tools</dt><dd>${t.count} registered: ${t.read_only} read-only, ${t.write} write</dd>
        </dl>
        <dl class="kv">${settings}</dl>
      </div>
      <p class="small">${esc(s.note)}</p>
      <details><summary>Tool list (✎ = write)</summary><p>${toolsList}</p></details>
    </section>`;
  }

  function renderSubscription(sub, all) {
    const live = sub.live;
    const auth = live ? live.auth : null;
    const authLine = !live ? `${dot("muted")}live data not requested`
      : auth.ok ? `${dot("ok")}token obtained in ${auth.latency_ms} ms, expires in ${fmtAge(auth.token_expires_in_sec)}`
      : `${dot("bad")}<span class="bad">authentication failed</span>`;
    const cfgPerms = Object.entries(sub.permissions).map(([env, p]) => `<tr><td><code>${esc(env)}</code></td><td>${chips(p, all)}</td></tr>`).join("");
    let envRows = "";
    if (live && live.environments.length) {
      envRows = live.environments.map((e) => {
        const dep = e.deployed;
        const depCell = !e.readable ? `<span class="muted">not readable (no <code>read</code> permission)</span>`
          : e.error ? `<span class="err">${esc(e.error)}</span>`
          : dep ? `<code>${esc(dep.build_code)}</code> <span class="muted">${esc(dep.branch || "")}${dep.build_name ? " · " + esc(dep.build_name) : ""}</span><br><span class="small">${esc(dep.strategy)}/${esc(dep.database_update_mode)} · ${ago(dep.deployed_at)}</span>`
          : `<span class="muted">nothing deployed</span>`;
        const active = e.active_deployment ? `<br><span class="info">deployment ${esc(e.active_deployment.deployment_code)} ${esc(e.active_deployment.status)} (build ${esc(e.active_deployment.build_code)})</span>` : "";
        return `<tr class="${e.production ? "prod" : ""}">
          <td>${dot(envStatusClass(e))}<b>${esc(e.code)}</b><br><span class="small">${esc(e.name || "")} ${e.type ? "· " + esc(e.type) : ""}</span></td>
          <td>${esc(e.status || "?")}<br><span class="small">${esc(e.deployment_status || "")}${e.hibernation_status ? " · " + esc(e.hibernation_status) : ""}</span>${active}</td>
          <td>${depCell}</td>
          <td>${chips(e.permissions, all)}${e.permissions_from_default ? `<br><span class="small">from default</span>` : ""}</td>
        </tr>`;
      }).join("");
    }
    const unknown = live && live.unknown_environments.length
      ? `<p class="warn">Configured but not in the portal: ${live.unknown_environments.map((u) => `<code>${esc(u)}</code>`).join(", ")}</p>` : "";
    const liveErr = live && live.error ? `<p class="err">${esc(live.error)}</p>` : "";
    const authErr = auth && !auth.ok ? `<p class="err">${esc(auth.error)}</p>` : "";
    const cacheNote = live ? `<span class="small">${live.cached ? `cached ${fmtAge(live.age_sec)} ago` : "fetched just now"}</span>` : "";
    return `<section>
      <h2>Subscription <code>${esc(sub.name)}</code>${sub.is_default ? ' <span class="chip">default</span>' : ""}</h2>
      <div class="grid2">
        <dl class="kv">
          <dt>code</dt><dd><code>${esc(sub.code)}</code></dd>
          <dt>API</dt><dd><code>${esc(sub.base_url)}</code></dd>
          <dt>token URL</dt><dd><code>${esc(sub.token_url)}</code></dd>
          <dt>client id</dt><dd><code>${esc(sub.client_id)}</code></dd>
          <dt>secret / resource</dt><dd>${sub.client_secret_set ? "set" : '<span class="bad">missing</span>'} / ${sub.resource_set ? "set" : '<span class="bad">missing</span>'}</dd>
          <dt>auth</dt><dd>${authLine}</dd>
        </dl>
        <div>
          <h3>Configured permissions</h3>
          <table><tr><th>environment</th><th>permissions</th></tr>${cfgPerms || `<tr><td colspan="2" class="muted">none listed</td></tr>`}
          <tr><td class="muted">any other</td><td>${chips(sub.default_permissions, all)}</td></tr></table>
        </div>
      </div>
      ${authErr}${liveErr}${unknown}
      <h3>Environments ${cacheNote}</h3>
      <table>
        <tr><th>environment</th><th>status</th><th>deployed build</th><th>agent permissions</th></tr>
        ${envRows || `<tr><td colspan="4" class="muted">${live ? "no environments returned" : "live data off"}</td></tr>`}
      </table>
    </section>`;
  }

  function renderAudit(a) {
    const rows = a.entries.map((e) => e.raw ? `<tr><td colspan="5"><code>${esc(e.raw)}</code></td></tr>` : `<tr>
      <td class="small">${esc(fmtTs(e.ts))}</td><td>${esc(e.subscription)}</td><td><code>${esc((e.tool || "").replace(/^ccv2portal_/, ""))}</code></td>
      <td>${esc(e.environment || "")}</td>
      <td>${e.error ? `<span class="bad">${esc(e.error)}</span>` : `<span class="ok">ok</span> <span class="small">${esc(JSON.stringify(e.args))}</span>`}</td></tr>`).join("");
    return `<section>
      <h2>Recent write calls</h2>
      ${a.note ? `<p class="small">${esc(a.note)}</p>` : `<p class="small"><code>${esc(a.path)}</code> · ${a.total_lines} entries, newest first</p>`}
      ${rows ? `<table><tr><th>time</th><th>subscription</th><th>tool</th><th>env</th><th>result · args</th></tr>${rows}</table>` : ""}
    </section>`;
  }

  function render(d) {
    $("#hdr-server").textContent = `v${d.server.version} · ${d.server.transport}`;
    $("#hdr-time").textContent = `updated ${fmtTs(d.generated_at)}`;
    document.title = `ccv2-portal-mcp · ${d.subscriptions.map((s) => s.name).join(", ") || "status"}`;
    const all = d.server.permissions_catalogue;
    let html = renderServer(d);
    if (d.config_error) html += `<section><h2>Configuration</h2><p class="err">${esc(d.config_error)}</p></section>`;
    html += d.subscriptions.map((s) => renderSubscription(s, all)).join("");
    html += renderAudit(d.audit);
    $("#main").innerHTML = html;
  }

  async function load(refresh) {
    const btn = $("#btn-refresh");
    btn.disabled = true;
    try {
      const r = await fetch(`api/status${refresh ? "?refresh=1" : ""}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      render(await r.json());
    } catch (e) {
      $("#main").innerHTML = `<section><p class="err">Cannot load status: ${esc(e.message)}</p></section>`;
    } finally {
      btn.disabled = false;
    }
  }
  $("#btn-refresh").addEventListener("click", () => load(true));
  load(false);
  setInterval(() => load(false), 60000);
})();
</script>
</body>
</html>
"""
