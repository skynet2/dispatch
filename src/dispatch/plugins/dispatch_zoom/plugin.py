"""
.. module: dispatch.plugins.dispatch_zoom.plugin
    :platform: Unix
    :copyright: (c) 2019 by HashCorp Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Will Bengtson <wbengtson@hashicorp.com>
"""

import logging
import random

from dispatch.decorators import apply, counter, timer
from dispatch.plugins import dispatch_zoom as zoom_plugin
from dispatch.plugins.bases import ConferencePlugin

from .config import ZoomConfiguration
from .client import ZoomClient

log = logging.getLogger(__name__)


def gen_conference_challenge(length: int):
    """Generate a random challenge for Zoom."""
    if length > 10:
        length = 10
    field = "abcdefghijklmnopqrstuvwxyz01234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.sample(field, length))


def delete_meeting(client, event_id: int):
    return client.delete("/meetings/{}".format(event_id))


def create_meeting(
    client,
    user_id: str,
    name: str,
    description: str = None,
    title: str = None,
    duration: int = 60000,  # duration in mins ~6 weeks
):
    """Create a Zoom Meeting."""
    body = {
        "topic": title if title else f"Situation Room for {name}",
        "agenda": description if description else f"Situation Room for {name}. Please join.",
        "duration": duration,
        "password": gen_conference_challenge(8),
        "settings": {"join_before_host": True},
    }

    return client.post("/users/{}/meetings".format(user_id), data=body)


@apply(timer, exclude=["__init__"])
@apply(counter, exclude=["__init__"])
class ZoomConferencePlugin(ConferencePlugin):
    title = "Zoom Plugin - Conference Management"
    slug = "zoom-conference"
    description = "Uses Zoom to manage conference meetings."
    version = zoom_plugin.__version__

    author = "HashCorp"
    author_url = "https://github.com/netflix/dispatch.git"

    def __init__(self):
        self.configuration_schema = ZoomConfiguration

    def create(
        self, name: str, description: str = None, title: str = None, participants: list[str] = None
    ):
        """Create a new event."""
        client = ZoomClient(
            self.configuration.api_key, self.configuration.api_secret.get_secret_value()
        )

        conference_response = create_meeting(
            client, self.configuration.api_user_id, name, description=description, title=title, duration=self.configuration.default_duration_minutes
        )

        conference_json = conference_response.json()

        return {
            "weblink": conference_json.get("join_url", "https://zoom.us"),
            "id": conference_json.get("id", "1"),
            "challenge": conference_json.get("password", "123"),
        }

    def delete(self, event_id: str):
        """Deletes an existing event."""
        client = ZoomClient(
            self.configuration.api_key, self.configuration.api_secret.get_secret_value()
        )
        delete_meeting(client, event_id)

    def add_participant(self, event_id: str, participant: str):
        """Adds a new participant to event."""
        return

    def remove_participant(self, event_id: str, participant: str):
        """Removes a participant from event."""
        return
