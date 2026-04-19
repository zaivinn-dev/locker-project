from os import getenv

from .esp32 import ESP32DeviceController


def get_device_controller():
    mode = getenv("LOCKER_DEVICE_MODE", "esp32").lower()
    if mode == "esp32":
        return ESP32DeviceController()
    raise ValueError(f"Unknown LOCKER_DEVICE_MODE: {mode}. Only 'esp32' is supported.")


__all__ = ["get_device_controller", "ESP32DeviceController"]

