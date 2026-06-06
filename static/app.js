let nodes = [];
let ports = {};
let statusSnapshot = {};
let testingTags = new Set();
let validationDetails = {};
let autoSelectAliveForAssign = true;
let assignFilterTags = null;
let validatingPorts = new Set();
let proxyAdminResults = {};
let proxyAdminImported = [];
let proxyAdminConfigLoaded = false;
const ACTIVE_TAB_KEY = "proxyPoolManager.activeTab";

const DEFAULT_VALIDATION_URLS = [
  "http://cp.cloudflare.com/generate_204",
  "https://www.google.com/generate_204",
  "https://www.gstatic.com/generate_204",
  "https://www.cloudflare.com/cdn-cgi/trace"
];
const EXIT_IP_CHECK_URL = "https://ipv4.webshare.io/";
const GEOIP_CHIP_STYLE = "display:inline-block;max-width:150px;overflow:hidden;color:#3f5e52;font-size:12px;text-overflow:ellipsis;vertical-align:middle;white-space:nowrap";

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
  if (!latency.alive) return "失败";
  return `${latency.delay ?? "-"}ms`;
}

function latencyTitle(latency) {
  if (!latency) return "未测速";
  if (!latency.alive) return latency.error || "不可用";
  return targetResponseText(latency);
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
  if (latency.target_results?.length > 1) {
    const okItems = latency.target_results.filter((item) => item.ok);
    const failed = latency.target_results.length - okItems.length;
    const detail = latency.target_results
      .map((item) => `${compactUrl(item.url)} ${item.ok ? item.elapsed_ms + "ms" : "失败"}`)
      .join("；");
    const status = `成功 ${okItems.length}/${latency.target_results.length}`;
    const average = latency.delay ? ` · 平均 ${latency.delay}ms` : "";
    const failedText = failed ? ` · 失败 ${failed}` : "";
    return `${status}${average}${failedText} · ${detail}`;
  }
  if (latency.target_url) {
    if (!latency.alive && latency.error) return latency.error;
    const status = latency.status_code ? `HTTP ${latency.status_code}` : "无状态码";
    const preview = latency.body_preview ? ` · ${latency.body_preview}` : "";
    return `${status}${preview}`;
  }
  if (latency.exit_ip) return `出口 ${latency.exit_ip}`;
  return latency.error || "-";
}

function geoIpSummary(geoip) {
  if (!geoip) return "";
  if (geoip.summary) return geoip.summary;
  if (geoip.error) return "地区未知";
  return [
    geoip.country_code || geoip.country,
    geoip.city || geoip.region,
    geoip.asn,
    geoip.org || geoip.isp
  ].filter(Boolean).join(" · ");
}

function geoIpCompact(geoip) {
  if (!geoip) return "";
  if (geoip.compact) return geoip.compact;
  if (geoip.error) return "未知";
  const org = String(geoip.org || geoip.isp || "")
    .replace("Corporation", "")
    .replace("Limited", "")
    .replace("Ltd.", "")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .join(" ");
  return [
    geoip.country_code || geoip.country,
    geoip.city || geoip.region,
    org
  ].filter(Boolean).join(" · ");
}

function geoIpForItem(item) {
  return item?.geoip || item?.latency?.geoip || null;
}

function geoIpText(item) {
  return geoIpCompact(geoIpForItem(item)) || "-";
}

function geoIpTitle(item) {
  return geoIpSummary(geoIpForItem(item)) || geoIpText(item);
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
  box.innerHTML = `
    <div class="overview-metric">
      <span>总节点</span>
      <strong>${stats.total}</strong>
    </div>
    <div class="overview-metric ok">
      <span>可用</span>
      <strong>${stats.alive}</strong>
    </div>
    <div class="overview-metric bad">
      <span>失败</span>
      <strong>${stats.failed}</strong>
    </div>
    <div class="overview-metric idle">
      <span>未测</span>
      <strong>${stats.untested}</strong>
    </div>
    <div class="overview-metric testing">
      <span>测速中</span>
      <strong>${stats.testing}</strong>
    </div>
    <div class="overview-metric">
      <span>平均延迟</span>
      <strong>${stats.avgDelay === null ? "-" : stats.avgDelay + "ms"}</strong>
    </div>
    <div class="overview-target" title="${escapeHtml(targetText)}">
      <span>本次目标</span>
      <strong>${escapeHtml(targetText)}</strong>
    </div>
    <div class="overview-metric">
      <span>地区模式</span>
      <strong>${escapeHtml(geoText)}</strong>
    </div>
  `;
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


function renderSummary() {
  const expectedPorts = statusSnapshot.expected_ports || [];
  const listeningPorts = statusSnapshot.listening_ports || [];
  const missingPorts = expectedPorts.filter((port) => !listeningPorts.includes(port));
  if (!statusSnapshot.running) {
    $("engineState").textContent = "未运行";
    $("engineState").style.color = "#999";              // 灰色
  } else if (statusSnapshot.config_matches_state === false) {
    const configured = statusSnapshot.config_ports?.length || 0;
    const expected = statusSnapshot.expected_ports?.length || 0;
    $("engineState").textContent = `配置不一致restart #${statusSnapshot.pid} ${configured}/${expected}`;
    $("engineState").style.color = "#e74c3c";            // 红色
  } else if (statusSnapshot.ready === false) {
    const listening = statusSnapshot.listening_ports?.length || 0;
    const expected = statusSnapshot.expected_ports?.length || 0;
    $("engineState").textContent = `启动中 #${statusSnapshot.pid} ${listening}/${expected}`;
    $("engineState").style.color = "#3498db";            // 蓝色
  } else {
    $("engineState").textContent = `运行中 #${statusSnapshot.pid}`;
    $("engineState").style.color = "#2ecc71";            // 绿色
  }
  $("nodeCount").textContent = String(statusSnapshot.node_count ?? nodes.length);
  $("nodeCount").style.color = $("engineState").style.color; // 节点数大于0显示绿色，否则红色
  $("mappingCount").textContent = String(statusSnapshot.mapping_count ?? Object.keys(ports).length);
  $("mappingCount").style.color = $("engineState").style.color; // 映射数大于0显示绿色，否则红色
  $("listeningCount").textContent = `${listeningPorts.length}/${expectedPorts.length}`;
  $("listeningCount").style.color = missingPorts.length ? "#e74c3c" : $("engineState").style.color;
  $("listeningCount").title = missingPorts.length
    ? `异常端口：${missingPorts.join(", ")}`
    : "所有映射端口均在监听";
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
          <th>出口 IP</th>
          <th>地区</th>
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
          <td data-label="延迟"><span class="latency-pill ${latencyClass(node.latency)}" title="${escapeHtml(latencyTitle(node.latency))}">${escapeHtml(latencyText(node.latency))}</span></td>
          <td data-label="出口 IP" class="mono">${escapeHtml(node.latency?.exit_ip || "-")}</td>
          <td data-label="地区"><span class="geoip-chip" style="${GEOIP_CHIP_STYLE}" title="${escapeHtml(geoIpTitle(node.latency))}">${escapeHtml(geoIpText(node.latency))}</span></td>
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
          <th>地区</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>${assignNodes.map(({ node, assignedPort }) => {
        const checked = assignedPort || (autoSelectAliveForAssign && node.latency?.alive) ? "checked" : "";
        return `<tr>
          <td data-label="使用"><input type="checkbox" class="assign-check" data-tag="${escapeHtml(node.tag)}" ${checked}></td>
          <td data-label="端口"><input class="port-input" type="number" data-port-for="${escapeHtml(node.tag)}" value="${assignedPort}" min="1024" max="65535"></td>
          <td data-label="节点">${escapeHtml(node.name)}</td>
          <td data-label="出口 IP" class="mono">${escapeHtml(node.latency?.exit_ip || "-")}</td>
          <td data-label="地区"><span class="geoip-chip" style="${GEOIP_CHIP_STYLE}" title="${escapeHtml(geoIpTitle(node.latency))}">${escapeHtml(geoIpText(node.latency))}</span></td>
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
          <th>ProxyAdmin</th>
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
          <td data-label="协议">${escapeHtml(item.type || "-")}</td>
          <td data-label="状态">${validatingPorts.has(String(port)) ? '<span class="badge testing">验证中</span>' : statusBadge(item.latency)}</td>
          <td data-label="延迟"><span class="latency-pill ${latencyClass(item.latency)}" title="${escapeHtml(latencyTitle(item.latency))}">${escapeHtml(latencyText(item.latency))}</span></td>
          <td data-label="验证结果" class="result-preview">${escapeHtml(targetResponseText(item.latency))}</td>
          <td data-label="ProxyAdmin">${proxyAdminPortSummary(port)}</td>
          <td data-label="出口 IP" class="mono" id="ip-${port}">${escapeHtml(item.exit_ip || item.latency?.exit_ip || "-")}</td>
          <td data-label="地区"><span class="geoip-chip" style="${GEOIP_CHIP_STYLE}" id="geo-${port}" title="${escapeHtml(geoIpTitle(item))}">${escapeHtml(geoIpText(item))}</span></td>
          <td data-label="curl"><code class="copyable" data-copy="${escapeHtml(curlCommand)}" data-copy-label="curl 命令" title="copy curl 命令">${escapeHtml(curlCommand)}</code></td>
          <td data-label="操作">
            <button data-ip-port="${port}">查出口</button>
            <button data-validate-port="${port}">验证</button>
            <button data-remove-port="${port}">移除映射</button>
            <button data-copy="${escapeHtml(socksProxy)}" data-copy-label="标准 SOCKS5">复制 SOCKS</button>
            <button data-remove-proxy-admin-port="${port}">移除代理</button>
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
        const geoCell = $("geo-" + port);
        if (geoCell) {
          geoCell.textContent = result.geoip_compact || geoIpCompact(result.geoip) || "-";
          geoCell.title = result.geoip_summary || geoIpSummary(result.geoip) || geoCell.textContent;
        }
        showQuickResult(
          `端口 ${port}`,
          result.exit_ip ? `出口 IP：${result.exit_ip}；地区：${result.geoip_compact || geoIpCompact(result.geoip) || "-"}` : `失败：${result.error || "未知错误"}`,
          Boolean(result.exit_ip)
        );
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
  document.querySelectorAll("[data-remove-port]").forEach((button) => {
    button.addEventListener("click", async () => {
      const port = button.dataset.removePort;
      await removePortMapping(port);
    });
  });
  document.querySelectorAll("[data-remove-proxy-admin-port]").forEach((button) => {
    button.addEventListener("click", async () => {
      await removeProxyAdminProxyForPort(button.dataset.removeProxyAdminPort);
    });
  });
  document.querySelectorAll("[data-copy]").forEach((item) => {
    item.addEventListener("click", () => {
      copyText(item.dataset.copy, item.dataset.copyLabel || "内容");
    });
  });
  renderValidationResults();
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
  if (!entry) return '<span class="muted">-</span>';
  const { result } = entry;
  const failed = result.grade === "ERR" || (result.items || []).some((item) => item.status === "fail");
  const targets = (result.items || []).slice(0, 4);
  const title = (result.items || [])
    .map((item) => `${item.target}: ${item.status} ${item.http_status || "-"} ${item.latency_ms || "-"}ms ${item.message || ""}`)
    .join("\n");
  return `
    <div class="proxy-admin-inline" title="${escapeHtml(title || result.error || "")}">
      <span class="badge ${failed ? "bad" : "ok"}">${escapeHtml(result.grade || "-")} · ${escapeHtml(result.score ?? "-")}</span>
      <span class="mono">${escapeHtml(result.exit_ip || "-")}</span>
      <span>${escapeHtml(result.country || "-")}</span>
      <span class="proxy-admin-mini-targets">
        ${targets.map((item) => `<b class="${escapeHtml(item.status || "err")}">${escapeHtml(shortTargetName(item.target))}</b>`).join("")}
      </span>
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

function proxyAdminQuality(entry) {
  if (!entry?.result) return { bucket: 1, gradeRank: 99, score: -1 };
  const result = entry.result;
  if (proxyAdminResultFailed(result)) return { bucket: 2, gradeRank: 99, score: Number(result.score || 0) };
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
  const proxyAdminByPort = proxyAdminResultByPort();
  const hasProxyAdminResults = proxyAdminByPort.size > 0;
  return Object.entries(ports).sort(([leftPort, left], [rightPort, right]) => {
    if (hasProxyAdminResults) {
      const leftQuality = proxyAdminQuality(proxyAdminByPort.get(String(leftPort)));
      const rightQuality = proxyAdminQuality(proxyAdminByPort.get(String(rightPort)));
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
        <button data-remove-port="${port}">移除映射</button>
      </header>
      <div class="validation-chips">${(detail.targets || []).map((target) => `
        <span class="target-chip ${target.ok ? "ok" : "bad"}">
          ${escapeHtml(compactUrl(target.url))}
          <b>${target.ok ? "成功" : "失败"}</b>
          <small>${escapeHtml(target.status_code || "-")} · ${escapeHtml(target.elapsed_ms || "-")}ms</small>
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
  for (;;) {
    const job = await request(`/api/proxy-admin/jobs/${jobId}`);
    proxyAdminImported = job.imported || proxyAdminImported;
    proxyAdminResults = job.results || proxyAdminResults;
    renderProxyAdminResults();
    renderPortsTable();
    if (job.status === "done") {
      showQuickResult("ProxyAdmin 检测", `完成 ${job.completed}/${job.total}`, true);
      return;
    }
    if (job.status === "error") {
      showQuickResult("ProxyAdmin 检测", job.error || "检测失败", false);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
}

function compactUrl(url) {
  try {
    const parsed = new URL(url);
    return `${parsed.hostname}${parsed.pathname === "/" ? "" : parsed.pathname}`;
  } catch {
    return url;
  }
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
  renderSummary();
  renderNodeTable();
  renderAssignTable();
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
    await pollTestJob(started.id);
  } catch (error) {
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
      validatingPorts.clear();
      const details = Object.values(validationDetails);
      const total = job.total || details.length;
      const ok = details.filter((detail) => (detail.targets || []).some((target) => target.ok)).length;
      renderPortsTable();
      renderValidationResults();
      showQuickResult("验证全部端口", `可用 ${ok}/${total}；目标：${urls.join("，")}`, ok > 0);
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

document.querySelectorAll(".node-target-check").forEach((item) => {
  item.addEventListener("change", renderNodeTestOverview);
});

$("nodeTestUrl").addEventListener("input", renderNodeTestOverview);

$("nodeTestGeo").addEventListener("change", renderNodeTestOverview);

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

$("clearAssignBtn").addEventListener("click", () => runTask("清空端口分配", async () => {
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
  const started = await request("/api/proxy-admin/check/start", {
    method: "POST",
    body: JSON.stringify(proxyAdminPayload({ ports: retryPorts }))
  });
  await pollProxyAdminJob(started.id);
  return "ProxyAdmin 失败项已重试";
}));

$("proxyAdminDeleteFailedBtn").addEventListener("click", () => runTask("ProxyAdmin 删除失败", async () => {
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

$("proxyAdminDeleteUnusedBtn").addEventListener("click", () => runTask("ProxyAdmin 删除未使用", async () => {
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

refresh().catch((error) => showNotice(error.message, "bad"));
setInterval(() => {
  if (document.hidden) return;
  refreshStatusOnly().catch(() => {});
}, 15000);
