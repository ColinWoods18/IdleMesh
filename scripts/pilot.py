"""Operator helper for the three-machine pilot test, run on the coordinator Mac.

Wraps the coordinator API (session cookie and CSRF token handled for you). The session is saved to
~/.local/share/difa/pilot-session.json (owner-only permissions); sessions expire after 30 idle minutes,
after which `login` signs in again.

  python scripts/pilot.py setup                 create the group (once per fresh database) and sign in
  python scripts/pilot.py login                 sign in again to the saved group
  python scripts/pilot.py code                  one-time enrollment code for a device (valid 10 minutes)
  python scripts/pilot.py devices               devices, their state, and whether their worker runs
  python scripts/pilot.py policy "Ubuntu VM" --pause      owner controls: --pause, --resume, --cap-gb N,
                                                          --allow-battery (the agent's own limits still apply)
  python scripts/pilot.py deploy                plan, prefetch, and load the test model on all devices
  python scripts/pilot.py status                current deployment, prefetch progress, and stage layout
  python scripts/pilot.py ask "Say hello."      one synthetic request, with timing
  python scripts/pilot.py bench --n 10          repeated requests, median timing, saved as JSON

Only use synthetic prompts: llama.cpp RPC is a research backend (spec 6.2).
"""
import argparse
import getpass
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ssl

import httpx

DATA = Path(os.environ.get("DIFA_DATA_DIR", Path.home() / ".local" / "share" / "difa"))
SESSION = DATA / "pilot-session.json"
ROOT = Path(__file__).resolve().parents[1]
GB = 1e9


def save(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    fd = os.open(SESSION, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)


def load() -> dict:
    if not SESSION.exists():
        sys.exit("No saved session. Run: python scripts/pilot.py setup")
    return json.loads(SESSION.read_text())


def verify(base: str):
    """Trust exactly this coordinator's own control certificate (it is self-signed and on this machine)."""
    if not base.startswith("https://"):
        return True
    cert = Path(os.environ.get("DIFA_CONTROL_CERT", DATA / "coordinator-control.crt"))
    if not cert.exists():
        sys.exit(f"{cert} not found: start the coordinator with scripts/dev_server.py first")
    ctx = ssl.create_default_context(cafile=str(cert))
    ctx.check_hostname = False  # pinned: the only trusted certificate is the coordinator's own
    return ctx


def client(state: dict) -> httpx.Client:
    c = httpx.Client(base_url=state["base"], timeout=60, verify=verify(state["base"]))
    if state.get("cookie"):
        c.cookies.set("difa_session", state["cookie"])
        c.headers["X-CSRF-Token"] = state["csrf"]
    return c


def check(r: httpx.Response) -> dict:
    if r.status_code == 401:
        sys.exit("Session expired or invalid. Run: python scripts/pilot.py login")
    if r.status_code >= 400:
        try:
            err = r.json()["error"]
            sys.exit(f"{r.status_code} {err['code']}: {err['message']}")
        except (ValueError, KeyError):
            sys.exit(f"{r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def sign_in(state: dict, passphrase: str) -> None:
    c = httpx.Client(base_url=state["base"], timeout=60, verify=verify(state["base"]))
    r = c.post("/api/v1/auth/login", json={"group_id": state["group_id"], "login": state["login"],
                                           "passphrase": passphrase})
    info = check(r)
    state["cookie"], state["csrf"] = r.cookies.get("difa_session"), info["csrf_token"]
    save(state)
    print(f"signed in as {state['login']} to {state['group_id']}")


def cmd_setup(a) -> None:
    state = {"base": a.base, "login": a.login}
    p1 = getpass.getpass("New owner passphrase (15+ characters): ")
    if getpass.getpass("Repeat passphrase: ") != p1:
        sys.exit("Passphrases differ.")
    r = httpx.post(f"{a.base}/api/v1/groups", json={"name": a.name, "owner_login": a.login,
                                                    "owner_display_name": a.display_name, "passphrase": p1}, timeout=60,
                   verify=verify(a.base))
    g = check(r)
    state["group_id"] = g["group_id"]
    print(f"group {g['group_id']} created (fingerprint {g['fingerprint']})")
    print(f"RECOVERY CODE (store it somewhere safe; shown once): {g['recovery_code']}")
    sign_in(state, p1)


def cmd_login(a) -> None:
    state = load()
    sign_in(state, getpass.getpass(f"Passphrase for {state['login']}: "))


def cmd_code(a) -> None:
    out = check(client(load()).post("/api/v1/devices/enrollment-codes"))
    print(out["code"])
    print(f"expires {out['expires_at']}; give it to one device's `difa-agent enroll --code ...`")
    fp = out.get("coordinator_fingerprint")
    if fp:
        print(f"coordinator fingerprint: {' '.join(fp[i:i + 4] for i in range(0, 64, 4))}")
        print(f"  on the device: --coordinator-fingerprint {fp[:16]}  (or the whole fingerprint)")


def cmd_devices(a) -> None:
    state = load()
    devs = check(client(state).get(f"/api/v1/groups/{state['group_id']}/devices"))["devices"]
    if not devs:
        print("no devices enrolled")
    for d in devs:
        print(f"{d['name']:<16} {d['state']:<12} worker={'running' if d['worker_running'] else '-':<8} "
              f"budget={d['budget_bytes'] / GB:6.1f} GB  power={d['power']:<8} {d['os']}/{d['arch']}  {d['device_id']}")


def cmd_policy(a) -> None:
    state = load()
    c = client(state)
    devs = check(c.get(f"/api/v1/groups/{state['group_id']}/devices"))["devices"]
    match = [d for d in devs if d["name"] == a.device or d["device_id"] == a.device]
    if len(match) != 1:
        sys.exit(f"No single device named {a.device!r}. Devices: {', '.join(d['name'] for d in devs)}")
    change = {}
    if a.pause or a.resume:
        change["paused"] = bool(a.pause)
    if a.cap_gb is not None:
        change["memory_cap_bytes"] = int(a.cap_gb * GB)
    if a.allow_battery:
        change["allow_battery"] = True
    if not change:
        sys.exit("Nothing to change: use --pause, --resume, --cap-gb, or --allow-battery.")
    out = check(c.patch(f"/api/v1/devices/{match[0]['device_id']}/policy", json=change))
    print(f"{a.device}: {json.dumps(out['policy'])}")


def show_stages(expl: dict) -> None:
    budget = expl.get("latency_budget") or {}
    for s in expl.get("stages", []):
        print(f"  {s['name']:<16} layers {s['layers'][0]:>3}-{s['layers'][1] - 1:<3} "
              f"head={'yes' if s.get('holds_output_head') else 'no ':<3} transport={s.get('transport', 'local'):<9} "
              f"endpoint={s.get('endpoint') or '-':<22} rtt={s.get('rtt_ms', 0)} ms ({s.get('rtt_source', 'heartbeat')})"
              + (f", planned with {s['planned_rtt_ms']} ms" if "planned_rtt_ms" in s else "")
              + (f"  [rtt measurement failed: {s['rtt_measurement_error']}]" if s.get("rtt_measurement_error") else ""))
    planned = expl.get("planned_latency_budget")
    if planned:
        print(f"  planned:   {planned.get('per_token_ms')} ms/token ({planned.get('tokens_per_s')} tokens/s) "
              f"from {planned.get('rtt_source') or 'heartbeat'} round trips")
    if budget:
        print(f"  predicted: {budget.get('per_token_ms')} ms/token ({budget.get('tokens_per_s')} tokens/s), "
              f"network {budget.get('network_ms')} ms, slowest stage {budget.get('dominant_stage')}"
              + (" (round trips timed through the tunnels at load)" if planned else ""))


def wait_deployment(c: httpx.Client, gid: str, timeout: float) -> dict:
    end, last = time.time() + timeout, None
    while time.time() < end:
        d = check(c.get(f"/api/v1/groups/{gid}/deployment"))
        line = d["state"]
        for p in d.get("prefetch", []):
            pct = 100 * p["bytes_done"] / p["bytes_total"] if p["bytes_total"] else 100
            line += f" | {p['name']}: {p['state']} {pct:.0f}%"
        if line != last:
            print(line, flush=True)
            last = line
        if d["state"] in ("ready", "failed", "unavailable"):
            return d
        time.sleep(2)
    sys.exit("timed out waiting for the deployment")


def cmd_deploy(a) -> None:
    state = load()
    c, gid = client(state), state["group_id"]
    c.timeout = httpx.Timeout(900)
    t0 = time.time()
    r = c.post(f"/api/v1/groups/{gid}/deployment", json={"model": a.model, "require_all": not a.allow_subset,
                                                          "prefetch": not a.no_prefetch})
    body = check(r)
    print(f"deployment {body['deployment_id']}: {body['state']}")
    show_stages(body.get("explanation", {}))
    final = wait_deployment(c, gid, a.timeout) if body["state"] != "ready" else body
    print(f"{final['state']} after {time.time() - t0:.1f} s")
    if final["state"] != "ready":
        sys.exit(f"deployment did not become ready: {final.get('error') or 'see coordinator log'}")
    loaded = check(c.get(f"/api/v1/groups/{gid}/deployment")).get("explanation") or {}
    if loaded.get("planned_latency_budget"):
        print("as loaded:")
        show_stages(loaded)


def cmd_status(a) -> None:
    state = load()
    d = check(client(state).get(f"/api/v1/groups/{state['group_id']}/deployment"))
    print(f"{d.get('deployment_id')}: {d['state']} ({d.get('model')})")
    for p in d.get("prefetch", []):
        print(f"  prefetch {p['name']}: {p['state']} {p['bytes_done']}/{p['bytes_total']} bytes {p.get('error') or ''}")
    if d.get("error"):
        print(f"  error: {d['error']}")
    show_stages(d.get("explanation", {}))


def ask(c: httpx.Client, prompt: str, max_tokens: int) -> dict:
    t0 = time.time()
    r = check(c.post("/api/v1/requests", json={"model": "group-default", "visibility": "private",
                                                "max_output_tokens": max_tokens,
                                                "messages": [{"role": "user", "content": prompt}]}))
    while True:
        s = check(c.get(r["status_url"]))
        if s["status"] in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(0.2)
    elapsed = time.time() - t0
    conv = check(c.get(f"/api/v1/conversations/{s['branch_id']}"))
    answer = next((m["content"] for m in reversed(conv["messages"]) if m["role"] == "assistant"), "")
    return {"status": s["status"], "error_code": s["error_code"], "elapsed_s": round(elapsed, 3),
            "completion_tokens": s["usage"]["completion_tokens"], "answer": answer}


def cmd_ask(a) -> None:
    res = ask(client(load()), a.prompt, a.max_tokens)
    print(res["answer"])
    print(f"-- {res['status']} in {res['elapsed_s']} s, {res['completion_tokens']} tokens"
          + (f", error {res['error_code']}" if res["error_code"] else ""))


def prediction_check(ok: list[dict], budget: dict) -> dict:
    """Fit elapsed = fixed + per_token * tokens across requests, and compare the slope with the prediction.

    The slope is decode time per token as the user sees it; the intercept absorbs prompt processing and
    polling. Spec 7.2 asks predicted and traced per-token time to agree within 30 percent.
    """
    pts = [(r["completion_tokens"], r["elapsed_s"]) for r in ok if r["completion_tokens"]]
    if len(pts) < 3 or len({t for t, _ in pts}) < 2:
        return {"note": "need at least 3 completed requests with different lengths to fit ms/token"}
    mx, my = statistics.fmean(t for t, _ in pts), statistics.fmean(e for _, e in pts)
    slope = sum((t - mx) * (e - my) for t, e in pts) / sum((t - mx) ** 2 for t, _ in pts)
    out = {"fit_ms_per_token": round(slope * 1000, 2), "fit_fixed_s": round(my - slope * mx, 3)}
    pred = budget.get("per_token_ms")
    if pred and slope > 0:
        err = (pred - slope * 1000) / (slope * 1000) * 100
        out.update({"predicted_ms_per_token": pred, "rtt_source": budget.get("rtt_source"),
                    "prediction_error_pct": round(err, 1), "within_30pct": abs(err) <= 30})
    return out


def cmd_bench(a) -> None:
    state = load()
    c = client(state)
    dep = check(c.get(f"/api/v1/groups/{state['group_id']}/deployment"))
    if dep["state"] != "ready":
        sys.exit("No ready deployment. Run: python scripts/pilot.py deploy")
    prompts = ["Describe a tide pool in three sentences.", "List five colours of coral.",
               "Explain why the sky looks blue, briefly.", "Write a short haiku about rain."]
    runs = []
    for i in range(a.n):
        res = ask(c, prompts[i % len(prompts)], a.max_tokens)
        rate = res["completion_tokens"] / res["elapsed_s"] if res["elapsed_s"] else 0
        print(f"{i + 1:>3}: {res['status']:<9} {res['elapsed_s']:6.2f} s  {res['completion_tokens']:>3} tokens  "
              f"{rate:5.1f} tokens/s end to end", flush=True)
        runs.append(res)
    ok = [r for r in runs if r["status"] == "completed"]
    summary = {"requests": len(runs), "completed": len(ok),
               "median_elapsed_s": round(statistics.median(r["elapsed_s"] for r in ok), 3) if ok else None,
               "median_tokens_per_s": round(statistics.median(r["completion_tokens"] / r["elapsed_s"] for r in ok), 2)
               if ok else None}
    summary.update(prediction_check(ok, (dep.get("explanation") or {}).get("latency_budget") or {}))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "benchmarks" / "results" / f"pilot-3machine-{stamp}.json"
    path.write_text(json.dumps({"benchmark": "pilot-3machine", "utc": stamp, "network": a.network, "deployment": dep,
                                "summary": summary, "runs": [{k: v for k, v in r.items() if k != "answer"}
                                                              for r in runs]}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="DIFA pilot operator helper (run on the coordinator Mac)")
    ap.add_argument("--base", default="https://127.0.0.1:8765",
                    help="coordinator address for setup (later commands use the saved one)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--name", default="Pilot")
    s.add_argument("--login", default="owner")
    s.add_argument("--display-name", default="Pilot owner")
    sub.add_parser("login")
    sub.add_parser("code")
    sub.add_parser("devices")
    pol = sub.add_parser("policy")
    pol.add_argument("device", help="device name as shown by `devices`, or its id")
    grp = pol.add_mutually_exclusive_group()
    grp.add_argument("--pause", action="store_true")
    grp.add_argument("--resume", action="store_true")
    pol.add_argument("--cap-gb", type=float)
    pol.add_argument("--allow-battery", action="store_true")
    d = sub.add_parser("deploy")
    d.add_argument("--model", default="qwen2.5-0.5b-q8")
    d.add_argument("--no-prefetch", action="store_true")
    d.add_argument("--allow-subset", action="store_true", help="let the planner leave devices out")
    d.add_argument("--timeout", type=float, default=3600)
    sub.add_parser("status")
    q = sub.add_parser("ask")
    q.add_argument("prompt")
    q.add_argument("--max-tokens", type=int, default=64)
    b = sub.add_parser("bench")
    b.add_argument("--n", type=int, default=10)
    b.add_argument("--max-tokens", type=int, default=64)
    b.add_argument("--network", default=None,
                   help='how each machine is connected, recorded with the results, e.g. "Mac=ethernet HP=wifi VM=wifi"')
    a = ap.parse_args()
    {"setup": cmd_setup, "login": cmd_login, "code": cmd_code, "devices": cmd_devices, "deploy": cmd_deploy,
     "status": cmd_status, "ask": cmd_ask, "bench": cmd_bench, "policy": cmd_policy}[a.cmd](a)


if __name__ == "__main__":
    main()
