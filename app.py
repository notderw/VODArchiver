import os
import asyncio
import json
import pathlib

from enum import Enum
from typing import Optional, List
from datetime import datetime, timedelta

import aiofiles
import aiogoogle
from aiogoogle import Aiogoogle

from pydantic import BaseModel, SecretStr

from twitch import Twitch, Stream
from logger import makeLogger

log = makeLogger(__file__)

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")


google = Aiogoogle(
    client_creds = {
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET
    })


class Status(Enum):
    OFFLINE = 0
    LIVE = 1

class GameUpdate(BaseModel):
    game_name: str
    timestamp: timedelta

class CurrentStream(Stream):
    saving_at: datetime = datetime.utcnow()
    timeline: List[GameUpdate] = []

    @classmethod
    def new(cls, stream: Stream):
        return cls(
            id = stream.id,
            type = stream.type,
            title = stream.title,
            user_login = stream.user_login,
            user_name = stream.user_name,
            game_id = 0,
            game_name = "",
            started_at = stream.started_at,
            viewer_count = stream.viewer_count
        )

    def update(self, game_id, game_name, viewer_count, **kwargs):
        if game_id != self.game_id:
            self.game_id = game_id
            self.game_name = game_name

            self.timeline.append(GameUpdate(
                game_name = self.game_name,
                timestamp = timedelta(seconds = int((datetime.utcnow() - self.saving_at).total_seconds()))
            ))

            log.info(f'Channel {self.user_name} game changed to {self.game_name}')

        if viewer_count != self.viewer_count:
            self.viewer_count = viewer_count

    @property
    def file(self):
        return f"{self.user_login}_{self.id}.mp4"

    async def save(self):
        cmd = [
            "streamlink",
            "--loglevel", "info",
            "--twitch-disable-ads",
            "--twitch-disable-hosting",
            "--twitch-disable-reruns",
            "--default-stream", "best",
            "--retry-streams", "10",
            "--retry-open", "30",
            "--url", f"twitch.tv/{self.user_login}",
            "--output", self.file,
            # "--stdout"
        ]

        proc = await asyncio.create_subprocess_shell(
            ' '.join(cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        async def printer(stream, log):
            async for line in stream:
                log(f'[{self.user_login}]{line.decode().strip()}')

        await asyncio.wait([
            printer(proc.stdout, log.info),
            printer(proc.stderr, log.error)
        ])

        await proc.wait()


class Monitor(BaseModel):
    status: Status = Status.OFFLINE
    twitch: str
    g_refresh_token: SecretStr
    stream: Optional[CurrentStream] = None

    async def update(self, stream: Stream):
        currently_live = stream and stream.type == "live"

        if self.status is Status.OFFLINE and currently_live: # channel was offline but is now online
            log.info(f'{self.twitch} is now live: {stream.title}')
            self.status = Status.LIVE
            self.stream = CurrentStream.new(stream)

            self.stream.update(**stream.dict())

            asyncio.create_task(self.start())

        elif self.status is Status.LIVE and currently_live: # channel was live and is still live
            self.stream.update(**stream.dict())


        elif self.status is Status.LIVE and not currently_live: # channel was live but now is not
            log.info(f'Channel {self.twitch} is now offline')
            self.status = Status.OFFLINE
            # self.stream = None

    async def start(self):
        try:
            await self.stream.save()

        except Exception as e:
            log.error(f'Error in save: {e}')
            import traceback
            traceback.print_exc()

        finally:
            await self.upload()

    async def upload(self):
        log.info("INIT YOUTUBE UPLOAD")

        user_creds = aiogoogle.auth.creds.UserCreds(refresh_token = self.g_refresh_token.get_secret_value())

        yt = await google.discover('youtube', 'v3')

        r = await google.as_user(
            yt.channels.list(part = "snippet", mine = True),
            user_creds = user_creds
        )

        c = r["items"][0]
        log.info(f'Logged in to YouTube as "{c["snippet"]["title"]}" ({c["id"]})')

        # either aiogoogle docs suck or I'm sending this request wrong, but I cant send it all in "insert" for some reason
        r = await google.as_user(
            yt.videos.insert(part = "id", upload_file=pathlib.Path(self.stream.file).absolute()),
            user_creds = user_creds
        )

        log.info(r)

        stream_date = self.stream.started_at.strftime("%Y/%m/%d")

        desc = ""
        desc += f'Title: {self.stream.title}\n'
        desc += f'Streamed {stream_date} on https://www.twitch.tv/jericho/\n'
        desc += f'\nTimeline:\n'

        for t in self.stream.timeline:
            desc += f'[{t.timestamp}] {t.game_name}\n'

        data = {
            "id": r.get("id"),
            "snippet": {
                "title": f'[{stream_date}] {self.stream.title}',
                "description": desc,
                "categoryId": "24"
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False
            }
        }

        r = await google.as_user(
            yt.videos.update(part = ",".join(data.keys()), json = data),
            user_creds = user_creds
        )

        log.info(r)

        log.info(f'Removing file {self.stream.file}')
        await aiofiles.os.remove(self.stream.file)


        # Theoretically, we might be able to use something like this to just stream the content
        # Straight to the YouTube videos.insert endpoint without putting anything on disk.
        # Set streamlink to output to stdout and using the stream from subprocess.PIPE as "payload"
        # However since I can't even get the upload to work correctly using aiogoogle in one api call,
        # I will not be investigating this at this time.

        #     ##################################
        #     endpoint = yt.videos.insert(part = "id")
        #     async with ClientSession() as session:
        #         async with session.post(endpoint.url, data = payload, headers = {
        #             "Authorization": f"Bearer {google.user_creds['access_token']}",
        #             "Content-Type": f"multipart/related; boundary={payload.boundary}",
        #             "Accept": "application/json"}, timeout = None) as r:
        #
        #                 print("#"*10, len(payload))
        #                 log.info(f'{r.status} {await r.json()}')
        #     ##################################


class TwitchArchiver:
    monitored = List[Monitor]
    def __init__(self):

        with open("monitored.json", "r") as f:
            self.monitored = [Monitor(**c) for c in json.load(f)]

    async def run(self):
        self.tw = Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, "")

        log.info("Watching")

        while True:
            channels = [c.twitch for c in self.monitored]
            streams = [item async for item in self.tw.get_streams(user_login=channels)]

            # reduce API hits when a stream is live to avoid hitting cache
            # this is lazy but since we only have 1 stream its fine for now
            if "live" in list(s.type for s in streams):
                refresh = 30
            else:
                refresh = 2

            log.debug(f'Refresh interval: {refresh}s')

            for mon in self.monitored:
                if stream := list(filter(lambda x: x.user_login == mon.twitch, streams)):
                    await mon.update(stream[0])

            await asyncio.sleep(refresh)

    async def close(self):
        await self.tw.close()


ta = TwitchArchiver()

loop = asyncio.get_event_loop()
try:
    loop.run_until_complete(ta.run())

except KeyboardInterrupt:
    pass

finally:
    loop.run_until_complete(ta.close())
