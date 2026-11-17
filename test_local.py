#!/usr/bin/env python3
"""Local smoke tests for main.py (stdlib only). Starts the forge on an
ephemeral port, exercises REST / GraphQL / MCP / git smart HTTP with
the real git CLI, checks deterministic export, and runs a small load
smoke test. Usage: python3 test_local.py"""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error

BASE = None
def req(method, path, body=None, headers=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def j(method, path, body=None):
    s, b = req(method, path, body)
    try:
        return s, json.loads(b.decode() or "{}")
    except Exception:
        return s, b

def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail[:200])
    if not cond:
        check.failed += 1
check.failed = 0

def git(*args, cwd=None, env=None):
    e = dict(os.environ); e.pop("GIT_DIR", None)
    if env: e.update(env)
    p = subprocess.run(["git"] + list(args), cwd=cwd, env=e, capture_output=True, text=True, timeout=60)
    return p

def main():
    global BASE
    proc = subprocess.Popen([sys.executable, "main.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    try:
        line = proc.stdout.readline()
        assert line.startswith("PORT="), line
        port = int(line.strip().split("=")[1])
        BASE = f"http://127.0.0.1:{port}"
        print("forge at", BASE)
        time.sleep(0.3)
        s, b = j("GET", "/health"); check("health", s == 200, str(b))
        s, r = j("POST", "/user/repos", {"name": "demo", "description": "demo repo"})
        check("create repo REST", s == 201 and r.get("full_name") == "forge/demo", str(s) + str(r)[:150])
        s, r = j("GET", "/repos/forge/demo"); check("get repo", s == 200 and r.get("name") == "demo")
        # contents: create file via REST (creates a commit)
        import base64
        s, r = j("PUT", "/repos/forge/demo/contents/README.md", {"message": "add readme", "content": base64.b64encode(b"hello forge\n").decode()})
        check("PUT contents", s in (200, 201), str(s) + str(r)[:200])
        s, r = j("GET", "/repos/forge/demo/contents/README.md")
        check("GET contents", s == 200 and base64.b64decode(r["content"]).decode() == "hello forge\n", str(r)[:150])
        s, r = j("GET", "/repos/forge/demo/commits"); check("list commits", s == 200 and len(r) == 1 and len(r[0]["sha"]) == 40, str(r)[:150])
        # issues
        s, r = j("POST", "/repos/forge/demo/issues", {"title": "first bug", "body": "details"})
        check("create issue", s == 201 and r.get("number") == 1, str(r)[:150])
        s, r = j("GET", "/repos/forge/demo/issues"); check("list issues", s == 200 and len(r) == 1)
        s, r = j("PATCH", "/repos/forge/demo/issues/1", {"state": "closed"})
        check("close issue", s == 200 and r.get("state") == "closed")
        s, r = j("POST", "/repos/forge/demo/issues/1/comments", {"body": "hi"})
        check("issue comment", s == 201, str(r)[:100])
        # branch + PR
        s, r = j("POST", "/repos/forge/demo/git/refs", {"ref": "refs/heads/feature", "sha": j("GET", "/repos/forge/demo/branches")[1][0]["commit"]["sha"]})
        check("create branch ref", s == 201, str(s) + str(r)[:150])
        s, r = j("PUT", "/repos/forge/demo/contents/feature.txt?ref=feature", {"message": "feat", "content": base64.b64encode(b"feat\n").decode(), "branch": "feature"}) if False else (None, None)
        # contents PUT with branch in body
        s, r = j("PUT", "/repos/forge/demo/contents/feature.txt", {"message": "feat", "content": base64.b64encode(b"feat\n").decode(), "branch": "feature"})
        check("PUT contents on branch", s == 200, str(s))
        s, r = j("POST", "/repos/forge/demo/pulls", {"title": "add feature", "head": "feature", "base": "main"})
        check("create PR", s == 201 and r.get("number") == 1, str(r)[:150])
        s, r = j("PUT", "/repos/forge/demo/pulls/1/merge", {})
        check("merge PR", s == 200 and r.get("merged") is True, str(r)[:150])
        s, r = j("GET", "/repos/forge/demo/contents/feature.txt")
        check("merged file on main", s == 200, str(r)[:100])
        # search
        s, r = j("GET", "/search/repositories?q=demo"); check("search repos", s == 200 and r["total_count"] >= 1)
        s, r = j("GET", "/search/issues?q=bug"); check("search issues", s == 200 and r["total_count"] >= 1)
        # GraphQL: repository + issues + createIssue mutation
        q = {"query": 'query { repository(owner: "forge", name: "demo") { name nameWithOwner issues(first: 10) { nodes { number title state } totalCount } } }'}
        s, r = j("POST", "/graphql", q)
        check("GraphQL repository query", s == 200 and r.get("data", {}).get("repository", {}).get("name") == "demo" and "errors" not in r, str(r)[:300])
        repo_id = j("GET", "/repos/forge/demo")[1]["node_id"]
        q = {"query": 'mutation($input: CreateIssueInput!) { createIssue(input: $input) { issue { number title } } }', "variables": {"input": {"repositoryId": repo_id, "title": "gql issue", "body": "via gql"}}}
        s, r = j("POST", "/graphql", q)
        check("GraphQL createIssue", s == 200 and r.get("data", {}).get("createIssue", {}).get("issue", {}).get("title") == "gql issue", str(r)[:300])
        # MCP
        s, r = j("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check("MCP tools/list", s == 200 and len(r["result"]["tools"]) >= 10, str(r)[:150])
        s, r = j("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "create_issue", "arguments": {"owner": "forge", "repo": "demo", "title": "mcp issue"}}})
        check("MCP create_issue", s == 200 and r["result"]["isError"] is False, str(r)[:200])
        # deterministic export: two exports identical
        _, e1 = req("GET", "/_forge/export")
        _, e2 = req("GET", "/_forge/export")
        check("deterministic export", e1 == e2 and len(e1) > 100, f"len={len(e1)}")
        # --- real git CLI: clone, push, shallow clone ---
        tmp = tempfile.mkdtemp(prefix="forge-test-")
        p = git("clone", BASE + "/forge/demo.git", os.path.join(tmp, "c1"))
        check("git clone (real git)", p.returncode == 0, p.stderr[:300])
        if p.returncode == 0:
            check("clone has README", os.path.exists(os.path.join(tmp, "c1", "README.md")))
            check("clone has merged file", os.path.exists(os.path.join(tmp, "c1", "feature.txt")))
            with open(os.path.join(tmp, "c1", "new.txt"), "w") as f: f.write("pushed\n")
            git("add", "new.txt", cwd=os.path.join(tmp, "c1"))
            git("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-m", "add new", cwd=os.path.join(tmp, "c1"))
            p = git("push", "origin", "main", cwd=os.path.join(tmp, "c1"))
            check("git push (real git)", p.returncode == 0, p.stderr[:400])
            s, r = j("GET", "/repos/forge/demo/contents/new.txt")
            check("pushed file via REST", s == 200, str(r)[:100])
            p = git("clone", "--depth", "1", BASE + "/forge/demo.git", os.path.join(tmp, "c2"))
            check("git shallow clone --depth 1", p.returncode == 0, p.stderr[:400])
            if p.returncode == 0:
                check("shallow clone has file", os.path.exists(os.path.join(tmp, "c2", "new.txt")))
                check("shallow is shallow", os.path.exists(os.path.join(tmp, "c2", ".git", "shallow")))
        # export still deterministic after git ops
        _, e3 = req("GET", "/_forge/export"); _, e4 = req("GET", "/_forge/export")
        check("deterministic export after git", e3 == e4, f"len={len(e3)}")
        # --- small load smoke: 50 issue reads, 20 issue writes ---
        t0 = time.time()
        for i in range(50):
            s, _ = j("GET", "/repos/forge/demo/issues")
            assert s == 200
        reads_s = time.time() - t0
        t0 = time.time()
        for i in range(20):
            s, _ = j("POST", "/repos/forge/demo/issues", {"title": f"load {i}"})
            assert s == 201
        writes_s = time.time() - t0
        print(f"LOAD 50 issue reads in {reads_s:.2f}s, 20 issue writes in {writes_s:.2f}s")
        check("load smoke", reads_s < 30 and writes_s < 30)
        print("FAILED:", check.failed)
        return 1 if check.failed else 0
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()

if __name__ == "__main__":
    sys.exit(main())
