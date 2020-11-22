import asyncio
import logging
import sys
import tempfile
from typing import List, Dict, Union, Optional

from galaxy.http import create_client_session

from galaxy.api.errors import AccessDenied, InvalidCredentials, AuthenticationRequired
from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.consts import Platform, LicenseType, LocalGameState, OSCompatibility
from galaxy.api.types import (
    NextStep,
    Authentication,
    LocalGame,
    Game,
    LicenseInfo,
    GameTime,
)

import butlerd


class ItchIntegration(Plugin):
    def __init__(self, reader, writer, token):
        super().__init__(
            Platform.ItchIo,  # Choose platform from available list
            "0.1",  # Version
            reader,
            writer,
            token,
        )
        self.game_ids = []
        self.butler = butlerd.AioButler()
        self.butler.event_handlers.append(self._handle_events)
        self.butler_sem = asyncio.Semaphore()

    async def _handle_events(self, event: str, params: Dict[str, str]):
        if event == "Log":
            return

        logging.info("Event %r: %r", event, params)

        if event == "Downloads.Drive.Finished" or (
            event == "TaskSucceeded" and params.get("type") == "uninstall"
        ):
            self.create_task(self._update_local_games(), "local games update")

    async def _update_local_games(self):
        logging.info("Updating locally installed games after (un-)install...")
        for g in await self.get_local_games():
            self.update_local_game_status(g)

    async def authenticate(self, stored_credentials=None):
        profiles = (await self.butler.call("Profile.List"))["profiles"]

        # User needs to authenticate via itch.io client first
        if not profiles:
            raise InvalidCredentials()

        self.profile_id = profiles[0]["id"]

        return Authentication(
            profiles[0]["id"],
            profiles[0]["user"]["displayName"] or profiles[0]["user"]["username"],
        )

    async def fetch_records(self, args: Dict[Any]):
        offset = 0
        records = []

        while True:
            chunk = await self.butler.call(
                "Fetch.GameRecords",
                {"profileId": self.profile_id, "limit": 100, "offset": offset, **args},
            )

            if not chunk["records"]:
                break

            records.extend(chunk["records"])
            offset += len(chunk["records"])

        return records

    async def get_owned_games(self):
        all_games = await self.fetch_records(
            {"source": "owned", "filters": {"classification": "game"},}
        )

        return [
            Game(
                str(g["id"]), g["title"], None, LicenseInfo(LicenseType.SinglePurchase)
            )
            for g in all_games
        ]

    async def get_local_games(self):
        all_games = await self.fetch_records(
            {"source": "owned", "filters": {"classification": "game"}}
        )

        return [
            LocalGame(
                str(g["id"]),
                LocalGameState(
                    LocalGameState.Installed
                    if g.get("installedAt")
                    else LocalGameState.None_
                ),
            )
            for g in all_games
        ]

    async def launch_game(self, game_id: str):
        caves = await self.butler.call(
            "Fetch.Caves", {"filters": {"gameId": int(game_id),}}
        )

        await self.butler.call(
            "Launch",
            {"caveId": caves["items"][0]["id"], "prereqsDir": tempfile.gettempdir()},
        )

    async def install_game(self, game_id: str):

        game = (await self.butler.call("Fetch.Game", {"gameId": int(game_id)}))["game"]

        uploads = (await self.butler.call("Game.FindUploads", {"game": game}))[
            "uploads"
        ]

        install_locations = (await self.butler.call("Install.Locations.List"))[
            "installLocations"
        ]

        await self.butler.call(
            "Install.Queue",
            {
                "game": game,
                "upload": uploads[0],
                "installLocationId": install_locations[0]["id"],
                "queueDownload": True,
            },
        )

        await self.butler.call("Downloads.Drive")

    async def uninstall_game(self, game_id: str):
        caves = await self.butler.call(
            "Fetch.Caves", {"filters": {"gameId": int(game_id)}}
        )

        for cave in caves["items"]:
            await self.butler.call("Uninstall.Perform", {"caveId": cave["id"]})

        # Trigger local games update regardless of uninstall status to
        # fixup possible state discrepancy
        self.create_task(self._update_local_games(), "local games update")

    async def get_os_compatibility(self, game_id: str, context):
        async with self.butler_sem:
            game = (await self.butler.call("Fetch.Game", {"gameId": int(game_id)}))[
                "game"
            ]

            return OSCompatibility(
                (
                    OSCompatibility.Windows.value
                    if game["platforms"].get("windows")
                    else 0
                )
                | (OSCompatibility.MacOS.value if game["platforms"].get("osx") else 0)
                | (OSCompatibility.Linux.value if game["platforms"].get("linux") else 0)
            )


def main():
    create_and_run_plugin(ItchIntegration, sys.argv)


if __name__ == "__main__":
    main()
