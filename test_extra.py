#!/usr/bin/env python3
"""Extra checks: cross-run export determinism, delta (thin-pack) push,
fetch with haves, concurrent shallow clones, empty-repo ls-remote.
Usage: python3 test_extra.py"""
import base64, json, os, subprocess, sys, tempfile, threading, urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
def start():
    p = subprocess.Popen([sys.executable, "main.py"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=DIR)
    port = int(p.stdout.readline().strip().split("=")[1])
    return p, f"http://127.0.0.1:{port}"
def j(base, m, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base+path, data=data, method=m, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return resp.status, json.loads(resp.read() or b'{}')
def git(*a, cwd=None):
    return subprocess.run(["git"]+list(a), cwd=cwd, capture_output=True, text=True, timeout=120)
def scenario(base):
    j(base, "POST", "/user/repos", {"name": "demo"})
    j(base, "PUT", "/repos/forge/demo/contents/a.txt", {"message": "a", "content": base64.b64encode(b"line1\nline2\nline3\n").decode()})
    j(base, "POST", "/repos/forge/demo/issues", {"title": "bug", "body": "x"})
    return urllib.request.urlopen(base+"/_forge/export", timeout=30).read()

failed = 0
def check(name, cond, detail=""):
    global failed
    print(("PASS" if cond else "FAIL"), name, detail[:200])
    if not cond: failed += 1

# 1. cross-run determinism
e = []
for _ in range(2):
    p, base = start()
    try: e.append(scenario(base))
    finally: p.terminate(); p.wait(timeout=5)
check("cross-run export identical", e[0] == e[1], f"lens {len(e[0])} {len(e[1])}")

# 2. delta push + fetch with haves + concurrent shallow clones
p, base = start()
try:
    j(base, "POST", "/user/repos", {"name": "demo"})
    j(base, "PUT", "/repos/forge/demo/contents/a.txt", {"message": "a", "content": base64.b64encode((b"line\n"*200)).decode()})
    tmp = tempfile.mkdtemp()
    r = git("clone", base+"/forge/demo.git", tmp+"/c1"); check("clone for delta test", r.returncode == 0, r.stderr[:150])
    # modify a.txt slightly -> push should send a thin (delta) pack
    with open(tmp+"/c1/a.txt", "a") as f: f.write("line\n")
    git("add", "a.txt", cwd=tmp+"/c1")
    git("-c","user.email=a@b.c","-c","user.name=t","commit","-m","append", cwd=tmp+"/c1")
    r = git("push", "origin", "main", cwd=tmp+"/c1"); check("delta/thin-pack push", r.returncode == 0, r.stderr[:200])
    s, d = j(base, "GET", "/repos/forge/demo/contents/a.txt")
    check("delta-pushed content correct", s == 200 and len(base64.b64decode(d["content"]).splitlines()) == 201, str(s))
    # fetch with haves: clone again, then fetch after another push
    r = git("clone", base+"/forge/demo.git", tmp+"/c2"); check("second clone", r.returncode == 0, r.stderr[:150])
    with open(tmp+"/c1/a.txt", "a") as f: f.write("more\n")
    git("add", "a.txt", cwd=tmp+"/c1"); git("-c","user.email=a@b.c","-c","user.name=t","commit","-m","more", cwd=tmp+"/c1")
    git("push", "origin", "main", cwd=tmp+"/c1")
    r = git("fetch", "origin", cwd=tmp+"/c2"); check("fetch with haves (incremental)", r.returncode == 0, r.stderr[:200])
    r = git("log", "--oneline", "origin/main", cwd=tmp+"/c2"); check("fetch got new commit", "more" in r.stdout, r.stdout[:100])
    # concurrent shallow clones x10
    results = []
    def shallow(i):
        d = tmp+f"/s{i}"
        r = git("clone", "--depth", "1", base+"/forge/demo.git", d)
        results.append(r.returncode)
    threads = [threading.Thread(target=shallow, args=(i,)) for i in range(10)]
    [t.start() for t in threads]; [t.join() for t in threads]
    check("10 concurrent shallow clones", results == [0]*10, str(results))
    # empty repo ls-remote
    j(base, "POST", "/user/repos", {"name": "empty"})
    r = git("ls-remote", base+"/forge/empty.git"); check("ls-remote empty repo", r.returncode == 0, r.stderr[:150] + r.stdout[:100])
finally:
    p.terminate(); p.wait(timeout=5)
print("FAILED:", failed)
sys.exit(1 if failed else 0)
