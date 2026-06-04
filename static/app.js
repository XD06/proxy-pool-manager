let nodes = [];
let ports = {};
let statusSnapshot = {};
let testingTags = new Set();
let validationDetails = {};
const ACTIVE_TAB_KEY = "proxyPoolManager.activeTab";

const DEFAULT_VALIDATION_URLS = [
  "https://ipv4.webshare.io/",
  "https://www.google.com/generate_204",
  "https://www.gstatic.com/generate_204",
  "https://www.cloudflare.com/cdn-cgi/trace"
];

const $ = (id) => document.getElementById(id);

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[char]);
}

function showNotice(message, tone = "info") {
  const notice = $("notice");
  if (!message) {
    notice.className = "notice hidden";
    notice.textContent = "";
    return;
  }
  notice.className = `notice ${tone}`;
  notice.textContent = message;
}

function latencyText(latency) {
  if (!latency) return "未测";
  if (!latency.alive) return latency.error || "不可用";
  return `${latency.delay ?? "-"}ms`;
}

function latencyClass(latency) {
  const delay = latency?.delay;
  if (!latency || !latency.alive || typeof delay !== "number") return "latency-unknown";
  if (delay < 3000) return "latency-fast";
  if (delay <= 8000) return "latency-mid";
  return "latency-slow";
}

function targetResponseText(latency) {
  if (!latency) return "-";
  if (latency.target_url) {
    if (!latency.alive && latency.error) return latency.error;
    const status = latency.status_code ? `HTTP ${latency.status_code}` : "无状态码";
    const preview = latency.body_preview ? ` · ${latency.body_preview}` : "";
    return `${status}${preview}`;
  }
  if (latency.exit_ip) return `出口 ${latency.exit_ip}`;
  return latency.error || "-";
}

function statusBadge(latency) {
  if (!latency) return `<span class="badge idle">未测</span>`;
  if (latency.alive) return `<span class="badge ok">可用</span>`;
  return `<span class="badge bad">失败</span>`;
}

function nodeStatusBadge(node) {
  if (testingTags.has(node.tag)) return `<span class="badge testing">测速中</span>`;
  return statusBadge(node.latency);
}

function renderSummary() {
  if (!statusSnapshot.running) {
    $("engineState").textContent = "未运行";
  } else if (statusSnapshot.config_matches_state === false) {
    const configured = statusSnapshot.config_ports?.length || 0;
    const expected = statusSnapshot.expected_ports?.length || 0;
    $("engineState").textContent = `配置不一致 #${statusSnapshot.pid} ${configured}/${expected}`;
  } else if (statusSnapshot.ready === false) {
    const listening = statusSnapshot.listening_ports?.length || 0;
    const expected = statusSnapshot.expected_ports?.length || 0;
    $("engineState").textContent = `启动中 #${statusSnapshot.pid} ${listening}/${expected}`;
  } else {
    $("engineState").textContent = `运行中 #${statusSnapshot.pid}`;
  }
  $("nodeCount").textContent = String(statusSnapshot.node_count ?? nodes.length);
  $("mappingCount").textContent = String(statusSnapshot.mapping_count ?? Object.keys(ports).length);
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
  if (!nodes.length) {
    $("nodeTable").innerHTML = `<div class="empty">还没有节点。先在导入页粘贴订阅或单节点链接。</div>`;
    return;
  }
  $("nodeTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th><input id="checkAllNodes" type="checkbox" checked></th>
          <th>节点</th>
          <th>协议</th>
          <th>服务器</th>
          <th>状态</th>
          <th>延迟</th>
          <th>目标结果</th>
        </tr>
      </thead>
      <tbody>${nodes.map((node) => `
        <tr>
          <td data-label="选择"><input type="checkbox" class="node-check" data-tag="${escapeHtml(node.tag)}" checked></td>
          <td data-label="节点">
            <strong>${escapeHtml(node.name)}</strong>
            <small>${escapeHtml(node.tag)}</small>
          </td>
          <td data-label="协议">${escapeHtml(node.type)}</td>
          <td data-label="服务器"><span class="mono">${escapeHtml(node.server)}:${node.server_port}</span></td>
          <td data-label="状态">${nodeStatusBadge(node)}</td>
          <td data-label="延迟"><span class="latency-pill ${latencyClass(node.latency)}">${escapeHtml(latencyText(node.latency))}</span></td>
          <td data-label="目标结果" class="result-preview">${escapeHtml(targetResponseText(node.latency))}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
  $("checkAllNodes").addEventListener("change", (event) => {
    document.querySelectorAll(".node-check").forEach((box) => {
      box.checked = event.target.checked;
    });
  });
}

function renderAssignTable() {
  if (!nodes.length) {
    $("assignTable").innerHTML = `<div class="empty">没有可分配节点。</div>`;
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
          <th>状态</th>
        </tr>
      </thead>
      <tbody>${nodes.map((node) => {
        const assignedPort = Object.entries(ports).find(([, item]) => item.node_tag === node.tag)?.[0] || "";
        const checked = assignedPort || node.latency?.alive ? "checked" : "";
        return `<tr>
          <td data-label="使用"><input type="checkbox" class="assign-check" data-tag="${escapeHtml(node.tag)}" ${checked}></td>
          <td data-label="端口"><input class="port-input" type="number" data-port-for="${escapeHtml(node.tag)}" value="${assignedPort}" min="1024" max="65535"></td>
          <td data-label="节点">${escapeHtml(node.name)}</td>
          <td data-label="出口 IP" class="mono">${escapeHtml(node.latency?.exit_ip || "-")}</td>
          <td data-label="状态">${statusBadge(node.latency)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
}

function renderPortsTable() {
  const entries = sortedPortEntries();
  const curlTarget = activeValidationUrl();
  if (!entries.length) {
    $("portsTable").innerHTML = `<div class="empty">还没有端口映射。先到分配页保存端口。</div>`;
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
          <th>出口 IP</th>
          <th>curl 验证</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>${entries.map(([port, item]) => {
        const authority = proxyAuthority(port);
        const httpProxy = `http://${authority}/`;
        const socksProxy = `socks5://${authority}`;
        const socksCurlProxy = `socks5h://${authority}`;
        const curlCommand = `curl --proxy "http://${authority}/" ${curlTarget}`;
        return `
        <tr>
          <td data-label="端口" class="mono copyable" data-copy="${escapeHtml(httpProxy)}" data-copy-label="HTTP 代理" title="copy ${escapeHtml(httpProxy)}">${port}</td>
          <td data-label="节点">${escapeHtml(item.node_name || item.node_tag)}</td>
          <td data-label="协议">${escapeHtml(item.type || "-")}</td>
          <td data-label="状态">${statusBadge(item.latency)}</td>
          <td data-label="延迟"><span class="latency-pill ${latencyClass(item.latency)}">${escapeHtml(latencyText(item.latency))}</span></td>
          <td data-label="验证结果" class="result-preview">${escapeHtml(targetResponseText(item.latency))}</td>
          <td data-label="出口 IP" class="mono" id="ip-${port}">${escapeHtml(item.exit_ip || item.latency?.exit_ip || "-")}</td>
          <td data-label="curl"><code class="copyable" data-copy="${escapeHtml(curlCommand)}" data-copy-label="curl 命令" title="copy curl 命令">${escapeHtml(curlCommand)}</code></td>
          <td data-label="操作">
            <button data-ip-port="${port}">查出口</button>
            <button data-validate-port="${port}">验证</button>
            <button data-copy="${escapeHtml(socksProxy)}" data-copy-label="标准 SOCKS5">复制 SOCKS</button>
            <button data-copy="${escapeHtml(socksCurlProxy)}" data-copy-label="curl SOCKS5H">curl SOCKS</button>
          </td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
  document.querySelectorAll("[data-ip-port]").forEach((button) => {
    button.addEventListener("click", async () => {
      const port = button.dataset.ipPort;
      await runTask(`查询端口 ${port} 出口 IP`, async () => {
        const result = await request(`/api/ports/${port}/ip`);
        $("ip-" + port).textContent = result.exit_ip || result.error || "失败";
        showQuickResult(`端口 ${port}`, result.exit_ip ? `出口 IP：${result.exit_ip}` : `失败：${result.error || "未知错误"}`, Boolean(result.exit_ip));
        await refresh();
      });
    });
  });
  document.querySelectorAll("[data-validate-port]").forEach((button) => {
    button.addEventListener("click", async () => {
      const port = Number(button.dataset.validatePort);
      await runSinglePortValidation(port);
    });
  });
  document.querySelectorAll("[data-copy]").forEach((item) => {
    item.addEventListener("click", () => {
      copyText(item.dataset.copy, item.dataset.copyLabel || "内容");
    });
  });
  renderValidationResults();
}

function sortedPortEntries() {
  return Object.entries(ports).sort(([leftPort, left], [rightPort, right]) => {
    const leftLatency = left.latency;
    const rightLatency = right.latency;
    const leftRank = !leftLatency ? 2 : (leftLatency.alive ? 0 : 1);
    const rightRank = !rightLatency ? 2 : (rightLatency.alive ? 0 : 1);
    if (leftRank !== rightRank) return leftRank - rightRank;
    const leftDelay = leftLatency?.delay ?? Number.MAX_SAFE_INTEGER;
    const rightDelay = rightLatency?.delay ?? Number.MAX_SAFE_INTEGER;
    if (leftDelay !== rightDelay) return leftDelay - rightDelay;
    return Number(leftPort) - Number(rightPort);
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
    showQuickResult("已复制", `${label}：${text}`, true);
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
      </header>
      <table>
        <thead>
          <tr>
            <th>目标</th>
            <th>结果</th>
            <th>状态</th>
            <th>耗时</th>
            <th>响应 / 错误</th>
          </tr>
        </thead>
        <tbody>${(detail.targets || []).map((target) => `
          <tr>
            <td data-label="目标" class="mono">${escapeHtml(target.url)}</td>
            <td data-label="结果">${target.ok ? '<span class="badge ok">成功</span>' : '<span class="badge bad">失败</span>'}</td>
            <td data-label="状态">${escapeHtml(target.status_code || "-")}</td>
            <td data-label="耗时">${escapeHtml(target.elapsed_ms || "-")}ms</td>
            <td data-label="响应 / 错误" class="result-preview">${escapeHtml(target.body_preview || target.error || "-")}</td>
          </tr>
        `).join("")}</tbody>
      </table>
    </article>
  `).join("");
}

async function refresh() {
  const [status, nodeData, portData] = await Promise.all([
    request("/api/status"),
    request("/api/nodes"),
    request("/api/ports")
  ]);
  statusSnapshot = status;
  nodes = nodeData.nodes;
  ports = portData.ports;
  renderSummary();
  renderNodeTable();
  renderAssignTable();
  renderPortsTable();
}

async function refreshStatusOnly() {
  statusSnapshot = await request("/api/status");
  renderSummary();
}

async function runTask(label, task) {
  showNotice(`${label}...`);
  try {
    await task();
    showNotice(`${label}完成`, "ok");
  } catch (error) {
    showNotice(error.message, "bad");
  }
}

async function runNodeTest(pruneSameIp, overrideTags = null) {
  const tags = overrideTags || selectedNodeTags();
  const targetUrl = $("nodeTestUrl").value.trim() || null;
  testingTags = new Set(tags);
  renderNodeTable();
  showNotice(targetUrl ? `已开始测速目标 URL：${targetUrl}` : `已开始测速：0/${tags.length}`);
  try {
    const started = await request("/api/test/start", {
      method: "POST",
      body: JSON.stringify({ node_tags: tags, prune_same_ip: pruneSameIp, target_url: targetUrl })
    });
    await pollTestJob(started.id);
  } catch (error) {
    testingTags.clear();
    renderNodeTable();
    showNotice(error.message, "bad");
  }
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
      await refresh();
      const removed = job.removed?.length ? `；已去重：${job.removed.join("；")}` : "";
      showNotice(`测速完成：${job.completed}/${job.total}${removed}`, "ok");
      return;
    }
    if (job.status === "error") {
      testingTags.clear();
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
  const mappings = {};
  document.querySelectorAll(".assign-check").forEach((box) => {
    if (!box.checked) return;
    const tag = box.dataset.tag;
    const port = document.querySelector(`[data-port-for="${CSS.escape(tag)}"]`).value;
    if (port) mappings[port] = tag;
  });
  return mappings;
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
    renderPortsTable();
    renderValidationResults();
  } catch (error) {
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
    if (ports[port] && detail.exit_ip) ports[port].exit_ip = detail.exit_ip;
  });
}

async function runAllPortValidation() {
  setPortValidationRunning(true);
  try {
    validationDetails = {};
    renderValidationResults();
    renderPortsTable();
    const urls = validationUrls();
    showQuickResult("验证全部端口", `验证中；目标：${urls.join("，")}；进度 0/${Object.keys(ports).length}`);
    const started = await request("/api/test-ports/start", {
      method: "POST",
      body: JSON.stringify({ urls })
    });
    await pollPortTestJob(started.id, urls);
  } finally {
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
      const details = Object.values(validationDetails);
      const total = job.total || details.length;
      const ok = details.filter((detail) => (detail.targets || []).some((target) => target.ok)).length;
      renderPortsTable();
      renderValidationResults();
      showQuickResult("验证全部端口", `可用 ${ok}/${total}；目标：${urls.join("，")}`, ok > 0);
      return;
    }
    if (job.status === "error") {
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
    http_proxy: `http://${proxyAuthority(port)}/`,
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
  if (format === "csv") {
    const header = ["host", "port", "node", "type", "exit_ip", "http_proxy", "socks5_proxy", "socks5h_proxy"];
    const lines = rows.map((row) => header.map((key) => `"${String(row[key]).replace(/"/g, '""')}"`).join(","));
    return [header.join(","), ...lines].join("\n");
  }
  return rows.map((row) => `${row.http_proxy} # ${row.node} ${row.exit_ip}`).join("\n");
}

function activateTab(tabId, persist = true) {
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

activateTab(localStorage.getItem(ACTIVE_TAB_KEY) || "import", false);

$("importUrlBtn").addEventListener("click", () => runTask("导入订阅", async () => {
  await request("/api/import", { method: "POST", body: JSON.stringify({ url: $("urlInput").value }) });
  await refresh();
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

$("testPruneBtn").addEventListener("click", () => runNodeTest(true));

$("selectAliveBtn").addEventListener("click", () => {
  document.querySelectorAll(".node-check").forEach((box) => {
    const node = nodes.find((item) => item.tag === box.dataset.tag);
    box.checked = Boolean(node?.latency?.alive);
  });
});

$("deleteSelectedBtn").addEventListener("click", () => runTask("删除选中节点", async () => {
  const tags = selectedNodeTags();
  if (!tags.length) throw new Error("没有选中节点");
  await request("/api/nodes/delete", {
    method: "POST",
    body: JSON.stringify({ node_tags: tags })
  });
  await refresh();
}));

$("deleteFailedBtn").addEventListener("click", () => runTask("删除测试失败节点", async () => {
  const tags = nodes.filter((node) => node.latency && !node.latency.alive).map((node) => node.tag);
  if (!tags.length) throw new Error("没有测试失败节点");
  await request("/api/nodes/delete", {
    method: "POST",
    body: JSON.stringify({ node_tags: tags })
  });
  await refresh();
}));

$("clearNodesBtn").addEventListener("click", () => runTask("清空节点", async () => {
  await request("/api/nodes/delete", {
    method: "POST",
    body: JSON.stringify({ all: true })
  });
  await refresh();
}));

$("autoAssignBtn").addEventListener("click", () => {
  let nextPort = Number($("startPort").value || 8001);
  const usedPorts = new Set();
  document.querySelectorAll(".port-input").forEach((input) => {
    if (input.value) usedPorts.add(Number(input.value));
  });
  document.querySelectorAll(".assign-check").forEach((box) => {
    if (!box.checked) return;
    const input = document.querySelector(`[data-port-for="${CSS.escape(box.dataset.tag)}"]`);
    if (input.value) return;
    while (usedPorts.has(nextPort)) nextPort++;
    input.value = nextPort;
    usedPorts.add(nextPort);
    nextPort++;
  });
});

$("saveAssignBtn").addEventListener("click", () => runTask("保存端口映射", async () => {
  await request("/api/assign", { method: "PUT", body: JSON.stringify({ mappings: collectMappings() }) });
  await refresh();
}));

$("startBtn").addEventListener("click", () => runTask("启动引擎", async () => {
  await request("/api/start", { method: "POST", body: "{}" });
  await refreshStatusOnly();
}));

$("stopBtn").addEventListener("click", () => runTask("停止引擎", async () => {
  await request("/api/stop", { method: "POST", body: "{}" });
  await refreshStatusOnly();
}));

$("testPortsBtn").addEventListener("click", () => runTask("验证全部端口", runAllPortValidation));

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

refresh().catch((error) => showNotice(error.message, "bad"));
setInterval(() => {
  if (document.hidden) return;
  refreshStatusOnly().catch(() => {});
}, 15000);
