/**
 * helpers.js — Pure utility functions for Proxy Pool Manager frontend.
 * Loaded via <script> before app.js. No dependencies on app state.
 */

// ── DOM helpers ─────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

// ── String/HTML helpers ─────────────────────────────────────────────────────

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[char]);
}

// ── Latency display helpers ─────────────────────────────────────────────────

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

// ── Target response helpers ─────────────────────────────────────────────────

function targetResponseText(latency) {
  if (!latency) return "-";
  if (latency.target_results?.length > 1) {
    const okItems = latency.target_results.filter((item) => item.ok);
    const failed = latency.target_results.length - okItems.length;
    const detail = latency.target_results
      .map((item) => `${compactUrl(item.url)} ${item.ok ? item.elapsed_ms + "ms" : compactCheckMessage(item.error || "失败")}`)
      .join("；");
    const status = `成功 ${okItems.length}/${latency.target_results.length}`;
    const average = latency.delay ? ` · 平均 ${latency.delay}ms` : "";
    const failedText = failed ? ` · 失败 ${failed}` : "";
    return `${status}${average}${failedText} · ${detail}`;
  }
  if (latency.target_url) {
    if (!latency.alive && latency.error) return compactCheckMessage(latency.error);
    const status = latency.status_code ? `HTTP ${latency.status_code}` : "无状态码";
    const preview = latency.body_preview ? ` · ${latency.body_preview}` : "";
    return `${status}${preview}`;
  }
  if (latency.exit_ip) return `出口 ${latency.exit_ip}`;
  return compactCheckMessage(latency.error) || "-";
}

function targetResponsePreview(latency) {
  const text = targetResponseText(latency);
  return text.length > 82 ? `${text.slice(0, 79)}...` : text;
}

// ── GeoIP display helpers ───────────────────────────────────────────────────

function geoIpUsable(geoip) {
  if (!geoip || geoip.error) return false;
  return Boolean(geoip.country_code || geoip.country || geoip.city || geoip.region || geoip.compact);
}

function geoFromLocalProxyCheck(lpc) {
  if (!lpc) return null;
  const code = String(lpc.country_code || "").trim();
  const country = String(lpc.country || code || "").trim();
  if (!code && !country) return null;
  const compact = code || country;
  return {
    country_code: code || null,
    country: country || null,
    compact,
    summary: country && code && country !== code ? `${country} · ${code}` : compact,
    source: "local-proxy-check"
  };
}

function geoIpSummary(geoip) {
  if (!geoip) return "";
  if (geoip.summary && !geoip.error) return geoip.summary;
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
  if (geoip.compact && !geoip.error) return geoip.compact;
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
  const geo = item?.geoip || item?.latency?.geoip || null;
  if (geoIpUsable(geo)) return geo;
  const lpc = item?.local_proxy_check
    || item?.latency?.local_proxy_check
    || null;
  const fromLpc = geoFromLocalProxyCheck(lpc);
  if (fromLpc) return fromLpc;
  return geo;
}

function geoIpText(item) {
  return geoIpCompact(geoIpForItem(item)) || "-";
}

function geoIpTitle(item) {
  const geo = geoIpForItem(item);
  return geoIpSummary(geo) || geoIpText(item);
}

// ── Status badge helpers ────────────────────────────────────────────────────

function statusBadge(latency) {
  if (!latency) return `<span class="badge idle">未测</span>`;
  if (latency.alive) return `<span class="badge ok"><span class="dot"></span>可用</span>`;
  return `<span class="badge bad"><span class="dot"></span>失败</span>`;
}

function protocolChip(type) {
  const raw = String(type || "-").toLowerCase();
  const known = ["vless", "vmess", "ss", "trojan", "hysteria2", "hysteria", "tuic", "wireguard"];
  const cls = known.includes(raw) ? raw.replace("hysteria2", "hysteria2") : "other";
  return `<span class="proto ${escapeHtml(cls)}">${escapeHtml(type || "-")}</span>`;
}

function latencyBarHtml(latency) {
  if (!latency || !latency.alive || typeof latency.delay !== "number") {
    return `<span class="latency-pill ${latencyClass(latency)}" title="${escapeHtml(latencyTitle(latency))}">${escapeHtml(latencyText(latency))}</span>`;
  }
  const delay = latency.delay;
  // ponytail: simple inverse width; fine for visual scan, not scientific scale
  const width = Math.max(8, Math.min(100, Math.round(100 - delay / 30)));
  return `<span class="latency-visual ${latencyClass(latency)}" title="${escapeHtml(latencyTitle(latency))}"><span class="latency-bar"><i style="width:${width}%"></i></span><span class="latency-ms">${escapeHtml(String(delay))}ms</span></span>`;
}

function geoChipHtml(item) {
  const text = geoIpText(item);
  if (!text || text === "-") return `<span class="badge idle">—</span>`;
  const code = String(geoIpForItem(item)?.country_code || text).slice(0, 2).toLowerCase();
  const flagCls = ["hk", "jp", "us", "tw", "sg", "de", "kr", "gb", "uk", "cn"].includes(code) ? code : "";
  return `<span class="geoip-chip" title="${escapeHtml(geoIpTitle(item))}"><span class="flag ${flagCls}"></span>${escapeHtml(text)}</span>`;
}

// ── URL / message compaction ────────────────────────────────────────────────

function compactUrl(url) {
  try {
    const parsed = new URL(url);
    return `${parsed.hostname}${parsed.pathname === "/" ? "" : parsed.pathname}`;
  } catch {
    return url;
  }
}

function compactCheckMessage(message) {
  const text = String(message || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  const certMismatch = text.match(/x509: certificate is valid for ([^,]+).*not ([^\s"]+)/i);
  if (certMismatch) return `TLS 证书不匹配：${certMismatch[2]} 返回了 ${certMismatch[1]} 证书`;
  if (/No connection could be made|connection refused|actively refused/i.test(text)) return "本地端口未监听或连接被拒绝";
  if (/timeout|deadline exceeded|timed out/i.test(text)) return "请求超时";
  if (/tls: failed to verify certificate/i.test(text)) return "TLS 证书校验失败";
  if (/proxyconnect tcp/i.test(text)) return "代理连接失败";
  return text.length > 96 ? `${text.slice(0, 96)}...` : text;
}

// ── Local proxy check helpers ───────────────────────────────────────────────

function localProxyCheckFailed(result) {
  return result?.grade === "ERR" || (result?.items || []).some((item) => item.status === "fail");
}
