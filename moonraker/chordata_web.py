# Chordata page for Moonraker: serves web/index.html at /server/chordata
#
# Install: ./install.sh links this file into ~/moonraker/moonraker/components/
# and adds [chordata_web] to moonraker.conf. The file must stay in the Chordata
# folder (it reads ../web/index.html from there, so updates need no restart).
#
# Copyright (C) 2026  Chordata contributors
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import os

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                    "web", "index.html")


class ChordataWeb:
    def __init__(self, config):
        self.server = config.get_server()
        self.page = os.path.expanduser(config.get("page", PAGE))
        # the page itself is public like Mainsail's; its API calls use Moonraker's normal auth
        self.server.register_endpoint(
            "/server/chordata", ["GET"], self._handle_page, transports=["http"],
            wrap_result=False, content_type="text/html; charset=UTF-8", auth_required=False)

        # the page's "accept" while G-code is busy listening (a G-code command would wait for it)
        self.server.register_endpoint(
            "/server/chordata/accept", ["POST"], self._handle_accept)
        # the page's Reset history, also while a listen runs
        self.server.register_endpoint(
            "/server/chordata/history_reset", ["POST"], self._handle_history_reset)

    async def _handle_accept(self, web_request):
        kapis = self.server.lookup_component("klippy_apis")
        return await kapis._send_klippy_request("chordata/accept", {})

    async def _handle_history_reset(self, web_request):
        kapis = self.server.lookup_component("klippy_apis")
        return await kapis._send_klippy_request("chordata/history_reset", {})

    async def _handle_page(self, web_request):
        eventloop = self.server.get_event_loop()
        return await eventloop.run_in_thread(self._read)

    def _read(self):
        with open(self.page, "rb") as f:
            return f.read()


def load_component(config):
    return ChordataWeb(config)
