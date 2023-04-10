import sys
import os.path
import logging
import argparse
import datetime

from groupy.client import Client
from groupy.api.groups import Group
from groupy.exceptions import BadResponse
from requests.exceptions import HTTPError

from autogroupchat.makers.automakegroupchat import AutoMakeGroupChat, MESSAGE_ALWAYS_SEND

global logger
logger = logging.getLogger(__name__)


class AutoMakeGroupMe(AutoMakeGroupChat):
    '''
    to access API documentationo, go to 
    https://groupy.readthedocs.io/en/latest/pages/api.html
    '''

    def __init__(self, *args, **kwargs):
        super(AutoMakeGroupMe, self).__init__(*args, **kwargs)
        self.groupme_token = self.config['groupme_token']
        self.autogroupchat_name = "AutoGroupMe"

        self.client = Client.from_token(self.groupme_token)

    def _catch_bad_response(self, func, *args, **kwargs):
        retval = None
        # loop for making sure group has been created successfully.
        # there's a little race condition with the API here that this fixes
        while 1:
            try:
                retval = func(*args, **kwargs)

                # wait for results to be ready
                if hasattr(retval, "is_ready"):
                    while not retval.is_ready():
                        pass

                if hasattr(retval, "results"):
                    if retval.results.failures:
                        raise Exception(f"{func} call returned failure: {retval.results.failures}")

                # break out of loop if success
                break
            except (BadResponse, HTTPError):
                pass
        return retval

    def purge_groups(self, group_delete_age_days: int=30):
        for g in self.client.groups.list_all():
            # if description shows that it's an AutoGroupChat group
            if g.data['description'] == MESSAGE_ALWAYS_SEND:
                created_ms = g.data['created_at']
                created_datetime = datetime.datetime.fromtimestamp(created_ms)
                now_datetime = datetime.datetime.today()
                # if is older than timedelta, defaults to 30 days
                timedelta = datetime.timedelta(days=int(group_delete_age_days))
                if (now_datetime - created_datetime) > timedelta:
                    if g.is_mine:
                        # I am the owner, destroy
                        logger.info(
                            f"Found group {g} with description {g.data['description']} older than {timedelta}. Destroying group.")
                        g.destroy()
                    else:
                        # i am not the owner, leave
                        logger.info(
                            f"Found group {g} with description {g.data['description']} older than {timedelta}. Leaving group.")
                        g.leave()

    def create_group(self, group_name: str, image_url: str=None, description: str=None):
        # create group
        new_group = self._catch_bad_response(
            self.client.groups.create, name=group_name)

        self._catch_bad_response(self.client.groups.get, new_group.id)

        # rename self to autogroupme
        self._catch_bad_response(
            new_group.update_membership, self.autogroupchat_name)

        # update group photo
        if image_url:
            self._catch_bad_response(new_group.update, image_url=image_url, office_mode=False)
        # update group description
        if description:
            self._catch_bad_response(new_group.update, description=description, office_mode=False)
        # new_group_id = new_group.id
        return new_group

    def add_member_group(self, group: Group, member_display_name: str, member_number: str):
        return self._catch_bad_response(group.memberships.add, member_display_name, phone_number=member_number)

    def change_group_owner(self, group: Group, name: str, phone_number: str):
        '''
        Be sure to call `change_group_owner` before `add_member_group`
        because it will fail if the admin is already a member of the group
        '''
        member_add_request = self._catch_bad_response(group.memberships.add,
                                                      name, phone_number=phone_number)
        while 1:
            if self._catch_bad_response(member_add_request.check_if_ready):
                break
        member_add_result = self._catch_bad_response(member_add_request.get)
        member_add_success = member_add_result.members
        # member_add_failures = member_add_result.failures

        if len(member_add_success) == 1:
            admin_id = member_add_success[0].user_id
            self._catch_bad_response(group.change_owners, admin_id)

    def remove_self_group(self, group):
        self._catch_bad_response(group.leave)

    def send_message_to_group(self, group, message):
        self._catch_bad_response(group.post, message)


def run(args):
    members = {m.split(":")[0]: m.split(":")[1] for m in args.members if m}

    admin = {args.admin.split(":")[0]: args.admin.split(":")[
        1]} if args.admin else {}

    # turn string into class with validation of subclass and existence
    gc_class = getattr(sys.modules[__name__], args.group_creation_class, None)
    if not gc_class:
        raise Exception(
            f"Invalid group creation class: args.group_creation_class={args.group_creation_class}, gc_class={gc_class}")
    assert issubclass(
        gc_class, AutoMakeGroupChat) and "gc_class must be subclass of AutoGroupChat"

    gc_class.group_startup(
        gc_class,
        args.config_file,
        args.group_name,
        members,
        admin,
        args.startup_messages,
        args.image,
        args.description,
        args.dont_leave_group,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument("-g", "--config-file", default=f"{os.path.dirname(__file__)}/../../configs/config_groupme.json",
                        help="json configuration file specifying credentials")
    parser.add_argument("group_name")
    parser.add_argument("members", nargs="+", help="members of the group")
    parser.add_argument("-a", "--admin", default={},
                        help="admins of the group")
    parser.add_argument("-s", "--startup-messages", nargs="+",
                        default=[], help="Messages to send after forming the group")
    parser.add_argument("--image", default="")
    parser.add_argument("--description", default=MESSAGE_ALWAYS_SEND,
                        help="Don't recommend making this dynamic. This is assumed to be constant for purging old groups")
    parser.add_argument("--dont-leave-group", action='store_true')
    parser.add_argument("--group-creation-class", default="AutoMakeGroupMe")
    parser.set_defaults(func=run)

    args = parser.parse_args(
        ["test_group", "gv:+<phone_number>", "--dont-leave-group"])

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
    sys.exit()
