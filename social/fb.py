from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import urllib.parse
import time

# --- Configuration ---
EMAIL = "??"
PASSWORD = "??"


options = Options()
#options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
# --- ADD THESE THREE LINES ---
options.add_argument("--proxy-server='direct://'")
options.add_argument("--proxy-bypass-list=*")
options.add_argument("--disable-gpu")
# -----------------------------
driver = webdriver.Chrome(options=options)

import socket
from selenium.common.exceptions import WebDriverException


def check_internet():
    try:
        # Check if we can actually reach Facebook's servers
        socket.create_connection(("://facebook.com", 80))
        return True
    except OSError:
        return False


def fb_login():
    if not check_internet():
        print("❌ Network Error: Cannot reach Facebook. Check your internet/VPN.")
        return

    print("Navigating to Facebook...")
    try:
        driver.get("https://facebook.com")
        wait = WebDriverWait(driver, 15)

        # 1. Handle Cookie Consent (Crucial for page visibility)
        try:
            cookie_btn = wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(., 'Allow')] | //div[@aria-label='Allow all cookies']")))
            cookie_btn.click()
        except:
            pass

        # 2. Enter Credentials
        email_field = wait.until(EC.presence_of_element_located((By.ID, "email")))
        email_field.send_keys(EMAIL)

        driver.find_element(By.ID, "pass").send_keys(PASSWORD)

        # 3. Flexible Login Button Search
        # Uses multiple attributes in case the ID or Name is blocked
        login_btn = wait.until(EC.element_to_be_clickable((
            By.XPATH, "//button[@name='login'] | //button[@type='submit'] | //button[contains(., 'Log In')]"
        )))
        login_btn.click()

        print("Login clicked. Waiting for load...")
        time.sleep(5)
    except WebDriverException as e:
        print(f"Browser failed to load: {e}")


import undetected_chromedriver as uc
import time


def smart_fb_writer(business_name, message):
    # 1. Specialized Driver Setup
    options = uc.ChromeOptions()
    # If it still fails, comment out the headless line to see what's happening
    options.add_argument("--headless")

    # This driver automatically handles version matching
    driver = uc.Chrome(options=options)

    try:
        print("Navigating to Facebook...")
        driver.get("https://www.facebook.com")
        time.sleep(5)  # Allow page to resolve

        # Check if we reached the page
        if "facebook.com" in driver.current_url:
            print("✅ Connection Successful!")
            # [Insert your login and posting logic here]
        else:
            print("❌ Still blocked. Trying mobile fallback...")
            driver.get("https://m.facebook.com")
        # 1. Search via Direct URL (Prevents TimeoutException on Search Box)
        print(f"Searching for: {business_name}")
        query = urllib.parse.quote(business_name)
        driver.get(f"https://facebook.com{query}")

        # Click the first result
        wait = WebDriverWait(driver, 15)
        first_result = wait.until(EC.element_to_be_clickable((
            By.XPATH,
            "(//div[@role='article']//a[@role='presentation'])[1] | (//a[contains(@href, 'facebook.com/')])[15]"
        )))
        page_url = first_result.get_attribute("href").split('?')[0]
        if not page_url.endswith('/'): page_url += '/'
        print(f"Targeting Page: {page_url}")

        # --- OPTION A: Review ---
        if not success:
            print("Trying Review...")
            driver.get(f"{page_url}reviews/")
            time.sleep(3)
            if "reviews" in driver.current_url:
                try:
                    yes_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[text()='Yes']/ancestor::a")))
                    yes_btn.click()
                    box = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox']")))
                    box.send_keys(message)
                    driver.find_element(By.XPATH, "//div[@aria-label='Post']").click()
                    print("✅ Success: Review posted.")
                    success = True
                except:
                    print("Review unavailable.")

        # --- OPTION B: Visitor Post ---
        if not success:
            print("Trying Visitor Post...")
            driver.get(f"{page_url}mentions/")
            time.sleep(3)
            try:
                trigger = wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Write something')]")))
                trigger.click()
                box = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox']")))
                box.send_keys(message)
                driver.find_element(By.XPATH, "//div[@aria-label='Post']").click()
                print("✅ Success: Visitor Post created.")
                success = True
            except:
                print("Visitor posting disabled.")

        # --- OPTION C: Direct Message ---
        if not success:
            print("Trying Direct Message...")
            driver.get(page_url)
            try:
                msg_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[@aria-label='Message']")))
                msg_btn.click()
                msg_box = wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//div[@role='textbox' and contains(@aria-label, 'Message')]")))
                msg_box.send_keys(message)
                msg_box.send_keys(Keys.ENTER)
                print("✅ Success: DM sent to support.")
                success = True
            except:
                print("Message button not found.")

    except Exception as e:
        print(f"Fatal error during execution: {e}")
    finally:
        driver.quit()


# Execute
fb_login()
smart_fb_writer("Southwest Airlines", "I had an incredible experience with the crew on my flight today!")
