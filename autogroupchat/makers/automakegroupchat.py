import json
import logging
import datetime

MESSAGE_ALWAYS_SEND = "Group created by autogroupchat. Please contact s41l8hu2@duck.com with any issues."

global logger
logger = logging.getLogger(__name__)


class AutoMakeGroupChat:
    def __init__(self, config_file):
        self.config_file = config_file
        self.MESSAGE_ALWAYS_SEND = MESSAGE_ALWAYS_SEND

        with open(self.config_file) as f:
            self.config = json.load(f)

        self.autogroupchat_name = "AutoGroupChat"

    def create_group(self, group_name: str, image: str, description: str):
        raise NotImplementedError

    def add_members_group(self, group, members: dict):
        for name, number in members.items():
            self.add_member_group(group, name, number)

    def add_member_group(self, name: str, phone_number: str):
        raise NotImplementedError

    def change_group_owner(self, group, name: str, phone_number: str):
        raise NotImplementedError

    def send_message_to_group(self, group, message: str):
        raise NotImplementedError

    def remove_self_group(self, group):
        raise NotImplementedError

    def purge_groups(self, group_delete_age_days: int):
        raise NotImplementedError

    def group_startup(clazz,
                      config_file: str,
                      group_name: str,
                      members: dict[str, str],
                      admin: dict[str, str]={},
                      startup_messages: list[str]=[],
                      image: str=None,
                      description: str=None,
                      dont_leave_group: bool=True,
                      group_delete_age_days: int=30):
        agc = clazz(config_file)

        if not description:
            description = MESSAGE_ALWAYS_SEND

        # create group
        group = agc.create_group(group_name, image, description)

        # add admin first because it will fail if the admin is already a member
        # of the group
        if admin:
            assert len(admin) == 1 and "Only one owner is allowed per group."

            admin_name, admin_phone_number = admin.popitem()
            # add admin to the group
            agc.add_member_group(group, admin_name, admin_phone_number)
            # make admin new owner
            agc.change_group_owner(group, admin_name, admin_phone_number)

        # add members
        for member_name, member_number in members.items():
            agc.add_member_group(group, member_name, member_number)

        # send MESSAGE_ALWAYS_SEND
        agc.send_message_to_group(group, MESSAGE_ALWAYS_SEND)

        # default startup message
        if not startup_messages:
            startup_messages = [
                f"Welcome to {group_name}. {description}"]
        # send startup messages
        for m in startup_messages:
            agc.send_message_to_group(group, m)

        if not dont_leave_group:
            # remove self from group
            agc.remove_self_group(group)

        # purge groups made by AutoGroupChat older than 30 days
        agc.purge_groups(group_delete_age_days=group_delete_age_days)

        return group