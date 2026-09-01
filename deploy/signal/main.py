"""
Cloud Run entry point for the Signal maker.

Separate from google_cloud_main.py, which hardcodes the GroupMe scraper config.
The config path is environment-overridable so one image can serve staging and
production without a rebuild.
"""

import base64
import json
import logging
import os

from autogroupchat.scrapers.autoscrapegooglesheets import scrape_using_dict

DEFAULT_CONFIG = "configs/config_googlesheets_signal.json"

global logger
logger = logging.getLogger(__name__)


def get_config(conf_file: str) -> dict:
    """Read a scraper config from disk."""
    with open(conf_file) as f:
        return json.load(f)


def autogroupchat_pubsub(event, context) -> None:
    """
    Triggered by a Pub/Sub message via Eventarc.

    Args:
        event: event payload; `data` is base64-encoded.
        context: event metadata.
    """
    message = base64.b64decode(event["data"]).decode("utf-8")

    config = get_config(os.environ.get("AUTOGROUPCHAT_CONFIG", DEFAULT_CONFIG))

    log_level = logging.DEBUG if config.get("verbose") else logging.INFO
    logging.basicConfig(level=log_level, format=f'[{log_level}] %(message)s')

    logger.info(f"autogroupchat triggered: {message}")

    scrape_using_dict(config)
