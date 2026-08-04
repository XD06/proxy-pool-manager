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
let singBoxInfoLoaded = false;
let localProxyCheckResults = {};
let localProxyCheckingPorts = new Set();
let activeNodeTestJobId = null;
let nodeTestProgress = null;
let nodeTestRevision = 0;
let activePortValidationJobId = null;
let activeLocalProxyCheckJobId = null;
let pools = [];
let trafficRange = localStorage.getItem("proxyPoolManager.trafficRange") || "24h";
let trafficEntityType = "port";
let trafficSearchQuery = "";
let poolEditorMembers = new Map();
let poolMemberSearchQuery = "";
let nodeGroups = [];
let subscriptionSources = [];
let nodePageItems = [];
let nodePagination = { page: 1, page_size: 50, total: 0, total_pages: 1 };
let activeNodeGroupId = "";
let nodeWorkspaceView = "nodes";
let selectedNodePageTags = new Set();
let trafficDisplayLimit = 10;
let trafficEntities = [];
let _trafficReqSeq = 0;
const ACTIVE_TAB_KEY = "proxyPoolManager.activeTab";
let authState = { enabled: false, authenticated: true };
let portAvailabilityCache = {};
let _portCheckTimer = null;

const DEFAULT_VALIDATION_URLS = [
  "https://www.google.com/generate_204",
  "https://www.gstatic.com/generate_204",
  "http://cp.cloudflare.com/generate_204",
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

// Job polling helpers: a transient network hiccup must not abort progress
// tracking of a running job. fetch rejects with TypeError on network errors,
// while HTTP errors surface as plain Error and should fail fast.
async function requestJobWithRetry(path, maxAttempts = 6) {
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await request(path);
    } catch (error) {
      if (!(error instanceof TypeError) || attempt >= maxAttempts) throw error;
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * attempt, 5000)));
    }
  }
}

function pollDelay(ms) {
  // Hidden tabs poll slower to save server round-trips.
  return new Promise((resolve) => setTimeout(resolve, document.hidden ? Math.max(ms * 4, 3000) : ms));
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
    notice.removeAttribute("title");
    notice.removeAttribute("data-tone");
    return;
  }
  const visible = !notice.classList.contains("hidden");
  notice.className = `notice ${tone}${visible ? "" : " hidden"}`;
  notice.textContent = tone === "bad" ? compactCheckMessage(message) : message;
  notice.title = String(message || "");
  notice.dataset.tone = tone;
  if (!visible) requestAnimationFrame(() => notice.classList.remove("hidden"));
  // ponytail: auto-dismiss; errors get more time to read
  const ms = tone === "bad" ? 6000 : 3500;
  _noticeTimer = setTimeout(() => {
    if (notice.textContent) {
      showNotice("");
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

function nodeHealthCellHtml(node) {
  return `<div>${nodeStatusBadge(node)}</div>${latencyBarHtml(node.latency)}`;
}

function nodeEgressCellHtml(node) {
  return `<span class="mono">${escapeHtml(node.latency?.exit_ip || "-")}</span>${geoChipHtml(geoContextForNode(node))}`;
}

function nodeTargetResultCellHtml(node) {
  return escapeHtml(targetResponsePreview(node.latency));
}

function updateNodeLatency(tag, result) {
  const node = nodes.find((item) => item.tag === tag);
  if (node) node.latency = result;
  const visibleNode = nodePageItems.find((item) => item.tag === tag);
  if (visibleNode) visibleNode.latency = result;
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
  const targetText = targetUrls?.length
    ? targetUrls.map(compactUrl).join(" / ")
    : "Google 204";
  const geoText = $("nodeTestGeo")?.checked ? "查出口/地区" : "只测速";
  const alivePct = stats.total ? Math.round((stats.alive / stats.total) * 100) : 0;
  box.innerHTML = `
    <div class="node-health-line">
      <div class="node-health-primary"><strong>${stats.total}</strong><span>节点</span></div>
      <div><b class="ok">${stats.alive}</b><span>可用</span></div>
      <div><b class="bad">${stats.failed}</b><span>失败</span></div>
      <div><b>${stats.untested}</b><span>未测</span></div>
      ${stats.testing ? `<div><b class="warn">${stats.testing}</b><span>测速中</span></div>` : ""}
      ${nodeTestProgress ? `<div><b>${nodeTestProgress.completed}/${nodeTestProgress.total}</b><span>进度</span></div>` : ""}
      <div class="node-health-quality"><b>${alivePct}%</b><span>可用率</span>${stats.avgDelay === null ? "" : `<i>平均 ${stats.avgDelay}ms</i>`}</div>
      <small>${escapeHtml(targetText)} · ${geoText}</small>
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
  const engineToggle = $("engineToggleBtn");
  const engineRunning = Boolean(statusSnapshot.running);
  engineToggle.classList.toggle("on", engineRunning);
  engineToggle.setAttribute("aria-checked", String(engineRunning));
  engineToggle.setAttribute("aria-label", engineRunning ? "暂停 sing-box" : "启动 sing-box");
  $("nodeCount").textContent = String(statusSnapshot.node_count ?? nodes.length);
  $("mappingCount").textContent = String(statusSnapshot.mapping_count ?? Object.keys(ports).length);
  $("listeningCount").textContent = `${listeningPorts.length}/${expectedPorts.length}`;
  setTone("nodeCount", tone);
  setTone("mappingCount", tone);
  setTone("listeningCount", missingPorts.length ? "bad" : tone);
  const failedListeners = statusSnapshot.failed_listeners || [];
  if (failedListeners.length) {
    const detail = failedListeners
      .map((item) => {
        const port = String(item.listen || "").split(":").pop();
        return `${port}（${item.error || "不可用"}）`;
      })
      .join("，");
    $("listeningCount").title = `端口监听 ${listeningPorts.length}/${expectedPorts.length}，被跳过：${detail}`;
  } else if (missingPorts.length) {
    $("listeningCount").title = `异常端口：${missingPorts.join(", ")}`;
  } else {
    $("listeningCount").title = "所有映射端口均在监听";
  }
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
  const failedListeners = statusSnapshot.failed_listeners || [];
  if (failedListeners.length) {
    const skippedText = failedListeners.map((item) => {
      const port = String(item.listen || "").split(":").pop();
      return `${port}`;
    }).join(", ");
    bits.push(`<span class="eh-issue" title="${escapeHtml(skippedText)}">跳过 ${failedListeners.length}</span>`);
  }
  const lastError = statusSnapshot.last_error || (statusSnapshot.pool_router || {}).last_error;
  if (lastError) {
    bits.push(`<span class="eh-issue" title="${escapeHtml(lastError)}">错误</span>`);
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
  summary.textContent = result.timed_out ? "超时" : tone === "ok" ? "通过" : tone === "warn" ? "有警告" : "需处理";
  summary.className = `tool-state ${tone === "ok" ? "ok" : tone === "warn" ? "warn" : "bad"}`;
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

function groupKindLabel(kind) {
  return ({ subscription: "订阅", import_batch: "导入批次", manual: "手动", quality_snapshot: "质量快照" })[kind] || "分组";
}

function groupHealthText(group) {
  const tested = Number(group.healthy_count || 0) + Number(group.failed_count || 0);
  const availability = tested ? `${Math.round((Number(group.healthy_count || 0) / tested) * 100)}% 可用` : "未测速";
  const delay = group.average_delay === null || group.average_delay === undefined ? "—" : `${group.average_delay}ms`;
  return `${availability} · ${delay}`;
}

function renderNodeGroups() {
  const target = $("nodeGroupList");
  if (!target) return;
  $("nodeGroupTotal").textContent = String(nodes.length);
  target.innerHTML = nodeGroups.length ? nodeGroups.map((group) => `
    <details class="node-group-item" ${group.id === activeNodeGroupId ? "open" : ""}>
      <summary><span><b>${escapeHtml(group.name)}</b><small>${groupKindLabel(group.kind)} · ${group.node_count}</small></span><i>${escapeHtml(groupHealthText(group))}</i></summary>
      <div class="node-group-item-body"><span>${group.failed_count ? `${group.failed_count} 失败` : "无失败记录"}${group.pool_count ? ` · ${group.pool_count} 个节点池` : ""}</span><div><button data-node-group-open="${escapeHtml(group.id)}">查看</button>${group.kind !== "subscription" ? `<button data-node-group-delete="${escapeHtml(group.id)}">删除</button>` : ""}</div></div>
    </details>`).join("") : `<div class="node-group-empty">导入或选择节点后可建立工作集。</div>`;
}

function renderNodePagination() {
  const target = $("nodePagination");
  if (!target) return;
  if (nodeWorkspaceView !== "nodes" || nodePagination.total <= nodePagination.page_size) {
    target.innerHTML = "";
    return;
  }
  const start = (nodePagination.page - 1) * nodePagination.page_size + 1;
  const end = Math.min(nodePagination.total, nodePagination.page * nodePagination.page_size);
  target.innerHTML = `<span>${start}–${end} / ${nodePagination.total}</span><div><button data-node-page="${nodePagination.page - 1}" ${nodePagination.page <= 1 ? "disabled" : ""} aria-label="上一页">上一页</button><strong>${nodePagination.page} / ${nodePagination.total_pages}</strong><button data-node-page="${nodePagination.page + 1}" ${nodePagination.page >= nodePagination.total_pages ? "disabled" : ""} aria-label="下一页">下一页</button></div>`;
}

function renderGroupManager() {
  const target = $("nodeTable");
  target.innerHTML = nodeGroups.length ? `<div class="group-manager-list">${nodeGroups.map((group) => `
    <details class="group-manager-row" open>
      <summary><div><strong>${escapeHtml(group.name)}</strong><span>${groupKindLabel(group.kind)} · ${group.node_count} 个节点</span></div><div><b>${group.healthy_count}</b><span>可用</span></div><div><b>${group.failed_count}</b><span>失败</span></div><div><b>${group.average_delay === null ? "—" : `${group.average_delay}ms`}</b><span>平均延迟</span></div></summary>
      <div class="group-manager-detail"><span>创建于 ${group.created_at ? new Date(group.created_at).toLocaleDateString() : "—"}</span><span>${group.pool_count ? `已被 ${group.pool_count} 个节点池引用` : "尚未加入节点池"}</span><div><button data-node-group-open="${escapeHtml(group.id)}">打开节点</button><button data-node-group-assign="${escapeHtml(group.id)}">分配端口</button>${group.kind !== "subscription" ? `<button data-node-group-delete="${escapeHtml(group.id)}">删除分组</button>` : ""}</div></div>
    </details>`).join("")}</div>` : `<div class="empty"><strong>还没有分组</strong><span>订阅和每次粘贴导入会自动形成分组；也可以从当前选择建立手动分组。</span></div>`;
}

function renderSubscriptionSources() {
  const target = $("nodeTable");
  target.innerHTML = subscriptionSources.length ? `<div class="source-list">${subscriptionSources.map((source) => {
    const next = source.next_refresh_in_seconds === null || source.next_refresh_in_seconds === undefined ? "手动刷新" : `${Math.ceil(source.next_refresh_in_seconds / 60)} 分钟后`;
    const status = source.last_error ? "失败" : source.last_refresh_at ? "正常" : "未刷新";
    return `<article class="source-row"><div><strong>${escapeHtml(source.name)}</strong><span class="mono" title="${escapeHtml(source.url)}">${escapeHtml(source.url)}</span></div><div><b>${source.last_count || 0}</b><span>最近节点</span></div><div><b>${source.refresh_interval_minutes ? `${source.refresh_interval_minutes}m` : "—"}</b><span>${escapeHtml(next)}</span></div><div><span class="badge ${source.last_error ? "bad" : source.last_refresh_at ? "ok" : "idle"}">${status}</span><button data-source-refresh="${escapeHtml(source.id)}">刷新</button><button data-source-open="${escapeHtml(source.group_id || "")}">查看节点</button><button data-source-delete="${escapeHtml(source.id)}">移除</button></div></article>`;
  }).join("")}</div>` : `<div class="empty"><strong>还没有订阅来源</strong><span>通过上方导入订阅 URL 后，它会单独保存、刷新并形成一个分组。</span></div>`;
}

function renderNodeTable() {
  const table = $("nodeTable");
  const filters = document.querySelector(".node-filter-bar");
  const overview = $("nodeTestOverview");
  if (nodeWorkspaceView === "groups") {
    filters?.classList.add("hidden");
    overview?.classList.add("hidden");
    renderGroupManager();
    renderNodePagination();
    return;
  }
  if (nodeWorkspaceView === "sources") {
    filters?.classList.add("hidden");
    overview?.classList.add("hidden");
    renderSubscriptionSources();
    renderNodePagination();
    return;
  }
  filters?.classList.remove("hidden");
  overview?.classList.remove("hidden");
  renderNodeTestOverview();
  const visible = nodePageItems;
  $("nodeFilterCount").textContent = nodePagination.total ? `${nodePagination.total} 个结果` : "";
  if (!visible.length) {
    _nodeTableFingerprint = "";
    table.innerHTML = `<div class="empty"><strong>${nodes.length ? "没有匹配节点" : "还没有节点"}</strong><span>${nodes.length ? "调整分组、状态或关键词后再试。" : "导入订阅 URL 或粘贴节点链接后，节点会显示在这里。"}</span></div>`;
    renderNodePagination();
    return;
  }
  const fingerprint = `${nodeWorkspaceView}|${activeNodeGroupId}|${nodePagination.page}|${visible.map((n) => `${n.tag}:${n.latency?.alive ?? ""}:${n.latency?.delay ?? ""}:${n.latency?.exit_ip ?? ""}`).join(",")}`;
  if (fingerprint === _nodeTableFingerprint) return;
  _nodeTableFingerprint = fingerprint;
  // Do not auto-select visible nodes on render: live speed-test polling re-renders
  // this table ~1x/sec, and re-adding every tag would revive rows the user just
  // unchecked — then "delete selected" would remove them. Selection is owned by
  // the checkbox change handlers and cleared on page change.
  const allChecked = visible.every((node) => selectedNodePageTags.has(node.tag));
  table.innerHTML = `
    <table class="node-data-table">
      <thead><tr><th><input id="checkAllNodes" type="checkbox" aria-label="选择本页全部节点" ${allChecked ? "checked" : ""}></th><th>节点</th><th>协议</th><th>服务器</th><th>健康</th><th>出口与地区</th><th>测试结果</th></tr></thead>
      <tbody>${visible.map((node) => `
        <tr data-node-tag="${escapeHtml(node.tag)}">
          <td data-label="选择"><input type="checkbox" class="node-check" data-tag="${escapeHtml(node.tag)}" aria-label="选择节点 ${escapeHtml(node.name)}" ${selectedNodePageTags.has(node.tag) ? "checked" : ""}></td>
          <td data-label="节点" class="node-identity"><strong title="${escapeHtml(node.name)}">${escapeHtml(node.name)}</strong><small class="mono" title="${escapeHtml(node.tag)}">${escapeHtml(node.tag)}</small></td>
          <td data-label="协议">${protocolChip(node.type)}</td>
          <td data-label="服务器"><span class="mono server-address" title="${escapeHtml(node.server)}:${Number(node.server_port)}">${escapeHtml(node.server)}:${Number(node.server_port)}</span></td>
          <td data-label="健康" class="node-health-cell" data-node-cell="health">${nodeHealthCellHtml(node)}</td>
          <td data-label="出口与地区" class="node-egress-cell" data-node-cell="egress">${nodeEgressCellHtml(node)}</td>
          <td data-label="目标结果" class="result-preview node-target-result" data-node-cell="target" title="${escapeHtml(targetResponseText(node.latency))}">${nodeTargetResultCellHtml(node)}</td>
        </tr>`).join("")}</tbody>
    </table>`;
  renderNodePagination();
}

// Unsaved edits in the assign table. Live speed-test polling re-renders this
// table every second; without these drafts the user's in-progress checkbox
// and port edits would be silently wiped by the innerHTML replacement.
const assignDraftPorts = new Map();
const assignDraftChecks = new Map();
let _assignTableHtml = "";

function clearAssignDrafts() {
  assignDraftPorts.clear();
  assignDraftChecks.clear();
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
  const isSelected = ({ node, assignedPort }) => (assignDraftChecks.has(node.tag)
    ? assignDraftChecks.get(node.tag)
    : Boolean(assignedPort || (autoSelectAliveForAssign && node.latency?.alive)));
  const portValue = ({ node, assignedPort }) => (assignDraftPorts.has(node.tag)
    ? assignDraftPorts.get(node.tag)
    : assignedPort);
  const allSelected = assignNodes.length > 0 && assignNodes.every(isSelected);
  if (!assignNodes.length) {
    _assignTableHtml = "";
    $("assignTable").innerHTML = `<div class="empty"><strong>没有可分配节点</strong><span>先到节点页导入并测速，再回来分配端口。</span></div>`;
    return;
  }
  const html = `
    <table>
      <thead>
        <tr>
          <th><label class="assign-select-all" title="选择当前分配视图中的全部节点"><input id="checkAllAssign" type="checkbox" aria-label="全选当前分配视图" ${allSelected ? "checked" : ""}><span data-assign-select-all-label>${allSelected ? "全不选" : "全选"}</span></label></th>
          <th>端口</th>
          <th>节点</th>
          <th>出口 IP</th>
          <th>地区</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>${assignNodes.map((entry) => {
        const { node } = entry;
        const checked = isSelected(entry) ? "checked" : "";
        return `<tr>
          <td data-label="使用"><input type="checkbox" class="assign-check" data-tag="${escapeHtml(node.tag)}" aria-label="分配节点 ${escapeHtml(node.name)}" ${checked}></td>
          <td data-label="端口"><input class="port-input" type="number" data-port-for="${escapeHtml(node.tag)}" value="${escapeHtml(String(portValue(entry)))}" min="1024" max="65535"></td>
          <td data-label="节点">${escapeHtml(node.name)}</td>
          <td data-label="出口 IP" class="mono">${escapeHtml(node.latency?.exit_ip || "-")}</td>
          <td data-label="地区">${geoChipHtml(geoContextForNode(node))}</td>
          <td data-label="状态">${statusBadge(node.latency)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
  if (html === _assignTableHtml) {
    syncAssignSelectionControl();
    return;
  }
  _assignTableHtml = html;
  // Preserve keyboard focus on a port input across the innerHTML swap.
  const active = document.activeElement;
  const focusTag = active?.classList?.contains("port-input") && $("assignTable").contains(active)
    ? active.dataset.portFor
    : null;
  $("assignTable").innerHTML = html;
  if (focusTag) {
    document.querySelector(`[data-port-for="${CSS.escape(focusTag)}"]`)?.focus();
  }
  syncAssignSelectionControl();
  attachPortInputListeners();
}

function syncAssignSelectionControl() {
  const control = $("checkAllAssign");
  if (!control) return;
  const boxes = [...document.querySelectorAll(".assign-check")];
  const selected = boxes.filter((box) => box.checked).length;
  const allSelected = boxes.length > 0 && selected === boxes.length;
  control.checked = allSelected;
  control.indeterminate = selected > 0 && !allSelected;
  control.title = allSelected ? "取消选择当前分配视图中的全部节点" : "选择当前分配视图中的全部节点";
  const label = control.closest(".assign-select-all")?.querySelector("[data-assign-select-all-label]");
  if (label) label.textContent = allSelected ? "全不选" : "全选";
}

let _portsTableHtml = "";

function renderPortsTable() {
  const entries = sortedPortEntries();
  const curlTarget = activeValidationUrl();
  if (!entries.length) {
    _portsTableHtml = "";
    $("portsTable").innerHTML = `<div class="empty"><strong>还没有端口映射</strong><span>到「分配」页勾选节点、填端口并保存映射。</span></div>`;
    return;
  }
  const html = `
    <table>
      <thead>
        <tr>
          <th>端口</th>
          <th>节点</th>
          <th>协议</th>
          <th>状态</th>
          <th>延迟</th>
          <th>响应</th>
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
          <td data-label="响应" class="result-preview" title="${escapeHtml(targetResponseText(item.latency))}">${escapeHtml(targetResponsePreview(item.latency))}</td>
          <td data-label="本地检测">${localProxyPortSummary(port) || '<span class="muted">-</span>'}</td>
          <td data-label="出口 IP" class="mono" id="ip-${port}">${escapeHtml(item.exit_ip || item.latency?.exit_ip || localProxyCheckResults[String(port)]?.exit_ip || "-")}</td>
          <td data-label="地区"><span id="geo-${port}">${geoChipHtml(geoContextForPort(port, item))}</span></td>
          <td data-label="curl">
            <button class="copy-command" data-copy="${escapeHtml(curlCommand)}" data-copy-label="curl 命令" title="${escapeHtml(curlCommand)}" aria-label="复制端口 ${port} 的 curl 验证命令">复制 curl</button>
          </td>
          <td data-label="操作">
            <div class="table-actions">
              <button data-ip-port="${port}" title="查询出口 IP" aria-label="查询端口 ${port} 出口 IP">出口</button>
              <button data-validate-port="${port}" title="验证端口 ${port}">验证</button>
              <button data-copy="${escapeHtml(socksProxy)}" data-copy-label="标准 SOCKS5" title="复制 SOCKS5 代理" aria-label="复制端口 ${port} 的 SOCKS5 代理">SOCKS</button>
              <button data-remove-port="${port}" title="移除端口 ${port} 映射" aria-label="移除端口 ${port} 映射">移除</button>
            </div>
          </td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
  // Skip identical re-renders: polling redraws this table up to ~1x/sec and
  // rebuilding the DOM would eat in-flight clicks and hover states.
  if (html !== _portsTableHtml) {
    _portsTableHtml = html;
    $("portsTable").innerHTML = html;
  }
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
      if (result.exit_ip) clearQuickResult();
      else showQuickResult(`端口 ${port}`, `失败：${result.error || "未知错误"}`, false);
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


function renderLocalProxyCheckProgress(message = "", tone = "muted") {
  const box = $("localProxyCheckResult");
  if (!box) return;
  if (!message) {
    box.className = "tool-progress hidden";
    box.textContent = "";
    box.title = "";
    return;
  }
  box.className = `tool-progress ${tone}`;
  box.textContent = message;
  box.title = message;
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
  try {
    for (;;) {
      const job = await requestJobWithRetry(`/api/proxy-check/jobs/${jobId}`);
      finalJob = job;
      Object.entries(job.results || {}).forEach(([port, result]) => {
        localProxyCheckResults[String(port)] = result;
        localProxyCheckingPorts.delete(String(port));
      });
      if (job.status === "done" || job.status === "error" || job.status === "canceled") {
        portList.forEach((port) => localProxyCheckingPorts.delete(String(port)));
      }
      renderPortsTable();

      renderLocalProxyCheckProgress(`检测中 ${job.completed}/${job.total}`);
      if (job.status === "done") break;
      if (job.status === "canceled") break;
      if (job.status === "error") {
        const message = job.error || "本地检测任务失败";
        renderLocalProxyCheckProgress(message, "bad");
        throw new Error(message);
      }
      await pollDelay(700);
    }
  } catch (error) {
    // Network drop / job error: release checking state so the UI can recover.
    portList.forEach((port) => localProxyCheckingPorts.delete(String(port)));
    activeLocalProxyCheckJobId = null;
    setCancelButton("cancelLocalProxyCheckBtn", false);
    renderPortsTable();
    throw error;
  }
  activeLocalProxyCheckJobId = null;
  setCancelButton("cancelLocalProxyCheckBtn", false);
  renderLocalProxyCheckProgress();
  const results = Object.values(finalJob?.results || {});
  return {
    passed: results.filter((result) => !localProxyCheckFailed(result)).length,
    failed: results.filter((result) => localProxyCheckFailed(result)).length
  };
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

function qualityFromResult(result) {
  if (!result) return { bucket: 1, gradeRank: 99, score: -1 };
  if (result?.grade === "ERR" || (result?.items || []).some((item) => item.status === "fail")) return { bucket: 2, gradeRank: 99, score: Number(result.score || 0) };
  const gradeOrder = { A: 0, B: 1, C: 2, D: 3, F: 4 };
  return {
    bucket: 0,
    gradeRank: gradeOrder[String(result.grade || "").toUpperCase()] ?? 50,
    score: Number(result.score || 0)
  };
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

function showQuickResult(title, body, ok = true) {
  const box = $("quickResult");
  box.className = `quick-result ${ok ? "ok" : "bad"}`;
  box.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(body)}</span>`;
}

function clearQuickResult() {
  const box = $("quickResult");
  if (!box) return;
  box.className = "quick-result hidden";
  box.innerHTML = "";
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

function shortAssetVersion(version) {
  const text = String(version || "");
  const withoutDate = text.replace(/^\d{8}-/, "");
  return withoutDate || text || "-";
}

function renderSingBoxInfo(info) {
  const current = info.current_version ? `v${info.current_version}` : "未安装";
  const latest = info.latest_version ? ` · 最新 v${info.latest_version}` : "";
  const backup = info.backup_version ? ` · 备份 v${info.backup_version}` : "";
  const platform = `${info.platform || "-"}/${info.architecture || "-"}`;
  $("singBoxVersionSummary").textContent = `${current}${latest}${backup} · ${platform}`;
  $("singBoxVersionSummary").title = info.path || "";
  $("updateSingBoxBtn").disabled = !info.managed || !info.update_available;
  $("rollbackSingBoxBtn").disabled = !info.rollback_available;
}

async function loadSingBoxInfo(checkLatest = false) {
  const info = await request(`/api/engine/version?check_latest=${checkLatest ? "true" : "false"}`);
  singBoxInfoLoaded = true;
  renderSingBoxInfo(info);
  return info;
}

function formatTraffic(value) {
  const amount = Number(value || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  let display = amount;
  while (display >= 1024 && unit < units.length - 1) {
    display /= 1024;
    unit += 1;
  }
  return `${display >= 100 || unit === 0 ? Math.round(display) : display.toFixed(1)} ${units[unit]}`;
}

function formatTrafficRate(value) {
  return `${formatTraffic(value)}/s`;
}

function trafficPoints(values, key, maxValue, width = 620, height = 150) {
  return values.map((item, index) => ({
    x: values.length === 1 ? width / 2 : (index / (values.length - 1)) * width,
    y: height - ((Number(item[key] || 0) / maxValue) * (height - 22)) - 11
  }));
}

function smoothTrafficSeries(series, key) {
  return series.map((item, index) => {
    let total = 0;
    let weight = 0;
    for (let offset = -2; offset <= 2; offset += 1) {
      const neighbor = series[index + offset];
      if (!neighbor) continue;
      const currentWeight = 3 - Math.abs(offset);
      total += Number(neighbor[key] || 0) * currentWeight;
      weight += currentWeight;
    }
    return { ...item, [key]: weight ? total / weight : Number(item[key] || 0) };
  });
}

function smoothTrafficPath(points) {
  if (!points.length) return "";
  if (points.length === 1) return `M${points[0].x.toFixed(1)},${points[0].y.toFixed(1)}`;
  let path = `M${points[0].x.toFixed(1)},${points[0].y.toFixed(1)}`;
  for (let index = 0; index < points.length - 1; index += 1) {
    const previous = points[index - 1] || points[index];
    const current = points[index];
    const next = points[index + 1];
    const following = points[index + 2] || next;
    const firstControl = { x: current.x + (next.x - previous.x) / 6, y: current.y + (next.y - previous.y) / 6 };
    const secondControl = { x: next.x - (following.x - current.x) / 6, y: next.y - (following.y - current.y) / 6 };
    path += ` C${firstControl.x.toFixed(1)},${firstControl.y.toFixed(1)} ${secondControl.x.toFixed(1)},${secondControl.y.toFixed(1)} ${next.x.toFixed(1)},${next.y.toFixed(1)}`;
  }
  return path;
}

function renderTrafficChart(series = []) {
  const chart = $("trafficChart");
  if (!chart) return;
  if (!series.length) {
    chart.innerHTML = `<div class="empty"><strong>暂无流量数据</strong><span>启动引擎后，监控会每 15 秒聚合一次公开端口的流量。</span></div>`;
    return;
  }
  const maxValue = Math.max(1, ...series.flatMap((item) => [Number(item.download || 0), Number(item.upload || 0)]));
  const downloadPoints = trafficPoints(smoothTrafficSeries(series, "download"), "download", maxValue);
  const uploadPoints = trafficPoints(smoothTrafficSeries(series, "upload"), "upload", maxValue);
  const download = smoothTrafficPath(downloadPoints);
  const upload = smoothTrafficPath(uploadPoints);
  const downloadArea = `${download} L${downloadPoints.at(-1).x.toFixed(1)},150 L${downloadPoints[0].x.toFixed(1)},150 Z`;
  const uploadArea = `${upload} L${uploadPoints.at(-1).x.toFixed(1)},150 L${uploadPoints[0].x.toFixed(1)},150 Z`;
  const first = new Date(Number(series[0].bucket_start) * 1000).toLocaleString();
  const last = new Date(Number(series[series.length - 1].bucket_start) * 1000).toLocaleString();
  const labels = [series[0], series[Math.floor(series.length / 2)], series.at(-1)].map((item) => new Date(Number(item.bucket_start) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  chart.innerHTML = `<svg viewBox="0 0 620 150" preserveAspectRatio="none" role="img" aria-labelledby="trafficChartTitle trafficChartDesc"><title id="trafficChartTitle">流量趋势</title><desc id="trafficChartDesc">范围从 ${escapeHtml(first)} 到 ${escapeHtml(last)}。下载总量 ${formatTraffic(series.reduce((sum, item) => sum + Number(item.download || 0), 0))}，上传总量 ${formatTraffic(series.reduce((sum, item) => sum + Number(item.upload || 0), 0))}。</desc><g class="traffic-grid"><path d="M0 26H620M0 72H620M0 118H620"/></g><path class="traffic-area download" d="${downloadArea}"/><path class="traffic-area upload" d="${uploadArea}"/><path class="traffic-line download" d="${download}"/><path class="traffic-line upload" d="${upload}"/><g class="traffic-axis"><text x="0" y="147">${escapeHtml(labels[0])}</text><text x="310" y="147" text-anchor="middle">${escapeHtml(labels[1])}</text><text x="620" y="147" text-anchor="end">${escapeHtml(labels[2])}</text></g></svg>`;
}

function renderTrafficEvents(items = []) {
  const target = $("trafficEvents");
  if (!target) return;
  target.innerHTML = items.length ? items.slice(0, 5).map((item) => `<article><strong>${escapeHtml(item.event_type || "事件")}</strong><span>${escapeHtml(item.message || "-")}</span><small>${new Date(Number(item.created_at) * 1000).toLocaleString()}</small></article>`).join("") : `<div class="empty"><strong>暂无运行事件</strong><span>节点池操作和运行状态变更将显示在这里。</span></div>`;
}

function renderTrafficTable(items = []) {
  const target = $("trafficTable");
  if (!target) return;
  const search = trafficSearchQuery.trim().toLowerCase();
  const filtered = items.filter((item) => `${item.entity_id || ""} ${item.name || ""}`.toLowerCase().includes(search));
  if (!filtered.length) {
    target.innerHTML = `<div class="empty"><strong>暂无${trafficEntityType === "port" ? "端口" : trafficEntityType === "pool" ? "节点池" : "节点"}流量</strong><span>等待公开端口产生流量，或调整筛选条件。</span></div>`;
    return;
  }
  const labels = { port: "端口", pool: "节点池", node: "节点" };
  const visible = filtered.slice(0, trafficDisplayLimit);
  target.innerHTML = `<table><thead><tr><th>${labels[trafficEntityType]}</th><th>下载</th><th>上传</th><th>活跃连接</th><th>最后活动</th><th>查看</th></tr></thead><tbody>${visible.map((item) => `<tr><td data-label="${labels[trafficEntityType]}"><strong>${escapeHtml(item.name || item.entity_id)}</strong><small class="mono">${escapeHtml(item.entity_id)}</small></td><td data-label="下载">${formatTraffic(item.download)}</td><td data-label="上传">${formatTraffic(item.upload)}</td><td data-label="活跃连接">${Number(item.active || 0)}</td><td data-label="最后活动">${item.last_activity ? new Date(Number(item.last_activity) * 1000).toLocaleString() : "—"}</td><td data-label="查看"><button data-traffic-detail="${escapeHtml(item.entity_id)}">详情</button></td></tr>`).join("")}</tbody></table>${filtered.length > trafficDisplayLimit ? `<div class="traffic-table-more"><span>显示前 ${trafficDisplayLimit} 条活跃记录</span><button data-traffic-show-more>展开全部 ${filtered.length} 条</button></div>` : ""}`;
}

async function openTrafficDetail(entityId) {
  const target = $("trafficDetail");
  target.classList.remove("hidden");
  target.innerHTML = `<div class="traffic-drawer-head"><strong>正在读取详情…</strong><button data-close-traffic-detail>关闭</button></div>`;
  try {
    const detail = await request(`/api/traffic/entities/${encodeURIComponent(trafficEntityType)}/${encodeURIComponent(entityId)}?range=${trafficRange}`);
    target.innerHTML = `<div class="traffic-drawer-head"><div><strong>${escapeHtml(entityId)}</strong><span>${trafficEntityType === "port" ? "端口" : trafficEntityType === "pool" ? "节点池" : "节点"}流量明细</span></div><button data-close-traffic-detail>关闭</button></div><div class="traffic-drawer-kpis"><span>下载 <b>${formatTraffic(detail.download)}</b></span><span>上传 <b>${formatTraffic(detail.upload)}</b></span></div><div class="traffic-drawer-chart">${detail.series.length ? `${detail.series.length} 个时间桶 · 最近 ${trafficRange}` : "暂无历史数据"}</div>`;
  } catch (error) {
    target.innerHTML = `<div class="traffic-drawer-head"><strong>详情不可用</strong><button data-close-traffic-detail>关闭</button></div><p>${escapeHtml(error.message)}</p>`;
  }
}

function renderPools() {
  const list = $("poolList");
  const routerNotice = $("poolRouterNotice");
  if (!list || !routerNotice) return;
  const router = statusSnapshot.pool_router || {};
  $("createPoolBtn").disabled = !router.available;
  $("createPoolBtn").title = router.available ? "" : "先运行安装脚本构建 Pool Router";
  routerNotice.className = `pool-router-notice ${router.available ? (router.running ? "ok" : "warn") : "bad"}`;
  routerNotice.textContent = router.available ? (router.running ? "Pool Router 正在运行，统计与轮询已启用。" : "Pool Router 已安装；启用节点池后可直接启动引擎，无需建立普通端口映射。") : "Pool Router 未安装：固定端口仍可使用；节点池和端口流量监控需要安装脚本构建 Go Router。";
  if (!pools.length) {
    list.innerHTML = `<div class="empty"><strong>还没有节点池</strong><span>创建后即可用一个代理端口在多个健康节点间轮询。</span></div>`;
    return;
  }
  list.innerHTML = pools.map((pool) => {
    const active = pool.members.filter((member) => member.enabled && !member.draining).length;
    const preview = pool.members.slice(0, 12);
    const remaining = pool.members.length - preview.length;
    const policy = pool.policy === "time_window" ? `固定 ${pool.rotation_interval_seconds || 600} 秒` : pool.policy === "weighted_round_robin" ? "加权新连接" : "每条新连接";
    const switchLabel = `${pool.enabled ? "停用" : "启用"}节点池 ${pool.name}`;
    return `<article class="pool-card"><header><div><strong>${escapeHtml(pool.name)}</strong><span class="mono">:${pool.listen_port}</span></div><button type="button" class="pool-toggle ${pool.enabled ? "on" : ""}" role="switch" aria-checked="${pool.enabled ? "true" : "false"}" aria-label="${escapeHtml(switchLabel)}" title="${escapeHtml(switchLabel)}" data-pool-toggle="${escapeHtml(pool.id)}"><span class="pool-toggle-knob" aria-hidden="true"></span></button></header><p>${policy} · ${active}/${pool.members.length} 活跃成员</p><div class="pool-member-chips">${preview.map((member) => `<span class="${member.draining ? "draining" : member.alive === false ? "unhealthy" : ""}">${escapeHtml(member.node_name || member.node_tag)} ×${member.weight}${member.draining ? " · 排空" : ""}</span>`).join("")}${remaining ? `<span class="member-overflow">+ ${remaining} 个成员</span>` : ""}</div><footer><button data-copy="${escapeHtml(pool.http_proxy)}" data-copy-label="节点池 HTTP 地址">复制 HTTP</button><button data-pool-advance="${escapeHtml(pool.id)}">切换下一节点</button><button data-pool-edit="${escapeHtml(pool.id)}">编辑</button><button data-pool-delete="${escapeHtml(pool.id)}">删除</button></footer></article>`;
  }).join("");
}

function poolUpdatePayload(pool, enabled = pool.enabled) {
  return {
    name: pool.name,
    listen_port: Number(pool.listen_port),
    policy: pool.policy,
    rotation_interval_seconds: Number(pool.rotation_interval_seconds || 600),
    enabled,
    members: (pool.members || []).map((member) => ({
      node_tag: member.node_tag,
      weight: Number(member.weight || 1),
      enabled: member.enabled !== false,
      draining: Boolean(member.draining)
    }))
  };
}

function renderPoolPolicyHint() {
  const policy = $("poolPolicy")?.value || "weighted_round_robin";
  const isWindow = policy === "time_window";
  $("poolRotationIntervalField")?.classList.toggle("hidden", !isWindow);
  const hint = isWindow
    ? "窗口内的所有新连接复用同一出口；窗口结束或成员拨号失败后才切换。已建立连接始终保持原出口。"
    : policy === "round_robin"
      ? "每条新的 TCP 代理连接按成员顺序切换；连接建立后不会中途换 IP。"
      : "每条新的 TCP 代理连接按权重选择成员；连接建立后不会中途换 IP。";
  $("poolPolicyHint").textContent = hint;
}

function openPoolEditor(pool = null) {
  $("poolEditor").classList.remove("hidden");
  $("poolEditorTitle").textContent = pool ? `编辑节点池 · ${pool.name}` : "新建节点池";
  $("poolId").value = pool?.id || "";
  $("poolName").value = pool?.name || "";
  $("poolPort").value = pool?.listen_port || "8201";
  $("poolPolicy").value = pool?.policy || "weighted_round_robin";
  $("poolRotationInterval").value = pool?.rotation_interval_seconds || 600;
  $("poolEnabled").checked = pool?.enabled ?? true;
  const selected = new Map((pool?.members || []).map((member) => [member.node_tag, member]));
  poolEditorMembers = new Map(nodes.map((node) => {
    const member = selected.get(node.tag);
    return [node.tag, {
      enabled: Boolean(member) || (!pool && Boolean(node.latency?.alive)),
      weight: member?.weight || 1,
      draining: Boolean(member?.draining)
    }];
  }));
  poolMemberSearchQuery = "";
  renderPoolPolicyHint();
  renderPoolMembers();
}

function hidePoolEditor() {
  $("poolEditor").classList.add("hidden");
  poolEditorMembers = new Map();
  poolMemberSearchQuery = "";
}

function poolEditorVisibleNodes() {
  const query = poolMemberSearchQuery.trim().toLowerCase();
  if (!query) return nodes;
  return nodes.filter((node) => {
    const status = node.latency?.alive ? "可用" : node.latency?.alive === false ? "不可用" : "未测速";
    return `${node.name} ${node.type} ${node.tag} ${status}`.toLowerCase().includes(query);
  });
}

function syncPoolEditorMembers() {
  document.querySelectorAll("[data-pool-member]").forEach((input) => {
    const member = poolEditorMembers.get(input.dataset.poolMember);
    if (member) member.enabled = input.checked;
  });
  document.querySelectorAll("[data-pool-weight]").forEach((input) => {
    const member = poolEditorMembers.get(input.dataset.poolWeight);
    if (member) member.weight = Math.max(1, Math.min(100, Number(input.value) || 1));
  });
  document.querySelectorAll("[data-pool-draining]").forEach((input) => {
    const member = poolEditorMembers.get(input.dataset.poolDraining);
    if (member) member.draining = input.checked;
  });
}

function renderPoolMembers() {
  const target = $("poolMembers");
  if (!target) return;
  if (!nodes.length) {
    target.innerHTML = `<div class="empty"><strong>没有可用节点</strong><span>请先导入节点。</span></div>`;
    return;
  }
  const visible = poolEditorVisibleNodes();
  const selectedCount = [...poolEditorMembers.values()].filter((member) => member.enabled).length;
  target.innerHTML = `
    <div class="pool-members-head">
      <h3>成员与权重</h3>
      <span aria-live="polite">已选 ${selectedCount} / ${nodes.length}</span>
      <label class="pool-member-search"><span class="sr-only">筛选节点池成员</span><input data-pool-member-search type="search" value="${escapeHtml(poolMemberSearchQuery)}" placeholder="筛选成员"></label>
      <div class="pool-member-actions">
        <button type="button" data-pool-members-action="add-healthy">添加可用</button>
        <button type="button" data-pool-members-action="select-visible">全选当前</button>
        <button type="button" data-pool-members-action="clear">清空</button>
      </div>
    </div>
    <div class="pool-member-list">
      ${visible.map((node) => {
        const member = poolEditorMembers.get(node.tag);
        const state = node.latency?.alive
          ? (node.latency.delay ? `可用 · ${node.latency.delay}ms` : "可用")
          : node.latency?.alive === false
            ? "不可用"
            : "未测速";
        const disabled = member.enabled ? "" : "disabled";
        return `<div class="pool-member-row"><label class="pool-member-check"><input type="checkbox" data-pool-member="${escapeHtml(node.tag)}" ${member.enabled ? "checked" : ""}><span>${escapeHtml(node.name)}</span></label><small>${escapeHtml(node.type)} · ${state}</small><input type="number" data-pool-weight="${escapeHtml(node.tag)}" value="${member.weight}" min="1" max="100" aria-label="${escapeHtml(node.name)} 权重" ${disabled}><label class="pool-drain-toggle"><input type="checkbox" data-pool-draining="${escapeHtml(node.tag)}" ${member.draining ? "checked" : ""} ${disabled}>排空</label></div>`;
      }).join("") || `<div class="empty"><strong>没有匹配的节点</strong><span>更换筛选条件后再试。</span></div>`}
    </div>`;
}

async function refreshTraffic() {
  const seq = ++_trafficReqSeq;
  try {
    const [overview, entityData, eventData] = await Promise.all([
      request(`/api/traffic/overview?range=${trafficRange}`),
      request(`/api/traffic/entities?type=${trafficEntityType}&range=${trafficRange}`),
      request("/api/traffic/events?limit=5")
    ]);
    if (seq !== _trafficReqSeq) return;
    $("trafficDownload").textContent = formatTraffic(overview.download);
    $("trafficUpload").textContent = formatTraffic(overview.upload);
    $("trafficDownloadRate").textContent = `当前 ${formatTrafficRate(overview.download_rate)}`;
    $("trafficUploadRate").textContent = `当前 ${formatTrafficRate(overview.upload_rate)}`;
    $("trafficActive").textContent = Number(overview.active || 0);
    const router = overview.router || {};
    $("trafficRouterState").textContent = router.running ? "Router 计量中" : router.available ? "等待引擎启动" : "Router 未安装";
    const attention = Number(!router.running && statusSnapshot.running) + Number(!router.available);
    $("trafficAttention").textContent = attention ? `${attention} 项` : "正常";
    $("trafficChartSummary").textContent = `${overview.series.length} 个时间桶 · ${trafficRange}`;
    renderTrafficChart(overview.series || []);
    trafficEntities = entityData.items || [];
    renderTrafficEvents(eventData.items || []);
    renderTrafficTable(trafficEntities);
  } catch (error) {
    if (seq !== _trafficReqSeq) return;
    $("trafficChart").innerHTML = `<div class="empty"><strong>监控暂不可用</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function nodeWorkspaceQuery() {
  const params = new URLSearchParams({
    page: String(nodePagination.page || 1),
    page_size: String(nodePagination.page_size || 50),
    sort: "quality"
  });
  if (activeNodeGroupId) params.set("group_id", activeNodeGroupId);
  if (nodeStatusFilter !== "all") params.set("status", nodeStatusFilter);
  if (nodeSearchQuery.trim()) params.set("query", nodeSearchQuery.trim());
  return params.toString();
}

let _nodeWorkspaceReqSeq = 0;

async function refreshNodeWorkspace() {
  const seq = ++_nodeWorkspaceReqSeq;
  const data = await request(`/api/nodes?${nodeWorkspaceQuery()}`);
  // A newer request superseded this one; drop the stale response.
  if (seq !== _nodeWorkspaceReqSeq) return;
  nodePageItems = data.nodes || [];
  nodePagination = { ...nodePagination, ...(data.pagination || {}) };
  renderNodeGroups();
  renderNodeTable();
}

async function refresh() {
  const [status, nodeData, portData, poolData, groupData, sourceData, nodePageData] = await Promise.all([
    request("/api/status"),
    request("/api/nodes"),
    request("/api/ports"),
    request("/api/pools"),
    request("/api/groups"),
    request("/api/subscriptions"),
    request(`/api/nodes?${nodeWorkspaceQuery()}`)
  ]);
  statusSnapshot = status;
  nodes = nodeData.nodes;
  ports = portData.ports;
  localProxyCheckResults = portData.local_proxy_checks || {};
  pools = poolData.pools || [];
  nodeGroups = groupData.groups || [];
  subscriptionSources = sourceData.sources || [];
  nodePageItems = nodePageData.nodes || [];
  nodePagination = { ...nodePagination, ...(nodePageData.pagination || {}) };
  renderSummary();
  renderSubscription(status.subscription);
  renderNodeGroups();
  renderNodeTable();
  renderAssignTable();
  renderLocalProxyCheckPorts();
  renderPortsTable();
  renderPools();
  await refreshTraffic();
  if (!singBoxInfoLoaded) {
    try {
      await loadSingBoxInfo(false);
    } catch (error) {
      $("singBoxVersionSummary").textContent = `检测失败：${error.message}`;
    }
  }
}

async function refreshStatusOnly() {
  statusSnapshot = await request("/api/status");
  renderSummary();
  if ($("monitor")?.classList.contains("active")) await refreshTraffic();
}

const _runningTaskLabels = new Set();

async function runTask(label, task) {
  // Guard against double-clicks: the same action must not run concurrently.
  if (_runningTaskLabels.has(label)) {
    showNotice(`「${label}」正在执行中，请稍候`, "info");
    return;
  }
  _runningTaskLabels.add(label);
  showNotice(`${label}...`);
  try {
    const message = await task();
    showNotice(message || `${label}完成`, "ok");
  } catch (error) {
    showNotice(error.message, "bad");
  } finally {
    _runningTaskLabels.delete(label);
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
  nodeTestProgress = { completed: 0, total: tags.length };
  renderNodeTable();
  showNotice(targetUrls?.length ? `已开始测速目标：${targetUrls.join("，")}` : `已开始测速默认目标：${DEFAULT_VALIDATION_URLS[0]}`);
  try {
    const started = await request("/api/test/start", {
      method: "POST",
      body: JSON.stringify({ node_tags: tags, prune_same_ip: pruneSameIp, include_geoip: Boolean($("nodeTestGeo")?.checked), target_urls: targetUrls })
    });
    activeNodeTestJobId = started.id;
    nodeTestRevision = Number(started.revision || 0);
    setCancelButton("cancelNodeTestBtn", true);
    await pollTestJob(started.id);
  } catch (error) {
    activeNodeTestJobId = null;
    setCancelButton("cancelNodeTestBtn", false);
    testingTags.clear();
    nodeTestProgress = null;
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
    const job = await requestJobWithRetry(`/api/test/jobs/${jobId}?since=${nodeTestRevision}`);
    Object.entries(job.results || {}).forEach(([tag, result]) => {
      updateNodeLatency(tag, result);
      testingTags.delete(tag);
    });
    nodeTestRevision = Number(job.revision || nodeTestRevision);
    nodeTestProgress = { completed: job.completed, total: job.total };
    renderNodeTable();
    if ($("assign")?.classList.contains("active")) renderAssignTable();
    renderNodeTestOverview();
    if (job.status === "done") {
      testingTags.clear();
      nodeTestProgress = null;
      activeNodeTestJobId = null;
      nodeTestRevision = 0;
      setCancelButton("cancelNodeTestBtn", false);
      await refresh();
      const removed = job.removed?.length ? `；已去重：${job.removed.join("；")}` : "";
      showNotice(`测速完成：${job.completed}/${job.total}${removed}`, "ok");
      return;
    }
    if (job.status === "canceled") {
      testingTags.clear();
      nodeTestProgress = null;
      activeNodeTestJobId = null;
      nodeTestRevision = 0;
      setCancelButton("cancelNodeTestBtn", false);
      renderNodeTable();
      showNotice(`测速已取消：${job.completed}/${job.total}`, "bad");
      return;
    }
    if (job.status === "error") {
      testingTags.clear();
      nodeTestProgress = null;
      activeNodeTestJobId = null;
      nodeTestRevision = 0;
      setCancelButton("cancelNodeTestBtn", false);
      renderNodeTable();
      showNotice(job.error || "测速失败", "bad");
      return;
    }
    await pollDelay(1000);
  }
}
function selectedNodeTags() {
  return nodePageItems.filter((node) => selectedNodePageTags.has(node.tag)).map((node) => node.tag);
}

function workspaceNodeTags() {
  const group = activeNodeGroupId ? nodeGroups.find((item) => item.id === activeNodeGroupId) : null;
  const candidates = group ? nodes.filter((node) => group.node_tags.includes(node.tag)) : nodes;
  return candidates
    .filter((node) => {
      if (nodeStatusFilter === "alive") return Boolean(node.latency?.alive);
      if (nodeStatusFilter === "failed") return Boolean(node.latency && !node.latency.alive);
      if (nodeStatusFilter === "untested") return !node.latency;
      return true;
    })
    .filter((node) => {
      const query = nodeSearchQuery.trim().toLowerCase();
      return !query || `${node.name} ${node.tag} ${node.type} ${node.server}`.toLowerCase().includes(query);
    })
    .map((node) => node.tag);
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
  const result = await request("/api/assign", { method: "PUT", body: JSON.stringify({ mappings }) });
  // Server state is now the source of truth; stale drafts would otherwise
  // shadow the freshly saved values on the next render.
  clearAssignDrafts();
  return result;
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
    // Keeping an already-owned mapping (or reshuffling onto a port this
    // project already listens on) is fine; only foreign occupations block.
    if (currentPorts.has(port) && (item.reason === "project-listening" || item.reason === "project-mapped")) {
      return false;
    }
    return true;
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
    validatingPorts.delete(String(port));
    renderPortsTable();
    renderValidationResults();
    clearQuickResult();
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
    const job = await requestJobWithRetry(`/api/test-ports/jobs/${jobId}`);
    applyPortValidationResults(job);
    renderPortsTable();
    renderValidationResults();
    showQuickResult("验证全部端口", `验证中；目标：${urls.join("，")}；进度 ${job.completed}/${job.total}`);
    if (job.status === "done") {
      validatingPorts.clear();
      renderPortsTable();
      renderValidationResults();
      clearQuickResult();
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
    await pollDelay(800);
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

function csvCell(value) {
  let text = String(value ?? "");
  // Prevent CSV formula injection: a leading = + - @ (or tab/CR) makes Excel
  // and other spreadsheets evaluate the cell as a formula. Prefix a single
  // quote to neutralise it, then escape embedded double quotes.
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return text.replace(/"/g, '""');
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
    const lines = rows.map((row) => header.map((key) => `"${csvCell(row[key])}"`).join(","));
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
  if (tabId === "assign" && activeNodeTestJobId) renderAssignTable();
  if (persist) localStorage.setItem(ACTIVE_TAB_KEY, tabId);
  return true;
}

function wireExclusiveNodeActionMenus() {
  // These are sibling command menus. Nested details such as the import text
  // expander deliberately stay outside this group so they cannot close import.
  const menus = [...document.querySelectorAll("#test .test-actions > details")];
  menus.forEach((menu) => {
    menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      menus.forEach((other) => {
        if (other !== menu) other.open = false;
      });
    });
  });
  document.addEventListener("pointerdown", (event) => {
    if (event.target instanceof Element && event.target.closest("#test .test-actions")) return;
    menus.forEach((menu) => { menu.open = false; });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    menus.forEach((menu) => { menu.open = false; });
  });
}

wireExclusiveNodeActionMenus();

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    activateTab(button.dataset.tab);
  });
});

activateTab(localStorage.getItem(ACTIVE_TAB_KEY) || "monitor", false);

document.querySelectorAll(".traffic-range").forEach((button) => {
  button.classList.toggle("active", button.dataset.range === trafficRange);
  button.addEventListener("click", async () => {
    trafficRange = button.dataset.range;
    localStorage.setItem("proxyPoolManager.trafficRange", trafficRange);
    document.querySelectorAll(".traffic-range").forEach((item) => item.classList.toggle("active", item === button));
    await refreshTraffic();
  });
});

document.querySelectorAll(".traffic-entity").forEach((button) => {
  button.addEventListener("click", async () => {
    trafficEntityType = button.dataset.entity;
    document.querySelectorAll(".traffic-entity").forEach((item) => item.classList.toggle("active", item === button));
    await refreshTraffic();
  });
});

$("trafficSearch").addEventListener("input", () => {
  trafficSearchQuery = $("trafficSearch").value;
  // Filtering happens client-side in renderTrafficTable, so re-render the
  // cached rows instead of firing a request on every keystroke (that also
  // let slow, out-of-order responses overwrite freshly typed results).
  renderTrafficTable(trafficEntities);
});

$("trafficTable").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-traffic-detail]");
  if (button) openTrafficDetail(button.dataset.trafficDetail);
  if (event.target.closest("[data-traffic-show-more]")) {
    trafficDisplayLimit = 500;
    renderTrafficTable(trafficEntities);
  }
});

$("trafficDetail").addEventListener("click", (event) => {
  if (event.target.closest("[data-close-traffic-detail]")) $("trafficDetail").classList.add("hidden");
});

document.querySelectorAll(".run-subtab").forEach((button) => {
  button.addEventListener("click", () => {
    const poolsView = button.dataset.runView === "pools";
    document.querySelectorAll(".run-subtab").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    $("dashboardRunTools").classList.toggle("hidden", poolsView);
    $("poolManager").classList.toggle("hidden", !poolsView);
  });
});

$("createPoolBtn").addEventListener("click", () => openPoolEditor());
$("cancelPoolEditBtn").addEventListener("click", hidePoolEditor);
$("poolEditor").addEventListener("submit", (event) => {
  event.preventDefault();
  runTask("保存节点池", async () => {
    syncPoolEditorMembers();
    const members = nodes
      .filter((node) => poolEditorMembers.get(node.tag)?.enabled)
      .map((node) => {
        const member = poolEditorMembers.get(node.tag);
        return { node_tag: node.tag, weight: member.weight, enabled: true, draining: member.draining };
      });
    const payload = { name: $("poolName").value.trim(), listen_port: Number($("poolPort").value), policy: $("poolPolicy").value, rotation_interval_seconds: Number($("poolRotationInterval").value || 600), enabled: $("poolEnabled").checked, members };
    const id = $("poolId").value;
    await request(id ? `/api/pools/${encodeURIComponent(id)}` : "/api/pools", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    hidePoolEditor();
    await refresh();
    return "节点池已保存";
  });
});

$("poolPolicy").addEventListener("change", renderPoolPolicyHint);

$("poolMembers").addEventListener("click", (event) => {
  const action = event.target.closest("button[data-pool-members-action]")?.dataset.poolMembersAction;
  if (!action) return;
  syncPoolEditorMembers();
  if (action === "add-healthy") {
    nodes.filter((node) => node.latency?.alive).forEach((node) => {
      poolEditorMembers.get(node.tag).enabled = true;
    });
  } else if (action === "select-visible") {
    poolEditorVisibleNodes().forEach((node) => {
      poolEditorMembers.get(node.tag).enabled = true;
    });
  } else if (action === "clear") {
    poolEditorMembers.forEach((member) => { member.enabled = false; });
  }
  renderPoolMembers();
});

$("poolMembers").addEventListener("input", (event) => {
  const target = event.target;
  if (target.matches("[data-pool-member-search]")) {
    poolMemberSearchQuery = target.value;
    renderPoolMembers();
    return;
  }
  const tag = target.dataset.poolMember || target.dataset.poolWeight || target.dataset.poolDraining;
  if (!tag || !poolEditorMembers.has(tag)) return;
  const member = poolEditorMembers.get(tag);
  if (target.dataset.poolMember) member.enabled = target.checked;
  if (target.dataset.poolWeight) member.weight = Math.max(1, Math.min(100, Number(target.value) || 1));
  if (target.dataset.poolDraining) member.draining = target.checked;
  renderPoolMembers();
});

$("poolList").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.copy) {
    copyText(button.dataset.copy, button.dataset.copyLabel || "地址");
    return;
  }
  const id = button.dataset.poolEdit || button.dataset.poolAdvance || button.dataset.poolDelete || button.dataset.poolToggle;
  if (!id) return;
  const pool = pools.find((item) => item.id === id);
  if (button.dataset.poolToggle) runTask(pool?.enabled ? "停用节点池" : "启用节点池", async () => {
    if (!pool) throw new Error("节点池已不存在，请刷新页面后重试");
    // PUT is supported by both the running legacy backend and the upgraded
    // backend, so the quick switch remains usable during a rolling update.
    const enabled = !pool.enabled;
    await request(`/api/pools/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(poolUpdatePayload(pool, enabled)) });
    await refresh();
    return enabled ? `节点池「${pool.name}」已启用` : `节点池「${pool.name}」已停用`;
  });
  if (button.dataset.poolEdit) openPoolEditor(pool);
  if (button.dataset.poolAdvance) runTask("切换下一节点", async () => {
    await request(`/api/pools/${encodeURIComponent(id)}/advance`, { method: "POST", body: "{}" });
    await refresh();
    return "下一条新连接将从后续成员开始选择";
  });
  if (button.dataset.poolDelete) confirmTask("删除节点池", `将删除节点池「${pool?.name || id}」及其公开端口。`, async () => {
    await request(`/api/pools/${encodeURIComponent(id)}`, { method: "DELETE", body: "{}" });
    hidePoolEditor();
    await refresh();
    return "节点池已删除";
  });
});

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

$("testWorkspaceBtn").addEventListener("click", () => {
  const tags = workspaceNodeTags();
  if (!tags.length) {
    showNotice("当前分组没有可测速节点", "ok");
    return;
  }
  runNodeTest(false, tags);
});

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
    nodePagination.page = 1;
    refreshNodeWorkspace().catch((error) => showNotice(error.message, "bad"));
  }, 200);
});

document.querySelectorAll(".node-filter-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".node-filter-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    nodeStatusFilter = tab.dataset.filter;
    nodePagination.page = 1;
    refreshNodeWorkspace().catch((error) => showNotice(error.message, "bad"));
  });
});

$("nodePageSize").addEventListener("change", () => {
  nodePagination.page_size = Number($("nodePageSize").value || 50);
  nodePagination.page = 1;
  refreshNodeWorkspace().catch((error) => showNotice(error.message, "bad"));
});

document.querySelectorAll(".node-view-tab").forEach((button) => {
  button.addEventListener("click", () => {
    nodeWorkspaceView = button.dataset.nodeView;
    document.querySelectorAll(".node-view-tab").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    renderNodeTable();
  });
});

$("nodeCollections").addEventListener("click", (event) => {
  const groupButton = event.target.closest("[data-node-group]");
  if (groupButton) {
    activeNodeGroupId = groupButton.dataset.nodeGroup || "";
    nodePagination.page = 1;
    document.querySelectorAll(".node-collection-item").forEach((item) => item.classList.toggle("active", item === groupButton));
    nodeWorkspaceView = "nodes";
    document.querySelector('[data-node-view="nodes"]')?.click();
    refreshNodeWorkspace().catch((error) => showNotice(error.message, "bad"));
  }
});

function openNodeGroup(groupId) {
  if (!groupId) return;
  activeNodeGroupId = groupId;
  nodePagination.page = 1;
  nodeWorkspaceView = "nodes";
  document.querySelectorAll(".node-view-tab").forEach((item) => {
    const active = item.dataset.nodeView === "nodes";
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".node-collection-item").forEach((item) => item.classList.toggle("active", item.dataset.nodeGroup === groupId));
  refreshNodeWorkspace().catch((error) => showNotice(error.message, "bad"));
}

function assignNodeGroup(groupId) {
  const group = nodeGroups.find((item) => item.id === groupId);
  if (!group) return;
  assignFilterTags = new Set(group.node_tags || []);
  autoSelectAliveForAssign = true;
  renderAssignTable();
  activateTab("assign");
  showNotice(`已载入分组「${group.name}」的 ${group.node_count} 个节点`, "ok");
}

function showNodeGroupForm() {
  $("nodeGroupForm").classList.remove("hidden");
  $("nodeGroupName").focus();
}

$("createNodeGroupBtn").addEventListener("click", showNodeGroupForm);
$("cancelNodeGroupBtn").addEventListener("click", () => $("nodeGroupForm").classList.add("hidden"));
$("nodeGroupForm").addEventListener("submit", (event) => {
  event.preventDefault();
  runTask("保存分组", async () => {
    const tags = selectedNodeTags();
    if (!tags.length) throw new Error("请先在当前页选择要加入分组的节点");
    await request("/api/groups", {
      method: "POST",
      body: JSON.stringify({ name: $("nodeGroupName").value.trim(), kind: $("nodeGroupKind").value, node_tags: tags })
    });
    $("nodeGroupForm").reset();
    $("nodeGroupForm").classList.add("hidden");
    await refresh();
    return `已创建分组，包含 ${tags.length} 个节点`;
  });
});

async function refreshSource(sourceId) {
  await request(`/api/subscriptions/${encodeURIComponent(sourceId)}/refresh`, { method: "POST", body: "{}" });
  await refresh();
}

$("nodeGroupList").addEventListener("click", (event) => {
  const open = event.target.closest("[data-node-group-open]");
  if (open) return openNodeGroup(open.dataset.nodeGroupOpen);
  const remove = event.target.closest("[data-node-group-delete]");
  if (remove) confirmTask("删除分组", "删除分组不会删除其中的节点。", async () => {
    await request(`/api/groups/${encodeURIComponent(remove.dataset.nodeGroupDelete)}`, { method: "DELETE", body: "{}" });
    if (activeNodeGroupId === remove.dataset.nodeGroupDelete) activeNodeGroupId = "";
    await refresh();
  });
});

$("nodeTable").addEventListener("click", (event) => {
  const open = event.target.closest("[data-node-group-open]");
  if (open) return openNodeGroup(open.dataset.nodeGroupOpen);
  const assignGroup = event.target.closest("[data-node-group-assign]");
  if (assignGroup) return assignNodeGroup(assignGroup.dataset.nodeGroupAssign);
  const removeGroup = event.target.closest("[data-node-group-delete]");
  if (removeGroup) return confirmTask("删除分组", "删除分组不会删除其中的节点。", async () => {
    await request(`/api/groups/${encodeURIComponent(removeGroup.dataset.nodeGroupDelete)}`, { method: "DELETE", body: "{}" });
    await refresh();
  });
  const refreshButton = event.target.closest("[data-source-refresh]");
  if (refreshButton) return runTask("刷新订阅", async () => {
    await refreshSource(refreshButton.dataset.sourceRefresh);
    return "订阅已刷新";
  });
  const openSource = event.target.closest("[data-source-open]");
  if (openSource) return openNodeGroup(openSource.dataset.sourceOpen);
  const deleteSource = event.target.closest("[data-source-delete]");
  if (deleteSource) return confirmTask("移除订阅来源", "移除来源不会删除已经导入的节点。", async () => {
    await request(`/api/subscriptions/${encodeURIComponent(deleteSource.dataset.sourceDelete)}`, { method: "DELETE", body: "{}" });
    await refresh();
  });
  const pageButton = event.target.closest("[data-node-page]");
  if (pageButton && !pageButton.disabled) {
    nodePagination.page = Number(pageButton.dataset.nodePage);
    selectedNodePageTags.clear();
    return refreshNodeWorkspace().catch((error) => showNotice(error.message, "bad"));
  }
});

// Event delegation for node table — avoids re-attaching listeners on every render
$("nodeTable").addEventListener("change", (event) => {
  const target = event.target;
  if (target.id === "checkAllNodes") {
    document.querySelectorAll(".node-check").forEach((box) => {
      box.checked = target.checked;
      if (target.checked) selectedNodePageTags.add(box.dataset.tag);
      else selectedNodePageTags.delete(box.dataset.tag);
    });
  }
  if (target.classList.contains("node-check")) {
    if (target.checked) selectedNodePageTags.add(target.dataset.tag);
    else selectedNodePageTags.delete(target.dataset.tag);
  }
});

$("assignTable").addEventListener("change", (event) => {
  const target = event.target;
  if (target.id === "checkAllAssign") {
    document.querySelectorAll(".assign-check").forEach((box) => {
      box.checked = target.checked;
      assignDraftChecks.set(box.dataset.tag, target.checked);
    });
  }
  if (target.classList.contains("assign-check")) {
    assignDraftChecks.set(target.dataset.tag, target.checked);
  }
  if (target.id === "checkAllAssign" || target.classList.contains("assign-check")) {
    syncAssignSelectionControl();
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
  let nextPort = Number($("startPort").value || 8001);
  const usedPorts = new Set();
  // Exclude every already-owned public port, not just values visible in the
  // current DOM page (pagination / unsaved drafts otherwise collide).
  Object.keys(ports).forEach((port) => usedPorts.add(Number(port)));
  pools.forEach((pool) => {
    if (pool?.listen_port) usedPorts.add(Number(pool.listen_port));
  });
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
      if (!input.value) {
        box.checked = true;
        assignDraftChecks.set(box.dataset.tag, true);
      }
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
    assignDraftPorts.set(box.dataset.tag, String(allocation.ports[index]));
    usedPorts.add(allocation.ports[index]);
  });
  const skipped = allocation.skipped || {};
  const skippedEntries = Object.entries(skipped);
  if (skippedEntries.length) {
    const detail = skippedEntries
      .slice(0, 5)
      .map(([port, info]) => `${port}（${info.label || info.reason || "不可用"}）`)
      .join("、");
    const more = skippedEntries.length > 5 ? `等 ${skippedEntries.length} 个` : "";
    return `已分配 ${targets.length} 个可用端口，已跳过 ${detail}${more}`;
  }
  return `已分配 ${targets.length} 个可用端口`;
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
  clearAssignDrafts();
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

function attachPortInputListeners() {
  document.querySelectorAll(".port-input").forEach((input) => {
    input.addEventListener("blur", () => checkPortInputAvailability(input));
    input.addEventListener("input", () => {
      // Record the draft so poll-driven re-renders keep the user's edit.
      assignDraftPorts.set(input.dataset.portFor, input.value);
      input.classList.remove("port-busy");
      const hint = input.parentElement.querySelector(".port-hint");
      if (hint) hint.remove();
    });
  });
}

async function checkPortInputAvailability(input) {
  const port = parseInt(input.value, 10);
  if (!port || port < 1024 || port > 65535) {
    input.classList.remove("port-busy");
    const hint = input.parentElement.querySelector(".port-hint");
    if (hint) hint.remove();
    return;
  }
  if (portAvailabilityCache[port] !== undefined) {
    applyPortAvailability(input, port, portAvailabilityCache[port]);
    return;
  }
  input.classList.remove("port-busy");
  const hint = input.parentElement.querySelector(".port-hint");
  if (hint) hint.remove();
  if (_portCheckTimer) clearTimeout(_portCheckTimer);
  _portCheckTimer = setTimeout(async () => {
    try {
      const result = await request("/api/ports/check", {
        method: "POST",
        body: JSON.stringify({ ports: [port] }),
      });
      const info = result.ports?.[String(port)];
      if (info) {
        portAvailabilityCache[port] = info;
        applyPortAvailability(input, port, info);
      }
    } catch (e) { /* ignore */ }
  }, 200);
}

function applyPortAvailability(input, port, info) {
  const hint = input.parentElement.querySelector(".port-hint");
  if (info.available) {
    input.classList.remove("port-busy");
    if (hint) hint.remove();
    return;
  }
  input.classList.add("port-busy");
  const label = info.label || info.reason || "不可用";
  if (hint) {
    hint.textContent = label;
  } else {
    const el = document.createElement("span");
    el.className = "port-hint";
    el.textContent = label;
    input.parentElement.appendChild(el);
  }
}

async function preCheckAllPorts() {
  const mappings = collectMappings();
  const portList = Object.keys(mappings).map(Number);
  if (!portList.length) return { ok: true, mappings };
  const currentPorts = new Set(Object.keys(ports).map(String));
  try {
    const result = await request("/api/ports/check", {
      method: "POST",
      body: JSON.stringify({ ports: portList }),
    });
    const busyPorts = [];
    for (const [portText, info] of Object.entries(result.ports || {})) {
      if (info.available) continue;
      // Re-saving an existing mapping must not be treated as a conflict.
      if (
        currentPorts.has(String(portText))
        && (info.reason === "project-listening" || info.reason === "project-mapped")
      ) {
        continue;
      }
      busyPorts.push({ port: Number(portText), ...info });
      portAvailabilityCache[Number(portText)] = info;
    }
    if (busyPorts.length) {
      const detail = busyPorts
        .slice(0, 10)
        .map((item) => `${item.port}（${item.label || item.reason}）`)
        .join("，");
      return { ok: false, error: `以下端口不可用：${detail}。请更换后再保存。`, busyPorts };
    }
  } catch (e) {
    /* 如果检查失败，继续保存 */
  }
  return { ok: true, mappings };
}

$("saveAssignBtn").addEventListener("click", () => runTask("保存端口映射", async () => {
  const check = await preCheckAllPorts();
  if (!check.ok) {
    document.querySelectorAll(".port-input").forEach((input) => {
      const port = parseInt(input.value, 10);
      if (port && check.busyPorts.some((item) => item.port === port)) {
        input.classList.add("port-busy");
        const info = check.busyPorts.find((item) => item.port === port);
        const label = info?.label || info?.reason || "不可用";
        let hint = input.parentElement.querySelector(".port-hint");
        if (!hint) {
          hint = document.createElement("span");
          hint.className = "port-hint";
          input.parentElement.appendChild(hint);
        }
        hint.textContent = label;
      }
    });
    throw new Error(check.error);
  }
  const result = await saveMappings(check.mappings);
  await refresh();
  if (result.engine_restarted) return "保存端口映射完成，已自动重启引擎";
  if (result.engine_stopped) return "保存端口映射完成，已停止引擎";
  return "保存端口映射完成";
}));

$("engineToggleBtn").addEventListener("click", () => {
  const running = Boolean(statusSnapshot.running);
  return runTask(running ? "暂停引擎" : "启动引擎", async () => {
    await request(running ? "/api/stop" : "/api/start", { method: "POST", body: "{}" });
    await refreshStatusOnly();
  });
});

$("repairEngineBtn").addEventListener("click", () => runTask("重启修复引擎", async () => {
  await request("/api/stop", { method: "POST", body: "{}" });
  await request("/api/start", { method: "POST", body: "{}" });
  await refresh();
  const missingPorts = statusSnapshot.missing_ports || [];
  const failedListeners = statusSnapshot.failed_listeners || [];
  if (failedListeners.length) {
    const skipped = failedListeners.map((item) => String(item.listen || "").split(":").pop()).join(", ");
    return `引擎已重启，端口被跳过：${skipped}（已被其他程序占用）`;
  }
  if (missingPorts.length) return `引擎已重启，仍缺失端口：${missingPorts.join(", ")}`;
  return "引擎已重启，端口监听正常";
}));

$("checkSingBoxUpdateBtn").addEventListener("click", () => runTask("检测 sing-box 更新", async () => {
  const info = await loadSingBoxInfo(true);
  return info.update_available
    ? `发现新版本 v${info.latest_version}`
    : `当前已是最新版本 v${info.current_version}`;
}));

$("updateSingBoxBtn").addEventListener("click", () => runTask("更新 sing-box", async () => {
  if (!confirm("更新时将暂停并在完成后恢复 sing-box，是否继续？")) return "已取消更新";
  const result = await request("/api/engine/update", { method: "POST", body: "{}" });
  await loadSingBoxInfo(true);
  await refreshStatusOnly();
  return result.updated
    ? `sing-box 已从 v${result.previous_version} 更新到 v${result.current_version}`
    : `当前已是最新版本 v${result.current_version}`;
}));

$("rollbackSingBoxBtn").addEventListener("click", () => runTask("回退 sing-box", async () => {
  if (!confirm("将当前 sing-box 与备份版本交换，是否继续？")) return "已取消回退";
  const result = await request("/api/engine/rollback", { method: "POST", body: "{}" });
  await loadSingBoxInfo(true);
  await refreshStatusOnly();
  return `sing-box 已回退到 v${result.current_version}`;
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
  return result.fail ? `自检发现 ${result.fail} 个失败项` : "自检完成";
}));

$("localProxyCheckBtn").addEventListener("click", () => runTask("本地检测", async () => {
  const port = Number($("localProxyCheckPort").value || 0);
  if (!port) throw new Error("没有可检测端口");
  renderLocalProxyCheckProgress(`检测端口 ${port}`);
  await runLocalProxyCheckForPort(port);
  renderLocalProxyCheckProgress();
  return `本地检测完成：${port}`;
}));

$("localProxyCheckAllBtn").addEventListener("click", () => runTask("全部检测", async () => {
  const portList = Object.keys(ports).sort((left, right) => Number(left) - Number(right));
  if (!portList.length) throw new Error("没有可检测端口");
  renderLocalProxyCheckProgress(`检测中 0/${portList.length}`);
  const result = await runLocalProxyCheckForPorts(portList, 3);
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
