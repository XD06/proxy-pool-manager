// 示例程序：输入代理 URL，输出质量检测结果
//
// 用法:
//
//	直接检测:  go run . -proxy "socks5://127.0.0.1:1080"
//	JSON 输出: go run . -proxy "http://user:pass@1.2.3.4:8080" -json
//
// 代理 URL 格式支持:
//   http://host:port
//   http://user:pass@host:port
//   https://host:port
//   socks5://host:port
//   socks5h://host:port
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/yourname/proxycheck"
)

func main() {
	proxyURL := flag.String("proxy", "", "代理 URL (如 socks5://127.0.0.1:1080)")
	jsonOutput := flag.Bool("json", false, "以 JSON 格式输出")
	timeout := flag.Int("timeout", 30, "检测超时时间（秒）")
	flag.Parse()

	if *proxyURL == "" {
		fmt.Fprintf(os.Stderr, "用法: %s -proxy <代理URL> [-json] [-timeout 30]\n", os.Args[0])
		fmt.Fprintf(os.Stderr, "示例: %s -proxy socks5://127.0.0.1:1080\n", os.Args[0])
		fmt.Fprintf(os.Stderr, "      %s -proxy http://user:pass@1.2.3.4:8080 -json\n", os.Args[0])
		os.Exit(1)
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(*timeout)*time.Second)
	defer cancel()

	fmt.Fprintf(os.Stderr, "正在检测代理: %s ...\n", maskProxyURL(*proxyURL))
	start := time.Now()

	result, err := proxycheck.CheckProxyQuality(ctx, *proxyURL, nil)
	elapsed := time.Since(start)

	if err != nil {
		fmt.Fprintf(os.Stderr, "检测失败: %v\n", err)
		os.Exit(1)
	}

	if *jsonOutput {
		data, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(data))
	} else {
		printResult(result, elapsed)
	}
}

func printResult(r *proxycheck.QualityResult, elapsed time.Duration) {
	fmt.Println(strings.Repeat("─", 50))
	fmt.Printf(" 代理:        %s\n", maskProxyURL(r.ProxyURL))
	fmt.Printf(" 评分:        %d / 100 (%s)\n", r.Score, r.Grade)
	fmt.Printf(" 汇总:        %s\n", r.Summary)
	fmt.Printf(" 出口 IP:     %s\n", r.ExitIP)
	if r.Country != "" {
		fmt.Printf(" 位置:        %s [%s]\n", r.Country, r.CountryCode)
	}
	fmt.Printf(" 基础延迟:    %d ms\n", r.BaseLatencyMs)
	fmt.Printf(" 检测耗时:    %v\n", elapsed)
	fmt.Println(strings.Repeat("─", 50))

	fmt.Printf(" %-20s %-10s %-8s %s\n", "目标", "状态", "延迟", "详情")
	fmt.Println(strings.Repeat("─", 50))
	for _, item := range r.Items {
		latency := fmt.Sprintf("%dms", item.LatencyMs)
		if item.LatencyMs == 0 {
			latency = "-"
		}
		status := item.Status
		switch status {
		case "pass":
			status = "✅ pass"
		case "warn":
			status = "⚠️  warn"
		case "challenge":
			status = "🛡️  challenge"
		default:
			status = "❌ " + status
		}
		detail := item.Message
		if item.CFRay != "" {
			detail += fmt.Sprintf(" (cf-ray: %s)", item.CFRay)
		}
		fmt.Printf(" %-20s %-10s %-8s %s\n", item.Target, status, latency, detail)
	}
	fmt.Println(strings.Repeat("─", 50))
}

func maskProxyURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	if u.User != nil {
		pass, _ := u.User.Password()
		if pass != "" {
			u.User = url.UserPassword(u.User.Username(), "****")
			return u.String()
		}
	}
	return raw
}
