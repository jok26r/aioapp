import asyncio
import aioredis
import aiozipkin as az
import aiozipkin.span as azs
from ..app import Component
from ..error import PrepareError


class Redis(Component):
    def __init__(self, dsn: str, pool_min_size: int = 1,
                 pool_max_size: int = 10,
                 connect_max_attempts: int = 10,
                 connect_retry_delay: float = 1.0) -> None:
        super(Redis, self).__init__()
        self.dsn = dsn
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self.pool = None

    async def prepare(self):
        self.app.log_info("Connecting to %s" % self.dsn)
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                self.app.log_info("Connected to %s" % self.dsn)
                return
            except Exception as e:
                self.app.log_err(str(e))
                await asyncio.sleep(self.connect_retry_delay)
        raise PrepareError("Could not connect to %s" % self.dsn)

    async def _connect(self):
        self.pool = await aioredis.create_pool(self.dsn,
                                               minsize=self.pool_min_size,
                                               maxsize=self.pool_max_size,
                                               loop=self.loop)

    async def start(self):
        pass

    async def stop(self):
        if self.pool:
            self.app.log_info("Disconnecting from %s" % self.dsn)
            self.pool.close()
            await self.pool.wait_closed()

    def connection(self,
                   context_span: azs.SpanAbc) -> 'ConnectionContextManager':
        return ConnectionContextManager(self, context_span)

    async def execute(self, context_span: azs.SpanAbc, id: str,
                      command: str, *args):
        async with self.connection(context_span) as conn:
            return await conn.execute(context_span, id, command, *args)


class ConnectionContextManager:
    def __init__(self, redis, context_span) -> None:
        self._redis = redis
        self._conn = None
        self._context_span = context_span

    async def __aenter__(self) -> 'Connection':
        with self._context_span.tracer.new_child(
                self._context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("redis:Acquire")
            span.remote_endpoint("redis")
            span.tag('redis.size_before', self._redis.pool.size)
            span.tag('redis.free_before', self._redis.pool.freesize)
            self._conn = await self._redis.pool.acquire()
        c = Connection(self._redis, self._conn)
        return c

    async def __aexit__(self, exc_type, exc, tb):
        self._redis.pool.release(self._conn)


class Connection:
    def __init__(self, redis, conn) -> None:
        """
        :type redis: Redis
        """
        self._redis = redis
        self._conn = conn
        self.loop = self._redis.loop

    @property
    def pubsub_channels(self):
        return self._conn.pubsub_channels

    async def execute(self, context_span: azs.SpanAbc, id: str,
                      command: str, *args):
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("redis:%s" % id)
            span.remote_endpoint("redis")
            span.tag("redis.command", command)
            span.annotate(repr(args))
            res = await self._conn.execute(command, *args)
        return res

    async def execute_pubsub(self, context_span: azs.SpanAbc, id: str,
                             command, *channels_or_patterns):
        with context_span.tracer.new_child(context_span.context) as span:
            span.kind(az.CLIENT)
            span.name("redis:%s" % id)
            span.remote_endpoint("redis")
            span.tag("redis.pubsub", command)
            span.annotate(repr(channels_or_patterns))
            res = await self._conn.execute_pubsub(command,
                                                  *channels_or_patterns)
        return res
