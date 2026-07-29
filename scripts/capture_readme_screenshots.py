"""Capture README screenshots from a running Flask instance."""

from __future__ import annotations

import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "http://127.0.0.1:5000/"
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def createDriver() -> webdriver.Edge:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--disable-gpu")
    return webdriver.Edge(options=options)


def waitForResults(driver: webdriver.Edge) -> None:
    WebDriverWait(driver, 15).until(
        ec.presence_of_element_located((By.CSS_SELECTOR, ".kpi .value"))
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    driver = createDriver()
    try:
        driver.get(BASE_URL)
        time.sleep(0.5)
        driver.save_screenshot(str(OUT_DIR / "01-input-form.png"))

        driver.find_element(By.CSS_SELECTOR, "button[value='design']").click()
        waitForResults(driver)
        time.sleep(0.75)
        driver.save_screenshot(str(OUT_DIR / "02-design-results.png"))

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5)
        driver.save_screenshot(str(OUT_DIR / "03-bom-and-cables.png"))

        driver.find_element(By.CSS_SELECTOR, "button[value='compare']").click()
        WebDriverWait(driver, 15).until(
            ec.presence_of_element_located((By.CSS_SELECTOR, ".compare-modal.is-open"))
        )
        time.sleep(0.5)
        driver.save_screenshot(str(OUT_DIR / "04-compare-plans-modal.png"))

        driver.find_element(By.CSS_SELECTOR, ".compare-modal-close").click()
        time.sleep(0.3)

        driver.find_element(By.ID, "rail-preview-link").click()
        WebDriverWait(driver, 10).until(
            ec.presence_of_element_located((By.CSS_SELECTOR, ".rail-modal.is-open"))
        )
        time.sleep(0.5)
        driver.save_screenshot(str(OUT_DIR / "05-rail-design-modal.png"))

        driver.find_element(By.CSS_SELECTOR, ".rail-modal-close").click()
        time.sleep(0.3)

        driver.execute_script("window.scrollTo(0, 0);")
        driver.find_element(By.ID, "diagram_zoom_fit").click()
        time.sleep(0.5)
        driver.save_screenshot(str(OUT_DIR / "06-topology-fit-view.png"))
    finally:
        driver.quit()

    print(f"Screenshots saved to {OUT_DIR}")


if __name__ == "__main__":
    main()
