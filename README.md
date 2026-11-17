# makefun-git-forge

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/adhiejoey/makefun-git-forge/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/adhiejoey/makefun-git-forge/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A standalone, single-process, no-UI GitHub-compatible git forge in one Python file: it serves git smart HTTP, the REST v3 API, GraphQL and MCP from its own store.

## Run it

```sh
python3 main.py
```

---

Entered on [make.fun](https://make.fun/?ref=mcpf7r82&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards

# Forge — a GitHub-compatible git forge (make.fun bounty `github-core-clone-01`)

A standalone, single-process, no-UI forge in **one Python file** (`main.py`,
Python 3.13 target, standard library only, no git binary, no package
manager, no outside service). It serves git smart HTTP, the REST v3 API,
GraphQL and MCP from its own store.

**Entry for make.fun's “Build a GitHub-compatible git forge” bounty (`github-core-clone-01`, $500).**

## Run

```sh
python3 main.py [--port PORT] [--host HOST]
# PORT env var is honoured unless it is 8080 (the suite listens on 8080).
# With no port, it binds an ephemeral port and prints PORT=<port>.
python3 main.py --export   # print the canonical (empty) state export
```

State is in-memory by default (fresh on every start, which is what makes
two runs on the same instance deterministic). Set `FORGE_DATA_FILE=<path>`
to persist state to a file between restarts.

## Deterministic state export

`GET /_forge/export` (aliases `/export`, `/_export`, `/state/export`)
returns a canonical JSON document: keys sorted, separators compact,
repositories/objects sorted, and **no wall-clock time anywhere** — all
timestamps derive from a logical sequence counter (`2024-01-01T00:00:00Z`
+ seq seconds), and commit author/committer timestamps use the same
logical clock, so identical operation sequences produce byte-identical
exports and identical commit SHAs across runs.

## What is implemented

### Git smart HTTP (`/<owner>/<repo>[.git]/...`) — real, tested with git 2.43
- `GET info/refs?service=git-upload-pack` / `git-receive-pack` — pkt-line
  advertisement, HEAD first, capabilities listed per service.
- `POST git-upload-pack` (protocol v0): `want`/`have`/`done`, `deepen`
  (shallow clones are two HTTP rounds, as git's stateless transport
  requires: round 1 returns only the shallow list, round 2 repeats it,
  then NAK + pack), `filter blob:none` (blobs omitted; other filters are
  treated as no filter), `side-band-64k`. Packfiles are generated, not
  faked: full (non-delta) objects, correct object headers, zlib streams,
  and the 20-byte SHA-1 trailer. No flush pkt-line between the final
  NAK/ACK and the packfile (per pack-protocol; a flush there is read as
  the pack header).
- `POST git-receive-pack`: command list (refs/heads/* only), packfile
  unpacking with **OFS_DELTA and REF_DELTA support** (git's thin packs
  on push), ref updates/deletes, `report-status` — side-band-wrapped
  with inner pkt-lines and an inner flush when the client requested
  side-band-64k.
- Protocol v2 (`command=ls-refs` / `command=fetch`) is implemented in a
  minimal form in `handle_upload_pack_v2` and exercised only by direct
  request; the git CLI in our tests negotiates v0 against this server,
  so v2 is **partially verified** (see "Stubs / partial" below).

### REST v3 (all under `/repos/:owner/:repo`, plus `/user/repos`, `/search/*`)
- Repos: create (`POST /user/repos`, `POST /orgs/:org/repos`), get,
  patch, delete, list by user; branches; git refs (get/list/create/patch);
  low-level git blobs/trees/commits (create + get); commits list/get.
- Issues: list/create/get/patch (close/reopen), comments list/create.
- Pull requests: list/create/get/patch, merge (`PUT .../pulls/:n/merge`
  creates a real merge commit with two parents), `/commits`, `/files`,
  `/reviews` (return `[]` — see stubs).
- Contents: get file/dir listings, create/update/delete file via a real
  commit on the requested branch.
- Search: `/search/repositories`, `/search/issues` (substring match).
- `/user`, `/rate_limit`, `/meta`, `/health`, `/` (forge info).

### GraphQL (`POST /graphql`)
A small real parser (tokenizer + AST, variables, aliases, fragments,
inline fragments) and executor for: `viewer`, `repository` (incl.
`issues`/`pullRequests` connections with `nodes`/`edges`/`totalCount`/
`pageInfo`, `issue`, `pullRequest`, `defaultBranchRef`), `search`, and
mutations `createIssue`, `updateIssue`, `addComment`, `createPullRequest`.
Unknown fields return a GraphQL `errors` entry instead of invented data.

### MCP (`POST /mcp`, JSON-RPC 2.0; `GET /mcp` lists tool names)
`initialize`, `ping`, `tools/list`, `tools/call` for 15 tools:
`create_repository`, `list_repositories`, `get_repository`,
`create_issue`, `list_issues`, `get_issue`, `update_issue`,
`add_issue_comment`, `create_pull_request`, `list_pull_requests`,
`merge_pull_request`, `get_file_contents`, `create_or_update_file`,
`search_issues`, `list_commits`. No SSE / stdio transport — HTTP
JSON-RPC only (see stubs).

## Stubs / partial / known limits
- **No authentication**: any token (or none) acts as user `forge`;
  owners are taken from the URL/body. No users/orgs/permissions model.
- **No tags**: `refs/tags/*` are not stored (receive-pack rejects
  non-`refs/heads/*` refs; upload-pack advertises heads only). No
  annotated-tag (`^{}`) peeling.
- **PRs**: `GET /pulls/:n/{commits,files,reviews}` returns `[]`;
  no review API, no checks, no diff computation; merge is a merge
  commit (or fast-forward-equivalent when base is empty), not a
  three-way merge with conflict detection.
- **GraphQL**: `node(id:)` is not supported (returns an error);
  only the fields listed above are resolved — anything else returns
  `null` plus an error entry. No `edges { cursor }` pagination cursors
  (pageInfo is static), no subscriptions.
- **Git**: no `filter` except `blob:none`; no `include-tag`; no
  `deepen-since`/`deepen-not` (parsed, not honoured — depth is honoured);
  no pack deltas on the *upload* side (full objects only — valid, just
  larger); protocol v2 fetch is minimal and not exercised by the git
  CLI in our tests; empty-repo clone behaves like an empty repo
  (zero-id `capabilities^{}` advertisement; git reports an empty
  repository warning).
- **MCP**: HTTP JSON-RPC only, no SSE stream, no stdio, no resources/
  prompts, no sampling.
- **No webhooks, actions, releases, packages, wikis, gists, or UI**
  (the bounty forbids a UI).
- **Persistence**: in-memory by default; `FORGE_DATA_FILE` opt-in file
  persistence is the same canonical export format.
- **Performance**: pure-Python, single store lock, no pack deltas on
  fetch — the timed workloads (200 pushes, 50 concurrent shallow
  clones, 2,000 issue reads, 200 issue writes) have only been smoke-
  tested at small scale locally (see `test_local.py`).

## Tests

```sh
python3 test_local.py   # 32 checks, all passing on 2026-11-17
```

Covers: REST repos/issues/PRs/contents/branches/refs/search, GraphQL
repository query + `createIssue`, MCP `tools/list` + `create_issue`,
deterministic export (two exports identical, before and after git ops),
real `git clone`, `git push`, and `git clone --depth 1` with git 2.43,
and a small load smoke test (50 issue reads, 20 issue writes).

Extra checks run ad hoc (see `TESTS.md` in the submission notes, if any):
cross-run export determinism, delta/thin-pack pushes, fetch with haves,
concurrent shallow clones.

## Bounty spec confirmed (from https://make.fun/bounty/github-core-clone-01)
- $500 total, 20 places paid (1st $100 … 11th–20th $14); a prize needs a
  score **above** the house reference forge (a tie does not beat it).
- Ends **Sat 21 Nov 2026 06:30 UTC**. At most 5 submissions/day, 10 total;
  last submission before the deadline is the final entry.
- Deliverable: public GitHub repo owned by your own account, created
  after the competition opens, OSI licence, `main.ts` (Node 22) **or**
  `main.py` (Python 3.13) in the repo root starting the forge as one
  process; AI coding-agent transcripts uploaded with each submission.
- Node 22 or Python 3.13, stdlib plus vendored source only; no git
  binary, package manager or outside service; one process binding an
  ephemeral port of its own (never 8080); deterministic state export
  byte-for-byte across two runs; ≤16 MiB source total, ≤8 MiB per file.
- Grading: sealed suite drives it as a client (push/clone, REST,
  GraphQL of the shape `gh` and `github-mcp-server` send, MCP), plus
  four timed workloads: 200 pushes, 50 concurrent shallow clones,
  2,000 issue reads, 200 issue writes. Three scored instances are
  drawn after the deadline.

## Submission notes
- Public GitHub repo `adhiejoey/makefun-git-forge` with `main.py` at its root, under the MIT licence.
- AI coding-agent transcripts are uploaded with the submission.
- Submission through make.fun before 21 Nov 2026 06:30 UTC (≤5/day, ≤10 total).
- A run of the make.fun kit's local runner / sealed-suite practice instances, if the kit is available — not run here.
- Performance work if the timed workloads are to beat the house reference: pack deltas on fetch, and load testing at full scale (200 pushes / 50 concurrent shallow clones / 2,000 reads / 200 writes).
