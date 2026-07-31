"""
Agent Brain desktop app.

A thin shell. Every decision lives in a module with no tkinter import, so the logic
is testable headlessly and the CLI can do everything this window can.

Tabs:
  Home      status, the one thing worth clicking, scan, search
  Graph     model picker, spend cap, extraction (the only tab that costs money)
  Settings  provider, API key, sources, scheduling
  Data      export, delete everything, privacy choice
"""
from __future__ import annotations

import json
import queue
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import (config, consent, data, graph, models, normalize, providers, recall,
               scheduler, secrets, sources, status)

BG, FG, DIM = "#14161a", "#e6e6e6", "#9aa0a6"
OK, WARN, BAD, ACC = "#5bd6a0", "#f0c674", "#f08080", "#7aa2f7"


class Tip:
    def __init__(self, w, text):
        self.w, self.text, self.tip = w, text, None
        w.bind("<Enter>", self._show); w.bind("<Leave>", self._hide)

    def _show(self, _e=None):
        if self.tip:
            return
        self.tip = t = tk.Toplevel(self.w)
        t.wm_overrideredirect(True)
        t.wm_geometry(f"+{self.w.winfo_rootx()+12}+{self.w.winfo_rooty()+self.w.winfo_height()+6}")
        tk.Label(t, text=self.text, bg="#242830", fg=FG, justify="left", padx=8, pady=5,
                 wraplength=430, font=("Segoe UI", 9)).pack()

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy(); self.tip = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Agent Brain")
        self.geometry("1040x820"); self.minsize(900, 700); self.configure(bg=BG)
        self.cfg = config.load()
        self.vault = config.vault_path(self.cfg)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self._all_models: list[dict] = []
        self._ranked: dict[str, dict] = {}
        self._style(); self._build()
        self.after(120, self._drain)
        self.after(300, self._first_run)

    # ------------------------------------------------------------------ #
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        for w in ("TFrame", "TLabelframe", "TNotebook"):
            s.configure(w, background=BG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("TLabelframe.Label", background=BG, foreground=DIM)
        s.configure("TNotebook.Tab", padding=(14, 7))

    def _build(self):
        head = ttk.Frame(self); head.pack(fill="x", padx=14, pady=(12, 2))
        ttk.Label(head, text="Agent Brain", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Button(head, text="↻", width=3, command=self.refresh).pack(side="right")
        self.vault_lbl = ttk.Label(head, text=str(self.vault), foreground=DIM)
        self.vault_lbl.pack(side="right", padx=8)

        nxf = tk.Frame(self, bg="#1b1f27", highlightthickness=1, highlightbackground="#2b303b")
        nxf.pack(fill="x", padx=14, pady=(6, 0))
        self.next_lbl = tk.Label(nxf, text="…", bg="#1b1f27", fg=FG, anchor="w",
                                 font=("Segoe UI", 10, "bold"), padx=12, pady=9, justify="left")
        self.next_lbl.pack(side="left", fill="x", expand=True)

        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=14, pady=8)
        self.tab_home, self.tab_graph = ttk.Frame(nb), ttk.Frame(nb)
        self.tab_set, self.tab_data = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(self.tab_home, text="Home"); nb.add(self.tab_graph, text="Graph  ($)")
        nb.add(self.tab_set, text="Settings"); nb.add(self.tab_data, text="Data")
        self._home(); self._graph(); self._settings(); self._data()

        pf = tk.Frame(self, bg=BG); pf.pack(fill="x", padx=14)
        self.pbar = ttk.Progressbar(pf, mode="indeterminate", length=220)
        self.prog = tk.Label(pf, text="idle", bg=BG, fg=DIM, anchor="w", font=("Consolas", 9))
        self.prog.pack(side="left", fill="x", expand=True)

        lf = ttk.Labelframe(self, text=" log ")
        lf.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.log = scrolledtext.ScrolledText(lf, height=8, bg="#0f1115", fg=DIM,
                                             font=("Consolas", 9), relief="flat", wrap="none")
        self.log.pack(fill="both", expand=True, padx=10, pady=8)
        for t, c in (("ok", OK), ("warn", WARN), ("bad", BAD), ("hdr", ACC)):
            self.log.tag_config(t, foreground=c)

    # ---- tabs ---------------------------------------------------------- #
    def _home(self):
        f = self.tab_home
        box = ttk.Labelframe(f, text=" state "); box.pack(fill="x", padx=8, pady=8)
        self.lbl = {}
        for k, cap in (("sources", "agents found"), ("notes", "conversations"),
                       ("index", "search index"), ("graph", "semantic graph"),
                       ("provider", "AI provider")):
            r = ttk.Frame(box); r.pack(fill="x", padx=10, pady=2)
            ttk.Label(r, text=cap, width=16, foreground=DIM).pack(side="left")
            l = tk.Label(r, text="…", bg=BG, fg=FG, anchor="w", font=("Consolas", 9),
                         justify="left", wraplength=760)
            l.pack(side="left", fill="x", expand=True); self.lbl[k] = l
        tk.Label(box, text="green = fine  ·  amber = degraded  ·  red = broken", bg=BG,
                 fg=DIM, font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10, pady=(2, 8))

        bar = ttk.Frame(f); bar.pack(fill="x", padx=8, pady=(0, 6))
        self.b_scan = ttk.Button(bar, text="Scan My Computer  (free)", command=self.do_scan)
        self.b_scan.pack(side="left")
        Tip(self.b_scan, "Reads your saved sessions from Claude Code, Cowork, Codex and "
                         "Gemini, turns each conversation into a note, and rebuilds the "
                         "search index.\n\nEntirely local. Nothing is uploaded. Free.")
        ttk.Button(bar, text="Open folder",
                   command=lambda: webbrowser.open(self.vault.as_uri())).pack(side="left", padx=6)

        sf = ttk.Labelframe(f, text=" ask your history "); sf.pack(fill="both", expand=True,
                                                                  padx=8, pady=6)
        r = ttk.Frame(sf); r.pack(fill="x", padx=10, pady=(8, 4))
        self.q_var = tk.StringVar()
        e = ttk.Entry(r, textvariable=self.q_var, font=("Segoe UI", 10))
        e.pack(side="left", fill="x", expand=True); e.bind("<Return>", lambda _e: self.do_search())
        ttk.Button(r, text="Search", command=self.do_search).pack(side="left", padx=6)
        self.results = scrolledtext.ScrolledText(sf, height=12, bg="#0f1115", fg=FG,
                                                 font=("Consolas", 9), relief="flat", wrap="word")
        self.results.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _graph(self):
        f = self.tab_graph
        tk.Label(f, text="The graph is OPTIONAL and is the only thing that costs money or "
                         "leaves your computer.\nSearch works fully without it.",
                 bg=BG, fg=WARN, font=("Segoe UI", 9), anchor="w",
                 justify="left").pack(fill="x", padx=10, pady=(10, 4))
        r = ttk.Frame(f); r.pack(fill="x", padx=10, pady=4)
        ttk.Label(r, text="search", width=10, foreground=DIM).pack(side="left")
        self.mfilter = tk.StringVar()
        ent = ttk.Entry(r, textvariable=self.mfilter, font=("Consolas", 9))
        ent.pack(side="left", fill="x", expand=True)
        self.mfilter.trace_add("write", lambda *_: self._refill())
        Tip(ent, "Filter: gemini · free · thinks:no · struct:yes · under:5")
        ttk.Button(r, text="↻ Load models", command=self.load_models).pack(side="left", padx=6)

        cols = ("fit", "model", "in", "out", "ctx", "think", "struct", "job")
        w = {"fit": 34, "model": 290, "in": 66, "out": 70, "ctx": 80, "think": 54,
             "struct": 54, "job": 62}
        tf = ttk.Frame(f); tf.pack(fill="both", expand=True, padx=10, pady=4)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=9, selectmode="browse")
        for c in cols:
            self.tree.heading(c, text=c, command=lambda cc=c: self._sort(cc))
            self.tree.column(c, width=w[c], anchor=("w" if c in ("fit", "model") else "e"),
                             stretch=(c == "model"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        for t, c in (("good", OK), ("risky", WARN), ("bad", BAD)):
            self.tree.tag_configure(t, foreground=c)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_model())
        self._sortk, self._sortd = "score", True

        r2 = ttk.Frame(f); r2.pack(fill="x", padx=10, pady=6)
        ttk.Label(r2, text="max spend $", foreground=DIM).pack(side="left")
        self.cap_var = tk.StringVar(value="5.00")
        ttk.Entry(r2, textvariable=self.cap_var, width=8).pack(side="left", padx=(4, 12))
        ttk.Label(r2, text="workers", foreground=DIM).pack(side="left")
        self.workers_var = tk.StringVar(value="4")
        ttk.Entry(r2, textvariable=self.workers_var, width=5).pack(side="left", padx=4)
        ttk.Button(r2, text="Preview (spends nothing)",
                   command=self.do_preview).pack(side="left", padx=12)
        self.b_graph = tk.Button(r2, text="BUILD GRAPH  ($)", command=self.do_graph,
                                 bg="#5a2020", fg="#ffdede", relief="flat", padx=12, pady=5,
                                 font=("Segoe UI", 9, "bold"))
        self.b_graph.pack(side="right")
        self.minfo = scrolledtext.ScrolledText(f, height=8, bg="#0f1115", fg=FG,
                                               font=("Consolas", 9), relief="flat", wrap="word")
        self.minfo.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.minfo.insert("1.0", "Click 'Load models' to rank every OpenRouter model for this "
                                 "job.\n\nThe ranking is NOT cheapest-first. Models that think "
                                 "by default can spend their whole output budget reasoning and "
                                 "return nothing, and models without structured output wrap "
                                 "JSON in code fences that fail to parse. Both are weighted "
                                 "above price.")

    def _settings(self):
        f = self.tab_set
        pf = ttk.Labelframe(f, text=" AI provider (only used for the graph) ")
        pf.pack(fill="x", padx=8, pady=8)
        r = ttk.Frame(pf); r.pack(fill="x", padx=10, pady=6)
        ttk.Label(r, text="provider", width=12, foreground=DIM).pack(side="left")
        self.prov = tk.StringVar(value=self.cfg.get("provider", "gemini"))
        ttk.Combobox(r, textvariable=self.prov, state="readonly", width=32,
                     values=[f"{k} - {v['label']}" for k, v in config.PROVIDERS.items()]
                     ).pack(side="left")
        r2 = ttk.Frame(pf); r2.pack(fill="x", padx=10, pady=6)
        ttk.Label(r2, text="API key", width=12, foreground=DIM).pack(side="left")
        self.key = tk.StringVar()
        self.key_e = ttk.Entry(r2, textvariable=self.key, show="•", font=("Consolas", 9))
        self.key_e.pack(side="left", fill="x", expand=True)
        self.show_key = tk.IntVar()
        ttk.Checkbutton(r2, text="show", variable=self.show_key,
                        command=lambda: self.key_e.config(
                            show="" if self.show_key.get() else "•")).pack(side="left", padx=4)
        ttk.Button(r2, text="Save", command=self.save_key).pack(side="left", padx=3)
        ttk.Button(r2, text="Forget", command=self.forget_key).pack(side="left")
        self.key_status = tk.Label(pf, text="", bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w")
        self.key_status.pack(fill="x", padx=10, pady=(0, 8))

        sf = ttk.Labelframe(f, text=" where your conversations come from ")
        sf.pack(fill="both", expand=True, padx=8, pady=8)
        self.src_box = scrolledtext.ScrolledText(sf, height=8, bg="#0f1115", fg=FG,
                                                 font=("Consolas", 9), relief="flat")
        self.src_box.pack(fill="both", expand=True, padx=10, pady=8)
        ttk.Button(sf, text="Add a folder manually…",
                   command=self.add_source).pack(padx=10, pady=(0, 8), anchor="w")

        kf = ttk.Labelframe(f, text=" automatic scanning (free, local) ")
        kf.pack(fill="x", padx=8, pady=8)
        r3 = ttk.Frame(kf); r3.pack(fill="x", padx=10, pady=8)
        self.sched_lbl = tk.Label(r3, text="…", bg=BG, fg=DIM, font=("Consolas", 9))
        self.sched_lbl.pack(side="left")
        ttk.Button(r3, text="Disable", command=lambda: self.sched(False)).pack(side="right")
        ttk.Button(r3, text="Enable daily 3am",
                   command=lambda: self.sched(True)).pack(side="right", padx=6)

    def _data(self):
        f = self.tab_data
        ef = ttk.Labelframe(f, text=" export - your data, out ")
        ef.pack(fill="x", padx=8, pady=8)
        r = ttk.Frame(ef); r.pack(fill="x", padx=10, pady=8)
        for fmt, label in (("obsidian", "Obsidian vault"), ("json", "JSON"),
                           ("markdown", "Markdown digest")):
            ttk.Button(r, text=label,
                       command=lambda x=fmt: self.do_export(x)).pack(side="left", padx=(0, 8))

        cf = ttk.Labelframe(f, text=" privacy ")
        cf.pack(fill="x", padx=8, pady=8)
        self.consent_lbl = tk.Label(cf, text="…", bg=BG, fg=FG, anchor="w", justify="left",
                                    font=("Consolas", 9), wraplength=880)
        self.consent_lbl.pack(fill="x", padx=10, pady=(8, 4))
        r2 = ttk.Frame(cf); r2.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(r2, text="Review the disclosure",
                   command=self.show_consent).pack(side="left")
        ttk.Button(r2, text="Switch to local-only",
                   command=self.go_local).pack(side="left", padx=6)

        df = ttk.Labelframe(f, text=" delete ")
        df.pack(fill="both", expand=True, padx=8, pady=8)
        self.del_box = scrolledtext.ScrolledText(df, height=9, bg="#0f1115", fg=FG,
                                                 font=("Consolas", 9), relief="flat")
        self.del_box.pack(fill="both", expand=True, padx=10, pady=8)
        r3 = ttk.Frame(df); r3.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(r3, text="What would be deleted?",
                   command=self.show_delete_plan).pack(side="left")
        tk.Button(r3, text="DELETE EVERYTHING", command=self.do_delete, bg="#5a2020",
                  fg="#ffdede", relief="flat", padx=10, pady=4).pack(side="right")

    # ---- first run ----------------------------------------------------- #
    def _first_run(self):
        if consent.answered():
            return
        self.show_consent(first=True)

    def show_consent(self, first: bool = False):
        w = tk.Toplevel(self); w.title("Before we start"); w.geometry("820x640")
        w.configure(bg=BG); w.transient(self); w.grab_set()
        t = scrolledtext.ScrolledText(w, bg="#0f1115", fg=FG, font=("Consolas", 9),
                                      relief="flat", wrap="word", padx=14, pady=12)
        t.pack(fill="both", expand=True, padx=10, pady=10)
        t.insert("1.0", consent.SUMMARY); t.config(state="disabled")
        r = ttk.Frame(w); r.pack(fill="x", padx=10, pady=(0, 12))

        def choose(accept, local):
            consent.record(accepted=accept, local_only=local)
            self._w("ok", f"privacy choice recorded: "
                          f"{'local only' if local else 'uploads allowed for the graph'}")
            w.destroy(); self.refresh()

        ttk.Button(r, text="See a sample of what would be sent",
                   command=lambda: messagebox.showinfo(
                       "Sample", consent.sample_upload(self.vault))).pack(side="left")
        ttk.Button(r, text="Local only - never upload",
                   command=lambda: choose(False, True)).pack(side="right", padx=6)
        ttk.Button(r, text="I understand - allow uploads for the graph",
                   command=lambda: choose(True, False)).pack(side="right")
        if not first:
            ttk.Button(r, text="Close", command=w.destroy).pack(side="right", padx=6)

    def go_local(self):
        consent.record(accepted=False, local_only=True)
        self._w("ok", "switched to local-only - nothing will be uploaded")
        self.refresh()

    # ---- actions ------------------------------------------------------- #
    def _bg(self, fn, name: str):
        if self.busy:
            messagebox.showinfo("Busy", "Something is already running."); return
        self.busy = True
        self.pbar.pack(side="right"); self.pbar.start(12)
        self.prog.config(text=f"▶ {name}", fg=ACC)
        self._w("hdr", f"\n$ {name}")

        def run():
            try:
                fn(lambda s: self.q.put(("", str(s))))
                self.q.put(("__done__", "ok"))
            except Exception as e:                       # noqa: BLE001
                self.q.put(("bad", f"{e.__class__.__name__}: {e}"))
                self.q.put(("__done__", "fail"))
        threading.Thread(target=run, daemon=True).start()

    def do_scan(self):
        def job(log):
            srcs = sources.active(self.cfg.get("extra_sources"))
            if not srcs:
                log("No agent session logs found on this computer."); return
            normalize.run(self.vault, srcs, self.cfg.get("max_msg_chars", 4000),
                          self.cfg.get("max_note_chars", 60000), log=log)
            recall.cmd_index(self.vault)
        self._bg(job, "scanning your computer (local, free)")

    def do_search(self):
        q = self.q_var.get().strip()
        if not q:
            return
        self.results.delete("1.0", "end")
        try:
            hits, total = recall.search(self.vault, q, 8)
        except FileNotFoundError:
            self.results.insert("1.0", "No index yet - click Scan My Computer."); return
        except ValueError as e:
            self.results.insert("1.0", str(e)); return
        if not hits:
            self.results.insert("1.0", f"No matches across {total} conversations.\n\n"
                                       "That does not prove it never happened - the current "
                                       "session is never indexed.")
            return
        out = [f"{len(hits)} of {total} conversations\n"]
        for h in hits:
            out.append(f"[{h['score']}] {h['title']}")
            out.append(f"   {h['source']} · {h['date'] or '?'} · notes/{h['note']}")
            if h["topics"]:
                out.append("   " + ", ".join(h["topics"]))
            out.append("")
        self.results.insert("1.0", "\n".join(out))

    def do_preview(self):
        todo = graph.pending(self.vault)
        res = config.resolve(self.cfg)
        a = self._ranked.get(self._sel())
        est = sum(t[2] for t in todo)
        cost = (f"~${est/1e6*a['in'] + est*0.45/1e6*a['out']:.2f}"
                if a and not a.get("free") else "unknown until you load models")
        messagebox.showinfo("Preview - nothing sent, nothing spent",
                            f"{len(todo)} note(s) would be extracted.\n"
                            f"~{est/1e6:.2f}M input tokens.\n\n"
                            f"Model: {self._sel()}\nEstimated: {cost}\n"
                            f"Cap: ${self.cap_var.get()}\n\n"
                            f"Consent: {'given' if consent.has_consent() else 'NOT GIVEN'}\n"
                            f"Endpoint: {res['base_url']}")

    def do_graph(self):
        res = config.resolve(self.cfg)
        ok, why = consent.gate(res["base_url"])
        if not ok:
            messagebox.showwarning("Refused", why + "\n\nSee the Data tab."); return
        a = self._ranked.get(self._sel())
        try:
            cap = float(self.cap_var.get() or 0)
        except ValueError:
            cap = 0.0
        warn = ""
        if a and a["thinks"]:
            warn = ("\n\nWARNING: this model thinks by default. Reasoning tokens consume the "
                    "output budget and can produce empty responses.")
        if not messagebox.askyesno("Build graph - this spends money",
                                   f"Model: {self._sel()}\nSpend cap: ${cap:.2f}\n"
                                   f"Endpoint: {res['base_url']}{warn}\n\nProceed?",
                                   icon="warning", default="no"):
            return
        res = dict(res, model=self._sel() or res["model"])
        try:
            workers = max(1, min(int(self.workers_var.get() or 4), 16))
        except ValueError:
            workers = 4
        meter = providers.Meter(a["in"] if a else 0, a["out"] if a else 0, cap)

        def job(log):
            out = graph.build(self.vault, res, meter=meter, workers=workers, log=log)
            log(f"done: {out}")
            log(f"spend: {meter.line()}")
        self._bg(job, f"building graph with {self._sel()}")

    # ---- models -------------------------------------------------------- #
    def load_models(self):
        n = status.notes_stats(self.vault).get("count", 0) or 500

        def job(log):
            rows = models.rank(models.fetch(self.key.get().strip()), n, 60)
            self.q.put(("__models__", json.dumps({"rows": rows, "notes": n})))
        self._bg(job, "fetching OpenRouter models (public, no key needed)")

    def _models_loaded(self, payload):
        d = json.loads(payload)
        self._all_models = d["rows"]
        self._ranked = {a["id"]: a for a in d["rows"]}
        self._notes = d["notes"]
        self._refill()

    def _sel(self) -> str:
        s = self.tree.selection()
        return s[0] if s else (self.cfg.get("model") or "")

    def _sort(self, col):
        k = {"fit": "score", "model": "id", "in": "in", "out": "out", "ctx": "ctx",
             "think": "thinks", "struct": "structured", "job": "cost"}.get(col, "score")
        self._sortd = (not self._sortd) if self._sortk == k else k in ("score", "ctx")
        self._sortk = k
        self._refill()

    def _refill(self):
        if not self._all_models:
            return
        q = self.mfilter.get().strip().lower()
        rows = [a for a in self._all_models if models.matches(a, q)]
        k = self._sortk
        rows.sort(key=lambda a: a[k] if not isinstance(a[k], str) else a[k].lower(),
                  reverse=self._sortd)
        self.tree.delete(*self.tree.get_children())
        for a in rows:
            good = not a["thinks"] and a["structured"]
            self.tree.insert("", "end", iid=a["id"],
                             tags=("good" if good else ("bad" if not a["structured"] else "risky"),),
                             values=("OK" if good else ("!!" if not a["structured"] else "~"),
                                     a["id"], "free" if a["free"] else f"{a['in']:.3f}",
                                     "free" if a["free"] else f"{a['out']:.3f}",
                                     f"{a['ctx']:,}", "yes" if a["thinks"] else "no",
                                     "yes" if a["structured"] else "NO",
                                     "free" if a["free"] else f"{a['cost']:.2f}"))
        if rows:
            self.tree.selection_set(rows[0]["id"]); self._show_model()

    def _show_model(self):
        a = self._ranked.get(self._sel())
        self.minfo.delete("1.0", "end")
        if a:
            self.minfo.insert("1.0", models.explain(a, getattr(self, "_notes", 500)))

    # ---- settings ------------------------------------------------------ #
    def save_key(self):
        ok, msg = secrets.save(self.key.get().strip())
        self._w("ok" if ok else "bad", msg)
        if not ok:
            messagebox.showwarning("Not saved", msg)
        self.cfg["provider"] = self.prov.get().split(" - ")[0]
        config.save(self.cfg)
        self.refresh()

    def forget_key(self):
        self.key.set("")
        messagebox.showinfo("Forget API key", secrets.purge_report())
        self.refresh()

    def add_source(self):
        d = filedialog.askdirectory(title="Folder containing agent session logs")
        if not d:
            return
        self.cfg.setdefault("extra_sources", []).append(d)
        config.save(self.cfg)
        self._w("ok", f"added source: {d}")
        self.refresh()

    def sched(self, on):
        ok, msg = scheduler.install(self.vault) if on else scheduler.uninstall()
        self._w("ok" if ok else "bad", str(msg))
        if not ok:
            messagebox.showwarning("Scheduling", str(msg))
        self.refresh()

    # ---- data ---------------------------------------------------------- #
    def do_export(self, fmt):
        if fmt == "obsidian":
            out = filedialog.askdirectory(title="Export Obsidian vault to…")
        else:
            out = filedialog.asksaveasfilename(
                title="Export to…",
                defaultextension=".json" if fmt == "json" else ".md")
        if not out:
            return
        p = data.EXPORTERS[fmt](self.vault, Path(out))
        self._w("ok", f"exported {fmt} -> {p}")
        messagebox.showinfo("Exported", str(p))

    def show_delete_plan(self):
        plan = data.deletion_plan(self.vault)
        self.del_box.delete("1.0", "end")
        if not plan:
            self.del_box.insert("1.0", "Nothing stored - nothing to delete."); return
        lines = ["This would permanently delete:\n"]
        for i in plan:
            lines.append(f"  {i['bytes']/1e6:>8.1f} MB  {i['files']:>5} files  {i['what']}")
            lines.append(f"            {i['path']}")
        lines.append(f"\n  TOTAL {sum(i['bytes'] for i in plan)/1e6:.1f} MB")
        self.del_box.insert("1.0", "\n".join(lines))

    def do_delete(self):
        self.show_delete_plan()
        if not messagebox.askyesno("Delete everything",
                                   "Permanently delete all notes, indexes, the graph, your "
                                   "settings and the stored key?\n\nThis cannot be undone. "
                                   "Export first if you want a copy.",
                                   icon="warning", default="no"):
            return
        for line in data.delete_everything(self.vault):
            self._w("warn", line)
        messagebox.showinfo("Deleted", "Everything was removed.")
        self.refresh()

    # ---- plumbing ------------------------------------------------------ #
    def _w(self, tag, line):
        self.log.insert("end", str(line).rstrip() + "\n", tag); self.log.see("end")

    def _drain(self):
        try:
            while True:
                tag, line = self.q.get_nowait()
                if tag == "__done__":
                    self.busy = False
                    self.pbar.stop(); self.pbar.pack_forget()
                    self.prog.config(text="idle", fg=DIM)
                    self.refresh()
                elif tag == "__models__":
                    self._models_loaded(line)
                else:
                    self._w(tag, line)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def refresh(self):
        self.cfg = config.load()
        snap = status.snapshot(self.vault, self.cfg)
        for k in self.lbl:
            d = snap[k]
            txt = d.get("detail", "?")
            self.lbl[k].config(text=txt,
                               fg=BAD if not d.get("ok", True) else
                               (WARN if "⚠" in txt else OK))
        nx = snap["next"]
        self.next_lbl.config(text="Next:  " + nx["text"],
                             fg={"ok": OK, "warn": WARN, "bad": BAD}.get(nx["level"], FG))
        key, src = secrets.load()
        self.key_status.config(
            text=f"stored via: {secrets.backend()}   ·   current: {secrets.mask(key)} ({src})")
        self.src_box.delete("1.0", "end")
        for s in sources.discover(self.cfg.get("extra_sources")):
            mark = "OK " if (s.found and s.files) else ("-- " if s.found else "   ")
            self.src_box.insert("end", f"{mark}{s.name:<28}{s.files:>6} files  {s.path}\n")
        okk, _ = scheduler.status()
        self.sched_lbl.config(text=f"{scheduler.backend()}: "
                                   f"{'enabled' if okk else 'not scheduled'}")
        if consent.is_local_only():
            self.consent_lbl.config(text="LOCAL ONLY - nothing is ever uploaded.", fg=OK)
        elif consent.has_consent():
            self.consent_lbl.config(
                text="Uploads allowed for the graph step only. Scanning and searching "
                     "remain entirely local.", fg=FG)
        else:
            self.consent_lbl.config(text="No choice recorded yet - uploads are blocked.", fg=WARN)


def run() -> int:
    App().mainloop()
    return 0
