from flocker.testtools import TestCase

from benchmark.scenarios import RateMeasurer


class RateMeasurerTest(TestCase):
    """
    RateMeasurer tests.
    """

    def send_requests(self, rate_measurer, num_requests, num_samples):
        """
        Helper function that will send the desired number of request.

        :param rate_measurer: The `RateMeasurer` we are testing.
        :param num_requests: The number of request we want to send.
        :param num_samples: The number of samples to collect.
        """
        for i in range(num_samples * num_requests):
            rate_measurer.request_sent()

    def receive_requests(self, rate_measurer, num_requests, num_samples):
        """
        Helper function that will receive the desired number of requests.

        :param rate_measurer: The `RateMeasurer` we are testing.
        :param num_requests: The number of request we want to receive.
        :param num_samples: The number of samples to collect.
        """
        ignored = ""
        for i in range(num_samples):
            for i in range(num_requests):
                rate_measurer.response_received(ignored)
            rate_measurer.update_rate()

    def failed_requests(self, rate_measurer, num_failures, num_samples):
        """
        Helper function that will result the desired number of response
        failures.

        :param rate_measurer: The `RateMeasurer` we are testing.
        :param num_failures: The number of requests we want to fail.
        :param num_samples: The number of samples to collect.
        """
        result = None
        for i in range(num_samples):
            for i in range(num_failures):
                rate_measurer.request_failed(result)
            rate_measurer.update_rate()

    def increase_rate(self, rate_measurer, num_requests, num_samples):
        """
        Helper function that will increase the rate, sending the
        desired number of request, and receiving the same
        amount of them.

        :param rate_measurer: The `RateMeasurer` we are testing.
        :param num_requests: The number of request we want to make.
        :param num_samples: The number of samples to collect.
        """
        self.send_requests(rate_measurer, num_requests, num_samples)
        self.receive_requests(rate_measurer, num_requests, num_samples)

    def test_rate_is_zero_when_no_samples(self):
        """
        When no samples have been collected, the rate should be 0.
        """
        r = RateMeasurer()
        self.assertEqual(r.rate(), 0, "Expected initial rate to be zero")

    def test_rate_is_lower_than_target_when_not_enough_samples(self):
        """
        When the number of samples collected is less than the sample
        size, the rate should be lower than `target_rate`.
        """
        r = RateMeasurer()
        target_rate = 5
        num_samples = r.sample_size - 1

        self.increase_rate(r, target_rate, num_samples)

        self.assertTrue(r.rate() < target_rate)

    def test_rate_is_correct_when_enough_samples(self):
        """
        A RateMeasurer should correctly report the rate when enough
        samples have been collected.
        """
        r = RateMeasurer()
        target_rate = 5

        self.increase_rate(r, target_rate, r.sample_size)

        self.assertEqual(target_rate, r.rate())

    def test_old_samples_are_not_considered(self):
        """
        Old samples should be discarded, meaning that only `sample_size`
        number of requests are considered for the rate, and when receiving
        a new sample, the oldest one is discarded.
        """
        r = RateMeasurer()
        target_rate = 5

        # Generate samples that will achieve a high request rate
        self.increase_rate(r, target_rate * 2, r.sample_size)

        # Generate samples to lower the request rate to the target rate
        self.increase_rate(r, target_rate, r.sample_size)

        self.assertEqual(target_rate, r.rate())

    def test_rate_only_considers_received_samples(self):
        """
        The rate should be based on the number of received requests,
        not the number of sent or failed requests.
        """
        r = RateMeasurer()
        send_request_rate = 100
        failed_request_rate = 10
        receive_request_rate = 5

        self.send_requests(r, send_request_rate, r.sample_size)
        self.failed_requests(r, failed_request_rate, r.sample_size)
        self.receive_requests(r, receive_request_rate, r.sample_size)

        self.assertEqual(receive_request_rate, r.rate())

    def test_outstanding_considers_all_responses(self):
        """
        Requests that fail are considered to be completed requests and
        should be included when calculating the number of outstanding
        requests.
        """
        r = RateMeasurer()

        # Send 25 requests
        self.send_requests(r, 5, r.sample_size)

        # Receive successful responses for 20 of those requests
        self.receive_requests(r, 4, r.sample_size)

        # Mark 5 of the requests as failed
        self.failed_requests(r, 1, r.sample_size)

        self.assertEqual(0, r.outstanding())
