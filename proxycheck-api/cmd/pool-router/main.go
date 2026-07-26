// pool-router is a small L4 edge router for Proxy Pool Manager.
// It keeps Python out of the proxy data path while accounting traffic per
// public listener and selected backend.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type config struct {
	ControlListen string           `json:"control_listen"`
	Listeners     []listenerConfig `json:"listeners"`
}

type listenerConfig struct {
	ID                      string          `json:"id"`
	Listen                  string          `json:"listen"`
	Policy                  string          `json:"policy"`
	RotationIntervalSeconds int             `json:"rotation_interval_seconds"`
	Backends                []backendConfig `json:"backends"`
}

type backendConfig struct {
	ID       string `json:"id"`
	Address  string `json:"address"`
	Weight   int    `json:"weight"`
	Enabled  bool   `json:"enabled"`
	Draining bool   `json:"draining"`
}

type counters struct {
	Upload       atomic.Uint64
	Download     atomic.Uint64
	Active       atomic.Int64
	Selections   atomic.Uint64
	DialFailures atomic.Uint64
}

type backend struct {
	backendConfig
	counters      counters
	lastDialError atomic.Value
}

type listener struct {
	listenerConfig
	backends       []*backend
	ln             net.Listener
	cursor         atomic.Uint64
	counters       counters
	selectionMu    sync.Mutex
	windowBackend  *backend
	windowDeadline time.Time
}

type runtime struct {
	mu               sync.RWMutex
	listeners        map[string]*listener
	failedListeners []map[string]string
}

type countWriter struct {
	w io.Writer
	c []*atomic.Uint64
}

func (w countWriter) Write(p []byte) (int, error) {
	n, err := w.w.Write(p)
	if n > 0 {
		for _, counter := range w.c {
			counter.Add(uint64(n))
		}
	}
	return n, err
}

func main() {
	configPath := flag.String("config", "", "router JSON config path")
	flag.Parse()
	if strings.TrimSpace(*configPath) == "" {
		log.Fatal("usage: pool-router -config <path>")
	}
	data, err := os.ReadFile(filepath.Clean(*configPath))
	if err != nil {
		log.Fatalf("read config: %v", err)
	}
	var cfg config
	if err := json.Unmarshal(data, &cfg); err != nil {
		log.Fatalf("parse config: %v", err)
	}
	if cfg.ControlListen == "" {
		cfg.ControlListen = "127.0.0.1:9091"
	}
	rt := &runtime{listeners: make(map[string]*listener)}
	for _, item := range cfg.Listeners {
		if err := rt.add(item); err != nil {
			log.Printf("warning: skipping listener: %v", err)
			rt.failedListeners = append(rt.failedListeners, map[string]string{
				"id":    item.ID,
				"listen": item.Listen,
				"error": err.Error(),
			})
			continue
		}
	}
	if len(rt.listeners) == 0 {
		rt.close()
		log.Fatal("all listeners failed to start; nothing to serve")
	}
	server := &http.Server{Addr: cfg.ControlListen, Handler: rt.handler(), ReadHeaderTimeout: 3 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("control server: %v", err)
		}
	}()
	if len(rt.failedListeners) > 0 {
		log.Printf("pool-router started: %d/%d proxy listeners active, %d skipped, control=%s",
			len(rt.listeners), len(cfg.Listeners), len(rt.failedListeners), cfg.ControlListen)
	} else {
		log.Printf("pool-router started: %d proxy listeners, control=%s", len(cfg.Listeners), cfg.ControlListen)
	}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	rt.close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = server.Shutdown(ctx)
}

func (rt *runtime) add(cfg listenerConfig) error {
	if cfg.ID == "" || cfg.Listen == "" {
		return fmt.Errorf("listener requires id and listen address")
	}
	if len(cfg.Backends) == 0 {
		return fmt.Errorf("listener %s has no backends", cfg.ID)
	}
	if cfg.Policy != "round_robin" && cfg.Policy != "weighted_round_robin" && cfg.Policy != "time_window" {
		return fmt.Errorf("listener %s has unsupported policy %q", cfg.ID, cfg.Policy)
	}
	if cfg.RotationIntervalSeconds <= 0 {
		cfg.RotationIntervalSeconds = 600
	}
	l := &listener{listenerConfig: cfg}
	for _, item := range cfg.Backends {
		if item.ID == "" || item.Address == "" {
			return fmt.Errorf("listener %s has invalid backend", cfg.ID)
		}
		if item.Weight < 1 {
			item.Weight = 1
		}
		l.backends = append(l.backends, &backend{backendConfig: item})
	}
	ln, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return fmt.Errorf("listen %s (%s): %w", cfg.ID, cfg.Listen, err)
	}
	l.ln = ln
	rt.mu.Lock()
	if _, exists := rt.listeners[cfg.ID]; exists {
		rt.mu.Unlock()
		_ = ln.Close()
		return fmt.Errorf("duplicate listener id %s", cfg.ID)
	}
	rt.listeners[cfg.ID] = l
	rt.mu.Unlock()
	go l.serve()
	return nil
}

func (rt *runtime) close() {
	rt.mu.RLock()
	defer rt.mu.RUnlock()
	for _, l := range rt.listeners {
		_ = l.ln.Close()
	}
}

func (l *listener) serve() {
	for {
		conn, err := l.ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			log.Printf("accept %s: %v", l.ID, err)
			continue
		}
		go l.handle(conn)
	}
}

func (l *listener) available() []*backend {
	items := make([]*backend, 0, len(l.backends))
	for _, item := range l.backends {
		if item.Enabled && !item.Draining {
			items = append(items, item)
		}
	}
	return items
}

func (l *listener) pick(exclude map[*backend]bool) *backend {
	if l.Policy == "time_window" {
		return l.pickWindow(exclude)
	}
	return l.pickFromSchedule(exclude)
}

func (l *listener) pickFromSchedule(exclude map[*backend]bool) *backend {
	items := l.available()
	schedule := make([]*backend, 0, len(items))
	for _, item := range items {
		if exclude[item] {
			continue
		}
		copies := 1
		if l.Policy == "weighted_round_robin" {
			copies = item.Weight
		}
		for i := 0; i < copies; i++ {
			schedule = append(schedule, item)
		}
	}
	if len(schedule) == 0 {
		return nil
	}
	index := l.cursor.Add(1) - 1
	return schedule[index%uint64(len(schedule))]
}

func (l *listener) pickWindow(exclude map[*backend]bool) *backend {
	l.selectionMu.Lock()
	defer l.selectionMu.Unlock()
	now := time.Now()
	if l.windowBackend != nil && now.Before(l.windowDeadline) && !exclude[l.windowBackend] && l.windowBackend.Enabled && !l.windowBackend.Draining {
		return l.windowBackend
	}
	selected := l.pickFromSchedule(exclude)
	if selected == nil {
		return nil
	}
	l.windowBackend = selected
	l.windowDeadline = now.Add(time.Duration(l.RotationIntervalSeconds) * time.Second)
	return selected
}

func (l *listener) handle(client net.Conn) {
	l.counters.Active.Add(1)
	defer l.counters.Active.Add(-1)
	defer client.Close()
	excluded := map[*backend]bool{}
	var selected *backend
	var upstream net.Conn
	for range l.backends {
		selected = l.pick(excluded)
		if selected == nil {
			return
		}
		candidate, err := net.DialTimeout("tcp", selected.Address, 3*time.Second)
		if err != nil {
			l.counters.DialFailures.Add(1)
			selected.counters.DialFailures.Add(1)
			selected.lastDialError.Store(err.Error())
			excluded[selected] = true
			continue
		}
		selected.lastDialError.Store("")
		upstream = candidate
		break
	}
	if upstream == nil || selected == nil {
		return
	}
	defer upstream.Close()
	l.counters.Selections.Add(1)
	selected.counters.Selections.Add(1)
	selected.counters.Active.Add(1)
	defer selected.counters.Active.Add(-1)
	done := make(chan struct{}, 2)
	go func() {
		_, _ = io.Copy(countWriter{w: upstream, c: []*atomic.Uint64{&l.counters.Upload, &selected.counters.Upload}}, client)
		closeWrite(upstream)
		done <- struct{}{}
	}()
	go func() {
		_, _ = io.Copy(countWriter{w: client, c: []*atomic.Uint64{&l.counters.Download, &selected.counters.Download}}, upstream)
		closeWrite(client)
		done <- struct{}{}
	}()
	<-done
	<-done
}

func closeWrite(conn net.Conn) {
	if tcp, ok := conn.(*net.TCPConn); ok {
		_ = tcp.CloseWrite()
	}
}

func (rt *runtime) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		writeJSON(w, rt.snapshot())
	})
	mux.HandleFunc("/listeners/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
		if len(parts) != 3 || parts[0] != "listeners" || parts[2] != "advance" || r.Method != http.MethodPost {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		rt.mu.RLock()
		l := rt.listeners[parts[1]]
		rt.mu.RUnlock()
		if l == nil {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if l.Policy == "time_window" {
			l.selectionMu.Lock()
			l.windowDeadline = time.Time{}
			l.windowBackend = nil
			l.selectionMu.Unlock()
		} else {
			// Stateless policies have no active connection to evict. Move the
			// schedule cursor once so the following new connection is advanced.
			l.cursor.Add(1)
		}
		writeJSON(w, map[string]any{"ok": true, "id": l.ID})
	})
	return mux
}

func (rt *runtime) snapshot() map[string]any {
	rt.mu.RLock()
	defer rt.mu.RUnlock()
	listeners := make([]map[string]any, 0, len(rt.listeners))
	for _, l := range rt.listeners {
		backends := make([]map[string]any, 0, len(l.backends))
		for _, b := range l.backends {
			payload := counterPayload(b.ID, b.Enabled, b.Draining, &b.counters)
			if value := b.lastDialError.Load(); value != nil {
				payload["last_dial_error"] = value
			}
			backends = append(backends, payload)
		}
		item := counterPayload(l.ID, true, false, &l.counters)
		item["listen"] = l.Listen
		item["policy"] = l.Policy
		item["rotation_interval_seconds"] = l.RotationIntervalSeconds
		item["backends"] = backends
		listeners = append(listeners, item)
	}
	result := map[string]any{"running": true, "listeners": listeners}
	if len(rt.failedListeners) > 0 {
		result["failed_listeners"] = rt.failedListeners
	}
	return result
}

func counterPayload(id string, enabled, draining bool, c *counters) map[string]any {
	return map[string]any{
		"id": id, "enabled": enabled, "draining": draining,
		"upload": c.Upload.Load(), "download": c.Download.Load(), "active": c.Active.Load(),
		"selections": c.Selections.Load(), "dial_failures": c.DialFailures.Load(),
	}
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}
