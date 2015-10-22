from random import randrange
import time

from twisted.internet import reactor
from twisted.internet.task import LoopingCall, deferLater

from twisted.web.client import getPage
from twisted.trial.unittest import TestCase

from flocker.common import gather_deferreds


def url_requester():
    while True:
        url = "http://localhost:8080?{}".format(randrange(10 ** 6))
        yield getPage(url)


class RateMeasurer(object):
    _sample_size = 5
    _count = 0
    _last_second = int(time.time())

    def __init__(self):
        self._counts = []

    def new_sample(self):
        now = int(time.time())
        if now > self._last_second:
            self._counts.append(self._count)
            self._counts = self._counts[-self._sample_size:]
            self._last_second = now
            self._count = 0
        self._count += 1

    @property
    def rate(self):
        return float(sum(self._counts) / float(len(self._counts)))


class LoadGenerator(object):
    def __init__(self, request_generator, req_per_sec):
        self._request_generator = request_generator
        self.req_per_sec = req_per_sec
        self._loops = []
        self._starts = []

    def start(self):
        for i in range(self.req_per_sec):
            loop = LoopingCall(
                self._request_generator.next,
            )
            self._loops.append(loop)
            started = loop.start(interval=1)
            self._starts.append(started)

    def stop(self):
        for loop in self._loops:
            loop.stop()
        return gather_deferreds(self._starts)


class TestResponseTime(TestCase):
    def setUp(self):
        self.rate_measurer = RateMeasurer()

        def sample_and_return(result):
            self.rate_measurer.new_sample()
            return result

        def request_and_measure():
            for d in url_requester():
                d.addCallback(sample_and_return)
                yield d

        self.load_generator = LoadGenerator(
            request_generator=request_and_measure(),
            req_per_sec=10,
        )
        self.load_generator.start()

    def tearDown(self):
        return self.load_generator.stop()

    def test_rate(self):
        def do_assert():
            self.assertEqual(10, self.rate_measurer.rate)
        return deferLater(reactor, 10, do_assert)
