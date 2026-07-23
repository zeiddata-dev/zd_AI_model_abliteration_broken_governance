# Namespace Isolation and Pod Security Standards — Abliterated Model Runtime

**Scope:** target-state design. Same caveat as Paper 1: no cluster exists yet, this is the isolation boundary to build toward.

## 1. Why Namespace Isolation Matters Here

The current repo runs one workload on one VM with one bearer token as the entire access boundary. That's fine for a single-tenant research box. It stops being fine the moment this cluster hosts anything else, staging environments, other client workloads, CI runners, because a Kubernetes namespace alone is not a security boundary by default. It's an organizational one. The controls below are what turn it into an actual boundary.

## 2. Namespace Design

- Dedicated namespace for the abliterated model workload only: `ai-gov-abliteration`. No other workload, including internal tooling, shares it.
- NetworkPolicy default-deny on ingress and egress for the namespace, then explicit allow rules:
  - Ingress: only from the proxy/gateway layer (the Caddy equivalent, or an ingress controller replacing it), on the model's serving port.
  - Egress: only to what Ollama actually needs at runtime (model registry pull at build/update time, not runtime; DNS; nothing else). No open egress to the internet from the pod. This directly closes the gap the README calls out, Ollama has zero auth of its own, by making the network layer refuse to let anything but the authenticated proxy reach it, regardless of what's misconfigured at the application layer.
- ResourceQuota and LimitRange on the namespace: cap GPU count, CPU, memory, and pod count. A quota is also a governance control, it bounds how much compute this model can ever consume even if something inside the app logic goes wrong.

## 3. Pod Security Standards (PSS)

Kubernetes PSS defines three levels: Privileged, Baseline, Restricted. Enforce via the built-in Pod Security admission controller (labels on the namespace), not a third-party policy engine, unless you already run one (OPA Gatekeeper, Kyverno) for other reasons.

- Target: **Restricted** for this namespace. Restricted requires everything in Paper 1 Section 3 (non-root, no privilege escalation, dropped capabilities, seccomp) as a matter of admission, not developer discipline. A pod spec that violates it is rejected at the API server, before it ever reaches a node.
- Namespace labels:
  ```
  pod-security.kubernetes.io/enforce: restricted
  pod-security.kubernetes.io/enforce-version: latest
  pod-security.kubernetes.io/audit: restricted
  pod-security.kubernetes.io/warn: restricted
  ```
- One known friction point: GPU workloads sometimes need `NET_RAW` or specific device access that Restricted blocks by default. Resolve this through the NVIDIA device plugin's supported pattern (device requests, not capability grants), not by dropping to Baseline for the whole namespace.

## 4. Implementation Path

1. Stand up the namespace with PSS labels set to `restricted` from day one. Retrofitting Restricted onto a namespace already running Baseline pods is far more painful than starting there.
2. Write the default-deny NetworkPolicy first, before any workload is deployed into the namespace. Add allow rules one at a time, each tied to a specific observed need, not a guess.
3. Set ResourceQuota before the first GPU request lands, so there's no window where an unbounded pod could be scheduled.
4. Run `kubectl label --dry-run` and a policy scan (`kubescape`, `polaris`) in CI against every manifest before it merges, so violations are caught before they reach the cluster.
5. Periodic audit: review NetworkPolicy allow-lists and RBAC bindings on a fixed cadence (recommend monthly, tied to the same cadence as bearer token rotation in the current setup).

## 5. Open Questions

- Is this namespace going into an existing shared cluster or a new one built specifically for this workload? Isolation guarantees differ significantly between the two, and this isn't decided.
- Any other workloads expected to share the cluster (not the namespace) in the next 6 months? If yes, cluster-level admission policy needs to be decided now, not after the second workload shows up.
