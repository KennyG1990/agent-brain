"""
Semantic graph over the notes — the only part that needs an API key.

TWO BACKENDS
  1. graphify (https://github.com/safishamsi/graphify) if installed — the better,
     purpose-built tool: AST parsing, Leiden communities, HTML export.
  2. Built-in — a small LLM concept extractor using whatever provider you configured.
     Not as good; there so the app works for someone who won't install anything.

WHY THE WRITE IS A MERGE, NOT A REPLACE
A previous incarnation of this pipeline re-extracted a rolling subset every night and
OVERWROTE the graph. It went 1,781 nodes -> 201 in five weeks while the corpus tripled,
and most nights produced zero edges. Extraction here is incremental on input AND
additive on output, plus a shrink guard. Never reintroduce a bare overwrite.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import providers

SYSTEM = (
    "You extract a knowledge graph from a transcript of a work session between a person "
    "and an AI assistant. Return STRICT JSON only, no prose, no markdown fence.\n"
    'Schema: {"concepts":[{"name":str,"kind":"project|tool|file|decision|problem|technology"}],'
    '"edges":[{"from":str,"to":str,"rel":str}],"summary":str}\n'
    "Rules: 3-10 concepts, names under 40 chars, prefer proper nouns actually present in the "
    "text. Every edge must connect two names from your own concepts list. rel is 1-3 words. "
    "summary is one sentence, max 25 words. If the transcript is trivial, return empty lists."
)
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def graphify_available() -> bool:
    return shutil.which("graphify") is not None


def run_graphify(notes_dir: Path, log=print) -> bool:
    """Hand off to graphify if the user has it. Returns True on success."""
    log("graphify detected — using it (better than the built-in extractor).")
    try:
        r = subprocess.run(["graphify", "extract", str(notes_dir)],
                           capture_output=True, text=True, timeout=60 * 60)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"graphify failed to run: {e}")
        return False
    for line in (r.stdout or "").splitlines()[-25:]:
        log("  " + line)
    if r.returncode != 0:
        log(f"graphify exited {r.returncode}: {(r.stderr or '')[:300]}")
        return False
    return True


def _note_text(p: Path, cap: int = 12000) -> tuple[str, str]:
    raw = p.read_text(encoding="utf-8", errors="replace")
    title = p.stem
    m = re.search(r'^title:\s*"(.*)"', raw, re.M)
    if m:
        title = m.group(1)
    body = raw.split("## Conversation", 1)[-1]
    return title, body[:cap]


def _parse_json(txt: str) -> dict | None:
    txt = FENCE_RE.sub("", txt.strip())
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.S)   # model wrapped it in chatter
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def pending(vault: Path) -> list[tuple[Path, str, int]]:
    """(path, signature, estimated input tokens) for notes not yet extracted.

    Public so the CLI/GUI can show scope and cost BEFORE spending anything.
    """
    out_dir = vault / "graph"
    mp = out_dir / "extract-manifest.json"
    manifest = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    todo = []
    for p in sorted((vault / "notes").glob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue
        sig = f"{st.st_mtime_ns}:{st.st_size}"
        if manifest.get(p.name) != sig:
            todo.append((p, sig, min(st.st_size, 12000) // 4))   # ~4 chars/token
    return todo


def build(vault: Path, cfg: dict, limit: int | None = None, log=print,
          should_stop=lambda: False, meter=None, workers: int = 4) -> dict:
    """Extract concepts per note and MERGE into graph.json. Incremental + additive.

    Resumable: the manifest is written after every batch, so a crash, a spend cap or
    Ctrl-C costs you only the batch in flight. Re-running continues where it stopped.
    """
    import concurrent.futures as cf
    import threading

    out_dir = vault / "graph"
    out_dir.mkdir(parents=True, exist_ok=True)
    gp, mp = out_dir / "graph.json", out_dir / "extract-manifest.json"

    graph = json.loads(gp.read_text(encoding="utf-8")) if gp.exists() else {"nodes": [], "links": []}
    before = len(graph["nodes"])
    manifest = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    nodes = {n["id"]: n for n in graph["nodes"]}
    links = {(l["source"], l["target"], l.get("rel", "")): l for l in graph["links"]}
    lock = threading.Lock()

    todo = [(p, sig) for p, sig, _ in pending(vault)]
    if limit:
        todo = todo[:limit]
    if not todo:
        log("Graph is already current - nothing to extract.")
        return {"extracted": 0, "nodes": before, "links": len(links)}

    log(f"{len(todo)} note(s) to extract with {workers} worker(s). "
        "Unchanged notes are kept.")
    stop = threading.Event()
    counters = {"done": 0, "failed": 0}

    def work(item):
        p, sig = item
        if stop.is_set() or should_stop():
            return None
        title, body = _note_text(p)
        if len(body.strip()) < 200:
            return (p, sig, None)
        try:
            raw = providers.chat(cfg, SYSTEM, f"TITLE: {title}\n\nTRANSCRIPT:\n{body}",
                                 meter=meter, log=log)
        except providers.SpendCap:
            stop.set()
            raise
        except providers.ProviderError as e:
            log(f"  {p.name}: {e}")
            with lock:
                counters["failed"] += 1
                if counters["failed"] >= 5:
                    log("Five failures - stopping rather than burning quota on a bad config.")
                    stop.set()
            return None
        parsed = _parse_json(raw)
        if not parsed:
            log(f"  {p.name}: unparseable JSON, skipped")
            with lock:
                counters["failed"] += 1
            return None
        with lock:
            counters["failed"] = 0
        return (p, sig, parsed)

    capped = None
    # batch so the manifest and graph are flushed regularly -> genuinely resumable
    BATCH = max(workers * 3, 12)
    for start in range(0, len(todo), BATCH):
        if stop.is_set() or should_stop():
            break
        batch = todo[start:start + BATCH]
        results = []
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(work, it) for it in batch]
            for f in cf.as_completed(futures):
                try:
                    r = f.result()
                except providers.SpendCap as e:
                    capped = e
                    stop.set()
                    continue
                if r:
                    results.append(r)
        for p, sig, parsed in results:
            manifest[p.name] = sig
            if parsed is None:
                continue
            counters["done"] += 1
            _absorb(nodes, links, p, parsed)
        _write(gp, mp, nodes, links, manifest, out_dir)
        log(f"  ...{counters['done']}/{len(todo)} notes, {len(nodes)} nodes"
            + (f", {meter.line()}" if meter else ""))
    done = counters["done"]
    if capped:
        log(f"\n{capped}")
    # SHRINK GUARD - never install a graph smaller than what we already had.
    if len(nodes) < before * 0.8:
        log(f"SHRINK GUARD: refusing to save {len(nodes)} nodes over an existing {before}. "
            "Nothing was changed.")
        return {"extracted": done, "nodes": before, "links": len(links), "guarded": True}

    _write(gp, mp, nodes, links, manifest, out_dir)
    log(f"Graph: {len(nodes)} nodes, {len(links)} links (was {before} nodes). "
        f"Extracted {done} note(s). Open graph/graph.html to browse it.")
    return {"extracted": done, "nodes": len(nodes), "links": len(links)}


def _absorb(nodes: dict, links: dict, p: Path, data: dict) -> None:
    """Fold one note's extraction into the accumulating graph. Additive, never replacing."""
    nid = hashlib.md5(p.name.encode()).hexdigest()[:10]
    title = data.get("title") or p.stem
    nodes[f"note-{nid}"] = {"id": f"note-{nid}", "label": str(title)[:80],
                            "kind": "conversation", "source_file": p.name,
                            "summary": (data.get("summary") or "")[:200]}
    for c in (data.get("concepts") or [])[:10]:
        name = str(c.get("name", "")).strip()[:40]
        if not name:
            continue
        cid = "c-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        nodes.setdefault(cid, {"id": cid, "label": name,
                               "kind": str(c.get("kind", "concept"))[:20]})
        links[(f"note-{nid}", cid, "mentions")] = {
            "source": f"note-{nid}", "target": cid, "rel": "mentions"}
    for e in (data.get("edges") or [])[:20]:
        a = "c-" + re.sub(r"[^a-z0-9]+", "-", str(e.get("from", "")).lower()).strip("-")
        b = "c-" + re.sub(r"[^a-z0-9]+", "-", str(e.get("to", "")).lower()).strip("-")
        if a in nodes and b in nodes and a != b:
            rel = str(e.get("rel", "related"))[:30]
            links[(a, b, rel)] = {"source": a, "target": b, "rel": rel}


def _write(gp: Path, mp: Path, nodes: dict, links: dict, manifest: dict, out_dir: Path) -> None:
    gp.write_text(json.dumps({"nodes": list(nodes.values()),
                              "links": list(links.values())}, indent=1), encoding="utf-8")
    mp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    _html(out_dir / "graph.html", list(nodes.values()), list(links.values()))


def _html(path: Path, nodes: list, links: list) -> None:
    """Self-contained force-directed view. No CDN, works offline, opens in any browser."""
    payload = json.dumps({"nodes": nodes, "links": links})
    path.write_text(
        "<!doctype html><meta charset=utf-8><title>Agent Brain graph</title>"
        "<style>html,body{margin:0;background:#12141a;color:#ddd;"
        "font:13px system-ui,sans-serif;overflow:hidden}"
        "#i{position:fixed;top:10px;left:12px;z-index:2;background:#1b1f27cc;padding:8px 12px;"
        "border-radius:6px;max-width:340px}canvas{display:block}</style>"
        f"<div id=i><b>Agent Brain</b><br>{len(nodes)} nodes · {len(links)} links<br>"
        "<span style=color:#9aa0a6>drag to pan · scroll to zoom · hover a node</span>"
        "<div id=h style=margin-top:6px;color:#7aa2f7></div></div><canvas id=c></canvas>"
        f"<script>const D={payload};" + _JS + "</script>",
        encoding="utf-8")


_JS = r"""
const c=document.getElementById('c'),x=c.getContext('2d'),H=document.getElementById('h');
let W,Ht,tx=0,ty=0,z=1;function size(){W=c.width=innerWidth;Ht=c.height=innerHeight}
size();addEventListener('resize',size);
const N=D.nodes.map((n,i)=>({...n,x:Math.cos(i)*300+W/2,y:Math.sin(i*2.4)*300+Ht/2,vx:0,vy:0}));
const M=new Map(N.map(n=>[n.id,n]));
const L=D.links.map(l=>({s:M.get(l.source),t:M.get(l.target),rel:l.rel})).filter(l=>l.s&&l.t);
const col=k=>({conversation:'#7aa2f7',project:'#5bd6a0',tool:'#f0c674',file:'#c39ac9',
decision:'#f08080',problem:'#e88'}[k]||'#8b93a1');
function step(){for(const n of N){n.vx*=.85;n.vy*=.85}
for(let i=0;i<N.length;i++)for(let j=i+1;j<N.length;j++){const a=N[i],b=N[j];
let dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1;if(d<220){const f=(220-d)/d*.5;
a.vx-=dx*f*.02;a.vy-=dy*f*.02;b.vx+=dx*f*.02;b.vy+=dy*f*.02}}
for(const l of L){let dx=l.t.x-l.s.x,dy=l.t.y-l.s.y,d=Math.hypot(dx,dy)||1;const f=(d-120)/d*.01;
l.s.vx+=dx*f;l.s.vy+=dy*f;l.t.vx-=dx*f;l.t.vy-=dy*f}
for(const n of N){n.vx+=(W/2-n.x)*.0004;n.vy+=(Ht/2-n.y)*.0004;n.x+=n.vx;n.y+=n.vy}}
function draw(){step();x.setTransform(1,0,0,1,0,0);x.clearRect(0,0,W,Ht);
x.translate(tx,ty);x.scale(z,z);x.strokeStyle='#2b303b';x.lineWidth=1;
for(const l of L){x.beginPath();x.moveTo(l.s.x,l.s.y);x.lineTo(l.t.x,l.t.y);x.stroke()}
for(const n of N){const r=n.kind==='conversation'?5:4;x.beginPath();x.arc(n.x,n.y,r,0,7);
x.fillStyle=col(n.kind);x.fill();if(z>1.3){x.fillStyle='#aab';x.font='10px sans-serif';
x.fillText((n.label||'').slice(0,28),n.x+7,n.y+3)}}requestAnimationFrame(draw)}draw();
let dn=0,px,py;c.onmousedown=e=>{dn=1;px=e.clientX;py=e.clientY};
onmouseup=()=>dn=0;c.onmousemove=e=>{if(dn){tx+=e.clientX-px;ty+=e.clientY-py;px=e.clientX;py=e.clientY;return}
const mx=(e.clientX-tx)/z,my=(e.clientY-ty)/z;let best=null,bd=18;
for(const n of N){const d=Math.hypot(n.x-mx,n.y-my);if(d<bd){bd=d;best=n}}
H.textContent=best?(best.label+(best.summary?' — '+best.summary:'')):''};
c.onwheel=e=>{e.preventDefault();const k=e.deltaY<0?1.1:.9;z*=k;
tx=e.clientX-(e.clientX-tx)*k;ty=e.clientY-(e.clientY-ty)*k};
"""
