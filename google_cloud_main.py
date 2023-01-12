import json
import base64
import logging

from autogroupchat.scrapers.autoscrapegooglesheets import scrape_using_dict

global logger


def get_config(conf_file):
    with open(conf_file) as f:
        conf = json.load(f)
    return conf


def autogroupchat_pubsub(event, context):
    """Triggered from a message on a Cloud Pub/Sub topic.
    Args:
         event (dict): Event payload.
         context (google.cloud.functions.Context): Metadata for the event.
    """
    pubsub_message = base64.b64decode(event['data']).decode('utf-8')
    print(pubsub_message)
    config = get_config("configs/config_googlesheets_groupme.json")

    log_level = logging.INFO
    if config['verbose']:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, format=f'[{log_level}] %(message)s')
    logger = logging.getLogger(__name__)

    scrape_using_dict(config)


if __name__ == "__main__":
    autogroupchat_pubsub(
        {"data": "VGVzdGluZyBBdXRvR3JvdXBDaGF0Li4u"}, None)
