// Package proxycheck 提供代理质量检测功能
//
// 从 Sub2API 项目提取的独立模块，用于检测代理服务器的连通性和质量。
// 输入代理 URL，输出结构化质量检测结果。
package proxycheck

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// ============================================================
// 数据结构（与 Sub2API 保持一致）
// ============================================================

// QualityResult 代理质量检测结果
type QualityResult struct {
	ProxyID   int    `json:"proxy_id,omitempty"`
	ProxyURL  string `json:"proxy_url"`
	Score     int    `json:"score"`
	Grade     string `json:"grade"`
	Summary   string `json:"summary"`
	ExitIP    string `json:"exit_ip,omitempty"`
	Country   string `json:"country,omitempty"`
	CountryCode string `json:"country_code,omitempty"`

	BaseLatencyMs  int64 `json:"base_latency_ms,omitempty"`
	PassedCount    int   `json:"passed_count"`
	WarnCount      int   `json:"warn_count"`
	FailedCount    int   `json:"failed_count"`
	ChallengeCount int   `json:"challenge_count"`
	CheckedAt      int64 `json:"checked_at"`

	Items []QualityCheckItem `json:"items"`
}

// QualityCheckItem 单项检测结果
type QualityCheckItem struct {
	Target     string `json:"target"`
	Status     string `json:"status"` // pass / warn / fail / challenge
	HTTPStatus int    `json:"http_status,omitempty"`
	LatencyMs  int64  `json:"latency_ms,omitempty"`
	Message    string `json:"message,omitempty"`
	CFRay      string `json:"cf_ray,omitempty"`
}

// ExitInfo 代理出口信息
type ExitInfo struct {
	IP          string
	City        string
	Region      string
	Country     string
	CountryCode string
}

// ============================================================
// 检测目标配置
// ============================================================

type qualityTarget struct {
	Target          string
	URL             string
	Method          string
	AllowedStatuses map[int]struct{}
}

var defaultTargets = []qualityTarget{
	{
		Target: "openai",
		URL:    "https://api.openai.com/v1/models",
		Method: http.MethodGet,
		AllowedStatuses: map[int]struct{}{
			http.StatusUnauthorized: {},
		},
	},
	{
		Target: "anthropic",
		URL:    "https://api.anthropic.com/v1/messages",
		Method: http.MethodGet,
		AllowedStatuses: map[int]struct{}{
			http.StatusUnauthorized:     {},
			http.StatusMethodNotAllowed: {},
			http.StatusNotFound:         {},
			http.StatusBadRequest:       {},
		},
	},
	{
		Target: "gemini",
		URL:    "https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta",
		Method: http.MethodGet,
		AllowedStatuses: map[int]struct{}{
			http.StatusOK: {},
		},
	},
}

const (
	qualityRequestTimeout        = 15 * time.Second
	qualityResponseHeaderTimeout = 10 * time.Second
	qualityMaxBodyBytes          = int64(8 * 1024)
	qualityUserAgent             = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

// ============================================================
// 对外 API
// ============================================================

// Options 检测选项
type Options struct {
	// 自定义检测目标，为空时使用默认目标（OpenAI / Anthropic / Gemini）
	Targets []CheckTarget
}

// CheckTarget 自定义检测目标
type CheckTarget struct {
	Name     string // 目标名称，如 "openai"
	URL      string // 请求 URL
	Method   string // HTTP 方法，默认 GET
	// AllowedStatuses 视为"可达"的 HTTP 状态码集合
	// 例如 401（未鉴权）说明网络层面是通的
	AllowedStatuses []int
}

// CheckProxyQuality 检测代理质量
// proxyURL 格式: http://user:pass@host:port 或 socks5://host:port 等
func CheckProxyQuality(ctx context.Context, proxyURL string, opts *Options) (*QualityResult, error) {
	result := &QualityResult{
		ProxyURL:  proxyURL,
		Score:     100,
		Grade:     "A",
		CheckedAt: time.Now().Unix(),
	}

	// 1. 探测基础连通性
	exitInfo, latencyMs, err := probeProxy(ctx, proxyURL)
	if err != nil {
		result.Items = append(result.Items, QualityCheckItem{
			Target:    "base_connectivity",
			Status:    "fail",
			LatencyMs: latencyMs,
			Message:   err.Error(),
		})
		result.FailedCount++
		finalize(result)
		return result, nil
	}

	result.ExitIP = exitInfo.IP
	result.Country = exitInfo.Country
	result.CountryCode = exitInfo.CountryCode
	result.BaseLatencyMs = latencyMs
	result.Items = append(result.Items, QualityCheckItem{
		Target:    "base_connectivity",
		Status:    "pass",
		LatencyMs: latencyMs,
		Message:   "代理出口连通正常",
	})
	result.PassedCount++

	// 2. 创建代理 HTTP 客户端
	client, err := newProxyClient(proxyURL, qualityRequestTimeout, qualityResponseHeaderTimeout)
	if err != nil {
		result.Items = append(result.Items, QualityCheckItem{
			Target:  "http_client",
			Status:  "fail",
			Message: fmt.Sprintf("创建检测客户端失败: %v", err),
		})
		result.FailedCount++
		finalize(result)
		return result, nil
	}

	// 3. 遍历检测目标
	targets := buildTargets(opts)
	for _, target := range targets {
		item := checkTarget(ctx, client, target)
		result.Items = append(result.Items, item)
		switch item.Status {
		case "pass":
			result.PassedCount++
		case "warn":
			result.WarnCount++
		case "challenge":
			result.ChallengeCount++
		default:
			result.FailedCount++
		}
	}

	finalize(result)
	return result, nil
}

// ============================================================
// 内部实现
// ============================================================

func buildTargets(opts *Options) []qualityTarget {
	if opts == nil || len(opts.Targets) == 0 {
		return defaultTargets
	}
	out := make([]qualityTarget, len(opts.Targets))
	for i, t := range opts.Targets {
		method := t.Method
		if method == "" {
			method = http.MethodGet
		}
		allowed := make(map[int]struct{}, len(t.AllowedStatuses))
		for _, code := range t.AllowedStatuses {
			allowed[code] = struct{}{}
		}
		out[i] = qualityTarget{
			Target:          t.Name,
			URL:             t.URL,
			Method:          method,
			AllowedStatuses: allowed,
		}
	}
	return out
}

func checkTarget(ctx context.Context, client *http.Client, target qualityTarget) QualityCheckItem {
	item := QualityCheckItem{Target: target.Target}

	req, err := http.NewRequestWithContext(ctx, target.Method, target.URL, nil)
	if err != nil {
		item.Status = "fail"
		item.Message = fmt.Sprintf("构建请求失败: %v", err)
		return item
	}
	req.Header.Set("Accept", "application/json,text/html,*/*")
	req.Header.Set("User-Agent", qualityUserAgent)

	start := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		item.Status = "fail"
		item.LatencyMs = time.Since(start).Milliseconds()
		item.Message = fmt.Sprintf("请求失败: %v", err)
		return item
	}
	defer resp.Body.Close()
	item.LatencyMs = time.Since(start).Milliseconds()
	item.HTTPStatus = resp.StatusCode

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, qualityMaxBodyBytes+1))
	if readErr != nil {
		item.Status = "fail"
		item.Message = fmt.Sprintf("读取响应失败: %v", readErr)
		return item
	}
	if int64(len(body)) > qualityMaxBodyBytes {
		body = body[:qualityMaxBodyBytes]
	}

	// Cloudflare challenge 检测
	if isCloudflareChallenge(resp.StatusCode, resp.Header, body) {
		item.Status = "challenge"
		item.CFRay = extractCFRay(resp.Header, body)
		item.Message = "命中 Cloudflare challenge"
		return item
	}

	if _, ok := target.AllowedStatuses[resp.StatusCode]; ok {
		item.Status = "pass"
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			item.Message = fmt.Sprintf("HTTP %d", resp.StatusCode)
		} else {
			item.Message = fmt.Sprintf("HTTP %d（目标可达）", resp.StatusCode)
		}
		return item
	}

	if resp.StatusCode == http.StatusTooManyRequests {
		item.Status = "warn"
		item.Message = "目标返回 429，可能存在频控"
		return item
	}

	item.Status = "fail"
	item.Message = fmt.Sprintf("非预期状态码: %d", resp.StatusCode)
	return item
}

func finalize(result *QualityResult) {
	if result == nil {
		return
	}
	score := 100 - result.WarnCount*10 - result.FailedCount*22 - result.ChallengeCount*30
	if score < 0 {
		score = 0
	}
	result.Score = score
	result.Grade = grade(score)
	result.Summary = fmt.Sprintf(
		"通过 %d 项，告警 %d 项，失败 %d 项，挑战 %d 项",
		result.PassedCount, result.WarnCount, result.FailedCount, result.ChallengeCount,
	)
}

func grade(score int) string {
	switch {
	case score >= 90:
		return "A"
	case score >= 75:
		return "B"
	case score >= 60:
		return "C"
	case score >= 40:
		return "D"
	default:
		return "F"
	}
}

// ============================================================
// 代理探测（获取出口 IP 和延迟）
// ============================================================

var probeURLs = []struct {
	url    string
	parser string
}{
	{"http://ip-api.com/json/?lang=zh-CN", "ip-api"},
	{"http://httpbin.org/ip", "httpbin"},
}

func probeProxy(ctx context.Context, proxyURL string) (*ExitInfo, int64, error) {
	client, err := newProxyClient(proxyURL, 10*time.Second, 0)
	if err != nil {
		return nil, 0, fmt.Errorf("failed to create proxy client: %w", err)
	}

	var lastErr error
	for _, probe := range probeURLs {
		info, latency, err := probeWithURL(ctx, client, probe.url, probe.parser)
		if err == nil {
			return info, latency, nil
		}
		lastErr = err
	}
	return nil, 0, fmt.Errorf("all probe URLs failed, last error: %w", lastErr)
}

func probeWithURL(ctx context.Context, client *http.Client, url, parser string) (*ExitInfo, int64, error) {
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, 0, fmt.Errorf("create request failed: %w", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("proxy connection failed: %w", err)
	}
	defer resp.Body.Close()

	latencyMs := time.Since(start).Milliseconds()

	if resp.StatusCode != http.StatusOK {
		return nil, latencyMs, fmt.Errorf("request failed with status: %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1024*1024+1))
	if err != nil {
		return nil, latencyMs, fmt.Errorf("read response failed: %w", err)
	}

	switch parser {
	case "ip-api":
		return parseIPAPI(body, latencyMs)
	case "httpbin":
		return parseHTTPBin(body, latencyMs)
	default:
		return nil, latencyMs, fmt.Errorf("unknown parser: %s", parser)
	}
}

func parseIPAPI(body []byte, latencyMs int64) (*ExitInfo, int64, error) {
	var data struct {
		Status      string `json:"status"`
		Message     string `json:"message"`
		Query       string `json:"query"`
		City        string `json:"city"`
		RegionName  string `json:"regionName"`
		Country     string `json:"country"`
		CountryCode string `json:"countryCode"`
	}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, latencyMs, fmt.Errorf("parse ip-api response failed: %w", err)
	}
	if strings.ToLower(data.Status) != "success" {
		msg := data.Message
		if msg == "" {
			msg = "ip-api request failed"
		}
		return nil, latencyMs, fmt.Errorf("ip-api: %s", msg)
	}
	return &ExitInfo{
		IP:          data.Query,
		City:        data.City,
		Region:      data.RegionName,
		Country:     data.Country,
		CountryCode: data.CountryCode,
	}, latencyMs, nil
}

func parseHTTPBin(body []byte, latencyMs int64) (*ExitInfo, int64, error) {
	var data struct {
		Origin string `json:"origin"`
	}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, latencyMs, fmt.Errorf("parse httpbin response failed: %w", err)
	}
	if data.Origin == "" {
		return nil, latencyMs, fmt.Errorf("httpbin: no IP found")
	}
	return &ExitInfo{
		IP: data.Origin,
	}, latencyMs, nil
}

// ============================================================
// HTTP 客户端（简化版，无连接池）
// ============================================================

func newProxyClient(proxyURL string, timeout, headerTimeout time.Duration) (*http.Client, error) {
	transport := &http.Transport{
		DialContext: (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		TLSHandshakeTimeout:   5 * time.Second,
		MaxIdleConns:          10,
		IdleConnTimeout:       90 * time.Second,
	}

	if proxyURL != "" {
		parsed, err := url.Parse(proxyURL)
		if err != nil {
			return nil, fmt.Errorf("invalid proxy URL: %w", err)
		}
		if parsed.Scheme == "" {
			return nil, fmt.Errorf("proxy URL missing scheme (use http://, socks5://, etc.)")
		}
		transport.Proxy = http.ProxyURL(parsed)
	}

	if headerTimeout > 0 {
		transport.ResponseHeaderTimeout = headerTimeout
	}

	return &http.Client{
		Transport: transport,
		Timeout:   timeout,
	}, nil
}

// ============================================================
// Cloudflare 挑战检测（简化版）
// ============================================================

var cfRayPattern = regexpOrPanic(`(?i)cf-ray[:\s=]+([a-z0-9-]+)`)
var cRayPattern = regexpOrPanic(`(?i)cRay:\s*'([a-z0-9-]+)'`)

var htmlChallengeMarkers = []string{
	"window._cf_chl_opt",
	"just a moment",
	"enable javascript and cookies to continue",
	"__cf_chl_",
	"challenge-platform",
}

func regexpOrPanic(pattern string) *regexp.Regexp {
	return regexp.MustCompile(pattern)
}

func isCloudflareChallenge(statusCode int, headers http.Header, body []byte) bool {
	if statusCode != http.StatusForbidden && statusCode != http.StatusTooManyRequests {
		return false
	}

	if headers != nil && strings.EqualFold(strings.TrimSpace(headers.Get("cf-mitigated")), "challenge") {
		return true
	}

	preview := strings.ToLower(truncateBody(body, 4096))
	for _, marker := range htmlChallengeMarkers {
		if strings.Contains(preview, marker) {
			return true
		}
	}

	contentType := ""
	if headers != nil {
		contentType = strings.ToLower(strings.TrimSpace(headers.Get("content-type")))
	}
	if strings.Contains(contentType, "text/html") &&
		(strings.Contains(preview, "<html") || strings.Contains(preview, "<!doctype html")) &&
		(strings.Contains(preview, "cloudflare") || strings.Contains(preview, "challenge")) {
		return true
	}

	return false
}

func extractCFRay(headers http.Header, body []byte) string {
	if headers != nil {
		rayID := strings.TrimSpace(headers.Get("cf-ray"))
		if rayID != "" {
			return rayID
		}
		rayID = strings.TrimSpace(headers.Get("Cf-Ray"))
		if rayID != "" {
			return rayID
		}
	}

	preview := truncateBody(body, 8192)
	if matches := cfRayPattern.FindStringSubmatch(preview); len(matches) >= 2 {
		return strings.TrimSpace(matches[1])
	}
	if matches := cRayPattern.FindStringSubmatch(preview); len(matches) >= 2 {
		return strings.TrimSpace(matches[1])
	}
	return ""
}

func truncateBody(body []byte, max int) string {
	if max <= 0 {
		max = 512
	}
	raw := strings.TrimSpace(string(body))
	if len(raw) <= max {
		return raw
	}
	return raw[:max] + "...(truncated)"
}
