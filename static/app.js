let nodes = [];
let ports = {};
let statusSnapshot = {};
let testingTags = new Set();
let validationDetails = {};
let autoSelectAliveForAssign = true;
let assignFilterTags = null;
let validatingPorts = new Set();
let nodeSearchQuery = "";
let nodeStatusFilter = "all";
let _nodeTableFingerprint = "";
let proxyAdminResults = {};
let proxyAdminImported = [];
let proxyAdminConfigLoaded = false;
let localProxyCheckResults = {};
let localProxyCheckingPorts = new Set();
let activeNodeTestJobId = null;
let activePortValidationJobId = null;
let activeLocalProxyCheckJobId = null;
let activeProxyAdminJobId = null;
const ACTIVE_TAB_KEY = "proxyPoolManager.activeTab";
let authState = { enabled: false, authenticated: true };

const DEFAULT_VALIDATION_URLS = [
  "http://cp.cloudflare.com/generate_204",
  "https://www.google.com/generate_204",
  "https://www.gstatic.com/generate_204",
  "https://www.cloudflare.com/cdn-cgi/trace"
];
const EXIT_IP_CHECK_URL = "https://ipv4.webshare.io/";

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    showAuthGate(data.detail || "需要登录");
    throw new Error(data.detail || "需要登录");
  }
  if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
  return data;
}

let _noticeTimer = null;

function showNotice(message, tone = "info") {
  const notice = $("notice");
  if (!notice) return;
  if (_noticeTimer) {
    clearTimeout(_noticeTimer);
    _noticeTimer = null;
  }
  if (!message) {
    notice.className = "notice hidden";
    notice.textContent = "";
    return;
  }
  notice.className = `notice ${tone}`;
  notice.textContent = tone === "bad" ? compactCheckMessage(message) : message;
  notice.title = String(message || "");
  // ponytail: auto-dismiss; errors get more time to read
  const ms = tone === "bad" ? 6000 : 3500;
  _noticeTimer = setTimeout(() => {
    if (notice.textContent) {
      notice.className = "notice hidden";
      notice.textContent = "";
    }
    _noticeTimer = null;
  }, ms);
}

function showAuthGate(message = "") {
  const gate = $("authGate");
  const error = $("authError");
  document.body.classList.remove("auth-pending");
  if (gate) gate.classList.remove("hidden");
  if (error) error.textContent = message;
}

function hideAuthGate() {
  const gate = $("authGate");
  const error = $("authError");
  document.body.classList.remove("auth-pending");
  if (gate) gate.classList.add("hidden");
  if (error) error.textContent = "";
}

async function loadAuthState() {
  const response = await fetch("/api/auth/status");
  authState = await response.json();
  if (authState.enabled && !authState.authenticated) {
    showAuthGate();
    return false;
  }
  hideAuthGate();
  return true;
}

async function loginWithAdminKey(key) {
  const result = await request("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ key })
  });
  authState = result;
  hideAuthGate();
  await refresh();
}

function nodeStatusBadge(node) {
  if (testingTags.has(node.tag)) return `<span class="badge testing">测速中</span>`;
  return statusBadge(node.latency);
}

function localProxyCheckForNodeTag(tag) {
  if (!tag) return null;
  for (const [port, item] of Object.entries(ports || {})) {
    if (item?.node_tag !== tag) continue;
    return item.local_proxy_check || localProxyCheckResults[String(port)] || null;
  }
  return null;
}

function geoContextForNode(node) {
  return {
    latency: node?.latency || null,
    geoip: node?.latency?.geoip || null,
    local_proxy_check: localProxyCheckForNodeTag(node?.tag)
  };
}

function geoContextForPort(port, item) {
  return {
    latency: item?.latency || null,
    geoip: item?.geoip || item?.latency?.geoip || null,
    local_proxy_check: item?.local_proxy_check || localProxyCheckResults[String(port)] || null
  };
}

function nodeTestStats() {
  const total = nodes.length;
  const testing = testingTags.size;
  const alive = nodes.filter((node) => node.latency?.alive).length;
  const failed = nodes.filter((node) => node.latency && !node.latency.alive).length;
  const untested = Math.max(total - alive - failed, 0);
  const delays = nodes
    .map((node) => node.latency?.delay)
    .filter((delay) => typeof delay === "number");
  const avgDelay = delays.length ? Math.round(delays.reduce((sum, delay) => sum + delay, 0) / delays.length) : null;
  return { total, testing, alive, failed, untested, avgDelay };
}

function renderNodeTestOverview() {
  const box = $("nodeTestOverview");
  if (!box) return;
  const stats = nodeTestStats();
  const targetUrls = selectedNodeTestUrls();
  const targetText = targetUrls?.length ? targetUrls.map(compactUrl).join(" / ") : "Cloudflare 204";
  const geoText = $("nodeTestGeo")?.checked ? "查出口/地区" : "只测速";
  const alivePct = stats.total ? Math.round((stats.alive / stats.total) * 100) : 0;
  const pct = (n) => (stats.total ? Math.round((n / stats.total) * 100) : 0);
  box.innerHTML = `
    <div class="health" title="可用率">
      <div class="ring" style="--p:${alivePct}">
        <div>
          <strong>${alivePct}%</strong>
          <small>可用率</small>
        </div>
      </div>
    </div>
    <div class="overview-metric">
      <span><svg class="mi" width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7h16M7 12h10M9 17h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>总节点</span>
      <strong>${stats.total}</strong>
      <span class="spark"><i style="width:100%"></i></span>
    </div>
    <div class="overview-metric ok">
      <span><svg class="mi" width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>可用</span>
      <strong>${stats.alive}</strong>
      <span class="spark"><i style="width:${pct(stats.alive)}%"></i></span>
    </div>
    <div class="overview-metric bad">
      <span><svg class="mi" width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 7l10 10M17 7 7 17" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>失败</span>
      <strong>${stats.failed}</strong>
      <span class="spark"><i style="width:${pct(stats.failed)}%"></i></span>
    </div>
    <div class="overview-metric idle">
      <span><svg class="mi" width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="2"/><path d="M12 8v4l3 2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>未测</span>
      <strong>${stats.untested}</strong>
      <span class="spark"><i style="width:${pct(stats.untested)}%;background:var(--faint,#94a3b8)"></i></span>
    </div>
    <div class="overview-metric testing">
      <span><svg class="mi" width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M13 3 4 14h7l-1 7 10-12h-7l0-6z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>测速中</span>
      <strong>${stats.testing}</strong>
      <span class="spark"><i style="width:${stats.testing ? 40 : 0}%"></i></span>
    </div>
  `;
  // target line stays compact under filter via title on overview container
  box.title = `${targetText} · ${geoText}${stats.avgDelay === null ? "" : ` · 平均 ${stats.avgDelay}ms`}`;
}

// function renderSummary() {
//   if (!statusSnapshot.running) {
//     $("engineState").textContent = "未运行";
//   } else if (statusSnapshot.config_matches_state === false) {
//     const configured = statusSnapshot.config_ports?.length || 0;
//     const expected = statusSnapshot.expected_ports?.length || 0;
//     $("engineState").textContent = `配置不一致 #${statusSnapshot.pid} ${configured}/${expected}`;
//   } else if (statusSnapshot.ready === false) {
//     const listening = statusSnapshot.listening_ports?.length || 0;
//     const expected = statusSnapshot.expected_ports?.length || 0;
//     $("engineState").textContent = `启动中 #${statusSnapshot.pid} ${listening}/${expected}`;
//   } else {
//     $("engineState").textContent = `运行中 #${statusSnapshot.pid}`;
//   }
//   $("nodeCount").textContent = String(statusSnapshot.node_count ?? nodes.length);
//   $("mappingCount").textContent = String(statusSnapshot.mapping_count ?? Object.keys(ports).length);
// }


function setTone(id, tone) {
  const element = $(id);
  if (!element) return;
  element.classList.remove("tone-ok", "tone-bad", "tone-warn", "tone-muted", "tone-info");
  element.classList.add(`tone-${tone}`);
}

function renderSummary() {
  const expectedPorts = statusSnapshot.expected_ports || [];
  const listeningPorts = statusSnapshot.listening_ports || [];
  const missingPorts = statusSnapshot.missing_ports || expectedPorts.filter((port) => !listeningPorts.includes(port));
  let tone = "muted";
  if (!statusSnapshot.running) {
    $("engineState").textContent = "未运行";
  } else if (statusSnapshot.config_matches_state === false) {
    const configured = statusSnapshot.config_ports?.length || 0;
    const expected = statusSnapshot.expected_ports?.length || 0;
    $("engineState").textContent = `需重启 #${statusSnapshot.pid} ${configured}/${expected}`;
    tone = "bad";
  } else if (statusSnapshot.ready === false) {
    const listening = statusSnapshot.listening_ports?.length || 0;
    const expected = statusSnapshot.expected_ports?.length || 0;
    $("engineState").textContent = `启动中 #${statusSnapshot.pid} ${listening}/${expected}`;
    tone = "info";
  } else {
    $("engineState").textContent = `运行中 #${statusSnapshot.pid}`;
    tone = "ok";
  }
  $("engineState").title = $("engineState").textContent;
  setTone("engineState", tone);
  $("nodeCount").textContent = String(statusSnapshot.node_count ?? nodes.length);
  $("mappingCount").textContent = String(statusSnapshot.mapping_count ?? Object.keys(ports).length);
  $("listeningCount").textContent = `${listeningPorts.length}/${expectedPorts.length}`;
  setTone("nodeCount", tone);
  setTone("mappingCount", tone);
  setTone("listeningCount", missingPorts.length ? "bad" : tone);
  $("listeningCount").title = missingPorts.length
    ? `异常端口：${missingPorts.join(", ")}`
    : "所有映射端口均在监听";
  renderEngineHealth(expectedPorts, listeningPorts, missingPorts);
}

function renderSubscription(subscription) {
  const data = subscription || statusSnapshot.subscription || {};
  const urlInput = $("urlInput");
  const intervalInput = $("subscriptionInterval");
  const summary = $("subscriptionSummary");
  if (!summary) return;
  if (urlInput && document.activeElement !== urlInput) {
    urlInput.value = data.url || "";
  }
  if (intervalInput && document.activeElement !== intervalInput) {
    intervalInput.value = Number(data.refresh_interval_minutes || 0);
  }
  if (!data.url) {
    summary.textContent = "未配置";
    summary.className = "local-check-result muted";
    summary.title = "";
    return;
  }
  if (data.last_error) {
    summary.textContent = "刷新失败";
    summary.className = "local-check-result bad";
    summary.title = data.last_error;
    return;
  }
  const interval = Number(data.refresh_interval_minutes || 0);
  const nextText = data.next_refresh_in_seconds === null || data.next_refresh_in_seconds === undefined
    ? ""
    : `；下次 ${Math.ceil(Number(data.next_refresh_in_seconds || 0) / 60)} 分钟`;
  const lastText = data.last_refresh_at ? `；上次 ${data.last_count || 0} 个` : "";
  summary.textContent = interval > 0 ? `每 ${interval} 分钟${lastText}${nextText}` : `已保存${lastText}`;
  summary.className = "local-check-result ok";
  summary.title = data.url;
}

function renderEngineHealth(expectedPorts, listeningPorts, missingPorts) {
  const box = $("engineHealth");
  if (!box) return;
  const configMismatch = statusSnapshot.config_matches_state === false;
  const hasIssue = configMismatch || missingPorts.length > 0 || (statusSnapshot.running && statusSnapshot.ready === false);
  const repairButton = $("repairEngineBtn");
  if (repairButton) repairButton.classList.toggle("hidden", !hasIssue);

  let statusText = "未运行";
  let tone = "idle";
  if (statusSnapshot.running && configMismatch) {
    statusText = "需重启";
    tone = "bad";
  } else if (statusSnapshot.running && statusSnapshot.ready === false) {
    statusText = "启动中";
    tone = "bad";
  } else if (statusSnapshot.running) {
    statusText = "运行中";
    tone = hasIssue ? "bad" : "ok";
  }
  if (missingPorts.length) tone = "bad";

  const listen = `${listeningPorts.length}/${expectedPorts.length}`;
  const bits = [
    `<span class="eh-status">${escapeHtml(statusText)}</span>`,
    `<span>监听 <strong>${escapeHtml(listen)}</strong></span>`,
  ];
  if (missingPorts.length) {
    const missingText = missingPorts.join(", ");
    bits.push(`<span class="eh-issue" title="${escapeHtml(missingText)}">缺失 ${missingPorts.length}</span>`);
  } else if (configMismatch) {
    bits.push(`<span class="eh-issue">配置不一致</span>`);
  }
  box.className = `engine-health ${tone}`;
  box.innerHTML = bits.join("");
  box.title = missingPorts.length
    ? `异常端口：${missingPorts.join(", ")}`
    : (statusSnapshot.pid ? `PID #${statusSnapshot.pid}` : "");
}

function renderDoctorResult(result) {
  const box = $("doctorResult");
  const summary = $("doctorSummary");
  if (!box || !summary) return;
  if (!result) {
    box.className = "doctor-result hidden";
    box.innerHTML = "";
    summary.textContent = "未运行";
    return;
  }
  const tone = result.fail ? "fail" : result.warn ? "warn" : "ok";
  const summaryText = result.summary || `ok=${result.ok || 0} warn=${result.warn || 0} fail=${result.fail || 0}`;
  summary.textContent = result.timed_out ? "超时" : summaryText;
  summary.className = `local-check-result ${tone === "ok" ? "ok" : tone === "warn" ? "muted" : "bad"}`;
  const lines = (result.lines || [])
    .filter((line) => /^\[(OK|WARN|FAIL)\]/.test(line) || /^status:|^missing ports:|^config:/.test(line))
    .slice(0, 12);
  const chips = [
    `<span class="${tone}" title="${escapeHtml(summaryText)}">自检 ${escapeHtml(summaryText)}</span>`,
    `<span class="ok">OK ${Number(result.ok || 0)}</span>`,
    `<span class="warn">WARN ${Number(result.warn || 0)}</span>`,
    `<span class="fail">FAIL ${Number(result.fail || 0)}</span>`,
    ...lines.map((line) => {
      const cls = line.startsWith("[FAIL]") ? "fail" : line.startsWith("[WARN]") ? "warn" : "ok";
      return `<span class="${cls}" title="${escapeHtml(line)}">${escapeHtml(compactCheckMessage(line))}</span>`;
    })
  ];
  box.className = "doctor-result";
  box.innerHTML = chips.join("");
}

function proxyConnectHost() {
  return statusSnapshot.proxy_connect_host || window.location.hostname || "127.0.0.1";
}

function proxyAuthority(port) {
  const host = proxyConnectHost();
  const formattedHost = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  return `${formattedHost}:${port}`;
}

function renderNodeTable() {
  renderNodeTestOverview();
  if (!nodes.length) {
    _nodeTableFingerprint = "";
    $("nodeTable").innerHTML = `<div class="empty"><strong>还没有节点</strong><span>在上方导入订阅 URL，或展开粘贴单节点链接。</span></div>`;
    $("nodeFilterCount").textContent = "";
    return;
  }
  const query = nodeSearchQuery.trim().toLowerCase();
  const filtered = nodes.filter((node) => {
    if (query) {
      const haystack = [node.name, node.tag, node.type, node.server, String(node.server_port || "")].join(" ").toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    if (nodeStatusFilter === "alive") {
      return node.latency && node.latency.alive;
    } else if (nodeStatusFilter === "failed") {
      return node.latency && !node.latency.alive;
    } else if (nodeStatusFilter === "untested") {
      return !node.latency;
    }
    return true;
  });
  $("nodeFilterCount").textContent = filtered.length !== nodes.length ? `${filtered.length} / ${nodes.length}` : `${nodes.length}`;
  if (!filtered.length) {
    _nodeTableFingerprint = "";
    $("nodeTable").innerHTML = `<div class="empty"><strong>没有匹配节点</strong><span>${query ? `搜索「${escapeHtml(nodeSearchQuery)}」` : "当前过滤条件"}无结果，试试切换筛选。</span></div>`;
    return;
  }
  // Skip expensive DOM rebuild if data hasn't changed
  const fingerprint = `${nodeStatusFilter}|${nodeSearchQuery}|${filtered.map((n) => `${n.tag}:${n.latency?.alive ?? ""}:${n.latency?.delay ?? ""}:${n.latency?.exit_ip ?? ""}`).join(",")}`;
  if (fingerprint === _nodeTableFingerprint) return;
  _nodeTableFingerprint = fingerprint;

  $("nodeTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th><input id="checkAllNodes" type="checkbox" aria-label="选择全部节点" checked></th>
          <th>节点</th>
          <th>协议</th>
          <th>服务器</th>
          <th>状态</th>
          <th>延迟</th>
          <th>出口 IP</th>
          <th>地区</th>
          <th>目标结果</th>
        </tr>
      </thead>
      <tbody>${filtered.map((node) => `
        <tr>
          <td data-label="选择"><input type="checkbox" class="node-check" data-tag="${escapeHtml(node.tag)}" aria-label="选择节点 ${escapeHtml(node.name)}" checked></td>
          <td data-label="节点">
            <strong>${escapeHtml(node.name)}</strong>
            <small>${escapeHtml(node.tag)}</small>
          </td>
          <td data-label="协议">${protocolChip(node.type)}</td>
          <td data-label="服务器"><span class="mono">${escapeHtml(node.server)}:${node.server_port}</span></td>
          <td data-label="状态">${nodeStatusBadge(node)}</td>
          <td data-label="延迟">${latencyBarHtml(node.latency)}</td>
          <td data-label="出口 IP" class="mono">${escapeHtml(node.latency?.exit_ip || "-")}</td>
          <td data-label="地区">${geoChipHtml(geoContextForNode(node))}</td>
          <td data-label="目标结果" class="result-preview" title="${escapeHtml(targetResponseText(node.latency))}">${escapeHtml(targetResponsePreview(node.latency))}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderAssignTable() {
  const sourceNodes = assignFilterTags ? nodes.filter((node) => assignFilterTags.has(node.tag)) : nodes;
  const assignNodes = sourceNodes
    .map((node, index) => {
      const assignedPort = Object.entries(ports).find(([, item]) => item.node_tag === node.tag)?.[0] || "";
      return { node, index, assignedPort };
    })
    .sort((left, right) => {
      const leftPort = left.assignedPort ? Number(left.assignedPort) : Number.MAX_SAFE_INTEGER;
      const rightPort = right.assignedPort ? Number(right.assignedPort) : Number.MAX_SAFE_INTEGER;
      if (leftPort !== rightPort) return leftPort - rightPort;
      return left.index - right.index;
    });
  if (!assignNodes.length) {
    $("assignTable").innerHTML = `<div class="empty"><strong>没有可分配节点</strong><span>先到节点页导入并测速，再回来分配端口。</span></div>`;
    return;
  }
  $("assignTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>使用</th>
          <th>端口</th>
          <th>节点</th>
          <th>出口 IP</th>
          <th>地区</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>${assignNodes.map(({ node, assignedPort }) => {
        const checked = assignedPort || (autoSelectAliveForAssign && node.latency?.alive) ? "checked" : "";
        return `<tr>
          <td data-label="使用"><input type="checkbox" class="assign-check" data-tag="${escapeHtml(node.tag)}" aria-label="分配节点 ${escapeHtml(node.name)}" ${checked}></td>
          <td data-label="端口"><input class="port-input" type="number" data-port-for="${escapeHtml(node.tag)}" value="${assignedPort}" min="1024" max="65535"></td>
          <td data-label="节点">${escapeHtml(node.name)}</td>
          <td data-label="出口 IP" class="mono">${escapeHtml(node.latency?.exit_ip || "-")}</td>
          <td data-label="地区">${geoChipHtml(geoContextForNode(node))}</td>
          <td data-label="状态">${statusBadge(node.latency)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
}

function renderPortsTable() {
  const entries = sortedPortEntries();
  const curlTarget = activeValidationUrl();
  if (!entries.length) {
    $("portsTable").innerHTML = `<div class="empty"><strong>还没有端口映射</strong><span>到「分配」页勾选节点、填端口并保存映射。</span></div>`;
    return;
  }
  $("portsTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>端口</th>
          <th>节点</th>
          <th>协议</th>
          <th>状态</th>
          <th>延迟</th>
          <th>验证结果</th>
          <th>本地检测</th>
          <th>出口 IP</th>
          <th>地区</th>
          <th>curl 验证</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>${entries.map(([port, item]) => {
        const authority = proxyAuthority(port);
        const httpProxy = `http://${authority}/`;
        const socksProxy = `socks5://${authority}`;
        const curlCommand = `curl --proxy "http://${authority}/" https://ipv4.webshare.io`;
        return `
        <tr>
          <td data-label="端口" class="mono copyable" data-copy="${escapeHtml(httpProxy)}" data-copy-label="HTTP 代理" title="copy ${escapeHtml(httpProxy)}">${port}</td>
          <td data-label="节点">${escapeHtml(item.node_name || item.node_tag)}</td>
          <td data-label="协议">${protocolChip(item.type || "-")}</td>
          <td data-label="状态">${validatingPorts.has(String(port)) ? '<span class="badge testing">验证中</span>' : statusBadge(item.latency)}</td>
          <td data-label="延迟">${latencyBarHtml(item.latency)}</td>
          <td data-label="验证结果" class="result-preview" title="${escapeHtml(targetResponseText(item.latency))}">${escapeHtml(targetResponsePreview(item.latency))}</td>
          <td data-label="本地检测">${localProxyPortSummary(port) || '<span class="muted">-</span>'}</td>
          <td data-label="出口 IP" class="mono" id="ip-${port}">${escapeHtml(item.exit_ip || item.latency?.exit_ip || "-")}</td>
          <td data-label="地区"><span id="geo-${port}">${geoChipHtml(geoContextForPort(port, item))}</span></td>
          <td data-label="curl"><code class="copyable" data-copy="${escapeHtml(curlCommand)}" data-copy-label="curl 命令" title="copy curl 命令">${escapeHtml(curlCommand)}</code></td>
          <td data-label="操作">
            <div class="table-actions">
              <button data-ip-port="${port}">查出口</button>
              <button data-validate-port="${port}">验证</button>
              <button data-copy="${escapeHtml(socksProxy)}" data-copy-label="标准 SOCKS5">复制 SOCKS</button>
              <button data-remove-port="${port}">移除映射</button>
            </div>
          </td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
  renderValidationResults();
}

// Event delegation for ports table — avoids re-attaching listeners on every render
$("portsTable").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) {
    const copyEl = event.target.closest("[data-copy]");
    if (copyEl) {
      copyText(copyEl.dataset.copy, copyEl.dataset.copyLabel || "内容");
    }
    return;
  }
  if (button.dataset.ipPort) {
    const port = button.dataset.ipPort;
    await runTask(`查询端口 ${port} 出口 IP`, async () => {
      const result = await request(`/api/ports/${port}/ip`);
      $("ip-" + port).textContent = result.exit_ip || result.error || "失败";
      const geoCell = $("geo-" + port);
      if (geoCell) {
        const compact = result.geoip_compact || geoIpCompact(result.geoip) || "-";
        const summary = result.geoip_summary || geoIpSummary(result.geoip) || compact;
        const code = String(result.geoip?.country_code || compact).slice(0, 2).toLowerCase();
        const flagCls = ["hk", "jp", "us", "tw", "sg", "de", "kr", "gb", "uk", "cn"].includes(code) ? code : "";
        geoCell.innerHTML = compact === "-"
          ? `<span class="badge idle">—</span>`
          : `<span class="geoip-chip" title="${escapeHtml(summary)}"><span class="flag ${flagCls}"></span>${escapeHtml(compact)}</span>`;
      }
      showQuickResult(
        `端口 ${port}`,
        result.exit_ip ? `出口 IP：${result.exit_ip}；地区：${result.geoip_compact || geoIpCompact(result.geoip) || "-"}` : `失败：${result.error || "未知错误"}`,
        Boolean(result.exit_ip)
      );
      await refresh();
    });
  } else if (button.dataset.validatePort) {
    await runSinglePortValidation(Number(button.dataset.validatePort));
  } else if (button.dataset.removePort) {
    await removePortMapping(button.dataset.removePort);
  } else if (button.dataset.copy) {
    copyText(button.dataset.copy, button.dataset.copyLabel || "内容");
  }
});

function renderLocalProxyCheckPorts() {
  const select = $("localProxyCheckPort");
  if (!select) return;
  const selected = select.value;
  const portEntries = Object.keys(ports).sort((left, right) => Number(left) - Number(right));
  select.innerHTML = portEntries.length
    ? portEntries.map((port) => `<option value="${escapeHtml(port)}">${escapeHtml(port)}</option>`).join("")
    : '<option value="">无端口</option>';
  if (selected && portEntries.includes(selected)) select.value = selected;
}


function renderLocalProxyCheckResult(result) {
  const box = $("localProxyCheckResult");
  if (!box) return;
  if (!result) {
    box.className = "local-check-result muted";
    box.textContent = "未检测";
    box.title = "";
    return;
  }
  const failed = localProxyCheckFailed(result);
  const items = result.items || [];
  const failedMessage = result.error || items.find((item) => item.status === "fail")?.message || "";
  const compactFailedMessage = compactCheckMessage(failedMessage || result.summary || "失败");
  const targets = items.map((item) => `${item.target}: ${item.status} ${item.latency_ms || "-"}ms ${item.message || ""}`).join("\n");
  const targetChips = items
    .filter((item) => item.target !== "base_connectivity")
    .slice(0, 4)
    .map((item) => `<b class="${escapeHtml(item.status || "err")}">${escapeHtml(shortTargetName(item.target))}</b>`)
    .join("");
  box.className = `local-check-result ${failed ? "bad" : "ok"}`;
  box.innerHTML = failed
    ? `<span>${escapeHtml(result.grade || "ERR")} · ${escapeHtml(compactFailedMessage)}</span>`
    : `<span>${escapeHtml(result.grade || "-")} · ${escapeHtml(result.score ?? "-")} · ${escapeHtml(result.exit_ip || "-")} · ${escapeHtml(result.country || result.country_code || "-")}</span><span class="local-check-targets">${targetChips}</span>`;
  box.title = targets || result.summary || failedMessage || "";
}

function setCancelButton(id, visible) {
  const button = $(id);
  if (button) button.classList.toggle("hidden", !visible);
}

async function runLocalProxyCheckForPort(port) {
  localProxyCheckingPorts.add(String(port));
  renderPortsTable();
  
  try {
    const result = await request("/api/proxy-check", {
      method: "POST",
      body: JSON.stringify({ port: Number(port), timeout: 30 })
    });
    localProxyCheckResults[String(port)] = result;
    renderLocalProxyCheckResult(result);
    return result;
  } catch (error) {
    const result = {
      id: Number(port),
      score: 0,
      grade: "ERR",
      exit_ip: "",
      country: "",
      error: error.message,
      items: [{ target: "base_connectivity", status: "fail", message: error.message }]
    };
    localProxyCheckResults[String(port)] = result;
    renderLocalProxyCheckResult(result);
    return result;
  } finally {
    localProxyCheckingPorts.delete(String(port));
    renderPortsTable();
    
  }
}

async function runLocalProxyCheckForPorts(portList, concurrency = 3) {
  portList.forEach((port) => localProxyCheckingPorts.add(String(port)));
  renderPortsTable();
  
  const started = await request("/api/proxy-check/start", {
    method: "POST",
    body: JSON.stringify({ ports: portList.map(Number), timeout: 30, concurrency })
  });
  activeLocalProxyCheckJobId = started.id;
  setCancelButton("cancelLocalProxyCheckBtn", true);
  return pollLocalProxyCheckJob(started.id, portList);
}

async function pollLocalProxyCheckJob(jobId, portList) {
  let finalJob = null;
  for (;;) {
    const job = await request(`/api/proxy-check/jobs/${jobId}`);
    finalJob = job;
    Object.entries(job.results || {}).forEach(([port, result]) => {
      localProxyCheckResults[String(port)] = result;
      localProxyCheckingPorts.delete(String(port));
    });
    if (job.status === "done" || job.status === "error" || job.status === "canceled") {
      portList.forEach((port) => localProxyCheckingPorts.delete(String(port)));
    }
    renderPortsTable();
    
    $("localProxyCheckResult").textContent = `批量 ${job.completed}/${job.total}`;
    if (job.status === "done") break;
    if (job.status === "canceled") break;
    if (job.status === "error") {
      activeLocalProxyCheckJobId = null;
      setCancelButton("cancelLocalProxyCheckBtn", false);
      throw new Error(job.error || "本地检测任务失败");
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  activeLocalProxyCheckJobId = null;
  setCancelButton("cancelLocalProxyCheckBtn", false);
  const results = Object.values(finalJob?.results || {});
  return {
    passed: results.filter((result) => !localProxyCheckFailed(result)).length,
    failed: results.filter((result) => localProxyCheckFailed(result)).length
  };
}

function proxyAdminResultByPort() {
  const resultById = new Map(Object.values(proxyAdminResults).map((result) => [String(result.id), result]));
  const byPort = new Map();
  proxyAdminImported.forEach((item) => {
    const result = resultById.get(String(item.id));
    if (result) byPort.set(String(item.port), { imported: item, result });
  });
  return byPort;
}

function proxyAdminPortSummary(port) {
  const entry = proxyAdminResultByPort().get(String(port));
  const local = localProxyPortSummary(port);
  if (!entry) return local || '<span class="muted">-</span>';
  const { result } = entry;
  const failed = result.grade === "ERR" || (result.items || []).some((item) => item.status === "fail");
  const targets = (result.items || []).slice(0, 4);
  const failedMessage = result.error || (result.items || []).find((item) => item.status === "fail")?.message || "";
  const compactFailedMessage = compactCheckMessage(failedMessage || result.summary || "失败");
  const title = (result.items || [])
    .map((item) => `${item.target}: ${item.status} ${item.http_status || "-"} ${item.latency_ms || "-"}ms ${item.message || ""}`)
    .join("\n");
  return `
    <div class="proxy-admin-inline" title="${escapeHtml(title || result.error || "")}">
      <span class="badge ${failed ? "bad" : "ok"}">${escapeHtml(result.grade || "-")} · ${escapeHtml(result.score ?? "-")}</span>
      ${failed ? `<span>${escapeHtml(compactFailedMessage)}</span>` : `
        <span class="mono">${escapeHtml(result.exit_ip || "-")}</span>
        <span>${escapeHtml(result.country || "-")}</span>
        <span class="proxy-admin-mini-targets">
          ${targets.map((item) => `<b class="${escapeHtml(item.status || "err")}">${escapeHtml(shortTargetName(item.target))}</b>`).join("")}
        </span>
      `}
    </div>
    ${local}
  `;
}

function localProxyPortSummary(port) {
  if (localProxyCheckingPorts.has(String(port))) {
    return '<div class="proxy-admin-inline local"><span class="badge testing">本地检测中</span></div>';
  }
  const result = localProxyCheckResults[String(port)];
  if (!result) return "";
  const failed = localProxyCheckFailed(result);
  const items = result.items || [];
  const failedMessage = result.error || items.find((item) => item.status === "fail")?.message || "";
  const compactFailedMessage = compactCheckMessage(failedMessage || result.summary || "失败");
  const title = items
    .map((item) => `${item.target}: ${item.status} ${item.http_status || "-"} ${item.latency_ms || "-"}ms ${item.message || ""}`)
    .join("\n");
  const targetChips = items
    .filter((item) => item.target !== "base_connectivity")
    .slice(0, 4)
    .map((item) => `<b class="${escapeHtml(item.status || "err")}">${escapeHtml(shortTargetName(item.target))}</b>`)
    .join("");
  return `
    <div class="proxy-admin-inline local" title="${escapeHtml(title || failedMessage || result.summary || "")}">
      <span class="badge ${failed ? "bad" : "ok"}">本地 ${escapeHtml(result.grade || "-")} · ${escapeHtml(result.score ?? "-")}</span>
      ${failed ? `<span>${escapeHtml(compactFailedMessage)}</span>` : `
        <span class="mono">${escapeHtml(result.exit_ip || "-")}</span>
        <span>${escapeHtml(result.country || result.country_code || "-")}</span>
        <span class="proxy-admin-mini-targets">${targetChips}</span>
      `}
    </div>
  `;
}

function shortTargetName(target) {
  const names = {
    base_connectivity: "base",
    openai: "gpt",
    anthropic: "claude",
    gemini: "gemini"
  };
  return names[target] || target || "-";
}

function proxyAdminResultFailed(result) {
  return result?.grade === "ERR" || (result?.items || []).some((item) => item.status === "fail");
}

function qualityFromResult(result) {
  if (!result) return { bucket: 1, gradeRank: 99, score: -1 };
  if (proxyAdminResultFailed(result)) return { bucket: 2, gradeRank: 99, score: Number(result.score || 0) };
  const gradeOrder = { A: 0, B: 1, C: 2, D: 3, F: 4 };
  return {
    bucket: 0,
    gradeRank: gradeOrder[String(result.grade || "").toUpperCase()] ?? 50,
    score: Number(result.score || 0)
  };
}

function proxyAdminQuality(entry) {
  return qualityFromResult(entry?.result);
}

function latencySortValue(latency) {
  const rank = !latency ? 2 : (latency.alive ? 0 : 1);
  const delay = latency?.delay ?? Number.MAX_SAFE_INTEGER;
  return { rank, delay };
}

function sortedPortEntries() {
  const hasLocalProxyResults = Object.keys(localProxyCheckResults).length > 0;
  return Object.entries(ports).sort(([leftPort, left], [rightPort, right]) => {
    if (hasLocalProxyResults) {
      const leftQuality = qualityFromResult(localProxyCheckResults[String(leftPort)]);
      const rightQuality = qualityFromResult(localProxyCheckResults[String(rightPort)]);
      if (leftQuality.bucket !== rightQuality.bucket) return leftQuality.bucket - rightQuality.bucket;
      if (leftQuality.gradeRank !== rightQuality.gradeRank) return leftQuality.gradeRank - rightQuality.gradeRank;
      if (leftQuality.score !== rightQuality.score) return rightQuality.score - leftQuality.score;
    }
    const leftLatency = latencySortValue(left.latency);
    const rightLatency = latencySortValue(right.latency);
    if (leftLatency.rank !== rightLatency.rank) return leftLatency.rank - rightLatency.rank;
    if (leftLatency.delay !== rightLatency.delay) return leftLatency.delay - rightLatency.delay;
    return Number(leftPort) - Number(rightPort);
  });
}

async function removeProxyAdminProxyForPort(port) {
  await runTask(`移除端口 ${port} 远端代理`, async () => {
    await saveProxyAdminConfig();
    const entry = proxyAdminResultByPort().get(String(port));
    const id = Number(entry?.imported?.id || entry?.result?.id);
    if (!id || id <= 0) throw new Error("这个端口还没有有效的 ProxyAdmin 远端记录");
    const result = await request("/api/proxy-admin/remove", {
      method: "POST",
      body: JSON.stringify(proxyAdminPayload({ ids: [id], concurrency: 1 }))
    });
    const removed = (result.removed || []).find((item) => Number(item.id) === id);
    if (removed && !removed.success) throw new Error(removed.error || "远端删除失败");
    delete proxyAdminResults[String(id)];
    proxyAdminImported = proxyAdminImported.filter((item) => Number(item.id) !== id);
    renderProxyAdminResults();
    renderPortsTable();
    return `端口 ${port} 的远端代理已移除`;
  });
}

function showQuickResult(title, body, ok = true) {
  const box = $("quickResult");
  box.className = `quick-result ${ok ? "ok" : "bad"}`;
  box.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(body)}</span>`;
}

async function copyText(text, label = "内容") {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    showQuickResult("已复制", label, true);
  } catch (error) {
    showQuickResult("复制失败", error.message || "浏览器拒绝访问剪贴板", false);
  }
}

function renderValidationResults() {
  const entries = Object.entries(validationDetails || {});
  if (!entries.length) {
    $("validationResults").innerHTML = "";
    return;
  }
  $("validationResults").innerHTML = entries.map(([port, detail]) => `
    <article class="validation-card">
      <header>
        <strong>端口 ${port}</strong>
        <span class="mono">${escapeHtml(detail.exit_ip || "-")}</span>
        <button data-remove-port="${port}">移除映射</button>
      </header>
      <div class="validation-chips">${(detail.targets || []).map((target) => `
        <span class="target-chip ${target.ok ? "ok" : "bad"}" title="${escapeHtml(target.error || target.body_preview || "")}">
          ${escapeHtml(compactUrl(target.url))}
          <b>${target.ok ? "成功" : "失败"}</b>
          <small>${escapeHtml(target.ok ? `${target.status_code || "-"} · ${target.elapsed_ms || "-"}ms` : compactCheckMessage(target.error || target.body_preview || "失败"))}</small>
        </span>
      `).join("")}</div>
    </article>
  `).join("");
  document.querySelectorAll("#validationResults [data-remove-port]").forEach((button) => {
    button.addEventListener("click", async () => {
      await removePortMapping(button.dataset.removePort);
    });
  });
}

function proxyAdminPayload(extra = {}) {
  const baseUrl = $("proxyAdminBaseUrl").value.trim();
  const token = $("proxyAdminToken").value.trim();
  if (!baseUrl) throw new Error("请填写 ProxyAdmin API 地址");
  if (!token) throw new Error("请填写 Bearer Token");
  return {
    base_url: baseUrl,
    token,
    proxy_host: $("proxyAdminHost").value.trim() || null,
    replace_from: $("proxyAdminReplaceFrom").value.trim() || null,
    replace_to: $("proxyAdminReplaceTo").value.trim() || null,
    proxy_name_prefix: $("proxyAdminNamePrefix").value.trim() || "代理",
    concurrency: Number($("proxyAdminConcurrency").value || 10),
    ...extra
  };
}

function renderProxyAdminResults() {
  const results = Object.values(proxyAdminResults).sort((a, b) => {
    const scoreDiff = Number(b.score || 0) - Number(a.score || 0);
    if (scoreDiff) return scoreDiff;
    return String(a.id).localeCompare(String(b.id));
  });
  $("proxyAdminSummary").textContent = results.length
    ? `已返回 ${results.length}/${proxyAdminImported.filter((item) => item.id).length}`
    : proxyAdminImported.length
      ? `已导入 ${proxyAdminImported.length} 个，等待检测`
      : "未检测";
  if (!proxyAdminImported.length && !results.length) {
    $("proxyAdminResults").innerHTML = "";
    return;
  }
  const failed = results.filter((result) => result.grade === "ERR" || (result.items || []).some((item) => item.status === "fail")).length;
  const passed = results.length - failed;
  $("proxyAdminResults").innerHTML = `
    <div class="proxy-admin-compact">
      <span class="badge ok">通过 ${passed}</span>
      <span class="badge bad">失败 ${failed}</span>
      <span class="muted">详细结果已合并到下方端口表对应行。</span>
    </div>
  `;
}

async function pollProxyAdminJob(jobId) {
  activeProxyAdminJobId = jobId;
  setCancelButton("proxyAdminCancelBtn", true);
  for (;;) {
    const job = await request(`/api/proxy-admin/jobs/${jobId}`);
    proxyAdminImported = job.imported || proxyAdminImported;
    proxyAdminResults = job.results || proxyAdminResults;
    renderProxyAdminResults();
    renderPortsTable();
    if (job.status === "done") {
      showQuickResult("ProxyAdmin 检测", `完成 ${job.completed}/${job.total}`, true);
      activeProxyAdminJobId = null;
      setCancelButton("proxyAdminCancelBtn", false);
      return;
    }
    if (job.status === "canceled") {
      showQuickResult("ProxyAdmin 检测", `已取消，完成 ${job.completed}/${job.total}`, false);
      activeProxyAdminJobId = null;
      setCancelButton("proxyAdminCancelBtn", false);
      return;
    }
    if (job.status === "error") {
      showQuickResult("ProxyAdmin 检测", job.error || "检测失败", false);
      activeProxyAdminJobId = null;
      setCancelButton("proxyAdminCancelBtn", false);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
}


function shortAssetVersion(version) {
  const text = String(version || "");
  const withoutDate = text.replace(/^\d{8}-/, "");
  return withoutDate || text || "-";
}

async function refresh() {
  await loadProxyAdminConfig();
  const [status, nodeData, portData] = await Promise.all([
    request("/api/status"),
    request("/api/nodes"),
    request("/api/ports")
  ]);
  statusSnapshot = status;
  nodes = nodeData.nodes;
  ports = portData.ports;
  localProxyCheckResults = portData.local_proxy_checks || {};
  renderSummary();
  renderSubscription(status.subscription);
  renderNodeTable();
  renderAssignTable();
  renderLocalProxyCheckPorts();
  renderPortsTable();
  
}

async function loadProxyAdminConfig() {
  if (proxyAdminConfigLoaded) return;
  proxyAdminConfigLoaded = true;
  try {
    const config = await request("/api/proxy-admin/config");
    $("proxyAdminBaseUrl").value = config.base_url || "";
    $("proxyAdminToken").value = config.token || "";
    $("proxyAdminHost").value = config.proxy_host || "";
    $("proxyAdminReplaceFrom").value = config.replace_from || "127.0.0.1";
    $("proxyAdminReplaceTo").value = config.replace_to || "";
    $("proxyAdminNamePrefix").value = config.proxy_name_prefix || "代理";
    $("proxyAdminConcurrency").value = config.concurrency || 10;
  } catch {
    proxyAdminConfigLoaded = false;
  }
}

async function saveProxyAdminConfig() {
  const payload = proxyAdminPayload();
  const config = {
    base_url: payload.base_url,
    token: payload.token,
    proxy_host: payload.proxy_host || "",
    replace_from: payload.replace_from || "127.0.0.1",
    replace_to: payload.replace_to || "",
    proxy_name_prefix: payload.proxy_name_prefix || "代理",
    concurrency: payload.concurrency || 10
  };
  await request("/api/proxy-admin/config", {
    method: "PUT",
    body: JSON.stringify(config)
  });
}

async function refreshStatusOnly() {
  statusSnapshot = await request("/api/status");
  renderSummary();
}

async function runTask(label, task) {
  showNotice(`${label}...`);
  try {
    const message = await task();
    showNotice(message || `${label}完成`, "ok");
  } catch (error) {
    showNotice(error.message, "bad");
  }
}

async function confirmTask(label, message, task) {
  if (!confirm(`${message}\n\n确认执行「${label}」？`)) return;
  await runTask(label, task);
}

async function runNodeTest(pruneSameIp, overrideTags = null) {
  const tags = overrideTags || selectedNodeTags();
  if (!tags.length) {
    showNotice("没有需要测速的节点", "ok");
    return;
  }
  const targetUrls = selectedNodeTestUrls();
  testingTags = new Set(tags);
  renderNodeTable();
  showNotice(targetUrls?.length ? `已开始测速目标：${targetUrls.join("，")}` : `已开始测速默认目标：${DEFAULT_VALIDATION_URLS[0]}`);
  try {
    const started = await request("/api/test/start", {
      method: "POST",
      body: JSON.stringify({ node_tags: tags, prune_same_ip: pruneSameIp, include_geoip: Boolean($("nodeTestGeo")?.checked), target_urls: targetUrls })
    });
    activeNodeTestJobId = started.id;
    setCancelButton("cancelNodeTestBtn", true);
    await pollTestJob(started.id);
  } catch (error) {
    activeNodeTestJobId = null;
    setCancelButton("cancelNodeTestBtn", false);
    testingTags.clear();
    renderNodeTable();
    showNotice(error.message, "bad");
  }
}

function selectedNodeTestUrls() {
  const urls = Array.from(document.querySelectorAll(".node-target-check:checked"))
    .map((option) => option.value)
    .filter(Boolean);
  const custom = $("nodeTestUrl").value.trim();
  if (custom) urls.push(custom);
  return urls.length ? Array.from(new Set(urls)) : null;
}

async function pollTestJob(jobId) {
  while (true) {
    const job = await request(`/api/test/jobs/${jobId}`);
    Object.entries(job.results || {}).forEach(([tag, result]) => {
      const node = nodes.find((item) => item.tag === tag);
      if (node) node.latency = result;
      testingTags.delete(tag);
    });
    renderNodeTable();
    renderAssignTable();
    showNotice(`测速进度：${job.completed}/${job.total}`);
    if (job.status === "done") {
      testingTags.clear();
      activeNodeTestJobId = null;
      setCancelButton("cancelNodeTestBtn", false);
      await refresh();
      const removed = job.removed?.length ? `；已去重：${job.removed.join("；")}` : "";
      showNotice(`测速完成：${job.completed}/${job.total}${removed}`, "ok");
      return;
    }
    if (job.status === "canceled") {
      testingTags.clear();
      activeNodeTestJobId = null;
      setCancelButton("cancelNodeTestBtn", false);
      renderNodeTable();
      showNotice(`测速已取消：${job.completed}/${job.total}`, "bad");
      return;
    }
    if (job.status === "error") {
      testingTags.clear();
      activeNodeTestJobId = null;
      setCancelButton("cancelNodeTestBtn", false);
      renderNodeTable();
      showNotice(job.error || "测速失败", "bad");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

function selectedNodeTags() {
  return Array.from(document.querySelectorAll(".node-check"))
    .filter((box) => box.checked)
    .map((box) => box.dataset.tag);
}

function collectMappings() {
  const entries = [];
  document.querySelectorAll(".assign-check").forEach((box) => {
    if (!box.checked) return;
    const tag = box.dataset.tag;
    const port = document.querySelector(`[data-port-for="${CSS.escape(tag)}"]`).value;
    if (port) entries.push([String(port), tag]);
  });
  const mappings = {};
  entries
    .sort(([leftPort], [rightPort]) => Number(leftPort) - Number(rightPort))
    .forEach(([port, tag]) => {
      mappings[port] = tag;
    });
  return mappings;
}

async function saveMappings(mappings) {
  return request("/api/assign", { method: "PUT", body: JSON.stringify({ mappings }) });
}

function compactPortMappings(startPort) {
  const entries = Object.entries(ports)
    .sort(([leftPort], [rightPort]) => Number(leftPort) - Number(rightPort));
  const mappings = {};
  entries.forEach(([, item], index) => {
    mappings[String(startPort + index)] = item.node_tag;
  });
  return mappings;
}

async function assertCompactPortsAvailable(mappings) {
  const currentPorts = new Set(Object.keys(ports).map(String));
  const targetPorts = Object.keys(mappings).map(Number);
  const checked = await request("/api/ports/check", {
    method: "POST",
    body: JSON.stringify({ ports: targetPorts })
  });
  const checkedPorts = Object.entries(checked.ports || {}).map(([port, item]) => ({
    port: Number(port),
    ...item
  }));
  const blocked = checkedPorts.filter((item) => {
    const port = String(item.port);
    if (item.available) return false;
    return !(currentPorts.has(port) && item.reason === "project-listening");
  });
  if (blocked.length) {
    const detail = blocked
      .slice(0, 6)
      .map((item) => `${item.port} ${item.label || item.reason || "不可用"}`)
      .join("，");
    throw new Error(`目标端口不可用：${detail}`);
  }
}

async function removePortMapping(port) {
  await runTask(`移除端口 ${port} 映射`, async () => {
    const mappings = {};
    Object.entries(ports).forEach(([existingPort, item]) => {
      if (String(existingPort) !== String(port)) mappings[existingPort] = item.node_tag;
    });
    const result = await saveMappings(mappings);
    delete ports[String(port)];
    delete validationDetails[String(port)];
    validatingPorts.delete(String(port));
    await refresh();
    if (result.engine_restarted) return `端口 ${port} 已移除，已自动重启引擎`;
    if (result.engine_stopped) return `端口 ${port} 已移除，已停止引擎`;
    return `端口 ${port} 已移除`;
  });
}

function validationUrls() {
  const custom = $("customTestUrl").value.trim();
  if (custom) return [custom];
  return DEFAULT_VALIDATION_URLS;
}

function activeValidationUrl() {
  return $("customTestUrl").value.trim() || DEFAULT_VALIDATION_URLS[0];
}

async function runSinglePortValidation(port) {
  showQuickResult(`端口 ${port}`, "验证中...");
  validatingPorts = new Set([String(port)]);
  renderPortsTable();
  try {
    const result = await request("/api/test-ports", {
      method: "POST",
      body: JSON.stringify({ ports: [port], urls: validationUrls() })
    });
    applyPortValidationResults(result);
    const detail = result.details?.[String(port)];
    const okCount = (detail?.targets || []).filter((item) => item.ok).length;
    const total = (detail?.targets || []).length;
    const exitIp = detail?.exit_ip || "-";
    showQuickResult(`端口 ${port}`, `出口 IP：${exitIp}；目标成功 ${okCount}/${total}`, okCount > 0);
    validatingPorts.delete(String(port));
    renderPortsTable();
    renderValidationResults();
  } catch (error) {
    validatingPorts.delete(String(port));
    renderPortsTable();
    showQuickResult(`端口 ${port}`, error.message, false);
  }
}

function applyPortValidationResults(result) {
  validationDetails = { ...validationDetails, ...(result.details || {}) };
  Object.entries(result.results || {}).forEach(([tag, latency]) => {
    const entry = Object.values(ports).find((item) => item.node_tag === tag);
    if (entry) entry.latency = latency;
  });
  Object.entries(result.details || {}).forEach(([port, detail]) => {
    validatingPorts.delete(String(port));
    if (ports[port] && detail.exit_ip) ports[port].exit_ip = detail.exit_ip;
    if (ports[port] && detail.geoip) ports[port].geoip = detail.geoip;
  });
}

async function runAllPortValidation() {
  setPortValidationRunning(true);
  try {
    validationDetails = {};
    renderValidationResults();
    validatingPorts = new Set(Object.keys(ports));
    renderPortsTable();
    const urls = validationUrls();
    showQuickResult("验证全部端口", `验证中；目标：${urls.join("，")}；进度 0/${Object.keys(ports).length}`);
    const started = await request("/api/test-ports/start", {
      method: "POST",
      body: JSON.stringify({ urls })
    });
    activePortValidationJobId = started.id;
    setCancelButton("cancelPortValidationBtn", true);
    await pollPortTestJob(started.id, urls);
  } finally {
    activePortValidationJobId = null;
    setCancelButton("cancelPortValidationBtn", false);
    setPortValidationRunning(false);
  }
}

async function pollPortTestJob(jobId, urls) {
  while (true) {
    const job = await request(`/api/test-ports/jobs/${jobId}`);
    applyPortValidationResults(job);
    renderPortsTable();
    renderValidationResults();
    showQuickResult("验证全部端口", `验证中；目标：${urls.join("，")}；进度 ${job.completed}/${job.total}`);
    if (job.status === "done") {
      validatingPorts.clear();
      const details = Object.values(validationDetails);
      const total = job.total || details.length;
      const ok = details.filter((detail) => (detail.targets || []).some((target) => target.ok)).length;
      renderPortsTable();
      renderValidationResults();
      showQuickResult("验证全部端口", `可用 ${ok}/${total}；目标：${urls.join("，")}`, ok > 0);
      return;
    }
    if (job.status === "canceled") {
      validatingPorts.clear();
      renderPortsTable();
      renderValidationResults();
      showQuickResult("验证全部端口", `已取消；完成 ${job.completed}/${job.total}`, false);
      return;
    }
    if (job.status === "error") {
      validatingPorts.clear();
      renderPortsTable();
      showQuickResult("验证全部端口", job.error || "验证失败", false);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
}

function setPortValidationRunning(isRunning) {
  const button = $("testPortsBtn");
  button.disabled = isRunning;
  button.textContent = isRunning ? "验证中..." : "验证全部端口";
}

function exportRows() {
  const target = activeValidationUrl();
  return Object.entries(ports).map(([port, item]) => ({
    host: proxyConnectHost(),
    port,
    node: item.node_name || item.node_tag,
    type: item.type || "",
    exit_ip: item.exit_ip || item.latency?.exit_ip || "",
    geoip: geoIpText(item),
    http_proxy: `http://${proxyAuthority(port)}`,
    socks5_proxy: `socks5://${proxyAuthority(port)}`,
    socks5h_proxy: `socks5h://${proxyAuthority(port)}`,
    curl: `curl --proxy "http://${proxyAuthority(port)}/" ${target}`
  }));
}

function generateExport() {
  const rows = exportRows();
  const format = $("exportFormat").value;
  if (!rows.length) throw new Error("没有可导出的端口映射");
  if (format === "json") return JSON.stringify(rows, null, 2);
  if (format === "curl") return rows.map((row) => row.curl).join("\n");
  if (format === "socks-list") return rows.map((row) => row.socks5_proxy).join("\n");
  if (format === "mixed-list") {
    return rows.flatMap((row) => [row.http_proxy, row.socks5_proxy]).join("\n");
  }
  if (format === "csv") {
    const header = ["host", "port", "node", "type", "exit_ip", "geoip", "http_proxy", "socks5_proxy", "socks5h_proxy"];
    const lines = rows.map((row) => header.map((key) => `"${String(row[key]).replace(/"/g, '""')}"`).join(","));
    return [header.join(","), ...lines].join("\n");
  }
  return rows.map((row) => row.http_proxy).join("\n");
}

function activateTab(tabId, persist = true) {
  // ponytail: import merged into nodes (test); keep old localStorage keys working
  if (tabId === "import") tabId = "test";
  const button = document.querySelector(`.tab[data-tab="${CSS.escape(tabId)}"]`);
  const panel = $(tabId);
  if (!button || !panel) return false;
  document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  panel.classList.add("active");
  if (persist) localStorage.setItem(ACTIVE_TAB_KEY, tabId);
  return true;
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    activateTab(button.dataset.tab);
  });
});

activateTab(localStorage.getItem(ACTIVE_TAB_KEY) || "test", false);

document.querySelectorAll(".node-target-check").forEach((item) => {
  item.addEventListener("change", renderNodeTestOverview);
});

$("nodeTestUrl").addEventListener("input", renderNodeTestOverview);

$("nodeTestGeo").addEventListener("change", renderNodeTestOverview);

$("importUrlBtn").addEventListener("click", () => runTask("导入订阅", async () => {
  await request("/api/import", { method: "POST", body: JSON.stringify({ url: $("urlInput").value }) });
  await refresh();
}));

$("saveSubscriptionBtn").addEventListener("click", () => runTask("保存订阅", async () => {
  const result = await request("/api/subscription", {
    method: "PUT",
    body: JSON.stringify({
      url: $("urlInput").value,
      refresh_interval_minutes: Number($("subscriptionInterval").value || 0)
    })
  });
  renderSubscription(result);
  return "订阅设置已保存";
}));

$("refreshSubscriptionBtn").addEventListener("click", () => runTask("刷新订阅", async () => {
  const result = await request("/api/subscription/refresh", {
    method: "POST",
    body: "{}"
  });
  renderSubscription(result);
  await refresh();
  return `订阅刷新完成：新增 ${result.added || 0}，更新 ${result.updated || 0}，总节点 ${result.total_nodes || 0}`;
}));

$("importTextBtn").addEventListener("click", () => runTask("解析文本", async () => {
  await request("/api/import", { method: "POST", body: JSON.stringify({ text: $("textInput").value }) });
  await refresh();
}));

$("testSelectedBtn").addEventListener("click", () => runNodeTest(false));

$("testUntestedBtn").addEventListener("click", () => {
  const tags = nodes.filter((node) => !node.latency).map((node) => node.tag);
  if (!tags.length) {
    showNotice("没有未测速节点", "ok");
    return;
  }
  runNodeTest(false, tags);
});

$("testFailedOnlyBtn").addEventListener("click", () => {
  const tags = nodes.filter((node) => node.latency && !node.latency.alive).map((node) => node.tag);
  if (!tags.length) {
    showNotice("没有测试失败节点", "ok");
    return;
  }
  runNodeTest(false, tags);
});

$("testPruneBtn").addEventListener("click", () => runNodeTest(true));

$("selectAliveBtn").addEventListener("click", () => {
  const aliveTags = nodes.filter((node) => node.latency?.alive).map((node) => node.tag);
  document.querySelectorAll(".node-check").forEach((box) => {
    const node = nodes.find((item) => item.tag === box.dataset.tag);
    box.checked = Boolean(node?.latency?.alive);
  });
  assignFilterTags = new Set(aliveTags);
  autoSelectAliveForAssign = true;
  renderAssignTable();
  activateTab("assign");
  showNotice(`已筛选可用节点：${aliveTags.length} 个`, aliveTags.length ? "ok" : "bad");
});

let _nodeSearchTimer = null;
$("nodeSearchInput").addEventListener("input", (event) => {
  clearTimeout(_nodeSearchTimer);
  _nodeSearchTimer = setTimeout(() => {
    nodeSearchQuery = event.target.value;
    renderNodeTable();
  }, 200);
});

document.querySelectorAll(".node-filter-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".node-filter-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    nodeStatusFilter = tab.dataset.filter;
    renderNodeTable();
  });
});

// Event delegation for node table — avoids re-attaching listeners on every render
$("nodeTable").addEventListener("change", (event) => {
  const target = event.target;
  if (target.id === "checkAllNodes") {
    document.querySelectorAll(".node-check").forEach((box) => {
      box.checked = target.checked;
    });
  }
});

$("deleteSelectedBtn").addEventListener("click", () => confirmTask("删除选中节点", `将删除 ${selectedNodeTags().length} 个选中节点，此操作不可撤销。`, async () => {
  const tags = selectedNodeTags();
  if (!tags.length) throw new Error("没有选中节点");
  await request("/api/nodes/delete", {
    method: "POST",
    body: JSON.stringify({ node_tags: tags })
  });
  await refresh();
}));

$("deleteFailedBtn").addEventListener("click", () => confirmTask("删除测试失败节点", `将删除所有测试失败的节点，此操作不可撤销。`, async () => {
  const tags = nodes.filter((node) => node.latency && !node.latency.alive).map((node) => node.tag);
  if (!tags.length) throw new Error("没有测试失败节点");
  await request("/api/nodes/delete", {
    method: "POST",
    body: JSON.stringify({ node_tags: tags })
  });
  await refresh();
}));

$("clearNodesBtn").addEventListener("click", () => confirmTask("清空节点", `将删除所有已导入的节点（共 ${nodes.length} 个），此操作不可撤销。`, async () => {
  await request("/api/nodes/delete", {
    method: "POST",
    body: JSON.stringify({ all: true })
  });
  await refresh();
}));

$("autoAssignBtn").addEventListener("click", () => runTask("自动分配可用端口", async () => {
  let nextPort = Number($("startPort").value || 9001);
  const usedPorts = new Set();
  document.querySelectorAll(".port-input").forEach((input) => {
    if (input.value) usedPorts.add(Number(input.value));
  });
  let targets = [...document.querySelectorAll(".assign-check")].filter((box) => {
    if (!box.checked) return false;
    const input = document.querySelector(`[data-port-for="${CSS.escape(box.dataset.tag)}"]`);
    return !input.value;
  });
  if (!targets.length) {
    const visibleBoxes = [...document.querySelectorAll(".assign-check")];
    const aliveBoxes = visibleBoxes.filter((box) => {
      const node = nodes.find((item) => item.tag === box.dataset.tag);
      return Boolean(node?.latency?.alive);
    });
    targets = aliveBoxes.length ? aliveBoxes : visibleBoxes;
    targets.forEach((box) => {
      const input = document.querySelector(`[data-port-for="${CSS.escape(box.dataset.tag)}"]`);
      if (!input.value) box.checked = true;
    });
    targets = targets.filter((box) => {
      const input = document.querySelector(`[data-port-for="${CSS.escape(box.dataset.tag)}"]`);
      return !input.value;
    });
  }
  if (!targets.length) return "没有需要分配的新节点";
  const allocation = await request("/api/ports/allocate", {
    method: "POST",
    body: JSON.stringify({
      start_port: nextPort,
      count: targets.length,
      exclude: [...usedPorts]
    })
  });
  targets.forEach((box, index) => {
    const input = document.querySelector(`[data-port-for="${CSS.escape(box.dataset.tag)}"]`);
    input.value = allocation.ports[index];
    usedPorts.add(allocation.ports[index]);
  });
  const skippedCount = Object.keys(allocation.skipped || {}).length;
  return `已分配 ${targets.length} 个可用端口${skippedCount ? `，已避让 ${skippedCount} 个不可用端口` : ""}`;
}));

$("compactPortsBtn").addEventListener("click", () => runTask("重排端口", async () => {
  const entries = Object.entries(ports);
  if (!entries.length) throw new Error("没有可重排的端口映射");
  const startPort = Number($("startPort").value || 8001);
  if (!Number.isInteger(startPort) || startPort < 1024 || startPort > 65535) {
    throw new Error("起始端口无效");
  }
  const endPort = startPort + entries.length - 1;
  if (endPort > 65535) throw new Error(`端口范围超过 65535：${startPort}-${endPort}`);
  const mappings = compactPortMappings(startPort);
  const before = Object.keys(ports).sort((left, right) => Number(left) - Number(right)).join(",");
  const after = Object.keys(mappings).sort((left, right) => Number(left) - Number(right)).join(",");
  if (before === after) return `端口已经集中：${after}`;
  await assertCompactPortsAvailable(mappings);
  const result = await saveMappings(mappings);
  validationDetails = {};
  validatingPorts.clear();
  localProxyCheckResults = {};
  await refresh();
  if (result.engine_restarted) return `端口已重排为 ${after}，已自动重启引擎`;
  if (result.engine_stopped) return `端口已重排为 ${after}，已停止引擎`;
  return `端口已重排为 ${after}`;
}));

$("clearAssignBtn").addEventListener("click", () => confirmTask("清空端口分配", `将清空所有端口映射（共 ${Object.keys(ports).length} 个），此操作不可撤销。`, async () => {
  autoSelectAliveForAssign = false;
  assignFilterTags = null;
  document.querySelectorAll(".assign-check").forEach((box) => {
    box.checked = false;
  });
  document.querySelectorAll(".port-input").forEach((input) => {
    input.value = "";
  });
  await request("/api/assign", { method: "PUT", body: JSON.stringify({ mappings: {} }) });
  ports = {};
  validationDetails = {};
  validatingPorts.clear();
  renderAssignTable();
  renderPortsTable();
  renderValidationResults();
  await refreshStatusOnly();
  return "端口分配已清空";
}));

$("saveAssignBtn").addEventListener("click", () => runTask("保存端口映射", async () => {
  const result = await saveMappings(collectMappings());
  await refresh();
  if (result.engine_restarted) return "保存端口映射完成，已自动重启引擎";
  if (result.engine_stopped) return "保存端口映射完成，已停止引擎";
  return "保存端口映射完成";
}));

$("startBtn").addEventListener("click", () => runTask("启动引擎", async () => {
  await request("/api/start", { method: "POST", body: "{}" });
  await refreshStatusOnly();
}));

$("stopBtn").addEventListener("click", () => runTask("停止引擎", async () => {
  await request("/api/stop", { method: "POST", body: "{}" });
  await refreshStatusOnly();
}));

$("repairEngineBtn").addEventListener("click", () => runTask("重启修复引擎", async () => {
  await request("/api/stop", { method: "POST", body: "{}" });
  await request("/api/start", { method: "POST", body: "{}" });
  await refresh();
  const missingPorts = statusSnapshot.missing_ports || [];
  if (missingPorts.length) return `引擎已重启，仍缺失端口：${missingPorts.join(", ")}`;
  return "引擎已重启，端口监听正常";
}));

$("testPortsBtn").addEventListener("click", () => runTask("验证全部端口", runAllPortValidation));

$("cancelNodeTestBtn").addEventListener("click", () => runTask("取消测速", async () => {
  if (!activeNodeTestJobId) throw new Error("没有正在运行的节点测速任务");
  await request(`/api/test/jobs/${activeNodeTestJobId}/cancel`, { method: "POST", body: "{}" });
  return "已发送取消测速请求";
}));

$("cancelPortValidationBtn").addEventListener("click", () => runTask("取消端口验证", async () => {
  if (!activePortValidationJobId) throw new Error("没有正在运行的端口验证任务");
  await request(`/api/test-ports/jobs/${activePortValidationJobId}/cancel`, { method: "POST", body: "{}" });
  return "已发送取消端口验证请求";
}));

$("customTestUrl").addEventListener("input", () => {
  renderPortsTable();
});

$("exportBtn").addEventListener("click", () => {
  try {
    const output = generateExport();
    const box = $("exportOutput");
    box.value = output;
    box.classList.remove("hidden");
    showQuickResult("导出已生成", `${$("exportFormat").selectedOptions[0].textContent}，共 ${Object.keys(ports).length} 条`, true);
  } catch (error) {
    showQuickResult("导出失败", error.message, false);
  }
});

$("proxyAdminCheckBtn").addEventListener("click", () => runTask("ProxyAdmin 导入并检测", async () => {
  await saveProxyAdminConfig();
  proxyAdminResults = {};
  proxyAdminImported = [];
  renderProxyAdminResults();
  const started = await request("/api/proxy-admin/check/start", {
    method: "POST",
    body: JSON.stringify(proxyAdminPayload())
  });
  $("proxyAdminSummary").textContent = `任务已启动：${started.id}`;
  await pollProxyAdminJob(started.id);
  return "ProxyAdmin 检测完成";
}));

$("proxyAdminSaveConfigBtn").addEventListener("click", () => runTask("保存 ProxyAdmin 配置", async () => {
  await saveProxyAdminConfig();
  return "ProxyAdmin 配置已保存";
}));

$("proxyAdminRetryFailedBtn").addEventListener("click", () => runTask("ProxyAdmin 重试失败", async () => {
  await saveProxyAdminConfig();
  const failedIds = Object.values(proxyAdminResults)
    .filter((result) => result.grade === "ERR" || (result.items || []).some((item) => item.status === "fail"))
    .map((result) => Number(result.id))
    .filter((id) => id > 0);
  if (!failedIds.length) throw new Error("没有失败结果可重试");
  const retryPorts = proxyAdminImported
    .filter((item) => failedIds.includes(Number(item.id)))
    .map((item) => Number(item.port));
  const proxyIdsByPort = Object.fromEntries(
    proxyAdminImported
      .filter((item) => failedIds.includes(Number(item.id)))
      .map((item) => [String(item.port), Number(item.id)])
  );
  const started = await request("/api/proxy-admin/check/start", {
    method: "POST",
    body: JSON.stringify(proxyAdminPayload({
      ports: retryPorts,
      check_only: true,
      proxy_ids_by_port: proxyIdsByPort
    }))
  });
  await pollProxyAdminJob(started.id);
  return "ProxyAdmin 失败项已重试";
}));

$("proxyAdminDeleteFailedBtn").addEventListener("click", () => confirmTask("ProxyAdmin 删除失败", `将从远端 ProxyAdmin 平台删除所有检测失败的代理，此操作不可撤销。`, async () => {
  await saveProxyAdminConfig();
  const ids = Object.values(proxyAdminResults)
    .filter((result) => result.grade === "ERR" || (result.items || []).some((item) => item.status === "fail"))
    .map((result) => Number(result.id))
    .filter((id) => id > 0);
  if (!ids.length) throw new Error("没有失败节点可删除");
  const result = await request("/api/proxy-admin/remove", {
    method: "POST",
    body: JSON.stringify(proxyAdminPayload({ ids, concurrency: Number($("proxyAdminConcurrency").value || 5) }))
  });
  (result.removed || []).forEach((item) => {
    if (item.success) {
      delete proxyAdminResults[String(item.id)];
      proxyAdminImported = proxyAdminImported.filter((imported) => Number(imported.id) !== Number(item.id));
    }
  });
  renderProxyAdminResults();
  renderPortsTable();
  showQuickResult("ProxyAdmin 删除失败", `处理 ${result.count} 个`, true);
  return `ProxyAdmin 删除失败完成：${result.count} 个`;
}));

$("proxyAdminDeleteUnusedBtn").addEventListener("click", () => confirmTask("ProxyAdmin 删除未使用", `将从远端 ProxyAdmin 平台删除所有未使用的代理，此操作不可撤销。`, async () => {
  await saveProxyAdminConfig();
  const result = await request("/api/proxy-admin/remove", {
    method: "POST",
    body: JSON.stringify(proxyAdminPayload({ unused: true, concurrency: Number($("proxyAdminConcurrency").value || 5) }))
  });
  (result.removed || []).forEach((item) => {
    if (item.success) {
      delete proxyAdminResults[String(item.id)];
      proxyAdminImported = proxyAdminImported.filter((imported) => Number(imported.id) !== Number(item.id));
    }
  });
  renderProxyAdminResults();
  renderPortsTable();
  showQuickResult("ProxyAdmin 删除未使用", `处理 ${result.count} 个`, true);
  return `ProxyAdmin 删除未使用完成：${result.count} 个`;
}));

$("refreshBtn").addEventListener("click", () => runTask("刷新", refresh));

$("doctorBtn").addEventListener("click", () => runTask("系统自检", async () => {
  $("doctorSummary").textContent = "运行中";
  renderDoctorResult(null);
  $("doctorSummary").textContent = "运行中";
  const result = await request("/api/doctor", {
    method: "POST",
    body: JSON.stringify({ timeout: 30 })
  });
  renderDoctorResult(result);
  showQuickResult("系统自检", result.summary || `OK ${result.ok || 0} / WARN ${result.warn || 0} / FAIL ${result.fail || 0}`, !result.fail);
  return result.fail ? `自检发现 ${result.fail} 个失败项` : "自检完成";
}));

$("proxyAdminCancelBtn").addEventListener("click", () => runTask("取消 ProxyAdmin 检测", async () => {
  if (!activeProxyAdminJobId) throw new Error("没有正在运行的 ProxyAdmin 检测任务");
  await request(`/api/proxy-admin/jobs/${activeProxyAdminJobId}/cancel`, { method: "POST", body: "{}" });
  return "已发送取消 ProxyAdmin 检测请求";
}));

$("localProxyCheckBtn").addEventListener("click", () => runTask("本地检测", async () => {
  const port = Number($("localProxyCheckPort").value || 0);
  if (!port) throw new Error("没有可检测端口");
  renderLocalProxyCheckResult({ grade: "...", score: "-", exit_ip: "检测中", country: "" });
  const result = await runLocalProxyCheckForPort(port);
  showQuickResult(
    `本地检测 ${port}`,
    `${result.grade || "-"} · ${result.score ?? "-"} · ${result.exit_ip || "-"} · ${result.country || result.country_code || "-"}`,
    !localProxyCheckFailed(result)
  );
  return `本地检测完成：${port}`;
}));

$("localProxyCheckAllBtn").addEventListener("click", () => runTask("全部检测", async () => {
  const portList = Object.keys(ports).sort((left, right) => Number(left) - Number(right));
  if (!portList.length) throw new Error("没有可检测端口");
  renderLocalProxyCheckResult({ grade: "...", score: "-", exit_ip: "批量检测中", country: "" });
  const result = await runLocalProxyCheckForPorts(portList, 3);
  showQuickResult("一键本地检测", `通过 ${result.passed}，失败 ${result.failed}`, result.failed === 0);
  return `一键本地检测完成：通过 ${result.passed}，失败 ${result.failed}`;
}));

$("cancelLocalProxyCheckBtn").addEventListener("click", () => runTask("取消本地检测", async () => {
  if (!activeLocalProxyCheckJobId) throw new Error("没有正在运行的本地检测任务");
  await request(`/api/proxy-check/jobs/${activeLocalProxyCheckJobId}/cancel`, { method: "POST", body: "{}" });
  return "已发送取消本地检测请求";
}));

$("authForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("adminKeyInput");
  const error = $("authError");
  if (error) error.textContent = "";
  try {
    await loginWithAdminKey(input?.value || "");
    if (input) input.value = "";
  } catch (exc) {
    if (error) error.textContent = exc.message || "登录失败";
  }
});

async function bootstrap() {
  try {
    const ready = await loadAuthState();
    if (ready) await refresh();
  } catch (error) {
    document.body.classList.remove("auth-pending");
    throw error;
  }
}

bootstrap().catch((error) => showNotice(error.message, "bad"));
setInterval(() => {
  if (document.hidden) return;
  if (authState.enabled && !authState.authenticated) return;
  refreshStatusOnly().catch(() => {});
}, 15000);
