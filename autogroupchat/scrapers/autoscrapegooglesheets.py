import sys
import json
import os.path
import logging
import argparse
import datetime
import pandas as pd

import gspread

from autogroupchat.makers.automakegroupchat import AutoMakeGroupChat
from autogroupchat.makers.automakegroupme import AutoMakeGroupMe
from autogroupchat.scrapers.autoscrapegroup import AutoScrapeGroup

# requires spreadsheets and drive scopes
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly',
          'https://www.googleapis.com/auth/drive.readonly']

global logger
logger = logging.getLogger(__name__)


class AutoScrapeGoogleSheets(AutoScrapeGroup):
    def __init__(self, *args, **kwargs):
        super(AutoScrapeGoogleSheets, self).__init__(*args, **kwargs)

    def auth(self):
        self.gspread_client = gspread.service_account(filename=self.api_config_file, scopes=SCOPES)

    def get_df(self):
        spreadsheet = self.gspread_client.open(self.spreadsheet)
        worksheet = spreadsheet.worksheet(self.spreadsheet_worksheet)
        values = worksheet.get(self.spreadsheet_range)

        if not values:
            print(
                f'No data found in spreadsheet `{self.spreadsheet}` sheet `{self.spreadsheet_worksheet}` range `{self.spreadsheet_range}`.')
            return

        df = pd.DataFrame(values)
        return df


def scrape_using_dict(args):
    # turn string into class with validation of subclass and existence
    gc_class_string = args['group_creation_class']
    gc_class = getattr(sys.modules[__name__], gc_class_string, None)
    if not gc_class:
        raise Exception(
            f"Invalid group creation class: args.group_creation_class={gc_class_string}, gc_class={gc_class}")
    assert issubclass(
        gc_class, AutoMakeGroupChat) and "gc_class must be subclass of AutoGroupChat"

    # overwrite arg field with the actual class after validation
    args['group_creation_class'] = gc_class

    asg = AutoScrapeGoogleSheets(
        spreadsheet=args['spreadsheet'],
        spreadsheet_worksheet=args['worksheet'],
        spreadsheet_range=args['range'],
        api_config=args['api_config'],
        scopes=args['scopes'])

    asg.create_groups(args['group_creation_class'],
                      args['group_creation_config'],)


def run(args):
    args_dict = args.__dict__

    # remove func key because it isnt serializable
    args_dict.pop('func')

    scrape_using_dict(args_dict)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument("-g", "--api-config", default=f"{os.path.dirname(__file__)}/../../configs/config_googleapi.json",
                        help="json configuration file specifying credentials")
    parser.add_argument("spreadsheet")
    parser.add_argument("-w", "--worksheet", default="Sheet1")
    parser.add_argument("-r", "--range", default="")
    parser.add_argument("-s", "--scopes", nargs="+",
                        default=SCOPES)
    parser.add_argument("--group-creation-class",
                        default="AutoMakeGroupMe")
    parser.add_argument("--group-creation-config",
                        default=f"{os.path.dirname(__file__)}/../../configs/config_groupme.json")
    parser.set_defaults(func=run)

    args = parser.parse_args(["<spreadsheet_id>"])

    log_level = logging.INFO
    if args.verbose:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, format=f'[{log_level}] %(message)s')
    logger = logging.getLogger(__name__)

    #---------------------------------------
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()
    #---------------------------------------
