import os
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def load_dotenv_file(path: str = '.env') -> None:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except FileNotFoundError:
            pass

    load_dotenv_file()


@dataclass
class LockerState:
    locker_id: int
    locked: bool
    item_detected: bool


class ESP32DeviceController:
    def __init__(self, base_url: str | None = None, timeout: float = 2.0, retries: int = 1):
        self.base_url = base_url or os.getenv("ESP32_BASE_URL", "http://esp32-locker.local")
        fallback_urls = os.getenv("ESP32_FALLBACK_URLS", "")
        self.fallback_urls = [url.strip() for url in fallback_urls.split(",") if url.strip()]
        
        # Initialize URL list with fallback
        self.configured_urls = [self.base_url] + self.fallback_urls
        self.device_urls = self._resolve_urls(self.configured_urls)
        
        self.timeout = float(os.getenv("ESP32_TIMEOUT", timeout))
        self.connect_timeout = float(os.getenv("ESP32_CONNECT_TIMEOUT", 5.0))
        self.retries = int(os.getenv("ESP32_MAX_RETRIES", retries))
        fingerprint_paths = os.getenv("ESP32_FINGERPRINT_PATHS", "/fingerprint/start-enrollment")
        self.fingerprint_paths = [p.strip() for p in fingerprint_paths.split(",") if p.strip()]
        
        # Health check tracking
        self.last_working_url = None
        self.last_health_check = 0
        self.health_check_interval = int(os.getenv("ESP32_HEALTH_CHECK_INTERVAL", 30))
        
        self.session = requests.Session()
        self.session.trust_env = False

        retry_strategy = Retry(
            total=self.retries,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            backoff_factor=0,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        print(f"[ESP32] Configured URLs: {self.configured_urls}")
        print(f"[ESP32] Resolved URLs: {self.device_urls}")
        print(f"[ESP32] Connect timeout: {self.connect_timeout}s, read timeout: {self.timeout}s")
        print(f"[ESP32] HTTP retries: {self.retries}")
        print(f"[ESP32] Health check interval: {self.health_check_interval}s")
        print(f"[ESP32] Fingerprint paths: {self.fingerprint_paths}")

    def _resolve_urls(self, urls: list) -> list:
        """Resolve hostnames to IPs where possible for faster connection."""
        resolved = []
        for url in urls:
            try:
                parsed = urlparse(url)
                hostname = parsed.netloc.split(":")[0]
                
                # Try to resolve hostname to IP
                try:
                    ip = socket.gethostbyname(hostname)
                    if ip != hostname:  # Only replace if resolution succeeded
                        resolved_url = url.replace(hostname, ip)
                        print(f"[ESP32] Resolved hostname '{hostname}' → {ip}")
                        resolved.append(resolved_url)
                    else:
                        resolved.append(url)
                except socket.gaierror:
                    # Hostname couldn't be resolved, keep as-is for mDNS fallback
                    print(f"[ESP32] Hostname '{hostname}' not resolved yet (will retry at runtime)")
                    resolved.append(url)
            except Exception as e:
                print(f"[ESP32] Error parsing URL {url}: {e}")
                resolved.append(url)
        
        return resolved

    def _get_urls_for_request(self) -> list:
        """Get URLs in optimal order: last working first, then others."""
        current_time = time.time()
        
        # If we have a recently working URL, try it first
        if (self.last_working_url and 
            (current_time - self.last_health_check) < self.health_check_interval):
            other_urls = [u for u in self.device_urls if u != self.last_working_url]
            return [self.last_working_url] + other_urls
        
        return self.device_urls

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Make HTTP request with intelligent fallback and health checking."""
        urls_to_try = self._get_urls_for_request()
        current_time = time.time()
        last_error = None
        
        for base_url in urls_to_try:
            url = f"{base_url}{path}"
            try:
                if method == "POST":
                    print(f"[ESP32] POST {url} payload={payload}")
                    r = self.session.post(url, json=payload or {}, timeout=(self.connect_timeout, self.timeout))
                else:
                    print(f"[ESP32] GET {url}")
                    r = self.session.get(url, timeout=(self.connect_timeout, self.timeout))

                r.raise_for_status()
                print(f"[ESP32] Response {r.status_code} from {url}")
                
                # Update health check state
                self.last_working_url = base_url
                self.last_health_check = current_time
                
                return r.json()
            except requests.exceptions.HTTPError as e:
                print(f"[ESP32] HTTP ERROR contacting {url}: {type(e).__name__}: {e}")
                last_error = e
                if r.status_code == 404:
                    continue
                raise
            except requests.exceptions.Timeout as e:
                print(f"[ESP32] TIMEOUT contacting {url}: {e}")
                last_error = e
                continue
            except requests.exceptions.ConnectionError as e:
                print(f"[ESP32] CONNECTION ERROR contacting {url}: {e}")
                last_error = e
                continue
            except Exception as e:
                print(f"[ESP32] ERROR contacting {url}: {type(e).__name__}: {e}")
                last_error = e
                continue

        if last_error:
            raise last_error
        raise RuntimeError(f"Failed to contact ESP32 at any configured URL: {urls_to_try}")

    def _post(self, path: str, payload: dict | None = None) -> dict:
        return self._request("POST", path, payload)

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def get_locker(self, locker_id: int) -> LockerState:
        data = self._get(f"/locker/{locker_id}/status")
        return LockerState(
            locker_id=locker_id,
            locked=bool(data.get("locked", True)),
            item_detected=bool(data.get("item_detected", False)),
        )

    def lock(self, locker_id: int) -> LockerState:
        data = self._post(f"/locker/{locker_id}/lock")
        return LockerState(
            locker_id=locker_id,
            locked=True,
            item_detected=bool(data.get("item_detected", False)),
        )

    def unlock(self, locker_id: int) -> LockerState:
        data = self._post(f"/locker/{locker_id}/unlock")
        return LockerState(
            locker_id=locker_id,
            locked=False,
            item_detected=bool(data.get("item_detected", False)),
        )

    def start_fingerprint_enrollment(self) -> dict:
        last_error = None
        for path in self.fingerprint_paths:
            try:
                return self._post(path)
            except requests.exceptions.HTTPError as e:
                last_error = e
                if e.response is not None and e.response.status_code == 404:
                    print(f"[ESP32] Fingerprint path not found: {path}")
                    continue
                raise
            except Exception as e:
                last_error = e
                continue

        raise last_error or RuntimeError(f"No supported fingerprint path found: {self.fingerprint_paths}")

    def clear_fingerprint_templates(self) -> bool:
        """Clear all fingerprint templates from device."""
        # Note: ESP32 firmware currently doesn't have HTTP server endpoints
        # This would need to be implemented in the ESP32 firmware
        # For now, return False to indicate the operation is not supported
        print("ESP32DeviceController: Fingerprint clearing not implemented in firmware yet")
        return False
