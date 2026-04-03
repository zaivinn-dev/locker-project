from dataclasses import dataclass


@dataclass
class LockerState:
    locker_id: int
    locked: bool = True
    item_detected: bool = False


class MockDeviceController:
    """
    Temporary device controller used during UI/backend development.
    Later you can replace this with an ESP32 controller (serial/MQTT/HTTP).
    """

    def __init__(self) -> None:
        self._lockers: dict[int, LockerState] = {1: LockerState(locker_id=1)}

    def get_locker(self, locker_id: int) -> LockerState:
        if locker_id not in self._lockers:
            self._lockers[locker_id] = LockerState(locker_id=locker_id)
        return self._lockers[locker_id]

    def unlock(self, locker_id: int) -> LockerState:
        st = self.get_locker(locker_id)
        st.locked = False
        return st

    def lock(self, locker_id: int) -> LockerState:
        st = self.get_locker(locker_id)
        st.locked = True
        return st

