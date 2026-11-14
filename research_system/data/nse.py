"""Shared HTTP access to NSE's public website and archives.

nsearchives.nseindia.com serves static daily files; www.nseindia.com/api needs
a cookie from the home page first. One polite session is reused per run.
"""
from __future__ import annotations

import io
import random
import time
import zipfile

import requests

NSE_HOME = "https://www.nseindia.com/"
UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


class NSE:
    def __init__(self, pause: float = 0.6, timeout: float = 40.0):
        self.pause = pause
        self.timeout = timeout
        self._last = 0.0
        self._new_session()

    def _new_session(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": random.choice(UA),
            "Accept": "application/json,text/csv,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": NSE_HOME,
        })
        try:
            self.s.get(NSE_HOME, timeout=20)
        except requests.RequestException:
            pass

    def _wait(self):
        dt = self._last + self.pause - time.time()
        if dt > 0:
            time.sleep(dt + random.uniform(0, 0.2))
        self._last = time.time()

    def get(self, url: str, retries: int = 3) -> requests.Response | None:
        """GET with retry. Returns None for 404 (holiday / not published)."""
        for attempt in range(retries):
            self._wait()
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                self._new_session()
            time.sleep(2 ** attempt)
        return None

    def json(self, url: str):
        r = self.get(url)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def text(self, url: str) -> str | None:
        r = self.get(url)
        return r.text if r is not None else None

    def zip_member(self, url: str) -> bytes | None:
        r = self.get(url)
        if r is None:
            return None
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            return z.read(z.namelist()[0])
        except zipfile.BadZipFile:
            return None
