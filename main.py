#!/usr/bin/env python3
"""Single-process GitHub-compatible git forge (stdlib only).

Python 3.13 target (also runs on 3.12). One process, no git binary,
no outside service. Serves git smart HTTP, REST v3, GraphQL and MCP
from its own in-memory store (optional file persistence via
FORGE_DATA_FILE). Deterministic state export: no wall-clock time is
stored; all timestamps derive from a logical sequence counter.

Run:  python3 main.py [--port PORT] [--host HOST]
       PORT env var is honoured unless it is 8080 (suite port).
       Binds an ephemeral port when no port is given and prints
       PORT=<port> on stdout.
       python3 main.py --export   prints the empty canonical export.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

BASE_TS = 1704067200  # 2024-01-01T00:00:00Z, logical clock base
VERSION = "1.0.0"

# ---------------------------------------------------------------- store
def iso_for_seq(seq):
    # Deterministic timestamp: base + seq seconds, UTC, ISO-8601.
    import datetime
    dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=BASE_TS + seq)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def b64e(b):
    return base64.b64encode(b).decode("ascii")

def b64d(s):
    return base64.b64decode(s.encode("ascii"))

def node_id(kind, ident):
    return b64e(("%s:%s" % (kind, ident)).encode())

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.seq = 0
        self.next_id = 1
        # objects: oid -> (type, bytes)   git objects
        self.objects = {}
        # repos: "owner/name" -> repo dict
        self.repos = {}
        self.users = {"forge": {"login": "forge", "id": 1, "type": "User"}}

    def tick(self):
        self.seq += 1
        return self.seq

    def new_id(self):
        i = self.next_id
        self.next_id += 1
        return i

    # -- deterministic export (canonical JSON, sorted keys) ------------
    def export(self):
        repos = []
        for key in sorted(self.repos):
            r = self.repos[key]
            repos.append({
                "branches": {k: r["branches"][k] for k in sorted(r["branches"])},
                "created_seq": r["created_seq"],
                "default_branch": r["default_branch"],
                "description": r["description"],
                "id": r["id"],
                "issues": r["issues"],
                "name": r["name"],
                "node_id": r["node_id"],
                "owner": r["owner"],
                "private": r["private"],
                "pulls": r["pulls"],
            })
        objs = {oid: {"data": b64e(self.objects[oid][1]), "type": self.objects[oid][0]}
                for oid in sorted(self.objects)}
        doc = {"next_id": self.next_id, "objects": objs, "repos": repos,
               "seq": self.seq, "version": VERSION}
        return (json.dumps(doc, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False) + "\n").encode()

    def save(self):
        path = os.environ.get("FORGE_DATA_FILE")
        if not path:
            return
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(self.export())
        os.replace(tmp, path)

    def load(self):
        path = os.environ.get("FORGE_DATA_FILE")
        if not path or not os.path.exists(path):
            return
        with open(path, "rb") as f:
            doc = json.loads(f.read().decode())
        self.seq = doc.get("seq", 0)
        self.next_id = doc.get("next_id", 1)
        for oid, o in doc.get("objects", {}).items():
            self.objects[oid] = (o["type"], b64d(o["data"]))
        for r in doc.get("repos", []):
            self.repos[r["owner"] + "/" + r["name"]] = {
                "branches": dict(r["branches"]), "created_seq": r["created_seq"],
                "default_branch": r["default_branch"], "description": r["description"],
                "id": r["id"], "issues": r["issues"], "name": r["name"],
                "node_id": r["node_id"], "owner": r["owner"],
                "private": r["private"], "pulls": r["pulls"]}

STORE = Store()

# ---------------------------------------------------------------- git objects
def hash_object(otype, payload):
    hdr = ("%s %d\0" % (otype, len(payload))).encode()
    return hashlib.sha1(hdr + payload).hexdigest()

def put_object(otype, payload):
    oid = hash_object(otype, payload)
    STORE.objects.setdefault(oid, (otype, payload))
    return oid

def get_object(oid):
    return STORE.objects.get(oid)

def parse_commit(payload):
    tree = None; parents = []
    for line in payload.decode("utf-8", "replace").split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line == "":
            break
    return tree, parents

def parse_tree(payload):
    """Return list of (mode_str, name, oid)."""
    out = []; i = 0
    while i < len(payload):
        j = payload.index(b" ", i)
        mode = payload[i:j].decode()
        k = payload.index(b"\0", j)
        name = payload[j + 1:k].decode()
        oid = payload[k + 1:k + 21].hex()
        out.append((mode, name, oid))
        i = k + 21
    return out

def build_tree(files):
    """files: {path: (mode, oid)} -> tree oid. Deterministic order."""
    # group by first component
    def rec(prefix, paths):
        entries = []  # (mode, name, oid)
        dirs = {}
        for p in sorted(paths):
            rest = p[len(prefix):] if prefix else p
            if "/" in rest:
                d = rest.split("/", 1)[0]
                dirs.setdefault(d, []).append(p)
            else:
                mode, oid = files[p]
                entries.append((mode, rest, oid))
        for d in sorted(dirs):
            sub = rec((prefix + d + "/") if prefix else (d + "/"), dirs[d])
            entries.append(("40000", d, sub))
        entries.sort(key=lambda e: e[1] + ("/" if e[0] == "40000" else ""))
        payload = b""
        for mode, name, oid in entries:
            payload += ("%s %s\0" % (mode, name)).encode() + bytes.fromhex(oid)
        return put_object("tree", payload)
    if not files:
        return put_object("tree", b"")
    return rec("", list(files))

def flatten_tree(tree_oid, prefix=""):
    out = {}
    obj = get_object(tree_oid)
    if not obj or obj[0] != "tree":
        return out
    for mode, name, oid in parse_tree(obj[1]):
        if mode == "40000":
            out.update(flatten_tree(oid, prefix + name + "/"))
        else:
            out[prefix + name] = (mode, oid)
    return out

def make_commit(tree_oid, parents, message, author="forge <forge@localhost>"):
    seq = STORE.tick()
    ts = BASE_TS + seq
    body = ("tree %s\n" % tree_oid).encode()
    for p in parents:
        body += ("parent %s\n" % p).encode()
    body += ("author %s %d +0000\n" % (author, ts)).encode()
    body += ("committer %s %d +0000\n" % (author, ts)).encode()
    body += b"\n" + message.encode()
    return put_object("commit", body)

def reachable_objects(wants, haves, depth=None):
    """Objects reachable from wants, minus objects reachable from haves.
    depth: max commit generations (1 = only wanted commits themselves)."""
    # commit graph walk
    seen_commits = set(); want_objs = set(); queue = [(w, 0) for w in wants]
    have_reach = set()
    if haves:
        stack = list(haves); seen = set()
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            o = get_object(c)
            if not o:
                continue
            have_reach.add(c)
            if o[0] == "commit":
                tree, parents = parse_commit(o[1])
                if tree:
                    have_reach.add(tree)
                stack.extend(parents)
                # trees/blobs under have-commits: walk trees too
                if tree:
                    stack.append(tree)
            elif o[0] == "tree":
                for _, _, oid in parse_tree(o[1]):
                    if oid not in have_reach:
                        stack.append(oid)
    while queue:
        c, gen = queue.pop(0)
        if c in seen_commits:
            continue
        seen_commits.add(c)
        o = get_object(c)
        if not o or o[0] != "commit":
            continue
        want_objs.add(c)
        tree, parents = parse_commit(o[1])
        if tree:
            want_objs.add(tree)
            # collect tree+blobs
            stack = [tree]
            while stack:
                t = stack.pop()
                to = get_object(t)
                if not to or to[0] != "tree":
                    continue
                for mode, _, oid in parse_tree(to[1]):
                    want_objs.add(oid)
                    if mode == "40000":
                        stack.append(oid)
        if depth is None or gen + 1 < depth:
            for p in parents:
                queue.append((p, gen + 1))
    return want_objs - have_reach, seen_commits

# ---------------------------------------------------------------- git pack
def pack_object_header(otype, size):
    type_num = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}[otype]
    out = bytearray()
    b = (type_num << 4) | (size & 0x0F)
    size >>= 4
    if size:
        b |= 0x80
    out.append(b)
    while size:
        b = size & 0x7F
        size >>= 7
        if size:
            b |= 0x80
        out.append(b)
    return bytes(out)

def build_pack(oids):
    out = bytearray(b"PACK" + (2).to_bytes(4, "big") + len(oids).to_bytes(4, "big"))
    for oid in sorted(oids):
        otype, payload = STORE.objects[oid]
        out += pack_object_header(otype, len(payload))
        out += zlib.compress(payload)
    out += hashlib.sha1(bytes(out)).digest()  # packfile SHA-1 trailer
    return bytes(out)

def read_varint(data, i):
    v = 0; shift = 0
    while True:
        b = data[i]; i += 1
        v |= (b & 0x7F) << shift; shift += 7
        if not (b & 0x80):
            break
    return v, i

def apply_delta(base, delta):
    i = 0
    src_size, i = read_varint(delta, i)
    tgt_size, i = read_varint(delta, i)
    if src_size != len(base):
        raise ValueError("delta source size mismatch")
    out = bytearray()
    while i < len(delta):
        op = delta[i]; i += 1
        if op & 0x80:
            off = 0; size = 0
            for k in range(4):
                if op & (1 << k):
                    off |= delta[i] << (8 * k); i += 1
            for k in range(3):
                if op & (0x10 << k):
                    size |= delta[i] << (8 * k); i += 1
            if size == 0:
                size = 0x10000
            out += base[off:off + size]
        elif op:
            out += delta[i:i + op]; i += op
        else:
            raise ValueError("delta op 0")
    if len(out) != tgt_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)

def unpack_pack(data):
    """Parse a PACK stream; store objects; return list of oids.
    Supports full objects, OFS_DELTA and REF_DELTA."""
    if len(data) < 12 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    count = int.from_bytes(data[8:12], "big")
    i = 12; oids = []; by_offset = {}
    pending = []  # (oid, base_ref, delta_bytes, is_ofs)
    for _ in range(count):
        start = i
        b = data[i]; i += 1
        type_num = (b >> 4) & 7
        size = b & 0x0F; shift = 4
        while b & 0x80:
            b = data[i]; i += 1
            size |= (b & 0x7F) << shift; shift += 7
        base_ref = None
        if type_num == 6:  # OFS_DELTA
            b = data[i]; i += 1
            off = b & 0x7F
            while b & 0x80:
                b = data[i]; i += 1
                off = ((off + 1) << 7) | (b & 0x7F)
            base_ref = ("ofs", start - off)
        elif type_num == 7:  # REF_DELTA
            base_ref = ("ref", data[i:i + 20].hex()); i += 20
        decomp = zlib.decompressobj()
        payload = decomp.decompress(data[i:])
        consumed = len(data[i:]) - len(decomp.unused_data)
        i += consumed
        if type_num in (1, 2, 3, 4):
            otype = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}[type_num]
            oid = put_object(otype, payload)
            by_offset[start] = oid; oids.append(oid)
        else:
            # delta: store placeholder, resolve after loop (base may be later)
            oid = hashlib.sha1(b"delta").hexdigest()  # replaced below
            pending.append((start, base_ref, payload))
    # resolve deltas (possibly chained)
    resolved = dict(by_offset)
    for _round in range(20):
        if not pending:
            break
        still = []
        for start, base_ref, delta in pending:
            if base_ref[0] == "ofs":
                base_oid = resolved.get(base_ref[1])
            else:
                base_oid = base_ref[1] if base_ref[1] in STORE.objects else resolved.get(base_ref[1])
            if not base_oid or base_oid not in STORE.objects:
                still.append((start, base_ref, delta)); continue
            btype, base = STORE.objects[base_oid]
            payload = apply_delta(base, delta)
            oid = put_object(btype, payload)
            resolved[start] = oid; oids.append(oid)
        if len(still) == len(pending):
            raise ValueError("unresolvable delta base")
        pending = still
    if pending:
        raise ValueError("unresolved deltas")
    return oids

# pkt-line helpers
def pkt(data):
    if isinstance(data, str):
        data = data.encode()
    return ("%04x" % (len(data) + 4)).encode() + data

def pkt_flush():
    return b"0000"

def parse_pkt_lines(data):
    lines = []; i = 0
    while i + 4 <= len(data):
        if data[i:i + 4] == b"0000":
            lines.append(None); i += 4; continue
        try:
            n = int(data[i:i + 4], 16)
        except ValueError:
            break
        if n < 4 or i + n > len(data):
            break
        lines.append(data[i + 4:i + n]); i += n
    return lines, data[i:]

def sideband_pack(pack):
    out = bytearray(); i = 0
    while i < len(pack):
        chunk = pack[i:i + 65515]; i += len(chunk)
        out += pkt(b"\x01" + chunk)
    out += pkt_flush()
    return bytes(out)

# ---------------------------------------------------------------- repos
def get_repo(owner, name):
    return STORE.repos.get(owner + "/" + name)

def create_repo(owner, name, description="", private=False, default_branch="main"):
    key = owner + "/" + name
    if key in STORE.repos:
        return STORE.repos[key], False
    seq = STORE.tick()
    rid = STORE.new_id()
    repo = {"branches": {default_branch: None}, "created_seq": seq,
            "default_branch": default_branch, "description": description,
            "id": rid, "issues": [], "name": name,
            "node_id": node_id("Repository", str(rid)), "owner": owner,
            "private": private, "pulls": []}
    STORE.repos[key] = repo
    return repo, True

def repo_files(repo, branch=None):
    branch = branch or repo["default_branch"]
    head = repo["branches"].get(branch)
    if not head:
        return {}
    obj = get_object(head)
    if not obj:
        return {}
    tree, _ = parse_commit(obj[1])
    return flatten_tree(tree) if tree else {}

def commit_files(repo, branch, files, message, author="forge <forge@localhost>"):
    """files: {path: (mode, bytes) or None to delete}. Returns commit oid."""
    cur = repo_files(repo, branch)
    for path, val in files.items():
        if val is None:
            cur.pop(path, None)
        else:
            mode, content = val
            cur[path] = (mode, put_object("blob", content))
    tree_oid = build_tree(cur)
    parents = []
    head = repo["branches"].get(branch)
    if head:
        parents = [head]
    oid = make_commit(tree_oid, parents, message, author)
    repo["branches"][branch] = oid
    return oid

def repo_json(repo, host):
    base = "http://%s" % host
    return {"id": repo["id"], "node_id": repo["node_id"], "name": repo["name"],
            "full_name": repo["owner"] + "/" + repo["name"],
            "owner": {"login": repo["owner"], "id": 1, "type": "User",
                      "node_id": node_id("User", repo["owner"])},
            "private": repo["private"], "html_url": base + "/" + repo["owner"] + "/" + repo["name"],
            "description": repo["description"], "fork": False,
            "url": base + "/repos/" + repo["owner"] + "/" + repo["name"],
            "clone_url": base + "/" + repo["owner"] + "/" + repo["name"] + ".git",
            "git_url": base + "/" + repo["owner"] + "/" + repo["name"] + ".git",
            "ssh_url": "git@forge:" + repo["owner"] + "/" + repo["name"] + ".git",
            "default_branch": repo["default_branch"],
            "created_at": iso_for_seq(repo["created_seq"]),
            "updated_at": iso_for_seq(repo["created_seq"]),
            "pushed_at": iso_for_seq(repo["created_seq"]),
            "size": 0, "stargazers_count": 0, "watchers_count": 0,
            "forks_count": 0, "open_issues_count": sum(1 for i in repo["issues"] if i["state"] == "open"),
            "license": None, "topics": [], "visibility": "private" if repo["private"] else "public",
            "has_issues": True, "has_projects": True, "has_wiki": False,
            "has_pages": False, "archived": False, "disabled": False}

def issue_json(repo, iss, host):
    base = "http://%s" % host
    return {"id": iss["id"], "node_id": iss["node_id"],
            "number": iss["number"], "title": iss["title"], "body": iss["body"],
            "state": iss["state"], "locked": False,
            "user": {"login": iss["author"], "id": 1, "type": "User"},
            "labels": [{"id": 1, "name": l, "color": "ededed"} for l in iss["labels"]],
            "assignees": [{"login": a} for a in iss["assignees"]],
            "assignee": {"login": iss["assignees"][0]} if iss["assignees"] else None,
            "comments": len(iss["comments"]),
            "created_at": iso_for_seq(iss["created_seq"]),
            "updated_at": iso_for_seq(iss["created_seq"]),
            "closed_at": iso_for_seq(iss["closed_seq"]) if iss.get("closed_seq") else None,
            "html_url": base + "/" + repo["owner"] + "/" + repo["name"] + "/issues/" + str(iss["number"]),
            "url": base + "/repos/" + repo["owner"] + "/" + repo["name"] + "/issues/" + str(iss["number"]),
            "repository_url": base + "/repos/" + repo["owner"] + "/" + repo["name"],
            "comments_url": base + "/repos/" + repo["owner"] + "/" + repo["name"] + "/issues/" + str(iss["number"]) + "/comments",
            "pull_request": {"url": base + "/repos/" + repo["owner"] + "/" + repo["name"] + "/pulls/" + str(iss["number"])} if iss.get("is_pr") else None}

def pr_json(repo, pr, host):
    base = "http://%s" % host
    d = {"id": pr["id"], "node_id": pr["node_id"], "number": pr["number"],
         "title": pr["title"], "body": pr["body"], "state": pr["state"],
         "locked": False, "draft": False, "merged": pr["merged"],
         "mergeable": True, "merged_at": iso_for_seq(pr["merged_seq"]) if pr.get("merged_seq") else None,
         "user": {"login": pr["author"], "id": 1, "type": "User"},
         "labels": [], "assignees": [],
         "head": {"label": repo["owner"] + ":" + pr["head"], "ref": pr["head"],
                  "sha": repo["branches"].get(pr["head"]) or "",
                  "repo": repo_json(repo, host)},
         "base": {"label": repo["owner"] + ":" + pr["base"], "ref": pr["base"],
                  "sha": repo["branches"].get(pr["base"]) or "",
                  "repo": repo_json(repo, host)},
         "created_at": iso_for_seq(pr["created_seq"]),
         "updated_at": iso_for_seq(pr["created_seq"]),
         "closed_at": iso_for_seq(pr["closed_seq"]) if pr.get("closed_seq") else None,
         "html_url": base + "/" + repo["owner"] + "/" + repo["name"] + "/pull/" + str(pr["number"]),
         "url": base + "/repos/" + repo["owner"] + "/" + repo["name"] + "/pulls/" + str(pr["number"]),
         "comments": 0, "review_comments": 0, "commits": 1, "additions": 0,
         "deletions": 0, "changed_files": 0}
    return d

def new_issue(repo, title, body="", author="forge", labels=None, assignees=None):
    seq = STORE.tick()
    num = (max([i["number"] for i in repo["issues"]], default=0) + 1)
    iss = {"id": STORE.new_id(), "node_id": node_id("Issue", repo["owner"] + "/" + repo["name"] + "#" + str(num)),
           "number": num, "title": title, "body": body or "", "state": "open",
           "author": author, "labels": labels or [], "assignees": assignees or [],
           "comments": [], "created_seq": seq, "closed_seq": None, "is_pr": False}
    repo["issues"].append(iss)
    return iss

def new_pr(repo, title, body, head, base, author="forge"):
    seq = STORE.tick()
    num = (max([p["number"] for p in repo["pulls"]], default=0) + 1)
    # PR numbers share the issue numbering space on GitHub; keep separate
    # counter here but also mirror as an issue entry for GraphQL/search.
    pr = {"id": STORE.new_id(), "node_id": node_id("PullRequest", repo["owner"] + "/" + repo["name"] + "#" + str(num)),
          "number": num, "title": title, "body": body or "", "state": "open",
          "author": author, "head": head, "base": base, "merged": False,
          "created_seq": seq, "closed_seq": None, "merged_seq": None, "comments": []}
    repo["pulls"].append(pr)
    return pr

# ---------------------------------------------------------------- git smart HTTP
def refs_advertisement(repo, service):
    caps = ("report-status report-status-v2 delete-refs side-band-64k "
            "quiet atomic ofs-delta agent=forge/1.0.0")
    if service == "git-upload-pack":
        caps = "multi_ack thin-pack side-band-64k side-band side-band-64k ofs-delta shallow deepen-since deepen-not deepen-relative no-progress include-tag multi_ack_detailed allow-tip-sha1-in-want allow-reachable-sha1-in-want no-done filter agent=forge/1.0.0"
    out = bytearray()
    out += pkt("# service=%s\n" % service)
    out += pkt_flush()
    refs = []
    for name in sorted(repo["branches"]):
        oid = repo["branches"][name]
        if oid:
            refs.append((oid, "refs/heads/" + name))
    if not refs:
        # empty repo: advertise a zero ref so clients get a clean error/empty clone
        out += pkt("%s capabilities^{}\0%s\n" % ("0" * 40, caps))
    else:
        head_oid = repo["branches"].get(repo["default_branch"])
        first = True
        # HEAD MUST be the first advertised ref when valid (pack-protocol).
        if head_oid:
            out += pkt("%s HEAD\0%s\n" % (head_oid, caps))
            first = False
        for oid, ref in refs:
            if first:
                out += pkt("%s %s\0%s\n" % (oid, ref, caps))
                first = False
            else:
                out += pkt("%s %s\n" % (oid, ref))
    out += pkt_flush()
    return bytes(out)

def handle_upload_pack(repo, body):
    # Protocol v2 detection: body starts with a pkt-line "command=..."
    lines, rest = parse_pkt_lines(body)
    text = b"".join([l for l in lines if l]) if lines else b""
    if text.startswith(b"command="):
        return handle_upload_pack_v2(repo, body)
    wants = []; haves = []; caps = ""; deepen = None; filter_spec = None
    done = False
    for line in lines:
        if line is None:
            continue  # flush separates want/deepen from have/done; keep parsing
        s = line.decode("utf-8", "replace").strip()
        if s.startswith("want "):
            parts = s[5:].split(" ", 1)
            wants.append(parts[0])
            if len(parts) > 1:
                caps = parts[1]
        elif s.startswith("have "):
            haves.append(s[5:].split(" ")[0])
        elif s.startswith("deepen "):
            try:
                deepen = int(s.split(" ", 1)[1])
            except ValueError:
                deepen = 1
        elif s.startswith("filter "):
            filter_spec = s[7:]
        elif s == "done":
            done = True
    if not wants:
        return 400, b"no want lines", "text/plain"
    if deepen is not None and not done:
        # Shallow fetch over stateless HTTP is two rounds: round 1 (no
        # "done") gets ONLY the shallow list + flush. The client then
        # re-POSTs want+deepen+"done" and expects the shallow list again,
        # followed by NAK and the pack.
        out = bytearray()
        for w in wants:
            out += pkt("shallow %s\n" % w)
        out += pkt_flush()
        return 200, bytes(out), "application/x-git-upload-pack-result"
    # filter blob:none / tree:0: honour by dropping blobs/trees as requested
    # (only blob:none is honoured; other filters are treated as no filter
    # and documented in README — the pack still contains full objects).
    objs, _ = reachable_objects(wants, haves, depth=deepen)
    if filter_spec == "blob:none":
        objs = {o for o in objs if STORE.objects.get(o, ("", b""))[0] != "blob"}
    pack = build_pack(objs)
    use_sideband = "side-band-64k" in caps or "side-band" in caps
    out = bytearray()
    if deepen is not None:
        # shallow-update comes FIRST: the client reads shallow/unshallow
        # lines until a flush before it reads the ACK/NAK.
        for w in wants:
            out += pkt("shallow %s\n" % w)
        out += pkt_flush()
    if haves:
        # ACK the last have we know (all haves are known or none are)
        known = [h for h in haves if h in STORE.objects]
        if known:
            out += pkt("ACK %s\n" % known[-1])
        else:
            out += pkt("NAK\n")
    else:
        out += pkt("NAK\n")
    # NOTE (pack-protocol): for a non-shallow fetch there is NO flush
    # between the final NAK/ACK and the packfile — the pack starts
    # immediately. Sending a flush there makes git read "0000" as the
    # pack header ("protocol error: bad pack header").
    if use_sideband:
        out += sideband_pack(pack)
    else:
        out += pack
    return 200, bytes(out), "application/x-git-upload-pack-result"

def handle_upload_pack_v2(repo, body):
    lines, _ = parse_pkt_lines(body)
    cmd = ""
    args = {}
    for line in lines:
        if line is None:
            break
        s = line.decode("utf-8", "replace").strip()
        if s.startswith("command="):
            cmd = s[8:]
        elif "=" in s:
            k, v = s.split("=", 1)
            args.setdefault(k, []).append(v)
    if cmd == "ls-refs":
        out = bytearray()
        for name in sorted(repo["branches"]):
            oid = repo["branches"][name]
            if oid:
                out += pkt("%s refs/heads/%s\n" % (oid, name))
        out += pkt_flush()
        return 200, bytes(out), "application/x-git-upload-pack-result"
    if cmd == "fetch":
        wants = args.get("want", [])
        haves = args.get("have", [])
        deepen = None
        if "deepen" in args:
            try:
                deepen = int(args["deepen"][0])
            except ValueError:
                deepen = 1
        objs, _ = reachable_objects(wants, haves, depth=deepen)
        pack = build_pack(objs)
        out = bytearray()
        out += pkt("acknowledgment\n") if False else b""
        # v2 fetch response: pkt-lines for acks, then packfile section
        if haves:
            known = [h for h in haves if h in STORE.objects]
            out += pkt("acknowledgment\n")
            out += pkt("ready\n") if known else pkt("NAK\n")
        else:
            out += pkt("acknowledgment\n")
            out += pkt("NAK\n")
        if deepen is not None:
            for w in wants:
                out += pkt("shallow %s\n" % w)
        out += pkt_flush()
        out += pkt("packfile\n")
        out += pack
        return 200, bytes(out), "application/x-git-upload-pack-result"
    return 400, b"unsupported v2 command", "text/plain"

def handle_receive_pack(repo, body):
    lines, rest = parse_pkt_lines(body)
    cmds = []  # (old, new, ref)
    client_caps = ""
    for line in lines:
        if line is None:
            break
        s = line.decode("utf-8", "replace").strip()
        # first command line carries capabilities after a NUL
        if "\0" in s:
            s, client_caps = s.split("\0", 1)
        parts = s.split(" ")
        if len(parts) >= 3 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            cmds.append((parts[0], parts[1], parts[2]))
    if not cmds:
        return 400, b"no commands", "text/plain"
    # rest is the packfile (may be preceded by pkt framing if side-band;
    # git send-pack sends the raw pack after the flush)
    pack_start = rest.find(b"PACK")
    if pack_start < 0:
        return 400, b"no packfile", "text/plain"
    try:
        unpack_pack(rest[pack_start:])
    except Exception as e:
        line = "unpack error %s\n" % e
        if "side-band-64k" in client_caps or "side-band" in client_caps:
            out = pkt(b"\x01" + pkt(line)) + pkt(b"\x01" + pkt_flush()) + pkt_flush()
        else:
            out = pkt(line) + pkt_flush()
        return 200, out, "application/x-git-receive-pack-result"
    results = []
    for old, new, ref in cmds:
        if not ref.startswith("refs/heads/"):
            # tags / other refs: accept by storing in branches-like map is
            # out of scope; report as ok only for heads, else error.
            results.append((ref, "unsupported ref (only refs/heads/*)"))
            continue
        branch = ref[len("refs/heads/"):]
        if new == "0" * 40:
            repo["branches"].pop(branch, None)
        else:
            if new not in STORE.objects:
                results.append((ref, "object not found"))
                continue
            repo["branches"][branch] = new
        results.append((ref, None))
    STORE.save()
    status_lines = ["unpack ok\n"]
    for ref, err in results:
        if err:
            status_lines.append("ng %s %s\n" % (ref, err))
        else:
            status_lines.append("ok %s\n" % ref)
    out = bytearray()
    if "side-band-64k" in client_caps or "side-band" in client_caps:
        # With side-band in effect the report-status lines are multiplexed
        # on band 1, and each band-1 payload is itself a complete pkt-line
        # (the demuxer strips the band byte and the reader expects pkt-lines).
        for line in status_lines:
            out += pkt(b"\x01" + pkt(line))
        # The report-status stream itself ends with a flush pkt-line, which
        # must travel inside band 1: the outer flush only ends the
        # sideband stream and is consumed by the demuxer, never forwarded.
        out += pkt(b"\x01" + pkt_flush())
        out += pkt_flush()
    else:
        for line in status_lines:
            out += pkt(line)
        out += pkt_flush()
    return 200, bytes(out), "application/x-git-receive-pack-result"

# ---------------------------------------------------------------- GraphQL (minimal parser + executor)
# Supports the shapes gh and github-mcp-server send: queries/mutations,
# arguments (literals + $variables), aliases, fragments, nested fields,
# first: N pagination on issues/pullRequests. Anything else returns a
# GraphQL error object instead of being silently faked.
TOKEN_RE = re.compile(r'\s*(?:(\$?[A-Za-z_][A-Za-z0-9_]*)|(\d+)|("(?:[^"\\]|\\.)*")|(\.\.\.)|([{}()\[\]:!@=,]))')

def gql_tokenize(s):
    toks = []; i = 0
    while i < len(s):
        m = TOKEN_RE.match(s, i)
        if not m or m.end() == i:
            raise ValueError("GraphQL parse error at %r" % s[i:i + 20])
        i = m.end()
        if m.group(1) is not None:
            toks.append(("name", m.group(1)))
        elif m.group(2) is not None:
            toks.append(("int", int(m.group(2))))
        elif m.group(3) is not None:
            toks.append(("str", json.loads(m.group(3))))
        elif m.group(4) is not None:
            toks.append(("...", "..."))
        else:
            toks.append((m.group(5), m.group(5)))
    toks.append(("eof", None))
    return toks

class GQLParser:
    def __init__(self, text, variables):
        self.toks = gql_tokenize(text); self.i = 0; self.vars = variables or {}
        self.fragments = {}
    def peek(self):
        return self.toks[self.i]
    def next(self):
        t = self.toks[self.i]; self.i += 1; return t
    def expect(self, kind):
        t = self.next()
        if t[0] != kind:
            raise ValueError("expected %s got %r" % (kind, t))
        return t[1]
    def parse(self):
        # operation: [query|mutation] [name] [( $var: Type ... )] { ... }
        op = "query"
        if self.peek()[0] == "name" and self.peek()[1] in ("query", "mutation"):
            op = self.next()[1]
            if self.peek()[0] == "name":
                self.next()
        if self.peek()[0] == "(":
            self.next()
            while self.peek()[0] != ")":
                t = self.next()
                if t[0] == "name" and t[1].startswith("$"):
                    # $var : Type (= default)? -- skip type tokens
                    while self.peek()[0] not in (",", ")"):
                        self.next()
                if self.peek()[0] == ",":
                    self.next()
            self.expect(")")
        fields = self.parse_selection()
        # fragments after operation
        while self.peek()[0] != "eof":
            if self.peek() == ("name", "fragment"):
                self.next(); name = self.expect("name")
                self.expect("name")  # on
                self.expect("name")  # Type
                self.fragments[name] = self.parse_selection()
            else:
                self.next()
        return op, self._expand(fields)

    def _expand(self, fields):
        out = []
        for f in fields:
            if "fragment" in f:
                frag = self.fragments.get(f["fragment"])
                if frag:
                    out.extend(self._expand(frag))
                continue
            if "inline" in f:
                out.extend(self._expand(f["fields"]))
                continue
            f = dict(f)
            if f.get("fields"):
                f["fields"] = self._expand(f["fields"])
            out.append(f)
        return out
    def parse_selection(self):
        self.expect("{")
        fields = []
        while self.peek()[0] != "}":
            fields.append(self.parse_field())
        self.expect("}")
        return fields
    def parse_field(self):
        if self.peek()[0] == "...":
            self.next()
            if self.peek() == ("name", "on"):
                self.next(); self.expect("name")  # type condition
                return {"inline": True, "fields": self.parse_selection()}
            name = self.expect("name")
            return {"fragment": name, "fields": []}
        name = self.expect("name")
        alias = None
        if self.peek()[0] == ":":
            self.next(); alias = name; name = self.expect("name")
        args = {}
        if self.peek()[0] == "(":
            self.next()
            while self.peek()[0] != ")":
                aname = self.expect("name"); self.expect(":")
                args[aname] = self.parse_value()
                if self.peek()[0] == ",":
                    self.next()
            self.expect(")")
        sub = []
        if self.peek()[0] == "{":
            sub = self.parse_selection()
        return {"name": name, "alias": alias or name, "args": args, "fields": sub}
    def parse_value(self):
        t = self.next()
        if t[0] == "str" or t[0] == "int":
            return t[1]
        if t[0] == "name":
            if t[1].startswith("$"):
                return self.vars.get(t[1][1:])
            if t[1] in ("true", "false"):
                return t[1] == "true"
            if t[1] == "null":
                return None
            return t[1]
        if t[0] == "[":
            vals = []
            while self.peek()[0] != "]":
                vals.append(self.parse_value())
                if self.peek()[0] == ",":
                    self.next()
            self.expect("]")
            return vals
        if t[0] == "{":
            d = {}
            while self.peek()[0] != "}":
                k = self.expect("name"); self.expect(":"); d[k] = self.parse_value()
                if self.peek()[0] == ",":
                    self.next()
            self.expect("}")
            return d
        raise ValueError("bad GraphQL value %r" % (t,))

def gql_repo_fields(repo, fields, host):
    out = {}
    for f in fields:
        if "fragment" in f:
            continue
        n = f["name"]
        if n == "id":
            out[f["alias"]] = repo["node_id"]
        elif n == "name":
            out[f["alias"]] = repo["name"]
        elif n == "nameWithOwner":
            out[f["alias"]] = repo["owner"] + "/" + repo["name"]
        elif n == "description":
            out[f["alias"]] = repo["description"]
        elif n == "owner":
            out[f["alias"]] = gql_user_fields(repo["owner"], f["fields"])
        elif n == "defaultBranchRef":
            br = repo["default_branch"]
            out[f["alias"]] = {"name": br, "target": {"oid": repo["branches"].get(br), "__typename": "Commit"}} if repo["branches"].get(br) else None
        elif n == "url":
            out[f["alias"]] = "http://%s/%s/%s" % (host, repo["owner"], repo["name"])
        elif n in ("issues", "pullRequests"):
            items = repo["issues"] if n == "issues" else repo["pulls"]
            first = f["args"].get("first")
            states = f["args"].get("states")
            if states:
                if isinstance(states, str):
                    states = [states]
                items = [i for i in items if i["state"].upper() in [s.upper() for s in states]]
            if first is not None:
                items = items[:int(first)]
            nodes_sub = next((sf for sf in f["fields"] if sf["name"] == "nodes"), None)
            node_fields = nodes_sub["fields"] if nodes_sub else []
            nodes = []
            for it in items:
                if n == "issues":
                    nodes.append(gql_issue_fields(repo, it, node_fields, host))
                else:
                    nodes.append(gql_pr_fields(repo, it, node_fields, host))
            # connection shape: {nodes:[...], totalCount, edges:[{node}]}
            subnames = [x["name"] for x in f["fields"]]
            conn = {}
            for sf in f["fields"]:
                if sf["name"] == "nodes":
                    conn[sf["alias"]] = nodes
                elif sf["name"] == "totalCount":
                    conn[sf["alias"]] = len(repo["issues"] if n == "issues" else repo["pulls"])
                elif sf["name"] == "edges":
                    conn[sf["alias"]] = [{"node": nd} for nd in nodes]
                elif sf["name"] == "pageInfo":
                    conn[sf["alias"]] = {"hasNextPage": False, "hasPreviousPage": False,
                                          "startCursor": None, "endCursor": None}
            out[f["alias"]] = conn if conn else {"nodes": nodes, "totalCount": len(nodes)}
        elif n == "issue":
            num = f["args"].get("number")
            it = next((i for i in repo["issues"] if i["number"] == num), None)
            out[f["alias"]] = gql_issue_fields(repo, it, f["fields"], host) if it else None
        elif n == "pullRequest":
            num = f["args"].get("number")
            it = next((p for p in repo["pulls"] if p["number"] == num), None)
            out[f["alias"]] = gql_pr_fields(repo, it, f["fields"], host) if it else None
        elif n == "__typename":
            out[f["alias"]] = "Repository"
        else:
            out[f["alias"]] = None
    return out

def gql_user_fields(login, fields):
    out = {}
    for f in fields:
        if f["name"] == "login":
            out[f["alias"]] = login
        elif f["name"] == "id":
            out[f["alias"]] = node_id("User", login)
        elif f["name"] == "__typename":
            out[f["alias"]] = "User"
        else:
            out[f["alias"]] = None
    return out

def gql_issue_fields(repo, iss, fields, host):
    out = {}
    for f in fields:
        n = f["name"]
        if n == "id":
            out[f["alias"]] = iss["node_id"]
        elif n == "number":
            out[f["alias"]] = iss["number"]
        elif n == "title":
            out[f["alias"]] = iss["title"]
        elif n == "body":
            out[f["alias"]] = iss["body"]
        elif n == "state":
            out[f["alias"]] = iss["state"].upper()
        elif n == "url":
            out[f["alias"]] = "http://%s/%s/%s/issues/%d" % (host, repo["owner"], repo["name"], iss["number"])
        elif n == "author":
            out[f["alias"]] = gql_user_fields(iss["author"], f["fields"])
        elif n == "labels":
            out[f["alias"]] = {"nodes": [{"name": l} for l in iss["labels"]], "totalCount": len(iss["labels"])}
        elif n == "comments":
            nodes = [{"id": node_id("IssueComment", str(c["id"])), "body": c["body"],
                      "author": gql_user_fields(c["author"], [{"name": "login", "alias": "login", "args": {}, "fields": []}])} for c in iss["comments"]]
            out[f["alias"]] = {"nodes": nodes, "totalCount": len(nodes)}
        elif n == "createdAt":
            out[f["alias"]] = iso_for_seq(iss["created_seq"])
        elif n == "__typename":
            out[f["alias"]] = "Issue"
        else:
            out[f["alias"]] = None
    return out

def gql_pr_fields(repo, pr, fields, host):
    out = {}
    for f in fields:
        n = f["name"]
        if n == "id":
            out[f["alias"]] = pr["node_id"]
        elif n == "number":
            out[f["alias"]] = pr["number"]
        elif n == "title":
            out[f["alias"]] = pr["title"]
        elif n == "body":
            out[f["alias"]] = pr["body"]
        elif n == "state":
            out[f["alias"]] = "MERGED" if pr["merged"] else pr["state"].upper()
        elif n == "merged":
            out[f["alias"]] = pr["merged"]
        elif n == "url":
            out[f["alias"]] = "http://%s/%s/%s/pull/%d" % (host, repo["owner"], repo["name"], pr["number"])
        elif n == "author":
            out[f["alias"]] = gql_user_fields(pr["author"], f["fields"])
        elif n == "headRefName":
            out[f["alias"]] = pr["head"]
        elif n == "baseRefName":
            out[f["alias"]] = pr["base"]
        elif n == "createdAt":
            out[f["alias"]] = iso_for_seq(pr["created_seq"])
        elif n == "__typename":
            out[f["alias"]] = "PullRequest"
        else:
            out[f["alias"]] = None
    return out

def handle_graphql(body, host):
    try:
        req = json.loads(body.decode() or "{}")
    except Exception:
        return 400, {"errors": [{"message": "invalid JSON"}]}
    query = req.get("query", "")
    variables = req.get("variables") or {}
    try:
        op, fields = GQLParser(query, variables).parse()
    except Exception as e:
        return 400, {"errors": [{"message": str(e)}]}
    data = {}; errors = []
    for f in fields:
        if "fragment" in f:
            continue
        n = f["name"]
        try:
            if n == "viewer":
                data[f["alias"]] = gql_user_fields("forge", f["fields"])
            elif n == "repository":
                repo = get_repo(f["args"].get("owner", ""), f["args"].get("name", ""))
                data[f["alias"]] = gql_repo_fields(repo, f["fields"], host) if repo else None
                if not repo:
                    errors.append({"message": "repository not found"})
            elif n == "node":
                data[f["alias"]] = None
                errors.append({"message": "node lookup by id is not supported"})
            elif n == "search":
                q = (f["args"].get("query") or "")
                hits = []
                for key in sorted(STORE.repos):
                    r = STORE.repos[key]
                    if q.lower() in (r["owner"] + "/" + r["name"]).lower() or q.lower() in (r["description"] or "").lower():
                        hits.append({"__typename": "Repository", "name": r["name"], "nameWithOwner": key})
                data[f["alias"]] = {"nodes": hits, "issueCount": 0, "repositoryCount": len(hits)}
            elif n == "createIssue" and op == "mutation":
                inp = f["args"].get("input") or {}
                repo = None
                rid = inp.get("repositoryId")
                for r in STORE.repos.values():
                    if r["node_id"] == rid:
                        repo = r
                if not repo:
                    errors.append({"message": "repository not found for createIssue"})
                    data[f["alias"]] = None
                else:
                    iss = new_issue(repo, inp.get("title", ""), inp.get("body", ""), "forge",
                                    labels=inp.get("labelIds") or [])
                    STORE.save()
                    inner = {}
                    for sf in f["fields"]:
                        if sf["name"] == "issue":
                            inner[sf["alias"]] = gql_issue_fields(repo, iss, sf["fields"], host)
                        elif sf["name"] == "clientMutationId":
                            inner[sf["alias"]] = inp.get("clientMutationId")
                    data[f["alias"]] = inner
            elif n == "updateIssue" and op == "mutation":
                inp = f["args"].get("input") or {}
                target = None; trepo = None
                for r in STORE.repos.values():
                    for iss in r["issues"]:
                        if iss["node_id"] == inp.get("id"):
                            target = iss; trepo = r
                if not target:
                    errors.append({"message": "issue not found"})
                    data[f["alias"]] = None
                else:
                    if "title" in inp:
                        target["title"] = inp["title"]
                    if "body" in inp:
                        target["body"] = inp["body"]
                    if "state" in inp:
                        target["state"] = inp["state"].lower()
                        if target["state"] == "closed":
                            target["closed_seq"] = STORE.tick()
                    STORE.save()
                    inner = {}
                    for sf in f["fields"]:
                        if sf["name"] == "issue":
                            inner[sf["alias"]] = gql_issue_fields(trepo, target, sf["fields"], host)
                    data[f["alias"]] = inner
            elif n == "addComment" and op == "mutation":
                inp = f["args"].get("input") or {}
                target = None; trepo = None
                for r in STORE.repos.values():
                    for iss in r["issues"]:
                        if iss["node_id"] == inp.get("subjectId"):
                            target = iss; trepo = r
                if not target:
                    errors.append({"message": "subject not found"})
                    data[f["alias"]] = None
                else:
                    seq = STORE.tick()
                    c = {"id": STORE.new_id(), "body": inp.get("body", ""), "author": "forge", "created_seq": seq}
                    target["comments"].append(c)
                    STORE.save()
                    inner = {}
                    for sf in f["fields"]:
                        if sf["name"] == "commentEdge":
                            inner[sf["alias"]] = {"node": {"id": node_id("IssueComment", str(c["id"])), "body": c["body"]}}
                    data[f["alias"]] = inner
            elif n == "createPullRequest" and op == "mutation":
                inp = f["args"].get("input") or {}
                repo = next((r for r in STORE.repos.values() if r["node_id"] == inp.get("repositoryId")), None)
                if not repo:
                    errors.append({"message": "repository not found"})
                    data[f["alias"]] = None
                else:
                    pr = new_pr(repo, inp.get("title", ""), inp.get("body", ""), inp.get("headRefName", ""), inp.get("baseRefName", repo["default_branch"]))
                    STORE.save()
                    inner = {}
                    for sf in f["fields"]:
                        if sf["name"] == "pullRequest":
                            inner[sf["alias"]] = gql_pr_fields(repo, pr, sf["fields"], host)
                    data[f["alias"]] = inner
            else:
                errors.append({"message": "unsupported field %s" % n})
                data[f["alias"]] = None
        except Exception as e:
            errors.append({"message": str(e)})
            data[f["alias"]] = None
    resp = {"data": data}
    if errors:
        resp["errors"] = errors
    return 200, resp

# ---------------------------------------------------------------- MCP (JSON-RPC over POST /mcp)
MCP_TOOLS = [
    ("create_repository", "Create a repository", {"type": "object", "properties": {"owner": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}, "private": {"type": "boolean"}}, "required": ["owner", "name"]}),
    ("list_repositories", "List repositories", {"type": "object", "properties": {"owner": {"type": "string"}}}),
    ("get_repository", "Get a repository", {"type": "object", "properties": {"owner": {"type": "string"}, "name": {"type": "string"}}, "required": ["owner", "name"]}),
    ("create_issue", "Create an issue", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}}, "required": ["owner", "repo", "title"]}),
    ("list_issues", "List issues", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "state": {"type": "string"}}}),
    ("get_issue", "Get an issue", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "number": {"type": "integer"}}, "required": ["owner", "repo", "number"]}),
    ("update_issue", "Update/close an issue", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "number": {"type": "integer"}, "title": {"type": "string"}, "body": {"type": "string"}, "state": {"type": "string"}}, "required": ["owner", "repo", "number"]}),
    ("add_issue_comment", "Comment on an issue", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "number": {"type": "integer"}, "body": {"type": "string"}}, "required": ["owner", "repo", "number", "body"]}),
    ("create_pull_request", "Create a pull request", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}, "head": {"type": "string"}, "base": {"type": "string"}}, "required": ["owner", "repo", "title", "head", "base"]}),
    ("list_pull_requests", "List pull requests", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}}}),
    ("merge_pull_request", "Merge a pull request", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "number": {"type": "integer"}}, "required": ["owner", "repo", "number"]}),
    ("get_file_contents", "Get file contents", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "path": {"type": "string"}, "branch": {"type": "string"}}, "required": ["owner", "repo", "path"]}),
    ("create_or_update_file", "Create or update a file", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}, "message": {"type": "string"}, "branch": {"type": "string"}}, "required": ["owner", "repo", "path", "content"]}),
    ("search_issues", "Search issues", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
    ("list_commits", "List commits on a branch", {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "branch": {"type": "string"}}}),
]

def mcp_call_tool(name, args, host):
    owner = args.get("owner", "forge"); repo_name = args.get("repo") or args.get("name")
    repo = get_repo(owner, repo_name) if repo_name else None
    if name == "create_repository":
        r, _ = create_repo(owner, args["name"], args.get("description", ""), bool(args.get("private", False)))
        STORE.save(); return repo_json(r, host)
    if name == "list_repositories":
        return [repo_json(r, host) for k, r in sorted(STORE.repos.items()) if not owner or r["owner"] == owner]
    if name == "get_repository":
        if not repo:
            raise ValueError("repository not found")
        return repo_json(repo, host)
    if name == "create_issue":
        if not repo:
            raise ValueError("repository not found")
        iss = new_issue(repo, args["title"], args.get("body", ""))
        STORE.save(); return issue_json(repo, iss, host)
    if name == "list_issues":
        if not repo:
            raise ValueError("repository not found")
        st = args.get("state", "open")
        items = repo["issues"] if st == "all" else [i for i in repo["issues"] if i["state"] == st]
        return [issue_json(repo, i, host) for i in items]
    if name == "get_issue":
        if not repo:
            raise ValueError("repository not found")
        iss = next((i for i in repo["issues"] if i["number"] == int(args["number"])), None)
        if not iss:
            raise ValueError("issue not found")
        return issue_json(repo, iss, host)
    if name == "update_issue":
        if not repo:
            raise ValueError("repository not found")
        iss = next((i for i in repo["issues"] if i["number"] == int(args["number"])), None)
        if not iss:
            raise ValueError("issue not found")
        if "title" in args:
            iss["title"] = args["title"]
        if "body" in args:
            iss["body"] = args["body"]
        if "state" in args:
            iss["state"] = args["state"]
            if args["state"] == "closed":
                iss["closed_seq"] = STORE.tick()
        STORE.save(); return issue_json(repo, iss, host)
    if name == "add_issue_comment":
        if not repo:
            raise ValueError("repository not found")
        iss = next((i for i in repo["issues"] if i["number"] == int(args["number"])), None)
        if not iss:
            raise ValueError("issue not found")
        c = {"id": STORE.new_id(), "body": args["body"], "author": "forge", "created_seq": STORE.tick()}
        iss["comments"].append(c); STORE.save()
        return {"id": c["id"], "body": c["body"], "author": "forge"}
    if name == "create_pull_request":
        if not repo:
            raise ValueError("repository not found")
        pr = new_pr(repo, args["title"], args.get("body", ""), args["head"], args["base"])
        STORE.save(); return pr_json(repo, pr, host)
    if name == "list_pull_requests":
        if not repo:
            raise ValueError("repository not found")
        return [pr_json(repo, p, host) for p in repo["pulls"]]
    if name == "merge_pull_request":
        if not repo:
            raise ValueError("repository not found")
        pr = next((p for p in repo["pulls"] if p["number"] == int(args["number"])), None)
        if not pr:
            raise ValueError("pull request not found")
        merged = do_merge(repo, pr)
        STORE.save(); return {"merged": merged, "sha": repo["branches"].get(pr["base"]) or ""}
    if name == "get_file_contents":
        if not repo:
            raise ValueError("repository not found")
        files = repo_files(repo, args.get("branch"))
        if args["path"] not in files:
            raise ValueError("file not found")
        mode, oid = files[args["path"]]
        return {"path": args["path"], "sha": oid, "content": b64e(STORE.objects[oid][1])}
    if name == "create_or_update_file":
        if not repo:
            raise ValueError("repository not found")
        content = args["content"]
        try:
            raw = b64d(content)
        except Exception:
            raw = content.encode()
        branch = args.get("branch") or repo["default_branch"]
        oid = commit_files(repo, branch, {args["path"]: ("100644", raw)}, args.get("message") or "Update " + args["path"])
        STORE.save(); return {"sha": oid, "path": args["path"]}
    if name == "search_issues":
        q = args["query"].lower(); out = []
        for r in STORE.repos.values():
            for iss in r["issues"]:
                if q in (iss["title"] + " " + iss["body"]).lower():
                    out.append(issue_json(r, iss, host))
        return out
    if name == "list_commits":
        if not repo:
            raise ValueError("repository not found")
        branch = args.get("branch") or repo["default_branch"]
        head = repo["branches"].get(branch)
        out = []
        seen = set(); cur = head
        while cur and cur not in seen:
            seen.add(cur); o = get_object(cur)
            if not o:
                break
            tree, parents = parse_commit(o[1])
            out.append({"sha": cur, "message": o[1].decode("utf-8", "replace").split("\n\n", 1)[-1]})
            cur = parents[0] if parents else None
        return out
    raise ValueError("unknown tool " + name)

def do_merge(repo, pr):
    if pr["merged"] or pr["state"] != "open":
        return False
    head = repo["branches"].get(pr["head"]); base = repo["branches"].get(pr["base"])
    if not head:
        return False
    if base:
        tree, _ = parse_commit(get_object(head)[1])
        parents = [base, head]
    else:
        tree, _ = parse_commit(get_object(head)[1])
        parents = [head]
    oid = make_commit(tree, parents, "Merge pull request #%d: %s" % (pr["number"], pr["title"]))
    repo["branches"][pr["base"]] = oid
    pr["merged"] = True; pr["state"] = "closed"; pr["merged_seq"] = STORE.tick(); pr["closed_seq"] = pr["merged_seq"]
    return True

def handle_mcp(body):
    try:
        req = json.loads(body.decode() or "{}")
    except Exception:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
    mid = req.get("id"); method = req.get("method", "")
    host = req.get("_host", "127.0.0.1")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "forge", "version": VERSION}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, d, s in MCP_TOOLS]}}
    if method == "tools/call":
        params = req.get("params") or {}
        try:
            result = mcp_call_tool(params.get("name"), params.get("arguments") or {}, host)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}], "isError": False}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: " + method}}

# ---------------------------------------------------------------- REST v3 + HTTP
def err404(msg="Not Found"):
    return 404, {"message": msg, "documentation_url": "https://docs.github.com/rest"}

def json_body(handler):
    ln = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(ln) if ln else b""
    if not raw:
        return {}, raw
    try:
        return json.loads(raw.decode()), raw
    except Exception:
        return {}, raw

def current_user(handler):
    # No auth enforced: any token (or none) acts as user "forge".
    return "forge"

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Forge/1.0.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))

    # -- response helpers
    def send_json(self, status, obj, ctype="application/json"):
        data = json.dumps(obj, sort_keys=False).encode() if not isinstance(obj, (bytes, bytearray)) else bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-GitHub-Media-Type", "github.v3")
        self.send_header("X-RateLimit-Limit", "5000")
        self.send_header("X-RateLimit-Remaining", "4999")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_bytes(self, status, data, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        self.route()
    def do_POST(self):
        self.route()
    def do_PUT(self):
        self.route()
    def do_PATCH(self):
        self.route()
    def do_DELETE(self):
        self.route()
    def do_HEAD(self):
        self.route()

    def route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        host = self.headers.get("Host", "127.0.0.1")
        method = self.command
        try:
            with STORE.lock:
                status, obj, ctype = self.dispatch(method, path, qs, host)
        except BrokenPipeError:
            return
        except Exception as e:
            self.send_json(500, {"message": "internal error: %s" % e})
            return
        if ctype == "application/json":
            self.send_json(status, obj)
        else:
            self.send_bytes(status, obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode(), ctype)

    def dispatch(self, method, path, qs, host):
        body, raw = json_body(self) if method in ("POST", "PUT", "PATCH", "DELETE") else ({}, b"")
        # -- meta / export / health
        if path in ("/", "") and method == "GET":
            return 200, {"forge": "forge/1.0.0", "git": True, "rest": True, "graphql": True, "mcp": True}, "application/json"
        if path == "/health" and method == "GET":
            return 200, {"status": "ok"}, "application/json"
        if path in ("/_forge/export", "/_export", "/state/export", "/export") and method == "GET":
            return 200, STORE.export(), "application/octet-stream"
        if path == "/rate_limit" and method == "GET":
            return 200, {"resources": {"core": {"limit": 5000, "remaining": 4999, "reset": 1704067200}}, "rate": {"limit": 5000, "remaining": 4999}}, "application/json"
        if path == "/meta" and method == "GET":
            return 200, {"verifiable_password_authentication": False, "hooks": [], "git": [], "packages": [], "pages": []}, "application/json"
        if path in ("/user", "/user/") and method == "GET":
            return 200, {"login": "forge", "id": 1, "node_id": node_id("User", "forge"), "type": "User", "site_admin": False}, "application/json"
        if path == "/graphql" and method == "POST":
            return handle_graphql(raw, host) + ("application/json",)
        if path == "/mcp" and method == "POST":
            # inject host for URL building
            try:
                req = json.loads(raw.decode() or "{}")
                req["_host"] = host
                resp = handle_mcp(json.dumps(req).encode())
            except Exception:
                resp = handle_mcp(raw)
            if resp is None:
                return 202, b"", "application/json"
            return 200, resp, "application/json"
        if path == "/mcp" and method == "GET":
            return 200, {"jsonrpc": "2.0", "result": {"tools": [n for n, _, _ in MCP_TOOLS]}}, "application/json"

        # -- git smart HTTP: /owner/repo[.git]/info/refs , /owner/repo[.git]/git-upload-pack etc.
        m = re.match(r"^/([^/]+)/([^/]+?)(?:\.git)?/(info/refs|git-upload-pack|git-receive-pack)$", path)
        if m:
            owner, repo_name, svc = m.group(1), m.group(2), m.group(3)
            repo = get_repo(owner, repo_name)
            if not repo:
                return err404("Repository not found") + ("application/json",)
            if svc == "info/refs":
                service = (qs.get("service") or [""])[0]
                if service not in ("git-upload-pack", "git-receive-pack"):
                    return 400, {"message": "unknown service"}, "application/json"
                return 200, refs_advertisement(repo, service), "application/x-%s-advertisement" % service
            if svc == "git-upload-pack" and method == "POST":
                return handle_upload_pack(repo, raw)
            if svc == "git-receive-pack" and method == "POST":
                return handle_receive_pack(repo, raw)
            return 405, {"message": "method not allowed"}, "application/json"

        # -- search
        if path == "/search/repositories" and method == "GET":
            q = (qs.get("q") or [""])[0].lower()
            items = [repo_json(r, host) for k, r in sorted(STORE.repos.items())
                     if q in k.lower() or q in (r["description"] or "").lower()]
            return 200, {"total_count": len(items), "incomplete_results": False, "items": items}, "application/json"
        if path == "/search/issues" and method == "GET":
            q = (qs.get("q") or [""])[0].lower()
            items = []
            for r in STORE.repos.values():
                for iss in r["issues"]:
                    if q in (iss["title"] + " " + iss["body"]).lower():
                        items.append(issue_json(r, iss, host))
            return 200, {"total_count": len(items), "incomplete_results": False, "items": items}, "application/json"

        # -- create repo
        if path in ("/user/repos", "/orgs/forge/repos") and method == "POST":
            name = body.get("name")
            if not name:
                return 400, {"message": "name required"}, "application/json"
            repo, created = create_repo(current_user(self), name, body.get("description", ""), bool(body.get("private", False)))
            STORE.save()
            return (201 if created else 422), repo_json(repo, host), "application/json"
        m = re.match(r"^/orgs/([^/]+)/repos$", path)
        if m and method == "POST":
            name = body.get("name")
            if not name:
                return 400, {"message": "name required"}, "application/json"
            repo, created = create_repo(m.group(1), name, body.get("description", ""), bool(body.get("private", False)))
            STORE.save()
            return (201 if created else 422), repo_json(repo, host), "application/json"
        if path == "/user/repos" and method == "GET":
            return 200, [repo_json(r, host) for k, r in sorted(STORE.repos.items()) if r["owner"] == current_user(self)], "application/json"
        m = re.match(r"^/users/([^/]+)/repos$", path)
        if m and method == "GET":
            return 200, [repo_json(r, host) for k, r in sorted(STORE.repos.items()) if r["owner"] == m.group(1)], "application/json"

        # -- repos/:owner/:repo/...  (REST)
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if m:
            owner, repo_name, rest = m.group(1), m.group(2), m.group(3) or ""
            repo = get_repo(owner, repo_name)
            if not repo:
                return err404("Repository not found") + ("application/json",)
            return self.rest_repo(repo, rest, method, body, qs, host)
        # -- bare repo page /owner/repo  (git clone uses .git paths above; plain GET returns repo JSON)
        m = re.match(r"^/([^/]+)/([^/]+?)(?:\.git)?$", path)
        if m and method == "GET":
            repo = get_repo(m.group(1), m.group(2))
            if repo:
                return 200, repo_json(repo, host), "application/json"
        return err404() + ("application/json",)

    def rest_repo(self, repo, rest, method, body, qs, host):
        owner, name = repo["owner"], repo["name"]
        # GET /repos/:owner/:repo
        if rest in ("", "/") and method == "GET":
            return 200, repo_json(repo, host), "application/json"
        if rest in ("", "/") and method in ("PATCH",):
            if "description" in body:
                repo["description"] = body["description"]
            if "default_branch" in body:
                repo["default_branch"] = body["default_branch"]
            STORE.save()
            return 200, repo_json(repo, host), "application/json"
        if rest in ("", "/") and method == "DELETE":
            del STORE.repos[owner + "/" + name]
            STORE.save()
            return 204, b"", "application/json"
        # branches
        if rest == "/branches" and method == "GET":
            out = []
            for bname in sorted(repo["branches"]):
                oid = repo["branches"][bname]
                out.append({"name": bname, "commit": {"sha": oid or "", "url": ""}, "protected": False})
            return 200, out, "application/json"
        m = re.match(r"^/branches/([^/]+)$", rest)
        if m and method == "GET":
            bname = m.group(1)
            if bname not in repo["branches"]:
                return err404("Branch not found") + ("application/json",)
            return 200, {"name": bname, "commit": {"sha": repo["branches"][bname] or ""}, "protected": False}, "application/json"
        # git refs
        if rest == "/git/refs" and method == "GET":
            out = []
            for bname in sorted(repo["branches"]):
                oid = repo["branches"][bname]
                if oid:
                    out.append({"ref": "refs/heads/" + bname, "sha": oid, "url": "", "object": {"sha": oid, "type": "commit"}})
            return 200, out, "application/json"
        if rest == "/git/refs" and method == "POST":
            ref = body.get("ref", ""); sha = body.get("sha", "")
            if not ref.startswith("refs/heads/") or sha not in STORE.objects:
                return 422, {"message": "invalid ref/sha"}, "application/json"
            repo["branches"][ref[11:]] = sha
            STORE.save()
            return 201, {"ref": ref, "sha": sha, "object": {"sha": sha, "type": "commit"}}, "application/json"
        m = re.match(r"^/git/refs/(.+)$", rest)
        if m and method == "GET":
            ref = m.group(1)
            if ref.startswith("heads/") and ref[6:] in repo["branches"]:
                sha = repo["branches"][ref[6:]]
                return 200, {"ref": "refs/" + ref, "sha": sha, "object": {"sha": sha, "type": "commit"}}, "application/json"
            return err404("Reference not found") + ("application/json",)
        if m and method == "PATCH":
            ref = m.group(1); sha = body.get("sha", "")
            if ref.startswith("heads/") and sha in STORE.objects:
                repo["branches"][ref[6:]] = sha; STORE.save()
                return 200, {"ref": "refs/" + ref, "sha": sha, "object": {"sha": sha, "type": "commit"}}, "application/json"
            return 422, {"message": "invalid sha"}, "application/json"
        # git blobs / trees / commits (low-level, used by some clients)
        if rest == "/git/blobs" and method == "POST":
            content = body.get("content", "")
            encoding = body.get("encoding", "utf-8")
            raw = b64d(content) if encoding == "base64" else content.encode()
            oid = put_object("blob", raw); STORE.save()
            return 201, {"sha": oid, "size": len(raw), "url": ""}, "application/json"
        m = re.match(r"^/git/blobs/([0-9a-f]{40})$", rest)
        if m and method == "GET":
            o = get_object(m.group(1))
            if not o:
                return err404("Blob not found") + ("application/json",)
            return 200, {"sha": m.group(1), "size": len(o[1]), "content": b64e(o[1]), "encoding": "base64"}, "application/json"
        if rest == "/git/trees" and method == "POST":
            files = {}
            for e in body.get("tree", []):
                if e.get("type") == "blob" and "content" in e:
                    files[e["path"]] = ("100644", put_object("blob", e["content"].encode()))
                elif e.get("sha"):
                    files[e["path"]] = ("100644", e["sha"])
            tree_oid = build_tree(files); STORE.save()
            return 201, {"sha": tree_oid, "tree": []}, "application/json"
        m = re.match(r"^/git/trees/([0-9a-f]{40})$", rest)
        if m and method == "GET":
            o = get_object(m.group(1))
            if not o or o[0] != "tree":
                return err404("Tree not found") + ("application/json",)
            entries = [{"path": n, "mode": mo, "type": "tree" if mo == "40000" else "blob", "sha": oid, "size": len(get_object(oid)[1]) if get_object(oid) else 0} for mo, n, oid in parse_tree(o[1])]
            return 200, {"sha": m.group(1), "tree": entries, "truncated": False}, "application/json"
        if rest == "/git/commits" and method == "POST":
            tree = body.get("tree", ""); parents = body.get("parents", [])
            oid = make_commit(tree, parents, body.get("message", ""))
            STORE.save()
            return 201, {"sha": oid, "message": body.get("message", ""), "tree": {"sha": tree}, "parents": [{"sha": p} for p in parents]}, "application/json"
        m = re.match(r"^/git/commits/([0-9a-f]{40})$", rest)
        if m and method == "GET":
            o = get_object(m.group(1))
            if not o or o[0] != "commit":
                return err404("Commit not found") + ("application/json",)
            tree, parents = parse_commit(o[1])
            msg = o[1].decode("utf-8", "replace").split("\n\n", 1)[-1]
            return 200, {"sha": m.group(1), "message": msg, "tree": {"sha": tree}, "parents": [{"sha": p} for p in parents]}, "application/json"
        # commits list
        if rest == "/commits" and method == "GET":
            sha = (qs.get("sha") or [repo["default_branch"]])[0]
            head = repo["branches"].get(sha) or sha
            out = []; seen = set(); cur = head
            while cur and cur not in seen:
                seen.add(cur); o = get_object(cur)
                if not o or o[0] != "commit":
                    break
                tree, parents = parse_commit(o[1])
                out.append({"sha": cur, "commit": {"message": o[1].decode("utf-8", "replace").split("\n\n", 1)[-1], "tree": {"sha": tree}}, "parents": [{"sha": p} for p in parents]})
                cur = parents[0] if parents else None
            return 200, out, "application/json"
        m = re.match(r"^/commits/([0-9a-f]{40})$", rest)
        if m and method == "GET":
            o = get_object(m.group(1))
            if not o or o[0] != "commit":
                return err404("Commit not found") + ("application/json",)
            tree, parents = parse_commit(o[1])
            return 200, {"sha": m.group(1), "commit": {"message": o[1].decode("utf-8", "replace").split("\n\n", 1)[-1], "tree": {"sha": tree}}, "parents": [{"sha": p} for p in parents], "files": []}, "application/json"
        # contents
        m = re.match(r"^/contents(?:/(.*))?$", rest)
        if m:
            return self.contents(repo, m.group(1) or "", method, body, qs, host)
        # issues
        if rest == "/issues" and method == "GET":
            state = (qs.get("state") or ["open"])[0]
            items = repo["issues"] if state == "all" else [i for i in repo["issues"] if i["state"] == state]
            return 200, [issue_json(repo, i, host) for i in items], "application/json"
        if rest == "/issues" and method == "POST":
            if not body.get("title"):
                return 422, {"message": "title required"}, "application/json"
            iss = new_issue(repo, body["title"], body.get("body", ""), current_user(self),
                            labels=body.get("labels") or [], assignees=[a["login"] if isinstance(a, dict) else a for a in body.get("assignees") or []])
            STORE.save()
            return 201, issue_json(repo, iss, host), "application/json"
        m = re.match(r"^/issues/(\d+)$", rest)
        if m:
            num = int(m.group(1))
            iss = next((i for i in repo["issues"] if i["number"] == num), None)
            if not iss:
                return err404("Issue not found") + ("application/json",)
            if method == "GET":
                return 200, issue_json(repo, iss, host), "application/json"
            if method == "PATCH":
                if "title" in body:
                    iss["title"] = body["title"]
                if "body" in body:
                    iss["body"] = body["body"]
                if "state" in body:
                    iss["state"] = body["state"]
                    if body["state"] == "closed":
                        iss["closed_seq"] = STORE.tick()
                STORE.save()
                return 200, issue_json(repo, iss, host), "application/json"
        m = re.match(r"^/issues/(\d+)/comments$", rest)
        if m:
            num = int(m.group(1))
            iss = next((i for i in repo["issues"] if i["number"] == num), None)
            if not iss:
                return err404("Issue not found") + ("application/json",)
            if method == "GET":
                return 200, [{"id": c["id"], "body": c["body"], "user": {"login": c["author"]}, "created_at": iso_for_seq(c["created_seq"])} for c in iss["comments"]], "application/json"
            if method == "POST":
                c = {"id": STORE.new_id(), "body": body.get("body", ""), "author": current_user(self), "created_seq": STORE.tick()}
                iss["comments"].append(c); STORE.save()
                return 201, {"id": c["id"], "body": c["body"], "user": {"login": c["author"]}, "created_at": iso_for_seq(c["created_seq"])}, "application/json"
        # pulls
        if rest == "/pulls" and method == "GET":
            state = (qs.get("state") or ["open"])[0]
            items = repo["pulls"] if state == "all" else [p for p in repo["pulls"] if p["state"] == state]
            return 200, [pr_json(repo, p, host) for p in items], "application/json"
        if rest == "/pulls" and method == "POST":
            pr = new_pr(repo, body.get("title", ""), body.get("body", ""), body.get("head", ""), body.get("base", repo["default_branch"]))
            STORE.save()
            return 201, pr_json(repo, pr, host), "application/json"
        m = re.match(r"^/pulls/(\d+)$", rest)
        if m:
            num = int(m.group(1))
            pr = next((p for p in repo["pulls"] if p["number"] == num), None)
            if not pr:
                return err404("Pull request not found") + ("application/json",)
            if method == "GET":
                return 200, pr_json(repo, pr, host), "application/json"
            if method == "PATCH":
                if "title" in body:
                    pr["title"] = body["title"]
                if "body" in body:
                    pr["body"] = body["body"]
                if "state" in body:
                    pr["state"] = body["state"]
                    if body["state"] == "closed":
                        pr["closed_seq"] = STORE.tick()
                STORE.save()
                return 200, pr_json(repo, pr, host), "application/json"
        m = re.match(r"^/pulls/(\d+)/merge$", rest)
        if m and method == "PUT":
            num = int(m.group(1))
            pr = next((p for p in repo["pulls"] if p["number"] == num), None)
            if not pr:
                return err404("Pull request not found") + ("application/json",)
            if pr["merged"]:
                return 405, {"message": "already merged"}, "application/json"
            ok = do_merge(repo, pr)
            STORE.save()
            if not ok:
                return 405, {"message": "cannot merge"}, "application/json"
            return 200, {"merged": True, "sha": repo["branches"].get(pr["base"]) or "", "message": "Pull Request successfully merged"}, "application/json"
        m = re.match(r"^/pulls/(\d+)/(commits|files|reviews)$", rest)
        if m and method == "GET":
            num = int(m.group(1))
            pr = next((p for p in repo["pulls"] if p["number"] == num), None)
            if not pr:
                return err404("Pull request not found") + ("application/json",)
            return 200, [], "application/json"
        return err404() + ("application/json",)

    def contents(self, repo, path, method, body, qs, host):
        branch = (qs.get("ref") or [body.get("branch") or repo["default_branch"]])[0] if method in ("GET",) else (body.get("branch") or repo["default_branch"])
        if method == "GET":
            files = repo_files(repo, branch)
            if path == "":
                out = []
                for p in sorted(files):
                    if "/" not in p:
                        mode, oid = files[p]
                        out.append({"type": "file", "name": p, "path": p, "sha": oid, "size": len(STORE.objects[oid][1]), "url": "", "download_url": ""})
                for d in sorted({p.split("/")[0] for p in files if "/" in p}):
                    out.append({"type": "dir", "name": d, "path": d, "sha": "", "size": 0, "url": "", "download_url": None})
                return 200, out, "application/json"
            if path in files:
                mode, oid = files[path]
                raw = STORE.objects[oid][1]
                return 200, {"type": "file", "name": path.split("/")[-1], "path": path, "sha": oid,
                             "size": len(raw), "content": b64e(raw), "encoding": "base64", "url": ""}, "application/json"
            # directory listing
            sub = [p for p in files if p.startswith(path + "/")]
            if sub:
                out = []
                seen = set()
                for p in sorted(sub):
                    rest = p[len(path) + 1:]
                    first = rest.split("/")[0]
                    if first in seen:
                        continue
                    seen.add(first)
                    if "/" in rest:
                        out.append({"type": "dir", "name": first, "path": path + "/" + first, "sha": "", "size": 0})
                    else:
                        mode, oid = files[p]
                        out.append({"type": "file", "name": first, "path": p, "sha": oid, "size": len(STORE.objects[oid][1])})
                return 200, out, "application/json"
            return err404("File not found") + ("application/json",)
        if method in ("PUT", "DELETE"):
            if method == "DELETE":
                if path not in repo_files(repo, branch):
                    return err404("File not found") + ("application/json",)
                oid = commit_files(repo, branch, {path: None}, body.get("message") or "Delete " + path)
            else:
                if not body.get("content"):
                    return 422, {"message": "content required"}, "application/json"
                try:
                    raw = b64d(body["content"])
                except Exception:
                    raw = body["content"].encode()
                oid = commit_files(repo, branch, {path: ("100644", raw)}, body.get("message") or "Update " + path)
            STORE.save()
            return 200, {"content": {"name": path.split("/")[-1], "path": path, "sha": oid}, "commit": {"sha": oid, "message": body.get("message", "")}}, "application/json"
        return 405, {"message": "method not allowed"}, "application/json"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--export", action="store_true", help="print canonical empty export and exit")
    ap.add_argument("--export-file", default=None)
    args = ap.parse_args()
    if args.export or args.export_file:
        data = STORE.export()
        if args.export_file:
            with open(args.export_file, "wb") as f:
                f.write(data)
        else:
            sys.stdout.buffer.write(data)
        return
    STORE.load()
    port = args.port
    if port is None:
        port = int(os.environ.get("PORT", "0") or 0)
    if port == 8080:
        port = 0  # suite listens on 8080; never bind it
    server = ThreadingHTTPServer((args.host, port), Handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    print("PORT=%d" % actual, flush=True)
    print("Forge listening on http://%s:%d" % (args.host, actual), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
