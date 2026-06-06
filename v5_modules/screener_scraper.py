"""
screener_scraper.py
===================
Production-grade fetcher for Screener.in fundamentals data.

Implements the fetch_screener_ratios() function declared in quality_gate.py.
Designed to run on Oracle Cloud (where outbound HTTPS works) — the local
Cowork sandbox blocks outbound network. Validated parsing against BIOCON +
MANKIND HTML structure (Screener.in layout as of 2026-06).

Features:
  * SQLite cache with configurable TTL (default 24 hours).
  * User-Agent rotation across a small pool.
  * Rate-limited: 1 request every 2 seconds (be a good citizen).
  * Retries with exponential backoff on 429/503.
  * Robust HTML parsing — works even when Screener layout shifts a little.

Usage as a library:
    from screener_scraper import ScreenerScraper
    s = ScreenerScraper(cache_dir="./fundamentals_cache")
    data = s.fetch("BIOCON")
    # data has keys: roce_pct, roe_pct, debt_to_equity, opm_pct,
    #               cfo_last_year, cfo_prior_year,
    #               sales_growth_3y, pat_growth_3y, market_cap_inr,
    #               promoter_pct_latest, promoter_pct_prev_q,
    #               n_promoter_quarters, screener_pros, screener_cons

Usage from the command line:
    python screener_scraper.py --bootstrap symbols.txt
    # symbols.txt: one NSE symbol per line
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]


@dataclass
class ScreenerData:
    symbol: str
    fetched_at: int = 0
    # Top-line ratios
    roce_pct: Optional[float] = None
    roe_pct: Optional[float] = None
    debt_to_equity: Optional[float] = None
    opm_pct: Optional[float] = None
    stock_pe: Optional[float] = None
    market_cap_inr: Optional[float] = None
    book_value_inr: Optional[float] = None
    # Cash flow
    cfo_last_year: Optional[float] = None
    cfo_prior_year: Optional[float] = None
    # Growth
    sales_growth_3y_pct: Optional[float] = None
    pat_growth_3y_pct: Optional[float] = None
    sales_growth_10y_pct: Optional[float] = None
    # Shareholding (latest 2 quarters)
    promoter_pct_latest: Optional[float] = None
    promoter_pct_prev_q: Optional[float] = None
    promoter_drop_qoq_pp: Optional[float] = None
    # Screener.in's own annotations
    screener_pros: list = None
    screener_cons: list = None
    # Raw page hash for change detection
    page_hash: Optional[str] = None
    _fetched_ok: bool = False

    def to_dict(self):
        return asdict(self)


# =========================================================
# Cache
# =========================================================
class _Cache:
    def __init__(self, path: Path, ttl_seconds: int = 86_400):
        self.path = path
        self.ttl = ttl_seconds
        # Resilient to read-only filesystem fallbacks
        try:
            self.db = sqlite3.connect(str(path))
        except sqlite3.OperationalError:
            self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sc(symbol TEXT PRIMARY KEY, fetched_at INTEGER, payload TEXT)"
        )
        self.db.commit()

    def get(self, sym: str) -> Optional[dict]:
        row = self.db.execute("SELECT fetched_at, payload FROM sc WHERE symbol=?", (sym,)).fetchone()
        if not row:
            return None
        when, payload = row
        if (time.time() - when) > self.ttl:
            return None
        return json.loads(payload)

    def put(self, sym: str, data: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO sc(symbol, fetched_at, payload) VALUES (?, ?, ?)",
            (sym, int(time.time()), json.dumps(data)),
        )
        self.db.commit()


# =========================================================
# Parsing helpers
# =========================================================
_NUM_RE = re.compile(r"-?\d+(?:,\d+)*(?:\.\d+)?")


def _to_float(s: str | None) -> Optional[float]:
    if s is None:
        return None
    m = _NUM_RE.search(str(s).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _crore_to_inr(s: str | None) -> Optional[float]:
    """Convert '57,088 Cr.' -> 570880000000 INR."""
    v = _to_float(s)
    return v * 1e7 if v is not None else None


def _find_top_ratio(soup: BeautifulSoup, label: str) -> Optional[str]:
    """
    Find one of the top-card 'ratio' values by label.
    Screener.in renders these in <li> with <span class="name"> and <span class="number">.
    """
    for li in soup.select("ul li"):
        name_el = li.select_one(".name")
        num_el = li.select_one(".number, .nowrap")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if name.lower() == label.lower():
            if num_el:
                return num_el.get_text(strip=True)
            return li.get_text(" ", strip=True)
    return None


def _parse_screener_html(html: str, symbol: str) -> ScreenerData:
    soup = BeautifulSoup(html, "lxml")
    data = ScreenerData(symbol=symbol, fetched_at=int(time.time()))
    data.page_hash = hashlib.md5(html.encode("utf-8", errors="ignore")).hexdigest()

    # Top card ratios
    mc_text   = _find_top_ratio(soup, "Market Cap")
    roce_text = _find_top_ratio(soup, "ROCE")
    roe_text  = _find_top_ratio(soup, "ROE")
    pe_text   = _find_top_ratio(soup, "Stock P/E")
    bv_text   = _find_top_ratio(soup, "Book Value")
    data.market_cap_inr = _crore_to_inr(mc_text)
    data.roce_pct = _to_float(roce_text)
    data.roe_pct  = _to_float(roe_text)
    data.stock_pe = _to_float(pe_text)
    data.book_value_inr = _to_float(bv_text)

    # Pros / Cons
    pros, cons = [], []
    for sec in soup.select("div.pros, div.cons, .company-pros, .company-cons"):
        items = [li.get_text(" ", strip=True) for li in sec.select("li")]
        if "pros" in sec.get("class", []) or "pros" in (sec.get("id") or ""):
            pros.extend(items)
        else:
            cons.extend(items)
    # Fallback heuristic: headers labelled "Pros" / "Cons"
    if not pros and not cons:
        for h in soup.find_all(["h2", "h3", "strong", "b"]):
            t = h.get_text(strip=True).lower()
            if t in ("pros", "cons"):
                ul = h.find_next("ul")
                if ul:
                    items = [li.get_text(" ", strip=True) for li in ul.select("li")]
                    if t == "pros":
                        pros.extend(items)
                    else:
                        cons.extend(items)
    data.screener_pros = pros
    data.screener_cons = cons

    # ---- Balance sheet -> D/E ----
    # Find table that lists 'Borrowings', 'Reserves', 'Equity Capital' rows
    de = None
    for tbl in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in tbl.find_all("th")]
        if "Mar 2025" in headers or "Mar 2024" in headers:
            rows = {tr.find("td").get_text(strip=True).rstrip("+").strip(): tr
                    for tr in tbl.find_all("tr") if tr.find("td")}
            borr_row = rows.get("Borrowings") or rows.get("Borrowings+")
            res_row  = rows.get("Reserves")
            eq_row   = rows.get("Equity Capital")
            if borr_row and res_row and eq_row:
                # Take last column (most recent)
                bs_cells = [td.get_text(" ", strip=True) for td in borr_row.find_all("td")][1:]
                rs_cells = [td.get_text(" ", strip=True) for td in res_row.find_all("td")][1:]
                eq_cells = [td.get_text(" ", strip=True) for td in eq_row.find_all("td")][1:]
                if bs_cells and rs_cells and eq_cells:
                    b = _to_float(bs_cells[-1])
                    r = _to_float(rs_cells[-1])
                    e = _to_float(eq_cells[-1])
                    if b is not None and r is not None and e is not None and (r + e) > 0:
                        de = b / (r + e)
                        break
    data.debt_to_equity = de

    # ---- Cash flow -> CFO last 2 years ----
    cfo_last = cfo_prior = None
    for tbl in soup.find_all("table"):
        first_col_texts = []
        for tr in tbl.find_all("tr"):
            td = tr.find("td")
            if td:
                first_col_texts.append(td.get_text(strip=True).rstrip("+").strip())
        if "Cash from Operating Activity" in first_col_texts:
            tr_target = next(tr for tr in tbl.find_all("tr")
                             if tr.find("td") and "Cash from Operating Activity" in tr.find("td").get_text())
            cells = [_to_float(td.get_text(" ", strip=True)) for td in tr_target.find_all("td")][1:]
            cells = [c for c in cells if c is not None]
            if len(cells) >= 2:
                cfo_last, cfo_prior = cells[-1], cfo_last if cfo_last is None else cells[-2]
                cfo_prior = cells[-2]
            elif len(cells) == 1:
                cfo_last = cells[-1]
            break
    data.cfo_last_year, data.cfo_prior_year = cfo_last, cfo_prior

    # ---- Growth: look for 'Compounded Sales Growth' and 'Compounded Profit Growth' boxes ----
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Compounded Sales Growth.*?3 Years:\s*(-?\d+)%", text, re.S)
    if m: data.sales_growth_3y_pct = float(m.group(1))
    m = re.search(r"Compounded Sales Growth.*?10 Years:\s*(-?\d+)%", text, re.S)
    if m: data.sales_growth_10y_pct = float(m.group(1))
    m = re.search(r"Compounded Profit Growth.*?3 Years:\s*(-?\d+)%", text, re.S)
    if m: data.pat_growth_3y_pct = float(m.group(1))

    # ---- OPM (from quarterly table 'OPM %' row, most recent) ----
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        for tr in rows:
            td = tr.find("td")
            if td and td.get_text(strip=True).startswith("OPM"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")][1:]
                opms = [_to_float(c) for c in cells if _to_float(c) is not None]
                if opms:
                    data.opm_pct = opms[-1]
                break
        if data.opm_pct is not None:
            break

    # ---- Promoter holding latest + prev-quarter ----
    prom_pcts = []
    for tbl in soup.find_all("table"):
        first_col = []
        for tr in tbl.find_all("tr"):
            td = tr.find("td")
            if td:
                first_col.append(td.get_text(strip=True).rstrip("+").strip())
        if "Promoters" in first_col:
            tr_target = next(tr for tr in tbl.find_all("tr")
                             if tr.find("td") and "Promoters" in tr.find("td").get_text())
            cells = [_to_float(td.get_text(" ", strip=True)) for td in tr_target.find_all("td")][1:]
            cells = [c for c in cells if c is not None]
            if cells:
                prom_pcts = cells
                break
    if len(prom_pcts) >= 2:
        data.promoter_pct_latest = prom_pcts[-1]
        data.promoter_pct_prev_q = prom_pcts[-2]
        data.promoter_drop_qoq_pp = prom_pcts[-2] - prom_pcts[-1]
    elif prom_pcts:
        data.promoter_pct_latest = prom_pcts[-1]

    # Sanity: a successful parse must give at least market cap + ROCE
    data._fetched_ok = data.market_cap_inr is not None and data.roce_pct is not None
    return data


# =========================================================
# Main scraper
# =========================================================
class ScreenerScraper:
    BASE = "https://www.screener.in/company/{}/consolidated/"

    def __init__(
        self,
        cache_dir: str | Path = "./fundamentals_cache",
        ttl_seconds: int = 86_400,
        min_request_interval: float = 2.0,
        timeout: float = 20.0,
        max_retries: int = 4,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache = _Cache(self.cache_dir / "screener.sqlite", ttl_seconds=ttl_seconds)
        self.min_req = min_request_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_req = 0.0
        self._session = requests.Session()
        self._log_path = self.cache_dir / "scrape.log"

    def _log(self, msg: str):
        with open(self._log_path, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    def _sleep_for_rate_limit(self):
        now = time.time()
        wait = self._last_req + self.min_req - now
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.3))
        self._last_req = time.time()

    def _get(self, url: str) -> Optional[str]:
        for attempt in range(self.max_retries):
            self._sleep_for_rate_limit()
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
            try:
                r = self._session.get(url, headers=headers, timeout=self.timeout)
                if r.status_code == 200:
                    return r.text
                if r.status_code in (429, 503):
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    self._log(f"GET {url} -> {r.status_code}, sleep {backoff:.1f}s")
                    time.sleep(backoff)
                    continue
                self._log(f"GET {url} -> {r.status_code} (giving up)")
                return None
            except requests.RequestException as e:
                self._log(f"GET {url} exception: {e}")
                time.sleep(2 + random.uniform(0, 1))
        return None

    def fetch(self, symbol: str, force_refresh: bool = False) -> dict:
        """Returns a dict (the ScreenerData fields)."""
        symbol = symbol.upper().strip()
        if not force_refresh:
            cached = self.cache.get(symbol)
            if cached:
                return cached
        url = self.BASE.format(symbol)
        html = self._get(url)
        if html is None:
            empty = ScreenerData(symbol=symbol, _fetched_ok=False, fetched_at=int(time.time())).to_dict()
            self.cache.put(symbol, empty)
            return empty
        data = _parse_screener_html(html, symbol).to_dict()
        if data["_fetched_ok"]:
            self.cache.put(symbol, data)
        return data

    def bootstrap(self, symbols: list, force_refresh: bool = False, log_every: int = 25):
        """Fetch every symbol in the list. Returns a summary dict."""
        ok = fail = cached = 0
        t0 = time.time()
        for i, s in enumerate(symbols, 1):
            d = self.fetch(s, force_refresh=force_refresh)
            if d.get("_fetched_ok"):
                ok += 1
            else:
                fail += 1
            if i % log_every == 0:
                elapsed = time.time() - t0
                eta = elapsed / i * (len(symbols) - i)
                msg = f"[{i}/{len(symbols)}] ok={ok} fail={fail}  elapsed={elapsed/60:.1f}m  ETA={eta/60:.1f}m"
                print(msg, flush=True)
                self._log(msg)
        return {"ok": ok, "fail": fail, "total": len(symbols),
                "elapsed_minutes": (time.time() - t0) / 60}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", help="Path to a file with one symbol per line.")
    ap.add_argument("--one", help="Fetch a single symbol and print its data.")
    ap.add_argument("--cache-dir", default="./fundamentals_cache")
    ap.add_argument("--force", action="store_true", help="Ignore cache, re-fetch.")
    args = ap.parse_args()
    sc = ScreenerScraper(cache_dir=args.cache_dir)
    if args.one:
        d = sc.fetch(args.one, force_refresh=args.force)
        print(json.dumps(d, indent=2, default=str))
        return
    if args.bootstrap:
        with open(args.bootstrap) as f:
            syms = [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]
        print(f"Bootstrapping {len(syms)} symbols...")
        res = sc.bootstrap(syms, force_refresh=args.force)
        print("Done:", res)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
