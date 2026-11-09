"""Minimal PCA9685 driver over I2C (smbus2), with a mock fallback for development."""
import time

MODE1 = 0x00
MODE2 = 0x01
PRESCALE = 0xFE
LED0_ON_L = 0x06
OSC_HZ = 25_000_000

MODE1_SLEEP = 0x10
MODE1_AI = 0x20
MODE1_RESTART = 0x80
MODE2_OUTDRV = 0x04


class PCA9685:
    def __init__(self, bus: int = 0, address: int = 0x40, freq: float = 50.0):
        from smbus2 import SMBus

        self.address = address
        self.bus = SMBus(bus)
        self.freq = freq
        self.bus.write_byte_data(address, MODE2, MODE2_OUTDRV)
        self.bus.write_byte_data(address, MODE1, MODE1_AI)
        time.sleep(0.005)
        self.set_frequency(freq)
        self.all_off()

    def set_frequency(self, hz: float) -> float:
        hz = max(24.0, min(1526.0, hz))
        prescale = max(3, min(255, round(OSC_HZ / (4096 * hz)) - 1))
        old = self.bus.read_byte_data(self.address, MODE1)
        self.bus.write_byte_data(self.address, MODE1, (old & 0x7F) | MODE1_SLEEP)
        self.bus.write_byte_data(self.address, PRESCALE, prescale)
        self.bus.write_byte_data(self.address, MODE1, old & ~MODE1_SLEEP)
        time.sleep(0.005)
        self.bus.write_byte_data(self.address, MODE1, (old & ~MODE1_SLEEP) | MODE1_RESTART | MODE1_AI)
        self.freq = OSC_HZ / (4096 * (prescale + 1))
        return self.freq

    def set_duty(self, channel: int, duty: float) -> None:
        """duty in 0..1. 0 = fully off, 1 = fully on."""
        base = LED0_ON_L + 4 * channel
        if duty <= 0:
            data = [0, 0, 0, 0x10]  # full off
        elif duty >= 1:
            data = [0, 0x10, 0, 0]  # full on
        else:
            off = int(round(duty * 4096))
            data = [0, 0, off & 0xFF, off >> 8]
        self.bus.write_i2c_block_data(self.address, base, data)

    def all_off(self) -> None:
        # ALL_LED_OFF_H (0xFD) bit 4 = full off for every channel
        self.bus.write_byte_data(self.address, 0xFD, 0x10)

    def close(self) -> None:
        try:
            self.all_off()
        finally:
            self.bus.close()


class MockPCA9685:
    """Stand-in used when no I2C hardware is present."""

    def __init__(self, bus: int = 0, address: int = 0x40, freq: float = 50.0):
        self.freq = freq
        self.duty = [0.0] * 16
        print("[pca9685] using MOCK device (no hardware)")

    def set_frequency(self, hz: float) -> float:
        self.freq = max(24.0, min(1526.0, hz))
        return self.freq

    def set_duty(self, channel: int, duty: float) -> None:
        self.duty[channel] = duty

    def all_off(self) -> None:
        self.duty = [0.0] * 16

    def close(self) -> None:
        pass


def open_device(bus: int, address: int, freq: float, mock: bool = False):
    if mock:
        return MockPCA9685(bus, address, freq)
    try:
        return PCA9685(bus, address, freq)
    except (ImportError, OSError) as e:
        raise RuntimeError(f"cannot open PCA9685 on /dev/i2c-{bus} @ 0x{address:02x}: {e}") from e
