from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from config import APP_NAME, SELECTORS, SITES

if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "executable")).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

LOG_FILE = BASE_DIR / "bot.log"

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
except ImportError:  # pragma: no cover - depends on runtime environment
    BrowserContext = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]
    async_playwright = None  # type: ignore[assignment]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

AOD_DETAIL_PAGE_ASSET_URL = "https://m.media-amazon.com/images/I/61J36RU2hyL.js?AUIClients/"
AOD_TRIGGER_SELECTOR = "[data-action='show-all-offers-display']"


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def extract_asin_from_url(url: str) -> str | None:
    match = re.search(r"/(?:dp|gp/product|gp/aw/d|gp/offer-listing)/([A-Z0-9]{10})", url, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).upper()


@dataclass(slots=True)
class OfferCandidate:
    source: str
    title: str
    item_price: float
    shipping_price: float
    effective_total: float
    action_path: str
    action_name: str
    shipping_known: bool = False
    shipping_is_free: bool = False
    shipping_text: str | None = None
    offer_rank: int | None = None
    action_label: str | None = None


@dataclass(slots=True)
class ShippingInfo:
    price: float
    is_known: bool
    is_free: bool
    text: str | None = None


class BrowserConnectionLostError(RuntimeError):
    pass


class SniperBot:
    def __init__(
        self,
        log_callback=None,
        status_callback=None,
        confirm_callback=None,
    ) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.running = False
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.confirm_callback = confirm_callback
        self.refresh_interval = 2.0
        self.email_config: dict[str, Any] = {}
        self.port = 9226
        self._browser = None
        self._context: BrowserContext | None = None
        self._stop_event = threading.Event()
        self._site_key = "amazon_us"
        self._connected_over_cdp = False
        self._reconnect_requested = threading.Event()
        self._purchase_lock: asyncio.Lock | None = None

    def set_email_config(self, email_config: dict[str, Any]) -> None:
        self.email_config = dict(email_config)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        logger.info(message)
        if self.log_callback:
            self.log_callback(line)

    def update_status(self, key: str, status: str) -> None:
        if self.status_callback:
            self.status_callback(key, status)

    def send_notification(self, subject: str, body: str) -> None:
        if not self.email_config.get("email"):
            return

        def _send() -> None:
            try:
                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"] = self.email_config["email"]
                msg["To"] = self.email_config.get("target_email", self.email_config["email"])

                smtp_server = self.email_config.get("smtp_server", "")
                smtp_port = int(self.email_config.get("smtp_port") or 587)
                password = self.email_config.get("password", "")

                if smtp_port == 465:
                    with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                        server.login(self.email_config["email"], password)
                        server.send_message(msg)
                else:
                    with smtplib.SMTP(smtp_server, smtp_port) as server:
                        server.starttls()
                        server.login(self.email_config["email"], password)
                        server.send_message(msg)

                self.log(f"[EMAIL] Notification sent: {subject}")
            except Exception as exc:
                self.log(f"[EMAIL] Failed to send email: {exc}")

        threading.Thread(target=_send, daemon=True).start()

    def start(self, tasks: list[dict[str, Any]], site_key: str = "amazon_us") -> None:
        if self.running or not tasks:
            return

        self.tasks = list(tasks)
        self._site_key = site_key
        self.running = True
        self._stop_event = threading.Event()
        self.log("Initializing Amazon Sniper Engine...")
        threading.Thread(
            target=lambda: asyncio.run(self._run_main()),
            daemon=True,
        ).start()

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()
        self.log("Bot stopping...")

    async def clear_cart_on_startup(self) -> None:
        if not self._context:
            return

        self.log("[CART] Checking cart for existing items on startup...")
        page = await self._context.new_page()
        try:
            await page.goto(SITES[self._site_key]["cart_url"], wait_until="commit", timeout=30000)
            delete_selectors = [
                "input[value='Delete']",
                "input[data-action='delete']",
                ".sc-action-delete input",
                "input[name^='submit.delete']",
            ]

            for _ in range(20):
                if not self.running or self._stop_event.is_set():
                    break

                item = None
                delete_btn = None
                for selector in delete_selectors:
                    delete_btn = await self.get_visible_element(page, selector)
                    if delete_btn:
                        break

                if not delete_btn:
                    break

                try:
                    item = await delete_btn.evaluate_handle("el => el.closest('.sc-list-item')")
                except Exception:
                    item = None

                item_title = "Item"
                if item:
                    try:
                        title_elem = await item.as_element().query_selector(".sc-product-title")
                        if title_elem:
                            item_title = (await title_elem.inner_text()).strip() or item_title
                    except Exception:
                        pass

                self.log(f"[CART] Removing existing item from cart: '{item_title[:60]}...'")
                await delete_btn.click()
                await asyncio.sleep(1.0)
        except Exception as exc:
            self.log(f"[CART] Warning during cart clearing on startup: {exc}")
        finally:
            self.log("[CART] Cart check/clearing complete.")
            await page.close()

    def _debug_browser_ready(self) -> bool:
        if not is_port_open(self.port):
            return False

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as response:
                targets = json.load(response)
            return isinstance(targets, list) and len(targets) > 0
        except Exception:
            return False

    async def _wait_for_debug_browser_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await asyncio.to_thread(self._debug_browser_ready):
                return True
            await asyncio.sleep(0.5)
        return False

    def _terminate_debug_browser(self) -> None:
        if sys.platform.startswith("win"):
            try:
                output = subprocess.check_output(
                    ["netstat", "-ano"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                return

            pids = set()
            needle = f":{self.port}"
            for line in output.splitlines():
                if needle not in line or "LISTENING" not in line.upper():
                    continue
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    if pid.isdigit():
                        pids.add(pid)

            for pid in pids:
                subprocess.run(
                    ["taskkill", "/PID", pid, "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            return

        try:
            output = subprocess.check_output(
                ["lsof", "-ti", f"tcp:{self.port}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return

        for pid in {line.strip() for line in output.splitlines() if line.strip().isdigit()}:
            subprocess.run(
                ["kill", "-TERM", pid],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    async def _connect_browser_context(self, playwright, launch_browser) -> BrowserContext:
        last_exc: Exception | None = None

        for attempt in range(2):
            if attempt == 0:
                if not is_port_open(self.port):
                    self.log(f"Starting browser on port {self.port}...")
                    launch_browser()
            else:
                self.log("[BROWSER] Restarting debug browser after connection failure...")
                await asyncio.to_thread(self._terminate_debug_browser)
                await asyncio.sleep(1.5)
                launch_browser()

            if not await self._wait_for_debug_browser_ready():
                last_exc = RuntimeError(
                    f"Chrome debug browser did not become ready on port {self.port}."
                )
                self.log(f"[BROWSER] {last_exc}")
                continue

            try:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.port}"
                )
            except Exception as exc:
                last_exc = exc
                self.log(f"[BROWSER] CDP connect attempt {attempt + 1} failed: {exc}")
                continue

            if not browser.contexts:
                try:
                    await browser.close()
                except Exception:
                    pass
                last_exc = RuntimeError(
                    "Connected Chrome debug browser has no available contexts."
                )
                self.log(f"[BROWSER] {last_exc}")
                continue

            self._browser = browser
            self._connected_over_cdp = True
            return browser.contexts[0]

        if last_exc:
            raise last_exc
        raise RuntimeError("Failed to connect to Chrome debug browser.")

    def _is_connection_lost_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        markers = (
            "connection closed while reading from the driver",
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "connection closed",
            "connection disposed",
        )
        return any(marker in message for marker in markers)

    async def _close_browser_session(self) -> None:
        if self._context:
            try:
                await self._context.unroute("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf}")
            except Exception:
                pass
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser and self._connected_over_cdp:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._connected_over_cdp = False

    async def _run_main(self) -> None:
        if async_playwright is None:
            self.log("[FATAL] Fatal Error: Playwright is not installed.")
            self.running = False
            return

        try:
            from open_profile import ensure_user_data_dir, find_chrome, open_chrome_with_profile
        except Exception as exc:  # pragma: no cover - import path issue
            self.log(f"[FATAL] Fatal Error: {exc}")
            self.running = False
            return

        try:
            async with async_playwright() as playwright:
                profile_path = ensure_user_data_dir().resolve()
                profile_path.mkdir(parents=True, exist_ok=True)

                chrome = find_chrome()
                if not chrome:
                    raise RuntimeError("Google Chrome was not found.")

                while self.running and not self._stop_event.is_set():
                    self._browser = None
                    self._context = None
                    self._connected_over_cdp = False
                    self._reconnect_requested.clear()
                    self._purchase_lock = asyncio.Lock()
                    task_futures: list[asyncio.Task[Any]] = []
                    should_reconnect = False

                    try:
                        self._context = await self._connect_browser_context(
                            playwright,
                            lambda: open_chrome_with_profile("https://www.amazon.com"),
                        )

                        async def _block_route(route) -> None:
                            await route.abort()

                        await self._context.route(
                            "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf}",
                            _block_route,
                        )
                        self.log("Connected to browser. Launching tasks...")

                        await self.clear_cart_on_startup()

                        for task in self.tasks:
                            if task.get("_stopped_after_purchase"):
                                continue
                            task_page = await self._context.new_page()
                            await task_page.add_init_script(
                                "Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});"
                            )
                            await task_page.add_init_script(
                                "Object.defineProperty(document, 'hidden', {get: () => false});"
                            )
                            task_futures.append(asyncio.create_task(self.run_task_loop(task_page, task)))

                        await asyncio.gather(*task_futures)
                        self.running = False
                    except BrowserConnectionLostError as exc:
                        if self.running and not self._stop_event.is_set():
                            should_reconnect = True
                            self.log(f"[BROWSER] {exc} Reconnecting to Chrome...")
                        else:
                            self.running = False
                    except Exception as exc:
                        if self.running and not self._stop_event.is_set() and self._is_connection_lost_error(exc):
                            should_reconnect = True
                            self.log(f"[BROWSER] Browser connection dropped during setup: {exc}")
                        else:
                            self.log(f"[FATAL] Fatal Error: {exc}")
                            self.running = False
                    finally:
                        for future in task_futures:
                            if not future.done():
                                future.cancel()
                        if task_futures:
                            await asyncio.gather(*task_futures, return_exceptions=True)
                        await self._close_browser_session()

                    if not should_reconnect:
                        break

                    for task in self.tasks:
                        if not task.get("_stopped_after_purchase"):
                            self.update_status(str(task["key"]), "Reconnecting...")
                    await asyncio.sleep(2)
        except Exception as exc:
            self.log(f"[FATAL] Fatal Error: {exc}")
        finally:
            self.running = False
            await self._close_browser_session()

    async def run_task_loop(self, page: Page, task: dict[str, Any]) -> None:
        key = str(task["key"])
        try:
            while self.running and not self._stop_event.is_set():
                if task.get("_stopped_after_purchase"):
                    self.update_status(key, "Success - Monitoring Stopped")
                    break
                self.update_status(key, "Running...")
                try:
                    if str(task.get("type", "URL")).upper() == "URL":
                        await self.process_url_task(page, task, key)
                    else:
                        await self.process_keyword_task(page, task, key)
                except Exception as exc:
                    if self._is_connection_lost_error(exc):
                        self.update_status(key, "Reconnecting...")
                        if not self.running or self._stop_event.is_set():
                            break
                        if not self._reconnect_requested.is_set():
                            self._reconnect_requested.set()
                            self.log(
                                "[BROWSER] Shared browser connection was lost; restarting monitoring session..."
                            )
                        raise BrowserConnectionLostError(str(exc)) from exc
                    self.log(f"[TASK] Task {key} error: {exc}")
                    self.update_status(key, "Error - Retrying")

                if task.get("_stopped_after_purchase"):
                    self.update_status(key, "Success - Monitoring Stopped")
                    break

                for _ in range(max(1, int(self.refresh_interval))):
                    if not self.running or self._stop_event.is_set():
                        break
                    await asyncio.sleep(1)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def process_url_task(self, page: Page, task: dict[str, Any], key: str) -> None:
        url = str(task["query"])
        expected_asin = extract_asin_from_url(url)
        self.update_status(key, "Monitoring...")

        if not url.startswith(("http://", "https://")):
            self.log(f"[TASK] Invalid URL format skipped: '{url[:50]}...'")
            self.update_status(key, "Invalid URL")
            return

        try:
            await page.goto(url, wait_until="commit", timeout=30000)
            await self._wait_for_product_ready(page)
            await self._handle_captcha(page, key, "product")
            await self._wait_for_offer_controls(page)
            if not await self._ensure_expected_product(page, key, url, expected_asin):
                self.update_status(key, "Product mismatch")
                return

            oos_text = ""
            oos_elem = await page.query_selector(SELECTORS["oos_indicator"])
            if oos_elem:
                try:
                    oos_text = (await oos_elem.inner_text()).lower()
                except Exception:
                    oos_text = ""

            buying_options = await self.get_visible_element(page, SELECTORS["buying_options"])
            if oos_text and any(
                marker in oos_text
                for marker in ("currently unavailable", "out of stock", "back in stock", "unavailable")
            ):
                if not buying_options:
                    self.update_status(key, "OOS")
                    return

            restriction_selectors = [
                "#exports_desktop_qualifiedBuybox_delivery_message_feature_div",
                "#deliveryMessageFromOOS",
                "[id*='delivery-message']",
                "[id*='DeliveryMessage']",
                ".a-color-error",
            ]
            for selector in restriction_selectors:
                elem = await page.query_selector(selector)
                if not elem:
                    continue
                try:
                    text = (await elem.inner_text()).lower()
                except Exception:
                    continue
                if any(
                    token in text
                    for token in (
                        "cannot be shipped",
                        "not shippable",
                        "cannot ship",
                        "select delivery location",
                    )
                ):
                    self.log(
                        f"[LOCATION] Address/Delivery Restriction on {key}: "
                        "Cannot be shipped to your current Chrome address."
                    )
                    self.update_status(key, "Location Restricted")
                    return

            title_str = await self._read_product_title(page)
            main_candidate = await self._extract_main_offer(page, task, title_str)
            chosen = None
            aod_opened = False
            failures: list[str] = []

            if main_candidate:
                allowed, failure = self._validate_candidate(task, main_candidate)
                if allowed:
                    chosen = main_candidate
                    self.log(
                        f"[PRICE] Main offer qualifies for {key}; using the main product page offer."
                    )
                elif failure:
                    failures.append(failure)
                    if buying_options:
                        self.log(
                            f"[PRICE] Main offer did not qualify for {key}; checking Buying Options..."
                        )

            if not chosen and buying_options:
                aod_candidate = await self._extract_buying_options_offer(
                    page, task, key, title_str, buying_options
                )
                aod_opened = bool(aod_candidate)
                if aod_candidate:
                    allowed, failure = self._validate_candidate(task, aod_candidate)
                    if allowed:
                        chosen = aod_candidate
                    elif failure:
                        failures.append(failure)

            if not chosen:
                if oos_text and not buying_options:
                    self.update_status(key, "OOS")
                elif failures:
                    self.update_status(key, failures[-1])
                else:
                    self.update_status(key, "Button not found")
                return

            purchase_lock = self._purchase_lock or asyncio.Lock()
            async with purchase_lock:
                atc_btn = await self._prepare_selected_offer(
                    page,
                    task,
                    key,
                    url,
                    chosen,
                    aod_opened,
                )
                if not atc_btn:
                    self.update_status(key, "Button not found")
                    return

                self.log(
                    f"[RESTOCK] Restock Detected ({chosen.action_name})! "
                    f"Processing: '{title_str[:60]}' ({key})"
                )
                self.send_notification(
                    "Restock Detected",
                    f"Restock detected for task {key}:\n"
                    f"Action: {chosen.action_name}\n"
                    f"Title: {title_str}\n"
                    f"URL: {task['query']}",
                )
                cart_count_before = await self._read_cart_count(page)
                await self._click_action_element(page, atc_btn, key, chosen.action_name)
                if not await self._confirm_purchase_flow_start(page, key, url, expected_asin, cart_count_before):
                    self.update_status(key, "Add to Cart failed")
                    return
                if chosen.action_path != "buy_now":
                    if not await self._ensure_cart_matches_task(page, key, expected_asin):
                        self.update_status(key, "Cart Conflict")
                        return
                await self.checkout_sequence(page, task, key)
        except Exception as exc:
            self.log(f"Monitor error ({key}): {exc}")

    async def process_keyword_task(self, page: Page, task: dict[str, Any], key: str) -> None:
        keywords = task["query"]
        neg_keywords = [
            k.strip().lower()
            for k in str(task["negative"]).split(",")
            if k.strip()
        ]
        self.update_status(key, "Searching...")

        search_url = f"https://www.amazon.com/s?k={keywords.replace(' ', '+')}&s=date-desc-rank"
        try:
            await page.goto(search_url, wait_until="commit", timeout=30000)
            captcha_sel = (
                "form[action='/errors/validateCaptcha'], "
                "#captchacharacters, #captchacharacters-announcement"
            )
            try:
                await page.wait_for_selector(
                    f'div.s-result-item[data-asin], [data-component-type="s-search-result"], {captcha_sel}',
                    state="attached",
                    timeout=10000,
                )
            except Exception:
                pass

            if await page.query_selector(captcha_sel):
                self.log(f"[CAPTCHA] Captcha on search query {key}! Please solve in browser.")
                self.update_status(key, "CAPTCHA")
                try:
                    await page.wait_for_selector(captcha_sel, state="hidden", timeout=120000)
                except Exception:
                    return

            results = await page.query_selector_all(
                'div.s-result-item[data-asin], [data-component-type="s-search-result"]'
            )
            title_sel = (
                "h2 span, h2 a, a.a-link-normal h2 span, "
                "span.a-size-medium.a-color-base.a-text-normal, "
                "span.a-size-base-plus.a-color-base.a-text-normal"
            )

            titles_found: list[str] = []
            for result in results:
                title_elem = await result.query_selector(title_sel)
                if not title_elem:
                    continue
                title_text = (await title_elem.inner_text()).strip()
                if title_text:
                    titles_found.append(title_text)

            if titles_found:
                self.log(f"[SEARCH] Fetched search result titles ({len(titles_found)} items):")
                for idx, title in enumerate(titles_found[:5], start=1):
                    self.log(f"  [{idx}] {title[:75]}...")
                if len(titles_found) > 5:
                    self.log(f"  ... and {len(titles_found) - 5} more items.")
            else:
                self.log("[SEARCH] No search result titles found with active selectors on this search page.")

            for result in results:
                title_elem = await result.query_selector(title_sel)
                if not title_elem:
                    continue
                title = (await title_elem.inner_text()).lower().strip()
                if not title:
                    continue
                if any(neg in title for neg in neg_keywords):
                    continue

                link_elem = await result.query_selector("h2 a, a.a-link-normal, a.a-text-normal")
                if not link_elem:
                    continue
                href_attr = await link_elem.get_attribute("href")
                if not href_attr:
                    continue

                link = href_attr
                if href_attr.startswith("/"):
                    link = f"https://www.amazon.com{href_attr}"
                self.log(f"[SEARCH] Keyword Match: '{title[:60]}' for {key}")
                task_copy = task.copy()
                task_copy["query"] = link
                await self.process_url_task(page, task_copy, key)
                return

            self.update_status(key, "No matches")
        except Exception as exc:
            self.log(f"Keyword error: {exc}")

    async def get_price(self, page: Page) -> str | None:
        for selector in SELECTORS["price"].split(","):
            selector = selector.strip()
            if not selector:
                continue
            try:
                elem = await page.query_selector(selector)
                if elem:
                    text = (await elem.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return None

    async def set_product_quantity(self, page: Page, target_qty: int, key: str) -> None:
        if target_qty <= 1:
            return

        try:
            quantity_select = await page.query_selector("select[name='quantity']")
            if quantity_select and await quantity_select.is_visible():
                options = await quantity_select.query_selector_all("option")
                values = []
                for option in options:
                    value = await option.get_attribute("value")
                    if value and value.isdigit():
                        values.append(int(value))

                if values:
                    selected = max(min(target_qty, max(values)), min(values))
                    await quantity_select.select_option(str(selected))
                    self.log(
                        f"[QTY] Set quantity dropdown to {selected} (Target: {target_qty}) for {key}"
                    )
                    return
        except Exception as exc:
            self.log(f"[WARNING] Could not set product quantity to {target_qty} for {key}: {exc}")

    async def update_cart_quantity(self, page: Page, target_qty: int) -> None:
        if target_qty <= 1:
            return

        try:
            dropdown = await page.query_selector("select[name='quantity'], select.sc-quantity-selector")
            if dropdown and await dropdown.is_visible():
                await dropdown.select_option(str(target_qty))
                self.log(f"[CART] Selected quantity {target_qty} in classic dropdown.")
                await asyncio.sleep(1)
                return

            stepper = await self.get_visible_element(
                page,
                "[data-csa-c-content-id='quantity-stepper-desktop-content'], fieldset[name='sc-quantity']",
            )
            if not stepper:
                return

            inner = await self.get_visible_element(
                page,
                "span[data-a-selector='inner-value'], input[name='quantityBox']",
            )
            if not inner:
                return

            current_text = ""
            tag_name = ""
            try:
                tag_name = (await inner.evaluate("el => el.tagName.toLowerCase()")) or ""
            except Exception:
                tag_name = ""

            if tag_name == "input":
                current_text = await inner.get_attribute("value") or ""
            else:
                current_text = await inner.inner_text()

            current_text = current_text.strip()
            if not current_text.isdigit():
                return

            current_qty = int(current_text)
            if current_qty == target_qty:
                self.log(f"[CART] Cart quantity is already {target_qty}.")
                return

            inc_btn = await self.get_visible_element(
                page,
                "button[data-a-selector='increment'], button[data-action='a-stepper-increment']",
            )
            dec_btn = await self.get_visible_element(
                page,
                "button[data-a-selector='decrement'], button[data-action='a-stepper-decrement']",
            )
            if not inc_btn or not dec_btn:
                return

            if current_qty < target_qty:
                self.log(
                    f"[CART] Stepping up quantity from {current_qty} to {target_qty} (+{target_qty - current_qty})..."
                )
                for _ in range(target_qty - current_qty):
                    await inc_btn.click()
                    await asyncio.sleep(0.5)
            else:
                self.log(
                    f"[CART] Stepping down quantity from {current_qty} to {target_qty} (-{current_qty - target_qty})..."
                )
                for _ in range(current_qty - target_qty):
                    await dec_btn.click()
                    await asyncio.sleep(0.5)
        except Exception as exc:
            self.log(f"[WARNING] Error updating cart quantity: {exc}")

    async def adjust_checkout_quantity(self, page: Page, target_qty: int) -> None:
        if target_qty <= 1:
            return

        try:
            stepper = await self.get_visible_element(page, "fieldset[name='checkout-quantity-stepper']")
            if not stepper:
                return

            current = await stepper.get_attribute("data-steppervalue")
            if not current or not current.isdigit():
                inner = await self.get_visible_element(page, "span[data-a-selector='inner-value']")
                if inner:
                    current = (await inner.inner_text()).strip()
            if not current or not current.isdigit():
                return

            current_qty = int(current)
            if current_qty == target_qty:
                return

            inc_btn = await self.get_visible_element(
                page,
                "button[data-a-selector='increment'], button[aria-label^='Increase']",
            )
            dec_btn = await self.get_visible_element(
                page,
                "button[data-a-selector='decrement'], button[aria-label^='Decrease']",
            )
            if not inc_btn or not dec_btn:
                return

            self.log(
                f"[CHECKOUT] Adjusting quantity on checkout page from {current_qty} to {target_qty} "
                f"({('+' if target_qty > current_qty else '-')}{abs(target_qty - current_qty)})..."
            )
            click_btn = inc_btn if target_qty > current_qty else dec_btn
            for _ in range(abs(target_qty - current_qty)):
                await click_btn.click()
                await asyncio.sleep(0.5)
        except Exception as exc:
            self.log(f"[WARNING] Could not adjust checkout quantity: {exc}")

    def parse_price(self, price_text: str | None) -> float | None:
        if not price_text:
            return None

        match = re.search(r"(\d[\d\.,]*)", price_text)
        if not match:
            return None

        raw = match.group(1).replace(",", "")
        if raw.count(".") > 1:
            parts = raw.split(".")
            raw = "".join(parts[:-1]) + "." + parts[-1]

        try:
            return float(raw)
        except ValueError:
            return None

    async def get_visible_element(self, page: Page, selector_csv: str):
        for selector in selector_csv.split(","):
            selector = selector.strip()
            if not selector:
                continue
            try:
                for elem in await page.query_selector_all(selector):
                    if await elem.is_visible():
                        return elem
            except Exception:
                continue
        return None

    async def get_visible_elements(self, page: Page, selector_csv: str):
        visible = []
        for selector in selector_csv.split(","):
            selector = selector.strip()
            if not selector:
                continue
            try:
                for elem in await page.query_selector_all(selector):
                    if await elem.is_visible():
                        visible.append(elem)
            except Exception:
                continue
        return visible

    def parse_shipping_info_text(self, shipping_text: str | None) -> ShippingInfo:
        if not shipping_text:
            return ShippingInfo(0.0, False, False, None)

        cleaned = " ".join(shipping_text.split())
        lower = cleaned.lower()
        mentions_shipping = any(
            token in lower for token in ("shipping", "delivery", "import", "charge")
        )

        if "free" in lower and mentions_shipping:
            return ShippingInfo(0.0, True, True, cleaned)

        if mentions_shipping:
            parsed = self.parse_price(cleaned)
            if parsed is not None:
                return ShippingInfo(parsed, True, parsed == 0.0, cleaned)
            return ShippingInfo(0.0, False, False, cleaned)

        return ShippingInfo(0.0, False, False, cleaned)

    async def get_shipping_info(self, page: Page) -> ShippingInfo:
        selectors = [
            "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
            "#deliveryBlockMessage",
            "#mir-layout-DELIVERY_BLOCK",
            "#exports_desktop_qualifiedBuybox_delivery_message_feature_div",
            "#delivery-message",
            "#subtotals-marketplace-table",
            "#subtotals",
            "#sc-subtotals",
            "#sc-subtotal-amount-activecart",
            "#checkout-shipping-address-and-details",
            "#checkout-primary-continue-button-id",
            "#checkout-container",
            "#spc-orders",
            ".aod-shipping-message",
            "#aod-offer-shipping-charge",
        ]
        fallback_text: str | None = None
        for selector in selectors:
            try:
                for elem in await page.query_selector_all(selector):
                    if not await elem.is_visible():
                        continue
                    text = (await elem.inner_text()).strip()
                    info = self.parse_shipping_info_text(text)
                    if info.is_free or info.is_known:
                        return info
                    if info.text and fallback_text is None:
                        fallback_text = info.text
            except Exception:
                continue
        try:
            fallback_lines = await page.evaluate(
                """
                () => {
                    const keywords = ['shipping', 'delivery', 'import', 'charge', 'handling'];
                    const bodyText = (document.body?.innerText || '')
                        .split(/\\n+/)
                        .map(line => line.replace(/\\s+/g, ' ').trim())
                        .filter(Boolean);
                    const lines = [];
                    const seen = new Set();
                    for (const line of bodyText) {
                        const lower = line.toLowerCase();
                        if (!keywords.some(keyword => lower.includes(keyword))) {
                            continue;
                        }
                        if (seen.has(line)) {
                            continue;
                        }
                        seen.add(line);
                        lines.push(line);
                        if (lines.length >= 30) {
                            break;
                        }
                    }
                    return lines;
                }
                """
            )
        except Exception:
            fallback_lines = []

        for text in fallback_lines:
            info = self.parse_shipping_info_text(text)
            if info.is_free or info.is_known:
                return info
            if info.text and fallback_text is None:
                fallback_text = info.text
        return ShippingInfo(0.0, False, False, fallback_text)

    async def get_shipping_price(self, page: Page) -> float:
        return (await self.get_shipping_info(page)).price

    async def get_checkout_total(self, page: Page) -> str | None:
        try:
            return await page.evaluate(
                """
                () => {
                    const terms = Array.from(
                        document.querySelectorAll('.order-summary-line-term, td, span, .a-size-medium')
                    );
                    for (const term of terms) {
                        const text = (term.innerText || '').trim().toLowerCase();
                        if (text === 'order total' || text === 'order total:') {
                            const grid = term.closest('.order-summary-grid, tr, .a-list-item');
                            if (grid) {
                                const amountElem = grid.querySelector(
                                    '[data-shimmer-target="ordertotals-amount"], .order-summary-line-definition, td:last-child, span.grand-total-price'
                                );
                                if (amountElem) {
                                    const amountText = (amountElem.innerText || '').trim();
                                    if (amountText) return amountText;
                                }
                            }
                        }
                    }
                    return null;
                }
                """
            )
        except Exception:
            return None

    async def checkout_sequence(self, page: Page, task: dict[str, Any], key: str) -> None:
        try:
            self.update_status(key, "Checking out...")
            for _ in range(12):
                current_url = page.url.lower()

                email_field = await self.get_visible_element(page, "#ap_email, input[type='email']")
                if "signin" in current_url or email_field:
                    self.log(f"[AUTH] Manual login required for {key}!")
                    self.update_status(key, "Login Required")
                    self.send_notification("Login Required", f"Task {key} needs manual login.")
                    return

                if "/cart" in current_url:
                    await self.update_cart_quantity(page, int(task.get("quantity", 1)))

                order_btn = await self.get_visible_element(page, SELECTORS["place_order"])
                if order_btn:
                    await self.adjust_checkout_quantity(page, int(task.get("quantity", 1)))
                    order_total_text = await self.get_checkout_total(page)
                    order_total = self.parse_price(order_total_text)
                    shipping_info = await self.get_shipping_info(page)
                    if task.get("free_shipping") and not shipping_info.is_free and not shipping_info.is_known:
                        for _ in range(4):
                            await asyncio.sleep(0.75)
                            shipping_info = await self.get_shipping_info(page)
                            if shipping_info.is_free or shipping_info.is_known:
                                break
                    shipping_cost = shipping_info.price
                    if task.get("free_shipping") and not shipping_info.is_free:
                        if shipping_info.is_known:
                            self.log(
                                f"[CANCEL] Shipping is not free ({self._shipping_summary(shipping_info)}) "
                                "and the task requires Free Shipping. Cancelling purchase..."
                            )
                            self.update_status(key, "Non-Free Shipping")
                            self.send_notification(
                                "Purchase Cancelled - Shipping Cost",
                                f"Task {key} cancelled: Checkout shipping is not free ({self._shipping_summary(shipping_info)}).",
                            )
                        else:
                            self.log(
                                f"[CANCEL] Shipping could not be confirmed as free at checkout"
                                f"{f' ({shipping_info.text})' if shipping_info.text else ''}. "
                                "Cancelling purchase because the task requires Free Shipping..."
                            )
                            self.update_status(key, "Shipping Unconfirmed")
                            self.send_notification(
                                "Purchase Cancelled - Shipping Unconfirmed",
                                f"Task {key} cancelled: checkout shipping could not be confirmed as free.",
                            )
                        return

                    if self.confirm_callback:
                        shipping_display = (
                            "Free"
                            if shipping_info.is_free
                            else f"${shipping_cost:.2f}"
                            if shipping_info.is_known
                            else "Unknown"
                        )
                        msg = (
                            f"Task {key}\n\n"
                            f"URL: {task['query']}\n"
                            f"Qty: {task.get('quantity', 1)}\n"
                            f"Checkout Total: {order_total_text or 'unknown'}\n"
                            f"Shipping: {shipping_display}\n\n"
                            "Place order now?"
                        )
                        confirmed = await asyncio.to_thread(self.confirm_callback, msg)
                        if not confirmed:
                            self.log(f"[CANCEL] Order placement cancelled by user for {key}.")
                            self.update_status(key, "Cancelled")
                            return

                    self.log(f"[ORDER] PLACING ORDER for {key}...")
                    await order_btn.click(force=True)
                    if task.get("stop_after_purchase"):
                        task["_stopped_after_purchase"] = True
                        self.log(
                            f"[TASK] {key}: purchase succeeded; stopping monitoring for this task as configured."
                        )
                        self.update_status(key, "Success - Monitoring Stopped")
                    else:
                        self.update_status(key, "Success")
                    self.send_notification(
                        "Purchase Success!",
                        f"Purchased item from {task['query']}",
                    )
                    return

                proceed_btn = await self.get_visible_element(page, SELECTORS["proceed_to_checkout"])
                if proceed_btn:
                    self.log(f"[CHECKOUT] Clicking Proceed to Checkout for {key}...")
                    await proceed_btn.click(force=True)
                    try:
                        await page.wait_for_load_state("commit", timeout=5000)
                    except Exception:
                        pass
                    continue

                addr_btn = await self.get_visible_element(page, SELECTORS["address_select"])
                if addr_btn:
                    self.log(f"[CHECKOUT] Selecting delivery address for {key}...")
                    await addr_btn.click(force=True)
                    try:
                        await page.wait_for_load_state("commit", timeout=5000)
                    except Exception:
                        pass
                    continue

                pmt_btn = await self.get_visible_element(page, SELECTORS["payment_select"])
                if pmt_btn:
                    self.log(f"[CHECKOUT] Selecting payment method for {key}...")
                    await pmt_btn.click(force=True)
                    try:
                        await page.wait_for_load_state("commit", timeout=5000)
                    except Exception:
                        pass
                    continue

                cont_btn = await self.get_visible_element(page, SELECTORS["continue_button"])
                if cont_btn:
                    self.log(f"[CHECKOUT] Clicking continue checkout step for {key}...")
                    await cont_btn.click(force=True)
                    try:
                        await page.wait_for_load_state("commit", timeout=5000)
                    except Exception:
                        pass
                    continue

                prime_btn = await self.get_visible_element(page, SELECTORS["prime_decline"])
                if prime_btn:
                    self.log(f"[PRIME] Declining Prime upsell/signup for {key}...")
                    await prime_btn.click(force=True)
                    try:
                        await page.wait_for_load_state("commit", timeout=5000)
                    except Exception:
                        pass
                    continue

                await asyncio.sleep(1)

            self.update_status(key, "Final button missing")
            self.send_notification(
                "Checkout Failed",
                f"Checkout failed for task {key}:\nPlace Order button was not found during checkout steps.",
            )
        except Exception as exc:
            self.log(f"Checkout error: {exc}")
            self.send_notification("Checkout Error", f"Checkout error occurred for task {key}:\n{exc}")

    async def _wait_for_product_ready(self, page: Page) -> None:
        landmark_selectors = [
            SELECTORS["product_title"],
            SELECTORS["captcha_check"],
            SELECTORS["add_to_cart"],
            SELECTORS["buying_options"],
            "#availability",
            "#outOfStock",
            "#dp",
            "body",
        ]
        try:
            await page.wait_for_selector(
                ", ".join(landmark_selectors),
                state="attached",
                timeout=10000,
            )
        except Exception:
            pass

    async def _wait_for_offer_controls(self, page: Page) -> None:
        selectors = [
            SELECTORS["add_to_cart"],
            SELECTORS["buy_now"],
            SELECTORS["buying_options"],
            SELECTORS["oos_indicator"],
        ]
        try:
            await page.wait_for_selector(
                ", ".join(selectors),
                state="visible",
                timeout=8000,
            )
        except Exception:
            await asyncio.sleep(2)

    async def _handle_captcha(self, page: Page, key: str, scope: str) -> None:
        captcha = await page.query_selector(SELECTORS["captcha_check"])
        if not captcha:
            return

        prefix = "[CAPTCHA] Captcha on " if scope == "product" else "[CAPTCHA] Captcha on search query "
        self.log(f"{prefix}{key}! Please solve in browser.")
        self.update_status(key, "CAPTCHA")
        try:
            await page.wait_for_selector(
                SELECTORS["captcha_check"],
                state="hidden",
                timeout=120000,
            )
        except Exception:
            pass

    async def _read_product_title(self, page: Page) -> str:
        title_elem = await page.query_selector(SELECTORS["product_title"])
        if not title_elem:
            return "Unknown Product"

        try:
            title = (await title_elem.inner_text()).strip()
            return title or "Unknown Product"
        except Exception:
            return "Unknown Product"

    async def _read_cart_count(self, page: Page) -> int | None:
        cart_count_elem = await page.query_selector(SELECTORS["cart_count"])
        if not cart_count_elem:
            return None
        try:
            count_text = (await cart_count_elem.inner_text()).strip()
        except Exception:
            return None
        return int(count_text) if count_text.isdigit() else None

    async def _read_cart_items(self, page: Page) -> list[dict[str, Any]]:
        try:
            raw_items = await page.evaluate(
                """
                () => {
                    const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
                    const rows = Array.from(
                        document.querySelectorAll(
                            '[data-name="Active Items"] [data-asin], .sc-list-item[data-asin], [data-testid="cart-item"][data-asin]'
                        )
                    );
                    const items = [];
                    for (const row of rows) {
                        const asin = (
                            row.getAttribute('data-asin') ||
                            row.querySelector('input[name$=".asin"]')?.value ||
                            row.querySelector('input[name*="[asin]"]')?.value ||
                            ''
                        ).trim().toUpperCase();
                        if (!asin) {
                            continue;
                        }

                        const title = (
                            textOf(row.querySelector('.sc-product-title')) ||
                            textOf(row.querySelector('.a-truncate-cut')) ||
                            textOf(row.querySelector('img[alt]')) ||
                            textOf(row.querySelector('a[title]'))
                        );

                        let quantityText =
                            row.querySelector("select[name='quantity']")?.value ||
                            row.querySelector('select.sc-quantity-selector')?.value ||
                            row.querySelector("input[name='quantityBox']")?.value ||
                            textOf(row.querySelector("span[data-a-selector='value']")) ||
                            textOf(row.querySelector("span[data-a-selector='inner-value']")) ||
                            '';

                        const quantityMatch = String(quantityText).match(/\\d+/);
                        const quantity = quantityMatch ? parseInt(quantityMatch[0], 10) : 1;
                        items.push({ asin, title, quantity: Number.isFinite(quantity) ? quantity : 1 });
                    }
                    return items;
                }
                """
            )
        except Exception:
            return []

        merged: dict[str, dict[str, Any]] = {}
        for item in raw_items or []:
            asin = str(item.get("asin") or "").upper()
            if not asin:
                continue
            title = str(item.get("title") or "").strip()
            quantity = item.get("quantity")
            try:
                qty_int = int(quantity)
            except Exception:
                qty_int = 1
            if asin in merged:
                merged[asin]["quantity"] += qty_int
                if not merged[asin]["title"] and title:
                    merged[asin]["title"] = title
            else:
                merged[asin] = {"asin": asin, "title": title, "quantity": max(1, qty_int)}
        return list(merged.values())

    async def _ensure_cart_matches_task(
        self,
        page: Page,
        key: str,
        expected_asin: str | None,
    ) -> bool:
        if not expected_asin:
            return True

        try:
            await page.goto(SITES[self._site_key]["cart_url"], wait_until="commit", timeout=30000)
        except Exception:
            return False

        items = await self._read_cart_items(page)
        if not items:
            self.log(f"[GUARD] {key}: cart is empty or unreadable after add-to-cart. Aborting checkout for safety.")
            return False

        matching = next((item for item in items if item["asin"] == expected_asin), None)
        if not matching:
            cart_summary = ", ".join(
                f"{item['asin']} x{item['quantity']}" for item in items[:4]
            ) or "unknown items"
            self.log(
                f"[GUARD] {key}: cart does not contain expected ASIN {expected_asin}. "
                f"Cart currently has {cart_summary}. Aborting checkout for safety."
            )
            return False

        unique_asins = {item["asin"] for item in items}
        if len(unique_asins) > 1:
            cart_summary = ", ".join(
                f"{item['asin']} x{item['quantity']}" for item in items[:4]
            )
            self.log(
                f"[GUARD] {key}: cart contains multiple products ({cart_summary}). "
                "Aborting automated checkout to avoid purchasing the wrong item."
            )
            return False

        return True

    async def _get_current_asin(self, page: Page) -> str | None:
        try:
            asin = await page.evaluate(
                """
                () => {
                    const candidates = [
                        document.querySelector('#ASIN')?.value,
                        document.querySelector('input[name="ASIN"]')?.value,
                        document.querySelector('#attach-baseAsin')?.value,
                        document.querySelector('link[rel="canonical"]')?.href,
                        window.location.href,
                    ].filter(Boolean);

                    for (const value of candidates) {
                        const match = String(value).match(/\\/(?:dp|gp\\/product|gp\\/aw\\/d|gp\\/offer-listing)\\/([A-Z0-9]{10})/i);
                        if (match) {
                            return match[1].toUpperCase();
                        }
                        if (/^[A-Z0-9]{10}$/i.test(String(value))) {
                            return String(value).toUpperCase();
                        }
                    }
                    return null;
                }
                """
            )
        except Exception:
            return None
        if not asin:
            return None
        return str(asin).upper()

    async def _ensure_expected_product(
        self,
        page: Page,
        key: str,
        url: str,
        expected_asin: str | None,
    ) -> bool:
        if not expected_asin:
            return True

        for attempt in range(2):
            current_asin = await self._get_current_asin(page)
            if not current_asin or current_asin == expected_asin:
                return True

            self.log(
                f"[GUARD] Product mismatch for {key}: expected ASIN {expected_asin}, "
                f"but page is {current_asin}. Reloading the task URL..."
            )
            if attempt == 0:
                try:
                    await page.goto(url, wait_until='commit', timeout=30000)
                    await self._wait_for_product_ready(page)
                    await self._handle_captcha(page, key, "product")
                    await self._wait_for_offer_controls(page)
                except Exception:
                    return False

        return False

    async def _confirm_purchase_flow_start(
        self,
        page: Page,
        key: str,
        url: str,
        expected_asin: str | None,
        cart_count_before: int | None,
    ) -> bool:
        try:
            await page.wait_for_function(
                """
                (previousCount) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            rect.width > 0 &&
                            rect.height > 0;
                    };

                    const path = window.location.pathname || '';
                    if (
                        path.includes('/gp/cart/') ||
                        path.includes('/cart') ||
                        path.includes('/checkout/')
                    ) {
                        return true;
                    }

                    const proceed = document.querySelector(
                        "input[name='proceedToRetailCheckout'], #sc-buy-box-ptc-button, #attach-sidesheet-checkout-button, #attach-sidesheet-checkout-button-deliv"
                    );
                    const placeOrder = document.querySelector(
                        "#placeYourOrder, input[name='placeYourOrder1'], input[name='placeYourOrder']"
                    );
                    if (isVisible(proceed) || isVisible(placeOrder)) {
                        return true;
                    }

                    const cartCount = document.querySelector('#nav-cart-count');
                    const currentCount = parseInt((cartCount?.innerText || '').trim(), 10);
                    return Number.isFinite(previousCount) && Number.isFinite(currentCount) && currentCount > previousCount;
                }
                """,
                arg=cart_count_before,
                timeout=8000,
            )
        except Exception:
            self.log(f"[CLICK] {key}: no cart/checkout confirmation detected after clicking.")
            return False

        current_url = page.url.lower()
        if any(token in current_url for token in ("/cart", "/checkout/", "/buy/")):
            return True

        current_asin = await self._get_current_asin(page)
        if expected_asin and current_asin and current_asin != expected_asin:
            self.log(
                f"[GUARD] {key}: click navigated to ASIN {current_asin} instead of {expected_asin}. "
                "Cancelling this attempt and returning to the monitored product."
            )
            try:
                await page.goto(url, wait_until="commit", timeout=30000)
            except Exception:
                pass
            return False

        return True

    def _shipping_summary(self, shipping_info: ShippingInfo) -> str:
        if shipping_info.is_free:
            return "Free Shipping"
        if shipping_info.is_known:
            return f"Shipping: ${shipping_info.price:.2f}"
        return "Shipping not confirmed"

    async def _extract_main_offer(
        self,
        page: Page,
        task: dict[str, Any],
        title_str: str,
    ) -> OfferCandidate | None:
        price_text = await self.get_price(page)
        if not price_text:
            return None

        price_val = self.parse_price(price_text)
        if price_val is None:
            return None

        shipping_info = await self.get_shipping_info(page)
        estimated_total = price_val + shipping_info.price

        atc_btn = await self.get_visible_element(page, SELECTORS["add_to_cart"])
        is_buy_now = False
        if not atc_btn:
            atc_btn = await self.get_visible_element(page, SELECTORS["buy_now"])
            is_buy_now = bool(atc_btn)
        if not atc_btn:
            return None

        self.log(
            f"[PRICE] Price Match: ${price_val:.2f} "
            f"({self._shipping_summary(shipping_info)} | Estimated Total: ${estimated_total:.2f}) "
            f"for '{title_str[:60]}'"
        )

        return OfferCandidate(
            source="main",
            title=title_str,
            item_price=price_val,
            shipping_price=shipping_info.price,
            effective_total=estimated_total,
            shipping_known=shipping_info.is_known,
            shipping_is_free=shipping_info.is_free,
            shipping_text=shipping_info.text,
            action_path="buy_now" if is_buy_now else "main_atc",
            action_name="Buy Now" if is_buy_now else "Add to Cart",
        )

    async def _extract_buying_options_offer(
        self,
        page: Page,
        task: dict[str, Any],
        key: str,
        title_str: str,
        buying_options,
    ) -> OfferCandidate | None:
        try:
            self.log(f"[ATC] Opening Buying Options for {key}...")
            await self._open_buying_options_surface(page, buying_options)
            if not await self._ensure_aod_assets_loaded(page):
                self.log(f"[ATC] Buying Options assets did not initialize for {key}.")
                return None
            await self._activate_buying_options_widget(page)
            await self._wait_for_buying_options_content(page)
            await self._expand_buying_options(page)
        except Exception:
            return None

        drawer_candidates = await self._extract_buying_options_candidates(page, title_str)
        if drawer_candidates:
            best_candidate = min(
                drawer_candidates,
                key=lambda candidate: (candidate.item_price, candidate.shipping_price),
            )
            self.log(
                f"[PRICE] Buying options best offer: ${best_candidate.item_price:.2f} "
                f"({self._shipping_summary(ShippingInfo(best_candidate.shipping_price, best_candidate.shipping_known, best_candidate.shipping_is_free, best_candidate.shipping_text))})"
            )
            return best_candidate

        aod_price_text = await self._first_visible_text(
            page,
            [
                "#aod-pinned-offer span.a-price",
                "#aod-offer-list span.a-price",
                ".aod-price span.a-price",
                "#aod-price-1",
                "#aod-offer-price",
            ],
        )
        if not aod_price_text:
            return None

        aod_price_val = self.parse_price(aod_price_text)
        if aod_price_val is None:
            return None

        aod_shipping_text = await self._first_visible_text(
            page,
            [
                "#aod-pinned-offer-additional-shipping-message",
                "#aod-offer-list [id*='shipping-message']",
                ".aod-shipping-message",
                "#aod-offer-shipping-charge",
            ],
        )
        aod_shipping_val = 0.0
        aod_shipping_info = self.parse_shipping_info_text(aod_shipping_text)
        if aod_shipping_info.is_known or aod_shipping_info.is_free:
            aod_shipping_val = aod_shipping_info.price

        atc_btn = await self.get_visible_element(page, SELECTORS["side_panel_atc"])
        if not atc_btn:
            return None

        estimated_total = aod_price_val + aod_shipping_val
        self.log(
            f"[PRICE] Side panel price match: ${aod_price_val:.2f} "
            f"({self._shipping_summary(aod_shipping_info)} | Estimated Total: ${estimated_total:.2f})"
        )
        return OfferCandidate(
            source="aod",
            title=title_str,
            item_price=aod_price_val,
            shipping_price=aod_shipping_val,
            effective_total=estimated_total,
            shipping_known=aod_shipping_info.is_known,
            shipping_is_free=aod_shipping_info.is_free,
            shipping_text=aod_shipping_info.text,
            action_path="side_panel_atc",
            action_name="Add to Cart",
            offer_rank=0,
        )

    def _validate_candidate(
        self,
        task: dict[str, Any],
        candidate: OfferCandidate,
    ) -> tuple[bool, str | None]:
        label = "Side panel offer" if candidate.source == "aod" else "Offer"

        if task.get("free_shipping"):
            if candidate.shipping_is_free:
                pass
            elif candidate.shipping_known:
                self.log(
                    f"[PRICE] Skip: {label} is not free shipping "
                    f"({self._shipping_summary(ShippingInfo(candidate.shipping_price, candidate.shipping_known, candidate.shipping_is_free, candidate.shipping_text))})."
                )
                return False, "Non-Free Shipping"
            else:
                self.log(
                    f"[PRICE] Skip: {label} shipping could not be confirmed as free."
                )
                return False, "Shipping Unconfirmed"

        max_price = float(task.get("max_price", 0) or 0)
        if max_price > 0 and candidate.item_price > max_price:
            self.log(
                f"[PRICE] Skip: {label} unit price ${candidate.item_price:.2f} "
                f"exceeds limit ${max_price:.2f}"
            )
            return False, f"Price too high (${candidate.item_price:.2f})"

        return True, None

    async def _wait_for_buying_options_ready(self, page: Page) -> None:
        try:
            await page.wait_for_function(
                """
                () => {
                    const bodyText = (document.body?.innerText || '');

                    if (window.location.pathname.includes('/gp/offer-listing/')) {
                        return true;
                    }

                    const selectors = [
                        '#all-offers-display',
                        '#all-offers-display-scroller',
                        '#aod-offer-list',
                        '#aod-pinned-offer',
                        '[aria-label*="Add to Cart from seller"]',
                        '[aria-label*="See more options"]',
                    ];

                    if (selectors.some((selector) => document.querySelector(selector))) {
                        return true;
                    }

                    return bodyText.includes('Sellers on Amazon') && /Add to Cart/i.test(bodyText);
                }
                """,
                timeout=15000,
            )
        except Exception:
            await asyncio.sleep(4)

    async def _wait_for_buying_options_content(self, page: Page) -> None:
        try:
            await page.wait_for_function(
                """
                () => {
                    const hasVisibleSellerButton = Array.from(
                        document.querySelectorAll(
                            '[aria-label*="Add to Cart from seller"], ' +
                            '#all-offers-display input[name="submit.addToCart"], ' +
                            '#all-offers-display button, ' +
                            '#aod-offer-list input[name="submit.addToCart"]'
                        )
                    ).some((el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            rect.width > 0 &&
                            rect.height > 0;
                    });

                    const bodyText = document.body?.innerText || '';
                    return hasVisibleSellerButton ||
                        bodyText.includes('Sellers on Amazon') ||
                        bodyText.includes('Add to Cart from seller');
                }
                """,
                timeout=12000,
            )
        except Exception:
            await asyncio.sleep(2)

    async def _open_buying_options_surface(self, page: Page, buying_options) -> None:
        href = await buying_options.get_attribute("href")
        if href and not href.lower().startswith("javascript:"):
            if href.startswith("/"):
                href = f"{SITES[self._site_key]['base_url']}{href}"
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                if "ERR_ABORTED" not in str(exc):
                    raise
            await self._wait_for_buying_options_ready(page)
            return

        await buying_options.click(force=True)
        await self._wait_for_buying_options_ready(page)

    async def _ensure_aod_assets_loaded(self, page: Page) -> bool:
        if await self._aod_assets_loaded(page):
            return True

        try:
            loaded = await page.evaluate(
                """
                async (assetUrl) => {
                    const P = window.P || window.AmazonUIPageJS;
                    if (!P || !P.load || typeof P.load.js !== 'function' || typeof P.when !== 'function') {
                        return false;
                    }

                    P.load.js(assetUrl);

                    const deadline = Date.now() + 8000;
                    const readState = () => new Promise((resolve) => {
                        try {
                            P.when('A').execute(function(A) {
                                const state = typeof A.state === 'function' ? A.state('aod:assetsLoaded') : null;
                                resolve(!!(state && state.isAodAssetsLoaded));
                            });
                        } catch (error) {
                            resolve(false);
                        }
                    });

                    while (Date.now() < deadline) {
                        if (await readState()) {
                            return true;
                        }
                        await new Promise((resolve) => setTimeout(resolve, 250));
                    }
                    return false;
                }
                """,
                AOD_DETAIL_PAGE_ASSET_URL,
            )
            return bool(loaded)
        except Exception:
            return False

    async def _aod_assets_loaded(self, page: Page) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """
                    () => new Promise((resolve) => {
                        const P = window.P || window.AmazonUIPageJS;
                        if (!P || typeof P.when !== 'function') {
                            return resolve(false);
                        }

                        try {
                            P.when('A').execute(function(A) {
                                const state = typeof A.state === 'function' ? A.state('aod:assetsLoaded') : null;
                                resolve(!!(state && state.isAodAssetsLoaded));
                            });
                        } catch (error) {
                            resolve(false);
                        }
                    })
                    """
                )
            )
        except Exception:
            return False

    async def _activate_buying_options_widget(self, page: Page) -> None:
        trigger = await self.get_visible_element(page, AOD_TRIGGER_SELECTOR)
        if trigger:
            await trigger.click(force=True)
            return

        buying_options = await self.get_visible_element(page, SELECTORS["buying_options"])
        if buying_options:
            await buying_options.click(force=True)

    async def _expand_buying_options(self, page: Page) -> None:
        for _ in range(3):
            more_btn = await self.get_visible_element(page, SELECTORS["buying_options_more"])
            if not more_btn:
                return

            try:
                await more_btn.click(force=True)
                await asyncio.sleep(1.0)
            except Exception:
                return

    async def _click_action_element(
        self,
        page: Page,
        element,
        key: str,
        action_name: str,
    ) -> None:
        try:
            await element.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        try:
            await element.click(timeout=5000)
            return
        except Exception as exc:
            first_exc = exc
            self.log(
                f"[CLICK] Retrying {action_name} click for {key} after actionability error: {exc}"
            )

        try:
            await element.evaluate(
                """
                (el) => {
                    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                }
                """
            )
            await asyncio.sleep(0.25)
            await element.click(force=True, timeout=5000)
            return
        except Exception:
            pass

        try:
            await element.dispatch_event("click")
            return
        except Exception:
            raise first_exc

    async def _find_matching_side_panel_button(
        self,
        page: Page,
        candidate: OfferCandidate,
    ):
        visible_buttons = await self.get_visible_elements(page, SELECTORS["side_panel_atc"])
        if not visible_buttons:
            return None

        if candidate.action_label:
            normalized_target = " ".join(candidate.action_label.split())
            for button in visible_buttons:
                aria = await button.get_attribute("aria-label") or ""
                label = await button.get_attribute("value") or ""
                combined = " ".join(f"{aria} {label}".split())
                if combined == normalized_target:
                    return button

        unique_buttons = []
        seen = set()
        for button in visible_buttons:
            aria = await button.get_attribute("aria-label") or ""
            label = await button.get_attribute("value") or ""
            bbox = await button.bounding_box()
            key = (
                " ".join(f"{aria} {label}".split()),
                round(bbox["x"], 1) if bbox else None,
                round(bbox["y"], 1) if bbox else None,
                round(bbox["width"], 1) if bbox else None,
                round(bbox["height"], 1) if bbox else None,
            )
            if key in seen:
                continue
            seen.add(key)
            unique_buttons.append(button)

        if candidate.offer_rank is not None and 0 <= candidate.offer_rank < len(unique_buttons):
            return unique_buttons[candidate.offer_rank]
        return unique_buttons[0]

    async def _extract_buying_options_candidates(
        self,
        page: Page,
        title_str: str,
    ) -> list[OfferCandidate]:
        try:
            extracted = await page.evaluate(
                """
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            rect.width > 0 &&
                            rect.height > 0;
                    };

                    const textOf = (el) => (
                        (el?.innerText || el?.textContent || '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                    );

                    const pickContainer = (el) => {
                        let current = el;
                        for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
                            const text = textOf(current);
                            if (/\\$\\s?\\d/.test(text)) {
                                return current;
                            }
                        }
                        return el.parentElement || el;
                    };

                    const parseShipping = (text) => {
                        if (!text) {
                            return { price_text: null, is_free: false, is_known: false };
                        }
                        if (/free\\s+(delivery|shipping)/i.test(text)) {
                            return { price_text: '0', is_free: true, is_known: true };
                        }
                        const shippingMatch = text.match(
                            /(?:shipping|delivery|import\\s+charges?)\\D{0,20}\\$\\s?([\\d,]+(?:\\.\\d{2})?)/i
                        );
                        if (shippingMatch) {
                            return { price_text: shippingMatch[1], is_free: false, is_known: true };
                        }
                        if (/(shipping|delivery|import\\s+charges?)/i.test(text)) {
                            return { price_text: null, is_free: false, is_known: false };
                        }
                        return { price_text: null, is_free: false, is_known: false };
                    };

                    const buttons = Array.from(
                        document.querySelectorAll('button, input[type="submit"], input[type="button"]')
                    ).filter((el) => {
                        if (!visible(el)) return false;
                        const label = [
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('value') || '',
                            textOf(el),
                        ].join(' ');
                        if (!/add to cart/i.test(label)) {
                            return false;
                        }

                        if (window.location.pathname.includes('/gp/offer-listing/')) {
                            return true;
                        }

                        if (/from seller/i.test(label)) {
                            return true;
                        }

                        return !!el.closest(
                            '#all-offers-display, #all-offers-display-scroller, #aod-offer-list, #aod-pinned-offer'
                        );
                    });

                    const offers = [];
                    for (const button of buttons) {
                        const container = pickContainer(button);
                        const buttonLabel = [
                            button.getAttribute('aria-label') || '',
                            button.getAttribute('value') || '',
                            textOf(button),
                        ].join(' ');
                        const containerText = textOf(container);
                        const combinedText = `${buttonLabel} ${containerText}`.trim();
                        const priceMatch = combinedText.match(/\\$\\s?([\\d,]+(?:\\.\\d{2})?)/);
                        if (!priceMatch) {
                            continue;
                        }

                        const shipping = parseShipping(combinedText);
                        offers.push({
                            rank: offers.length,
                            price_text: priceMatch[1],
                            shipping_text: shipping.price_text,
                            shipping_is_free: shipping.is_free,
                            shipping_known: shipping.is_known,
                            button_label: buttonLabel,
                        });
                    }

                    return offers;
                }
                """
            )
        except Exception:
            return []

        candidates: list[OfferCandidate] = []
        for offer in extracted or []:
            price_val = self.parse_price(offer.get("price_text"))
            if price_val is None:
                continue

            shipping_is_free = bool(offer.get("shipping_is_free"))
            shipping_known = bool(offer.get("shipping_known"))
            shipping_val = 0.0
            if shipping_is_free:
                shipping_val = 0.0
            elif shipping_known:
                shipping_val = self.parse_price(offer.get("shipping_text")) or 0.0
            candidates.append(
                OfferCandidate(
                    source="aod",
                    title=title_str,
                    item_price=price_val,
                    shipping_price=shipping_val,
                    effective_total=price_val + shipping_val,
                    shipping_known=shipping_known,
                    shipping_is_free=shipping_is_free,
                    shipping_text=offer.get("shipping_text"),
                    action_path="side_panel_atc",
                    action_name="Add to Cart",
                    offer_rank=offer.get("rank"),
                    action_label=offer.get("button_label"),
                )
            )

        unique_candidates: list[OfferCandidate] = []
        seen = set()
        for candidate in candidates:
            key = (
                candidate.action_label or "",
                candidate.item_price,
                candidate.shipping_price,
                candidate.shipping_known,
                candidate.shipping_is_free,
            )
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(candidate)

        return unique_candidates

    async def _prepare_selected_offer(
        self,
        page: Page,
        task: dict[str, Any],
        key: str,
        url: str,
        candidate: OfferCandidate,
        aod_opened: bool,
    ):
        quantity = int(task.get("quantity", 1))

        if candidate.source == "main":
            if aod_opened:
                await page.goto(url, wait_until="commit", timeout=30000)
                await self._wait_for_product_ready(page)
                await self._handle_captcha(page, key, "product")
                await self._wait_for_offer_controls(page)

            await self.set_product_quantity(page, quantity, key)
            selector = SELECTORS["buy_now"] if candidate.action_path == "buy_now" else SELECTORS["add_to_cart"]
            return await self.get_visible_element(page, selector)

        matched_button = await self._find_matching_side_panel_button(page, candidate)
        if matched_button:
            return matched_button

        buying_options = await self.get_visible_element(page, SELECTORS["buying_options"])
        if not buying_options:
            return None

        try:
            self.log(f"[ATC] Opening Buying Options for {key}...")
            await self._open_buying_options_surface(page, buying_options)
            if not await self._ensure_aod_assets_loaded(page):
                return None
            await self._activate_buying_options_widget(page)
            await self._wait_for_buying_options_content(page)
            await self._expand_buying_options(page)
        except Exception:
            return None

        return await self._find_matching_side_panel_button(page, candidate)

    async def _first_visible_text(self, page: Page, selectors: list[str]) -> str | None:
        for selector in selectors:
            try:
                for elem in await page.query_selector_all(selector):
                    if not await elem.is_visible():
                        continue
                    text = (await elem.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return None
