from contextlib import ExitStack
from typing import Type
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from afancontrol import fantest
from afancontrol.fantest import (
    CSVMeasurementsOutput,
    HumanMeasurementsOutput,
    MeasurementsOutput,
    fantest as main,
    run_fantest,
)
from afancontrol.pwmfan import (
    BasePWMFan,
    FanInputDevice,
    LinuxPWMFan,
    PWMDevice,
    PWMValue,
)


def test_main_smoke(temp_path):
    pwm_path = temp_path / "pwm2"
    pwm_path.write_text("")
    fan_input_path = temp_path / "fan2_input"
    fan_input_path.write_text("")

    with ExitStack() as stack:
        mocked_fantest = stack.enter_context(patch.object(fantest, "run_fantest"))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--fan-type",
                "linux",
                "--linux-fan-pwm",
                # "/sys/class/hwmon/hwmon0/device/pwm2",
                str(pwm_path),  # click verifies that this file exists
                "--linux-fan-input",
                # "/sys/class/hwmon/hwmon0/device/fan2_input",
                str(fan_input_path),  # click verifies that this file exists
                "--output-format",
                "human",
                "--direction",
                "increase",
                "--pwm-step-size",
                "accurate",
            ],
        )

        print(result.output)
        assert result.exit_code == 0

        assert mocked_fantest.call_count == 1

        args, kwargs = mocked_fantest.call_args
        assert not args
        assert kwargs.keys() == {"fan", "pwm_step_size", "output"}
        assert kwargs["fan"] == LinuxPWMFan(
            PWMDevice(str(pwm_path)), FanInputDevice(str(fan_input_path))
        )
        assert kwargs["pwm_step_size"] == 5
        assert isinstance(kwargs["output"], HumanMeasurementsOutput)


@pytest.mark.parametrize("pwm_step_size", [5, -5])
@pytest.mark.parametrize("output_cls", [HumanMeasurementsOutput, CSVMeasurementsOutput])
def test_fantest(output_cls: Type[MeasurementsOutput], pwm_step_size: PWMValue):
    mocked_fan = MagicMock(spec=BasePWMFan)
    mocked_fan.min_pwm = 0
    mocked_fan.max_pwm = 255
    output = output_cls()

    with ExitStack() as stack:
        mocked_sleep = stack.enter_context(patch.object(fantest, "sleep"))
        mocked_fan.get_speed.return_value = 999

        run_fantest(fan=mocked_fan, pwm_step_size=pwm_step_size, output=output)

        assert mocked_fan.set.call_count == (255 // abs(pwm_step_size)) + 1
        assert mocked_fan.get_speed.call_count == (255 // abs(pwm_step_size))
        assert mocked_sleep.call_count == (255 // abs(pwm_step_size)) + 1

        if pwm_step_size > 0:
            # increase
            expected_set = [0] + list(range(0, 255, pwm_step_size))
        else:
            # decrease
            expected_set = [255] + list(range(255, 0, pwm_step_size))
        assert [pwm for (pwm,), _ in mocked_fan.set.call_args_list] == expected_set
