from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import subprocess, re, time, os, signal

app = FastAPI(title="K38 Node Status")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

NODES = [
    ("ECS 香港", "127.0.0.1", "root", True),
    ("三万八", "100.109.169.92", "jagerm3uitra", False),
    ("小四", "100.124.200.40", "jagerstudiom4max", False),
    ("大傻", "100.72.1.120", "jager-dgx", False),
    ("二傻", "100.98.235.68", "jager-dgx-2", False),
]

SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")

def _r(host, user, cmd, is_local):
    try:
        if is_local:
            r = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=4)
        else:
            r = subprocess.run(
                ["ssh", "-i", SSH_KEY, "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no", f"{user}@{host}"] + cmd,
                shell=False, capture_output=True, text=True, timeout=6
            )
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except subprocess.SubprocessError:
        return ""
    except Exception:
        return ""

def _coll(name, host, user, is_local):
    try:
        u = _r(host, user, ["uname"], is_local)
        mac = "Darwin" in u
        cpu = mem = disk = uptime = "--"
        if not u: return (name, {"online": False, "cpu": "--", "mem": "--", "disk": "--", "uptime": "--"})
        if mac:
            o = _r(host, user, ["sh", "-c", "top -l 1 -n 0 2>/dev/null | grep 'CPU usage'"], is_local)
            m = re.search(r'(\d+\.?\d*)% idle', o)
            if m: cpu = f"{100 - float(m.group(1)):.1f}%"
            o = _r(host, user, ["sh", "-c", "vm_stat | head -8"], is_local)
            a = re.search(r'Pages active:\s+(\d+)', o)
            w = re.search(r'Pages wired down:\s+(\d+)', o)
            f = re.search(r'Pages free:\s+(\d+)', o)
            ia = re.search(r'Pages inactive:\s+(\d+)', o)
            if a and w and f and ia:
                used = int(a.group(1)) + int(w.group(1))
                av = int(f.group(1)) + int(ia.group(1))
                if used + av > 0: mem = f"{used*100//(used+av)}%"
        else:
            o = _r(host, user, ["sh", "-c", "cat /proc/stat | head -1"], is_local)
            p = o.split()
            if len(p) >= 8 and p[0] == "cpu":
                total = sum(int(x) for x in p[1:])
                if total > 0: cpu = f"{(total - int(p[4]))*100//total}%"
            o = _r(host, user, ["sh", "-c", "cat /proc/meminfo | head -3"], is_local)
            mt = re.search(r'MemTotal:\s+(\d+)', o)
            ma = re.search(r'MemAvailable:\s+(\d+)', o)
            if mt and ma:
                t, a = int(mt.group(1)), int(ma.group(1))
                if t > 0: mem = f"{(t-a)*100//t}%"
        o = _r(host, user, ["sh", "-c", "df -h / | tail -1"], is_local)
        parts = o.split()
        if len(parts) >= 5: disk = parts[4]
        o = _r(host, user, ["sh", "-c", "uptime -p 2>/dev/null || uptime"], is_local)
        m = re.search(r'up\s+(.*?),', o)
        if m: uptime = m.group(1).strip()[:40]
        return (name, {"online": True, "cpu": cpu, "mem": mem, "disk": disk, "uptime": uptime})
    except (ValueError, IndexError, AttributeError) as e:
        return (name, {"online": False, "cpu": "--", "mem": "--", "disk": "--", "uptime": "--"})
    except Exception as e:
        return (name, {"online": False, "cpu": "--", "mem": "--", "disk": "--", "uptime": "--"})

@app.get("/api/nodes-status")
def api_nodes_status():
    results = {}
    for name, host, user, is_local in NODES:
        try:
            name2, data = _coll(name, host, user, is_local)
            results[name2] = data
        except Exception as e:
            results[name] = {"online": False, "cpu": "--", "mem": "--", "disk": "--", "uptime": "--"}
    return {"ok": True, "nodes": results, "ts": time.time()}

@app.get("/api/ecs-only")
def ecs_only():
    return api_nodes_status()["nodes"].get("ECS 香港", {})

@app.get("/api/status")
async def status():
    return {"ok": True, "service": "k38-node-status", "ts": time.time()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9921)
