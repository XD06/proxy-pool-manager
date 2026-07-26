package main

import "testing"

func TestTimeWindowKeepsBackendUntilWindowExpires(t *testing.T) {
	a := &backend{backendConfig: backendConfig{ID: "a", Enabled: true, Weight: 1}}
	b := &backend{backendConfig: backendConfig{ID: "b", Enabled: true, Weight: 1}}
	l := &listener{listenerConfig: listenerConfig{Policy: "time_window", RotationIntervalSeconds: 60}, backends: []*backend{a, b}}
	first := l.pick(nil)
	if first == nil {
		t.Fatal("expected a backend")
	}
	for i := 0; i < 4; i++ {
		if got := l.pick(nil); got != first {
			t.Fatalf("window changed from %q to %q", first.ID, got.ID)
		}
	}
	if got := l.pick(map[*backend]bool{first: true}); got == first || got == nil {
		t.Fatal("expected a healthy replacement after a dial failure")
	}
}

func TestWeightedRoundRobinHonorsWeights(t *testing.T) {
	l := &listener{
		listenerConfig: listenerConfig{Policy: "weighted_round_robin"},
		backends: []*backend{
			{backendConfig: backendConfig{ID: "a", Enabled: true, Weight: 1}},
			{backendConfig: backendConfig{ID: "b", Enabled: true, Weight: 3}},
		},
	}
	seen := map[string]int{}
	for i := 0; i < 8; i++ {
		seen[l.pick(nil).ID]++
	}
	if seen["a"] != 2 || seen["b"] != 6 {
		t.Fatalf("unexpected weighted distribution: %#v", seen)
	}
}

func TestRoundRobinSkipsDrainingAndExcludedBackends(t *testing.T) {
	draining := &backend{backendConfig: backendConfig{ID: "draining", Enabled: true, Draining: true, Weight: 1}}
	active := &backend{backendConfig: backendConfig{ID: "active", Enabled: true, Weight: 1}}
	l := &listener{listenerConfig: listenerConfig{Policy: "round_robin"}, backends: []*backend{draining, active}}
	if got := l.pick(nil); got != active {
		t.Fatalf("got %q, want active", got.ID)
	}
	if got := l.pick(map[*backend]bool{active: true}); got != nil {
		t.Fatalf("got %q, want nil", got.ID)
	}
}
