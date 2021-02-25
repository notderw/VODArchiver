from datetime import datetime, timedelta

import backoff
import aiohttp
from aiohttp import ClientSession

from pydantic import BaseModel

from logger import makeLogger

log = makeLogger(__file__)

class Stream(BaseModel):
    id: str
    type: str
    title: str
    user_login: str
    user_name: str
    game_id: str
    game_name: str
    started_at: datetime
    viewer_count: int


class Token:
    token = None
    expires_at = 0
    def __init__(self, data = None) -> None:
        if data:
            self.token = data.get('access_token')
            self.expires_at = datetime.utcnow() + timedelta(seconds=data.get('expires_in'))

    @property
    def expired(self):
        return self.token is None or self.expires_at < datetime.utcnow()

class Twitch(ClientSession):
    __token = Token()
    def __init__(self, client_id, client_secret, scope, *args, **kwargs):
        self.__client_id = client_id
        self.__client_secret = client_secret
        self.__scope = scope

        super().__init__(*args, headers={
            "Client-ID": self.__client_id
        }, **kwargs)

    @backoff.on_exception(backoff.expo, aiohttp.ClientError, max_time=60)
    async def _request(self, *args, **kwargs):
        if self.__token.expired:
            await self._get_token()

        if not 'headers' in kwargs:
            kwargs["headers"] = {}

        kwargs["headers"]["Authorization"] = f"Bearer {self.__token.token}"

        r = await super()._request(*args, raise_for_status=True, **kwargs)

        rl_limit = r.headers.get('Ratelimit-Limit')
        rl_remaining = r.headers.get('Ratelimit-Remaining')
        rl_reset = int(r.headers.get('Ratelimit-Reset'))
        log.debug(f'Ratelmit {rl_remaining}/{rl_limit} - Resets in {rl_reset - datetime.now().timestamp()}')

        return r

    async def _get_token(self):
        async with ClientSession() as session:
            r = await session.post('https://id.twitch.tv/oauth2/token', data = {
                "client_id": self.__client_id,
                "client_secret": self.__client_secret,
                "grant_type": "client_credentials",
                "scope": self.__scope
            })

            self.__token = Token(await r.json())

            log.debug("Got new token")

    async def get_user(self, user):
        async with self.get('https://api.twitch.tv/helix/users', params = {
            "login": user
        }) as r:
            return (await r.json()).get('data')

    async def get_streams(self, **kwargs):
        async with self.get("https://api.twitch.tv/helix/streams", params = kwargs) as r:
            for s in (await r.json()).get('data'):
                yield Stream(**s)
