import darkroom_browser.client
from darkroom_browser import client as app_client
# ruleid: darkroom-no-raw-sockets
from darkroom_browser.client import OnSocket as Connection

from . import client as sibling
# ruleid: darkroom-no-raw-sockets
from .client import OnSocket as Local


# ruleid: darkroom-no-raw-sockets
class ThroughAnAlias(Connection):
    pass


# ruleid: darkroom-no-raw-sockets
class ThroughTheModule(app_client.OnSocket):
    pass


# ruleid: darkroom-no-raw-sockets
class ThroughTheFullName(darkroom_browser.client.OnSocket):
    def connect(self) -> None:
        # ruleid: darkroom-no-raw-sockets
        super(darkroom_browser.client.OnSocket, self).connect()


# ruleid: darkroom-no-raw-sockets
class ThroughARelativeModule(sibling.OnSocket):
    pass


def reach_the_app(path: str) -> object:
    # ok: darkroom-no-raw-sockets
    connection = Connection(path, 30.0)
    # ok: darkroom-no-raw-sockets
    connection.request("POST", "/internal/worker/heartbeat")
    return connection.getresponse()


def reach_the_app_through_the_module(path: str) -> object:
    # ok: darkroom-no-raw-sockets
    return app_client.OnSocket(path, 30.0)
