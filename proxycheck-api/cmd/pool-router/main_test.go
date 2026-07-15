package main

import "testing"

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
