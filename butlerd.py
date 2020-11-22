import sys
import os
import pathlib
import json
import logging
import asyncio


logger = logging.getLogger(__name__)


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__("%d: %s" % (code, message))


#
# butlred RPC Reference:
# http://docs.itch.ovh/butlerd/master
#
class AioButler:
    call_id = 0
    proc = None

    def __init__(self):
        self.requests = {}
        self.event_handlers = []
        self.create_task = asyncio.create_task

    async def start(self):
        # TODO: test MacOS support properly
        if sys.platform.startswith("darwin"):
            appdata = pathlib.Path("~/Library/Application Support").expanduser()
        else:
            appdata = pathlib.Path(os.getenv("APPDATA"))

        with open(appdata / "itch/broth/butler/.chosen-version") as fd:
            butler_version = fd.read()

        butler_binary = (
            appdata / "itch/broth/butler/versions" / butler_version / "butler"
        )
        butler_db = appdata / "itch/db/butler.db"

        user_agent = f"gog-galaxy-itch.io/0.0.0 ({sys.platform})"

        launch_invocation = [
            str(butler_binary),
            "--json",
            "daemon",
            "--transport",
            "tcp",
            # "--keep-alive", # ??
            "--dbpath",
            str(butler_db),
            "--address",
            "https://itch.io",
            "--user-agent",
            user_agent,
        ]

        self.proc = await asyncio.create_subprocess_exec(
            *launch_invocation, stdout=asyncio.subprocess.PIPE
        )

        while True:
            l = await self.proc.stdout.readuntil()
            try:
                msg = json.loads(l)
                logger.debug("< [proc] %r", msg)
                if msg["type"] == "butlerd/listen-notification":
                    self.connection_data = {
                        "secret": msg["secret"],
                        "address": msg["tcp"]["address"],
                    }
                    break
            except Exception as exc:
                logger.exception("butlerd response handling failed")

        host, _, port = self.connection_data["address"].rpartition(":")
        self.reader, self.writer = await asyncio.open_connection(host, int(port))

        self.handler_task = self.create_task(self.reader_handler())
        await self.call("Meta.Authenticate", {"secret": self.connection_data["secret"]})

    async def reader_handler(self):
        while True:
            data = json.loads(await self.reader.readuntil())
            logger.debug(f"< [rpc] {data}")
            if data.get("id") in self.requests:
                await self.requests[data.get("id")].put(data)
            elif data.get("method"):
                for h in self.event_handlers:
                    try:
                        await h(data.get("method"), data.get("params"))
                    except:
                        logger.exception("event_handler failed")

    async def shutdown(self):
        self.handler_task.cancel()

        self.writer.close()
        await self.writer.wait_closed()

        self.proc.terminate()
        await self.proc.wait()

    async def call(self, method, params={}):
        if not self.proc:
            await self.start()

        self.call_id += 1
        call_id = self.call_id

        try:
            self.requests[call_id] = asyncio.Queue()
            payload = {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": method,
                "params": params,
            }
            logger.debug(f"> [rpc] {payload}")
            message = json.dumps(payload) + "\n"
            self.writer.write(message.encode())
            await self.writer.drain()
            data = await self.requests[call_id].get()
        finally:
            self.requests.pop(call_id)

        if "error" in data:
            raise RPCError(data["error"]["code"], data["error"]["message"])
        else:
            return data.get("result")

    @classmethod
    async def perform_tests(cls):
        logging.basicConfig(level=logging.DEBUG)
        b = cls()
        print("Starting...")
        # Optional, butlerd is started on first call:
        # await b.start()

        try:
            print("Started!")
            profiles = (await b.call("Profile.List"))["profiles"]
            if not profiles:
                print("oops :(")
                return

            # caves = (await b.call("Fetch.Caves"))["items"]
            # print(caves)
            # print(await b.call("Fetch.Commons"))
            # await b.call(
            #    "Launch",
            #    {"caveId": caves[0]["id"], "prereqsDir": tempfile.gettempdir()},
            # )
            # await asyncio.sleep(60.0)
            # return

            profile_id = profiles[0]["id"]
            games = await b.call(
                "Fetch.GameRecords",
                {"profileId": profile_id, "source": "owned", "limit": 100,},
            )  # ["records"]
            print(games)

            print(await b.call("Fetch.ProfileGames", {"profileId": profile_id}))
            return

            uploads = (await b.call("Game.FindUploads", {"game": games[0]}))["uploads"]

            install_locations = (await b.call("Install.Locations.List"))[
                "installLocations"
            ]

            await b.call(
                "Install.Queue",
                {
                    "game": games[0],
                    "upload": uploads[0],
                    "installLocationId": install_locations[0]["id"],
                    "queueDownload": True,
                },
            )

            await b.call("Downloads.Drive")

            await asyncio.sleep(60.0)
        finally:
            print("Finished!")
            await b.shutdown()


if __name__ == "__main__":
    asyncio.run(AioButler.perform_tests())
