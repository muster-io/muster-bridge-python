# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""A bridge in the role gateway whose phone lives in memory and writes every notification to its log.

Muster, muster bridge check or a person with curl can try the role without a phone provider:

    printf example-token > /tmp/bt
    python examples/gateway_bridge.py --token-file /tmp/bt

Every notification the bot is asked to send is printed. Outside the protocol it serves
POST /demo/acknowledge {"notification_id"}, which acknowledges that notification as the person called would, by
pressing 1. Unlike the protocol, which needs the token, it has no authentication of its own: the example listens on
loopback by default.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route

import muster_bridge
from muster_bridge import (
    AcknowledgedNotification,
    Bot,
    BridgeApp,
    Config,
    Limits,
    NotificationInput,
    NotificationSent,
    NotOfferedError,
    PushTarget,
    UnknownNotificationError,
)

BOT = "main"
MAX_DEMO_BODY = 64 << 10

log = logging.getLogger("gateway_bridge")


def read_secret(path: str) -> str:
    """Return the token or push secret a file holds, without the white space around it."""
    secret = Path(path).read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError("it holds no secret")
    return secret


def parse_listen(listen: str) -> tuple[str, int]:
    """Return the host, without the brackets of an IPv6 address, and the port of --listen."""
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"--listen: {listen!r} is not host:port")
    return host.removeprefix("[").removesuffix("]"), int(port)


@dataclass
class MemoryPhone:
    """Implements Gateway: the notifications it was asked to send, numbered n1, n2 and so on."""

    sent: dict[str, NotificationInput] = field(default_factory=dict)
    last: int = 0

    async def send_notification(self, bot: str, notification: NotificationInput) -> NotificationSent:
        """Print the notification and answer its id."""
        self.last += 1
        notification_id = f"n{self.last}"
        self.sent[notification_id] = notification
        action = notification.action.label if notification.action else "none"
        # The example prints the contact and the text to show the call; a real gateway keeps them out of its logs.
        log.info(
            "notification sent: bot=%s notification_id=%s contact=%s language=%s action=%r text=%r",
            bot,
            notification_id,
            notification.contact,
            notification.language.value,
            action,
            notification.text,
        )
        return NotificationSent(notification_id=notification_id, replayed=False)

    async def answer(self, bot: str, event: AcknowledgedNotification, text: str) -> None:
        """Print what Muster says to the person who acknowledged."""
        log.info("answer read out: bot=%s notification_id=%s text=%r", bot, event.notification_id, text)

    async def demo_acknowledge(self, request: Request, bridge: BridgeApp) -> Response:
        """Acknowledge a notification: what the person on the phone does by pressing 1."""
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_DEMO_BODY:
                return PlainTextResponse("the body is larger than 64 KiB", HTTPStatus.BAD_REQUEST)
        try:
            ack = json.loads(body)
        except ValueError:
            ack = None
        if not isinstance(ack, dict) or not isinstance(ack.get("notification_id"), str):
            return PlainTextResponse('the body is not {"notification_id": …}', HTTPStatus.BAD_REQUEST)
        notification_id = ack["notification_id"]
        try:
            await bridge.acknowledge(BOT, notification_id, chat=f"call-{notification_id}")
        except UnknownNotificationError:
            return PlainTextResponse(
                f"no notification {json.dumps(notification_id)} with an action is kept", HTTPStatus.BAD_REQUEST
            )
        except NotOfferedError:
            return PlainTextResponse("this bridge does not have the role gateway", HTTPStatus.CONFLICT)
        except Exception:
            log.exception("the acknowledgement could not be reported")
            return PlainTextResponse("the acknowledgement could not be reported", HTTPStatus.INTERNAL_SERVER_ERROR)
        log.info("notification acknowledged: bot=%s notification_id=%s", BOT, notification_id)
        return Response(status_code=HTTPStatus.NO_CONTENT)


def configure(args: argparse.Namespace) -> tuple[Config, MemoryPhone]:
    """Return the bridge's Config and its in-memory phone, from the flags."""
    try:
        token = read_secret(args.token_file)
    except (OSError, ValueError) as error:
        raise ValueError(f"--token-file: {error}") from None
    if (args.push_url is None) != (args.push_secret_file is None):
        raise ValueError("--push-url and --push-secret-file go together")
    push = None
    if args.push_url is not None:
        try:
            push = PushTarget(url=args.push_url, secret=read_secret(args.push_secret_file))
        except (OSError, ValueError) as error:
            raise ValueError(f"--push-secret-file: {error}") from None
    phone = MemoryPhone()
    config = Config(
        name="muster-bridge-python gateway example",
        version=muster_bridge.__version__,
        messenger_name="Memory phone",
        bots=[Bot(name=BOT, token=token, push=push)],
        limits=Limits(max_body_length=4000, edit_max_age_seconds=None),
        roles=["gateway"],
        gateway=phone,
    )
    return config, phone


def main() -> None:
    """Serve the protocol and /demo/acknowledge until SIGINT or SIGTERM, then stop the server and the pushers."""
    parser = argparse.ArgumentParser(description="A gateway bridge whose phone lives in memory.")
    parser.add_argument("--listen", default="127.0.0.1:18095", help="the address to listen on")
    parser.add_argument("--token-file", required=True, help="the file that holds the bot's token")
    parser.add_argument("--push-url", help="where to push the events, the callback URL of the Bridge Connection")
    parser.add_argument("--push-secret-file", help="the file that holds the push secret, whsec_…")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        host, port = parse_listen(args.listen)
        config, phone = configure(args)
        bridge = BridgeApp(config)
    except ValueError as error:
        log.error("stopped: %s", error)
        sys.exit(2)

    async def demo_acknowledge(request: Request) -> Response:
        return await phone.demo_acknowledge(request, bridge)

    app = Starlette(
        routes=[Route("/demo/acknowledge", demo_acknowledge, methods=["POST"]), Mount("/", app=bridge)],
        lifespan=bridge.lifespan,
    )
    uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
