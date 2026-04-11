import os
from dataclasses import dataclass

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
    def __init__(self, base_url: str | None = None, timeout: float = 10.0, retries: int = 2):
        self.base_url = base_url or os.getenv("ESP32_BASE_URL", "http://192.168.4.1")
        self.timeout = timeout
        self.connect_timeout = float(os.getenv("ESP32_CONNECT_TIMEOUT", 3.0))
        self.retries = int(os.getenv("ESP32_MAX_RETRIES", retries))
        self.session = requests.Session()

        retry_strategy = Retry(
            total=self.retries,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            backoff_factor=0.5,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        print(f"[ESP32] Base URL: {self.base_url}")
        print(f"[ESP32] Connect timeout: {self.connect_timeout}s, read timeout: {self.timeout}s")
        print(f"[ESP32] HTTP retries: {self.retries}")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            if method == "POST":
                print(f"[ESP32] POST {url} payload={payload}")
                r = self.session.post(url, json=payload or {}, timeout=(self.connect_timeout, self.timeout))
            else:
                print(f"[ESP32] GET {url}")
                r = self.session.get(url, timeout=(self.connect_timeout, self.timeout))

            r.raise_for_status()
            print(f"[ESP32] Response {r.status_code} from {url}")
            return r.json()
        except requests.exceptions.Timeout as e:
            print(f"[ESP32] TIMEOUT contacting {url}: {e}")
            raise
        except requests.exceptions.ConnectionError as e:
            print(f"[ESP32] CONNECTION ERROR contacting {url}: {e}")
            raise
        except Exception as e:
            print(f"[ESP32] ERROR contacting {url}: {type(e).__name__}: {e}")
            raise

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
        return self._post("/fingerprint/start-enrollment")

    def clear_fingerprint_templates(self) -> bool:
        """Clear all fingerprint templates from device."""
        # Note: ESP32 firmware currently doesn't have HTTP server endpoints
        # This would need to be implemented in the ESP32 firmware
        # For now, return False to indicate the operation is not supported
        print("ESP32DeviceController: Fingerprint clearing not implemented in firmware yet")
        return False
