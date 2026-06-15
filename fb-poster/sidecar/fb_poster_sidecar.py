#!/usr/bin/env python3
"""
fb_poster_sidecar.py
--------------------
Called by the Tauri shell plugin as a sidecar binary.

Usage:
  fb_poster_sidecar load_complaints <db_path>
      → prints JSON array of complaint objects to stdout

  fb_poster_sidecar post <business_name> <message> <methods>
      methods = "review_flag,comment_flag,dm_flag"  e.g. "1,1,0"
      → streams LOG:<tag>:<text> lines to stdout while posting
"""

import sys
import json
import re
import sqlite3
import time
import urllib.parse

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(text: str, tag: str = "info"):
    """Emit a structured log line that the Rust layer forwards to the frontend."""
    print(f"LOG:{tag}:{text}", flush=True)


def extract_company(d: dict) -> str:
    raw     = d.get("complaint_raw", "")
    subject = d.get("subject", "")
    patterns = [
        r'\b(American Airlines|Southwest Airlines|United Airlines|Delta Airlines?'
        r'|CareFirst[\w\s]*|REI|IKEA|Home Depot|Lowe\'?s|Amazon|Walmart'
        r'|Robert Klein|David Chen|Mark Stevens)\b',
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return subject.strip().title() if subject else "Unknown Business"


# ── Command: load_complaints ──────────────────────────────────────────────────

def cmd_load_complaints(db_path: str):
    rows = []
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            "SELECT complaint_id, complaint_json FROM complaints ORDER BY rowid DESC")
        for cid, cjson in cur.fetchall():
            d = json.loads(cjson)
            rows.append({
                "complaint_id":           cid,
                "subject":                d.get("subject", ""),
                "company":                extract_company(d),
                "status":                 d.get("current_status_summary", ""),
                "complaint_raw":          d.get("complaint_raw", ""),
                "complaint_professional": d.get("complaint_professional", ""),
            })
        con.close()
    except Exception as e:
        print(json.dumps({"error": str(e)}), flush=True)
        return
    print(json.dumps(rows), flush=True)


# ── Command: post ─────────────────────────────────────────────────────────────

def cmd_post(business_name: str, message: str, methods_str: str):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        log("selenium not installed. Run: pip install selenium", "error")
        return

    flags      = methods_str.split(",")
    use_review  = len(flags) > 0 and flags[0] == "1"
    use_comment = len(flags) > 1 and flags[1] == "1"
    use_dm      = len(flags) > 2 and flags[2] == "1"

    # Connect to Chrome
    log(f"Connecting to Chrome on port 9222…", "step")
    try:
        opts = Options()
        opts.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
        driver = webdriver.Chrome(options=opts)
        log("Connected to Chrome ✓", "success")
    except Exception as e:
        log(f"Could not connect to Chrome: {e}", "error")
        log("Launch Chrome first and log into Facebook.", "warn")
        return

    wait    = WebDriverWait(driver, 20)
    success = False

    try:
        # Search
        log(f"Searching Facebook for: {business_name}", "step")
        driver.get(
            f"https://www.facebook.com/search/top?q={urllib.parse.quote(business_name)}")
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, "//div[@role='article']")))
            log("Search results loaded ✓", "success")
        except Exception:
            log("Search results didn't render — are you logged in?", "error")
            return

        # Page URL
        log("Finding business page URL…", "step")
        try:
            link_xpath = (
                "//div[@role='article']//a[contains(@href, '/pages/')] | "
                "//div[@role='article']//a[@role='link'] | "
                f"//a[contains(@aria-label, '{business_name}')]"
            )
            first    = wait.until(EC.element_to_be_clickable((By.XPATH, link_xpath)))
            page_url = first.get_attribute("href").split("?")[0]
            if not page_url.endswith("/"):
                page_url += "/"
            log(f"Page: {page_url}", "success")
        except Exception:
            log("Business page not found in results.", "error")
            return

        # ── Option 1: Review ──────────────────────────────────────────────────
        if use_review and not success:
            log("Option 1 — Reviews tab…", "step")
            driver.get(f"{page_url}reviews/")
            time.sleep(4)
            try:
                yes_btn = wait.until(EC.element_to_be_clickable((By.XPATH,
                    "//span[text()='Yes' or text()='Да']/ancestor::a")))
                yes_btn.click()
                log("Clicked 'Yes' recommendation.", "info")
                box = wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//div[@role='textbox']")))
                box.send_keys(message)
                driver.find_element(By.XPATH,
                    "//div[@aria-label='Post' or @aria-label='Опубликовать']").click()
                log("🏆  Review posted!", "success")
                success = True
            except Exception as e:
                log(f"Review unavailable ({type(e).__name__}).", "warn")

        # ── Option 2: Comment / discussion ───────────────────────────────────
        if use_comment and not success:
            log("Option 2 — comment on latest post…", "step")
            success = _post_to_discussion(
                driver, business_name, message, page_url, log)

        # ── Option 3: Direct Message ──────────────────────────────────────────
        if use_dm and not success:
            log("Option 3 — Direct Message…", "step")
            driver.get(page_url)
            try:
                msg_btn = wait.until(EC.element_to_be_clickable((By.XPATH,
                    "//div[@aria-label='Message' or @aria-label='Сообщение']")))
                msg_btn.click()
                log("Opened DM window.", "info")
                chat_box = wait.until(EC.presence_of_element_located((By.XPATH,
                    "//div[@role='textbox' and contains(@aria-label,'Message')]")))
                chat_box.send_keys(message)
                chat_box.send_keys(Keys.ENTER)
                log("🏆  DM sent!", "success")
                success = True
            except Exception as e:
                log(f"DM unavailable ({type(e).__name__}).", "warn")

        if not success:
            log("❌  All selected methods failed.", "error")

    except Exception as e:
        log(f"Unexpected error: {e}", "error")
    finally:
        log("── Session finished ──", "dim")


def _post_to_discussion(driver, business_name, message, page_url, log_fn):
    """Strategy A + B comment posting. Returns True on success."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    wait = WebDriverWait(driver, 10)

    # Navigate via broad search
    try:
        log_fn("Searching for page via broad link detection…", "step")
        query = urllib.parse.quote(business_name)
        driver.get(f"https://www.facebook.com/search/top?q={query}")
        xpath_link = (
            "//a[contains(@href, '://facebook.com') and "
            "(contains(@role, 'link') or contains(@role, 'presentation'))]"
        )
        first     = wait.until(EC.element_to_be_clickable((By.XPATH, xpath_link)))
        found_url = first.get_attribute("href").split("?")[0]
        log_fn(f"Navigating to: {found_url}", "info")
        driver.get(found_url)
        time.sleep(5)
    except Exception as e:
        log_fn(f"Broad search failed ({type(e).__name__}), using known URL.", "warn")
        driver.get(page_url)
        time.sleep(5)

    # Scroll to reveal lazy-loaded posts
    log_fn("Scrolling to find latest post…", "info")
    for _ in range(3):
        driver.execute_script("window.scrollBy(0, 600);")
        time.sleep(2)

    # Strategy A: directly visible textbox
    try:
        log_fn("Strategy A — hunting for comment textbox…", "step")
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
        log_fn("🏆  Comment posted via Strategy A!", "success")
        return True
    except Exception as e:
        log_fn(f"Strategy A failed ({type(e).__name__}).", "warn")

    # Strategy B: click button → active element
    try:
        log_fn("Strategy B — clicking Comment button…", "step")
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
        log_fn("🏆  Comment posted via Strategy B!", "success")
        return True
    except Exception as e:
        log_fn(f"Strategy B failed ({type(e).__name__}).", "warn")

    return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: fb_poster_sidecar <command> [args...]", flush=True)
        sys.exit(1)

    command = sys.argv[1]

    if command == "load_complaints":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "db_path required"}), flush=True)
        else:
            cmd_load_complaints(sys.argv[2])

    elif command == "post":
        if len(sys.argv) < 5:
            print("LOG:error:Usage: post <business> <message> <methods>", flush=True)
        else:
            cmd_post(sys.argv[2], sys.argv[3], sys.argv[4])

    else:
        print(f"LOG:error:Unknown command: {command}", flush=True)
        sys.exit(1)
