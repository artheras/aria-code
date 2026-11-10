"""Writing firmware and commanding a robot are L4: always asked, never undone by a checkpoint."""
import pytest

from aria_code.safety.risk import CAPABILITIES, approval_requirement, assess_command, unknown_capabilities

FLASH = [
    "esptool.py --port /dev/ttyUSB0 write_flash 0x0 fw.bin", "esptool erase_flash", "dfu-util -D fw.bin",
    "openocd -f board.cfg -c 'program fw.elf verify reset exit'", "avrdude -p m328p -U flash:w:fw.hex",
    "st-flash write fw.bin 0x8000000", "picotool load fw.uf2", "fastboot flash boot boot.img",
    "pio run -t upload", "west flash", "idf.py flash", "sudo esptool.py write_flash 0 fw.bin",
]
ACTUATE = [
    "ros2 topic pub /cmd_vel geometry_msgs/msg/Twist '{linear: {x: 0.5}}'", "ros2 service call /reset std_srvs/srv/Empty",
    "ros2 action send_goal /navigate nav2_msgs/action/NavigateToPose '{}'", "ros2 param set /driver max_speed 2.0",
    "ros2 lifecycle set /driver activate",
]
SAFE = [
    "esptool.py chip_id", "dfu-util -l", "openocd -f board.cfg", "avrdude -p m328p", "st-flash --version",
    "picotool info", "fastboot devices", "pio run", "west build", "idf.py build",
    "ros2 topic echo /scan", "ros2 topic list", "ros2 node list", "ros2 param get /driver max_speed",
    "colcon build", "colcon test", "ros2 launch my_robot sim.launch.py",
]


@pytest.mark.parametrize("command", FLASH)
def test_writing_firmware_is_critical_and_not_reversible(command):
    a = assess_command(command)
    assert a.level == 4 and "hardware.flash" in a.capabilities and not a.reversible
    assert "Device firmware" in a.systems and a.summary == "Flash firmware"


@pytest.mark.parametrize("command", ACTUATE)
def test_commanding_a_running_robot_is_critical(command):
    a = assess_command(command)
    assert a.level == 4 and "actuator.command" in a.capabilities and not a.reversible
    assert "Robot / actuators" in a.systems and any("move hardware" in r for r in a.reasons)


@pytest.mark.parametrize("command", SAFE)
def test_reading_building_and_launching_are_not_critical(command):
    a = assess_command(command)
    assert a.level < 4 and not ({"hardware.flash", "actuator.command"} & a.capabilities)


@pytest.mark.parametrize("command", FLASH[:2] + ACTUATE[:2])
def test_physical_actions_always_ask_even_when_risk_mode_would_auto_approve(command):
    a = assess_command(command)
    assert approval_requirement(a, mode="risk", auto_level=2) == "always"


def test_a_contract_can_forbid_the_physical_capabilities_and_a_typo_is_still_reported():
    assert {"hardware.flash", "actuator.command"} <= CAPABILITIES
    assert unknown_capabilities(["hardware.flash", "actuator.command"]) == []
    assert unknown_capabilities(["hardware.flsh"]) == ["hardware.flsh"]


def test_the_risk_card_for_a_flash_names_the_device_firmware():
    from aria_code.ui.render.output import format_risk_card

    text = "\n".join(line for _style, line in format_risk_card(assess_command("dfu-util -D fw.bin")))
    assert "L4 critical" in text and "not reversible" in text and "Device firmware" in text
