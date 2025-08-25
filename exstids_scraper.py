
import json
import os
import asyncio
import requests
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
import pandas as pd
import threading
from io import StringIO
import time

PROXY_FILE = "proxies.json"
EXPORT_FILE = "proxies.txt"
MAX_CONCURRENT_VALIDATIONS = 500
CONNECT_TIMEOUT_SECONDS = 3
GEO_LOOKUP_ENABLED_DEFAULT = True

# ----------------------
# Storage
# ----------------------
def load_proxies():
    if os.path.exists(PROXY_FILE):
        with open(PROXY_FILE, "r") as f:
            try:
                data = json.load(f)
                # Backward compat: allow list of strings
                if data and isinstance(data[0], str):
                    return [{"proxy": p, "status": "Unknown", "latency": None, "country": "-", "last_checked": None} for p in data]
                return data
            except Exception:
                return []
    return []

def save_proxies(data):
    with open(PROXY_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ----------------------
# Scraping
# ----------------------
def scrape_proxies(status_callback):
    proxies = set()
    html_sources = [
        "https://free-proxy-list.net/",
        "https://www.sslproxies.org/",
        "https://www.us-proxy.org/"
    ]
    for url in html_sources:
        try:
            status_callback(f"Scraping from {url}")
            res = requests.get(url, timeout=10)
            tables = pd.read_html(StringIO(res.text))
            if not tables:
                continue
            rows = tables[0]
            for _, row in rows.iterrows():
                ip = str(row.get('IP Address', '')).strip()
                port = str(row.get('Port', '')).strip()
                if ip and port and ':' not in ip and port.isdigit():
                    proxies.add(f"{ip}:{port}")
        except Exception:
            status_callback(f"[ERROR] Failed {url}")

    raw_sources = [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
        "https://www.proxy-list.download/api/v1/get?type=http"
    ]
    for url in raw_sources:
        try:
            status_callback(f"Scraping from {url}")
            res = requests.get(url, timeout=10)
            matches = socket_matches(res.text)
            proxies.update(matches)
        except Exception:
            status_callback(f"[ERROR] Failed {url}")

    return list(proxies)

def socket_matches(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        parts = line.split(':')
        if len(parts) == 2 and parts[1].isdigit():
            out.append(f"{parts[0]}:{parts[1]}")
    return out

# ----------------------
# Validation (async)
# ----------------------
async def geo_lookup_ip(ip, geo_cache, rate_sem):
    if ip in geo_cache:
        return geo_cache[ip]
    # throttle calls to the free API
    async with rate_sem:
        def _do_req():
            try:
                r = requests.get(f"http://ip-api.com/json/{ip}?fields=countryCode,status", timeout=5)
                j = r.json()
                if j.get("status") == "success":
                    return j.get("countryCode") or "-"
            except Exception:
                pass
            return "-"
        country = await asyncio.to_thread(_do_req)
        geo_cache[ip] = country
        return country

async def validate_one(proxy, sem, stop_flag, want_geo, geo_cache, geo_rate_sem):
    async with sem:
        if stop_flag["stop"]:
            return None
        result = {"proxy": proxy, "status": "Dead", "latency": None, "country": "-", "last_checked": datetime.now().isoformat()}
        try:
            ip, port = proxy.split(":")
            start = time.perf_counter()
            connect_coro = asyncio.open_connection(ip, int(port))
            reader, writer = await asyncio.wait_for(connect_coro, timeout=CONNECT_TIMEOUT_SECONDS)
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                writer.close()
                if hasattr(writer, "wait_closed"):
                    await writer.wait_closed()
            except Exception:
                pass
            result["status"] = "Live"
            result["latency"] = latency_ms
            if want_geo:
                result["country"] = await geo_lookup_ip(ip, geo_cache, geo_rate_sem)
        except Exception:
            pass
        return result

async def validate_stream(proxies, stop_flag, want_geo=True):
    sem = asyncio.Semaphore(MAX_CONCURRENT_VALIDATIONS)
    geo_cache = {}
    geo_rate_sem = asyncio.Semaphore(5)

    tasks = [asyncio.create_task(validate_one(p, sem, stop_flag, want_geo, geo_cache, geo_rate_sem)) for p in proxies]
    for fut in asyncio.as_completed(tasks):
        res = await fut
        if res is not None:
            yield res

# ----------------------
# GUI
# ----------------------
class ProxyDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Exstid Proxy Dashboard – Live Streaming")
        self.root.geometry("900x600")
        self.proxies = load_proxies()
        self.stop_flag = {"stop": False}
        self.geo_lookup_var = tk.BooleanVar(value=GEO_LOOKUP_ENABLED_DEFAULT)
        self._row_ids = {}  # proxy -> tree item id
        self.setup_widgets()
        self.rebuild_row_index()
        self.update_table()

    # --- UI helpers (thread-safe) ---
    def set_status(self, msg):
        self.root.after(0, lambda: self.status_label.config(text=msg))

    def set_progress(self, done, total):
        def _upd():
            self.progress["maximum"] = max(total, 1)
            self.progress["value"] = done
            self.progress_label.config(text=f"{done}/{total}")
        self.root.after(0, _upd)

    def insert_or_update_row(self, rec):
        # rec: dict with keys proxy,status,latency,country,last_checked
        def _upd():
            values = (
                rec.get("proxy",""),
                rec.get("status","Unknown"),
                rec.get("latency",""),
                rec.get("country","-"),
                (rec.get("last_checked","") or "")[:19].replace("T"," ")
            )
            key = rec.get("proxy","")
            if key in self._row_ids:
                self.tree.item(self._row_ids[key], values=values)
            else:
                iid = self.tree.insert("", tk.END, values=values)
                self._row_ids[key] = iid
        self.root.after(0, _upd)

    def rebuild_row_index(self):
        self._row_ids.clear()
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if vals:
                self._row_ids[vals[0]] = iid

    # --- Build UI ---
    def setup_widgets(self):
        cols = ("IP", "Status", "Latency (ms)", "Country", "Last Checked")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings")
        for col in cols:
            self.tree.heading(col, text=col)
        self.tree.column("IP", width=220, stretch=True)
        self.tree.column("Status", width=90, stretch=True)
        self.tree.column("Latency (ms)", width=110, stretch=True)
        self.tree.column("Country", width=80, stretch=True)
        self.tree.column("Last Checked", width=160, stretch=True)
        self.tree.pack(fill=tk.BOTH, expand=True, pady=(10, 6), padx=10)

        # Controls
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=4)
        tk.Button(btn_frame, text="Scrape", command=self.scrape_threaded).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Refresh", command=self.refresh_threaded).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Export to proxies.txt", command=self.auto_export).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Clear All", command=self.clear_all).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Stop", command=self.stop_now).pack(side=tk.LEFT, padx=5)

        # Options
        options_frame = tk.Frame(self.root)
        options_frame.pack(pady=(0, 6))
        tk.Checkbutton(options_frame, text="Geo lookup for live proxies", variable=self.geo_lookup_var).pack(side=tk.LEFT, padx=8)

        # Progress
        prog_frame = tk.Frame(self.root)
        prog_frame.pack(fill=tk.X, padx=10)
        self.progress = ttk.Progressbar(prog_frame, orient="horizontal", mode="determinate")
        self.progress.pack(fill=tk.X, expand=True, side=tk.LEFT)
        self.progress_label = tk.Label(prog_frame, text="0/0", width=8, anchor="e")
        self.progress_label.pack(side=tk.RIGHT, padx=(6, 0))

        # Status bar
        self.status_label = tk.Label(self.root, text="Ready.", anchor="w")
        self.status_label.pack(fill=tk.X, padx=10, pady=5)

    def update_table(self):
        self.tree.delete(*self.tree.get_children())
        for p in self.proxies:
            ip = p.get("proxy", "")
            status = p.get("status", "Unknown")
            latency = p.get("latency", "")
            country = p.get("country", "-")
            last = p.get("last_checked", "") or ""
            if last:
                last = last[:19].replace("T", " ")
            iid = self.tree.insert("", tk.END, values=(ip, status, latency, country, last))
            self._row_ids[ip] = iid

    # --- Threaded actions ---
    def scrape_threaded(self):
        threading.Thread(target=self.scrape, daemon=True).start()

    def refresh_threaded(self):
        threading.Thread(target=self.refresh, daemon=True).start()

    def scrape(self):
        self.stop_flag["stop"] = False
        self.set_status("Scraping proxies...")
        scraped = scrape_proxies(self.set_status)
        self.set_status(f"Found {len(scraped)} proxies. Validating (streaming live results)...")

        existing = {p["proxy"] for p in self.proxies}
        new_proxies = [p for p in scraped if p not in existing]
        if not new_proxies:
            self.set_status("No new proxies found. Validating existing list instead...")
            new_proxies = [p["proxy"] for p in self.proxies]

        self.validate_streaming(new_proxies, replace=False)

    def refresh(self):
        self.stop_flag["stop"] = False
        self.set_status("Re-validating current proxies (streaming updates)...")
        all_proxies = [p["proxy"] for p in self.proxies]
        if not all_proxies:
            self.set_status("No proxies saved yet. Try Scrape first.")
            return
        self.validate_streaming(all_proxies, replace=True)

    def auto_export(self):
        live = [p["proxy"] for p in self.proxies if p.get("status") == "Live"]
        with open(EXPORT_FILE, "w") as f:
            for proxy in live:
                f.write(proxy + "\n")
        self.set_status(f"Exported {len(live)} proxies to {EXPORT_FILE}")

    def clear_all(self):
        if messagebox.askyesno("Confirm", "Clear all proxies?"):
            self.proxies = []
            save_proxies(self.proxies)
            self.update_table()
            self.set_status("Cleared.")

    def stop_now(self):
        self.set_status("Stopping... (will finish in-flight checks)")
        self.stop_flag["stop"] = True

    # --- Streaming validation orchestrator ---
    def validate_streaming(self, proxy_list, replace=False):
        def run():
            async def runner():
                total = len(proxy_list)
                done = 0
                self.set_progress(done, total)

                # Map for quick updates
                by_key = {p["proxy"]: p for p in self.proxies}

                async for res in validate_stream(proxy_list, self.stop_flag, want_geo=self.geo_lookup_var.get()):
                    done += 1
                    self.set_progress(done, total)

                    key = res["proxy"]
                    if replace:
                        # Keep both Live/Dead when replacing (refresh mode)
                        by_key[key] = res
                        self.insert_or_update_row(res)
                    else:
                        # On scrape, stream only Lives into the UI and memory
                        if res["status"] == "Live":
                            by_key[key] = res
                            self.insert_or_update_row(res)

                    if done % 50 == 0:
                        # Periodic save to disk
                        save_proxies(list(by_key.values()))

                # Finalize
                self.proxies = list(by_key.values())
                save_proxies(self.proxies)
                live_count = sum(1 for p in self.proxies if p.get("status") == "Live")
                self.set_status(f"\u2714 Done. {live_count} live proxies. (Checked {total})")

            asyncio.run(runner())

        threading.Thread(target=run, daemon=True).start()

def main():
    root = tk.Tk()
    app = ProxyDashboard(root)
    root.mainloop()

if __name__ == "__main__":
    main()
