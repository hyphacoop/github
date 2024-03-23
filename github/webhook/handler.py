# github - A maubot plugin to act as a GitHub client and webhook receiver.
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Dict, Set, Deque, Optional, Any, TYPE_CHECKING
from collections import deque, defaultdict
from uuid import UUID
import asyncio
import logging
import re

from jinja2 import TemplateNotFound
import attr

from mautrix.types import (
    EventID,
    EventType as MautrixEventType,
    Format,
    MessageType,
    ReactionEventContent,
    RelatesTo,
    RelationType,
    RoomID,
    TextMessageEventContent,
)
from mautrix.util.formatter import parse_html

from ..template import TemplateManager, TemplateUtil
from ..api.types import (
    Event,
    EventType,
    MetaAction,
    PushEvent,
    RepositoryAction,
    WorkflowJobEvent,
    expand_enum,
    ACTION_CLASSES,
    OTHER_ENUMS,
)
from .manager import WebhookInfo
from .aggregation import PendingAggregation

if TYPE_CHECKING:
    from ..bot import GitHubBot

spaces = re.compile(" +")
space = " "


class WebhookHandler:
    log: logging.Logger
    bot: 'GitHubBot'
    msgtype: MessageType
    messages: TemplateManager
    templates: TemplateManager
    pending_aggregations: Dict[UUID, Deque[PendingAggregation]]

    def __init__(self, bot: 'GitHubBot') -> None:
        self.bot = bot
        self.log = self.bot.log.getChild("webhook")
        self.msgtype = MessageType(bot.config["message_options.msgtype"]) or MessageType.NOTICE
        PendingAggregation.timeout = int(bot.config["message_options.aggregation_timeout"])
        self.messages = TemplateManager(self.bot.config, "messages")
        self.templates = TemplateManager(self.bot.config, "templates")
        self.pending_aggregations = defaultdict(lambda: deque())

    def reload_config(self) -> None:
        self.messages.reload()
        self.templates.reload()
        self.msgtype = MessageType(self.bot.config["message_options.msgtype"]) or MessageType.NOTICE
        PendingAggregation.timeout = int(self.bot.config["message_options.aggregation_timeout"])

    async def __call__(self, evt_type: EventType, evt: Event, delivery_id: str, info: WebhookInfo
                       ) -> None:
        # Filter

        filter_passed=True
        if (info.label_filter):
            filter_passed=False
            if ("labels" in evt):
                for l in evt.labels:
                    if l.name==info.label_filter:
                        filter_passed=True
            if ("pull_request" in evt):
                for l in evt.pull_request.labels:
                    if l.name==info.label_filter:
                        filter_passed=True

            if ("issue" in evt):
                for l in evt.issue.labels:
                    if l.name==info.label_filter:
                        filter_passed=True
#            else:
#                self.log.debug(f"Received ping for {evt}")

        if evt_type == EventType.PING:
            self.log.debug(f"Received ping for {info}: {evt.zen}")
            self.bot.webhook_manager.set_github_id(info, evt.hook_id)
        elif evt_type == EventType.META and evt.action == MetaAction.DELETED:
            self.log.debug(f"Received delete hook for {info}")
            self.bot.webhook_manager.delete(info.id)
        elif evt_type == EventType.REPOSITORY:
            if evt.action in (RepositoryAction.TRANSFERRED, RepositoryAction.RENAMED):
                action = "transfer" if evt.action == RepositoryAction.TRANSFERRED else "rename"
                name = evt.repository.full_name
                self.log.debug(f"Received {action} hook {info} -> {name}")
                self.bot.webhook_manager.transfer(info, name)
            elif evt.action == RepositoryAction.DELETED:
                self.log.debug(f"Received repo delete hook for {info}")
                self.bot.webhook_manager.delete(info.id)
        elif evt_type == EventType.PUSH and (evt.size is None or evt.distinct_size is None):
            assert isinstance(evt, PushEvent)
            evt.size = len(evt.commits)
            evt.distinct_size = len([commit for commit in evt.commits if commit.distinct])
        elif evt_type == EventType.WORKFLOW_JOB:
            assert isinstance(evt, WorkflowJobEvent)
            push_evt = self.bot.db.get_event(evt.push_id, info.room_id)
            if not push_evt:
                self.bot.log.debug(f"No message found to react to push {evt.push_id}")
                return
            reaction = ReactionEventContent(
                RelatesTo(rel_type=RelationType.ANNOTATION, event_id=push_evt)
            )
            try:
                reaction.relates_to.key = f"{evt.color_circle} {evt.workflow_job.name}"
            except KeyError:
                return
            reaction["xyz.maubot.gitlab.webhook"] = {
                "event_type": evt_type.name,
                **evt.meta,
            }

            prev_reaction = self.bot.db.get_event(evt.reaction_id, info.room_id)
            if prev_reaction:
                await self.bot.client.redact(info.room_id, prev_reaction)
            event_id = await self.bot.client.send_message_event(
                info.room_id, MautrixEventType.REACTION, reaction
            )
            self.bot.db.put_event(
                evt.reaction_id, info.room_id, event_id, merge=prev_reaction is not None
            )

        if filter_passed:
            if PendingAggregation.timeout < 0:
                # Aggregations are disabled
                event_id = await self.send_message(evt_type, evt, info.room_id, {delivery_id})
                if evt_type == EventType.PUSH and event_id:
                    self.bot.db.put_event(evt.message_id, info.room_id, event_id)
                return

            for pending in self.pending_aggregations[info.id]:
                if pending.aggregate(evt_type, evt, delivery_id):
                    return
            asyncio.ensure_future(PendingAggregation(self, evt_type, evt, delivery_id, info).start())
        else:
            self.log.debug(f"Skipping {evt.action}")

    async def send_message(
        self,
        evt_type: EventType,
        evt: Event,
        room_id: RoomID,
        delivery_ids: Set[str],
        aggregation: Optional[Dict[str, Any]] = None,
    ) -> Optional[EventID]:
        try:
            tpl = self.messages[str(evt_type)]
        except TemplateNotFound:
            self.log.debug(f"Unhandled event of type {evt_type} -- {delivery_ids}")
            return None

        aborted = False

        def abort() -> None:
            nonlocal aborted
            aborted = True

        args = {
            **attr.asdict(evt, recurse=False),
            **expand_enum(ACTION_CLASSES.get(evt_type)),
            **OTHER_ENUMS,
            "util": TemplateUtil,
            "abort": abort,
            "aggregation": aggregation,
        }
        args["templates"] = self.templates.proxy(args)
        html = tpl.render(**args)
        if not html or aborted:
            return None
        content = TextMessageEventContent(
            msgtype=self.msgtype,
            format=Format.HTML,
            formatted_body=html,
            body=await parse_html(html.strip()),
        )
        content["xyz.maubot.github.webhook"] = {
            "delivery_ids": list(delivery_ids),
            "event_type": str(evt_type),
            **(evt.meta() if hasattr(evt, "meta") else {}),
        }
        content["com.beeper.linkpreviews"] = []
        return await self.bot.client.send_message(room_id, content)
