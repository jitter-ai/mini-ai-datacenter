#!/usr/bin/env python3
from __future__ import annotations
import os, sys, subprocess, time, shutil
from pathlib import Path
from typing import Sequence, Optional

# ================================================================
# Single-node K3s cluster bootstrap
# * Installs / verifies K3s
# * Installs Rancher via Helm (no cert-manager)
# * Applies NodePort service from bootstrap.yaml (if present)
# * Prints access banner (HTTPS only)
# ================================================================

# Refactored into four classes:
#   HostEnv           -> host/user discovery & kubeconfig handling
#   K3sCluster        -> install & ensure K3s readiness + kubectl shim
#   RancherDeployment -> helm install Rancher (https only)
#   ClusterBanner     -> verification & final access banner

# A lightweight logger is introduced for uniform, colorized output.

class Log:
    COLORS = {
        'INFO': '\u001b[36m',      # cyan
        'SUCCESS': '\u001b[32m',   # green
        'WARN': '\u001b[33m',      # yellow
        'ERROR': '\u001b[31m',     # red
        'RESET': '\u001b[0m',
        'SECTION': '\u001b[35m',   # magenta
    }

    @classmethod
    def _stamp(cls, level: str, msg: str) -> str:
        color = cls.COLORS.get(level, '')
        reset = cls.COLORS['RESET']
        return f"[{level}] {color}{msg}{reset}" if color else f"[{level}] {msg}"

    @classmethod
    def info(cls, msg: str):
        print(cls._stamp('INFO', msg))

    @classmethod
    def success(cls, msg: str):
        print(cls._stamp('SUCCESS', msg))

    @classmethod
    def warn(cls, msg: str):
        print(cls._stamp('WARN', msg))

    @classmethod
    def error(cls, msg: str):
        print(cls._stamp('ERROR', msg))

    @classmethod
    def section(cls, title: str):
        bar = '=' * 64
        print(f"{cls.COLORS['SECTION']}{bar}\n{title}\n{bar}{cls.COLORS['RESET']}")

def run(cmd: Sequence[str], *, check: bool = True, capture: bool = False,
        env: Optional[dict] = None, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    printable = " ".join(cmd)
    Log.info(f"$ {printable}" + (f" (cwd={cwd})" if cwd else ""))
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, env=env, cwd=cwd)

class HostEnv:
    def __init__(self):
        self.user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"
        self.user_home = Path(f"/home/{self.user}") if self.user != "root" else Path("/root")
        self.env = os.environ.copy()
        self._primary_ip = self._detect_primary_ip()

    @property
    def primary_ip(self) -> str:
        return self._primary_ip

    @property
    def kubeconfig_path(self) -> Path:
        return self.user_home / ".kube" / "config"

    def _detect_primary_ip(self) -> str:
        r = subprocess.run(["hostname", "-I"], text=True, capture_output=True, check=False)
        parts = (r.stdout or "").split()
        return parts[0] if parts else "127.0.0.1"

    def ensure_kube_dir(self):
        kube_dir = self.user_home / ".kube"
        run(["mkdir", "-p", str(kube_dir)], check=False)
        run(["chown", "-R", f"{self.user}:{self.user}", str(kube_dir)], check=False)
        run(["chmod", "700", str(kube_dir)], check=False)

    def kube_env(self) -> dict:
        e = os.environ.copy()
        e["KUBECONFIG"] = str(self.kubeconfig_path)
        return e

class K3sCluster:
    def __init__(self, h: HostEnv):
        self.h = h

    def ensure_hosts(self):
        hostname = subprocess.run(["hostname"], text=True, capture_output=True).stdout.strip()
        ip = self.h.primary_ip or "127.0.1.1"
        Log.info(f"Checking /etc/hosts entry for {hostname} ({ip})")
        grep_cmd = ["grep", "-Eq", rf"^[0-9A-Fa-f:.]+\s+{hostname}(\s|$)", "/etc/hosts"]
        if subprocess.run(grep_cmd, check=False).returncode != 0:
            line = f"{ip} {hostname}"
            Log.info(f"Adding: {line}")
            run(["bash", "-c", f"echo '{line}' >> /etc/hosts"], check=False)
        else:
            Log.info("Host entry already present.")

    def install_or_verify(self):
        kcfg = Path("/etc/rancher/k3s/k3s.yaml")
        if subprocess.run(["systemctl", "is-active", "--quiet", "k3s"], check=False).returncode == 0 and kcfg.exists():
            Log.info("k3s server already active.")
        else:
            Log.info("Installing K3s server (traefik disabled)...")
            run(["bash", "-lc", "curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC='server --disable traefik' sh -"], check=True)

        Log.info("Waiting for K3s kubeconfig...")
        for _ in range(60):
            if kcfg.exists():
                break
            time.sleep(2)
        else:
            Log.error("Timed out waiting for /etc/rancher/k3s/k3s.yaml")
            sys.exit(1)

        self._fix_kubeconfig_permissions()
        self._wait_nodes_ready()

    def _fix_kubeconfig_permissions(self):
        k3s_cfg = Path("/etc/rancher/k3s/k3s.yaml")
        if not k3s_cfg.exists():
            Log.error("kubeconfig not found, skipping copy.")
            return
        self.h.ensure_kube_dir()
        dest = self.h.kubeconfig_path
        run(["cp", str(k3s_cfg), str(dest)], check=False)
        run(["chown", "-R", f"{self.h.user}:{self.h.user}", str(dest.parent)], check=False)
        run(["chmod", "600", str(dest)], check=False)
        Log.info(f"Kubeconfig copied to {dest}")

    def _wait_nodes_ready(self):
        Log.info("Checking node readiness (2 min timeout)...")
        for _ in range(60):
            result = subprocess.run(
                ["k3s", "kubectl", "get", "nodes", "--no-headers"],
                text=True, capture_output=True
            )
            lines = result.stdout.strip().splitlines()
            total = len(lines)
            ready = sum(1 for l in lines if "Ready" in l)
            if total > 0 and ready == total:
                Log.success(f"All {ready}/{total} nodes Ready.")
                return
            time.sleep(2)
        Log.warn("Nodes not fully ready after 2 minutes. Continue verifying with `kubectl get nodes`.")

    def ensure_kubectl(self):
        if shutil.which("kubectl"):
            Log.info("kubectl already present in PATH.")
            return
        if not shutil.which("k3s"):
            Log.warn("k3s binary not found; cannot create kubectl shim.")
            return
        target_path = Path("/usr/local/bin/kubectl")
        if not target_path.exists():
            Log.info("Creating kubectl shim using 'k3s kubectl' ...")
            shim_contents = "#!/bin/sh\nexec k3s kubectl \"$@\"\n"
            try:
                with open(target_path, "w") as f:
                    f.write(shim_contents)
                os.chmod(target_path, 0o755)
                Log.success(f"kubectl shim created at {target_path}")
            except Exception as e:
                Log.error(f"Failed to create kubectl shim: {e}")

    def apply_bootstrap_yaml(self):
        tmpl = Path(__file__).resolve().parent / "templates" / "bootstrap.yaml"
        if tmpl.exists():
            Log.info(f"Applying {tmpl}")
            run(["kubectl", "apply", "-f", str(tmpl)], check=False, env=self.h.kube_env())
        else:
            Log.info("No bootstrap.yaml found; skipping.")

    def ensure_helm(self):
        if shutil.which("helm"):
            Log.info("helm present.")
            return
        Log.info("Installing helm (get-helm-3)...")
        run(["bash", "-lc", "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"], check=True)
        if not shutil.which("helm"):
            Log.error("helm not found after install attempt.")
            sys.exit(2)

class RancherDeployment:
    def __init__(self, h: HostEnv):
        self.h = h

    def install(self):
        ip = self.h.primary_ip
        # HTTPS only: use NodePort 30444 (assuming bootstrap.yaml defines it)
        external_https = f"https://{ip}:30444"
        hostname = f"{ip}.nip.io"
        env = self.h.kube_env()

        Log.info("Checking if Rancher helm release already exists (skip install if present)...")
        status_cp = subprocess.run([
            "helm", "status", "rancher", "-n", "cattle-system"
        ], text=True, capture_output=True, env=env, check=False)
        if status_cp.returncode == 0:
            Log.info("Rancher helm release already present; skipping install/upgrade per request.")
            return

        Log.info("Rancher release not found; proceeding with initial install (no cert-manager)...")
        run(["helm", "repo", "add", "rancher-stable", "https://releases.rancher.com/server-charts/stable"], check=False)
        run(["helm", "repo", "update"], check=False)

        helm_cmd = [
            "helm", "--kubeconfig", str(self.h.kubeconfig_path),
            "upgrade", "--install", "rancher", "rancher-stable/rancher",
            "--namespace", "cattle-system",
            "--create-namespace",
            "--set", "replicas=1",
            "--set", "ingress.enabled=false",
            "--set", "ingress.tls.source=secret",
            "--set", f"hostname={hostname}",
            "--set", "service.type=ClusterIP",
            "--set", "global.cattle.psp.enabled=false",
            "--set", "bootstrapPassword=admin123",
            "--set", "extraEnv[0].name=CATTLE_SERVER_URL",
            "--set", f"extraEnv[0].value={external_https}",
        ]
        run(helm_cmd, check=True, env=env)

    def wait_ready(self, timeout_sec: int = 300):
        env = self.h.kube_env()
        Log.info("Waiting for Rancher to be ready...")
        try:
            run(["kubectl", "-n", "cattle-system", "rollout", "status", "deploy/rancher", f"--timeout={timeout_sec}s"],
                check=True, env=env)
            Log.success("Rancher deployment is ready.")
            return
        except subprocess.CalledProcessError:
            Log.warn("rollout status timed out; checking pods directly...")

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            r = subprocess.run(
                ["kubectl", "-n", "cattle-system", "get", "pods", "-l", "app=rancher",
                 "-o", "jsonpath={range .items[*]}{.status.phase}{\"\\n\"}{end}"],
                text=True, capture_output=True, env=env, check=False
            )
            phases = [p.strip() for p in (r.stdout or "").splitlines() if p.strip()]
            if phases and all(p == "Running" for p in phases):
                Log.success("Rancher pods are Running.")
                return
            time.sleep(3)
        Log.warn("Rancher not ready within timeout; check with: kubectl -n cattle-system get pods")

class ClusterBanner:
    def __init__(self, h: HostEnv):
        self.h = h

    def verify_cluster(self):
        Log.section("Cluster Verification")
        env = self.h.kube_env()
        result = run(["kubectl", "get", "nodes"], check=False, capture=True, env=env)
        print(result.stdout or Log.warn("Could not get nodes output"))
        if result.stdout and "Ready" in result.stdout:
            Log.success("Cluster appears healthy and responsive.")
        else:
            Log.warn("Cluster not fully ready, check systemctl status k3s and logs.")

    def access(self):
        ip = self.h.primary_ip
        Log.section("Rancher Access")
        # Only HTTPS printed now
        Log.info(f"HTTPS Endpoint : https://{ip}:30444")
        Log.info("Namespace      : cattle-system")
        Log.info("NodePort Svc    : rancher-nodeport")

    def final(self):
        Log.section("Bootstrap Complete")
        Log.info(f"You can now run: KUBECONFIG={self.h.kubeconfig_path} kubectl -n cattle-system get pods")

class BootstrapOrchestrator:
    def __init__(self):
        self.h = HostEnv()
        self.cluster = K3sCluster(self.h)
        self.rancher = RancherDeployment(self.h)
        self.banner = ClusterBanner(self.h)

    def require_sudo(self):
        if os.geteuid() != 0:
            Log.error("This script must be run as root (sudo). Exiting.")
            sys.exit(1)

    def show_mode(self):
        Log.section("K3s Cluster Bootstrap")
        Log.info(f"Current user : {self.h.user}")
        Log.info(f"Home dir     : {self.h.user_home}")
        Log.info(f"Host IP      : {self.h.primary_ip}")

    def run(self):
        self.require_sudo()
        self.show_mode()
        self.cluster.ensure_hosts()
        self.cluster.install_or_verify()
        self.cluster.ensure_kubectl()
        self.cluster.apply_bootstrap_yaml()
        self.cluster.ensure_helm()
        self.rancher.install()
        self.rancher.wait_ready()
        self.banner.verify_cluster()
        self.banner.access()
        self.banner.final()

def main():
    orchestrator = BootstrapOrchestrator()
    orchestrator.run()

if __name__ == "__main__":
    main()
