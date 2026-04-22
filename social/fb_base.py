from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import urllib.parse
import time
import random


def get_driver():
    options = Options()
    # Connect to the manual Chrome session you opened via terminal
    options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")

    # Standard webdriver is used to attach to an existing session
    driver = webdriver.Chrome(options=options)
    return driver


def smart_fb_writer(driver, business_name, message):
    wait = WebDriverWait(driver, 20)
    success = False

    try:
        # Since we are already logged in via the manual session, go straight to search
        print(f"Searching for: {business_name}")
        query = urllib.parse.quote(business_name)
        search_url = f"https://www.facebook.com/search/top?q={query}"

        driver.get(search_url)

        # Wait for search results
        try:
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((
                By.XPATH, "//div[@role='article']"
            )))
            print("✅ Search results rendered.")
        except:
            print("⚠️ Results didn't render. Check if the browser window is visible.")
            return

        # 2. Extract the Page URL
        try:
            page_link_xpath = (
                    "//div[@role='article']//a[contains(@href, '/pages/')] | "
                    "//div[@role='article']//a[@role='link'] | "
                    "//a[contains(@aria-label, '" + business_name + "')]"
            )
            first_result = wait.until(EC.element_to_be_clickable((By.XPATH, page_link_xpath)))
            page_url = first_result.get_attribute("href").split('?')[0]

            if not page_url.endswith('/'):
                page_url += '/'
            print(f"✅ Target Page: {page_url}")
        except Exception:
            print("❌ Search result link not found.")
            return

        # 3. Execution Logic
        # --- ATTEMPT 1: Review ---
        if not success:
            print("Trying to leave a Review...")
            driver.get(f"{page_url}reviews/")
            time.sleep(4)
            try:
                # Support for English and Russian buttons
                yes_xpath = "//span/ancestor::a | //div[@role='button']//span"
                yes_btn = wait.until(EC.element_to_be_clickable((By.XPATH, yes_xpath)))
                yes_btn.click()

                box = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox']")))
                box.send_keys(message)

                post_xpath = "//div[@aria-label='Post' or @aria-label='Опубликовать']"
                driver.find_element(By.XPATH, post_xpath).click()
                print("🏆 Success: Review Posted!")
                success = True
            except:
                print("Review tab is locked or disabled.")

        # --- ATTEMPT 2: Direct Message (Fallback) ---
        if not success:
            print("Trying Direct Message...")
            driver.get(page_url)
            try:
                msg_xpath = "//div[@aria-label='Message' or @aria-label='Сообщение']"
                msg_btn = wait.until(EC.element_to_be_clickable((By.XPATH, msg_xpath)))
                msg_btn.click()

                chat_box = wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//div[@role='textbox' and contains(@aria-label, 'Message')]")))
                chat_box.send_keys(message)
                chat_box.send_keys(Keys.ENTER)
                print("🏆 Success: DM sent to Support!")
                success = True
            except:
                print("Message button not found.")

    except Exception as e:
        print(f"Error during execution: {e}")


# Main execution
if __name__ == "__main__":
    # Ensure you do NOT use driver.quit() if you want the human session to stay open
    my_driver = get_driver()
    smart_fb_writer(my_driver, "Southwest Airlines", "Great service on my flight today!")
