import time
import random
import urllib.parse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def get_driver():
    options = Options()
    options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
    try:
        return webdriver.Chrome(options=options)
    except Exception as e:
        print(f"❌ Connection Error: {e}")
        return None


def post_to_discussion(driver, business_name, message):
    wait = WebDriverWait(driver, 10)

    # --- STEP 1: Search ---
    try:
        print(f"🔍 Searching for: {business_name}")
        query = urllib.parse.quote(business_name)
        driver.get(f"https://www.facebook.com/search/top?q={query}")

        # Broaden search link detection
        xpath_link = "//a[contains(@href, '://facebook.com') and (contains(@role, 'link') or contains(@role, 'presentation'))]"
        first_result = wait.until(EC.element_to_be_clickable((By.XPATH, xpath_link)))
        page_url = first_result.get_attribute("href").split('?')[0]

        print(f"📍 Navigating to: {page_url}")
        driver.get(page_url)
        time.sleep(5)
    except Exception as e:
        print(f"⚠️ Search failed, staying on current page. Reason: {type(e).__name__}")

    # --- STEP 2: The Scroll-to-Find Loop ---
    # Facebook won't show comment boxes until you scroll a bit
    print("🖱️ Scrolling to find latest post...")
    for _ in range(3):
        driver.execute_script("window.scrollBy(0, 600);")
        time.sleep(2)

    # --- STEP 3: Attempt Strategy A (Direct Box) ---
    try:
        print("💡 Strategy A: Hunting for textbox...")
        box_xpath = "//div[@role='textbox'][contains(@aria-label, 'comment') or contains(@aria-label, 'ответить')]"
        comment_box = wait.until(EC.element_to_be_clickable((By.XPATH, box_xpath)))

        # Fixed Scroll Syntax
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", comment_box)
        time.sleep(1)

        comment_box.click()
        comment_box.send_keys(message)
        comment_box.send_keys(Keys.ENTER)
        print("🏆 Success: Commented via Strategy A.")
        return True
    except Exception:
        print("⚠️ Strategy A failed.")

    # --- STEP 4: Attempt Strategy B (Button Click) ---
    try:
        print("💡 Strategy B: Hunting for comment button...")
        btn_xpath = "//div[@aria-label='Leave a comment'] | //span[text()='Comment' or text()='Комментировать']"
        comment_btn = wait.until(EC.element_to_be_clickable((By.XPATH, btn_xpath)))

        # Fixed Scroll Syntax
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", comment_btn)
        time.sleep(1)

        comment_btn.click()
        time.sleep(2)

        # Type into active element
        active_box = driver.switch_to.active_element
        active_box.send_keys(message)
        active_box.send_keys(Keys.ENTER)
        print("🏆 Success: Commented via Strategy B.")
        return True
    except Exception:
        print("❌ Strategy B failed.")

    return False


def main():
    driver = get_driver()
    if driver:
        # Use a real business name and your complaint text
        post_to_discussion(driver, "Southwest Airlines", "Testing automated support outreach.")


if __name__ == "__main__":
    main()
