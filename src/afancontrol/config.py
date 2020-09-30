import configparser
from pathlib import Path
from typing import (
    Dict,
    Mapping,
    NamedTuple,
    NewType,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

from afancontrol.arduino import ArduinoConnection, ArduinoName
from afancontrol.configparser import ConfigParserSection
from afancontrol.filters import (
    MovingMedianFilter,
    MovingQuantileFilter,
    NullFilter,
    TempFilter,
)
from afancontrol.logger import logger
from afancontrol.pwmfan import (
    ArduinoFanPWMRead,
    ArduinoFanPWMWrite,
    ArduinoFanSpeed,
    BaseFanPWMRead,
    BaseFanPWMWrite,
    BaseFanSpeed,
    FreeIPMIFanSpeed,
    LinuxFanPWMRead,
    LinuxFanPWMWrite,
    LinuxFanSpeed,
    PWMValue,
)
from afancontrol.pwmfannorm import PWMFanNorm, ReadonlyPWMFanNorm
from afancontrol.temp import CommandTemp, FileTemp, HDDTemp, Temp

DEFAULT_CONFIG = "/etc/afancontrol/afancontrol.conf"
DEFAULT_PIDFILE = "/run/afancontrol.pid"
DEFAULT_INTERVAL = 5
DEFAULT_FANS_SPEED_CHECK_INTERVAL = 3
DEFAULT_HDDTEMP = "hddtemp"
DEFAULT_REPORT_CMD = (
    'printf "Subject: %s\nTo: %s\n\n%b"'
    ' "afancontrol daemon report: %REASON%" root "%MESSAGE%"'
    " | sendmail -t"
)

DEFAULT_FAN_TYPE = "linux"
DEFAULT_PWM_LINE_START = 100
DEFAULT_PWM_LINE_END = 240

DEFAULT_NEVER_STOP = True

DEFAULT_WINDOW_SIZE = 3

FilterName = NewType("FilterName", str)
TempName = NewType("TempName", str)
FanName = NewType("FanName", str)
ReadonlyFanName = NewType("ReadonlyFanName", str)
AnyFanName = Union[FanName, ReadonlyFanName]
MappingName = NewType("MappingName", str)

T = TypeVar("T")


class FanSpeedModifier(NamedTuple):
    fan: FanName
    modifier: float  # [0..1]


class FansTempsRelation(NamedTuple):
    temps: Sequence[TempName]
    fans: Sequence[FanSpeedModifier]


class AlertCommands(NamedTuple):
    enter_cmd: Optional[str]
    leave_cmd: Optional[str]


class Actions(NamedTuple):
    panic: AlertCommands
    threshold: AlertCommands


class TriggerConfig(NamedTuple):
    global_commands: Actions
    temp_commands: Mapping[TempName, Actions]


class DaemonCLIConfig(NamedTuple):
    pidfile: Optional[str]
    logfile: Optional[str]
    exporter_listen_host: Optional[str]


class DaemonConfig(NamedTuple):
    pidfile: Optional[str]
    logfile: Optional[str]
    interval: int
    exporter_listen_host: Optional[str]


class FilteredTemp(NamedTuple):
    temp: Temp
    filter: TempFilter


class ParsedConfig(NamedTuple):
    daemon: DaemonConfig
    report_cmd: str
    triggers: TriggerConfig
    arduino_connections: Mapping[ArduinoName, ArduinoConnection]
    fans: Mapping[FanName, PWMFanNorm]
    readonly_fans: Mapping[ReadonlyFanName, ReadonlyPWMFanNorm]
    temps: Mapping[TempName, FilteredTemp]
    mappings: Mapping[MappingName, FansTempsRelation]


def parse_config(config_path: Path, daemon_cli_config: DaemonCLIConfig) -> ParsedConfig:
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read_string(config_path.read_text(), source=str(config_path))
    except Exception as e:
        raise RuntimeError("Unable to parse %s:\n%s" % (config_path, e))

    daemon, hddtemp = _parse_daemon(config, daemon_cli_config)
    report_cmd, global_commands = _parse_actions(config)
    arduino_connections = _parse_arduino_connections(config)
    filters = _parse_filters(config)
    temps, temp_commands = _parse_temps(config, hddtemp, filters)
    fans = _parse_fans(config, arduino_connections)
    readonly_fans = _parse_readonly_fans(config, arduino_connections)
    _check_fans_namespace(fans, readonly_fans)
    mappings = _parse_mappings(config, fans, temps)

    return ParsedConfig(
        daemon=daemon,
        report_cmd=report_cmd,
        triggers=TriggerConfig(
            global_commands=global_commands, temp_commands=temp_commands
        ),
        arduino_connections=arduino_connections,
        fans=fans,
        readonly_fans=readonly_fans,
        temps=temps,
        mappings=mappings,
    )


def first_not_none(*parts: Optional[T]) -> Optional[T]:
    for part in parts:
        if part is not None:
            return part
    return parts[-1]  # None


def _parse_daemon(
    config: configparser.ConfigParser, daemon_cli_config: DaemonCLIConfig
) -> Tuple[DaemonConfig, str]:
    daemon: ConfigParserSection[str] = ConfigParserSection(config["daemon"])

    pidfile = first_not_none(
        daemon_cli_config.pidfile, daemon.get("pidfile", fallback=DEFAULT_PIDFILE)
    )
    if pidfile is not None and not pidfile.strip():
        pidfile = None

    logfile = first_not_none(
        daemon_cli_config.logfile, daemon.get("logfile", fallback=None)
    )

    interval = daemon.getint("interval", fallback=DEFAULT_INTERVAL)

    exporter_listen_host = first_not_none(
        daemon_cli_config.exporter_listen_host,
        daemon.get("exporter_listen_host", fallback=None),
    )

    hddtemp = daemon.get("hddtemp", fallback=DEFAULT_HDDTEMP)

    daemon.ensure_no_unused_keys()

    return (
        DaemonConfig(
            pidfile=pidfile,
            logfile=logfile,
            interval=interval,
            exporter_listen_host=exporter_listen_host,
        ),
        hddtemp,
    )


def _parse_actions(config: configparser.ConfigParser) -> Tuple[str, Actions]:
    actions: ConfigParserSection[str] = ConfigParserSection(config["actions"])

    report_cmd = actions.get("report_cmd", fallback=DEFAULT_REPORT_CMD)
    assert report_cmd is not None

    panic = AlertCommands(
        enter_cmd=actions.get("panic_enter_cmd", fallback=None),
        leave_cmd=actions.get("panic_leave_cmd", fallback=None),
    )

    threshold = AlertCommands(
        enter_cmd=actions.get("threshold_enter_cmd", fallback=None),
        leave_cmd=actions.get("threshold_leave_cmd", fallback=None),
    )

    actions.ensure_no_unused_keys()

    return report_cmd, Actions(panic=panic, threshold=threshold)


def _parse_arduino_connections(
    config: configparser.ConfigParser,
) -> Mapping[ArduinoName, ArduinoConnection]:
    arduino_connections: Dict[ArduinoName, ArduinoConnection] = {}
    for section_name in config.sections():
        section_name_parts = section_name.split(":", 1)

        if section_name_parts[0].strip().lower() != "arduino":
            continue

        arduino_name = ArduinoName(section_name_parts[1].strip())
        arduino = ConfigParserSection(config[section_name], arduino_name)

        if arduino_name in arduino_connections:
            raise RuntimeError(
                "Duplicate arduino section declaration for '%s'" % arduino_name
            )
        arduino_connections[arduino_name] = ArduinoConnection.from_configparser(arduino)

        arduino.ensure_no_unused_keys()

    # Empty arduino_connections is ok
    return arduino_connections


def _parse_filters(
    config: configparser.ConfigParser,
) -> Mapping[FilterName, TempFilter]:
    filters: Dict[FilterName, TempFilter] = {}
    for section_name in config.sections():
        section_name_parts = section_name.split(":", 1)

        if section_name_parts[0].strip().lower() != "filter":
            continue

        filter_name = FilterName(section_name_parts[1].strip())
        filter = ConfigParserSection(config[section_name], filter_name)

        filter_type = filter["type"]

        if filter_type == "moving_median":
            window_size = filter.getint("window_size", fallback=DEFAULT_WINDOW_SIZE)

            f: TempFilter = MovingMedianFilter(window_size=window_size)
        elif filter_type == "moving_quantile":
            window_size = filter.getint("window_size", fallback=DEFAULT_WINDOW_SIZE)
            quantile = filter.getfloat("quantile")
            f = MovingQuantileFilter(quantile=quantile, window_size=window_size)
        else:
            raise RuntimeError(
                "Unsupported filter type '%s' for filter '%s'. "
                "Supported types: `moving_median`, `moving_quantile`."
                % (filter_type, filter_name)
            )

        filter.ensure_no_unused_keys()

        if filter_name in filters:
            raise RuntimeError(
                "Duplicate filter section declaration for '%s'" % filter_name
            )
        filters[filter_name] = f

    # Empty filters is ok
    return filters


def _parse_temps(
    config: configparser.ConfigParser,
    hddtemp: str,
    filters: Mapping[FilterName, TempFilter],
) -> Tuple[Mapping[TempName, FilteredTemp], Mapping[TempName, Actions]]:
    temps: Dict[TempName, FilteredTemp] = {}
    temp_commands: Dict[TempName, Actions] = {}
    for section_name in config.sections():
        section_name_parts = section_name.split(":", 1)

        if section_name_parts[0].strip().lower() != "temp":
            continue

        temp_name = TempName(section_name_parts[1].strip())
        temp = ConfigParserSection(config[section_name], temp_name)

        actions_panic = AlertCommands(
            enter_cmd=temp.get("panic_enter_cmd", fallback=None),
            leave_cmd=temp.get("panic_leave_cmd", fallback=None),
        )

        actions_threshold = AlertCommands(
            enter_cmd=temp.get("threshold_enter_cmd", fallback=None),
            leave_cmd=temp.get("threshold_leave_cmd", fallback=None),
        )

        type = temp["type"]

        if type == "file":
            t = FileTemp.from_configparser(temp)  # type: Temp
        elif type == "hdd":
            t = HDDTemp.from_configparser(temp, hddtemp=hddtemp)
        elif type == "exec":
            t = CommandTemp.from_configparser(temp)
        else:
            raise RuntimeError(
                "Unsupported temp type '%s' for temp '%s'" % (type, temp_name)
            )

        filter_name = temp.get("filter", fallback=None)

        if filter_name is None:
            filter: TempFilter = NullFilter()
        else:
            filter = filters[FilterName(filter_name.strip())].copy()

        temp.ensure_no_unused_keys()

        if temp_name in temps:
            raise RuntimeError(
                "Duplicate temp section declaration for '%s'" % temp_name
            )
        temps[temp_name] = FilteredTemp(temp=t, filter=filter)
        temp_commands[temp_name] = Actions(
            panic=actions_panic, threshold=actions_threshold
        )

    return temps, temp_commands


def _parse_fans(
    config: configparser.ConfigParser,
    arduino_connections: Mapping[ArduinoName, ArduinoConnection],
) -> Mapping[FanName, PWMFanNorm]:
    fans: Dict[FanName, PWMFanNorm] = {}
    for section_name in config.sections():
        section_name_parts = section_name.split(":", 1)

        if section_name_parts[0].strip().lower() != "fan":
            continue

        fan_name = FanName(section_name_parts[1].strip())
        fan = ConfigParserSection(config[section_name], fan_name)

        fan_type = fan.get("type", fallback=DEFAULT_FAN_TYPE)

        if fan_type == "linux":
            fan_speed: BaseFanSpeed = LinuxFanSpeed.from_configparser(fan)
            pwm_read: BaseFanPWMRead = LinuxFanPWMRead.from_configparser(fan)
            pwm_write: BaseFanPWMWrite = LinuxFanPWMWrite.from_configparser(fan)
        elif fan_type == "arduino":
            fan_speed = ArduinoFanSpeed.from_configparser(fan, arduino_connections)
            pwm_read = ArduinoFanPWMRead.from_configparser(fan, arduino_connections)
            pwm_write = ArduinoFanPWMWrite.from_configparser(fan, arduino_connections)
        else:
            raise ValueError(
                "Unsupported FAN type %s. Supported ones are "
                "`linux` and `arduino`." % fan_type
            )

        never_stop = fan.getboolean("never_stop", fallback=DEFAULT_NEVER_STOP)

        pwm_line_start = PWMValue(
            fan.getint("pwm_line_start", fallback=DEFAULT_PWM_LINE_START)
        )

        pwm_line_end = PWMValue(
            fan.getint("pwm_line_end", fallback=DEFAULT_PWM_LINE_END)
        )

        for pwm_value in (pwm_line_start, pwm_line_end):
            if not (pwm_read.min_pwm <= pwm_value <= pwm_read.max_pwm):
                raise RuntimeError(
                    "Incorrect PWM value '%s' for fan '%s': it must be within [%s;%s]"
                    % (pwm_value, fan_name, pwm_read.min_pwm, pwm_read.max_pwm)
                )
        if pwm_line_start >= pwm_line_end:
            raise RuntimeError(
                "`pwm_line_start` PWM value must be less than `pwm_line_end` for fan '%s'"
                % (fan_name,)
            )

        fan.ensure_no_unused_keys()

        if fan_name in fans:
            raise RuntimeError("Duplicate fan section declaration for '%s'" % fan_name)
        fans[fan_name] = PWMFanNorm(
            fan_speed,
            pwm_read,
            pwm_write,
            pwm_line_start=pwm_line_start,
            pwm_line_end=pwm_line_end,
            never_stop=never_stop,
        )

    return fans


def _parse_readonly_fans(
    config: configparser.ConfigParser,
    arduino_connections: Mapping[ArduinoName, ArduinoConnection],
) -> Mapping[ReadonlyFanName, ReadonlyPWMFanNorm]:
    readonly_fans: Dict[ReadonlyFanName, ReadonlyPWMFanNorm] = {}
    for section_name in config.sections():
        section_name_parts = section_name.split(":", 1)

        if section_name_parts[0].strip().lower() != "readonly_fan":
            continue

        fan_name = ReadonlyFanName(section_name_parts[1].strip())
        fan = ConfigParserSection(config[section_name], fan_name)

        fan_type = fan.get("type", fallback=DEFAULT_FAN_TYPE)

        if fan_type == "linux":
            fan_speed: BaseFanSpeed = LinuxFanSpeed.from_configparser(fan)

            pwm_read: Optional[BaseFanPWMRead] = None
            if "pwm" in fan:
                pwm_read = LinuxFanPWMRead.from_configparser(fan)
        elif fan_type == "arduino":
            fan_speed = ArduinoFanSpeed.from_configparser(fan, arduino_connections)
            pwm_read = None
            if "pwm_pin" in fan:
                pwm_read = ArduinoFanPWMRead.from_configparser(fan, arduino_connections)
        elif fan_type == "freeipmi":
            fan_speed = FreeIPMIFanSpeed.from_configparser(fan)
            pwm_read = None
        else:
            raise ValueError(
                "Unsupported FAN type %s. Supported ones are "
                "`linux`, `arduino`, `freeipmi`." % fan_type
            )

        if fan_name in readonly_fans:
            raise RuntimeError(
                "Duplicate readonly_fan section declaration for '%s'" % fan_name
            )
        readonly_fans[fan_name] = ReadonlyPWMFanNorm(fan_speed, pwm_read)

    return readonly_fans


def _check_fans_namespace(
    fans: Mapping[FanName, PWMFanNorm],
    readonly_fans: Mapping[ReadonlyFanName, ReadonlyPWMFanNorm],
) -> None:
    common_keys = fans.keys() & readonly_fans.keys()
    if common_keys:
        raise RuntimeError(
            "Duplicate fan names has been found between `fan` "
            "and `readonly_fan` sections: %r" % (list(common_keys),)
        )


def _parse_mappings(
    config: configparser.ConfigParser,
    fans: Mapping[FanName, PWMFanNorm],
    temps: Mapping[TempName, FilteredTemp],
) -> Mapping[MappingName, FansTempsRelation]:

    mappings: Dict[MappingName, FansTempsRelation] = {}
    for section_name in config.sections():
        section_name_parts = section_name.split(":", 1)

        if section_name_parts[0].lower() != "mapping":
            continue

        mapping_name = MappingName(section_name_parts[1])
        mapping = ConfigParserSection(config[section_name], mapping_name)

        # temps:

        mapping_temps = [
            TempName(temp_name.strip()) for temp_name in mapping["temps"].split(",")
        ]
        mapping_temps = [s for s in mapping_temps if s]
        if not mapping_temps:
            raise RuntimeError(
                "Temps must not be empty in the '%s' mapping" % mapping_name
            )
        for temp_name in mapping_temps:
            if temp_name not in temps:
                raise RuntimeError(
                    "Unknown temp '%s' in mapping '%s'" % (temp_name, mapping_name)
                )
        if len(mapping_temps) != len(set(mapping_temps)):
            raise RuntimeError(
                "There are duplicate temps in mapping '%s'" % mapping_name
            )

        # fans:

        fans_with_speed = [
            fan_with_speed.strip() for fan_with_speed in mapping["fans"].split(",")
        ]
        fans_with_speed = [s for s in fans_with_speed if s]

        fan_speed_pairs = [
            fan_with_speed.split("*") for fan_with_speed in fans_with_speed
        ]
        for fan_speed_pair in fan_speed_pairs:
            if len(fan_speed_pair) not in (1, 2):
                raise RuntimeError(
                    "Invalid fan specification '%s' in mapping '%s'"
                    % (fan_speed_pair, mapping_name)
                )
        mapping_fans = [
            FanSpeedModifier(
                fan=FanName(fan_speed_pair[0].strip()),
                modifier=(
                    float(
                        fan_speed_pair[1].strip() if len(fan_speed_pair) == 2 else 1.0
                    )
                ),
            )
            for fan_speed_pair in fan_speed_pairs
        ]
        for fan_speed_modifier in mapping_fans:
            if fan_speed_modifier.fan not in fans:
                raise RuntimeError(
                    "Unknown fan '%s' in mapping '%s'"
                    % (fan_speed_modifier.fan, mapping_name)
                )
            if not (0 < fan_speed_modifier.modifier <= 1.0):
                raise RuntimeError(
                    "Invalid fan modifier '%s' in mapping '%s' for fan '%s': "
                    "the allowed range is (0.0;1.0]."
                    % (
                        fan_speed_modifier.modifier,
                        mapping_name,
                        fan_speed_modifier.fan,
                    )
                )
        if len(mapping_fans) != len(
            set(fan_speed_modifier.fan for fan_speed_modifier in mapping_fans)
        ):
            raise RuntimeError(
                "There are duplicate fans in mapping '%s'" % mapping_name
            )

        mapping.ensure_no_unused_keys()

        if mapping_name in fans:
            raise RuntimeError(
                "Duplicate mapping section declaration for '%s'" % mapping_name
            )
        mappings[mapping_name] = FansTempsRelation(
            temps=mapping_temps, fans=mapping_fans
        )

    unused_temps = set(temps.keys())
    unused_fans = set(fans.keys())
    for relation in mappings.values():
        unused_temps -= set(relation.temps)
        unused_fans -= set(
            fan_speed_modifier.fan for fan_speed_modifier in relation.fans
        )
    if unused_temps:
        logger.warning(
            "The following temps are defined but not used in any mapping: %s",
            unused_temps,
        )
    if unused_fans:
        raise RuntimeError(
            "The following fans are defined but not used in any mapping: %s"
            % unused_fans
        )
    return mappings
