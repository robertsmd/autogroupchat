import sys
import os.path
import logging
import argparse
import datetime
import time
import uuid

import requests
from requests.exceptions import HTTPError

from autogroupchat.makers.automakegroupchat import AutoMakeGroupChat, MESSAGE_ALWAYS_SEND

global logger
logger = logging.getLogger(__name__)

GROUPME_API = "https://api.groupme.com/v3"


# ---------------------------------------------------------------------------
# Minimal GroupMe REST client — replaces groupy.client.Client
# See https://dev.groupme.com/docs/v3
# ---------------------------------------------------------------------------

class _GroupMeClient:
    def __init__(self, token):
        self.token = token
        self._me = None

    def _request(self, method, path, **kwargs):
        params = kwargs.pop("params", {}) or {}
        params["token"] = self.token
        r = requests.request(
            method, f"{GROUPME_API}{path}",
            params=params, timeout=30, **kwargs,
        )
        r.raise_for_status()
        if not r.content:
            return None
        body = r.json()
        return body.get("response")

    @property
    def me(self):
        if self._me is None:
            self._me = self._request("GET", "/users/me")
        return self._me

    def list_groups(self):
        results, page = [], 1
        while True:
            batch = self._request(
                "GET", "/groups",
                params={"page": page, "per_page": 100},
            ) or []
            if not batch:
                break
            results.extend(Group(self, g) for g in batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    def create_group(self, name, **kwargs):
        payload = {"name": name, **kwargs}
        return Group(self, self._request("POST", "/groups", json=payload))

    def get_group(self, group_id):
        return Group(self, self._request("GET", f"/groups/{group_id}"))


class _AddedMember:
    def __init__(self, data):
        self.data = data
        self.user_id = data.get("user_id")
        self.nickname = data.get("nickname")


class _AddMembersResult:
    def __init__(self, payload):
        self.members = [_AddedMember(m) for m in (payload.get("members") or [])]
        self.failures = payload.get("failures") or []


class _AddMembersRequest:
    """
    Mirrors groupy's AddMembersRequest: GroupMe's add-members endpoint is
    asynchronous and returns a results_id that you poll.
    """

    def __init__(self, group, results_id):
        self.group = group
        self.results_id = results_id
        self._ready = False
        self._payload = None

    def _fetch(self):
        try:
            self._payload = self.group.client._request(
                "GET",
                f"/groups/{self.group.id}/members/results/{self.results_id}",
            )
            self._ready = True
        except HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (404, 503):
                # results not yet ready
                self._ready = False
                return
            raise

    def is_ready(self):
        if not self._ready:
            self._fetch()
        return self._ready

    # legacy alias used by change_group_owner
    def check_if_ready(self):
        return self.is_ready()

    @property
    def results(self):
        if not self._ready:
            self._fetch()
        return _AddMembersResult(self._payload or {})

    # legacy alias
    def get(self):
        return self.results


class Group:
    def __init__(self, client, data):
        self.client = client
        self.data = data or {}

    @property
    def id(self):
        return self.data["id"]

    @property
    def is_mine(self):
        return str(self.data.get("creator_user_id")) == str(self.client.me["id"])

    def _my_membership_id(self):
        my_user_id = str(self.client.me["id"])
        # the create-group response only includes the creator in members;
        # for arbitrary groups we may need to refresh first.
        members = self.data.get("members") or []
        if not any(str(m.get("user_id")) == my_user_id for m in members):
            self.refresh()
            members = self.data.get("members") or []
        for m in members:
            if str(m.get("user_id")) == my_user_id:
                return m.get("id")
        return None

    def refresh(self):
        self.data = self.client._request("GET", f"/groups/{self.id}") or self.data
        return self

    def update(self, **kwargs):
        new_data = self.client._request(
            "POST", f"/groups/{self.id}/update", json=kwargs,
        )
        if new_data:
            self.data = new_data
        return self

    def update_membership(self, nickname):
        membership_id = self._my_membership_id()
        if not membership_id:
            return None
        return self.client._request(
            "POST",
            f"/groups/{self.id}/memberships/{membership_id}/update",
            json={"membership": {"nickname": nickname}},
        )

    def add_member(self, nickname, phone_number=None, email=None, user_id=None):
        member = {"nickname": nickname, "guid": str(uuid.uuid4())}
        if phone_number:
            member["phone_number"] = phone_number
        if email:
            member["email"] = email
        if user_id:
            member["user_id"] = user_id

        resp = self.client._request(
            "POST", f"/groups/{self.id}/members/add",
            json={"members": [member]},
        )
        return _AddMembersRequest(self, resp["results_id"])

    def change_owner(self, owner_user_id):
        return self.client._request(
            "POST", "/groups/change_owners",
            json={"requests": [{
                "group_id": str(self.id),
                "owner_id": str(owner_user_id),
            }]},
        )

    def destroy(self):
        return self.client._request("POST", f"/groups/{self.id}/destroy")

    def leave(self):
        membership_id = self._my_membership_id()
        if not membership_id:
            return None
        return self.client._request(
            "POST",
            f"/groups/{self.id}/members/{membership_id}/remove",
        )

    def post(self, text):
        return self.client._request(
            "POST", f"/groups/{self.id}/messages",
            json={"message": {"source_guid": str(uuid.uuid4()), "text": text}},
        )

    def __repr__(self):
        return f"<Group id={self.data.get('id')} name={self.data.get('name')!r}>"


# ---------------------------------------------------------------------------
# AutoMakeGroupMe — public surface unchanged from the GroupyAPI version
# ---------------------------------------------------------------------------

class AutoMakeGroupMe(AutoMakeGroupChat):
    '''
    GroupMe automation backend. Talks to the GroupMe REST API directly
    (https://dev.groupme.com/docs/v3) instead of using the abandoned
    GroupyAPI wrapper.
    '''

    def __init__(self, *args, **kwargs):
        super(AutoMakeGroupMe, self).__init__(*args, **kwargs)
        self.groupme_token = self.config['groupme_token']
        self.autogroupchat_name = "AutoGroupMe"

        self.client = _GroupMeClient(self.groupme_token)

    def _catch_bad_response(self, func, *args, **kwargs):
        retval = None
        # loop for making sure the call succeeds.
        # GroupMe's API occasionally returns transient 4xx/5xx; we retry.
        while True:
            try:
                retval = func(*args, **kwargs)

                # async results: wait for them to be ready
                if hasattr(retval, "is_ready"):
                    while not retval.is_ready():
                        time.sleep(0.5)

                # surface failures from async add-member operations
                if hasattr(retval, "results"):
                    if retval.results.failures:
                        raise Exception(
                            f"{func} call returned failure: {retval.results.failures}")

                break
            except HTTPError:
                time.sleep(0.5)
        return retval

    def purge_groups(self, group_delete_age_days: int = 30):
        for g in self.client.list_groups():
            if g.data.get('description') == MESSAGE_ALWAYS_SEND:
                created_ms = g.data['created_at']
                created_datetime = datetime.datetime.fromtimestamp(created_ms)
                now_datetime = datetime.datetime.today()
                timedelta = datetime.timedelta(days=int(group_delete_age_days))
                if (now_datetime - created_datetime) > timedelta:
                    if g.is_mine:
                        logger.info(
                            f"Found group {g} with description {g.data['description']} older than {timedelta}. Destroying group.")
                        g.destroy()
                    else:
                        logger.info(
                            f"Found group {g} with description {g.data['description']} older than {timedelta}. Leaving group.")
                        g.leave()

    def create_group(self, group_name: str, image_url: str = None, description: str = None):
        new_group = self._catch_bad_response(
            self.client.create_group, name=group_name)

        # refresh to pick up the membership entry for the creator
        self._catch_bad_response(self.client.get_group, new_group.id)
        new_group.refresh()

        self._catch_bad_response(
            new_group.update_membership, self.autogroupchat_name)

        if image_url:
            self._catch_bad_response(
                new_group.update, image_url=image_url, office_mode=False)
        if description:
            self._catch_bad_response(
                new_group.update, description=description, office_mode=False)

        return new_group

    def add_member_group(self, group: Group, member_display_name: str, member_number: str):
        try:
            return self._catch_bad_response(
                group.add_member, member_display_name, phone_number=member_number)
        except Exception as e:
            if any(member_display_name in str(a) for a in e.args):
                logger.error(
                    f"Error adding member: '{member_display_name}' ({member_number})")
            else:
                raise

    def change_group_owner(self, group: Group, name: str, phone_number: str):
        '''
        Call `change_group_owner` BEFORE `add_member_group`; otherwise the
        new owner is already a member and the add fails.
        '''
        member_add_request = self._catch_bad_response(
            group.add_member, name, phone_number=phone_number)

        while True:
            if self._catch_bad_response(member_add_request.check_if_ready):
                break

        member_add_result = self._catch_bad_response(member_add_request.get)
        member_add_success = member_add_result.members

        if len(member_add_success) == 1:
            admin_id = member_add_success[0].user_id
            self._catch_bad_response(group.change_owner, admin_id)

    def remove_self_group(self, group):
        self._catch_bad_response(group.leave)

    def send_message_to_group(self, group, message):
        self._catch_bad_response(group.post, message)


def run(args):
    members = {m.split(":")[0]: m.split(":")[1] for m in args.members if m}

    admin = {args.admin.split(":")[0]: args.admin.split(":")[1]} \
        if args.admin else {}

    gc_class = getattr(sys.modules[__name__], args.group_creation_class, None)
    if not gc_class:
        raise Exception(
            f"Invalid group creation class: args.group_creation_class={args.group_creation_class}, gc_class={gc_class}")
    assert issubclass(gc_class, AutoMakeGroupChat) \
        and "gc_class must be subclass of AutoGroupChat"

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
    parser.add_argument("-g", "--config-file",
                        default=f"{os.path.dirname(__file__)}/../../configs/config_groupme.json",
                        help="json configuration file specifying credentials")
    parser.add_argument("group_name")
    parser.add_argument("members", nargs="+", help="members of the group")
    parser.add_argument("-a", "--admin", default={}, help="admins of the group")
    parser.add_argument("-s", "--startup-messages", nargs="+", default=[],
                        help="Messages to send after forming the group")
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

    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()
    sys.exit()
