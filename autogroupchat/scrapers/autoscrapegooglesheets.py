import sys
import json
import os.path
import logging
import argparse
import datetime
import pandas as pd

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from autogroupchat.makers.automakegroupchat import AutoMakeGroupChat
from autogroupchat.makers.automakegroupme import AutoMakeGroupMe
from autogroupchat.scrapers.autoscrapegroup import AutoScrapeGroup

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

global logger
logger = logging.getLogger(__name__)


class AutoScrapeGoogleSheets(AutoScrapeGroup):
    def __init__(self, *args, **kwargs):
        super(AutoScrapeGoogleSheets, self).__init__(*args, **kwargs)

    def auth(self):
        def user_interaction_auth():
            flow = InstalledAppFlow.from_client_secrets_file(
                self.api_config_file, SCOPES)
            self.creds = flow.run_local_server(port=0)

        self.creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists(self.token_config_file):
            try:
                self.creds = Credentials.from_authorized_user_file(
                    self.token_config_file, self.scopes)
            except ValueError:
                pass
        # If there are no (valid) credentials available, let the user log in.
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                try:
                    self.creds.refresh(Request())
                except RefreshError:
                    user_interaction_auth()
            else:
                user_interaction_auth()
            # Save the credentials for the next run
            try:
                with open(self.token_config_file, 'w') as f:
                    json.dump(json.loads(self.creds.to_json()), f, indent=4)
            except OSError:
                logger.error(f"File {self.token_config_file} is not writable. Not writing token to disk.")

    def get_df(self):
        try:
            service = build('sheets', 'v4', credentials=self.creds)

            # Call the Sheets API
            sheet = service.spreadsheets()
            result = sheet.values().get(spreadsheetId=self.spreadsheet,
                                        range=self.spreadsheet_range).execute()
            values = result.get('values', [])

            if not values:
                print(
                    f'No data found in spreadsheet `{self.spreadsheet}` range `{self.spreadsheet_range}`.')
                return

            df = pd.DataFrame(values)
            return df

        except HttpError as err:
            print(err)


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
        spreadsheet_range=args['range'],
        api_config=args['api_config'],
        token_config=args['token_config'],
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
    parser.add_argument("-t", "--token-config",
                        default=f"{os.path.dirname(__file__)}/../../configs/config_googleapi_token.json")
    parser.add_argument("spreadsheet")
    parser.add_argument("-r", "--range", default="Sheet1")
    parser.add_argument("-s", "--scopes", nargs="+",
                        default=['https://www.googleapis.com/auth/spreadsheets.readonly'])
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
