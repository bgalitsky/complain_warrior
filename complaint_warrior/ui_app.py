# ui_app.py
# -*- coding: utf-8 -*-

import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from text_processor import TextProcessing
from complaint_manager import ComplaintWarriorManager


YOUR_EMAIL_DEFAULT = "bgalitsky@hotmail.com"
YOUR_NAME_DEFAULT = "Boris Galitsky"


class ComplaintWarriorUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Complaint Warrior (multi-thread)")

        self.log_var = tk.StringVar(value="Ready.")

        self.selected_complaint_id = None
        self.selected_thread_id = None

        self._build_ui()

        self.tp = TextProcessing()
        self.manager = ComplaintWarriorManager(self.tp, log_cb=self.log)
        self.manager.start()

        self._refresh_loop()

    def log(self, msg: str):
        print(msg)
        self.log_var.set(msg)
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")
        self.root.update_idletasks()

    def on_load_inbound(self):
        cid = self.selected_complaint_id
        tid = self.selected_thread_id

        if not cid or not tid:
            messagebox.showwarning(
                "No selection",
                "Select a complaint and a thread first."
            )
            return

        try:
            view = self.manager.load_latest_inbound_view(cid, tid)

            if not view:
                messagebox.showinfo(
                    "No reply",
                    "No inbound reply found for this thread."
                )
                return

            # Show inbound message in timeline for now
            self.txt_timeline.insert(
                "end",
                "\n--- INBOUND FROM CUSTOMER SUPPORT ---\n"
                f"From: {view.get('from')}\n"
                f"Subject: {view.get('subject')}\n\n"
                f"{view.get('body')}\n"
            )
            self.txt_timeline.see("end")

            self.log("Inbound reply loaded.")

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _build_ui(self):
        paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True)

        # Left pane
        left = ttk.Frame(paned, padding=10)
        paned.add(left, weight=1)

        ttk.Label(left, text="New complaint", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        ttk.Label(left, text="Your email").pack(anchor="w")
        self.ent_email = ttk.Entry(left)
        self.ent_email.pack(fill="x")
        self.ent_email.insert(0, YOUR_EMAIL_DEFAULT)

        ttk.Label(left, text="Your name").pack(anchor="w", pady=(6, 0))
        self.ent_name = ttk.Entry(left)
        self.ent_name.pack(fill="x")
        self.ent_name.insert(0, YOUR_NAME_DEFAULT)

        ttk.Label(left, text="Subject").pack(anchor="w", pady=(10, 0))
        self.ent_subject = ttk.Entry(left)
        self.ent_subject.pack(fill="x")
        self.ent_subject.insert(0, "Complaint: flight delay reimbursement request")

        ttk.Label(left, text="Raw complaint").pack(anchor="w", pady=(6, 0))
        self.txt_raw = tk.Text(left, height=10)
        self.txt_raw.pack(fill="both", expand=False)
        self.txt_raw.insert("1.0", "Delayed 4+ hours, lost prepaid hotel cancellation fee $180. Request reimbursement + credit.")

        self.safe_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="SAFE MODE (drafts go to self only)", variable=self.safe_mode).pack(anchor="w", pady=(6, 0))

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="Add complaint", command=self.on_add_complaint).pack(side="left")
        ttk.Button(btns, text="Attach docs…", command=self.on_attach_docs).pack(side="left", padx=6)
        ttk.Button(btns, text="Build evidence PDF…", command=self.on_build_pdf).pack(side="left", padx=6)

        ttk.Separator(left).pack(fill="x", pady=10)

        ttk.Label(left, text="Complaints", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.lst_complaints = tk.Listbox(left, height=12)
        self.lst_complaints.pack(fill="both", expand=True)
        self.lst_complaints.bind("<<ListboxSelect>>", self.on_select_complaint)

        ttk.Button(left, text="Refresh list", command=self.refresh_lists).pack(anchor="w", pady=6)

        # Right pane
        right = ttk.Frame(paned, padding=10)
        paned.add(right, weight=2)

        self.lbl_current = ttk.Label(right, text="Select a complaint", font=("Segoe UI", 10, "bold"))
        self.lbl_current.pack(anchor="w")

        mid = ttk.Panedwindow(right, orient=tk.HORIZONTAL)
        mid.pack(fill="both", expand=True, pady=8)

        tleft = ttk.Frame(mid, padding=6)
        mid.add(tleft, weight=1)
        ttk.Label(tleft, text="Threads").pack(anchor="w")
        self.lst_threads = tk.Listbox(tleft, height=18)
        self.lst_threads.pack(fill="both", expand=True)
        self.lst_threads.bind("<<ListboxSelect>>", self.on_select_thread)

        tright = ttk.Frame(mid, padding=6)
        mid.add(tright, weight=3)

        # Decision/plan
        ttk.Label(tright, text="Latest GPT decision / plan").pack(anchor="w")
        self.txt_decision = tk.Text(tright, height=9)
        self.txt_decision.pack(fill="x", expand=False, pady=(0, 8))

        # Draft selector + send
        sendrow = ttk.Frame(tright)
        sendrow.pack(fill="x", pady=(0, 6))
        ttk.Button(
            sendrow,
            text="Load reply from Gmail",
            command=self.on_load_inbound  # ← must match method name
        ).pack(side="left", padx=6)

        ttk.Label(sendrow, text="Draft to send:").pack(side="left")
        self.cmb_drafts = ttk.Combobox(sendrow, state="readonly", width=75, values=[])
        self.cmb_drafts.pack(side="left", padx=6)

        self.btn_send = ttk.Button(sendrow, text="Send selected draft (to self, subject includes agent)", command=self.on_send_selected)
        self.btn_send.pack(side="left", padx=6)

        # Draft view
        ttk.Label(tright, text="Latest draft(s)").pack(anchor="w")
        self.txt_draft = tk.Text(tright, height=10)
        self.txt_draft.pack(fill="x", expand=False, pady=(0, 8))

        # Timeline
        ttk.Label(tright, text="Timeline").pack(anchor="w")
        self.txt_timeline = tk.Text(tright, height=12)
        self.txt_timeline.pack(fill="both", expand=True)

        # bottom log
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x")

        ttk.Label(bottom, textvariable=self.log_var, anchor="w").pack(side="left", fill="x", expand=True, padx=8)

        self.txt_log = tk.Text(self.root, height=6)
        self.txt_log.pack(fill="x")

    # ---------- actions ----------
    def on_add_complaint(self):
        subject = self.ent_subject.get().strip()
        raw = self.txt_raw.get("1.0", "end").strip()
        email = self.ent_email.get().strip()
        name = self.ent_name.get().strip()
        if not subject or not raw or not email:
            messagebox.showwarning("Missing", "Need subject, complaint text, and your email.")
            return
        try:
            cs = self.manager.add_complaint(
                subject=subject,
                complaint_raw=raw,
                user_email=email,
                user_name=name,
                safe_mode=bool(self.safe_mode.get()),
            )
            # Auto-create a starter thread so "Threads" is not empty.
            self.manager.create_agent_thread_seed(
                complaint_id=cs.complaint_id,
                agent_label="merchant_support",
                parent_thread_id=None,
                draft_email={"subject": cs.subject, "body": cs.complaint_professional},
            )

            self.log(f"Added complaint {cs.complaint_id}")
            self.refresh_lists()
        except Exception as e:
            messagebox.showerror("Add complaint failed", str(e))

    def on_attach_docs(self):
        cid = self.selected_complaint_id
        if not cid:
            messagebox.showwarning("No complaint", "Select a complaint first.")
            return
        paths = filedialog.askopenfilenames(title="Select evidence files")
        if not paths:
            return
        self.manager.attach_docs(cid, list(paths))
        self.log(f"Attached {len(paths)} doc(s) to {cid}")

    def on_build_pdf(self):
        cid = self.selected_complaint_id
        if not cid:
            messagebox.showwarning("No complaint", "Select a complaint first.")
            return
        out = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if not out:
            return
        self.manager.build_evidence_pdf(cid, out)
        self.log(f"Built evidence PDF for {cid}: {out}")

    def refresh_lists(self):
        self.lst_complaints.delete(0, tk.END)
        complaints = self.manager.list_complaints()
        for cs in sorted(complaints, key=lambda x: x.created_at, reverse=True):
            self.lst_complaints.insert(tk.END, f"{cs.complaint_id} | {cs.subject}")

        self.lst_threads.delete(0, tk.END)
        self.txt_decision.delete("1.0", "end")
        self.txt_draft.delete("1.0", "end")
        self.txt_timeline.delete("1.0", "end")
        self.cmb_drafts["values"] = []
        self.cmb_drafts.set("")

    def on_select_complaint(self, event=None):
        sel = self.lst_complaints.curselection()
        if not sel:
            return
        line = self.lst_complaints.get(sel[0])
        cid = line.split(" | ", 1)[0].strip()
        self.selected_complaint_id = cid
        cs = self.manager.get_complaint(cid)
        self.lbl_current.config(text=f"{cid} | {cs.subject}")

        self.lst_threads.delete(0, tk.END)
        for tid, ts in cs.threads.items():
            self.lst_threads.insert(tk.END, f"{tid} | {ts.label} | {ts.status}")

        self.selected_thread_id = None
        self._render_thread()

    def on_select_thread(self, event=None):
        sel = self.lst_threads.curselection()
        if not sel or not self.selected_complaint_id:
            return
        line = self.lst_threads.get(sel[0])
        tid = line.split(" | ", 1)[0].strip()
        self.selected_thread_id = tid
        self._render_thread()

    def _render_thread(self):
        cid = self.selected_complaint_id
        tid = self.selected_thread_id
        self.txt_decision.delete("1.0", "end")
        self.txt_draft.delete("1.0", "end")
        self.txt_timeline.delete("1.0", "end")
        self.cmb_drafts["values"] = []
        self.cmb_drafts.set("")

        if not cid or not tid:
            self.txt_decision.insert("1.0", "(Select a thread)")
            return

        cs = self.manager.get_complaint(cid)
        ts = cs.threads.get(tid)
        if not ts:
            self.txt_decision.insert("1.0", "(Thread missing)")
            return

        if ts.last_decision:
            self.txt_decision.insert("1.0", json.dumps(ts.last_decision, ensure_ascii=False, indent=2))
        else:
            self.txt_decision.insert("1.0", "(No decision yet — waiting for inbound reply)")

        # show all drafts
        drafts = ts.drafts or []
        if drafts:
            self.txt_draft.insert("1.0", json.dumps(drafts, ensure_ascii=False, indent=2))
            # populate combobox with user-friendly labels
            items = []
            for i, d in enumerate(drafts):
                agent = d.get("to_hint") or ts.label or "unknown"
                subj = (d.get("subject") or "")[:80]
                items.append(f"{i} | agent={agent} | {subj}")
            self.cmb_drafts["values"] = items
            self.cmb_drafts.current(0)
        else:
            self.txt_draft.insert("1.0", "(No drafts yet)")

        if ts.timeline:
            for ev in ts.timeline[-150:]:
                self.txt_timeline.insert("end", f"{ev.get('ts')} | {ev.get('kind')}: {ev.get('detail')}\n")

    def on_send_selected(self):
        cid = self.selected_complaint_id
        tid = self.selected_thread_id
        if not cid or not tid:
            messagebox.showwarning("No selection", "Select a complaint and thread.")
            return
        val = self.cmb_drafts.get().strip()
        if not val:
            messagebox.showwarning("No draft", "No draft selected.")
            return
        try:
            draft_index = int(val.split("|", 1)[0].strip())
            self.manager.send_selected_draft_to_self(cid, tid, draft_index)
            self.log("Draft sent (to self).")
        except Exception as e:
            messagebox.showerror("Send failed", str(e))

    # ---------- periodic refresh ----------
    def _refresh_loop(self):
        try:
            # refresh thread list for selected complaint
            if self.selected_complaint_id:
                cs = self.manager.get_complaint(self.selected_complaint_id)
                # rebuild thread list to show new auto-spawned threads
                current = set(self.lst_threads.get(0, "end"))
                desired = [f"{tid} | {ts.label} | {ts.status}" for tid, ts in cs.threads.items()]
                if set(desired) != current:
                    self.lst_threads.delete(0, tk.END)
                    for line in desired:
                        self.lst_threads.insert(tk.END, line)

            # refresh current thread view
            if self.selected_complaint_id and self.selected_thread_id:
                self._render_thread()
        finally:
            self.root.after(1500, self._refresh_loop)


def main():
    root = tk.Tk()
    root.geometry("1260x900")
    app = ComplaintWarriorUI(root)
    app.refresh_lists()
    root.mainloop()


if __name__ == "__main__":
    main()
