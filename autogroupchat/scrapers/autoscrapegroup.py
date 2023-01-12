import json
import os.path
import logging
import argparse
import datetime
import pandas as pd

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from autogroupchat.makers.automakegroupme import AutoMakeGroupMe

global logger
logger = logging.getLogger(__name__)


class AutoScrapeGroup:
    def __init__(self, spreadsheet, spreadsheet_worksheet, spreadsheet_range, api_config, token_config=None, scopes=None, *args, **kwargs):
        self.spreadsheet = spreadsheet
        self.spreadsheet_worksheet = spreadsheet_worksheet
        self.spreadsheet_range = spreadsheet_range
        self.api_config_file = api_config
        self.token_config_file = token_config
        self.scopes = scopes

        if not self.token_config_file:
            filename, ext = api_config_file.split(os.path.extsep)
            self.token_config_file = f"{filename}_token{os.path.extsep}{ext}"

        self.args = args
        self.auth()
        self.df = self.get_df()

        self.info = {}
        self.groups_to_create = []
        self.contacts = {}

        self.process_df()

    def auth(self):
        raise NotImplementedError

    def get_df(self):
        raise NotImplementedError

    def process_df(self):
        for col in self.df:
            self.process_column(col)

    def create_groups(self, clazz, cls_config_file):
        for group in self.groups_to_create:
            group_metadata = self.info.copy()
            date = group[0]
            group_metadata['date'] = date
            time = group[1]
            group_metadata['time'] = time
            group_metadata['group_name'] = group_metadata['group_name'].format(
                date=date, time=time)
            group_metadata['startup_messages'] = [i.format(**group_metadata) 
                                                  for i in group_metadata.get(
                                                      'startup_messages', [])]

            members = {}
            # ignore the first two lines, they hold date and time respectively
            for row in range(2, len(group)):
                # if cell isn't empty, it's a mark that the person is included
                if group[row]:
                    member = self.contacts[row]
                    # member is a dictionary, and we want to add it to members
                    # so we use the update method to add/update.
                    members.update(member)

            group_metadata['members'] = members
            logger.info("group_metadata = " +
                        json.dumps(group_metadata, indent=4))
            logger.info(
                f"Calling {clazz}.group_startup for group named {group_metadata['group_name']}")
            logger.debug(f"group_metadata={group_metadata}")
            clazz.group_startup(clazz,
                                cls_config_file,
                                group_metadata['group_name'],
                                group_metadata['members'],
                                group_metadata.get('admin', {}),
                                group_metadata.get('startup_messages', []),
                                group_metadata.get('image', ''),
                                group_metadata.get('description', ''),
                                group_metadata.get('dont_leave_group', True),
                                group_metadata.get('group_delete_age_days', 30),
                                )

    def process_column(self, col_num):
        column = self.df[col_num]
        if not isinstance(column[0], str):
            pass
        elif column[0].lower() == "key":
            self.info = self.get_keyvalue_info(col_num)
        elif column[0].lower() == "value":
            # this is processed with key
            pass
        elif column[0].lower() == "name":
            self.contacts = self.get_contacts(col_num)
        elif column[0].lower() == "number":
            # this is processed with name
            pass
        else:
            try:
                date = datetime.datetime.strptime(column[0], '%m/%d/%Y')
            except ValueError:
                return

            # if it is not already passed
            if date.date() < datetime.date.today():
                logger.debug(
                    f"Column for date {date} is already passed, not creating group.")
                pass
            # if it is scheduled for today
            elif (date.date() - datetime.date.today()) < datetime.timedelta(days=1):
                logger.info(
                    f"Column for date {date} is today, creating group.")
                self.groups_to_create.append(column)
            else:
                logger.debug(
                    f"Column for date {date} is not today, waiting on creating group.")

    def get_keyvalue_info(self, col_num):
        assert self.df[col_num][0].lower() == "key" and \
            self.df[col_num + 1][0].lower() == "value"

        info = {}
        keys = self.df[col_num]
        values = self.df[col_num + 1]
        last_key = ""
        # iterate to longer of keys or values
        for i in range(1, max(len(keys), len(values))):
            # if last_key is set, but current key is not,
            # then values should be set as a dictionary
            if last_key and not keys[i] and values[i]:
                if isinstance(info[last_key], list):
                    # append new value to the list if already a list
                    info[last_key].append(values[i])
                else:
                    # convert to list if not alredy a list
                    info[last_key] = [info[last_key], values[i]]
            if keys[i] and values[i]:
                last_key = keys[i] # set last_key for keys that should have lists of values
                info[keys[i]] = values[i]
        logger.info("info = " + json.dumps(info, indent=4))
        return info

    def get_contacts(self, col_num):
        assert self.df[col_num][0].lower() == "name" and \
            self.df[col_num + 1][0].lower() == "phone"

        contacts = {}
        names = self.df[col_num]
        phones = self.df[col_num + 1]
        for i in range(1, len(names)):
            if names[i] and phones[i]:
                contacts[i] = {names[i]: phones[i]}
        logger.info("contacts = " + json.dumps(contacts, indent=4))
        return contacts
