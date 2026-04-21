import os
import logging
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Set up logging for debugging Raspberry Pi connectivity issues
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
        self.base_url = base_url or os.getenv("ESP32_BASE_URL", "http://192.168.4.1")
        fallback_urls = os.getenv("ESP32_FALLBACK_URLS", "")
        self.fallback_urls = [url.strip() for url in fallback_urls.split(",") if url.strip()]
        self.device_urls = [self.base_url] + self.fallback_urls
        self.timeout = float(os.getenv("ESP32_TIMEOUT", timeout))
        self.connect_timeout = float(os.getenv("ESP32_CONNECT_TIMEOUT", 5.0))
        self.retries = int(os.getenv("ESP32_MAX_RETRIES", retries))
        fingerprint_paths = os.getenv("ESP32_FINGERPRINT_PATHS", "/fingerprint/start-enrollment")
        self.fingerprint_paths = [p.strip() for p in fingerprint_paths.split(",") if p.strip()]
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

        print(f"[ESP32] Device URLs: {self.device_urls}")
        print(f"[ESP32] Connect timeout: {self.connect_timeout}s, read timeout: {self.timeout}s")
        print(f"[ESP32] HTTP retries: {self.retries}")
        print(f"[ESP32] Fingerprint paths: {self.fingerprint_paths}")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        last_error = None
        last_url_attempted = None
        for idx, base_url in enumerate(self.device_urls, 1):
            url = f"{base_url}{path}"
            last_url_attempted = url
            try:
                logger.info(f"[ESP32] ({idx}/{len(self.device_urls)}) Attempting {method} {url} (timeout: {self.connect_timeout}s connect, {self.timeout}s read)")
                if method == "POST":
                    logger.debug(f"[ESP32] POST payload={payload}")
                    r = self.session.post(url, json=payload or {}, timeout=(self.connect_timeout, self.timeout))
                else:
                    r = self.session.get(url, timeout=(self.connect_timeout, self.timeout))

                r.raise_for_status()
                logger.info(f"[ESP32] ✓ Success {r.status_code} from {url}")
                return r.json()
            except requests.exceptions.HTTPError as e:
                logger.error(f"[ESP32] ✗ HTTP ERROR from {url}: {r.status_code} {r.reason}")
                last_error = e
                if r.status_code == 404:
                    continue
                raise
            except requests.exceptions.ConnectTimeout as e:
                logger.error(f"[ESP32] ✗ CONNECTION TIMEOUT to {url} ({self.connect_timeout}s)")
                logger.error(f"[ESP32]    → ESP32 may be powered off, unreachable, or not running HTTP server")
                logger.error(f"[ESP32]    → If IP changed, update ESP32_BASE_URL in .env file")
                last_error = e
                continue
            except requests.exceptions.ReadTimeout as e:
                logger.error(f"[ESP32] ✗ READ TIMEOUT from {url} ({self.timeout}s)")
                logger.error(f"[ESP32]    → ESP32 is reachable but not responding in time")
                logger.error(f"[ESP32]    → Try increasing ESP32_TIMEOUT in .env file")
                last_error = e
                continue
            except requests.exceptions.ConnectionError as e:
                logger.error(f"[ESP32] ✗ CONNECTION ERROR to {url}")
                logger.error(f"[ESP32]    → ESP32 is unreachable on the network")
                logger.error(f"[ESP32]    → Actions to take:")
                logger.error(f"[ESP32]       1. Verify ESP32 is powered on and booted")
                logger.error(f"[ESP32]       2. Confirm ESP32 is connected to the correct WiFi network")
                logger.error(f"[ESP32]       3. Get ESP32's actual IP address from WiFi router or serial monitor")
                logger.error(f"[ESP32]       4. Update ESP32_BASE_URL and ESP32_FALLBACK_URLS in .env")
                logger.error(f"[ESP32]       5. Restart the backend server after updating .env")
                logger.error(f"[ESP32]    → Error details: {str(e)}")
                last_error = e
                continue
            except Exception as e:
                logger.error(f"[ESP32] ✗ UNEXPECTED ERROR contacting {url}: {type(e).__name__}: {e}")
                last_error = e
                continue

        if last_error:
            logger.critical(f"[ESP32] ✗✗✗ ALL CONNECTION ATTEMPTS FAILED ✗✗✗")
            logger.critical(f"[ESP32] Tried {len(self.device_urls)} URL(s): {', '.join(self.device_urls)}")
            logger.critical(f"[ESP32] Last error: {type(last_error).__name__}: {last_error}")
            raise last_error
        logger.critical(f"[ESP32] ✗ Failed to contact ESP32 at any configured URL: {self.device_urls}")
        raise RuntimeError(f"Failed to contact ESP32 at any configured URL: {self.device_urls}")

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
