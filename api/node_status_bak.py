from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import subprocess, json, time

app = FastAPI(title=K38 Node Status)
app.add_middleware(CORSMiddleware, allow_origins=[*], allow_methods=[*], allow_headers=[*])

NODES = [
    (ECS 香港, 127.0.0.1, root),
    (三万八, 100.109.169.92, jagerm3uitra),
    (小四, 100.124.200.40, jagerstudiom4max),
    (大傻, 100.72.1.120, jager-dgx),
    (二傻, 100.98.235.68, jager-dgx-2),
]

# Simple shell agent - works on any Unix
AGENT = '''u=Darwin; if [  = Darwin ]; then   c=;   [ -z  ] && c=--;   m=;   [ -z  ] && m=--; else   c=;   [ -z  ] && c=;   m=; fi; d=; u=23:47  up 1 day, 12:42, 2 users, load averages: 2.45 2.81 2.90; echo \cpu\:\\'''

def _coll(host, user):
    if host == 127.0.0.1:
        r = subprocess.run([bash, -c, AGENT], capture_output=True, text=True, timeout=8)
    else:
        r = subprocess.run([ssh, -o, ConnectTimeout=5, -o, StrictHostKeyChecking=no,
            f{user}@{host}, AGENT], capture_output=True, text=True, timeout=12)
    data = json.loads(r.stdout.strip())
    data[online] = True
    return data

@app.get(/api/nodes-status)
async def api_nodes_status():
    results = {}
    for name, host, user in NODES:
        try: results[name] = _coll(host, user)
        except Exception as e: results[name] = {online: False, cpu: --, mem: --, disk: --, uptime: --, error: str(e)[:60]}
    return {ok: True, nodes: results, ts: time.time()}

@app.get(/api/status)
async def status():
    return {ok: True, service: k38-node-status, ts: time.time()}

if __name__ == __main__:
    import uvicorn
    uvicorn.run(app, host=0.0.0.0, port=9921)
