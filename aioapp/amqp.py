import logging
import asyncio
import traceback
from functools import partial
import aioamqp.protocol
import aioamqp.channel
import aioamqp.exceptions
import aioamqp.envelope
import aioamqp.properties as amqp_prop
import aioamqp
from typing import Optional, List, Callable
from .app import Component
from .error import PrepareError
from .misc import mask_url_pwd, async_call
import aiozipkin as az
import aiozipkin.span as azs
import aiozipkin.aiohttp_helpers as azah

#
aioamqp.channel.logger.level = logging.CRITICAL
aioamqp.protocol.logger.level = logging.CRITICAL

STOP_TIMEOUT = 5


class Channel:
    name: Optional[str] = None
    amqp: Optional['Amqp'] = None
    channel: 'aioamqp.channel.Channel' = None
    _cons_cnt: int = 0
    _cons_fut: asyncio.Future = None
    _cons_tag: str = None
    _stopping: bool = False

    async def open(self):
        try:
            await self.close()
        except Exception as err:
            if self.amqp:
                self.amqp.app.log_err(err)
        self.channel = await self.amqp._protocol.channel()

    async def close(self):
        if self.channel:
            await self.channel.close()

    async def publish(self, context_span: azs.SpanAbc, payload: bytes,
                      exchange_name: str, routing_key: str,
                      properties: Optional[dict] = None,
                      mandatory: bool = False, immediate: bool = False,
                      span_params: Optional[dict] = None,
                      propagate_trace: bool = True):
        span = None
        if context_span:
            span = context_span.new_child(
                'amqp:publish {} {}'.format(exchange_name, routing_key),
                azah.CLIENT
            )
            if propagate_trace:
                headers = context_span.context.make_headers()
                properties = properties or {}
                if 'headers' not in properties:
                    properties['headers'] = {}
                properties['headers'].update(headers)
            span.start()
        try:
            await self.channel.basic_publish(payload, exchange_name,
                                             routing_key,
                                             properties=properties,
                                             mandatory=mandatory,
                                             immediate=immediate)
            if span:
                span.finish()
        except Exception as err:
            if span:
                span.tag('error.message', str(err))
                span.annotate(traceback.format_exc())
                span.finish(exception=err)
            raise

    async def consume(self, fn, queue_name='', consumer_tag='', no_local=False,
                      no_ack=False, exclusive=False, no_wait=False,
                      arguments=None):
        if not asyncio.iscoroutinefunction(fn):
            raise UserWarning()

        callback = partial(self._consume_callback, fn)
        self._cons_fut = asyncio.Future(loop=self.amqp.loop)
        res = await self.channel.basic_consume(
            callback, queue_name=queue_name, consumer_tag=consumer_tag,
            no_local=no_local, no_ack=no_ack, exclusive=exclusive,
            no_wait=no_wait, arguments=arguments)
        self._cons_tag = res['consumer_tag']

    async def ack(self, context_span: azs.SpanAbc, delivery_tag: str,
                  multiple: bool = False):
        await self._ack_nack(context_span, True, delivery_tag, multiple)

    async def nack(self, context_span: azs.SpanAbc, delivery_tag: str,
                   multiple: bool = False):
        await self._ack_nack(context_span, False, delivery_tag, multiple)

    async def _ack_nack(self, context_span: azs.SpanAbc, is_ack: bool,
                        delivery_tag: str, multiple: bool = False):
        span = None
        if context_span:
            span = context_span.new_child('amqp:ack', azah.CLIENT)
            span.start()
        try:
            if is_ack:
                await self.channel.basic_client_ack(delivery_tag=delivery_tag,
                                                    multiple=multiple)
            else:
                await self.channel.basic_client_nack(delivery_tag=delivery_tag,
                                                     multiple=multiple)
            if span:
                span.finish()
        except Exception as err:
            if span:
                span.tag('error.message', str(err))
                span.annotate(traceback.format_exc())
                span.finish(exception=err)
            raise

    async def _consume_callback_handler(self, callback: Callable,
                                        channel: aioamqp.channel.Channel,
                                        body: bytes,
                                        envelope: aioamqp.envelope.Envelope,
                                        properties: amqp_prop.Properties):
        async_call(self.amqp.loop,
                   partial(
                       self._consume_callback, callback, channel, body,
                       envelope, properties))

    async def _consume_callback(self, callback: Callable,
                                channel: aioamqp.channel.Channel, body: bytes,
                                envelope: aioamqp.envelope.Envelope,
                                properties: amqp_prop.Properties):
        if not channel.is_open:
            return

        self._cons_cnt += 1
        try:
            span = None
            if self.amqp.app.tracer:
                context = az.make_context(properties.headers)
                if context is None:
                    sampled = azah.parse_sampled(properties.headers)
                    debug = azah.parse_debug(properties.headers)
                    span = self.amqp.app.tracer.new_trace(sampled=sampled,
                                                          debug=debug)
                else:
                    span = self.amqp.app.tracer.join_span(context)
                span.name('amqp:message')
                span.kind(azah.SERVER)
                if envelope.routing_key:
                    span.tag('amqp.routing_key', envelope.routing_key)
                if envelope.exchange_name:
                    span.tag('amqp.exchange_name', envelope.exchange_name)
                if properties.delivery_mode:
                    span.tag('amqp.delivery_mode',
                             properties.delivery_mode)
                if properties.expiration:
                    span.tag('amqp.expiration', properties.expiration)
                span.start()
            try:
                await callback(span, channel, body, envelope, properties)

                if span:
                    span.finish()
            except Exception as err:
                if span:
                    span.tag('error.message', str(err))
                    span.annotate(traceback.format_exc())
                    span.finish(exception=err)
                self.amqp.app.log_err(err)
                raise

        finally:
            self._cons_cnt -= 1
            if self._stopping and self._cons_cnt == 0 and self._cons_fut:
                self._cons_fut.set_result(1)

    async def start(self):
        self._stopping = False
        await self.open()

    async def stop(self):
        self._stopping = True
        if self._cons_tag:
            try:
                await self.channel.basic_cancel(self._cons_tag)
            except Exception as err:
                self.amqp.app.log_err(err)
            finally:
                self._cons_tag = None
        try:
            if self._cons_cnt > 0 and self._cons_fut:
                await asyncio.wait_for(self._cons_fut, timeout=STOP_TIMEOUT)
        finally:
            await self.close()

    async def _safe_declare_queue(self, queue_name=None, passive=False,
                                  durable=False, exclusive=False,
                                  auto_delete=False, no_wait=False,
                                  arguments=None) -> Optional[dict]:
        ch = await self.amqp._protocol.channel()
        try:
            res = await ch.queue_declare(queue_name=queue_name,
                                         passive=passive, durable=durable,
                                         exclusive=exclusive,
                                         auto_delete=auto_delete,
                                         no_wait=no_wait, arguments=arguments)
            return res
        except aioamqp.exceptions.ChannelClosed as e:
            if e.code == 406:
                # ignore error if attributes not match
                return None
            else:
                raise
        finally:
            if ch.is_open:
                await ch.close()

    async def _safe_declare_exchange(self, exchange_name, type_name,
                                     passive=False, durable=False,
                                     auto_delete=False, no_wait=False,
                                     arguments=None) -> Optional[dict]:
        ch = await self.amqp._protocol.channel()
        try:
            res = await ch.exchange_declare(exchange_name=exchange_name,
                                            type_name=type_name,
                                            passive=passive, durable=durable,
                                            auto_delete=auto_delete,
                                            no_wait=no_wait,
                                            arguments=arguments)
            return res
        except aioamqp.exceptions.ChannelClosed as e:
            if e.code == 406:
                # ignore error if attributes not match
                return None
            else:
                raise
        finally:
            if ch.is_open:
                await ch.close()


class Amqp(Component):

    def __init__(self, url: Optional[str] = None,
                 channels: List[Channel] = None,
                 heartbeat: int = 5,
                 connect_max_attempts: int = 10,
                 connect_retry_delay: float = 1.0) -> None:
        super().__init__()
        self.url = url
        self.connect_max_attempts = connect_max_attempts
        self.connect_retry_delay = connect_retry_delay
        self.heartbeat = heartbeat
        self._started = False
        self._shutting_down = False
        self._consuming = False
        self._transport = None
        self._protocol: aioamqp.protocol.AmqpProtocol = None
        self._channels = channels
        if channels:
            names = [ch.name for ch in channels if ch.name is not None]
            if len(names) != len(set(names)):
                raise UserWarning('There are not unique names in the channel '
                                  'names: %s' % (','.join(names)))

    @property
    def _masked_url(self) -> Optional[str]:
        return mask_url_pwd(self.url)

    async def prepare(self) -> None:
        for i in range(self.connect_max_attempts):
            try:
                await self._connect()
                return
            except Exception as e:
                self.app.log_err(str(e))
                await asyncio.sleep(self.connect_retry_delay,
                                    loop=self.app.loop)
        raise PrepareError("Could not connect to %s" % self._masked_url)

    async def start(self) -> None:
        self._started = True
        await self._start_channels()

    async def stop(self) -> None:
        self._started = False
        self._shutting_down = True
        await self._stop_channels()
        await self._cleanup()

    async def _connect(self):
        await self._cleanup()
        self.app.log_info("Connecting to %s" % self._masked_url)
        (self._transport,
         self._protocol) = await aioamqp.from_url(self.url,
                                                  on_error=self._con_error,
                                                  heartbeat=self.heartbeat)
        self.app.log_info("Connected to %s" % self._masked_url)

        if self._started:
            await self._start_channels()

    async def _con_error(self, error):
        if error and not self._shutting_down:
            self.app.log_err(error)
        if self._shutting_down or not self._started:
            return

        async_call(self.loop, self._reconnect,
                   delay=self.connect_retry_delay)

    async def _reconnect(self):
        try:
            await self._connect()
        except Exception as e:
            self.app.log_err(e)
            async_call(self.loop, self._reconnect,
                       delay=self.connect_retry_delay)

    async def _cleanup(self):
        if self._protocol:
            try:
                await self._protocol.close()
            except Exception as e:
                self.app.log_err(e)
            self._protocol = None
            self._transport = None

    async def _start_channels(self):
        self._consuming = True
        if self._channels:
            for ch in self._channels:
                ch.amqp = self
                await ch.start()

    async def _stop_channels(self):
        self._consuming = False
        if self._channels:
            for ch in reversed(self._channels):
                await ch.stop()

    def channel(self, name: str) -> Optional['Channel']:
        if self._channels:
            for ch in self._channels:
                if ch.name is not None and ch.name == name:
                    return ch
        return None
