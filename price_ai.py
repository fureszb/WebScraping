#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B+N PriceAI v1.8.1 - JAVÍTOTT VERZIÓ
Változások:
- ✅ Frissített Google Shopping selectorok (2025)
- ✅ Javított retail URL struktúrák (OBI, Bauhaus, Praktiker)
- ✅ Debug logging minden lépéshez
- ✅ HTML dump hibakereséshez
"""
VERSION = "B+N PriceAI v1.8.1"

import os
import re
import sys
import json
import time
import argparse
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter, Retry


DATE_FMT = "%Y-%m-%d %H:%M:%S"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)


def now_str() -> str:
    return dt.datetime.now().strftime(DATE_FMT)


def normalize(value: str | None) -> str:
    if value is None:
        return ""
    value = str(value)
    return re.sub(r"\s+", " ", value).strip()


DEFAULT_CONFIG: Dict[str, Any] = {
    "threads": 3,
    "ttl_days": 7,
    "debug_mode": False,
    "shops": {
        "OBI": True,
        "PRAKTIKER": True,
        "BAUHAUS": True,
        "EMAG": False,
        "MELEGET": False,
        "TERC": True,
        "TUZEPINFO": True,
        "GOOGLE_CSE": False,
        "GOOGLE_SHOPPING": True,
    },
    "terc": {"token": ""},
    "google_cse": {
        "api_key": "",
        "cx": "",
        "max_queries_per_item": 2,
        "timeout": 6,
    },
    "selenium": {
        "binary": None,
        "driver_path": None,
        "page_timeout": 15,
    },
    "proxy": {"enabled": False, "http": "", "https": ""},
    "ollama": {
        "enabled": False,
        "host": "http://127.0.0.1:11434",
        "primary": "phi4",
        "fallback": "llama3.1",
    },
}


def make_session(proxy: dict | None) -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
    )
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html;q=0.9",
        }
    )
    if proxy and proxy.get("enabled"):
        proxies = {}
        if proxy.get("http"):
            proxies["http"] = proxy["http"]
        if proxy.get("https"):
            proxies["https"] = proxy["https"]
        if proxies:
            session.proxies.update(proxies)
    return session


def build_driver(debug: bool, config: dict):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()

    if not debug:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("start-maximized")
    options.add_argument("disable-infobars")

    if config.get("proxy"):
        options.add_argument(f"--proxy-server={config['proxy']}")

    capabilities = config.get("capabilities", {})
    for key, value in capabilities.items():
        options.set_capability(key, value)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(config.get("timeout", 30))
    return driver


SUSPICIOUS = {0, 7468}


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\s]", "", text)
    cleaned = cleaned.replace("\xa0", " ").replace(" ", "")
    if cleaned.count(".") > 1:
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
        if value <= 0 or value > 10_000_000:
            return None
        if int(value) in SUSPICIOUS:
            return None
        return round(value, 2)
    except Exception:
        return None


def google_shopping_first_hit(
    driver, query: str, debug: bool = False
) -> Optional[Tuple[str, str, float]]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    url = (
        "https://www.google.com/search?q="
        + requests.utils.quote(query, safe="")
        + "&tbm=shop"
    )
    if debug:
        print(f"🔍 Google Shopping URL: {url}")

    driver.get(url)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
        )
    except Exception as exc:
        if debug:
            print(f"❌ Timeout loading page: {exc}")
        return None

    try:
        for button in driver.find_elements(By.CSS_SELECTOR, "button"):
            text = (button.text or "").lower()
            if any(
                needle in text
                for needle in ["elfogad", "accept all", "i agree", "elfogadom"]
            ):
                button.click()
                time.sleep(0.5)
                break
    except Exception:
        pass

    card_selectors = [
        "div[data-merchant-id]",
        "div.sh-sr__shop-result-group div[data-docid]",
        "div.sh-dgr__content",
        "div.sh-dlr__list-result",
        "div.sh-pr__product-results > div",
        "div[jsname] a[href*='shopping/product']",
        "div[data-hveid] div[data-docid]",
        "div.pla-unit",
        "div[data-sokoban-container]",
        "div[role='listitem']",
        "li.sh-sr__shop-result",
    ]

    card = None
    used_selector = None

    for selector in card_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                card = elements[0]
                used_selector = selector
                if debug:
                    print(f"✅ Találat: {selector} ({len(elements)} elem)")
                break
        except Exception:
            if debug:
                print(f"⚠️ Skip: {selector}")

    if not card:
        if debug:
            print("❌ Nincs termék kártya")
            try:
                html = driver.find_element(By.CSS_SELECTOR, "body").get_attribute(
                    "innerHTML"
                )
                with open("debug_google_shopping.html", "w", encoding="utf-8") as dump:
                    dump.write(html)
                print("💾 HTML mentve: debug_google_shopping.html")
            except Exception:
                pass
        return None

    title = ""
    title_selectors = [
        "h3",
        "h4",
        "span[role='heading']",
        "div[role='heading']",
        "div.eIuuYe",
        "a[aria-label]",
        "div.sh-np__product-title",
        "div[data-product-title]",
    ]

    for selector in title_selectors:
        try:
            element = card.find_element(By.CSS_SELECTOR, selector)
            title = element.text.strip() or element.get_attribute("aria-label") or ""
            if title:
                if debug:
                    print(f"📝 Cím: {title}")
                break
        except Exception:
            continue

    price_text = ""
    price_selectors = [
        "span.a8Pemb",
        "span[data-price]",
        "div[aria-label*='Ft']",
        "span.kHxwFf",
        "span.tPhRLe",
        "div[data-product-price]",
        "*[data-dtype='d3price']",
    ]

    for selector in price_selectors:
        try:
            element = card.find_element(By.CSS_SELECTOR, selector)
            price_text = element.text.strip() or element.get_attribute("aria-label") or ""
            if price_text:
                if debug:
                    print(f"💵 Ár: {price_text}")
                break
        except Exception:
            continue

    link = ""
    link_selectors = [
        "a[href*='shopping/product']",
        "a.shntl",
        "a.pla-unit-title-link",
        "a[jsname]",
        "a[href*='/aclk']",
    ]

    for selector in link_selectors:
        try:
            anchor = card.find_element(By.CSS_SELECTOR, selector)
            link = anchor.get_attribute("href") or ""
            if link:
                if debug:
                    print(f"🔗 Link: {link[:80]}...")
                break
        except Exception:
            continue

    price_value = parse_price(price_text)

    if not title or not price_value:
        if debug:
            print(f"⚠️ Hiányos: title={bool(title)}, price={price_value}")
        return None

    return (title, link, price_value)


RETAIL: Dict[str, Dict[str, str]] = {
    "OBI": {
        "search": "https://www.obi.hu/search/{q}",
        "product_link_sel": "a.product-tile__link, a[href*='/p/'], div.product-tile a",
        "price_sel": ".price__number, [data-testid='price'], .price, span[data-price]",
    },
    "PRAKTIKER": {
        "search": "https://www.praktiker.hu/search/{q}",
        "product_link_sel": "a.product-card__link, a[href*='/p/'], div.product-card a",
        "price_sel": ".price-tag__price, .product-price-value, .price, [data-price]",
    },
    "BAUHAUS": {
        "search": "https://www.bauhaus.hu/prefixbox/search?q={q}",
        "product_link_sel": "a.product-item-link, a[href*='.html'], div.product-item a",
        "price_sel": ".price-wrapper .price, [data-price-type='finalPrice'], .price, span[data-price]",
    },
}


def retail_first_price(
    driver, shop: str, query: str, debug: bool = False
) -> Optional[Tuple[str, str, float]]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    if shop not in RETAIL:
        return None

    config = RETAIL[shop]
    encoded_query = requests.utils.quote(query)
    url = config["search"].format(q=encoded_query)

    if debug:
        print(f"\n🏪 {shop}: {url}")

    driver.get(url)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
        )
    except Exception as exc:
        if debug:
            print(f"❌ {shop} timeout: {exc}")
        return None

    time.sleep(1)

    link_selectors = [selector.strip() for selector in config["product_link_sel"].split(",")]
    links = []

    for selector in link_selectors:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, selector)
            if found:
                links = found
                if debug:
                    print(f"✅ {len(found)} link: {selector}")
                break
        except Exception:
            continue

    if not links:
        if debug:
            print(f"❌ {shop}: Nincs link")
            try:
                html = driver.find_element(By.CSS_SELECTOR, "body").get_attribute(
                    "innerHTML"
                )
                with open(f"debug_{shop.lower()}.html", "w", encoding="utf-8") as dump:
                    dump.write(html)
                print(f"💾 HTML: debug_{shop.lower()}.html")
            except Exception:
                pass
        return None

    href = None
    title = None

    for anchor in links[:3]:
        candidate = anchor.get_attribute("href")
        candidate_title = anchor.text.strip()
        if candidate and not candidate.endswith("#") and "javascript:" not in candidate:
            href = candidate
            title = candidate_title or query
            if debug:
                print(f"🔗 Link: {href}")
            break

    if not href:
        if debug:
            print(f"❌ {shop}: Nincs href")
        return None

    driver.get(href)
    time.sleep(1.2)

    price_selectors = [selector.strip() for selector in config["price_sel"].split(",")]
    price_element = None

    for selector in price_selectors:
        try:
            element = driver.find_element(By.CSS_SELECTOR, selector)
            if element and element.text:
                price_element = element
                if debug:
                    print(f"💰 Ár: {element.text}")
                break
        except Exception:
            continue

    if not price_element:
        if debug:
            print(f"❌ {shop}: Nincs ár")
        return None

    price = parse_price(price_element.text)

    if not price:
        if debug:
            print(f"⚠️ {shop}: Parse hiba: {price_element.text}")
        return None

    try:
        heading = driver.find_element(By.CSS_SELECTOR, "h1")
        title = heading.text.strip() or title
    except Exception:
        pass

    if debug:
        print(f"✅ {shop} OK: {price} Ft")

    return (title or query, driver.current_url, price)


class TercClient:
    BASE = "https://api.terc.hu"

    def __init__(self, token: str, session: requests.Session):
        self.token = token
        self.session = session

    def search(self, name: str) -> dict:
        url = f"{self.BASE}/v2/product/search"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {
            "product-name": name,
            "page": 1,
            "page-size": 5,
            "sort-by": "description",
            "sort-order": "ASC",
        }
        response = self.session.get(url, headers=headers, params=params, timeout=12)
        if response.status_code == 401:
            raise RuntimeError("TERC Unauthorized")
        response.raise_for_status()
        return response.json()


class TuzepinfoClient:
    BASE = "https://api.terc.hu"

    def __init__(self, token: str, session: requests.Session):
        self.token = token
        self.session = session

    def search(self, name: str) -> dict:
        url = f"{self.BASE}/v2/tuzepinfo-product-search"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        body = {
            "filter": {"productName": name},
            "sort": {"nettoPrice": "ASC"},
            "pageIndex": 1,
            "pageSize": 5,
        }
        response = self.session.post(url, headers=headers, json=body, timeout=12)
        if response.status_code == 404:
            return {"results": []}
        if response.status_code == 401:
            raise RuntimeError("Tuzepinfo Unauthorized")
        response.raise_for_status()
        return response.json()


def rule_accept(query: str, price: float) -> Tuple[bool, str]:
    lowered = query.lower()
    bands = [
        (("szalag", "festőszalag", "ragasztó"), 5000),
        (("izzó", "led"), 4000),
        (("elosztó", "el osztó"), 20000),
        (("cső", "aluvent", "aluvents", "vent"), 20000),
        (("vakolat", "ragasztó"), 40000),
    ]
    for keys, limit in bands:
        if any(key in lowered for key in keys):
            if price <= limit:
                return True, "OK"
            return False, f"Túl magas ({price} Ft)"
    return True, "OK"


import hashlib


def cache_key(name: str) -> str:
    return hashlib.sha1(normalize(name).encode("utf-8")).hexdigest()


def load_cache(path: str, ttl_days: int) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    cutoff = time.time() - ttl_days * 86400
    return {k: v for k, v in data.items() if v.get("_ts", 0) >= cutoff}


def save_cache(path: str, data: dict) -> None:
    for value in data.values():
        value["_ts"] = value.get("_ts", time.time())
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


@dataclass
class ResultRow:
    name: str
    time: str = field(default_factory=now_str)
    source: Optional[str] = None
    product_name: Optional[str] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = "HUF"
    validation: str = "Hiba/hiány"
    score: float = 0.0
    reason: Optional[str] = None
    url: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


def process_one(
    name: str,
    cfg: dict,
    session: requests.Session,
    debug: bool,
    shared_driver=None,
) -> ResultRow:
    name_normalized = normalize(name)
    row = ResultRow(name=name_normalized)

    if debug:
        print("\n" + "=" * 60)
        print(f"🔍 {name_normalized}")
        print("=" * 60)

    driver = shared_driver
    own_driver = False
    if driver is None:
        driver = build_driver(debug, cfg.get("selenium", {}))
        own_driver = True

    try:
        if cfg["shops"].get("GOOGLE_SHOPPING", True):
            if debug:
                print("\n1️⃣ Google Shopping...")
            gs_hit = google_shopping_first_hit(driver, name_normalized, debug=debug)
            if gs_hit:
                title, url, price = gs_hit
                ok, reason = rule_accept(name_normalized, price)
                if ok:
                    row.source = "GOOGLE_SHOPPING"
                    row.product_name = title
                    row.price = price
                    row.validation = "OK"
                    row.score = 1.0
                    row.url = url
                    row.meta["via"] = "shopping_card"
                    if debug:
                        print("✅ SIKER!")
                    return row
                row.meta["gs_reject"] = reason
                if debug:
                    print(f"⚠️ Elutasítva: {reason}")

        for shop in ("OBI", "PRAKTIKER", "BAUHAUS"):
            if not cfg["shops"].get(shop, False):
                continue
            if debug:
                print(f"\n2️⃣ {shop}...")
            hit = retail_first_price(driver, shop, name_normalized, debug=debug)
            if hit:
                title, url, price = hit
                ok, reason = rule_accept(name_normalized, price)
                if ok:
                    row.source = f"{shop} Selenium"
                    row.product_name = title
                    row.price = price
                    row.validation = "OK"
                    row.score = 0.95
                    row.url = url
                    if debug:
                        print("✅ SIKER!")
                    return row
                row.meta[f"{shop.lower()}_reject"] = reason
                if debug:
                    print(f"⚠️ Elutasítva: {reason}")

        if cfg["shops"].get("TERC") and cfg.get("terc", {}).get("token"):
            if debug:
                print("\n3️⃣ TERC...")
            try:
                terc_client = TercClient(cfg["terc"]["token"], session)
                terc_data = terc_client.search(name_normalized)
                for product in terc_data.get("products", [])[:3]:
                    price = product.get("netPrice")
                    if price:
                        ok, reason = rule_accept(name_normalized, price)
                        if ok:
                            row.source = "TERC"
                            row.product_name = product.get("description") or product.get(
                                "identifier"
                            )
                            row.price = price
                            row.validation = "OK"
                            row.score = 0.9
                            row.meta["terc_id"] = product.get("id")
                            if debug:
                                print("✅ SIKER!")
                            return row
            except Exception as exc:
                row.meta["terc_err"] = str(exc)
                if debug:
                    print(f"❌ Hiba: {exc}")

        if cfg["shops"].get("TUZEPINFO") and cfg.get("terc", {}).get("token"):
            if debug:
                print("\n4️⃣ Tüzépinfó...")
            try:
                tz_client = TuzepinfoClient(cfg["terc"]["token"], session)
                data = tz_client.search(name_normalized)
                for item in data.get("results", [])[:3]:
                    for shop_key, price in (item.get("prices", {}) or {}).items():
                        if price:
                            price_value = float(price)
                            ok, reason = rule_accept(name_normalized, price_value)
                            if ok:
                                row.source = f"TUZEPINFO:{shop_key}"
                                row.product_name = item.get("productName")
                                row.price = price_value
                                row.validation = "OK"
                                row.score = 0.88
                                if debug:
                                    print("✅ SIKER!")
                                return row
            except Exception as exc:
                row.meta["tuzep_err"] = str(exc)
                if debug:
                    print(f"❌ Hiba: {exc}")

        row.reason = "Nincs érvényes találat"
        if debug:
            print("\n❌ Nincs találat")
        return row

    finally:
        if own_driver:
            try:
                driver.quit()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="B+N PriceAI v1.8.1")
    parser.add_argument("--excel-in", required=True)
    parser.add_argument("--excel-out", required=True)
    parser.add_argument("--name-col", default="Felhasznált anyag")
    parser.add_argument("--config", required=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG.copy()
    with open(args.config, "r", encoding="utf-8") as handle:
        user_cfg = json.load(handle)

    def merge(base: dict, override: dict) -> dict:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value
        return base

    cfg = merge(cfg, user_cfg)
    debug = bool(args.debug or cfg.get("debug_mode"))
    session = make_session(cfg.get("proxy", {}))

    df = pd.read_excel(args.excel_in)
    if args.name_col not in df.columns:
        print(f"❌ Hiányzó oszlop: {args.name_col}", file=sys.stderr)
        sys.exit(2)

    cache_path = os.path.join(
        os.path.dirname(args.excel_out) or ".", "cache_prices.json"
    )
    cache = load_cache(cache_path, int(cfg.get("ttl_days", 7)))

    names_series = df[args.name_col].astype(str).fillna("")
    names = [normalize(value) for value in names_series.tolist() if normalize(value)]
    seen: set[str] = set()
    unique: List[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)

    total = len(unique)
    print(
        f"🚀 {VERSION}\n\n"
        f"Sor: {len(names)} | Egyedi: {total} | Szál: {cfg['threads']} | Debug: {debug} | TTL: {cfg['ttl_days']} nap"
    )
    print("Shops: " + ", ".join([shop for shop, enabled in cfg["shops"].items() if enabled]))

    driver = build_driver(debug, cfg.get("selenium", {}))

    results_map: Dict[str, Dict[str, Any]] = {}
    start = time.perf_counter()

    def work(item_name: str) -> Tuple[str, Dict[str, Any]]:
        key = cache_key(item_name)
        if key in cache:
            cached = cache[key]
            return (item_name, cached | {"_cache": True})
        result = process_one(item_name, cfg, session, debug, shared_driver=driver)
        if result.validation == "OK" and result.price:
            cache[key] = {
                "source": result.source,
                "product_name": result.product_name,
                "price": result.price,
                "currency": result.currency,
                "validation": result.validation,
                "score": result.score,
                "reason": result.reason,
                "url": result.url,
                "meta": result.meta,
                "_ts": time.time(),
            }
            return (item_name, cache[key] | {"_cache": False})
        return (
            item_name,
            {
                "source": result.source,
                "product_name": result.product_name,
                "price": result.price,
                "currency": result.currency,
                "validation": result.validation,
                "score": result.score,
                "reason": result.reason,
                "url": result.url,
                "meta": result.meta,
                "_ts": time.time(),
                "_cache": False,
            },
        )

    with ThreadPoolExecutor(max_workers=int(cfg["threads"])) as executor:
        futures = {executor.submit(work, name): name for name in unique}
        completed = 0
        for future in as_completed(futures):
            item_name = futures[future]
            try:
                result = future.result()[1]
            except Exception as exc:
                result = {"validation": "Hiba/hiány", "reason": str(exc)}
            results_map[item_name] = result
            completed += 1
            elapsed = time.perf_counter() - start
            eta = (total - completed) * (elapsed / max(1, completed))
            print(
                f"[{completed}/{total}] '{item_name}' | ETA: ~{int(eta // 60):02d}:{int(eta % 60):02d} "
                f"| {result.get('source')} | {result.get('price')}"
            )

    try:
        driver.quit()
    except Exception:
        pass

    out_rows: List[Dict[str, Any]] = []
    for original in names_series.tolist():
        normalized_name = normalize(original)
        data = results_map.get(
            normalized_name, {"validation": "Hiba/hiány", "reason": "Nincs eredmény"}
        )
        out_rows.append(
            {
                "Keresett megnevezés": normalized_name,
                "Frissítés ideje": now_str(),
                "Forrás (kiválasztott)": data.get("source"),
                "Talált terméknév": data.get("product_name"),
                "Egység": None,
                "Ár (nettó)": data.get("price"),
                "Pénznem": data.get("currency", "HUF"),
                "Validáció típusa": "Automatikus",
                "AI/Validáció pontszám": data.get("score", 0.0),
                "AI indoklás": "OK"
                if data.get("validation") == "OK"
                else data.get("validation"),
                "Forrás link": data.get("url"),
                "Meta (indoklás, napló)": data.get("meta", {}),
                "Rendszer verzió": VERSION,
            }
        )

    out_df = pd.DataFrame(out_rows)
    merged = pd.concat([df.reset_index(drop=True), out_df.reset_index(drop=True)], axis=1)
    merged.to_excel(args.excel_out, index=False)
    save_cache(cache_path, cache)

    ok_count = sum(
        1
        for value in results_map.values()
        if value.get("validation") == "OK" and value.get("price")
    )
    print(f"\n✅ Kész: {args.excel_out} | {int(time.perf_counter() - start)} s")
    print(f"📊 Sikeres: {ok_count}/{total}")
    print(f"💾 Cache: {cache_path} ({len(cache)} bejegyzés)")


if __name__ == "__main__":
    main()
