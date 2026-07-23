# Container and Workload Hardening — Abliterated Model Runtime

**Scope:** target-state design for containerizing the Ollama/abliterated-model workload currently running as a bare process on a GPU VM (`setup_heretic.sh`). Current deployment has no container runtime, so everything below is greenfield, not a retrofit of existing manifests.

## 1. Threat Model, Short Version

An abliterated model has no refusal layer. If the container escapes, or the host is compromised, the blast radius is not "leaked data," it's an unrestricted model plus GPU compute in an attacker's hands. Hardening here is about containment, not about the model's output.

## 2. Base Image

- Build from a minimal CUDA runtime image (`nvidia/cuda:*-runtime`, not `-devel`) matched to the exact driver/CUDA version the g6.xlarge AMI ships. No compilers, no package managers left in the final layer.
- Multi-stage build: pull/quantize the GGUF model in a build stage, copy only the artifact and Ollama binary into the runtime stage.
- Pin every base image by digest, not tag. `FROM nvidia/cuda@sha256:...`
- Rebuild on a schedule (weekly) even with no code changes, to pick up upstream CVE patches.

## 3. Runtime Hardening

- Run as non-root. Ollama has no requirement for root; create a dedicated UID/GID in the image.
- `readOnlyRootFilesystem: true` at the pod level, with an explicit writable `emptyDir` for Ollama's model cache and scratch space only.
- Drop all Linux capabilities, add back only what's proven necessary (usually none for Ollama).
- `allowPrivilegeEscalation: false`, `privileged: false`, no `hostNetwork`, no `hostPID`.
- Seccomp profile: `RuntimeDefault` at minimum; author a custom profile once the syscall surface is confirmed via `strace` in staging.
- GPU access via the NVIDIA device plugin, not privileged mode or `hostPath` device mounts. This is the one place teams cut corners and it defeats most of the isolation above.

## 4. Workload Identity and RBAC

Kubernetes RBAC governs who can act on the workload, not what the workload can say.

- One ServiceAccount per workload (`llm-inference-sa`), no default ServiceAccount token auto-mounted (`automountServiceAccountToken: false`) unless the pod actually calls the API server.
- Role scoped to the workload's namespace only: `get`/`list` on its own Secrets and ConfigMaps, nothing else. No `create`, no `delete`, no wildcard verbs or resources.
- Separate the human-facing RBAC (who can `kubectl exec` into the pod, who can read logs, who can rotate the bearer token) from the workload's own RBAC. Bind human roles to actual identities via OIDC/IAM, not shared kubeconfig.
- `kubectl exec` and `port-forward` into this namespace should require its own RoleBinding, reviewed separately from general cluster access. Given the repo's current access model is "one bearer token," treat interactive pod access as equally sensitive and audit it the same way.

## 5. Implementation Path

1. Containerize `setup_heretic.sh` into the multi-stage build above; validate GPU passthrough with the device plugin in a dev cluster first.
2. Author the Pod spec with the security context fields listed in Section 3; run a conformance check (`kubectl-who-can`, or `kubescape`) before first deploy.
3. Create the workload ServiceAccount and Role/RoleBinding pair; deny-by-default, add permissions only when a concrete failure demands it.
4. Add a pre-deploy CI gate that fails the build if any of: root user, missing seccomp profile, privileged flag, or wildcard RBAC verb is detected in the manifest.
5. Document the human-access RoleBindings (who, why, expiry) in the same repo as the manifests, reviewed at each rotation of the bearer token.

## 6. Open Questions

- Does the eventual cluster run on the same AWS account as the current VM, or a separate governance-tier account? This changes the IAM boundary and isn't decided yet.
- Single-tenant cluster for this workload only, or shared cluster with namespace isolation (covered in Paper 2)? Recommend single-tenant given the abliterated model's risk profile, pending your call.
