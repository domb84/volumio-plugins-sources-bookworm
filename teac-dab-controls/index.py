import json
import logging
import queue
import signal
import threading
from pathlib import Path
from typing import Any, Dict, Tuple

from includes import api, controls, menu_manager, volumio

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
CONFIG_PATH = Path("/data/configuration/user_interface/teac-dab-controls/config.json")

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("Teac DAB controls")
logger.setLevel(logging.DEBUG)

stop_event = threading.Event()


def signal_handler(sig: int, frame: Any) -> None:
    logger.debug("Caught signal: %s", sig)
    stop_event.set()


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.error("Configuration file not found: %s", path)
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_int_field(config: Dict[str, Any], key: str) -> int:
    return int(config[key]["value"])


def parse_button_mapping(value: str) -> Tuple[str, ...]:
    return tuple(map(str, value.split(",")))


def load_button_config(config_data: Dict[str, Any]) -> Dict[str, Tuple[str, ...]]:
    config = {
        "btn_enter": parse_button_mapping(config_data["btn_enter"]["value"]),
        "btn_radio": parse_button_mapping(config_data["btn_radio"]["value"]),
        "btn_spotify": parse_button_mapping(config_data["btn_spotify"]["value"]),
        "btn_info": parse_button_mapping(config_data["btn_info"]["value"]),
        "btn_favourite": parse_button_mapping(config_data["btn_favourite"]["value"]),
        "btn_main_menu": parse_button_mapping(config_data["btn_main_menu"]["value"]),
        "btn_back": parse_button_mapping(config_data["btn_back"]["value"]),
    }
    # Optional so existing configs without the key keep working.
    if "btn_pause" in config_data and config_data["btn_pause"].get("value"):
        config["btn_pause"] = parse_button_mapping(config_data["btn_pause"]["value"])
    if "btn_remove_favourite" in config_data:
        config["btn_remove_favourite"] = parse_button_mapping(config_data["btn_remove_favourite"]["value"])
    if "btn_sleep_timer" in config_data and config_data["btn_sleep_timer"].get("value"):
        config["btn_sleep_timer"] = parse_button_mapping(config_data["btn_sleep_timer"]["value"])
    if "btn_cancel_sleep_timer" in config_data and config_data["btn_cancel_sleep_timer"].get("value"):
        config["btn_cancel_sleep_timer"] = parse_button_mapping(config_data["btn_cancel_sleep_timer"]["value"])
    if "btn_dimmer" in config_data and config_data["btn_dimmer"].get("value"):
        config["btn_dimmer"] = parse_button_mapping(config_data["btn_dimmer"]["value"])
    return config


def load_button_skip_config(config_data: Dict[str, Any]) -> Dict[str, Tuple[str, ...]]:
    return {
        "btn_no_press_channel1": parse_button_mapping(config_data["btn_no_press_channel1"]["value"]),
        "btn_no_press_channel2": parse_button_mapping(config_data["btn_no_press_channel2"]["value"]),
    }


def build_threads(config_data: Dict[str, Any]) -> Tuple[threading.Thread, threading.Thread, threading.Thread, threading.Thread]:
    buttons_clk = parse_int_field(config_data, "buttons_clk")
    buttons_miso = parse_int_field(config_data, "buttons_miso")
    buttons_mosi = parse_int_field(config_data, "buttons_mosi")
    buttons_cs = parse_int_field(config_data, "buttons_cs")
    buttons_channel1 = parse_int_field(config_data, "buttons_channel1")
    buttons_channel2 = parse_int_field(config_data, "buttons_channel2")
    button_poll_rate = parse_int_field(config_data, "button_poll_rate")
    button_debounce_rate = parse_int_field(config_data, "button_debounce_rate")
    button_cooldown_rate = parse_int_field(config_data, "button_cooldown_rate")
    spi_bus = parse_int_field(config_data, "spi_bus")
    spi = bool(config_data["spi"]["value"])

    btn_config = load_button_config(config_data)
    btn_skip_config = load_button_skip_config(config_data)

    rot_enc_A = parse_int_field(config_data, "rot_enc_A")
    rot_enc_B = parse_int_field(config_data, "rot_enc_B")

    lcd_rs = parse_int_field(config_data, "lcd_rs")
    lcd_e = parse_int_field(config_data, "lcd_e")
    lcd_d4 = parse_int_field(config_data, "lcd_d4")
    lcd_d5 = parse_int_field(config_data, "lcd_d5")
    lcd_d6 = parse_int_field(config_data, "lcd_d6")
    lcd_d7 = parse_int_field(config_data, "lcd_d7")

    control_queue: queue.Queue = queue.Queue()
    volumio_queue: queue.Queue = queue.Queue()
    menu_manager_queue: queue.Queue = queue.Queue()

    api_wrapper = api.ApiWrapper(control_queue)

    config = controls.ControlsConfig(
        encA=rot_enc_A,
        encB=rot_enc_B,
        butClk=buttons_clk,
        butDOUT=buttons_miso,
        butDIN=buttons_mosi,
        butCS=buttons_cs,
        but1=buttons_channel1,
        but2=buttons_channel2,
        spi_bus=spi_bus,
        spi=spi,
        btn_config=btn_config,
        btn_skip_config=btn_skip_config,
        button_poll_rate=button_poll_rate,
        button_debounce_rate=button_debounce_rate,
        button_cooldown_rate=button_cooldown_rate,
    )

    t1 = threading.Thread(
        target=controls.Controls,
        args=(
            control_queue,
            config,
            stop_event,
        ),
        name="ControlsThread",
    )

    t2 = threading.Thread(
        target=menu_manager.MenuManager,
        args=(
            control_queue,
            volumio_queue,
            menu_manager_queue,
            lcd_rs,
            lcd_e,
            lcd_d4,
            lcd_d5,
            lcd_d6,
            lcd_d7,
            stop_event,
        ),
        name="MenuManagerThread",
    )

    t3 = threading.Thread(
        target=volumio.Volumio,
        args=(volumio_queue, menu_manager_queue, stop_event),
        name="VolumioThread",
    )

    t4 = threading.Thread(
        target=api_wrapper.run_app,
        args=("0.0.0.0", 8889, stop_event),
        name="ApiThread",
    )

    return t1, t2, t3, t4


def main() -> None:
    logger.debug("Registering signal handler")
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        config_data = load_config(CONFIG_PATH)
        logger.info("Loaded config from %s", CONFIG_PATH)
    except FileNotFoundError:
        logger.error("Unable to find configuration; exiting")
        raise SystemExit(1)

    threads = build_threads(config_data)
    for thread in threads:
        # Daemonise so a stuck worker can never keep the process alive past
        # shutdown — systemd's restart then completes quickly instead of
        # waiting out the stop timeout (which blocks the Volumio plugin).
        thread.daemon = True
        thread.start()

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down")
        stop_event.set()
    finally:
        for thread in threads:
            thread.join(timeout=5)
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()