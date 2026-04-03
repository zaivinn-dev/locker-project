import os
from dataclasses import dataclass

import requests


@dataclass
class LockerState:
    locker_id: int
    locked: bool
    item_detected: bool


class ESP32DeviceController:
    def __init__(self, base_url: str | None = None, timeout: float = 3.0):
        self.base_url = base_url or os.getenv("ESP32_BASE_URL", "http://192.168.4.1")
        self.timeout = timeout

    def _post(self, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        r = requests.post(url, json=payload or {}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        r = requests.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

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
