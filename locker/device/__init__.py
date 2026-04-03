from os import getenv

from .mock import MockDeviceController
from .esp32 import ESP32DeviceController


def get_device_controller():
    mode = getenv("LOCKER_DEVICE_MODE", "mock").lower()
    if mode == "esp32":
        return ESP32DeviceController()
    return MockDeviceController()


__all__ = ["get_device_controller", "MockDeviceController", "ESP32DeviceController"]

