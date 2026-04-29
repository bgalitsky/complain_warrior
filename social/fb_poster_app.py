"""
Facebook Business Poster — Tkinter GUI
Loads complaints from cw_store.sqlite, lets you pick one, and posts to
the matching Facebook business page via a Chrome remote-debugging session.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import subprocess
import threading
import time
import urllib.parse
import json
import os
import re
import sqlite3


# ── Selenium imports ──────────────────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


# ── Config ────────────────────────────────────────────────────────────────────
CHROME_PATH   = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_DATA_DIR = r"C:\selenium_profile"
DEBUG_PORT    = 9222
DEFAULT_DB    = r"cw_store.sqlite"

# ── Colors ────────────────────────────────────────────────────────────────────
BG       = "#1a1d27"
PANEL    = "#23263a"
ACCENT   = "#4f8ef7"
ACCENT2  = "#6c63ff"
SUCCESS  = "#3ecf8e"
WARNING  = "#f7c948"
ERROR    = "#f75f5f"
TEXT     = "#e8eaf6"
SUBTEXT  = "#8890b5"
BORDER   = "#2e3250"
INPUT_BG = "#12141f"
SEL_BG   = "#2a3060"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_company(complaint_json: dict) -> str:
    """Best-effort company name from complaint data."""
    raw = complaint_json.get("complaint_raw", "")
    subject = complaint_json.get("subject", "")

    patterns = [
        r'\b(American Airlines|Southwest Airlines|United Airlines|Delta Airlines?'
        r'|CareFirst[\w\s]*|REI|IKEA|Home Depot|Lowe\'?s|Amazon|Walmart'
        r'|Robert Klein|David Chen|Mark Stevens)\b',
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    if subject:
        return subject.strip().title()
    return "Unknown Business"


def _load_complaints(db_path: str):
    rows = []
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT complaint_id, complaint_json FROM complaints ORDER BY rowid DESC")
        for cid, cjson in cur.fetchall():
            d = json.loads(cjson)
            rows.append({
                "complaint_id":           cid,
                "subject":                d.get("subject", ""),
                "company":                _extract_company(d),
                "status":                 d.get("current_status_summary", ""),
                "complaint_raw":          d.get("complaint_raw", ""),
                "complaint_professional": d.get("complaint_professional", ""),
            })
        con.close()
    except Exception:
        pass
    return rows


# ── Main App ──────────────────────────────────────────────────────────────────

class FBPosterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Facebook Business Poster")
        self.geometry("1020x700")
        self.resizable(True, True)
        self.configure(bg=BG)
        self.minsize(800, 560)

        self._chrome_proc       = None
        self._running           = False
        self._complaints        = []
        self._db_path           = DEFAULT_DB
        self._current_complaint = None

        self._build_ui()
        self._check_selenium()
        self._try_auto_load()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=ACCENT2, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="📣  Facebook Business Poster",
                 bg=ACCENT2, fg="white",
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=20, pady=10)
        self._status_dot = tk.Label(hdr, text="●", bg=ACCENT2, fg=SUBTEXT,
                                    font=("Segoe UI", 18))
        self._status_dot.pack(side="right", padx=18)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        # ── Left column ──────────────────────────────────────────────────────
        left = tk.Frame(body, bg=BG, width=230)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        # DB card
        db_card = self._card(left, "🗄  Database")
        db_card.pack(fill="x", pady=(0, 8))
        self._db_label = tk.Label(db_card, text=os.path.basename(self._db_path),
                                  bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 8),
                                  wraplength=200, justify="left")
        self._db_label.pack(anchor="w", padx=10, pady=(0, 6))
        self._button(db_card, "📂  Open DB…", self._open_db, ACCENT).pack(
            fill="x", padx=10, pady=(0, 10))

        # Chrome card
        ch_card = self._card(left, "⚙  Chrome")
        ch_card.pack(fill="x", pady=(0, 8))
        self._chrome_lbl = tk.Label(ch_card, text="Not launched",
                                    bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 8))
        self._chrome_lbl.pack(anchor="w", padx=10, pady=(0, 6))
        self._button(ch_card, "🚀  Launch Chrome", self._launch_chrome, ACCENT).pack(
            fill="x", padx=10, pady=(0, 10))

        # Methods card
        met_card = self._card(left, "📌  Posting Methods")
        met_card.pack(fill="x", pady=(0, 8))
        self._use_review  = tk.BooleanVar(value=True)
        self._use_comment = tk.BooleanVar(value=True)
        self._use_dm      = tk.BooleanVar(value=True)
        ck = dict(bg=PANEL, fg=TEXT, selectcolor=INPUT_BG,
                  activebackground=PANEL, activeforeground=TEXT,
                  font=("Segoe UI", 9), anchor="w", cursor="hand2")
        tk.Checkbutton(met_card, text="⭐  Review",
                       variable=self._use_review,  **ck).pack(fill="x", padx=10)
        tk.Checkbutton(met_card, text="💬  Comment on post",
                       variable=self._use_comment, **ck).pack(fill="x", padx=10)
        tk.Checkbutton(met_card, text="✉️  Direct Message",
                       variable=self._use_dm,      **ck).pack(fill="x", padx=10, pady=(0, 8))

        # Stop button
        self._btn_stop = self._button(left, "■  Stop", self._stop_posting, ERROR)
        self._btn_stop.pack(fill="x", pady=(6, 0))
        self._btn_stop.config(state="disabled")

        # ── Middle column: complaint list ─────────────────────────────────────
        mid = tk.Frame(body, bg=BG, width=310)
        mid.pack(side="left", fill="y", padx=(0, 10))
        mid.pack_propagate(False)

        mid_hdr = tk.Frame(mid, bg=BG)
        mid_hdr.pack(fill="x", pady=(0, 4))
        tk.Label(mid_hdr, text="📋  Complaints",
                 bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self._count_lbl = tk.Label(mid_hdr, text="", bg=BG, fg=SUBTEXT,
                                   font=("Segoe UI", 8))
        self._count_lbl.pack(side="right")

        list_frame = tk.Frame(mid, bg=BORDER)
        list_frame.pack(fill="both", expand=True)
        self._listbox = tk.Listbox(
            list_frame, bg=INPUT_BG, fg=TEXT,
            selectbackground=SEL_BG, selectforeground=TEXT,
            font=("Segoe UI", 9), relief="flat",
            activestyle="none", borderwidth=0,
            highlightthickness=0)
        sb = tk.Scrollbar(list_frame, orient="vertical",
                          command=self._listbox.yview, bg=PANEL)
        self._listbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._listbox.pack(fill="both", expand=True, padx=1, pady=1)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)
        self._listbox.bind("<Double-Button-1>",  self._on_double_click)

        # ── Right column: detail + log ────────────────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        det_card = self._card(right, "✏  Selected Complaint")
        det_card.pack(fill="x", pady=(0, 8))

        row1 = tk.Frame(det_card, bg=PANEL)
        row1.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(row1, text="Company:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9), width=10, anchor="w").grid(row=0, column=0, sticky="w")
        self._company_var = tk.StringVar()
        tk.Entry(row1, textvariable=self._company_var,
                 bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("Segoe UI", 10),
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).grid(row=0, column=1, sticky="ew",
                                             padx=(4, 0), ipady=4)
        row1.columnconfigure(1, weight=1)

        tk.Label(row1, text="Status:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9), width=10, anchor="w").grid(
                     row=1, column=0, sticky="w", pady=(4, 0))
        self._status_var = tk.StringVar(value="—")
        tk.Label(row1, textvariable=self._status_var,
                 bg=PANEL, fg=WARNING, font=("Segoe UI", 8),
                 wraplength=260, justify="left", anchor="w").grid(
                     row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        tk.Label(det_card, text="Message to post:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(4, 2))
        self._msg_text = tk.Text(det_card, bg=INPUT_BG, fg=TEXT,
                                 insertbackground=TEXT, relief="flat",
                                 font=("Segoe UI", 9), wrap="word", height=5,
                                 highlightthickness=1, highlightbackground=BORDER,
                                 highlightcolor=ACCENT)
        self._msg_text.pack(fill="x", padx=10, pady=(0, 6))

        tog = tk.Frame(det_card, bg=PANEL)
        tog.pack(fill="x", padx=10, pady=(0, 8))
        self._msg_mode = tk.StringVar(value="professional")
        for val, lbl in (("professional", "Professional"), ("raw", "Raw / Original")):
            tk.Radiobutton(tog, text=lbl, variable=self._msg_mode, value=val,
                           bg=PANEL, fg=TEXT, selectcolor=INPUT_BG,
                           activebackground=PANEL, activeforeground=TEXT,
                           font=("Segoe UI", 9), cursor="hand2",
                           command=self._refresh_message).pack(side="left", padx=(0, 14))

        # Post Now button — visible, explicit trigger
        self._btn_post = self._button(det_card, "▶  Post Now  (or double-click complaint)",
                                      self._start_posting, SUCCESS)
        self._btn_post.pack(fill="x", padx=10, pady=(0, 12))

        # Log
        log_hdr = tk.Frame(right, bg=BG)
        log_hdr.pack(fill="x", pady=(0, 4))
        tk.Label(log_hdr, text="📟  Activity Log", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Button(log_hdr, text="Clear", bg=BORDER, fg=SUBTEXT,
                  relief="flat", font=("Segoe UI", 8), cursor="hand2",
                  command=self._clear_log, padx=8).pack(side="right")
        log_outer = tk.Frame(right, bg=BORDER)
        log_outer.pack(fill="both", expand=True)
        self._log = scrolledtext.ScrolledText(
            log_outer, bg=INPUT_BG, fg=TEXT, font=("Consolas", 9),
            relief="flat", state="disabled", wrap="word")
        self._log.pack(fill="both", expand=True, padx=1, pady=1)
        for tag, color in [("info", TEXT), ("success", SUCCESS), ("warn", WARNING),
                           ("error", ERROR), ("step", ACCENT), ("dim", SUBTEXT)]:
            self._log.tag_config(tag, foreground=color)

        # Status bar
        bar = tk.Frame(self, bg=PANEL, height=24)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._statusbar = tk.Label(bar, text="Ready.", bg=PANEL, fg=SUBTEXT,
                                   font=("Segoe UI", 8), anchor="w")
        self._statusbar.pack(side="left", padx=10)

    # ── Widget helpers ────────────────────────────────────────────────────────

    def _card(self, parent, title):
        f = tk.Frame(parent, bg=PANEL,
                     highlightthickness=1, highlightbackground=BORDER)
        tk.Label(f, text=title, bg=PANEL, fg=ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10, pady=(8, 3))
        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", padx=6, pady=(0, 5))
        return f

    def _button(self, parent, text, cmd, color):
        return tk.Button(parent, text=text, command=cmd,
                         bg=color, fg="white",
                         activebackground=color, activeforeground="white",
                         relief="flat", font=("Segoe UI", 9, "bold"),
                         cursor="hand2", padx=8, pady=7)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_msg(self, text, tag="info"):
        def _do():
            self._log.config(state="normal")
            ts = time.strftime("%H:%M:%S")
            self._log.insert("end", f"[{ts}] ", "dim")
            self._log.insert("end", text + "\n", tag)
            self._log.config(state="disabled")
            self._log.see("end")
            self._statusbar.config(text=text[:100])
        self.after(0, _do)

    def _clear_log(self):
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")

    def _set_dot(self, color):
        self.after(0, lambda: self._status_dot.config(fg=color))

    # ── DB ────────────────────────────────────────────────────────────────────

    def _try_auto_load(self):
        if os.path.exists(self._db_path):
            self._load_db(self._db_path)
        else:
            self._log_msg("No database found. Click 'Open DB…' to load complaints.", "warn")

    def _open_db(self):
        path = filedialog.askopenfilename(
            title="Open cw_store database",
            filetypes=[("SQLite DB", "*.sqlite *.db"), ("All files", "*.*")])
        if path:
            self._load_db(path)

    def _load_db(self, path):
        self._db_path = path
        self._db_label.config(text=os.path.basename(path))
        self._complaints = _load_complaints(path)
        self._populate_list()
        n = len(self._complaints)
        self._log_msg(
            f"Loaded {n} complaint{'s' if n != 1 else ''} from {os.path.basename(path)}",
            "success")

    def _populate_list(self):
        self._listbox.delete(0, "end")
        for c in self._complaints:
            s = c["status"].lower()
            icon = ("✅" if "resolved" in s else
                    "⏳" if ("underway" in s or "negotiation" in s) else
                    "📤" if ("sent" in s or "waiting" in s) else "🔵")
            self._listbox.insert("end",
                f"{icon}  {c['company']}  —  {c['subject'][:28]}")
        self._count_lbl.config(text=f"{len(self._complaints)}")

    # ── Selection: fill form only, no auto-post ───────────────────────────────

    def _on_select(self, event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        c = self._complaints[sel[0]]
        self._current_complaint = c
        self._company_var.set(c["company"])
        self._status_var.set(c["status"][:120] or "—")
        self._refresh_message()
        self._log_msg(
            f"Loaded: '{c['company']}'  —  edit fields then click Post Now or double-click.",
            "dim")

    def _on_double_click(self, event=None):
        """Double-click on list item → start posting immediately."""
        sel = self._listbox.curselection()
        if not sel:
            return
        # Ensure form is populated (single-click may not have fired yet)
        self._on_select()
        self._start_posting()

    def _refresh_message(self):
        if not self._current_complaint:
            return
        c = self._current_complaint
        text = (c["complaint_professional"]
                if self._msg_mode.get() == "professional"
                else c["complaint_raw"])
        self._msg_text.delete("1.0", "end")
        self._msg_text.insert("1.0", text)

    # ── Chrome ────────────────────────────────────────────────────────────────

    def _launch_chrome(self):
        self._log_msg("Launching Chrome with remote debugging…", "step")
        try:
            self._chrome_proc = subprocess.Popen([
                CHROME_PATH,
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={USER_DATA_DIR}",
            ])
            self._log_msg(f"Chrome started (PID {self._chrome_proc.pid})", "success")
            self._chrome_lbl.config(
                text=f"PID {self._chrome_proc.pid}  •  port {DEBUG_PORT}",
                fg=SUCCESS)
            self._set_dot(SUCCESS)
            self._log_msg("Log in to Facebook, then click a complaint to post.", "warn")
        except FileNotFoundError:
            self._log_msg(f"Chrome not found at:  {CHROME_PATH}", "error")
        except Exception as e:
            self._log_msg(f"Chrome launch failed: {e}", "error")

    # ── Posting ───────────────────────────────────────────────────────────────

    def _start_posting(self):
        if self._running:
            self._log_msg("Already running — stop first or wait.", "warn")
            return
        if not SELENIUM_OK:
            self._log_msg("selenium not installed.  Run:  pip install selenium", "error")
            return

        biz = self._company_var.get().strip()
        msg = self._msg_text.get("1.0", "end").strip()

        if not biz:
            self._log_msg("Company name is empty!", "error")
            return
        if not msg:
            self._log_msg("Message is empty!", "error")
            return

        use = {
            "review":  self._use_review.get(),
            "comment": self._use_comment.get(),
            "dm":      self._use_dm.get(),
        }
        if not any(use.values()):
            self._log_msg("Select at least one posting method!", "error")
            return

        self._running = True
        self._btn_stop.config(state="normal")
        self._set_dot(WARNING)
        active = ", ".join(k for k, v in use.items() if v)
        self._log_msg(f"Posting to '{biz}'  [{active}]", "step")

        threading.Thread(target=self._run_poster,
                         args=(biz, msg, use), daemon=True).start()

    def _stop_posting(self):
        self._running = False
        self._log_msg("Stop requested.", "warn")
        self._btn_stop.config(state="disabled")
        self._set_dot(SUBTEXT)

    def _finish(self):
        self._running = False
        self.after(0, lambda: self._btn_stop.config(state="disabled"))
        self.after(0, lambda: self._set_dot(ACCENT))
        self._log_msg("── Session finished ──", "dim")

    # ── Selenium worker ───────────────────────────────────────────────────────

    def _run_poster(self, business_name, message, use):
        self._log_msg(f"Connecting to Chrome on port {DEBUG_PORT}…", "step")
        try:
            opts = Options()
            opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{DEBUG_PORT}")
            driver = webdriver.Chrome(options=opts)
            self._log_msg("Connected ✓", "success")
        except Exception as e:
            self._log_msg(f"Could not connect to Chrome: {e}", "error")
            self._log_msg("Launch Chrome and log into Facebook first.", "warn")
            self._finish()
            return

        wait    = WebDriverWait(driver, 20)
        success = False

        try:
            # Search
            if not self._running:
                return
            self._log_msg(f"Searching for: {business_name}", "step")
            driver.get(
                f"https://www.facebook.com/search/top?q={urllib.parse.quote(business_name)}")
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@role='article']")))
                self._log_msg("Search results loaded ✓", "success")
            except Exception:
                self._log_msg("Search results didn't render — logged in?", "error")
                self._finish(); return

            # Page URL
            if not self._running:
                return
            self._log_msg("Finding business page URL…", "step")
            try:
                link_xpath = (
                    "//div[@role='article']//a[contains(@href, '/pages/')] | "
                    "//div[@role='article']//a[@role='link'] | "
                    f"//a[contains(@aria-label, '{business_name}')]"
                )
                first = wait.until(EC.element_to_be_clickable((By.XPATH, link_xpath)))
                page_url = first.get_attribute("href").split("?")[0]
                if not page_url.endswith("/"):
                    page_url += "/"
                self._log_msg(f"Page: {page_url}", "success")
            except Exception:
                self._log_msg("Business page not found in results.", "error")
                self._finish(); return

            # Option 1: Review
            if use["review"] and not success and self._running:
                self._log_msg("Option 1 — Reviews tab…", "step")
                driver.get(f"{page_url}reviews/")
                time.sleep(4)
                try:
                    yes_btn = wait.until(EC.element_to_be_clickable((By.XPATH,
                        "//span[text()='Yes' or text()='Да']/ancestor::a")))
                    yes_btn.click()
                    self._log_msg("Clicked 'Yes' recommendation.", "info")
                    box = wait.until(EC.presence_of_element_located(
                        (By.XPATH, "//div[@role='textbox']")))
                    box.send_keys(message)
                    driver.find_element(By.XPATH,
                        "//div[@aria-label='Post' or @aria-label='Опубликовать']").click()
                    self._log_msg("🏆  Review posted!", "success")
                    success = True
                except Exception as e:
                    self._log_msg(f"Review unavailable ({type(e).__name__}).", "warn")

            # Option 2: Comment on latest post (discussion)
            if use["comment"] and not success and self._running:
                self._log_msg("Option 2 — comment on latest post…", "step")
                success = self._post_to_discussion(driver, business_name, message, page_url)

            # Option 3: Direct Message
            if use["dm"] and not success and self._running:
                self._log_msg("Option 3 — Direct Message…", "step")
                driver.get(page_url)
                try:
                    msg_btn = wait.until(EC.element_to_be_clickable((By.XPATH,
                        "//div[@aria-label='Message' or @aria-label='Сообщение']")))
                    msg_btn.click()
                    self._log_msg("Opened DM window.", "info")
                    chat_box = wait.until(EC.presence_of_element_located((By.XPATH,
                        "//div[@role='textbox' and contains(@aria-label,'Message')]")))
                    chat_box.send_keys(message)
                    chat_box.send_keys(Keys.ENTER)
                    self._log_msg("🏆  DM sent!", "success")
                    success = True
                except Exception as e:
                    self._log_msg(f"DM unavailable ({type(e).__name__}).", "warn")

            if not success:
                self._log_msg("❌  All selected methods failed.", "error")

        except Exception as e:
            self._log_msg(f"Unexpected error: {e}", "error")
        finally:
            self._finish()

    # ── Discussion / comment helper ───────────────────────────────────────────

    def _post_to_discussion(self, driver, business_name, message, page_url) -> bool:
        """
        Navigate to the business page and comment on its latest post.
        Tries two strategies:
          A — find an already-visible textbox labelled 'comment'
          B — click a Comment button, then type into the active element
        Returns True on success.
        """
        wait = WebDriverWait(driver, 10)

        # Navigate — try the broader search link first, fall back to known page_url
        try:
            self._log_msg("Searching for page via broad link detection…", "step")
            query = urllib.parse.quote(business_name)
            driver.get(f"https://www.facebook.com/search/top?q={query}")
            xpath_link = (
                "//a[contains(@href, '://facebook.com') and "
                "(contains(@role, 'link') or contains(@role, 'presentation'))]"
            )
            first = wait.until(EC.element_to_be_clickable((By.XPATH, xpath_link)))
            found_url = first.get_attribute("href").split("?")[0]
            self._log_msg(f"Navigating to: {found_url}", "info")
            driver.get(found_url)
            time.sleep(5)
        except Exception as e:
            self._log_msg(
                f"Broad search failed ({type(e).__name__}), using known page URL.", "warn")
            driver.get(page_url)
            time.sleep(5)

        # Scroll to reveal lazy-loaded posts
        self._log_msg("Scrolling to find latest post…", "info")
        for _ in range(3):
            driver.execute_script("window.scrollBy(0, 600);")
            time.sleep(2)

        # Strategy A: directly visible comment textbox
        try:
            self._log_msg("Strategy A — hunting for comment textbox…", "step")
            box_xpath = (
                "//div[@role='textbox']"
                "[contains(@aria-label, 'comment') or contains(@aria-label, 'ответить')]"
            )
            comment_box = wait.until(EC.element_to_be_clickable((By.XPATH, box_xpath)))
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", comment_box)
            time.sleep(1)
            comment_box.click()
            comment_box.send_keys(message)
            comment_box.send_keys(Keys.ENTER)
            self._log_msg("🏆  Comment posted via Strategy A!", "success")
            return True
        except Exception as e:
            self._log_msg(f"Strategy A failed ({type(e).__name__}).", "warn")

        # Strategy B: click Comment button → type into active element
        try:
            self._log_msg("Strategy B — clicking Comment button…", "step")
            btn_xpath = (
                "//div[@aria-label='Leave a comment'] | "
                "//span[text()='Comment' or text()='Комментировать']"
            )
            comment_btn = wait.until(EC.element_to_be_clickable((By.XPATH, btn_xpath)))
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", comment_btn)
            time.sleep(1)
            comment_btn.click()
            time.sleep(2)
            active_box = driver.switch_to.active_element
            active_box.send_keys(message)
            active_box.send_keys(Keys.ENTER)
            self._log_msg("🏆  Comment posted via Strategy B!", "success")
            return True
        except Exception as e:
            self._log_msg(f"Strategy B failed ({type(e).__name__}).", "warn")

        return False

    # ── Selenium check ────────────────────────────────────────────────────────

    def _check_selenium(self):
        if not SELENIUM_OK:
            self._log_msg("⚠  selenium not installed.  Run:  pip install selenium", "warn")
        else:
            self._log_msg("selenium ready ✓", "success")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = FBPosterApp()
    app.mainloop()
