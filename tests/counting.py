"""Synthetic services that count identity lookups and submissions, for tests."""

from dataclasses import replace

from alsense_chat.adapters import synthetic
from alsense_chat.adapters.synthetic.services import SyntheticIdentity, SyntheticReports, SyntheticSupport


class CountingServices:
    def __init__(self):
        self.lookups = 0
        self.submissions = 0
        counter = self

        class Identity(SyntheticIdentity):
            def users_me(self, identity):
                counter.lookups += 1
                return super().users_me(identity)

        class Reports(SyntheticReports):
            def submit_report(self, *args):
                counter.submissions += 1
                return super().submit_report(*args)

        class Support(SyntheticSupport):
            def submit_request(self, *args):
                counter.submissions += 1
                return super().submit_request(*args)

        self.services = replace(synthetic.build(), identity=Identity(), reports=Reports(), support=Support())
